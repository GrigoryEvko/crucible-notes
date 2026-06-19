# CCE (Compute-DMA) In-Transfer Compute

A **CCE** transfer ("Compute-CopyEngine" / compute-DMA) is an SDMA descriptor whose
meta-control word tags it as a **compute** op rather than a plain copy. The CDMA channel
(= the DDMA data-path plus the CCE block, [The DMA / Descriptor / Memory Subsystem](descriptor-model.md))
reads `N` source streams off the memory-to-stream (M2S) leg, applies a per-element ALU op
**while the data is in flight**, and writes the single reduced/scaled result down the
stream-to-memory (S2M) leg. There is no separate compute pass and no Q7-firmware
arithmetic loop — the FMA / ADD / MIN / MAX / dtype-convert is a hardware function of the
SDMA M2S data-path, configured entirely by fields packed into the 16-byte descriptor and
its meta-control word.

> **NOTE — the math is in silicon, not firmware.** The shipped GPSIMD custom-op firmware
> archive (`libnrtucode`) ships **no** FMA/reduce kernel; only the descriptor *builders*
> live in code. Every encoder discussed here (`al_sdma_m2s_build_*`) emits a 16-byte
> descriptor and returns — the accumulate/multiply/clamp happens in the CCE ALU when the
> engine drains the ring. A reimplementation therefore reproduces the **descriptor bit
> layout**, not an arithmetic loop. *(HIGH / OBSERVED — descriptor-only encoders; no
> compute kernel in the firmware archive.)*

This page is the per-element reduce arithmetic the collective AllReduce applies in-transfer
— the "reduce (ALU op)" leg of the firmware ring. All facts below are grounded in the host
runtime `libnrt.so.2.31.24.0` (x86-64, BuildID `8bb57aba…c102e`, with `debug_info`):
`.text`/`.rodata` are mapped VMA == file-offset, so addresses index the file directly.

---

## 1. The CCE op + dtype taxonomy and the host→device remap

### 1.1 Two op enums, one remap table

The host *kbin* op enum and the device *SDMA* op enum are differently ordered. Both are
recovered byte-exact from the shipped `enums.json`:

```c
// kbin_dma_desc_op (enums.json)               // SDMA_CCETYPE (enums.json)
KBIN_DMA_DESC_OP_INVALID   = (u64)-1;          SDMA_CCETYPE_ADD = 0;
KBIN_DMA_DESC_OP_COPY      = 0;                 SDMA_CCETYPE_FMA = 1;
KBIN_DMA_DESC_OP_FMA       = 1;                 SDMA_CCETYPE_MAX = 2;
KBIN_DMA_DESC_OP_ADD       = 2;                 SDMA_CCETYPE_MIN = 3;
KBIN_DMA_DESC_OP_MIN       = 3;                 SDMA_CCETYPE_EXT = 4;  // device-only
KBIN_DMA_DESC_OP_MAX       = 4;                 SDMA_CCETYPE_GCE = 5;  // device-only
KBIN_DMA_DESC_OP_TRANSPOSE = 5;
KBIN_DMA_DESC_OP_NUM       = 6;
```

`kbin_cce_op_to_sdma_cce_op` (`0x2664c0`) maps a kbin op to an SDMA op by indexing a
4-entry `.rodata` table with `(op − 1)`. The function is tiny and exact:

```c
// kbin_cce_op_to_sdma_cce_op @0x2664c0  [HIGH/OBSERVED]
uint32_t kbin_cce_op_to_sdma_cce_op(kbin_dma_desc_op op) {
    if ((uint32_t)(op - 1) > 3)                         // op ∉ {FMA,ADD,MIN,MAX}
        __assert_fail("...", "tdrv/helper.c", 0x1C0,    // "Unsupported CCE Op! %u"
                      "kbin_cce_op_to_sdma_cce_op");
    return CSWTCH_21[op - 1];                           // 4-entry remap table
}
```

`CSWTCH.21` @`0x9d6e00`, read byte-exact this pass, is `01 00 00 00 | 00 00 00 00 |
03 00 00 00 | 02 00 00 00`, i.e. the DWORD array `{1, 0, 3, 2}`:

| kbin op | index `op−1` | `CSWTCH.21[i]` | SDMA_CCETYPE |
|---------|-------------|----------------|--------------|
| FMA (1) | 0 | 1 | FMA |
| ADD (2) | 1 | 0 | ADD |
| MIN (3) | 2 | 3 | MIN |
| MAX (4) | 3 | 2 | MAX |

So `COPY(0)` and `TRANSPOSE(5)` are **not** CCE ops (COPY = plain DDMA, [§6](#6-how-cce-differs-from-a-plain-ddma-copy);
TRANSPOSE is the DRE / strided op). `EXT(4)` and `GCE(5)` have **no** kbin equivalent — the
4-entry map can never emit them; they are device-only values synthesized internally by the
combo path ([§5](#5-the-ext-and-gce-device-only-modes)). *(HIGH / OBSERVED — `CSWTCH.21`
read this pass; function disassembled.)*

### 1.2 The dtype remap and per-dtype byte size

`kbin_dtype_to_sdma_dtype` (`0x266460`) does the analogous remap with a 15-entry table,
gated to `dtype ∈ [1, 15]`:

```c
// kbin_dtype_to_sdma_dtype @0x266460  [HIGH/OBSERVED]
al_sdma_m2m_meta_ctrl_data_type kbin_dtype_to_sdma_dtype(kbin_dtype d) {
    if ((uint32_t)(d - 1) > 0xE)                        // d ∉ [1,15]
        __assert_fail("...", "tdrv/helper.c", 0x1AF,    // "Unsupported data type!"
                      "kbin_dtype_to_sdma_dtype");
    return CSWTCH_19[d - 1];                            // 15-entry remap table
}
```

`CSWTCH.19` @`0x9d6e20` (15 DWORDs, read byte-exact) and the SDMA-side dtype enum give:

| kbin_dtype | → SDMA dtype (name = value) | SDMA byte size |
|-----------|-----------------------------|----------------|
| FP8_E3 (1) | fp8_e3 = 13 | 1 |
| FP8_E4 (2) | fp8_e4 = 14 | 1 |
| FP8_E5 (3) | fp8_e5 = 15 | 1 |
| FP16 (4) | fp16 = 7 | 2 |
| FP32 (5) | fp32 = 10 | 4 |
| FP32r (6) | fp32r = 11 | **0** |
| BF16 (7) | bfloat16 = 6 | 2 |
| UINT8 (8) | uint8 = 3 | 1 |
| UINT16 (9) | uint16 = 5 | 2 |
| UINT32 (10) | uint32 = 9 | 4 |
| UINT64 (11) | uint64 = 1 | 8 |
| INT8 (12) | int8 = 2 | 1 |
| INT16 (13) | int16 = 4 | 2 |
| INT32 (14) | int32 = 8 | 4 |
| INT64 (15) | int64 = 12 | 8 |

The byte sizes come from `sdma_data_type_size` @`0x9be140` (16 × 8-byte entries, read
byte-exact this pass), indexed by the **SDMA** dtype value.

> **CORRECTION — `fp32r` has size 0, not 4.** The `sdma_data_type_size` entry for SDMA
> dtype `fp32r` (index 11, file offset `0x9be198`) reads `00 00 00 00 00 00 00 00` — **0
> bytes**, not 4. (Adjacent: `fp32`@idx10 = 4, `int64`@idx12 = 8.) Prior notes citing
> `fp32r = 4` are wrong against the binary. This matters: `encd_get_cce_reduce_packet_size`
> ([§7.1](#71-element--packet-size-limits)) asserts (line `0x1204`) when the table entry is
> 0, so a CCE reduce that asks for `fp32r` packet sizing **traps**. `fp32r` is a
> *round/replicated* tag that does not participate in the byte-length path; it rides the
> *accumulation* nibble, never the I/O-length nibble. *(HIGH / OBSERVED — table dumped
> byte-exact; `python3 struct.unpack` over the 16 entries.)*

---

## 2. How `cce_info` is consumed at the 16-byte descriptor level

### 2.1 The producer stack

The host producer chain bottoms out in the per-op descriptor encoders:

```text
add_dma_packet_cce @0x2307d0
  └─ stack-builds a kbin_dma_desc_cce_info_t (s_addrs[], d_addrs[], size, dtype,
     op, dmb, wrbar); fills per-source min_max_info.constant_dtype with the 16-B
     stride; memset(fma_info, 0, 132); tail-calls →
vring_add_dma_packet_cce @0x3134e0  →  vring_add_dma_packet_cce_int @0x311510   (DISPATCHER)
  └─ op-selects al_sdma_m2m_build_{add,min_max,fma}_packet @0x453210/0x454130/0x4538f0
       └─ allocates BDs, loops sources, per source calls
          al_sdma_m2s_build_<op>_descriptor + al_sdma_m2s_build_<op>_meta_ctrl
          (the byte-exact 16-B encoders, below).
```

`cce_info` is the 140-byte `kbin_dma_desc_cce_info_t` (struct recovered from
`structures.json`; see also [Host-Runtime Struct Layouts](../appendix/struct-exec-state-census.md)
for the broader census):

```c
struct kbin_dma_desc_cce_info_t {           // size 140  [HIGH/OBSERVED structures.json]
    uint32_t num_sources;                    // +0
    union $B94FBE {                          // +4 .. +135  (132 bytes)
        struct { kbin_dtype scale_dtype;     //   +4   (== fma_info.scale_dtype)
                 float    scale[32]; }  fma_info;        // +8 .. +135
        struct { bool     use_constant;      //   +4
                 kbin_dtype constant_dtype;  //   +8   (== fma_info.scale[0] address)
                 uint32_t constant; }   min_max_info;    // 12 bytes
    };
    uint32_t num_dests;                      // +136
};
```

> **GOTCHA — the union arms alias.** `min_max_info.constant_dtype` (union base `+4`) sits at
> exactly the same address as `fma_info.scale[0]` (also union base `+4`, since
> `fma_info.scale_dtype` occupies the first 4 bytes). This is why `add_dma_packet_cce`
> writes the per-source 16-byte stride into `min_max_info.constant_dtype` and the FMA reader
> later walks the scale vector starting from `&min_max_info.constant_dtype + 0`. Two views,
> one 132-byte payload. *(HIGH / OBSERVED — struct offsets; the scale-copy loop in
> [§3.2](#32-the-scale32-vector-application).)*

### 2.2 The common 16-byte M2S compute descriptor

Every `al_sdma_m2s_build_*_descriptor` writes four DWORDs through
`al_copy_descriptor(dst, src, 0x10)`:

| word | field |
|------|-------|
| `W0 [+0]` | `write_barrier_bit \| 0x800000 \| (meta_idx << 24)` — bit 23 (`0x800000`) = "this descriptor carries a meta-control word" |
| `W1 [+4]` | the op-tag / meta-ctrl DWORD (per-op base constant, [§2.6](#26-the-meta-control-word)) |
| `W2 [+8]` | `(sema/length & 0xFFFFFF) \| op-specific high bits` (operand selectors or const-dtype) |
| `W3 [+12]` | op-specific payload — the FP32 scale (FMA), or 0 (ADD/MIN/MAX) |

This 16-byte frame is the **compute specialization** of the same `SDMA_CME_BD_DESC` ring
entry a plain copy uses; the meta-ctrl DWORD is the WORD1 CME-command analogue carrying
`optype = CCE`, and `W0` bit 23 is the "has-extended-meta" flag. See
[The DMA / Descriptor / Memory Subsystem](descriptor-model.md) for the BD ring / doorbell
model that drains these. *(W0..W3 HIGH / OBSERVED this pass; the BD-frame overlay
MED / INFERRED — reconciled to the descriptor model, not independently DWARF'd device-side.)*

### 2.3 The FMA descriptor — `al_sdma_m2s_build_fma_descriptor` @0x451c20

Disassembled byte-exact this pass (128 bytes, tail-calls `al_copy_descriptor`):

```c
// al_sdma_m2s_build_fma_descriptor(dst, meta_idx, a_sel, b_sel, c_sel,
//                                  scale, ...wb, sub, last)             [HIGH/OBSERVED]
W0 = wb_arg | 0x800000 | (meta_idx << 24);                  // 0x451c4b/0x451c58
W2 = (sema & 0xFFFFFF)                                       // 0x451c69
   | ((a_sel & 3) << 24)                                     // 0x451c27/c31  [24:25]
   | ((b_sel & 3) << 26)                                     // 0x451c2e/c34  [26:27]
   | ((c_sel & 3) << 28);                                    // 0x451c37/c40  [28:29]
W1 = (a9 & 7)                                                // sub-op
   | ((a10 & 1) << 26)                                       // 0x451c82
   | (a8 << 29)                                              // 0x451c70
   | 0x2100000;                                              // FMA op tag (bit25+bit20)
W3 = scale;                                                  // 0x451c46  the FP32 scale
al_copy_descriptor(dst, &W, 0x10);
```

So the **three 2-bit operand selectors** live in `W2` bits `[24:25]=a_sel`, `[26:27]=b_sel`,
`[28:29]=c_sel`, and `W3` carries the per-source FP32 multiply scale.

### 2.4 The MIN/MAX descriptor — `al_sdma_m2s_build_min_max_descriptor` @0x451d10

```c
// al_sdma_m2s_build_min_max_descriptor(dst, meta_idx, W1, length,
//                                      use_const, const_dt, wb)         [HIGH/OBSERVED]
W0 = wb_arg | 0x800000 | (meta_idx << 24);
W1 = 0;                                                      // default (movq $0)
W2 = (length & 0xFFFFFF) | 0x2000000;                        // 0x451d47/d54
if (use_const)                                               // 0x451d2b test r9d
    W1 = ((const_dt & 7) << 20) | 0x6000000;                 // 0x451d3b  constant sub-desc
W3 = W1_arg;                                                 // (edx) 0x451d4d
al_copy_descriptor(dst, &W, 0x10);
```

The `0x6000000` `W1` value is the **constant sub-descriptor**: it carries the
`min_max_info.constant` (clamp/seed) plus the 3-bit `const_dtype` in bits `[22:20]`. A
MIN/MAX with `use_constant = 0` leaves `W1 = 0`. *(HIGH / OBSERVED.)*

### 2.5 ADD, seed_init, and replication

- **ADD** (`al_sdma_m2m_build_add_packet` @0x453210) emits per-source meta-ctrl via
  `al_sdma_m2s_build_add_meta_ctrl`; **no** per-source scale (pure accumulate). When the
  reduce also dtype-converts with stochastic rounding it prepends a seed_init descriptor.
- **seed_init** (`al_sdma_m2s_build_seed_init_descriptor` @0x451e40): `W0 |= 0x800000`; if
  the seed-enable arg is set, `W1 = 0x6000000` (loads the per-queue RNG seed before the
  compute descriptors). *(HIGH / OBSERVED.)*
- **replication** (`al_sdma_m2s_build_replication_descriptor` @0x451b10): builds a
  meta-ctrl via `al_sdma_m2s_build_replication_meta_ctrl` (`0x451b00`); when its mode arg
  is set, ORs `0x4a00000 | (mode << 29)`. Used by the combo path to broadcast a reduced
  result to multiple dests ([§4.3](#43-replication-num_dests--1)).

### 2.6 The meta-control word

The meta-control DWORD is the op-config word the CCE block reads. Each
`al_sdma_m2s_build_*_meta_ctrl` is a pure bit-pack; all five were disassembled byte-exact
this pass:

```c
// FMA  @0x451bd0  [HIGH/OBSERVED]
mc = (sub & 7) | ((in_dt & 0xF) << 8) | ((scale_dt & 0xF) << 12)
   | ((accum_dt & 0xF) << 16) | (f5 << 6) | (f6 << 4) | (f4 << 7) | 0x2100000;

// MIN/MAX  @0x451ca0
mc = (sub & 7) | ((const_dt & 7) << 20) | ((in_dt & 0xF) << 8) | ((out_dt & 0xF) << 12)
   | ((accum_dt & 0xF) << 16) | (notif << 7) | (wrbar << 6)
   | (use_const << 5) | (last << 4) | 0x2000000;     // use_const FORCES out_dt; else out_dt=0

// ADD  @0x451b80
mc = (sub & 7) | ((out_dt & 0xF) << 12) | ((accum_dt & 0xF) << 16)
   | (last << 7) | (wrbar << 6) | (f << 4) | 0x2000000;

// GCE/gradient  @0x451d70
mc = (sub & 7) | ((in_dt & 0xF) << 8) | ((scale_dt & 0xF) << 12) | ((accum_dt & 0xF) << 16)
   | (wrbar << 6) | (f << 4) | (last << 7) | 0x2500000;

// EXT (cce_ext)  @0x451dc0
mc = (sub & 7) | ((mid_dt & 0xF) << 12) | ((accum_dt & 0xF) << 16) | 0x2400000;
```

**Common decode** (the per-op base constants pin the op selector and `bit25`):

| bits | meaning |
|------|---------|
| `[25]` (`0x2000000`) | **CCE meta-ctrl VALID** — set by *every* base constant |
| `[22:20]` of the base | op selector: ADD/MIN/MAX = `0`; FMA = `0x100000` (bit20); EXT = `0x400000` (bit22); GCE = `0x500000` (bit22+bit20) |
| `[11:8]` | in / mid dtype nibble |
| `[15:12]` | scale / out dtype nibble |
| `[19:16]` | **accumulation** dtype nibble |
| `[22:20]` | const-dtype (MIN/MAX constant only) |
| `[4]` | `use_const` / final-flag |
| `[6]` | write-barrier |
| `[7]` | `last` / notify — set on the **final** source of a multi-source reduce |

The base constants verify the selector decode: `0x2100000 = bit25+bit20` (FMA),
`0x2400000 = bit25+bit22` (EXT), `0x2500000 = bit25+bit22+bit20` (GCE), `0x2000000 = bit25`
only (ADD/MIN/MAX). *(All five encoders HIGH / OBSERVED byte-exact; bit-name semantics
HIGH / INFERRED from the base constants + nibble positions + arg names.)*

---

## 3. The per-element FMA: `scale · src + accum`

### 3.1 The operand model

The CCE FMA unit has **three** operand inputs, each selected by a 2-bit field in the
descriptor's `W2` ([§2.3](#23-the-fma-descriptor--al_sdma_m2s_build_fma_descriptor-0x451c20)).
The selector values (`al_sdma_m2s_fma_operand`, `enums.json`):

```c
al_sdma_m2s_fma_operand_stored_buffer = 0;  // the running ACCUMULATOR (in-engine partial)
al_sdma_m2s_fma_operand_input_buffer  = 1;  // the SOURCE stream read on the M2S leg
al_sdma_m2s_fma_operand_scale         = 2;  // the per-source FP32 SCALE (descriptor W3)
```

`vring_add_dma_packet_cce_int` builds the default selector vectors for the FMA op:

```c
// per source i, FMA arm of vring_add_dma_packet_cce_int @0x311510  [HIGH/OBSERVED]
a_sel[i] = input_buffer;   // 1  ── multiply operand A = this source
b_sel[i] = scale;          // 2  ── multiply operand B = the FP32 scale
c_sel[i] = stored_buffer;  // 0  ── add operand C    = the accumulator
//  ⇒  acc' = (input_i · scale_i) + acc
```

So `a_sel`/`b_sel` are the **multiply** operands (`src × scale`) and `c_sel` is the **add**
operand (the accumulator). The result of source `i` becomes the accumulator for source
`i+1` — the engine threads the partial through `stored_buffer`, walking the source list. The
final source (meta-ctrl `last` bit) flushes the accumulated result down the S2M leg.

### 3.2 The `scale[32]` vector application

The FMA arm of `vring_add_dma_packet_cce_int` (op == FMA) reads `cce_info.fma_info`:

1. **Asserts the scale vector is FP32.** At `0x311d0d` the function calls `__assert_fail`
   with `mov $0x3ba, %edx` (line `0x3BA`) on string
   `sdma_scale_dtype == al_sdma_m2m_meta_ctrl_data_type_fp32` in
   `/opt/workspace/KaenaRuntime/tdrv/vring.c`. So `cce_info.fma_info.scale_dtype` **must**
   remap to SDMA `fp32 = 10` — the scale vector is always FP32. *(HIGH / OBSERVED — assert
   string + line number read byte-exact.)*
2. **Copies the per-source scales** into a local `sdma_scale[]` and marshals it to the
   packet builder: five `movdqa/movups` of 16 bytes each (`0x311b4e`..`0x311b8e`,
   `0x311c05`..`0x311c45`) copy 80 bytes = 20 floats of the scale vector onto the call
   frame, alongside the `a_sel`/`b_sel`/`c_sel` arrays (`lea 0x100(%r12)` / `lea
   0x120(%r12)`).
3. **Emits per source:** `al_sdma_m2m_build_fma_packet` (`0x4538f0`) then, per allocated tx
   BD, calls `al_sdma_m2s_build_fma_descriptor(BD, …, a_sel[i], b_sel[i], c_sel[i],
   scale[i], …)` — i.e. `scale[i] → W3`, the three selectors `→ W2`.

```c
// realized FMA emit (vring_add_dma_packet_cce_int → build_fma_packet → build_fma_descriptor)
assert(kbin_dtype_to_sdma_dtype(cce_info->fma_info.scale_dtype) == fp32);   // vring.c:0x3BA
for (i = 0; i < num_blocks; i++)
    sdma_scale[i] = cce_info->fma_info.scale[i];      // walk from union +4
al_sdma_m2m_build_fma_packet(sdma_scale, /*sdma_dtype=*/10 /*fp32*/,
                             a_sel /*=input*/, b_sel /*=scale*/, c_sel /*=stored*/, ...);
```

So the 32-wide `cce_info.scale[]` is realized as **one FP32 scale per per-source FMA
descriptor** (up to 32 sources). The scale is per-**source** (per-rank), not per-element:
element `i` of the tensor is computed across all sources, each source applying *its* scalar
scale. *(HIGH / OBSERVED — fp32 assert, scale-copy moves, per-source emit all read this
pass; the engine's per-element broadcast of the scalar scale is INFERRED.)*

### 3.3 Accumulation width and dtype convert

The meta-ctrl word carries **three independent** dtype nibbles
([§2.6](#26-the-meta-control-word)): `in_dtype [11:8]`, `scale/out_dtype [15:12]`, and
`accum_dtype [19:16]`. The CCE reads each source in `in_dtype`, multiplies by the FP32
scale, **accumulates in `accum_dtype`** (a wider internal type — e.g. BF16 inputs
accumulated in FP32), and writes the result **converted to `out_dtype`**. This is the
mixed-precision reduce: read low-precision, accumulate high-precision, write low-precision —
in one transfer. The accum nibble being *separate* from in/out is the byte-exact evidence
the accumulation width is configurable and independent of the I/O dtype. *(Nibble positions
HIGH / OBSERVED; the "read-in / accum-wider / write-out" data-path semantics HIGH / INFERRED
from the three-nibble split + the FP32-scale assert + the [§7.2](#72-dtype-validity-gate)
gate — the silicon ALU is not in the binary.)*

### 3.4 Stochastic rounding underlay

When the down-convert (`accum_dtype → out_dtype`) rounds, the CCE can use **stochastic
rounding**. `vring_add_dma_packet_cce_int` reads `vring->stochastic_rounding_en` and threads
it plus a 32-bit seed into the packet; when set, the builders prepend an
`al_sdma_m2s_build_seed_init_descriptor` (`0x451e40`, the `0x6000000` `W1` sub-word) that
loads the per-queue RNG seed before the compute descriptors. The seed and per-dtype mode are
programmed once via `aws_hal_stpb_stochastic_rounding_config` (`0x458e20`), which writes the
POOL/DVE/ACT sequencer `stochastic_rnd` CSRs (plus a global PSUM SR register on arch > 3).
*(HIGH / OBSERVED — SR-enable/seed thread + seed_init emit + SR-config CSR writes.)*

---

## 4. The ADD / MIN / MAX multi-source reduction

### 4.1 The dispatch

`vring_add_dma_packet_cce_int` (`0x311510`) op-selects the builder. The three op arms each
call a distinct `build_*_packet` (verified by the `call` targets at `0x3116a4`/`0x311943`/
`0x311b93`):

```c
// vring_add_dma_packet_cce_int @0x311510  [HIGH/OBSERVED]
switch (op) {
  case ADD:                                              // SDMA_CCETYPE_ADD = 0
    if (num_dests <= 1)
        al_sdma_m2m_build_add_packet(...);               // 0x453210  acc' = acc + src_i
    else
        aws_cayman_sdma_m2m_build_combo_op(...);         // §5  (arch>2 only)
    break;
  case MAX: case MIN:                                    // 2 / 3
    al_sdma_m2m_build_min_max_packet(use_const, constant, constant_dtype,
                                     is_max=(op==MAX), ...);  // 0x454130
    break;
  case FMA:                                              // 1
    al_sdma_m2m_build_fma_packet(...);                   // 0x4538f0  §3
    break;
}
```

- **ADD** is pure accumulate over the `N` sources (no scale): `acc' = acc + src_i`.
- **MIN/MAX** walk the `N` sources keeping the running extremum; if
  `cce_info.min_max_info.use_constant`, the result is *also* clamped against the constant
  (the `constant` + `constant_dtype` come straight from the `min_max_info` arm, converted via
  `kbin_dtype_to_sdma_dtype`; an unsupported dtype here logs `"Unsupported data type for
  min/max op %s"` @`0x82cc58`).
- **FMA** is the only op that consumes the `scale[]` arm.

> **QUIRK — `cce_info` may be NULL for ADD/MIN/MAX.** A constant-free reduce (ADD, or
> MIN/MAX without a clamp) tolerates `cce_info == NULL`. FMA does **not**: it asserts
> `cce_info != NULL` (`vring.c:0x3B8`, immediately before the FP32-scale assert at `0x3BA`).
> *(HIGH / OBSERVED.)*

The **multi-source walk**: each `build_*_packet` loops the tx BDs (one per source) and marks
the **last** source's meta-ctrl with the `last` bit ([§2.6](#26-the-meta-control-word) bit 7)
so the engine knows when to flush the accumulated result. `num_sources` ≤ 32
(`cce_info.num_sources`).

### 4.2 The u8 → u16 accumulation-lane guard

`vring_add_dma_packet_cce_int` has a special case: if the transfer is a single 1-byte-dtype
descriptor with `src_dtype == rx_dtype` (and `sdma_data_type_size[dtype] == 1`), it
**promotes** both the tx and rx meta-ctrl dtypes to `uint16`. This is the engine refusing a
1-byte accumulation lane — the CCE accumulates 8-bit data through a 16-bit datapath.
*(HIGH / OBSERVED — the `*tx_dtype = …uint16; rx_dtype = …uint16` branch, gated on
`sdma_data_type_size[dtype] == 1 && single-desc && same-dtype`.)*

### 4.3 Replication (`num_dests > 1`)

When `cce_info.num_dests > 1` the reduce result is **replicated** to multiple destinations
in one packet — but **only on arch > 2** (v3 / Cayman+): the v1/v2 path bails with the
string `"CCE replication not supported for v1/v2 archs"` (`0x81a4e0`). The replication is
built by the combo path ([§5](#5-the-ext-and-gce-device-only-modes)) via
`al_sdma_m2s_build_replication_descriptor` (`0x451b10`). So reduce-and-broadcast — the
all-reduce final leg — is a single CCE packet on Cayman and later. *(HIGH / OBSERVED — the
`num_dests > 1` → arch-gate → combo branch + the bail string.)*

---

## 5. The EXT and GCE device-only modes

### 5.1 EXT (4) — the combo packet

EXT is not a single ALU op; it is the **combined multi-descriptor CCE packet** the combo
builder emits when an operation needs more than one CCE descriptor.
`aws_cayman_sdma_m2m_build_combo_op` (`0x44a3f0`, disassembled this pass) chains:

1. a seed-init or constant **first** descriptor (`build_seed_init_descriptor` with the
   `0x4000000` combo-member flag set at `0x44ae3e`, or `build_min_max_descriptor`),
2. per-source FMA / ADD / MIN / MAX descriptors,
3. optional **replication** descriptors,

all linked by the `0x40000000` **"more-descriptors-follow"** bit (ORed at `0x44a83b`). The
combo builder's call targets are byte-exact: `build_fma_descriptor` (`0x44a7e0`),
`build_fma_meta_ctrl` (`0x44a81e`), `build_add_meta_ctrl` (`0x44a92b`),
`build_min_max_meta_ctrl` (`0x44aa35`), `build_gradient_meta_ctrl` (`0x44aade`),
`build_gradient_descriptor` (`0x44ab04`), `build_replication_descriptor` (`0x44ac20`),
`build_seed_init_descriptor` (`0x44ae49`), `build_min_max_descriptor` (`0x44ae85`). The EXT
meta-ctrl base `0x2400000` (`cce_ext` @`0x451dc0`) tags the packet. The combo path is the
**only** producer of replication and of fused (seed + FMA + broadcast) packets, and it is
arch > 2 only — overflow / v ≤ 2 logs `"CCE concatenation not supported"` (`0x8205f0`).
EXT(4) has no kbin op — it is synthesized device-side. *(HIGH / OBSERVED — combo_op emit
targets + the `0x40000000` more-bit + `0x4000000` combo-member flag.)*

### 5.2 GCE (5) — gradient compression / decompression

GCE is a separate in-transfer mode (`SDMA_CCETYPE_GCE = 5`; meta-ctrl base `0x2500000`,
`build_gradient_meta_ctrl` @`0x451d70`; descriptor via `build_gradient_descriptor`
@`0x451de0`), built by `al_sdma_m2m_build_gradient_packet` (`0x454990`). Its `.rodata`
strings — `"invalid %scompression header addr %lx"` (`0x82391a`), `"Failed to build
decompression header"` (`0x823948`), `"Failed to build buffer packet for gce op"`
(`0x823982`) — plus its mode arg show GCE does **in-flight gradient compression**: it can
write a compression header + compressed payload (compress mode) or read a header + expand
(decompress mode), optionally fused with the accumulate. GCE is the bandwidth-reduction leg
for large gradient all-reduces. GCE(5) likewise has no kbin op equivalent — it is reached
through the combo / gradient builders, never the `kbin_cce_op` map. *(GCE strings + gradient
encoders HIGH / OBSERVED; the "compression for collective bandwidth" purpose HIGH / INFERRED
from the strings + the GCE name + the header build path — the codec itself is in silicon.)*

### 5.3 Why EXT/GCE are device-only

`kbin_cce_op_to_sdma_cce_op` ([§1.1](#11-two-op-enums-one-remap-table)) only ever produces
`{ADD, FMA, MAX, MIN}` = SDMA_CCETYPE `0..3` (the 4-entry `CSWTCH.21`). `EXT(4)` / `GCE(5)`
are never emitted from a kbin op; they are produced internally when the runtime fuses ops
(EXT) or compresses gradients (GCE). That 4-entry map is the byte-exact basis for the
"device-only, no kbin equivalent" status. *(HIGH / OBSERVED.)*

---

## 6. How CCE differs from a plain DDMA copy

The contrast is exact and minimal — same ring, same 16-byte BD, same doorbell:

- **Plain COPY (DDMA):** `add_dma_packet` (`0x22ff20`) → `vring_add_dma_packet` → one BD per
  chunk with `buf_ptr` + length; `SDMA_CMETYPE_COPY = 0`, `SDMAOP_CME = 2`. One source, one
  dest, **no** meta-ctrl word (`W0` bit 23 clear), no ALU. The S2M leg writes exactly the
  bytes the M2S leg read.
- **CCE COMPUTE:** `add_dma_packet_cce` (`0x2307d0`) → … →
  `al_sdma_m2s_build_<op>_descriptor`. `SDMAOP_CCE = 4`. The descriptor sets `W0` bit 23 +
  a meta-ctrl word (op tag, `bit25` set, dtype nibbles) + (FMA) a `W3` scale + `W2` operand
  selectors. `N` sources (≤ 32) feed one accumulator; `M` dests (≤ 1, or > 1 on Cayman+ via
  EXT replication). The S2M leg writes the reduced/scaled/converted result.

| field | plain COPY | CCE compute |
|-------|-----------|-------------|
| SDMAOP | CME (2) | CCE (4) |
| sources | 1 | 1..32 (`num_sources`) |
| dests | 1 | 1 (or > 1 via EXT replication, arch > 2) |
| meta-ctrl word (`W0` bit 23) | absent (0) | present (`0x800000`), op tag `bit25` set |
| descriptor `W3` | — | FP32 scale (FMA) / 0 (ADD/MIN/MAX) |
| descriptor `W2` selectors | — | a/b/c operand selectors (FMA) |
| dtype handling | copy bytes | in/accum/out nibbles + convert + stochastic rounding |
| engine | DDMA data-path | CDMA = DDMA + CCE block |

A CCE transfer is a copy whose **M2S read leg is rewired through the CCE ALU** before the
S2M write — the descriptor bits are the only difference at the ring level. *(HIGH /
OBSERVED — the two producers + the descriptor field deltas read this pass.)*

---

## 7. Per-arch / HAL limits and the dtype-validity gate

### 7.1 Element / packet-size limits

```c
// aws_hal_get_sdma_max_cce_elements @0x44be60  [HIGH/OBSERVED]
uint32_t aws_hal_get_sdma_max_cce_elements(void) {
    int arch = al_hal_tpb_get_arch_type();
    if (arch == 2)            return 0x400;   // 1024  (v2)
    if (arch > 2 && arch <= 4) return 0x800;  // 2048  (v3/v4)
    __assert_fail(...);                       // line 0x2C
}
```

`aws_sdma_get_cce_params_cayman` (`0x471760`) returns the Cayman `{max_byte, max_element}`
pair (sunda / mariana siblings present). The per-collective reduce packet size:

```c
// encd_get_cce_reduce_packet_size @0x2396f0  [HIGH/OBSERVED]
size_t encd_get_cce_reduce_packet_size(ctx, sdma_dtype) {
    uint8_t  pc   = encd_get_ctx_curr_priority_class(ctx);   // 0..4
    if (pc > 4) __assert_fail(...);                          // line 0x1202
    uint32_t cap  = nrt_gconf()->cc_cce_reduce_prio_pkt_size[pc];  // +0x54
    size_t   dsz  = sdma_data_type_size[sdma_dtype];
    if (dsz == 0) __assert_fail(...);                        // line 0x1204  ← fp32r traps here
    size_t   want = dsz * ctx->elem_count;                   // +0x18
    size_t   pkt  = (cap && want > cap) ? cap : want;
    return pkt & ~0x1FuLL;                                   // 32-byte granular
}
```

So CCE reduce transfers are **chunked to a 32-byte granularity**, capped per QoS priority
class. The `dsz == 0` assert (line `0x1204`) is exactly the `fp32r` trap noted in
[§1.2](#12-the-dtype-remap-and-per-dtype-byte-size). *(HIGH / OBSERVED — both functions
disassembled.)*

### 7.2 Dtype-validity gate

```c
// has_valid_dma_cce_inout_dtype_nc_v1 @0x280820  [HIGH/OBSERVED]
bool has_valid_dma_cce_inout_dtype_nc_v1(bool compute_op, int in_dt, int out_dt) {
    if (!compute_op) return true;                       // NONE = always valid
    bool in_band  = (uint8_t)(in_dt  - 8) <= 3;         // in  ∈ {int32,uint32,fp32,fp32r}
    bool out_band = (uint8_t)(out_dt - 8) <= 3;         // out ∈ {int32,uint32,fp32,fp32r}
    return in_band == out_band;                         // both in the 32-bit class, or both out
}
```

For a non-NONE compute op, `in_dtype` and `out_dtype` must be in the **same class** (both in
the `int32/uint32/fp32/fp32r` band `[8..11]` or both outside it). This blocks mixing a
32-bit-class dtype with an incompatible I/O dtype on NC-v1. Per-arch indirect/gather variants
`is_valid_dma_indirect_cce_{0,1}` exist (`0x345550`/`0x3d3b80`). *(HIGH / OBSERVED.)*

### 7.3 Per-gen summary

The CCE op set `{ADD, FMA, MAX, MIN}` is flat across **v2 → v4** (Sunda / Cayman / Mariana).
Replication (`num_dests > 1`) and the EXT combo are arch > 2 (v3 / Cayman+) only; the
per-arch `cce_params` / `max_elements` differ (sunda / mariana / cayman variants).

> **NOTE — MAVERICK (v5) is header-OBSERVED only.** No shipped v5 image was disassembled; the
> claim that MAVERICK keeps the same `SDMA_CCETYPE` enum (incl. EXT/GCE) and the same CCE
> compute model — with only the DMA-engine naming (SDMA → DDMA/CDMA/UDMA) and the cross-die
> transport (→ UCIe) changing — is **MED / INFERRED**, reconciled across the per-gen split,
> not byte-grounded. Treat the MAVERICK on-engine collective variant interiors as INFERRED.

---

## 8. Ties to the collective reduce

The callers of `add_dma_packet_cce` are all collective-reduce paths:

- `encd_dma_reduce_copy` (`0x23d0c0`) — builds `s_addr[]` / `d_addr[]` vectors, calls
  `add_dma_packet_cce` for the reduce-copy leg.
- `encd_mesh_reduce` (`0x23c090`) / `encd_mesh_reduce_sb2sb` (`0x23b6a0`) — the mesh / SB2SB
  reduce steps (the on-engine collective's in-engine reduce-copy).
- `encd_mesh_cce_desc_padding` (`0x234000`) — the CCE descriptor padding / alignment helper.
- `enc_primitive::recv_reduce_copy` → `__recv_reduce_write` (`0x16a0a0`) — builds the source
  address vector (neighbor chunks) + dest, passing the SDMA_CCETYPE plus the reduction
  **topology** `enc_primitive::reduction_type_t {RING_2R1W = 0, RING_2R2W = 1,
  KANGARING_NR1W = 2}` (`enums.json`) down to `add_dma_packet_cce`.

So an all-reduce leg is a CCE ADD / MIN / MAX / FMA over the neighbor source streams, landing
the reduced chunk at the dest — the same M2S/S2M ring and the same completion semaphores as a
plain copy, now with the CCE meta-ctrl bits set. The compiler-level collective (the
`PSEUDO_TRIGGER_COLLECTIVE` op ADD = `0x04`, dtype FP32 = `0x0A`) lowers, host-side, to
`SDMA_CCETYPE_ADD` reduce packets. This path is what the ring / Kangaring collective and the
ALL_REDUCE op (covered in the collectives chapter) apply in-transfer; the on-engine SB2SB
collective reduces via this exact `encd_mesh_reduce_sb2sb → add_dma_packet_cce` chain. The
`cce_info` struct ([§2.1](#21-the-producer-stack)) is consumed end-to-end:
`fma_info.scale[i] → W3`, `min_max_info.{use_constant, constant_dtype, constant} →` the MIN/MAX
`0x6000000` sub-descriptor + the const-dtype nibble, `num_sources`/`num_dests` → the
per-source loop + the replication gate. *(HIGH / OBSERVED — caller set + `reduction_type_t` +
the address-vector build; the live CCE-reduce bit-exact validation is tracked in the
validation chapter's reduce family.)*

---

## 9. Worked examples

### 9.1 A 3-source all-reduce ADD (a ring reduce-copy step)

A ring all-reduce over 3 ranks; this step reduces this rank's chunk with two neighbor chunks
into a local dest. BF16 data, FP32 accumulate.

```c
cce_info = { .num_sources = 3, .num_dests = 1 /* op = ADD; cce_info may even be NULL */ };
// SDMA dtypes: in = bfloat16(6), accum = fp32(10), out = bfloat16(6).

add_dma_packet_cce(vring, s_addrs[3], 3, d_addrs[1], 1, size,
                   /*in_dtype=*/bf16, dmb, wrbar, /*op=*/SDMA_CCETYPE_ADD, ...);
// → vring_add_dma_packet_cce_int: op==ADD, num_dests<=1 → al_sdma_m2m_build_add_packet
// → 3 tx M2S BDs (one per source) + 1 rx S2M BD:
//     BD[0] src=s0  add_meta_ctrl(in=6, accum=10, last=0)  base 0x2000000
//     BD[1] src=s1  add_meta_ctrl(..., last=0)
//     BD[2] src=s2  add_meta_ctrl(..., LAST → bit7 set)    ← flushes to dst
//     rx    dst=d0  out=bf16
```

CCE math (in flight, FP32 accumulate):

```text
acc = (fp32)s0 + (fp32)s1 + (fp32)s2;
dst = bf16(acc);          // stochastic-rounded if vring->stochastic_rounding_en (§3.4)
```

Chunked to ≤ 2048 elements / ≤ 32-byte-multiple packets ([§7.1](#71-element--packet-size-limits)),
completed by the collective's two-semaphore + gen-tag poll. *(Formula HIGH; the specific
ranks/values are illustrative.)*

### 9.2 A weighted-average FMA (`dst = 0.5·s0 + 0.5·s1`)

```c
cce_info = { .num_sources = 2, .num_dests = 1,
             .fma_info = { .scale_dtype = FP32, .scale = {0.5f, 0.5f} } };  // op = FMA

// vring_add_dma_packet_cce_int, op==FMA:
assert(kbin_dtype_to_sdma_dtype(FP32) == fp32 /*10*/);   // vring.c:0x3BA  ✓
// sdma_scale = {0.5f, 0.5f};  a_sel={input,input}, b_sel={scale,scale}, c_sel={stored,stored}
al_sdma_m2m_build_fma_packet(sdma_scale, 10 /*fp32*/, a_sel, b_sel, c_sel, ...):
//   BD[0] FMA desc:  W3 = 0x3F000000 (0.5f);  W2: a_sel=1 b_sel=2 c_sel=0;
//                    W1 base 0x2100000; last=0   ⇒  acc = s0·0.5 + 0
//   BD[1] FMA desc:  W3 = 0x3F000000 (0.5f);  last=1  ⇒  acc = s1·0.5 + acc
//   rx:  dst = convert(acc, out_dtype)                ⇒  dst = 0.5·s0 + 0.5·s1
```

The two FP32 `0.5` scales ride in `BD[0].W3` and `BD[1].W3` (IEEE-754 `0.5f = 0x3F000000`,
confirmed). The accumulator threads `s0`'s partial into `s1`'s FMA via the `stored_buffer`
(`c_sel = 0`) operand. *(Encoder arithmetic + operand-selector defaults + the FP32-scale path
HIGH / OBSERVED; the `0.5` values + the `0x3F000000` bit pattern are illustrative.)*

### 9.3 A gradient all-reduce with GCE compression (EXT/GCE)

A large-gradient all-reduce reduces *then* compresses for the cross-die hop: the combo path
emits an EXT packet —

```text
[ seed_init desc | per-source ADD descs (last bit on the final) | GCE compress desc + compression header ]
```

— chained by the `0x40000000` more-bit, with base `0x2400000` (EXT) / `0x2500000` (GCE). The
peer's leg uses GCE decompress to expand before its own reduce. *(EXT/GCE combo **structure**
HIGH / OBSERVED — `combo_op` + `gradient_packet`; the end-to-end gradient-AR fusion MED /
INFERRED.)*

---

## 10. Confidence and gaps

- **HIGH / OBSERVED:** the kbin↔SDMA op + dtype remap tables (`CSWTCH.21`/`19` byte-exact)
  and the per-dtype byte-size table (incl. the `fp32r = 0` correction); the 16-byte M2S
  compute-descriptor encoders (FMA `W0..W3` with scale @`W3` + a/b/c selectors @`W2`; MIN/MAX
  constant sub-descriptor; ADD; GCE gradient; seed_init; replication; EXT) and the per-op
  meta-ctrl bitfields (`bit25` CCE-valid, the three dtype nibbles, the op sub-selector, the
  last/write-barrier/use-const flags); the FMA operand model (stored/input/scale) and the
  default `a=input/b=scale/c=stored ⇒ scale·src+accum`; the FP32-scale assert (`vring.c:0x3BA`,
  string read byte-exact); the `scale[32] →` per-source-`W3` application; the ADD/MIN/MAX
  dispatch + the `cce_info`-NULL rule + the last-source flush; the u8→u16 accumulation-width
  promotion; the replication arch-gate; the EXT combo multi-descriptor build (`0x40000000`
  more-bit) + the GCE gradient compress/decompress strings + encoders; the COPY-vs-CCE delta
  (SDMAOP CME 2 vs CCE 4, meta-ctrl presence); `max_cce_elements` (1024 v2 / 2048 v3/v4); the
  32-byte-granular priority-capped reduce packet size; the inout-dtype validity gate; the SR
  seed thread + seed_init + SR sequencer config; the collective-reduce caller set + the
  `reduction_type_t` topology enum; the `cce_info` 140-byte layout + union aliasing.
- **MED / INFERRED:** the `W0..W3 → SDMA_CME_BD_DESC` overlay (reconciled to the descriptor
  model, not device-side DWARF'd); the "read-in / accum-wider / write-out" data-path semantics
  (the silicon ALU is not in the binary); the GCE compression *purpose*; the MAVERICK
  CCE-flat-across-gens claim (no shipped v5 image — header-OBSERVED only).
- **LOW:** the exact meta-ctrl flag semantics at bits `[4]/[5]/[6]/[7]` beyond the decoded
  positions (use_const/write_barrier/notif/last named from base constants + arg names, not
  device prose); the §9 numeric values (illustrative).
