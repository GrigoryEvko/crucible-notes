# RangeSelect

> **Scope.** RangeSelect is **opcode `0xbc`** (`// Y` = tested/maintained) — the
> maintained **two-sided range-predicate select** of the NeuronCore GPSIMD ISA, and
> the sole maintained range member of the **`S2D2_RS`** instruction family. For every
> source element `x` it evaluates a *programmable* two-comparator predicate
> `(x cmp0 bound0) AND (x cmp1 bound1)`, writes `x` where the predicate holds and the
> hard-pinned sentinel **`-FLT_MAX`** where it does not, and *optionally* Max-reduces
> the result. This page decodes the operand struct
> (`NEURON_ISA_TPB_S2D2_RS_STRUCT`, **64 B**, compile-verified this session), proves
> the predicate-select-fill-reduce datapath from the shipped header validator bytes,
> tabulates the dtype matrix, classifies the dispatch surface as **DVE-engine,
> hardware-native** (not a POOL software kernel) two independent ways, and proves the
> **SUNDA absence** five ways. It runs on the **Cadence Tensilica Vision-Q7 NX "Cairo"
> 512-bit FLIX/VLIW DSP** (`ncore2gp`, one per NeuronCore), on the **DVE** sequencer —
> co-resident in the same firmware blob as the
> [FindIndex8 / MatchReplace search cluster](../../isa/ref/b21-select-shuffle.md).
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md). Prose
> is derived from static analysis of the shipped binaries only.

---

## 1. TL;DR — the eight pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | RangeSelect is **opcode `0xbc`** (`// Y` = supported/maintained); it sits encoding-wise in the `0xb8` DMA/extended block but its *runtime* engine is **DVE**. | `aws_neuron_isa_tpb_common.h:259` (cayman) / `:265` (mariana) / `:271` (maverick) | HIGH/OBSERVED |
| 2 | The operand struct is the **64-byte `NEURON_ISA_TPB_S2D2_RS_STRUCT`** (`s2d2_rs.h`), the sole struct mapped 1:1 to `RANGE_SELECT`. | `instruction_mapping.json` + `ISA_STATIC_ASSERT(==64)` + `gcc offsetof` this session | HIGH/OBSERVED |
| 3 | The per-element op is a **two-sided range-predicate select**: `pred = (x cmp0 bound0) && (x cmp1 bound1)`; `out = pred ? x : fill_val`. Both comparators and both bounds are independently programmable. | `is_valid_range_select` @ `assert.h:16064` (real C) | HIGH/OBSERVED |
| 4 | `fill_val` is **hard-pinned to bit pattern `0xff7fffff == -FLT_MAX`** (most-negative *finite* FP32, **not** `-Inf`) — the order identity for the optional Max-reduce. | `is_valid_fill_val` @ `assert.h:16279` returns `(i == 0xff7fffff)` | HIGH/OBSERVED |
| 5 | An **optional Max-reduction** follows: `reduce_cmd ∈ {Idle, Accumulate, ZeroAccumulate}` and `reduce_op` **must be `Max`** (`0x08`). No Sum/Min; no plain `Zero`; no `LoadAccumulate`. | `has_valid_range_select_reduce_cmd` @ `16210`, `…_reduce_op` @ `16235` | HIGH/OBSERVED |
| 6 | Bounds are **FP32 scalars** (immediate or pointer), validated `Dtype::FP32` regardless of tensor dtype — "DVE can only do FP32 comparison". | `has_valid_immediates(…, FP32)` + `IMM_SRC` enum `common.h:1207` | HIGH/OBSERVED |
| 7 | The dtype matrix is **FP-only**: input ∈ {FP8e3/e4/e5, FP16, BF16, FP32}; output adds **FP32R**. No integer dtype on either side. | `is_valid_fp_dtype_datapath` @ `assert.h:4305` (real C) | HIGH/OBSERVED |
| 8 | Per-gen: **ABSENT** on SUNDA (NC-v2); **PRESENT, byte-identical** on CAYMAN (NC-v3) / MARIANA (NC-v4) / MAVERICK (NC-v5). Gate: `has_valid_nc_rs(nc) == (nc >= V3)`. | opcode lines + `has_valid_nc_rs` @ `16256` + struct `diff` | HIGH/OBSERVED; MAVERICK interior INFERRED |

---

## 2. Provenance / anchors

All facts derive from static analysis of the shipped customop-lib package
`aws-neuronx-gpsimd-customop-lib 0.21.2.0` — the public **ISA C headers** under
`custom_op/c10/include/neuron_<gen>_arch_isa/tpb/`, the host ucode library
**`libnrtucode_internal.so`**, and the embedded POOL `extisa` ELF images carved from
it and decoded with the **shipped Cadence Xtensa toolchain** (`XTENSA_CORE=ncore2gp`).
No vendor source was consulted.

| Artifact | Value |
|----------|-------|
| Container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Container sha256 | `b7c67e898a116454…`, `10,276,288 B` — the FW-26/27/28/41 anchor, re-verified this session |
| Disassembler / readelf | `…/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-{objdump,readelf}` (Binutils 2.34.20200201, Xtensa Tools 14.09), `XTENSA_CORE=ncore2gp` |
| Struct header | `neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_s2d2_rs.h` (cayman==mariana==maverick modulo the NC-v comment) |
| Validators | `…/tpb/aws_neuron_isa_tpb_assert.h` — `is_valid_range_select` @ 16064 |
| Struct→opcode map | `…/tpb/instruction_mapping.json` — `S2D2_RS_STRUCT → RANGE_SELECT` |
| Compile-verify | `gcc -I…cayman… offsetof` run this session → `sizeof == 64`, every offset of §4 |

> **NOTE — VMA/offset arithmetic for this container.** `readelf -SW
> libnrtucode_internal.so` this session: `.rodata` (VMA `0x46b0`) is **VMA ==
> file-offset**, so the embedded `extisa` images (which live in `.rodata`) carve by
> file offset directly. (Its writable `.data` carries a `0x3000` VMA↔file delta, but
> nothing on this page reads `.data`.) The `extisa` *inner* ELFs are themselves
> Xtensa images with their own section table — read with the **native** `ncore2gp`
> readelf, never with host tooling. `[HIGH/OBSERVED]`

---

## 3. Opcode and enum neighborhood `[HIGH/OBSERVED]`

```
NEURON_ISA_TPB_OPCODE_RANGE_SELECT = 0xbc,    // Y
  cayman common.h:259 / mariana:265 / maverick:271   (byte-identical line, all 3 gens)
  sunda  common.h     : NO RANGE_SELECT entry
```

The `// Y` tail is the **maintenance** marker — the enum legend at `common.h:155`
spells it `Tested/Maintained/Not deprecated?`. RangeSelect carries uppercase `// Y`
(actively maintained), in deliberate contrast to its deprecated `S2D2` cousin
`TENSOR_REDUCE_RANGE_CHECK = 0x9c` (`// n, workaround with other instructions, to be
removed`). The `// Y` flag is a **maintenance** tag, **not** an engine tag — engine
classification is settled separately in §7.

The `0xbc` encoding sits inside the `0xb8` DMA/extended opcode block:

| opcode | mnemonic | flag |
|-------:|----------|------|
| `0xb8` | `DMAMEMCPY` | `// Y` |
| `0xbb` | `DMA_INDIRECT` | `// Y` |
| **`0xbc`** | **`RANGE_SELECT`** | **`// Y`** ← this page |
| `0xbd` | `DMA_TRANSPOSE` | `// Y` |
| `0xbe` | `GET_SEQUENCE_BOUNDS` | `// Y` |
| `0xbf` | `SB2SB_COLLECTIVE` | `// y` |

> **GOTCHA — encoding adjacency is not engine adjacency.** `0xbc` is *encoded* next
> to the bulk DMA movers, but it is **not** a DMA op. The bulk movers `0xb8`/`0xbb`
> route through the **POOL** software-kernel table (§7), whereas the compute op `0xbc`
> is **DVE-native** and absent from that table. Conversely, `0xbc` is *not* in the
> FW-44 DVE search cluster by **opcode value** — that cluster is `0x6c MAX8 /
> 0x6d MATCH_VALUE_LOAD / 0x6e FIND_INDEX8 / 0x6f MATCH_REPLACE8` (cayman
> `common.h:200-203`) — yet it **is** in the same DVE engine and the same DVE
> firmware blob as that cluster (§7.2). Engine membership tracks the firmware blob,
> not the opcode neighborhood. `[HIGH/OBSERVED]`

### 3.1 The `S2D2_RS` instruction family

`instruction_mapping.json` (`struct2opcode`, cayman) maps the full S2D2 family. The
range/select members:

| struct | → opcode | flag | role |
|--------|----------|------|------|
| `S2D2_RS_STRUCT` | `RANGE_SELECT` (`0xbc`) | `// Y` | **this page** — range-predicate select |
| `S2D2_TR_STRUCT` | `TENSOR_REDUCE_RANGE_CHECK` (`0x9c`) | `// n` | deprecated range-check cousin |
| `S2D2_TS_AS_STRUCT` | `TENSOR_SCALAR_AFFINE_SELECT` (`0x92`) | `// Y` | affine-scalar select sibling |
| `S2D2_ADDR_STRUCT` | `TENSOR_SCALAR_ADDR` | — | (unrelated S2D2 data-movement) |
| `S2D2_JPEG_STRUCT` | `JPEG_DECODE` | — | (unrelated) |
| `S2S2D2_STT_STRUCT` | `…SELECT_REDUCE` (+3 more) | — | the scalar-tensor-tensor select-reduce |

RangeSelect is the **maintained** range member of this family. The opcode→struct
mapping is **1:1** (`S2D2_RS_STRUCT` has exactly one target opcode), so the operand
contract below is the complete and authoritative encoding.

---

## 4. Operand struct — `NEURON_ISA_TPB_S2D2_RS_STRUCT` (64 B) `[HIGH/OBSERVED]`

Reproduced byte-for-byte from `aws_neuron_isa_tpb_s2d2_rs.h`; every offset was
**compile-verified this session** (`gcc … offsetof`, `_Static_assert(sizeof==64)`
compiled clean):

| off | size | field | type | notes |
|----:|----:|-------|------|-------|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `header.opcode == 0xbc` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | event semaphore block |
| 12 | 1 | `num_active_channels` | `uint8_t` | partition count |
| 13 | 1 | `in_out_dtype` | `NEURON_ISA_TPB_DTYPE_PAIR` | `dtype_lo:4`=in, `dtype_hi:4`=out |
| 14 | 1 | `reduce_cmd` | `NEURON_ISA_TPB_ACCUM_CMD` | Idle / Accumulate / ZeroAccumulate |
| 15 | 1 | `reduce_op` | `NEURON_ISA_TPB_ALU_OP` | **must be `Max` (`0x08`)** |
| 16 | 4 | `base` | `float` | per-element index origin, `≤ 2^24` |
| 20 | 4 | `fill_val` | `float` | **must be `-FLT_MAX` (`0xff7fffff`)** |
| 24 | 12 | `src_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | input 2D pattern (read) |
| 36 | 1 | `comp_op0` | `NEURON_ISA_TPB_ALU_OP` | comparator: IsEQ/GT/GE/LE/LT |
| 37 | 1 | `comp_op1` | `NEURON_ISA_TPB_ALU_OP` | comparator: IsEQ/GT/GE/LE/LT |
| 38 | 1 | `bound0_src` | `NEURON_ISA_TPB_IMM_SRC` | imm / ptr / regptr |
| 39 | 1 | `bound1_src` | `NEURON_ISA_TPB_IMM_SRC` | imm / ptr / regptr |
| 40 | 4 | `bound0` | `NEURON_ISA_TPB_IMM_VAL_INST_FIELD` | FP32 `lo` bound (union) |
| 44 | 4 | `bound1` | `NEURON_ISA_TPB_IMM_VAL_INST_FIELD` | FP32 `hi` bound (union) |
| 48 | 12 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | output 2D pattern (write) |
| 60 | 4 | `reserved1[4]` | `uint8_t` | padding to 64 B |

```
$ gcc -I…neuron_cayman_arch_isa/tpb -o ver ver.c && ./ver
sizeof=64
header=0 events=4 num_active_channels=12 in_out_dtype=13
reduce_cmd=14 reduce_op=15 base=16 fill_val=20
src_mem_pattern=24 comp_op0=36 comp_op1=37 bound0_src=38 bound1_src=39
bound0=40 bound1=44 dst_mem_pattern=48 reserved1=60
sizeof DTYPE_PAIR=1 TENSOR2D=12 IMM_VAL=4 EVENTS=8 HEADER=4
0xff7fffff as float = -3.40282e+38  (== -FLT_MAX)
```

The constituent types (all from `common.h`):

* **`DTYPE_PAIR`** (`:767`) is a single byte of two 4-bit nibbles
  `{ dtype_lo:4; dtype_hi:4; }` — input dtype in the low nibble, output in the high.
* **`TENSOR2D`** (`:643`) is 12 bytes: `{ ADDR4 start_addr; int16 step_elem[2];
  uint16 num_elem[2]; }` — the strided 2D walk. `num_elem[0]*num_elem[1]` is the
  element count, equal for src and dst (§5).
* **`IMM_VAL_INST_FIELD`** (`:866`) is a **4-byte union**:
  `{ PARTITION_OFFSET imm_ptr; IMM_REG imm_reg; float imm_arith_fp32; int32
  imm_bitvec_int32; … }`. For RangeSelect the bound is read as `imm_arith_fp32` when
  `*_src == 0` (instruction-immediate FP32) and as `imm_ptr` otherwise (§6).

> **CORRECTION (vs an earlier survey draft) — `bound0` is at off 40, not 38.** Offsets
> 38/39 are the **1-byte** `bound0_src`/`bound1_src` selectors; the FP32 `bound0`/
> `bound1` values begin at 40/44. Confirmed by the `offsetof` run above.
> `[HIGH/OBSERVED]`

---

## 5. Exact semantics — predicate · select · fill · reduce

The source of truth is `is_valid_range_select` (`assert.h:16064`, a **real C
function**, mirrored by a `dbg_…` debug-bool variant at `16086`), the field set, the
`-FLT_MAX` fill pin, and the `s2d2_rs.h` header design notes. The DVE blob body is
hardware-native with no `kernel_info` funcVA anchor (§7), so the device opcode stream
is not separately disassembled; the semantics are read from the validated operand
contract, which is byte-exact.

### 5.1 The validator — the complete contract `[HIGH/OBSERVED]`

`is_valid_range_select` is a conjunction of **21 sub-checks**, read verbatim:

```c
// assert.h:16064  (real C, reproduced exactly)
STATIC inline bool is_valid_range_select(NEURON_ISA_TPB_INST_UNION i,
                                         NEURON_ISA_TPB_NEURON_CORE_VERSION nc) {
  return(   has_valid_neuron_header(i)
         && has_valid_neuron_events(i)
         && has_valid_nc_rs(nc)                                    // nc >= V3
         && has_range_select_opcode(i)                            // opcode == 0xbc
         && start_addr_active_channels(i.s2d2_rs.src_mem_pattern.start_addr, i.s2d2_rs.num_active_channels)
         && start_addr_active_channels(i.s2d2_rs.dst_mem_pattern.start_addr, i.s2d2_rs.num_active_channels)
         && tensor2d_valid(i.s2d2_rs.src_mem_pattern, get_dtype_from_pair(i.s2d2_rs.in_out_dtype.dtype_lo),
                           WRITE_TENSOR_FALSE, ALLOWED_IN_PSUM_TRUE, ALLOWED_IN_SBUF_TRUE)   // src: read
         && tensor2d_valid(i.s2d2_rs.dst_mem_pattern, get_dtype_from_pair(i.s2d2_rs.in_out_dtype.dtype_hi),
                           WRITE_TENSOR_TRUE,  ALLOWED_IN_PSUM_TRUE, ALLOWED_IN_SBUF_TRUE)   // dst: write
         && has_same_elem_count_rs_src_dst(i.s2d2_rs.src_mem_pattern, i.s2d2_rs.dst_mem_pattern)
         && has_valid_total_index_range(i.s2d2_rs.src_mem_pattern, i.s2d2_rs.base)            // base+count <= 2^24
         && is_valid_fp_dtype_datapath(get_dtype_from_pair(i.s2d2_rs.in_out_dtype.dtype_lo), ALLOW_FP32R_FALSE)
         && is_valid_fp_dtype_datapath(get_dtype_from_pair(i.s2d2_rs.in_out_dtype.dtype_hi), ALLOW_FP32R_TRUE)
         && is_valid_fill_val(i.s2d2_rs.fill_val)                                             // == 0xff7fffff
         && is_valid_rs_comp_op(i.s2d2_rs.comp_op0)                                           // IsEQ/GT/GE/LE/LT
         && is_valid_rs_comp_op(i.s2d2_rs.comp_op1)
         && has_valid_immediates(i.s2d2_rs.bound0, i.s2d2_rs.bound0_src, i.s2d2_rs.num_active_channels, FP32)
         && has_valid_immediates(i.s2d2_rs.bound1, i.s2d2_rs.bound1_src, i.s2d2_rs.num_active_channels, FP32)
         && check_active_channels(i.s2d2_rs.num_active_channels)
         && has_valid_range_select_reduce_cmd(i.s2d2_rs.reduce_cmd)                           // Idle/Accum/ZeroAccum
         && has_valid_range_select_reduce_op(i.s2d2_rs.reduce_op));                           // == Max
}
```

### 5.2 The per-element datapath `[HIGH/OBSERVED for structure]`

```c
// Recovered semantics — names are the real validated struct fields.
// x = one source element, upcast to FP32 for the compare (DVE compares in FP32).
//
//   p0   = (x  comp_op0  bound0)    comp_op ∈ {IsEQ,IsGT,IsGE,IsLE,IsLT}  (independently chosen)
//   p1   = (x  comp_op1  bound1)
//   pred = p0 && p1                 // two-sided range test
//   out  = pred ? x : fill_val      // fill_val == -FLT_MAX (0xff7fffff)
// then OPTIONALLY, over the dst pattern:
//   if (reduce_cmd != Idle)  acc = max(acc, out)   // reduce_op == Max ONLY
```

The conventional two-sided range `lo ≤ x ≤ hi` is `comp_op0 = IsGE` (`bound0 = lo`)
AND `comp_op1 = IsLE` (`bound1 = hi`). But **both** comparators and **both** bounds
are fully programmable, so `0xbc` also expresses `x < hi`, `x > lo`, `x == v`
(degenerate, both compares on the same value), open half-rays, and inverted bands.
The `IsXX` comparators are the FP comparator opcodes — exact enum values:

| comp_op | enum | hex |
|---------|------|----:|
| `IsEQ` | `NEURON_ISA_TPB_ALU_OP_IS_EQ` | `0x12` |
| `IsGT` | `…_IS_GT` | `0x13` |
| `IsGE` | `…_IS_GE` | `0x14` |
| `IsLE` | `…_IS_LE` | `0x15` |
| `IsLT` | `…_IS_LT` | `0x16` |

`is_valid_rs_comp_op` (`assert.h:16131`) admits **exactly** these five and nothing
else — no `Min`/`Max`/`Add`. That is the structural proof RangeSelect is a **range
predicate**, not a clamp:

> **NOTE — why it is a range test, not a `min/max` clamp.** A two-sided clamp
> (`clamp(x, lo, hi)`) would need `Max(lo, …)` and `Min(…, hi)` *arithmetic* ALU ops.
> RangeSelect instead gets two **comparators** feeding a boolean predicate, then a
> *select*. The output of an in-range element is the element itself (`x`), not `lo` or
> `hi`. So this op gates/masks values; it does not saturate them.
> `[HIGH/OBSERVED — comparator-only `comp_op` set]`

### 5.3 Why it writes VALUES with a sentinel — a select, not a mask `[HIGH/OBSERVED]`

`dst_mem_pattern` is a full `TENSOR2D` with the **same element count** as `src`
(`has_same_elem_count_rs_src_dst`, `assert.h:16160`) and the **output** dtype
`dtype_hi`. It stores **one output element per input element** — not a packed bitmask.
Out-of-range elements are written as `fill_val`. So `0xbc` is a **select / replace**,
not a predicate-bitmask generator (contrast the bitmask-emitting compare ops).

The choice of `fill_val` is the tell. `is_valid_fill_val` pins it to one exact bit
pattern:

```c
// assert.h:16279  (real C)
STATIC inline bool is_valid_fill_val(float fill_val) {
    uint32_t i = *(uint32_t*)(&fill_val);
    return (i == 0xff7fffff);                 // -FLT_MAX, the most-negative FINITE FP32
}
```

> **QUIRK — the fill is `-FLT_MAX`, not `-Inf`.** `0xff7fffff` is the most-negative
> *finite* FP32 (`-3.40282e38`), one ULP above `-Inf` (`0xff800000`). Verified this
> session by reinterpreting the bits as a float. Using the finite max-neg (rather than
> `-Inf`) makes the **optional Max-reduce well-defined and overflow-clean**: rejected
> lanes contribute `-FLT_MAX` and can never win the max, but the accumulator stays in
> the finite range. The header says outright the value is *hard-coded* "due to DVE
> restrictions (out of immediate to send in programmable `fill_val`)". `[HIGH/OBSERVED]`

### 5.4 The optional Max-reduction `[HIGH/OBSERVED]`

The reduction is controlled by two fields. `reduce_cmd` (`ACCUM_CMD`):

```c
// assert.h:16210
STATIC inline bool has_valid_range_select_reduce_cmd(NEURON_ISA_TPB_ACCUM_CMD reduce_cmd) {
    return(   (reduce_cmd == NEURON_ISA_TPB_ACCUM_CMD_IDLE)            // 0  -> no reduction
           || (reduce_cmd == NEURON_ISA_TPB_ACCUM_CMD_ACCUMULATE)     // 2  -> max into existing acc
           || (reduce_cmd == NEURON_ISA_TPB_ACCUM_CMD_ZERO_ACCUMULATE)); // 3 -> clear, then max
}
```

and `reduce_op` (`ALU_OP`), pinned to a single value:

```c
// assert.h:16235
STATIC inline bool has_valid_range_select_reduce_op(NEURON_ISA_TPB_ALU_OP reduce_op) {
    return(reduce_op == NEURON_ISA_TPB_ALU_OP_MAX);                   // 0x08, MAX only
}
```

> **GOTCHA — `reduce_cmd` is the 5-value `ACCUM_CMD` enum, but only 3 are legal here.**
> `ACCUM_CMD` (`common.h:777`) is `{ IDLE=0, ZERO=1, ACCUMULATE=2, ZERO_ACCUMULATE=3,
> LOAD_ACCUMULATE=4 }`. RangeSelect rejects plain `ZERO` (1) and `LOAD_ACCUMULATE`
> (4). The header explains the missing `LoadAccumulate`: *"DVE cannot support
> LoadAccumulate because we ran out of imm_ptr (up to 2 per instruction)"* — both
> immediate-pointer slots are consumed by `bound0`/`bound1`, leaving none for a load
> source. `reduce_op` is pinned to `Max` to match the `-FLT_MAX` fill identity: no
> `Sum`, no `Min`. So a common use is **masked-max / argmax-style reduction**: keep
> in-range values, send rejects to `-FLT_MAX`, Max-reduce. `[HIGH/OBSERVED for the
> field legality; MED/INFERRED for the masked-max intent.]`

### 5.5 The `base` index facet `[HIGH that base is a 2^24-bounded origin / OBSERVED]`

`base` (off 16, declared `float` in the struct) is a per-element **ordinal/position
origin**. The validator bounds it to the FP32 integer ceiling:

```c
// assert.h:16185
STATIC inline bool has_valid_total_index_range(NEURON_ISA_TPB_TENSOR2D src_mem_pattern, uint32_t base) {
    return(   (base <= 16777216)                                  // 2^24 = largest exact FP32 integer
           && (   shape_from_register(src_mem_pattern.start_addr)
               || (base + t2d_element_count(src_mem_pattern) <= 16777216)));
}
```

> **QUIRK — `base` is `float` in the struct but `uint32_t` in the validator.** The
> field is declared `float base;` (off 16), yet `has_valid_total_index_range` takes it
> as `uint32_t` and tests `base <= 2^24` and `base + element_count <= 2^24`. The
> `2^24` ceiling is exactly the largest integer FP32 represents without loss, so the
> field is a running **integer ordinal carried in FP32** — `base + ordinal` stays
> exactly representable for the whole walk. This is RangeSelect's index facet and its
> structural link to the FW-44 index family: its **immediate** firmware-blob neighbor
> is `S: DveReadIndices` (§7.2), the per-lane index readout. `[HIGH that `base` is a
> `2^24`-bounded index origin / OBSERVED.]`

> **CORRECTION (residual ambiguity, flagged) — value-on-pass vs index-on-pass.**
> Whether the element *written to `dst`* on a pass is the VALUE `x` or the INDEX
> (`base + ordinal`) cannot be settled from the operand contract alone — both the
> "value-select with `-FLT_MAX` fill" reading and an "index-on-pass" reading are
> consistent with the fields. The `-FLT_MAX` fill plus `reduce_op == Max` strongly
> favor a **Max-reduction whose winner's index is then read via `DveReadIndices`**
> (the immediate neighbor tag), i.e. a fused *range-mask → max → argmax*. The
> value-vs-index of the stored element is the one residual ambiguity on this page.
> `[MED/INFERRED]`

---

## 6. Bounds source `[HIGH/OBSERVED]`

Two FP32 scalar bounds, each independently sourced by its 1-byte `*_src` selector
(`NEURON_ISA_TPB_IMM_SRC`, `common.h:1207`):

| `*_src` | enum | meaning |
|--------:|------|---------|
| `0` | `INSTRUCTION_IMMEDIATE` | FP32 value embedded in the instruction (`imm_arith_fp32`) |
| `1` | `POINTER_IMMEDIATE` | `PartitionOffset` pointer to the immediate(s) (`imm_ptr`) |
| `2` | `REG_PTR_IMMEDIATE` | `PartitionOffset` pointer taken from a register (`imm_reg`) |

`has_valid_immediates(bound, src, num_active_channels, FP32)` (`assert.h:413`, real C)
validates each bound: an instruction-immediate always passes; a pointer-immediate must
be channel-aligned (`tpb_addr_active_channels`) and dtype-aligned (`addr_aligned_dtype
(…, FP32)`); a reg-ptr must be a valid `imm_reg`. The `is_valid_range_select` call
sites pass the literal `NEURON_ISA_TPB_DTYPE_FP32`:

```c
&& has_valid_immediates(i.s2d2_rs.bound0, i.s2d2_rs.bound0_src, i.s2d2_rs.num_active_channels, NEURON_ISA_TPB_DTYPE_FP32)
&& has_valid_immediates(i.s2d2_rs.bound1, i.s2d2_rs.bound1_src, i.s2d2_rs.num_active_channels, NEURON_ISA_TPB_DTYPE_FP32)
```

> **NOTE — bounds are FP32 SCALARS, never a tensor.** `lo`/`hi` are two FP32 scalars
> (immediate or pointer), **not** a 2-element tensor and **not** tensor operands like a
> scalar-bounds-fetch sequence would use. The header is explicit: *"DVE can only do
> FP32 comparison"* — the bounds are intrinsically FP32 regardless of the in/out tensor
> dtype, and narrower-FP inputs are upcast to FP32 for the compare (the "FP32 hub").
> `[HIGH/OBSERVED]`

---

## 7. Dispatch-surface classification `[HIGH/OBSERVED]`

The discriminator is two-pronged: **(i)** presence in the `Q7_POOL`
`kernel_info_table` = a POOL **software** kernel routed by `funcVA`; absence =
hardware/engine-native; and for the native ones, **(ii)** the **blob multiplicity** of
the `"S: <Name>"` device self-name tag in the host ucode selects the engine family
(`4` copies = DVE). Both were re-derived this session.

### 7.1 POOL `kernel_info_table` — `0xbc` ABSENT (CAYMAN, carved & decoded this session)

The CAYMAN POOL `extisa` is embedded in `libnrtucode_internal.so` as the `.rodata`
object `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get.data` (VMA `0x2ef7e0`, `41,568 B`).
Carved by file offset (`.rodata` VMA == file-offset) and parsed with the native
`ncore2gp` readelf this session, its section `[7] kernel_info_table` is at file
`0x7400`, size `0x88` = **17 entries** of 8 bytes (`opcode` = byte 3 of word0,
`funcVA` = word1):

```
opcodes: 7e 7c 7d 45 51 41 f0 f0 f0 f0 f0 52 46 47 be f2 7b
0xbc present? -> False
  …  op=0xbe funcVA=0x01004204   (GET_SEQUENCE_BOUNDS — IS a POOL kernel)
  …  (no 0xbc entry anywhere in the table)
```

So **RangeSelect (`0xbc`) is absent from the CAYMAN POOL table**, while its
enum-neighbor `0xbe` (GET_SEQUENCE_BOUNDS) **is** a POOL kernel at funcVA
`0x01004204`. The bulk DMA movers `0xb8`/`0xbb` also route through POOL — but the
compute op `0xbc` does not. (The five `0xf0` entries are the extended-instruction
slots.) `[HIGH/OBSERVED — carved binary + native readelf, reproduced this session.]`

The same absence holds for SUNDA, with a twist on which image ships:

> **NOTE — SUNDA ships a `RELEASE` POOL extisa, not a `PERF` one.** In this container
> the SUNDA POOL `extisa` symbol is `SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get` (an UND
> weak reference — the release-variant image, not embedded as a `.data` blob like
> CAYMAN's `PERF` variant). `0xbc` is absent from the SUNDA POOL table for the deeper
> reason that the opcode itself is not minted in NC-v2 (§10.2). `[HIGH/OBSERVED]`

### 7.2 Host-ucode self-name multiplicity = 4 (DVE), co-resident with the search cluster

A byte scan of `libnrtucode_internal.so` for the device self-name tag this session:

```
S: RangeSelect        4   @ 0x18e1d3  0x428513  0x6f0233  0x8b0613
S: FindIndex8         4   @ 0x18dbf7  …
S: MatchValueLoad     4   @ 0x18d6a0  …
S: MatchReplace       4   @ 0x18db50  …
```

The **4-copy** multiplicity is the DVE family signature (one per DVE blob instance),
and the four RangeSelect copies are ~2.7 MB apart — the four DVE blobs — each within a
few KB of the FindIndex8/MatchValueLoad/MatchReplace tags of the *same* blob. Walking
the tag stream around the first copy (`0x18e000…0x18e800`) gives the DVE
select/predicate/index neighborhood, in address order:

```
… CopyPredicatedReduce  @0x18e1aa
   DveReadIndices        @0x18e1c0   <- IMMEDIATE neighbor (the index readout, §5.5)
   RangeSelect           @0x18e1d3   <- THIS
   DGE …
```

and the wider blob window carries `MatchValueLoad, MatchReplace, FindIndex8,
TensorScalarSelect, CopyPredicatedScalar, CopyPredicated, CastPredicated,
CopyPredicatedReduce, DveReadAccumulator, DveReadIndices, RangeSelect` — each at the
DVE multiplicity. `RangeSelect`'s immediate predecessor `DveReadIndices` matches its
`base` index facet; `DveReadAccumulator` backs the optional Max-reduce accumulator.
`[HIGH/OBSERVED — multiplicity 4 == DVE; tag adjacency re-read this session.]`

**Verdict.** RangeSelect (`0xbc`) is a **DVE-engine, hardware-native** instruction. It
is neither a `Q7_POOL` software kernel (absent from the POOL table) nor a
sequencer-native control op. It runs on the same DVE engine and in the same DVE
firmware blob as the FW-44 `FindIndex8`/`MatchReplace8` search/select cluster — see
the [predicate/select ISA batch](../../isa/ref/b21-select-shuffle.md). The exact
device handler VA inside the blob is not separately pinned (the DVE body is
hardware-native, no `kernel_info` funcVA to anchor a bounded objdump window) —
`[MED/unanchored]`.

---

## 8. Dtype matrix `[HIGH/OBSERVED]`

Input (`dtype_lo`) and output (`dtype_hi`) are each validated through
`is_valid_fp_dtype_datapath` — input with `ALLOW_FP32R_FALSE`, output with
`ALLOW_FP32R_TRUE`:

```c
// assert.h:4305  (real C)
STATIC inline bool is_valid_fp_dtype_datapath(NEURON_ISA_TPB_DTYPE dtype,
                                              NEURON_ISA_TPB_DTYPE_ALLOW_FP32R allow_fp32r) {
    return(   (dtype == NEURON_ISA_TPB_DTYPE_FP8_EXP3)     // 0xD
           || (dtype == NEURON_ISA_TPB_DTYPE_FP8_EXP4)     // 0xE
           || (dtype == NEURON_ISA_TPB_DTYPE_FP8_EXP5)     // 0xF
           || (dtype == NEURON_ISA_TPB_DTYPE_FP16)         // 0x7
           || (dtype == NEURON_ISA_TPB_DTYPE_BFLOAT16)     // 0x6
           || (dtype == NEURON_ISA_TPB_DTYPE_FP32)         // 0xA
           || (   (allow_fp32r == ALLOW_FP32R_TRUE)
               && (dtype == NEURON_ISA_TPB_DTYPE_FP32R)));  // 0xB, output only
}
```

| facet | nibble | accepted dtypes (enum hex) |
|-------|--------|----------------------------|
| **input** `dtype_lo` | `in_out_dtype` bits[3:0] | FP8e3 `0xD`, FP8e4 `0xE`, FP8e5 `0xF`, FP16 `0x7`, BF16 `0x6`, FP32 `0xA` — **no FP32R**, **no integer** |
| **output** `dtype_hi` | `in_out_dtype` bits[7:4] | FP8e3, FP8e4, FP8e5, FP16, BF16, FP32, **FP32R `0xB`** |

> **NOTE — FP-only, FP32-comparison hub.** No integer dtype is legal on **either**
> side. Narrower-FP inputs are upcast to FP32 for the compare, the bounds are FP32
> (§6), and the only asymmetry is that the **output** may be the rounded `FP32R`
> (`0xB`) variant whereas the input may not. The dtype rules are byte-identical
> cayman==maverick (`diff` SAME). `[HIGH/OBSERVED]`

---

## 9. Algorithm `[HIGH for the validated contract; MED for inner-loop ordering / INFERRED]`

```c
// RangeSelect — annotated reconstruction. Names are the real struct fields.
// Validated contract is byte-exact (§5); the inner-loop ordering and the
// value-vs-index of the PASS output (§5.5) are the inferred parts.
void range_select(const NEURON_ISA_TPB_S2D2_RS_STRUCT *i) {
    for (uint8_t c = 0; c < i->num_active_channels; c++) {           // partitions
        float ordinal = i->base;                                     // FP32 index, base <= 2^24
        if (i->reduce_cmd == ZERO_ACCUMULATE) acc[c] = -FLT_MAX;     // clear before max
        // TENSOR2D walk over src_mem_pattern[c] (num_elem[0..1] x step_elem[0..1]):
        for_each_elem (x, i->src_mem_pattern, c, /*dtype=*/dtype_lo) {
            float xf = to_fp32(x);                                   // DVE compares in FP32
            bool p0 = alu_cmp(i->comp_op0, xf, fp32(i->bound0));     // IsEQ/GT/GE/LE/LT
            bool p1 = alu_cmp(i->comp_op1, xf, fp32(i->bound1));
            float y = (p0 && p1) ? /* x  OR  ordinal — §5.5 */ xf
                                 : i->fill_val;                      // == -FLT_MAX
            store_elem (y, i->dst_mem_pattern, c, /*dtype=*/dtype_hi);
            if (i->reduce_cmd != IDLE)                               // optional reduction
                acc[c] = fmaxf(acc[c], y);                           // reduce_op == Max only
            ordinal += 1.0f;                                         // bounded base+count <= 2^24
        }
    }
}
```

`src_mem_pattern`/`dst_mem_pattern` carry `num_elem[0..1]` + `step_elem[0..1]` (the
12-byte `TENSOR2D`), giving the multi-tile / strided walk; src and dst must have equal
element counts (`has_same_elem_count_rs_src_dst`) — unless either start address is
register-supplied (`shape_from_register`), in which case the count check is waived.

---

## 10. Per-gen presence and dispatch chain

### 10.1 Presence (byte-check) `[HIGH/OBSERVED; MAVERICK interior INFERRED]`

| gen | NC-v | RangeSelect | evidence |
|-----|------|-------------|----------|
| **SUNDA** | NC-v2 | **ABSENT** | no `RANGE_SELECT` enum; no `s2d2_rs.h`; not in mapping; opcode block skips `0xbc` (§10.2) |
| **CAYMAN** | NC-v3 | **PRESENT** | `0xbc // Y` (`common.h:259`); `S2D2_RS_STRUCT` (64 B); gate `nc >= V3` |
| **MARIANA** | NC-v4 | **PRESENT** | `0xbc // Y` (`:265`); struct byte-identical to cayman (`diff` SAME) |
| **MAVERICK** | NC-v5 | **PRESENT** | `0xbc // Y` (`:271`); struct byte-identical to cayman (`diff` SAME) |

First introduced at CAYMAN; maintained (`// Y`) through MAVERICK. The struct/validator
headers are byte-identical across the three present gens (verified by `diff` ignoring
the `ISA header for NC-vN` comment). MAVERICK is NC-v5: header-OBSERVED only — its
device-blob interior is INFERRED to match cayman/mariana from the identical header.

> **CORRECTION — the SUNDA `*_ctrl_rs.h` is RegShuffle, NOT RangeSelect.** SUNDA ships
> `aws_neuron_isa_tpb_ctrl_rs.h`, whose header comment is `NEURON ISA / TPB /
> REG_SHUFFLE` ("RegShuffle re-shuffles the elements in the embedded pooling
> register-vector"). The `"rs"` there abbreviates **Reg**ister **S**huffle, an
> unrelated control op — do **not** conflate the two `"rs"` headers. SUNDA has **no**
> `s2d2_rs.h`. `[HIGH/OBSERVED]`

### 10.2 SUNDA absence — proven five ways `[HIGH/OBSERVED]`

1. **Zero header hits** — `rg 'RANGE_SELECT\|S2D2_RS'` over the entire
   `neuron_sunda_arch_isa/` tree returns **0 matches**.
2. **No struct header** — `s2d2_rs.h` exists for cayman/mariana/maverick; it does
   **not** exist under `neuron_sunda_arch_isa/tpb/`.
3. **`ctrl_rs.h` is RegShuffle** — the only `"*_rs.h"` SUNDA ships is `ctrl_rs.h` =
   `REG_SHUFFLE` (§10.1 correction), unrelated.
4. **Mapping omits it** — SUNDA `instruction_mapping.json` has **no** `S2D2_RS_STRUCT`
   key (cayman has `S2D2_RS_STRUCT → RANGE_SELECT`); SUNDA only retains the unrelated
   `PSEUDO_RANGE_CHECK_STRUCT`.
5. **Opcode block skips `0xbc`** — SUNDA's opcode enum jumps from `0xbb DMA_INDIRECT`
   straight to `0xc1 PSEUDO_DMATRIGGER`; the whole `0xbc..0xc0` range (RangeSelect,
   DMA_TRANSPOSE, GET_SEQUENCE_BOUNDS, SB2SB_COLLECTIVE) is **not minted** in NC-v2.

The structural gate behind all five is the single validator line:

```c
// assert.h:16256
STATIC inline bool has_valid_nc_rs(NEURON_ISA_TPB_NEURON_CORE_VERSION nc) {
    return((nc >= NEURON_ISA_TPB_NEURON_CORE_VERSION_V3));    // V2(sunda) fails; V3+(cayman+) passes
}
```

### 10.3 Dispatch chain (per gen) `[HIGH except funcVA]`

Identical for CAYMAN / MARIANA / MAVERICK (byte-identical struct + op line):

1. **(encode)** instruction = `NEURON_ISA_TPB_S2D2_RS_STRUCT`, 64 B,
   `header.opcode = 0xbc`; fields per §4.
2. **(validate)** `is_valid_range_select` / `dbg_is_valid_range_select` ANDs the 21
   sub-checks of §5.1 (incl. `nc >= V3`, `-FLT_MAX` fill, comparator legality, FP32
   bounds, equal src/dst counts, `base ≤ 2^24`, `reduce_op == Max`).
3. **(route)** **no** `Q7_POOL` `kernel_info_table` entry for `0xbc` (§7.1) ⇒ **no**
   `callx8` / no funcVA POOL hop. The DVE front-end decodes `0xbc` directly into the
   DVE blob handler whose self-name tag is `"S: RangeSelect"` (§7.2). The exact device
   handler VA is not separately pinned — `[MED/unanchored]`.

**SUNDA:** opcode `0xbc` does not exist (the `nc >= V3` gate); no dispatch.

> **FLIX-desync note.** No native Vision-Q7 FLIX disassembly of the DVE blob body was
> performed (the handler is hardware-native with no `kernel_info` funcVA to anchor a
> bounded objdump window), so the `ncore2gp` FLIX-vs-LX desync artifact does not affect
> any claim here. The POOL `extisa` carve of §7.1 used the native readelf for **section
> headers + raw bytes only** (no instruction decode) — desync-immune. `[N/A]`

---

## 11. Cross-references

* [ISA Batch 21 — Select / Shuffle / Compress](../../isa/ref/b21-select-shuffle.md) —
  the per-lane crossbar (`SEL`/`SHFL`/`DSEL`/`DCMPRS`) and the FW-44 DVE
  search/predicate cluster RangeSelect co-resides with; the predicate-select framing
  reference.
* [CastPredicated](castpredicated.md) — the dtype-converting predicated copy; a DVE
  select/predicate sibling tag in the same blob.
* [CopyPredicatedScalar](copypredicatedscalar.md) — the scalar-fill predicated copy;
  the closest "predicate → select with fill" relative.
* [TensorScalarSelect → ts-select](ts-select.md) — the tensor-scalar select-family
  kernel; the affine-select sibling (`S2D2_TS_AS_STRUCT`,
  `TENSOR_SCALAR_AFFINE_SELECT 0x92`).
* [Dropout](dropout.md) / [Rand2](rand2.md) — sibling DVE consumer kernels, for the
  carve methodology and the DVE-engine dispatch model.
* [Confidence & Walls model](../../reference/confidence-model.md) — the tag semantics
  used throughout.

> **DIVERGENCE LEDGER.**
> 1. **`bound0` offset** — the FP32 bound is at off **40** (not 38); 38/39 are the
>    1-byte `*_src` selectors (§4). `[HIGH/OBSERVED — `offsetof`]`
> 2. **fill = `-FLT_MAX` not `-Inf`** — `0xff7fffff`, the most-negative *finite* FP32
>    (§5.3). `[HIGH/OBSERVED]`
> 3. **value-on-pass vs index-on-pass** — the one residual ambiguity; both readings
>    fit the operand contract (§5.5). `[MED/INFERRED]`
> 4. **SUNDA `"rs"` header is RegShuffle** — `ctrl_rs.h = REG_SHUFFLE`, unrelated to
>    `s2d2_rs.h` (which SUNDA lacks) (§10.1). `[HIGH/OBSERVED]`
