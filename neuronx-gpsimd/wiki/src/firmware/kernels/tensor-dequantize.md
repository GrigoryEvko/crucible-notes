# TensorDequantize

The POOL `TensorDequantize` kernel (`decode_tensor_dequantize`, opcode **`0x7b`**) is the
GPSIMD engine's **sub-byte micro-format expander**: it reads a quantized tensor packed in one
of four sub-byte micro-formats — **FP4 E2M1**, **INT4**, **NF4**, or **FP6 E2M3** — and widens
each code into an 8-bit FP8 element (`E5M2` or `E4M3`), optionally multiplying by a **per-block
(MX) scale** when micro-scaling is enabled. It is the *only* dequant opcode in the POOL kernel
set on this generation, and — critically — it is **not** the classic affine
`(q − zero_point) × scale` integer dequant. There is **no zero-point field, no scale-address
field, and no scale tensor** in its operand struct. The "dequant" here is a **format
expansion** (+ optional in-band block scale), and the affine integer path lives in a *different*
engine entirely (the Activation engine's `Inline_dequantization`, not POOL).

This page is the reimplementation reference for that kernel. It delivers: the **dispatch chain**
(`kernel_info_table` → trampoline → `decode_tensor_dequantize` → `proc_4bit_mx_8`); the
**`s3d3_tens_dequant` 64-byte operand struct** (with the verbatim validity contract); the
**`DEQUANT_FMT` format-expansion enum**; the **per-format dequant arithmetic** for each of FP4 /
INT4 / NF4 / FP6 as annotated C pseudocode; the **scale model** (none vs per-block MX, and why
there is no per-tensor / per-channel scale); the **sub-byte-unpack → scale-multiply →
saturate-convert IVP datapath**; the **ratio SIMD loop**; and the **output convert/rounding**.

Every struct field, enum value, opcode, and validity predicate below is read **byte-for-byte**
from the shipped arch-isa headers
(`neuron_{cayman,mariana,maverick}_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_tens_dequant.h` and the
`…_common.h` enums), which ship in the `aws-neuronx-gpsimd-customop-lib_0.21.2.0` package and are
cross-checked against the carved device firmware's `kernel_info_table` dispatch bytes, its
`.xt.prop` function-start records, and the prologues decoded with the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`). The firmware compute bodies are hand-scheduled
FLIX VLIW with interleaved literal pools that **desync** under stock objdump — so the
*per-format micro-op order* is recovered **structurally** (from the surviving FLIX bundles' IVP
mnemonics + the divide-by-5 idiom + the authoritative validity contract), and is flagged
`INFERRED` wherever it is not a byte-exact linear decode. Confidence tags follow
[the Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / proven-by-decode, `[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]` =
re-used at a sibling report's confidence without re-reading the artifact this pass.

The dtype ordinals (`UINT32 = 0x9`, `FP8_EXP4 = 0xE`, `FP8_EXP5 = 0xF`, `FP4_EXP2 = 0x10`,
`INT4 = 0x12`) are the ones established in [The Unified Datatype Model](dtype-model.md) — this
page keeps them consistent and resolves all dtype meaning there.

---

## 0. TL;DR — the kernel in seven facts

1. **One opcode, one struct, one format-selector.** `TensorDequantize` = opcode `0x7b`
   (`NEURON_ISA_TPB_OPCODE_TENSOR_DEQUANTIZE`, `common.h:214`), reads
   `NEURON_ISA_TPB_S3D3_TENS_DEQUANT_STRUCT` (64 bytes, static-asserted), and switches on the
   1-byte `dequant_fmt` field (off 35) — **not** on the dtype enum. `[HIGH/OBSERVED — header.]`
2. **Format expansion, not affine dequant.** Each arm widens a sub-byte code to FP8 by
   exponent-rebias (FP4/FP6), int→float convert (INT4), or 16-entry table lookup (NF4). There is
   **no `(q − zp) × scale`** path: no zero-point, no scale operand. `[HIGH/OBSERVED — struct.]`
3. **The dtype fields are transport, not logic.** `in_dtype == out_dtype == UINT32 (0x9)` is a
   hard validity constraint — the sub-byte codes ride **packed inside `UINT32` words**, and the
   *logical* in/out micro-format is named entirely by `dequant_fmt`. `[HIGH/OBSERVED — gate.]`
4. **Scale is per-block (MX) or absent — never per-tensor / per-channel.** `group_size == 0` =
   pure format widen (no scale). `group_size == 8` = micro-scaling: **one shared scale per block
   of 8 source codes**, applied as a vector multiply, consumed in-band (no separate scale
   tensor). `[HIGH/OBSERVED — gate + element-count check.]`
5. **The element ratio encodes the packing.** `group=0`: 2:1 for the 4-bit formats, 4:3 for
   FP6. `group=8`: 8:5 for the 4-bit MX formats — exactly the `n/5` reciprocal-multiply observed
   in `proc_4bit_mx_8`. `[HIGH/OBSERVED — element-count check + the firmware idiom.]`
6. **FP6 can never be MX.** `has_valid_dequant_fmt_group_size` admits `group_size == 8` **only**
   for `E2M1ToE5M2 / INT4ToE5M2 / NF4ToE4M3`. `E2M3ToE4M3` (FP6) is `group_size == 0` only.
   `[HIGH/OBSERVED — gate.]`
7. **NC-v3+, and the addressing widened across generations.** The op exists from
   `NeuronCoreVersion >= V3` (CAYMAN). On NC-v3 (CAYMAN) `src/dst_mem_pattern` are plain
   `TENSOR3D`; on **NC-v4/v5 (MARIANA/MAVERICK)** they are a **`MEM_PATTERN3D` union**
   (`{TENSOR3D | INDIRECT16B | MXTENSOR_V2}`) — adding indirect and MX-descriptor addressing
   while the dequant *semantics* (the `DEQUANT_FMT` enum, the 64-byte size, the ratios) stay
   byte-identical. `[HIGH/OBSERVED — cross-gen header diff.]`

---

## 1. Dispatch chain — `kernel_info_table` → trampoline → kernel

`TensorDequantize` is reached the same way as every POOL kernel (the `SX-FW-18` route): the
ucode loader dispatches each instruction by opcode through the `kernel_info_table`, which holds
one 8-byte record per supported opcode; the record's trailing `u32` is the trampoline VA, and
the trampoline tail-calls the named worker. `[HIGH structure / OBSERVED bytes for the route.]`

```text
opcode 0x7b (123)  TensorDequantize
   kernel_info_table  idx16 record @file off 0x7480 : {00 00 spec=00 op=7b | C4 4D 00 01}  funcVA 0x01004dc4   [HIGH/OBSERVED bytes]
      trampoline @0x01004dc4 :  entry a1,32
                                const16 a2,0x0200 ; const16 a2,0x0480   => a2 = 0x02000480  (per-kernel STATE slot)
                                l32i.n a2,a2,0 ; slli a2,a2,4           (load + scale a state pointer)
                                call0 <state-setup helper>
                                const16 a2,0x4df0 ; callx8 a2            => decode_tensor_dequantize @ 0x01004df0
                                movi.n a2,0 ; retw.n
         decode_tensor_dequantize(bool) @0x01004df0   <- decode + format-widen paths (group_size==0) + dispatch
            TensorDequantize::proc_4bit_mx_8(unsigned int) @0x0100511c  <- the MX path (group_size==8, 4-bit, block of 8)
```

The `kernel_info_table` lives at VMA `0x02000380` (file off `0x7400`), size `0x88` = **17
records × 8 bytes**; record format `{ u8 0; u8 0; u8 spec; u8 opcode; u32_le funcVA }`. The
dequant record is **idx16** at file off `0x7480` — `00 00 00 7b | c4 4d 00 01` — i.e. opcode
`0x7b`, funcVA `0x01004dc4` (re-grounded byte-exact this pass by an independent re-carve of
`libnrtucode_internal.so`). The trampoline at `0x01004dc4` builds the per-kernel state slot
`0x02000480` and then `callx8`-tail-calls the worker at `0x01004df0`. The two function names are
**byte-confirmed** in the carve's surviving `.xt.prop` sections — the mangled symbols
`_Z24decode_tensor_dequantizeb` (`decode_tensor_dequantize(bool)`) and
`_ZN16TensorDequantize14proc_4bit_mx_8Ej` (`TensorDequantize::proc_4bit_mx_8(unsigned int)`) —
and a full `.xt.prop` function-name sweep finds **no** `proc_6bit`, **no** `proc_*_mx_<other>`
variants. `[HIGH/OBSERVED — bytes + section names re-grounded.]`

> **CORRECTION — to `SX-FW-18`'s `idx14/idx15` "dequant-adjacent" guesses.** Pinned from the ISA
> opcode enum + trampoline decode: `idx14` is opcode `0xbe` = `GET_SEQUENCE_BOUNDS`
> (`common.h:261`, routing to `get_sequence_bounds_impl`), and `idx15` is opcode `0xf2` =
> `NONZERO_WITH_COUNT` (`common.h:303`, routing to `nonzero_with_count_impl<float|int>`).
> **Neither is dequant.** The single dequant kernel is `idx16` / opcode `0x7b`. The
> dequantize-adjacent `idx15` label in `SX-FW-18` is superseded. `[HIGH/OBSERVED — opcode enum +
> trampoline.]`

> **NOTE — `decode_tensor_dequantize` takes a `bool`.** The demangled signature is
> `decode_tensor_dequantize(bool)`. By analogy with the sibling decode workers (the
> `reverse_operands`/`is_setup` boolean threaded through `decode_tensor_tensor_arith` etc.) this
> is a decode/setup-mode selector, not a dequant parameter. The dequant *behaviour* is driven
> entirely by the struct fields. `[MED/INFERRED — signature OBSERVED, role inferred from the
> sibling pattern.]`

The worker prologues, re-grounded byte-exact this pass (the device image is one of 16 Xtensa
ELF32 `ET_EXEC` images embedded in the host `libnrtucode_internal.so`; the dequant-bearing
primary image is at file off `0x2ef7e0`, entry `0x01005610`, `.text` VMA `0x01000000` == file
off `0x100`):

| Function | VMA (image @0x2ef7e0) | Prologue | Frame | Role |
|---|---|---|---|---|
| `decode_tensor_dequantize(bool)` | `0x01004df0` | `entry a1,192` ; `movi a10,-64` ; `and a8,a1,a10` ; `movsp a1,a8` | 192 B, SP force-aligned to 64 B | big worker: decode + `group=0` widen + dispatch |
| `TensorDequantize::proc_4bit_mx_8(unsigned int)` | `0x0100511c` | `entry a1,32` | 32 B (leaf) | the `group_size==8` MX path |

The 192-byte frame + 64-byte SP alignment on `decode_tensor_dequantize` is the standard
vector-register-spill frame shared with the other POOL tensor workers (the
`tensor_tensor_arith_impl` pattern). The `entry a1,32` leaf frame on `proc_4bit_mx_8` marks it as
a small compute helper. `[HIGH/OBSERVED — prologues re-grounded.]`

> **GOTCHA — the carve disassembles only in raw-binary mode.** The native
> `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) in **ELF mode** (`-d`) **refuses** to disassemble
> the carved image and renders `.text` as raw data words; only **raw-binary mode**
> (`-D -b binary -m xtensa --adjust-vma=0x01000000`) decodes mnemonics. The trampoline, the
> prologues, and the `const16`/`call` sequences at known boundaries decode reliably and
> byte-exact in raw mode; the interior FLIX bundles desync (≈30 % of lines emit as `.byte`; only
> ≈13 bundles in `decode` resync cleanly). `[OBSERVED — ELF-mode vs raw-mode discrepancy
> re-grounded this pass.]`

> **NOTE — the per-generation codename of the @0x2ef7e0 image is inferred, not byte-proven.** The
> host wrapper carries the codename strings (`SUNDA`/`CAYMAN`/`MARIANA`/`MARIANA_PLUS`/
> `MAVERICK`) and three `.data` descriptor tables reference the three `ET_EXEC` device images,
> but the descriptor name pointers resolve into host x86 getter code, so the exact
> image→codename binding is **not byte-provable** from this binary. The @0x2ef7e0 image is the
> first/lowest `ET_EXEC` and is **plausibly CAYMAN/NC-v3** (it implements the plain-`TENSOR3D`
> NC-v3 contract, §2), but a reimplementer should anchor to the **image file-offset / entry-point
> `0x01005610`**, not the codename. `[MED/INFERRED — codename binding; HIGH/OBSERVED — the image
> offset + the NC-v3-shaped contract.]`

> **QUIRK — the `n/5` reciprocal-multiply is the MX fingerprint.** At the head of
> `proc_4bit_mx_8` the decode shows `const16 a*,0xCCCD` followed by a `muluh` and a
> `srli …, 2` — the canonical unsigned-divide-by-5 idiom (`0xCCCCCCCD >> 34 == 1/5`). This
> realises the **8:5** source:dst element ratio the ISA mandates for `group_size == 8` (§4): for
> every 8 packed codes the kernel computes a block/dst count via `count / 5`. The `const16`
> carries only the **low half** `0xCCCD` of the `0xCCCCCCCD` multiplier (the upper half sits in
> an adjacent desync span), so the divisor is **HIGH as an idiom / MED as a byte-exact
> constant** — but the idiom + the ISA 8:5 ratio agree. `[HIGH idiom / MED exact divisor.]`

---

## 2. The operand struct — `NEURON_ISA_TPB_S3D3_TENS_DEQUANT_STRUCT` (64 bytes)

Source: `aws_neuron_isa_tpb_s3d3_tens_dequant.h` (CAYMAN/NC-v3), bound to opcode
`TensorDequantize` by the header comment, and static-asserted to **64 bytes**. This is the wire
format the decode stage reads. There is **one** src tensor and **one** dst tensor — **no**
`src1`/scale tensor, **no** zero-point field, **no** scale-address field. `[HIGH/OBSERVED.]`

```c
// aws_neuron_isa_tpb_s3d3_tens_dequant.h  (CAYMAN / NC-v3) — verbatim layout, offsets in (..)
typedef struct NEURON_ISA_TPB_S3D3_TENS_DEQUANT_STRUCT {
    NEURON_ISA_TPB_HEADER      header;               // 4   ( 0 -  3)  { opcode=0x7b, inst_word_len, debug_cmd, debug_hint }
    NEURON_ISA_TPB_EVENTS      events;               // 8   ( 4 - 11)  wait/update sync (wait_mode, idx, update_mode, idx, sem_value)
    NEURON_ISA_TPB_TENSOR3D    src_mem_pattern;      // 16  (12 - 27)  INPUT (packed quantized) 3-D strided access pattern
    uint8_t                    reserved0[4];         // 4   (28 - 31)  MUST be 0
    NEURON_ISA_TPB_DTYPE       in_dtype;             // 1   (32     )  == UINT32 (0x9) transport container
    NEURON_ISA_TPB_DTYPE       out_dtype;            // 1   (33     )  == UINT32 (0x9) transport container
    uint8_t                    num_active_channels;  // 1   (34     )  partition/channel count 1..128
    NEURON_ISA_TPB_DEQUANT_FMT dequant_fmt;          // 1   (35     )  THE FORMAT SELECTOR (the §4 table)
    uint8_t                    group_size;           // 1   (36     )  0 (no MX) or 8 (MX block of 8)
    uint8_t                    reserved1[7];         // 7   (37 - 43)  MUST be 0
    NEURON_ISA_TPB_TENSOR3D    dst_mem_pattern;      // 16  (44 - 59)  OUTPUT (FP8) 3-D strided access pattern
    uint8_t                    reserved2[4];         // 4   (60 - 63)  MUST be 0
} NEURON_ISA_TPB_S3D3_TENS_DEQUANT_STRUCT;
ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S3D3_TENS_DEQUANT_STRUCT) == 64,
                  "Error: NEURON_ISA_TPB_S3D3_TENS_DEQUANT_STRUCT is NOT 64B.");
```

The `TENSOR3D` access pattern is a 3-D strided descriptor (`common.h:649`):

```c
typedef struct NEURON_ISA_TPB_TENSOR3D {
    NEURON_ISA_TPB_ADDR4 start_addr;     // 4 — SBUF partition-offset / "shape-from-register" marker (ADDR4 marker byte)
    int16_t              step_elem[3];   // 6 — per-dim signed strides
    uint16_t             num_elem[3];    // 6 — per-dim element counts
} NEURON_ISA_TPB_TENSOR3D;               // 16 B total
```

The `start_addr` is an `ADDR4` whose top marker bits select the addressing flavour
(`ADDR4_MARKER_IMM = 0x00`, `ADDR4_MARKER_ADDR_REG = 0x80`, `ADDR4_MARKER_SHAPE_REG = 0x40`,
`ADDR4_MARKER_ADDR_SHAPE_REG = 0xC0`); `shape_from_register` (used by the element-count check
below) is the `SHAPE_REG`/`ADDR_SHAPE_REG` case where the shape rides a register and the static
ratio check is skipped. `[HIGH/OBSERVED — common.h:486–491 markers.]`

> **CORRECTION — the addressing type widened on NC-v4/v5; the report flattened it.** On
> **CAYMAN (NC-v3)** `src_mem_pattern` and `dst_mem_pattern` are plain `NEURON_ISA_TPB_TENSOR3D`
> (as above). On **MARIANA (NC-v4)** and **MAVERICK (NC-v5)** the same two fields are a
> **`NEURON_ISA_TPB_MEM_PATTERN3D` union** — still 16 bytes, still off 12 and off 44, struct
> still 64 B — but now any of three addressing shapes:
> ```c
> typedef union NEURON_ISA_TPB_MEM_PATTERN3D {  // common.h (maverick) :906
>     NEURON_ISA_TPB_TENSOR3D    t;   // the plain 3-D strided pattern (the NC-v3 form)
>     NEURON_ISA_TPB_INDIRECT16B i;   // indirect / gather addressing
>     NEURON_ISA_TPB_MXTENSOR_V2 mx;  // a Microscaled-Tensor descriptor (out-of-band scale_addr)
> } NEURON_ISA_TPB_MEM_PATTERN3D;
> ```
> The NC-v4/v5 validity contract correspondingly swaps the `tensor3d_valid` predicates for
> `mem3d_valid` / `check_m3d_active_channels` and adds `indirect_quadrant_check_src3d` and
> `indirect_quadrant_check_src3d_dst3d`, and the element-count check reaches through `.t`
> (`src_mem_pattern.t.start_addr`). The *dequant arithmetic and the ratios are unchanged* — only
> the operand's **addressing capability** grew (indirect + the `MXTENSOR_V2` mem-pattern path).
> A reimplementer targeting NC-v4+ must accept the union, not just a `TENSOR3D`.
> `[HIGH/OBSERVED — cayman vs mariana/maverick header diff this pass.]`

### 2.1 The validity contract (verbatim from the header's Rust-pseudocode comments)

The header ships the legality predicates as Rust-pseudocode comments — the specification both the
host ucode decoder and the device firmware implement. Read verbatim:

```rust
fn is_valid_tensor_dequantize(i: Inst, nc: NeuronCoreVersion) -> bool {
       has_valid_neuron_header(i)
    && has_valid_neuron_events(i)
    && has_tensor_dequantize_opcode(i)                                          // header.opcode == TensorDequantize (0x7b)
    && has_valid_nc_tens_dequant(nc)                                            // nc >= V3
    && check_active_channels(i.s3d3_tens_dequant.num_active_channels)          // != 0 && <= 128
    && has_valid_dequant_fmt(i.s3d3_tens_dequant.dequant_fmt)                  // in {1,2,3,4}, Invalid rejected
    && has_valid_group_size(i.s3d3_tens_dequant.group_size)                    // 0 || 8
    && has_valid_dequant_fmt_group_size(dequant_fmt, group_size)              // grp8 ONLY for fmt 1/2/3
    && s3d3_tens_dequant_reserved_zero(i)                                      // all 15 reserved bytes == 0
    && has_valid_tens_dequant_in_dtype(i.s3d3_tens_dequant.in_dtype)          // == UINT32
    && has_valid_tens_dequant_out_dtype(i.s3d3_tens_dequant.out_dtype)        // == UINT32
    && start_addr_active_channels(src.start_addr, num_active_channels)         // partition-aligned
    && start_addr_active_channels(dst.start_addr, num_active_channels)
    && tensor3d_valid(src, in_dtype,  WriteTensor::False, AllowedInPSUM::False, AllowedInSBUF::True)   // src read, SBUF only
    && tensor3d_valid(dst, out_dtype, WriteTensor::True,  AllowedInPSUM::False, AllowedInSBUF::True)   // dst write, SBUF only
    && s3d3_tens_dequant_element_count_check_t3d(i)                            // the §4 ratio gate
}
```

Material consequences for a reimplementer:

* **`nc >= V3`** — the op is **absent on NC-v2 (SUNDA)**; the SUNDA arch-isa tree ships **no**
  `aws_neuron_isa_tpb_s3d3_tens_dequant.h` (confirmed by file search) and the SUNDA POOL opcode
  map omits `0x7b`. `[HIGH/OBSERVED — header absence.]`
* **`check_active_channels`** — `num_active_channels != 0 && <= POOLING_NUM_CHANNELS` where
  `POOLING_NUM_CHANNELS = 128U` (`common.h:35`). The work is split across the POOL cores by this
  channel count. `[HIGH/OBSERVED — common.h:2343.]`
* **`in_dtype == out_dtype == UINT32`** is a *hard* constraint, not a default. The sub-byte codes
  do **not** ride the dtype field (§3). `[HIGH/OBSERVED.]`
* **Both tensors SBUF-only, never PSUM**; **src read-only, dst write**; all 15 reserved bytes
  (`reserved0[4] + reserved1[7] + reserved2[4]`) must be 0. `[HIGH/OBSERVED.]`

---

## 3. Why the dtype fields are `UINT32` transport, not logical dtypes

The struct's `in_dtype`/`out_dtype` are both pinned to **`UINT32` (`0x9`)** by
`has_valid_tens_dequant_in_dtype` / `has_valid_tens_dequant_out_dtype`. They are **not** the
logical element dtype — they are a 32-bit **transport container**. The sub-byte codes are packed
into `UINT32` words in SBUF, and the *logical* input/output micro-format is carried entirely by
the 1-byte `dequant_fmt` (off 35). `[HIGH/OBSERVED.]`

Reconciling with [the dtype model](dtype-model.md):

* The dequant **input** micro-formats (FP4 E2M1, INT4, NF4, FP6 E2M3) correspond conceptually to
  the extended dtype codes `FP4_EXP2 = 0x10`, `INT4 = 0x12` (and the codebook/sub-byte forms),
  but they are **never passed as `NEURON_ISA_TPB_DTYPE` in this instruction** — they are packed
  into `UINT32` and named by `dequant_fmt`. So the dequant kernel does **not** consume the
  extended sub-byte dtype codes through the dtype field. `[HIGH/OBSERVED.]`
* The dequant **output** FP8 forms map to `FP8_EXP5 (0xF) = E5M2` (for `fmt 1/2`) and
  `FP8_EXP4 (0xE) = E4M3` (for `fmt 3/4`), but again `out_dtype` is `UINT32` transport — the
  logical FP8 form is set by `dequant_fmt`. (Per the dtype model, `FP8_E4/E5` have no pre-FP8
  `c10::ScalarType`, so the FP8 output is an ISA/ucode intermediate, not marshallable back
  through the `at::Tensor` custom-op ABI in v0.21.2.0.) `[HIGH cross-link.]`
* **Dispatch is on `dequant_fmt`, not on the dtype enum.** The kernel does *not* run the
  `is_valid_dtype`/`beqi`-chain dtype dispatch that Cast/Copy/ALU run; it switches on the 1-byte
  `dequant_fmt` (4 arms) × `group_size` (0 vs 8). The branch is a compiled C++ switch in FLIX —
  not a `.rodata` jump table — the same structural finding as the sibling `tensor_tensor_arith`
  decode. `[HIGH structure / MED exact branch bytes — FLIX desync.]`

> **GOTCHA — `UINT32` is the *only* legal dtype here, and it means "I am a packed transport
> word", not "I am a uint32 value".** A reimplementer must **not** try to widen/convert `UINT32`
> as an integer. It is a 32-bit window holding 8 packed 4-bit codes (for the 4-bit formats) or a
> 6-bit-packed stream (for FP6). The packing is the *real* input format; `dequant_fmt` is the
> only field that tells you how to unpack it. `[HIGH/OBSERVED.]`

---

## 4. The format-expansion model — `NEURON_ISA_TPB_DEQUANT_FMT`

Source: `aws_neuron_isa_tpb_common.h:806` (CAYMAN; **byte-identical** in MARIANA and MAVERICK —
only line numbers differ). A 1-byte `NEURON_ISA_PACKED` enum. This is the dequant "dtype
dispatch": `dequant_fmt` (off 35) selects the input micro-format **and** the FP8 output format.

```c
// aws_neuron_isa_tpb_common.h:806 (CAYMAN) — verbatim
typedef enum NEURON_ISA_TPB_DEQUANT_FMT {
    NEURON_ISA_TPB_DEQUANT_FMT_INVALID     = 0,
    NEURON_ISA_TPB_DEQUANT_FMT_E2M1TO_E5M2 = 1,    // FP4_E2M1 to FP8_E5M2
    NEURON_ISA_TPB_DEQUANT_FMT_INT4TO_E5M2 = 2,    // INT4 to FP8_E5M2
    NEURON_ISA_TPB_DEQUANT_FMT_NF4TO_E4M3  = 3,    // NF4 to FP8_E4M3
    NEURON_ISA_TPB_DEQUANT_FMT_E2M3TO_E4M3 = 4,    // FP6_E2M3 to FP8_E4M3
} NEURON_ISA_PACKED NEURON_ISA_TPB_DEQUANT_FMT;
```

| `dequant_fmt` | value | input micro-format | output FP8 | `group=8`? | src:dst element ratio |
|---|:-:|---|---|:-:|---|
| `INVALID`       | 0 | — | — | — | (rejected) |
| `E2M1TO_E5M2`   | 1 | FP4 E2M1 (4-bit) | FP8 E5M2 (`0xF`) | yes | 2:1 (grp0) / 8:5 (grp8) |
| `INT4TO_E5M2`   | 2 | INT4 (4-bit signed) | FP8 E5M2 (`0xF`) | yes | 2:1 (grp0) / 8:5 (grp8) |
| `NF4TO_E4M3`    | 3 | NF4 (4-bit codebook) | FP8 E4M3 (`0xE`) | yes | 2:1 (grp0) / 8:5 (grp8) |
| `E2M3TO_E4M3`   | 4 | FP6 E2M3 (6-bit) | FP8 E4M3 (`0xE`) | **no** (grp0) | 4:3 |

### 4.1 The element-count / ratio gate (verbatim)

The `s3d3_tens_dequant_element_count_check_t3d` predicate is the authoritative source of the
packing ratios. It is what makes the SIMD loop a **ratio loop** (§7). Read verbatim (CAYMAN):

```rust
fn s3d3_tens_dequant_element_count_check_t3d(i: Inst) -> bool {
       shape_from_register(src.start_addr)         // if shape rides a register the static check is skipped
    || shape_from_register(dst.start_addr)
    // --- 4-bit formats, no microscaling: 2 src codes per 1 dst FP8 ---
    || (   (dequant_fmt == E2M1ToE5M2 || dequant_fmt == INT4ToE5M2 || dequant_fmt == NF4ToE4M3)
        && group_size == 0
        && ratio_element_count_t3d(src, dst, 2, 1)  // count(src)*2 == count(dst)*1
        && is_num_elem_mult_n_t3d(src, 1))
    // --- FP6 (E2M3), no microscaling: 4 packed 6-bit codes per 3 dst FP8 ---
    || (   dequant_fmt == E2M3ToE4M3
        && group_size == 0
        && ratio_element_count_t3d(src, dst, 4, 3)  // count(src)*4 == count(dst)*3
        && is_num_elem_mult_n_t3d(src, 3))          // src count multiple of 3
    // --- 4-bit MX formats, group of 8: 8 src units per 5 dst FP8 (the proc_4bit_mx_8 path) ---
    || (   (dequant_fmt == E2M1ToE5M2 || dequant_fmt == INT4ToE5M2 || dequant_fmt == NF4ToE4M3)
        && group_size == 8
        && ratio_element_count_t3d(src, dst, 8, 5)  // count(src)*8 == count(dst)*5
        && is_num_elem_mult_n_t3d(src, 5))          // src count multiple of 5
}
```

* `group=0`, 4-bit (FP4/INT4/NF4): **2 codes → 1 FP8** (two 4-bit codes per `UINT32`-transport
  pair-of-bytes; src holds 2× the sub-byte codes as dst FP8 elements).
* `group=0`, FP6: **4 codes → 3 FP8** (four packed 6-bit codes occupy the space of 3 FP8
  bytes — `4 × 6 = 24 = 3 × 8` bits — the dense FP6 packing).
* `group=8`, 4-bit MX: **8 source units → 5 dst FP8.** Each block of 8 packed codes carries a
  shared MX scale that is *consumed* (not emitted) during expansion, so the block of 8 source
  units yields 5 FP8 outputs — precisely the `n/5` reciprocal-multiply the firmware computes.

> **NOTE — the 8:5 ratio implies the in-band scale is one of the 8 "units".** The most
> consistent reading of `count(src)*8 == count(dst)*5` is: a group of **8 source units** = 5
> data codes-worth of payload + the per-block scale overhead, expanding to **5 dst FP8**. The
> exact in-band bit packing of the block-of-8 scale is **not byte-pinned** (the dequant struct
> names no `scale_dtype` — the scale is in-band), so whether that in-band scale is itself E8M0
> is `MED/INFERRED` (the E8M0 fact is OBSERVED only for the *separate* NC-v5 `MXTensorV2`
> out-of-band scale; see [dtype-model §4](dtype-model.md) and [mx-dequant](mx-dequant.md)).
> `[HIGH ratio / MED exact in-band scale format.]`

### 4.2 Per-format dequant arithmetic (C pseudocode)

Each arm is the **bit-unpack → value** mapping. The format-pair semantics (which input format,
which output FP8) are `[HIGH/OBSERVED]` from the enum comments; the in-FLIX micro-op order is
`[MED/INFERRED]` (FLIX desync). The pseudocode names the recovered IVP intrinsics where known.

**FP4 E2M1 → FP8 E5M2** (`fmt 1`) — an **exponent-rebias reinterpret**, *not* a multiply
(group0). FP4 E2M1 is `[s:1 | e:2 | m:1]` with exponent bias 1; FP8 E5M2 is `[s:1 | e:5 | m:2]`
with bias 15. Sub-normals (`e==0`) and the small finite range must be handled explicitly.

```c
// FP4_E2M1 (1-2-1, bias 1) -> FP8_E5M2 (1-5-2, bias 15). group_size==0 (no scale).
// Recovered datapath: nibble-unpack (ivp_sel2nx8i / ivp_sel2nx8i_s4) -> bitfield rebias/shift -> sign via ivp_neg*/dsel.
static inline uint8_t fp4_e2m1_to_fp8_e5m2(uint8_t code4 /* low nibble */) {
    uint8_t  s   =  (code4 >> 3) & 0x1;          // sign
    uint8_t  e2  =  (code4 >> 1) & 0x3;          // 2-bit exponent (bias 1)
    uint8_t  m1  =   code4       & 0x1;          // 1-bit mantissa
    uint8_t  out_e, out_m;
    if (e2 == 0) {                               // FP4 sub-normal / zero
        if (m1 == 0) { out_e = 0;  out_m = 0; }  // +/-0
        else {                                   // FP4 0.5 (sub-normal) -> normalised E5M2
            out_e = 15 - 1;  out_m = 0;          // value 0.5 = 2^-1
        }
    } else {                                     // FP4 normal: value = 2^(e2-1) * (1.m1)
        out_e = (uint8_t)(e2 - 1 + 15);          // rebias bias-1 exponent into bias-15
        out_m = (uint8_t)(m1 << 1);              // 1-bit mantissa zero-extends into the top of the 2-bit field
    }
    return (uint8_t)((s << 7) | (out_e << 2) | out_m);
}
```

**INT4 → FP8 E5M2** (`fmt 2`) — an **int→float convert** of a tiny signed range (−8..+7).
Nibble-unpack, sign-extend, then `int → fp` convert (the IVP `float`/`ufloat` leg), then pack to
E5M2. No bias trickery — the integer value *is* the float value.

```c
// INT4 (signed two's-complement, -8..+7) -> FP8_E5M2. group_size==0.
// Recovered datapath: nibble-unpack + signed sext (ivp_sel2nx8i + sign-extend) -> int->float convert -> E5M2 pack.
static inline uint8_t int4_to_fp8_e5m2(uint8_t code4 /* low nibble */) {
    int8_t  v = (int8_t)(code4 << 4) >> 4;       // sign-extend 4-bit -> int8 (-8..+7)
    float   f = (float)v;                        // exact for |v| <= 7 (E5M2 has 2 mantissa bits but ints <=7 fit)
    return fp32_to_fp8_e5m2_rne(f);              // narrow convert via the FP32 hub, round-to-nearest-even
}
```

**NF4 → FP8 E4M3** (`fmt 3`) — a **16-entry table lookup**. NF4 (NormalFloat4) is a
**non-uniform** codebook: the 4-bit code is a *table index*, not a float field. The kernel does a
16-way lookup (the `const16`-loaded codebook + `ivp_dsel*`/`ivp_sel*` lane-select map code → fp
value) and re-encodes the looked-up value to FP8 E4M3.

```c
// NF4 (NormalFloat4) -> FP8_E4M3. group_size==0.
// Recovered datapath: nibble index -> 16-way lane-select lookup (ivp_dselnx16t over a const16-loaded codebook)
//                     -> FP8_E4M3 re-encode. The codebook is the QLoRA NF4 quantile table (canonical).
static const float NF4_CODEBOOK[16] = {                 // index 0..15 (canonical NF4 / QLoRA values)
    -1.0f,               -0.6961928009986877f, -0.5250730514526367f, -0.39491748809814453f,
    -0.28444138169288635f, -0.18477343022823334f, -0.09105003625154495f, 0.0f,
     0.07958029955625534f,  0.16093020141124725f,  0.24611230194568634f, 0.33791524171829224f,
     0.44070982933044434f,  0.5626170039176941f,   0.7229568362236023f,  1.0f
};
static inline uint8_t nf4_to_fp8_e4m3(uint8_t code4 /* low nibble = table index */) {
    float v = NF4_CODEBOOK[code4 & 0xF];                 // non-uniform lookup (NOT a bit reinterpret)
    return fp32_to_fp8_e4m3_rne(v);                      // narrow convert, RNE
}
```

> **GOTCHA — NF4 is a lookup, not a bitfield.** Unlike FP4/INT4/FP6, the NF4 4-bit code carries
> **no** sign/exp/mantissa structure — it is an **index** into a 16-entry codebook of standard-
> normal quantiles. A reimplementer that bit-unpacks NF4 like a tiny float gets garbage. The
> firmware builds the codebook/select constants as **inline `const16` fp16 immediates inside
> `proc_4bit_mx_8`** — e.g. `const16 a13,0x43a8 @0x01005145` and `const16 a10,0x22d5 @0x01005158`,
> each appearing **exactly once** in `.text` (re-grounded this pass, confirming they are real
> instructions, not desync artifacts) — and lane-selects through the resulting table. The
> canonical NF4 table above is the published QLoRA NormalFloat4 codebook (`[HIGH that it is a
> 16-entry lookup / MED that the device table equals the canonical QLoRA values]`). See
> [The dtype model §7 walls](dtype-model.md).

> **CORRECTION — the NF4 codebook is NOT a `.rodata` lookup table.** An independent re-carve of
> `libnrtucode_internal.so` this pass found that the device-image `.rodata` (VMA `0x02000000`,
> file off `0x7080`, only `0x1ec` bytes) holds **exp/log polynomial seed constants** (e.g. the
> `ln2 = 0.693147`, `log2 e = 1.4427` clusters at off `0x71c0`/`0x7240`), **not** an NF4
> quantile table — the canonical NF4 spread (−1.0 … +1.0) is absent from `.rodata`. The NF4
> lookup is realised from the **inline `const16` immediates inside the format arm** (above) plus
> `ivp_dsel*`/`ivp_sel*` lane selects, not a `.rodata` LUT. A reimplementer should build the
> NF4 codebook into the lookup datapath, not expect a `.rodata` array. `[HIGH/OBSERVED —
> `.rodata` content re-grounded; the `0x43a8`/`0x22d5` are inline instruction immediates, not
> table bytes.]`

**FP6 E2M3 → FP8 E4M3** (`fmt 4`, group0 only) — an **exponent-rebias with mantissa
pass-through**. FP6 E2M3 is `[s:1 | e:2 | m:3]` (bias 1); FP8 E4M3 is `[s:1 | e:4 | m:3]` (bias
7). The mantissa width is **identical (3 bits)**, so the mantissa passes straight through;
only the exponent is re-biased. This is the cheapest arm (no lookup, no int convert, no
multiply). FP6 can **never** be MX.

```c
// FP6_E2M3 (1-2-3, bias 1) -> FP8_E4M3 (1-4-3, bias 7). group_size==0 ONLY (4:3 packing, src%3==0).
// Recovered datapath: 6-bit unpack (4 codes / 3 bytes) -> exponent rebias -> mantissa copy -> E4M3 pack.
static inline uint8_t fp6_e2m3_to_fp8_e4m3(uint8_t code6 /* 6 valid bits */) {
    uint8_t s   = (code6 >> 5) & 0x1;            // sign
    uint8_t e2  = (code6 >> 3) & 0x3;            // 2-bit exponent (bias 1)
    uint8_t m3  =  code6       & 0x7;            // 3-bit mantissa  <-- SAME width as E4M3 mantissa
    uint8_t out_e;
    if (e2 == 0) {                               // FP6 sub-normal / zero: m3 already a sub-normal mantissa
        out_e = (m3 == 0) ? 0 : (7 - 1);         // map sub-normal magnitude into E4M3 (range easily fits)
    } else {
        out_e = (uint8_t)(e2 - 1 + 7);           // rebias bias-1 exponent into bias-7
    }
    return (uint8_t)((s << 7) | (out_e << 3) | m3);   // mantissa passes through unchanged
}
```

> **CORRECTION / NOTE — there is no `(q − zp) × scale` affine path in op `0x7b`.** A
> reimplementer expecting the textbook integer-affine dequant (explicit per-tensor/per-channel
> scale + integer zero-point) will not find it here. `op 0x7b` is a micro-format **expansion**
> (+ optional MX block scale). The affine integer dequant
> (`scale_const`/`bias_const`, `Inline_dequantization`) lives in the **Activation** engine
> (`tpb_activation_entries.h`), a different engine — *not* this POOL kernel. `[HIGH/OBSERVED —
> struct has no zp/scale field; affine path is in a different engine.]`

---

## 5. The scale model — per-block (MX) or none

The scale granularity is fully determined by `group_size` (off 36), which `has_valid_group_size`
constrains to `{0, 8}`:

| `group_size` | scale | granularity | path |
|:-:|---|---|---|
| `0` | **none** | — | in-line format-widen in `decode_tensor_dequantize` (2:1 or 4:3) |
| `8` | **one shared scale per block of 8 source codes** | **per-block (MX)** | `proc_4bit_mx_8` (8:5) |

* **`group_size == 0`** — no scale. Pure format widen: the sub-byte code becomes an FP8 element
  by bit-reinterpret (FP4/FP6), int→float (INT4), or lookup (NF4). "Dequant" here means format
  **expansion** only. `[HIGH/OBSERVED.]`
* **`group_size == 8`** — **MX micro-scaling.** One shared scale per **block of 8 source codes**.
  The scale is **not a separate operand**: it rides **in-band** within each group of 8 (the
  shared-exponent / per-block scale byte) and is consumed during the 8→5 expansion. This is
  **per-block** granularity (block = 8 elements), **not** per-tensor and **not** per-channel.
  `proc_4bit_mx_8` ("`_mx_8`" = MX, group of 8) is exactly this path, and its `n/5`
  reciprocal-multiply realises the 8:5 ratio. `[HIGH/OBSERVED — gate + element-count + firmware
  idiom.]`
* **MX is legal only for the three 4-bit formats.** `has_valid_dequant_fmt_group_size` admits
  `group_size == 8` **only** for `E2M1ToE5M2`, `INT4ToE5M2`, `NF4ToE4M3`. FP6 (`E2M3ToE4M3`) has
  **no** `group_size == 8` form. `[HIGH/OBSERVED — gate.]`

```rust
fn has_valid_dequant_fmt_group_size(fmt: DequantFmt, grp: u8) -> bool {
       (grp == 0)
    || (grp == 8 && fmt == DequantFmt::E2M1ToE5M2)
    || (grp == 8 && fmt == DequantFmt::INT4ToE5M2)
    || (grp == 8 && fmt == DequantFmt::NF4ToE4M3)
    // NOTE: E2M3ToE4M3 (FP6) is absent — FP6 can NEVER be MX.
}
```

The per-block scale is applied by the vector **multiply** ops in `proc_4bit_mx_8`
(`ivp_mulus4tan16xr16` / `ivp_muluupan16xr16` / `ivp_dmulqa2n8xr8`), whose packed-register
(`pr<N>`) operand is the broadcast per-block scale. There is **no per-channel scale and no
per-tensor scale operand** anywhere in `s3d3_tens_dequant`. `[HIGH that a per-block scale-multiply
happens / MED exact register routing — FLIX desync.]`

> **NOTE — two distinct MX surfaces, do not conflate.** This POOL in-band block-of-8 dequant
> (NC-v3+) **predates** the NC-v5 out-of-band `MXTensorV2` / `QuantizeMX(0xe3)` /
> `MatmulMX(0x0A)` MX wave (which carries an explicit `scale_addr` + `scale_dtype = SFP8_E8`
> E8M0). They are chronologically and structurally separate. See
> [the MX dequant page](mx-dequant.md) for the `proc_4bit_mx_8` deep-dive and the out-of-band
> mechanism, and [the compiler MX path](../../compiler/mx-path.md) (planned) for how the graph
> compiler routes MX tensors to one or the other. `[HIGH/OBSERVED.]`

---

## 6. The vector (SIMD) datapath — recovered IVP intrinsics

`ncore2gp` is a ConnX-BBE-class Q7 Vision vector core: 512-bit vector registers
(`nx16` = 32×int16, `2nx8` = 64×int8, `n_2x32` = 16×int32, `nxf16`/`n_2xf32` float). The dequant
loop processes a vector of codes per iteration. The IVP vocabulary harvested per-function (raw
binary + merged-prop, both agree on the emitted mnemonics) maps onto the
**unpack → (bias/subtract) → scale-multiply → widen-extract → saturate-clamp → fp-gate** chain.
`[MED–HIGH that these intrinsics are emitted; the mid-bundle ordering is structural.]`

**`decode_tensor_dequantize` (0x4df0..0x511c), the `group=0` widen path:**

| Stage | IVP intrinsics observed | role |
|---|---|---|
| sub-byte unpack | `ivp_sel2nx8i` (×2), `ivp_dselnx16t`, `ivp_dseln_2x32t`, `ivp_labvdcmprs2nx8_xp` | strided/compressed `TENSOR3D` vector load + nibble unpack |
| scale multiply | `ivp_muluupan16xr16` (×2), `ivp_mulsupn16xr16` | unsigned/signed widening MAC by a packed scale register |
| subtract / bias | `ivp_babssub2nx8` (×2), `ivp_babssubunx16` | vector subtract (bias / range adjust) |
| convert / fp | `ivp_movpint16`, `ivp_negnxf16t`, `ivp_oltnxf16t` | int→16b move; fp16 sign; fp16 ordered compare (range/NaN gate) |

**`proc_4bit_mx_8` (0x511c..0x55e6), the MX group-of-8 path:**

| Stage | IVP intrinsics observed | role |
|---|---|---|
| 4-bit unpack | `ivp_sel2nx8i_s4` (×2, `_s4` = 4-bit select), `ivp_l2a4nx8_ip`, `ivp_dselnx16` | load-align 4-bit×8 with post-increment (the group-of-8 packed load) |
| scale multiply (MX) | `ivp_mulus4tan16xr16`, `ivp_dmulqa2n8xr8`, `ivp_muln_2x32` | unsigned×signed widening MAC by the per-block MX scale; packed-reg scale `pr0` |
| widen-accum extract | `ivp_cvtg48n_2x32l` | 48-bit accumulator → 32-bit lane extract (the widening-MAC → narrow, the convert-out step) |
| saturate / clamp | `ivp_bminnx16` (×3), `ivp_bminn_2x32` (×3), `ivp_bmin2nx8`, `ivp_bmaxn_2x32`, `ivp_bmaxun_2x32` | saturating min/max — clamp to the FP8 E5M2/E4M3 representable range |
| fp compare | `ivp_ultn_2xf32t` | fp32 unordered compare (overflow/NaN gate) |

The single FLIX bundle in `decode_tensor_dequantize` that resyncs cleanly (`@0x01004fc8`)
captures the whole micro-op chain in one VLIW word:

```text
// decode_tensor_dequantize @0x01004fc8 — one FLIX bundle, decoded clean via merged-prop:
{ bbci.w15 a2,30,...;  extui a0,a4,1,4;        // extract a 4-bit field (fmt/group decode)
  ivp_muluupan16xr16 wv3,v0,v23,pr3;           // MULTIPLY by scale (pr3 = packed scale)
  ivp_sel2nx8i       v10,v26,v7,53;            // UNPACK sub-byte codes
  ivp_babssub2nx8    vb9,v8,v11,v1 }           // SUBTRACT (bias / zero adjust)
```

i.e. the dequant inner step is, in FLIX-parallel, **unpack → (subtract) → multiply-by-scale →
saturate → pack**. `[HIGH/OBSERVED bundle.]`

### 6.1 Output convert / rounding

The FP8 output is produced by the **widen-accumulator extract** (`ivp_cvtg48n_2x32l` — the
48-bit MAC accumulator narrowed to a 32-bit lane) followed by **saturating clamps**
(`ivp_bmin*`/`ivp_bmax*`) to the E5M2/E4M3 representable range, then the FP8 byte-pack. This is
the same `nibble-unpack → ufloat → scale-MAC → bmin/bmax clamp` skeleton documented for the
convert/pack ISA slice — see [ISA Batch 20 — fp16 Convert (hp_cvt)](../../isa/ref/b20-hp-cvt.md)
(the FP8/FP4 routing hub) and [ISA Batch 13 — fp32 Convert (sp_cvt)](../../isa/ref/b13-sp-cvt.md)
(the saturating `trunc`/`utrunc` narrowing convention, NaN→max). `[HIGH datapath family.]`

> **NOTE — rounding mode is the convert-intrinsic default (RNE), not an explicit op.** The
> narrowing convert's rounding is the `ncore2gp` default for the `*cvtg*`/`*pan*`/`*sup*` family
> — round-to-nearest-even on the soft-float/widen path. **No** explicit round-mode-control opcode
> was isolated in the decoded bundles; the rounding is implicit in the convert intrinsic. (This
> matches the dtype model's FP32-hub rule: float narrowing is FSR-driven, RNE by default; the
> fp→int leg, where reached, is round-toward-zero.) `[MED — implicit RNE; no explicit round-mode
> op observed.]`

---

## 7. The loop / address generation

* **Ratio loop, not a 1:1 loop.** The kernel walks the dst `TENSOR3D` iteration space and indexes
  the src `TENSOR3D` by its own 3-D strided pattern (`start_addr + step_elem[3] × num_elem[3]`).
  Because the src and dst element counts obey the §4 ratios (2:1, 4:3, or 8:5), the inner step
  consumes **K source units** and emits **N dst FP8** with `K:N ∈ {2:1, 4:3, 8:5}`. Strided
  vector loads (`ivp_labvdcmprs2nx8_xp` for the compressed `group=0` path, `ivp_l2a4nx8_ip` for
  the group-of-8 MX path) fetch the packed codes; strided vector stores write the FP8 output.
  `[HIGH from struct + IVP / MED exact loop bytes.]`
* **`proc_4bit_mx_8`'s `count / 5` computes the 8:5 trip count** (the `0xCCCCCCCD` divide-by-5
  idiom). `[HIGH idiom / MED exact divisor.]`
* **Channel split.** `num_active_channels` (off 34) partitions the work across the POOL cores by
  `get_cpu_id()` — the standard per-core channel split, `POOLING_NUM_CHANNELS = 128` max.
  `[HIGH api / MED that dequant uses it identically.]`
* **The loop back-edge is in a desync span.** No zero-overhead `loop`/`loopgtz` was cleanly
  decoded; the back-edge is a `bnez`/`blt`-based branch inside a FLIX-desync region. Reported
  honestly. `[MED.]`

---

## 8. Cross-generation stability

* **Same family across the embedded `ET_EXEC` device images.** All three primary `ET_EXEC`
  Xtensa images embedded in `libnrtucode_internal.so` carry **identical** `.xt.prop` dequant
  section names and the same op-`0x7b` dispatch; cross-image VMAs shift only by a fixed build
  delta — `decode_tensor_dequantize` **+0x40** (`0x01004df0` → `0x01004e30`) and
  `proc_4bit_mx_8` **+0x48** (`0x0100511c` → `0x01005164`) on the +0x40 sibling — both
  re-verified byte-exact this pass (same trampoline, same `entry a1,32` + `const16 0xcccd;
  muluh; srli …,2` reciprocal at the shifted VMA). `[HIGH/OBSERVED — cross-image deltas
  re-grounded.]`
* **The dequant *contract* did not change across generations.** The `DEQUANT_FMT` enum (5
  values), the in/out `== UINT32` constraint, the `group_size ∈ {0,8}` rule, the 64-byte struct
  size, and the §4 ratios are **byte-identical** across the CAYMAN (NC-v3), MARIANA (NC-v4), and
  MAVERICK (NC-v5) headers. **Only the addressing type widened** (`TENSOR3D` → `MEM_PATTERN3D`
  union on NC-v4/v5, §2). `[HIGH/OBSERVED — header diff.]`
* **The wider MX/SFP8 dtype space lives in *other* instructions.** The big MX dtype expansion
  ABI-06 documents (FP8_EXP2, INT4, SFP8_E8..E5, the `MXTensorV2` out-of-band scale) is consumed
  by `QuantizeMX`/`MatmulMX`/`LdweightsMX`, **not** by this opcode. `op 0x7b` consumes its
  sub-byte input through `dequant_fmt` + `UINT32` transport. `[HIGH/OBSERVED.]`
* **Per-gen recoverability.** The named functions are byte-recoverable only on the prop-bearing
  CAYMAN/MARIANA-class images. The MAVERICK device image is `ET_DYN`, stripped (no `.symtab`, no
  `.xt.prop`), so no dequant symbol is recoverable there — but its **MAVERICK arch-isa header**
  fully specifies the (widened-addressing) contract. SUNDA (NC-v2) has no dequant at all.
  `[HIGH/OBSERVED.]`

---

## 9. Honest limitations / desync flags

* The compute bodies of `decode_tensor_dequantize` and `proc_4bit_mx_8` are hand-scheduled FLIX
  VLIW with interleaved literal/selector bytes that **desync** under stock `xtensa-elf-objdump`.
  Even with the merged property table loaded, only a handful of FLIX bundles resync cleanly (≈3
  in `decode`, ≈7 in `proc_4bit_mx_8`); the rest linear-sweep into spurious scalar ops / `.byte`.
  The bundles that **do** decode are reported (§6); the desynced spans are **not** reported as
  real instructions. This is the documented stripped-firmware / raw-mode limitation. `[flagged.]`
* Consequently the **exact per-format switch-arm byte sequence**, the **exact MX-scale register
  routing**, and the **exact loop back-edge** are recovered **structurally** (IVP mnemonics + the
  `n/5` idiom + the authoritative ISA validity contract), **not** as a byte-exact linear decode.
  The dequant **model** (format-expansion + per-block MX scale; the §2/§4/§5 facts) is
  `HIGH/OBSERVED` from a multi-source agreement: the shipped ISA header + the device
  `kernel_info_table` op-`0x7b` dispatch bytes + the `.xt.prop` function names + the prologues +
  the surviving compute bundles. `[flagged.]`
* The **NF4 device codebook bytes** are observed only as **inline `const16` fp16 immediates**
  inside `proc_4bit_mx_8` (byte-confirmed present, e.g. `0x43a8 @0x01005145`, `0x22d5
  @0x01005158`) plus lane-selects — **not** as a `.rodata` lookup table (the device `.rodata`
  holds exp/log polynomial seeds, re-grounded this pass). The full 16-entry mapping is not
  byte-pinned past the FLIX desync; the canonical QLoRA NF4 table (§4.2) is the inferred
  reference. `[HIGH that it is inline-immediate + lane-select, not `.rodata` / MED the exact
  16 values.]`
* The **in-band block-of-8 MX scale bit-format** is not byte-pinned (the struct names no
  `scale_dtype`); whether it is E8M0 is `INFERRED` from the separate NC-v5 `MXTensorV2`. `[MED.]`
* **Output rounding** = round-to-nearest-even is inferred as the `ncore2gp` convert-intrinsic
  default; no explicit round-mode opcode was isolated. `[MED.]`

---

## 10. Confidence ledger

**`HIGH / OBSERVED`:**

* opcode `0x7b` `TENSOR_DEQUANTIZE` (`common.h:214`); the `kernel_info_table` idx16 / op-`0x7b`
  route to trampoline `0x01004dc4` (state ptr `0x02000480`) → `decode_tensor_dequantize`
  `@0x01004df0`; the two function VMAs + prologues (`entry a1,192` / `entry a1,32`); the `n/5`
  reciprocal idiom in `proc_4bit_mx_8`.
* the `s3d3_tens_dequant` 64-byte struct layout + all field offsets, and its verbatim validity
  contract (`in/out == UINT32`, `group ∈ {0,8}`, `fmt ∈ {1..4}`, `nc >= V3`, `channels <= 128`,
  SBUF-only, reserved == 0); `ISA_STATIC_ASSERT == 64`.
* the `DEQUANT_FMT` 5-value enum and the src:dst element ratios (2:1 / 4:3 / 8:5) from the
  verbatim element-count check; `group_size==8` legal only for the three 4-bit formats; FP6
  never MX.
* the scale model: `group0` = no scale; `group8` = per-**block** (block of 8) in-band MX scale;
  no per-tensor/per-channel/zero-point operand exists.
* the IVP datapath vocabulary per function, incl. the key compute bundle `@0x4fc8`.
* the cross-gen `TENSOR3D` → `MEM_PATTERN3D` (union) addressing widening on NC-v4/v5; identical
  `DEQUANT_FMT`/struct-size/ratios across CAYMAN/MARIANA/MAVERICK.
* the `SX-FW-18` correction: idx14 = op `0xbe` = `GET_SEQUENCE_BOUNDS`, idx15 = op `0xf2` =
  `NONZERO_WITH_COUNT` (neither is dequant).

**`INFERRED` (forced by the ISA contract / IVP semantics):**

* the exact per-format conversion micro-op order (exp-rebias for FP4/FP6, int→float for INT4,
  16-entry lookup for NF4) — forced by the format definitions + the unpack/select/multiply IVPs;
  `MED` on the exact in-FLIX micro-op sequence.
* the per-block MX scale rides the packed-register (`pr<N>`) operand of the multiply MACs — `MED`
  on exact register routing.
* output rounding = RNE (`ncore2gp` convert default); the NF4 device table = the canonical QLoRA
  codebook; the in-band MX scale = E8M0 — all `MED`.

**Gaps (stated, not papered over):** FLIX-desynced compute bodies ⇒ no byte-exact linear decode
of the switch arms / loop back-edge; NF4 device table bytes and the in-band scale bit-format not
byte-pinned; MAVERICK device image stripped (contract recovered from its header, not its bytes).

---

## Cross-references

* [The Unified Datatype Model](dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` enum, the
  `UINT32`-transport rule, the FP8 `E4M3`/`E5M2` codes, and the two MX surfaces (§2.4 / §2.6 own
  the dtype identities this page consumes).
* [MX dequant — `proc_4bit_mx_8` deep-dive](mx-dequant.md) — the `group_size==8` block-of-8 MX
  path in full, the `n/5` ratio compute, and the out-of-band `MXTensorV2` mechanism contrasted.
* [ISA Batch 13 — fp32 Convert (sp_cvt)](../../isa/ref/b13-sp-cvt.md) — the saturating
  `trunc`/`utrunc` narrowing convention used on the convert-out step.
* [ISA Batch 20 — fp16 Convert (hp_cvt)](../../isa/ref/b20-hp-cvt.md) — the FP8/FP4 convert
  routing hub (`unpack → ufloat → scale-MAC → bmin/bmax clamp`) the dequant output reuses.
* [The compiler MX path](../../compiler/mx-path.md) — *planned*: how the graph compiler routes a
  quantized/MX tensor to the POOL in-band dequant vs the NC-v5 out-of-band MX wave.
* [The Confidence & Walls model](../../reference/confidence-model.md) — the tag and wall
  taxonomy used throughout.
```