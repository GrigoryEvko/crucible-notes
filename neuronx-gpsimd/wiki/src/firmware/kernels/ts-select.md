# TensorScalarSelect — the immediate-predicate tensor/scalar BLEND (opcode `0x98`)

> **Scope.** TensorScalarSelect is **opcode `0x98`** (`// Y` = maintained), the **conditional-SELECT**
> member of the GPSIMD `TensorScalar*` family — and the one member of that family that is *not* an ALU
> op. It is **not a comparison-select.** For each DVE lane it evaluates a **pre-computed immediate
> predicate** (no on-chip compare, no comparator field) and blends between **one tensor operand**
> (`src0`) and **one scalar immediate** (`src1`):
>
> ```text
> out[i] = pred[i] ? src0[i] : scalar          // reversed_pred == 0
> out[i] = pred[i] ? scalar  : src0[i]          // reversed_pred == 1
> ```
>
> The predicate is supplied as an instruction-immediate (one global `0`/`1`) **or** as a per-lane
> *pointer-immediate* (a distinct value per DVE partition). It binds a **distinct 64-byte operand
> struct** — `NEURON_ISA_TPB_S3D3_TS_SELECT_STRUCT` (compile-verified `sizeof == 64`) —
> that is a **separate layout** from the `S3D3_TS` the [cache ops](ts-cache-cumulative.md) use: no
> `AluOp`, no accumulator, a *single* copy-only dtype. This page decodes the opcode and its
> SortMerge-slot annotation, the struct field-for-field, the verbatim blend pseudocode and its three
> validity functions, the **copy-only MOVE datapath** (vbool-gated predicated select/move, not
> compare+arith), the dtype matrix, and the DVE dispatch chain instruction-exact. It runs on the
> **Cadence Tensilica Vision-Q7 NX "Cairo" 512-bit FLIX/VLIW DSP** (`ncore2gp`, one per NeuronCore),
> on the **DVE** sequencer.
>
> The op is the firmware-fused form of the legacy two-instruction `Copy` + `CopyPredicate` select, for
> the special case where one select operand and the predicate are both immediates.
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md). All prose is derived
> from static analysis of the shipped binaries and the shipped public ISA headers only.

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | TensorScalarSelect is **opcode `0x98`** (`// Y` maintained), carrying the inline comment `// SortMerge wip 0x97`. **Byte-identical value and comment on all four gens** (sunda/cayman/mariana/maverick). | `aws_neuron_isa_tpb_common.h` sunda `:234` / cayman `:231` / mariana `:236` / maverick `:239` | HIGH/OBSERVED |
| 2 | It binds the **distinct 64-byte struct** `NEURON_ISA_TPB_S3D3_TS_SELECT_STRUCT`, mapped **1:1** to the opcode — **not** the `S3D3_TS` the cache/arith ops share. | `instruction_mapping.json` `struct2opcode` + `ISA_STATIC_ASSERT(==64)` + `gcc offsetof` all four gens | HIGH/OBSERVED |
| 3 | The op is a **predicated BLEND, not a compare-select**: there is **no comparator, no `comp_op`, no bound**. The condition is `pred_imm`, an **immediate** `0`/`1`, sourced inst-imm or **per-lane pointer-imm**. | header doc-block §0 + `has_valid_pred_inst_imm_value` (`imm_bitvec_int32 ∈ {0,1}`) | HIGH/OBSERVED |
| 4 | The two select operands are **a tensor** (`src0_mem_pattern` @16, `TENSOR3D`) and **a scalar immediate** (`src1_imm` @40, broadcast to dst). `reversed_pred` flips which arm is pred-true. | verbatim header pseudocode (`dst = reversed_pred ? (pred?src1:src0) : (pred?src0:src1)`) | HIGH/OBSERVED |
| 5 | **One dtype** governs `src0`/`src1`/`dst` (`@34`) — *copy-only, no casting (DVE data converters off)*. `pred_imm_dtype` (`@35`) is forced to an **INT datapath** dtype of the **same byte width** as the data dtype. | header constraints + `is_valid_int_dtype_datapath` + `has_same_imm_dtype_width` + `is_valid_dtype(…, AllowFP32R::False)` | HIGH/OBSERVED |
| 6 | The datapath is the **MOVE path** (copy-only register/immediate move), realized by **vbool-gated predicated select/move IVP ops** — the predicate vbool is **loaded** from `pred_imm`, **not generated** by a compare. | DVE DRAM `"S: MOVE"` / `move.cpp` / `move_shape` strings; PERF `ivp_dselnx16t`/`ivp_mov2nx8t`/`ivp_seln_2x32t` (vbool-gated) | HIGH/OBSERVED datapath vocab; specific-bundle bind MED |
| 7 | Dispatched to the **DVE** sequencer, worker self-name **`"S: TensorScalarSelect"`** (4 library copies = DVE multiplicity; absent from every POOL kernel table; `DVE_NUM_CHANNELS` in the validator). | 4× self-name @ `0x18db61`/`0x427e41`/`0x6efb61`/`0x8aff11`; worker funcVAs CAY `0xc540` / MAR `0xcb24` / MAV `0xcaec` | HIGH/OBSERVED |
| 8 | **Defined and byte-identical on all four gens — including SUNDA** (NC-v2), unlike [RangeSelect](rangeselect.md) (`0xbc`, absent on SUNDA). MAVERICK swaps to the tile-aware channel-range check; otherwise no struct change. | sunda `:234` ships opcode + `s3d3_ts_select.h`; cayman has `RANGE_SELECT` but sunda does not | HIGH/OBSERVED; MAVERICK interior INFERRED |

> **CORRECTION (the SortMerge slot).** The investigation framing — "`0x98` was reassigned from the
> (phantom) SortMerge slot" — is **imprecise** and is corrected here against the enum bytes. In the
> opcode enum, `0x98` is allocated to `TENSOR_SCALAR_SELECT` directly; the **SortMerge** name appears
> *only* in the trailing comment `// SortMerge wip 0x97`, which reserves the **adjacent `0x97`** slot
> (which is **unallocated** in the enum) for a *work-in-progress* SortMerge op. A *separate*,
> *maintained* `NEURON_ISA_TPB_OPCODE_SORT = 0x96 // Y` already exists (struct `S1D2_SORT`). So `0x98`
> was **never** a SortMerge slot; SortMerge is a future reservation at `0x97`, and `0x98` is the live
> Select. `[HIGH/OBSERVED — common.h:235/236 mariana, the `0x97` line is absent from the enum]`

---

## 2. Provenance / carve anchors

All device-firmware facts derive from static analysis of the shipped device-firmware blob, disassembled
with the Cadence Xtensa toolchain that ships *inside* the gpsimd-tools package (`XTENSA_CORE=ncore2gp`,
Vision-Q7 FLIX/VLIW), plus the shipped host customop-lib **ISA C headers**. No source was consulted.

| Artifact | Value |
|----------|-------|
| Container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Container sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` (10,276,288 B) |
| Disassembler | `gpsimd_tools/…/bin/xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp` |
| ISA header | `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_ts_select.h` (sunda/cayman/mariana/maverick) |
| Opcode enum | `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h` |
| Struct→opcode | `…/neuron_<gen>_arch_isa/tpb/instruction_mapping.json` (`struct2opcode`) |
| `"S: TensorScalarSelect"` (4×) | file offsets `0x18db61` / `0x427e41` / `0x6efb61` / `0x8aff11` (CAY / MAR / MAR_PLUS / MAV DVE DEBUG blobs) |
| MARIANA Select worker funcVA | `0xcb24` (in the MARIANA `NX_DVE` DEBUG IRAM blob) |
| MARIANA DVE DEBUG DRAM self-name | DRAM-local `0x2921` (CAY `0x2841` / MAV `0x2951`) |

The `S3D3_TS_SELECT` struct is **byte-identical** (`sizeof 64`, same field offsets) on
cayman/mariana/maverick/sunda — compile-verified (§3).

---

## 3. The operand struct — `S3D3_TS_SELECT` (64 B), compile-verified four gens

The struct is read verbatim from `aws_neuron_isa_tpb_s3d3_ts_select.h` and its layout
*compile-verified* by a real C program (`gcc -I … ; offsetof/sizeof`) against each gen's header.
All four gens print **byte-identical** offsets:

```c
typedef struct NEURON_ISA_TPB_S3D3_TS_SELECT_STRUCT {
    NEURON_ISA_TPB_HEADER             header;              //  4  ( 0 -  3)  opcode = 0x98
    NEURON_ISA_TPB_EVENTS             events;              //  8  ( 4 - 11)  wait/update semaphores
    uint8_t                           reserved0[4];        //  4  (12 - 15)  MUST be 0
    NEURON_ISA_TPB_TENSOR3D           src0_mem_pattern;    // 16  (16 - 31)  *** SELECT OPERAND A — the TENSOR ***
    uint8_t                           reversed_pred;       //  1  (32     )  *** blend-flip bool {0,1} ***
    uint8_t                           num_active_channels; //  1  (33     )  partition count 1..128
    NEURON_ISA_TPB_DTYPE              dtype;               //  1  (34     )  *** ONE dtype: src0/src1/dst, copy-only ***
    NEURON_ISA_TPB_DTYPE              pred_imm_dtype;      //  1  (35     )  *** predicate dtype — INT, same width ***
    uint8_t                           reserved1[2];        //  2  (36 - 37)  MUST be 0
    NEURON_ISA_TPB_IMM_SRC            src1_imm_src;        //  1  (38     )  {Inst=0, Ptr=1, RegPtr=2}
    NEURON_ISA_TPB_IMM_SRC            pred_imm_src;        //  1  (39     )  {Inst=0, Ptr=1, RegPtr=2}  (per-lane via Ptr)
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD src1_imm;            //  4  (40 - 43)  *** SELECT OPERAND B — the SCALAR IMM ***
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD pred_imm;            //  4  (44 - 47)  *** THE PREDICATE (0x0/0x1 if inst-imm) ***
    NEURON_ISA_TPB_TENSOR3D           dst_mem_pattern;     // 16  (48 - 63)  the OUTPUT TENSOR (== src0 elem count)
} NEURON_ISA_TPB_S3D3_TS_SELECT_STRUCT;

ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S3D3_TS_SELECT_STRUCT) == 64, "…NOT 64B.");
```

`offsetof` output (**identical** on sunda / cayman / mariana / maverick):

```text
sizeof S3D3_TS_SELECT = 64
  header 0   events 4   reserved0 12   src0_mem_pattern 16
  reversed_pred 32   num_active_channels 33   dtype 34   pred_imm_dtype 35
  reserved1 36   src1_imm_src 38   pred_imm_src 39   src1_imm 40   pred_imm 44   dst_mem_pattern 48
```

`[HIGH/OBSERVED — gcc offsetof compile output, four gens]`

The two `TENSOR3D` patterns (`@16` src0, `@48` dst) are the 16-byte 3D mem-patterns shared by the whole
family — `ADDR4 start_addr` + `int16_t step_elem[3]` + `uint16_t num_elem[3]`. The two immediate fields
(`src1_imm` `@40`, `pred_imm` `@44`) are each a 4-byte `IMM_VAL_INST_FIELD` union (a `uint32`/`int32` /
two `uint16` / four `uint8` / `imm_ptr` / `imm_reg` overlay) — the same union the
[base Tensor-Scalar](tensor-scalar.md) op uses for its `imm0`/`imm1`. `[HIGH/OBSERVED — common.h:682, :959]`

### 3.1. How it differs from `S3D3_TS` — a separate layout, not a `+comparator` variant

> **NOTE — this is the structural crux of the page.** `S3D3_TS_SELECT` is **not** the
> [cache/arith struct `S3D3_TS`](ts-cache-reduce.md) with extra fields bolted on; it is a *different*
> 64-byte layout with a different field set. The same 64 bytes carry completely different meaning past
> offset 12. The two are compile-verified side-by-side:

| offset | `S3D3_TS` (arith/cache ops) | `S3D3_TS_SELECT` (this op) |
|---|---|---|
| `@12` | `accumulator_cmd` (the "cache") | `reserved0[4]` (MUST be 0 — **no accumulator**) |
| `@16` | `src0_mem_pattern` (TENSOR) | `src0_mem_pattern` (TENSOR) — *the only shared field* |
| `@32` / `@33` | `in_dtype` / `out_dtype` (a **PAIR**, supports cast) | `reversed_pred` / `num_active_channels` |
| `@34` / `@35` | `num_active_channels` / `imm0_src` | `dtype` (**SINGLE**, copy-only) / `pred_imm_dtype` |
| `@36` / `@37` | `op0` / `op1` (**AluOp** per-elem fold + accumulate) | `reserved1[2]` (**NO AluOp at all**) |
| `@38` | `reverse_operands` (AluOp rev-ops) | `src1_imm_src` (`IMM_SRC`) |
| `@39` | `imm1_src` | `pred_imm_src` |
| `@40` | `imm0` (scalar 0) | `src1_imm` (**select operand B**) |
| `@44` | `imm1` (seed) | `pred_imm` (**the predicate**) |
| `@48` | `dst_mem_pattern` | `dst_mem_pattern` |

So Select has **no `AluOp` `op0`/`op1`, no `accumulator_cmd`, no `reverse_operands` AluOp, no
in/out dtype pair**. It has `reversed_pred` (the blend flip), a *single* copy-only `dtype`, a
*predicate* dtype, a scalar-imm operand, and a predicate immediate. This is a **select/blend
descriptor**, not an arithmetic descriptor. `[HIGH/OBSERVED — both structs compile-verified, offsets diffed]`

---

## 4. The select semantics — the central deliverable

The operative section is the header doc-block (verbatim) plus the validator `is_valid_tensor_scalar_select`
and its helper functions, all read verbatim. The header states the purpose directly:

> *"The existing Select operation is done through two instructions: a Copy instruction and a
> CopyPredicate instruction. The TensorScalarSelect instruction can speed up a special category of
> select operations where 1) one of the two select operands is an immediate value instead of a tensor
> and 2) the select predicate is an immediate value. … these immediates can be embedded in the
> instruction (… often the case for the immediate operand) or an immediate_ptr that has a different
> value for each lane (… likely done for the select predicate)."*  `[HIGH/OBSERVED — header §0]`

### 4.1. The two select sources

* **`src0_mem_pattern` `@16` — the TENSOR operand.** Header: *"the other select operand that is of
  tensor format."* A `TENSOR3D` mem-pattern in SBUF/PSUM. `tensor3d_valid(…, WriteTensor::False,
  AllowedInPSUM::True, AllowedInSBUF::True)`.
* **`src1_imm` `@40` — the SCALAR IMMEDIATE operand.** Header: *"one of the two select operands that
  is of immediate format"*; **broadcast to `dst_mem_pattern` when chosen.** Its source is
  `src1_imm_src` `@38` ∈ `{InstructionImmediate=0, PointerImmediate=1, RegPtrImmediate=2}`.

The blend is therefore **{tensor, scalar-imm}** — never tensor-vs-tensor. One arm is always the
broadcast scalar. (Contrast [RangeSelect](rangeselect.md): its arms are `{value, fixed −FLT_MAX fill}`.)
`[HIGH/OBSERVED — header constraints block verbatim]`

### 4.2. The condition — an IMMEDIATE predicate, not a runtime compare

* **`pred_imm` `@44` — the predicate**, of immediate format. Its source `pred_imm_src` `@39` ∈
  `{Inst=0, Ptr=1, RegPtr=2}`: an **instruction-immediate** (one global value) **or** a
  **pointer-immediate** (a *different value per DVE lane* — the per-lane mask) **or** a register-ptr.
* `has_valid_pred_inst_imm_value` (verbatim): if `pred_imm_src == InstructionImmediate`, then
  `pred_imm.imm_bitvec_int32 ∈ {0, 1}` — **only `0x0`/`0x1` legal as an inline predicate.** When it is
  a *pointer*-immediate, the per-lane values are read as a raw `i32` ("`pred_imm` can be any integer
  value; simply read i32 union value, unused bytes must be 0").
* **`pred_imm_dtype` `@35`** — the predicate's dtype, forced two ways:
  * `is_valid_int_dtype_datapath(pred_imm_dtype)` → ∈ `{INT8, INT16, INT32, UINT8, UINT16, UINT32}`
    (an **integer** datapath dtype only);
  * `has_same_imm_dtype_width(pred_imm_dtype, dtype)` → `get_type_size(pred_imm_dtype) ==
    get_type_size(dtype)` — **same byte width** as the *data* dtype. Header: *"Due to HW restrictions,
    `pred_imm_dtype` must have the same width as `dtype`."*

> **GOTCHA — there is no on-chip compare.** Unlike every compare-then-select idiom (and unlike
> [RangeSelect](rangeselect.md)'s two-comparator predicate), TensorScalarSelect has **no comparator
> field, no `comp_op`, no bound**. The condition is a **pre-computed boolean** (`0`/`1`). The compare
> that produced it ran in a *prior* instruction. TensorScalarSelect only **consumes** the boolean and
> blends on it — it is the *fused* `Copy` + `CopyPredicate` the header cites.

### 4.3. The blend — verbatim pseudocode

The doc-block computes, per partition (per DVE lane):

```text
pred_imm: immediate            reversed_pred: bool
src0_mem_pattern: tensor       src1_imm: immediate    # broadcast to dst when chosen

if reversed_pred:
    dst_mem_pattern = src1_imm        if pred_imm else src0_mem_pattern
else:
    dst_mem_pattern = src0_mem_pattern if pred_imm else src1_imm
```

i.e. `out = pred ? A : B`, where `(A,B) = (src0_tensor, src1_imm)` when `reversed_pred == 0` and
`(A,B) = (src1_imm, src0_tensor)` when `reversed_pred == 1`. `reversed_pred` (`@32`,
`has_valid_reversed_pred ∈ {0,1}`) simply *flips which of {tensor, scalar} is the pred-true arm.*
`dst` has the **same element count** as `src0` (`same_element_count_t3d`) — one output per input
element, a **full tensor** (no reduction, no packing). `[HIGH/OBSERVED — verbatim pseudocode + validator]`

C reimplementation of the worker's per-lane inner op (the worker symbol is the DVE
`"S: TensorScalarSelect"` body at MARIANA funcVA `0xcb24`):

```c
/* TensorScalarSelect — per-lane predicated blend.  S3D3_TS_SELECT (64 B), opcode 0x98.
 * Worker: "S: TensorScalarSelect" (DVE), MARIANA funcVA 0xcb24.  COPY-ONLY: dtype governs
 * src0/src1/dst alike, no cast (DVE data converters off). One pass; no recurrence.
 *
 * @param i      decoded S3D3_TS_SELECT instruction (header-verified layout)
 * @param lane   DVE partition index in [0, i->num_active_channels)
 * @param elem   element index within the lane (3D mem-pattern walk over src0/dst)
 * @return       the blended element, in `dtype` representation (bit-copied, never converted)
 */
static inline uint32_t ts_select_lane(const NEURON_ISA_TPB_S3D3_TS_SELECT_STRUCT *i,
                                      unsigned lane, unsigned elem)
{
    /* 1. Resolve the predicate.  Instruction-imm => one global 0/1; pointer-imm => per-lane. */
    int32_t pred = (i->pred_imm_src == NEURON_ISA_TPB_IMM_SRC_INSTRUCTION_IMMEDIATE)
                       ? i->pred_imm.imm_bitvec_int32                 /* validated ∈ {0,1} */
                       : load_pred_pointer_imm(i, lane);              /* per-lane i32 (raw) */

    /* 2. Read the two select operands.  src0 is the tensor; src1 is the broadcast scalar.
     *    No casting: both are interpreted in `dtype` width and copied verbatim. */
    uint32_t tensor_elem = read_tensor3d_elem(&i->src0_mem_pattern, lane, elem, i->dtype);
    uint32_t scalar_imm  = i->src1_imm.imm_bitvec_uint32;            /* broadcast to dst */

    /* 3. Blend.  reversed_pred flips which arm is pred-true (verbatim header pseudocode). */
    uint32_t a = i->reversed_pred ? scalar_imm  : tensor_elem;       /* pred-true  arm */
    uint32_t b = i->reversed_pred ? tensor_elem : scalar_imm;        /* pred-false arm */
    return pred ? a : b;                                            /* one MOVE/select, no ALU */
}
```

`[blend/operand logic HIGH/OBSERVED from header; the per-lane pointer-imm fetch INFERRED from the IMM_SRC contract, MED]`

### 4.4. Why this is a SELECT and not a reduce/scan/compare

There is **no `AluOp` field** (no `op0`/`op1`), **no `accumulator_cmd`**, **no comparator**, **no
Max-reduce**. The only operative fields are the two operands, the predicate, and the blend-flip. `dst`
is full-sized (`same_element_count`). The worker has a **single compute edge** (§5.3) — a one-pass
per-lane move with no loop-carried recurrence. It is the predicated-copy member of the family.

---

## 5. The datapath — copy-only MOVE, vbool-gated select (not compare+arith)

### 5.1. The MOVE-path strings (DVE DEBUG DRAM)

The MARIANA DVE DEBUG DRAM (read with `xtensa-elf-strings -t x` on the carved blob) shows the Select
worker on the **MOVE** datapath, **not** the `alu_op.cpp` arith path the cache ops use:

| DRAM-local off | string | meaning |
|---|---|---|
| `0x2490` | `S: MOVE` | the MOVE op header (copy-only path) |
| `0x2591` | `S: R[%d] = R[%d] = 0x%x` | **register move** — the TENSOR arm (`src0`) |
| `0x25aa` | `S: R[%d] = imm = 0x%x` | **immediate move** — the SCALAR arm (`src1_imm`) |
| `0x2499` | `…/decode/move.cpp:41 ((dtype==UINT32)\|\|(==INT32)\|\|(==FP32)) && "highest priority is full-register moves…"` | the copy-only move decoder |
| `0x25cb` / `0x25d6` | `move_shape` / `move_shape.cpp` | the move-pattern shape walker |

The two MOVE trace forms — `R[d] = R[d']` (register-move) and `R[d] = imm` (immediate-move) — **are the
two select arms**: per lane, MOVE either the tensor register (`src0`) or the immediate (`src1`) into
`dst`, gated by the predicate. This is the *"copy only, no casting (DVE data converters off)"* the
header states. `[HIGH/OBSERVED — DRAM strings]`

### 5.2. The vbool-gated select/move IVP vocabulary

In the cleaner-FLIX MARIANA `NX_DVE` PERF IRAM blob, the predicated dual-select / masked-move family is
byte-OBSERVED, each taking a **VBOOL** register (`vb0..vb15`) as the predicate operand. These are the
[B21 Select/Shuffle crossbar](../../isa/ref/b21-select-shuffle.md) members at the three lane widths the
single `dtype` field spans:

| IVP op | width / form | sample bundle |
|---|---|---|
| `ivp_dselnx16t` | 16-bit DUAL-select, merge form `_t`, vbool-gated | `…; ivp_dselnx16t v29,v13,v8,v0,v0,vb2` |
| `ivp_sel2nx8i_s4` / `ivp_sel2nx8t` | 8-bit predicated select | `…; ivp_sel2nx8i_s4 v28,…` |
| `ivp_mov2nx8t` | 8-bit predicated MOVE (the copy arm) | `…; ivp_mov2nx8t v3,v8,v0,vb5` |
| `ivp_seln_2x32t` / `ivp_dseln_2x32t` | 32-bit select | `…; ivp_seln_2x32t v0,v4,v0,v13,vb7` |

> **NOTE — the vbool is LOADED, not computed.** The critical structural read: the vbool register is
> populated **from `pred_imm`** (the instruction-immediate or the per-lane pointer-imm), **not** from
> an on-chip compare. Contrast the Dropout/RangeSelect pattern, which *generates* the vbool with an
> `ivp_olt…`/compare op and *then* consumes it with the same `ivp_dsel/sel…t` family. TensorScalarSelect
> **skips the compare stage** and loads the predicate directly into the vbool. `[select/move IVP
> vocabulary HIGH/OBSERVED in PERF; the exact bind of a SPECIFIC dsel bundle to the `0x98` worker is
> MED — PERF strips the `"S:"` self-names, so the vocabulary is established at family level and the
> predicate-gated-dsel = the blend is the structural read]`

### 5.3. The single compute edge (the one-pass signature)

The MARIANA Select worker frame (`0xcb24..0xcc19`, clean-aligned disasm) has the family's standard
shape: `entry a1,96` → `const16 a10,8 ; const16 a10,0x2921 ; call8 <LOG>` (the `"S: TensorScalarSelect"`
self-name load) → in-carve setup helpers → a `l32i.n`/`s32i.n` block that copies the `src0`/`dst`
`TENSOR3D` 3D-pattern fields into the frame → shared DVE setup → **exactly ONE** negative-literal
(out-of-carve) compute edge (`call0 0xfffbdca0`) → post helpers → `retw.n`.

> **The compute-edge census is the structural signature of the op kind:**
>
> | worker | negative-literal compute edges | shape |
> |---|---|---|
> | **TensorScalarSelect** | **1** (`call0 0xfffbdca0`) | one-pass predicated blend |
> | [CacheReduce](ts-cache-reduce.md) | 4 (3 contiguous `alu_op.cpp` band) | scan/reduce |
> | [CacheCumulative](ts-cache-cumulative.md) | many (the family's largest; ≫ 4) | cumulative recurrence |
>
> One edge ⇒ no loop-carried scan/accumulate (cache ops) and no compare+fill+reduce chain
> (RangeSelect) — just one move/select pass over the tensor. `[entry/log/setup/single-edge/retw
> HIGH/OBSERVED (direct count); the out-of-carve target `0xfffbdca0` LOW; that the edge IS the
> move/select compute (vs the cache ops' `alu_op.cpp` band) INFERRED from the move.cpp/move_shape.cpp
> DRAM strings, MED]`

---

## 6. The DVE dispatch chain — `0x98` → `"S: TensorScalarSelect"`

### 6.1. Engine = DVE (confirmed three ways)

1. **Validator constant.** `is_valid_tensor_scalar_select` ends with
   `has_valid_active_channel_range(num_active_channels, DVE_NUM_CHANNELS)` — the **DVE** channel
   constant (`DVE_NUM_CHANNELS == 128U`, `common.h:36`), whereas POOL ops use `POOLING_NUM_CHANNELS`.
2. **Self-name multiplicity.** `"S: TensorScalarSelect"` appears **4 times** in the library
   (`0x18db61` / `0x427e41` / `0x6efb61` / `0x8aff11`) — multiplicity 4 = the DVE pattern, co-located
   with `"S: RangeSelect"` (4) and `"S: TensorScalarCacheReduce"` (4). `[HIGH/OBSERVED — byte-scan]`
3. **Absent from POOL.** `0x98` is in **no** Q7_POOL `kernel_info_table` — it is **not** a POOL
   software kernel.

### 6.2. The self-name strings + worker funcVAs

| GEN | self-name (DRAM-local) | worker funcVA | prologue / log loader |
|---|---|---|---|
| CAYMAN | `0x2841` | `0xc540` | `entry a1,96 ; const16 a10,8 ; const16 a10,0x2841 ; call8 <LOG>` |
| MARIANA | `0x2921` | `0xcb24` | `entry a1,96 ; const16 a10,8 ; const16 a10,0x2921 ; call8 0x188a4` |
| MAVERICK | `0x2951` | `0xcaec` | `entry a1,96 ; const16 a10,8 ; const16 a10,0x2951 ; call8 <LOG>` |

The frame size is **96** (vs the cache ops' `entry a1,80` — Select carries a slightly larger frame).
The `const16 a10,8 ; const16 a10,strVA ; call8 <LOG>` is the family's worker LOG prologue; `strVA` is the
Select string, so the worker binding is unambiguous. The DVE-DRAM neighbourhood of the self-name is the
**predicated-copy** cluster — `"TS: CopyPredicatedScalar"` `0x294f`, `"VS: CopyPredicated"` `0x29de`,
`"S: CastPredicated"` `0x29f3`, `"S: CopyPredicatedReduce"` `0x2f2a` — i.e. Select sits with the
predicated-copy ops it fuses, *not* the arith/scan ops. `[MAR entry+loader+strVA HIGH/OBSERVED; CAY/MAV
entry+strVA-loader HIGH/OBSERVED, the CAY/MAV LOG-fn VA MED]`

### 6.3. The registration stub (MARIANA, the family template)

```text
0x2084: entry a1,48
        const16 a2,0 ; const16 a2,0xcb24     ; the Select worker funcVA
        s32i a2,[a1+12]                       ; write funcVA into the DVE kernel_info slot
        l32r a11,0xfffc81fc ; l32r a2,0xfffc377c   ; (negative PC-rel → opcode→descriptor table, out-of-carve)
        call8 0x9920                          ; the DVE kernel-register routine
        retw
```

`s32i a2,[a1+12]` writes the funcVA into the slot; `call8 0x9920` is the DVE kernel-register routine
(shared with the cache workers). The two `l32r` literals resolve **outside** the carved IRAM (the
runtime-bound `opcode-0x98 → descriptor` table), so the opcode→funcVA byte is **out-of-carve**.
`[stub edges HIGH/OBSERVED; descriptor literal LOW]`

### 6.4. The canonical chain

```text
TensorScalarSelect:
  [SEQ opcode 0x98]
    → [DVE kernel_info slot]  (registered by MARIANA stub @0x2084: s32i 0xcb24,[a1+12])
    → funcVA 0xcb24 ("S: TensorScalarSelect" worker)
    → load pred_imm (per-lane via ptr) into a vbool register
    → FLIX-program the predicated dual-select/move (ivp_dselnx16t / ivp_mov2nx8t / ivp_seln_2x32t,
      gated by vbool)
    → the single move/select compute edge (out-of-carve)
    → writes dst[i] = reversed_pred ? (pred?src1:src0) : (pred?src0:src1)   — copy-only, no cast
```

`[funcVA+worker+stub HIGH; opcode→descriptor literal LOW/out-of-carve]`

---

## 7. The dtype matrix — copy-only; no int32/uint32 exclusion; predicate = INT, same width

`is_valid_tensor_scalar_select`'s dtype clauses (verbatim):

```c
has_same_imm_dtype_width(pred_imm_dtype, dtype)        // pred byte-width == data byte-width
is_valid_int_dtype_datapath(pred_imm_dtype)            // pred dtype is an INT datapath dtype
is_valid_dtype(dtype, DtypeAllowFP32R::False)          // data dtype, the broad set
```

* **`is_valid_dtype(d, AllowFP32R::False)`** (common.h): excludes `FP32R`, `UINT64`, `INT64`, `FP4`,
  and the `CPTC*` codes — i.e. the **data dtype** (`src0`/`src1`/`dst`, one field) ∈ `{INT8, UINT8,
  INT16, UINT16, INT32, UINT32, BF16, FP16, FP32, FP8_E3, FP8_E4, FP8_E5}`. (Header restates: *"any
  valid ISA dtype except FP32r/UINT64/INT64."*)
* **`is_valid_int_dtype_datapath(d)`** ⇒ predicate dtype ∈ `{INT8, INT16, INT32, UINT8, UINT16,
  UINT32}` (header restates exactly).
* **`has_same_imm_dtype_width`** ⇒ `get_type_size(pred_imm_dtype) == get_type_size(dtype)`: the
  predicate width must **match** the data width — an `fp16` data dtype pairs with an `int16`/`uint16`
  predicate (2 B), `int8` data with an `int8`/`uint8` predicate (1 B), `fp32`/`int32` data with an
  `int32`/`uint32` predicate (4 B).

| data `dtype` (one field, src0/src1/dst) | width | legal `pred_imm_dtype` |
|---|---|---|
| `INT8`, `UINT8`, `FP8_E3/E4/E5` | 1 B | `INT8`, `UINT8` |
| `INT16`, `UINT16`, `BF16`, `FP16` | 2 B | `INT16`, `UINT16` |
| `INT32`, `UINT32`, `FP32` | 4 B | `INT32`, `UINT32` |
| `FP32R`, `UINT64`, `INT64`, `FP4`, `CPTC*` | — | **excluded** (data dtype illegal) |

> **QUIRK — no int32/uint32 exclusion on the data dtype.** The [cache ops](ts-cache-reduce.md)
> *forbid* `in == int32/uint32` (the accumulator-overflow guard). Select **accepts** `int32`/`uint32`
> as the data dtype — because Select is a **copy** (no accumulate, no widening), so there is no
> accumulator-precision concern. This is the dtype *tell* that Select is a copy, not an accumulate.
> Likewise there is **no FP32-compare hub**: Select does no comparison, so unlike
> [RangeSelect](rangeselect.md) (FP32-compare-only) it accepts integer *and* FP data dtypes
> symmetrically — it just moves bits.

`num_active_channels` (`@33`) is `1..128` (`DVE_NUM_CHANNELS`). DTYPE ordinals (common.h): `INVALID 0x0,
UINT64 0x1, INT8 0x2, UINT8 0x3, INT16 0x4, UINT16 0x5, BF16 0x6, FP16 0x7, INT32 0x8, UINT32 0x9, FP32
0xA, FP32R 0xB, INT64 0xC, FP8_E3 0xD, FP8_E4 0xE, FP8_E5 0xF, FP4_E2 0x10, CPTC1..7 0x19..0x1F`.

---

## 8. Contrast vs RangeSelect, AffineSelect, and the cache twins

### 8.1. vs [RangeSelect](rangeselect.md) (`0xbc`) — the *other* DVE select op

Both are DVE select ops in the same blobs, but **fundamentally different kinds** of select:

| dimension | RangeSelect (`0xbc`) | **TensorScalarSelect (`0x98`)** |
|---|---|---|
| struct | `S2D2_RS` (64 B) | `S3D3_TS_SELECT` (64 B) — **different struct** |
| mem pattern | 2D (`TENSOR2D`) | 3D (`TENSOR3D` src0/dst) |
| condition | **runtime compare**: `comp_op0`/`comp_op1` ∈ {EQ,GT,GE,LE,LT} vs two FP32 bounds | **pre-computed immediate** `pred_imm ∈ {0,1}` — **no comparator at all** |
| predicate origin | **generated on-chip** (`x cmp bound`) | **loaded** as an immediate / per-lane ptr-imm |
| two arms | `{x (in-range value), fill_val}` | `{src0 TENSOR, src1 SCALAR-IMM}` (programmable) |
| blend control | `pred ? x : −FLT_MAX` (fixed fill) | `reversed_pred` flips `pred ? src0 : src1` |
| reduction | OPTIONAL Max-reduce (`reduce_op == Max`) | **NONE** — pure elementwise blend |
| dtype | FP datapath only (FP32 compare hub), no integer | **any** exc `{fp32r/u64/i64}`, copy-only; pred = INT, same width |
| per-gen | CAYMAN+ (**absent on SUNDA**, `nc ≥ V3`) | **all four incl SUNDA** |
| datapath | compare → vbool → sel + Max-reduce | predicate-**load** → vbool → dsel/mov (1 pass) |
| compute edges | compare+select(+reduce) chain | **single** move/select edge |

RangeSelect = *"compute the mask AND select AND optionally reduce"*; TensorScalarSelect = *"the mask is
already a `0`/`1` immediate, just blend tensor-vs-scalar."* `[HIGH/OBSERVED — both headers/validators verbatim]`

### 8.2. vs [AffineSelect](affineselect.md) (`0x92`) — a separate POOL op (do not conflate)

`common.h` also defines `TENSOR_SCALAR_AFFINE_SELECT = 0x92 // Y`, but it is a **distinct** op:

* **struct `S2D2_TS_AS`** (64 B, mapped 1:1) — **not** `S3D3_TS_SELECT`. Its fields are a *geometric*
  affine mask: `in_out_dtype` (a **pair** — it *does* cast), `fill_mode` (`AFFINE_SELECT_CMP`),
  `fill_reg`, a `DATA4D mask_pattern` `@16`, `TENSOR2D` src/dst, and a `channel_multiplier` `@60`. It
  selects between the tensor and a **fill register** by an *affine geometric mask*, not by an
  immediate boolean predicate.
* **engine: POOL** (a Q7_POOL software kernel), **not** DVE.

So `0x92` AffineSelect (POOL, `S2D2_TS_AS`, affine-mask select with cast) and `0x98`
TensorScalarSelect (DVE, `S3D3_TS_SELECT`, immediate-predicate copy-only blend) are **separate ops on
separate engines with separate structs.** The `// SortMerge wip 0x97` reservation sits between them.
`[HIGH/OBSERVED — json + common.h + s2d2_ts_as.h]`

### 8.3. vs the cache twins ([CacheCumulative](ts-cache-cumulative.md) `0xe6` / [CacheReduce](ts-cache-reduce.md) `0x9a`)

Those are AluOp arith ops (`op0` fold + `op1` accumulate + an accumulator cache + scan/reduce emit) on
`S3D3_TS`. TensorScalarSelect shares **none** of that — no AluOp, no accumulator, no fold. It is the
predicated-copy member of the family, fusing the separate `Copy` + `CopyPredicate` instructions the
header cites.

---

## 9. Per-generation presence

| GEN | opcode `0x98` | `S3D3_TS_SELECT` | DVE self-name | worker funcVA | wired? |
|---|---|---|---|---|---|
| SUNDA (NC-v2) | defined `// Y` | 64 B id. (hdr ships) | (DVE RELEASE — self-name stripped) | present (no dbg name) | WIRED (release) |
| CAYMAN (NC-v3) | defined `// Y` | 64 B id. | DRAM `0x2841` | `0xc540` | WIRED |
| MARIANA (NC-v4) | defined `// Y` | 64 B id. | DRAM `0x2921` | `0xcb24` | WIRED |
| MARIANA_PLUS | (mariana hdr) | 64 B id. | 4th DEBUG copy `0x6efb61` | (stable) | WIRED |
| MAVERICK (NC-v5) | defined `// Y` | 64 B id. | DRAM `0x2951` | `0xcaec` | WIRED |

The opcode value `0x98` + the `// SortMerge wip 0x97 // Y` comment + the `S3D3_TS_SELECT` struct
(`offsetof`) are **byte-identical on all four gens**. **Unlike** [RangeSelect](rangeselect.md)
(`0xbc`, first at CAYMAN / `nc ≥ V3`), **TensorScalarSelect IS defined on SUNDA (NC-v2)** — its opcode
line + `s3d3_ts_select.h` ship for sunda, byte-identical to the others (only the NC-v comment differs).
**MAVERICK** additionally swaps `has_valid_active_channel_range` → `has_valid_active_channel_range_with_tile`
(using `header.inst_flags`) — the same MAVERICK tile relaxation the base op and cache ops show, **not** a
struct change. The `"S: TensorScalarSelect"` DVE worker self-name is in the 4 DEBUG DVE blobs
(CAY/MAR/MAR+/MAV); SUNDA ships a DVE **RELEASE** image (self-names stripped) so its worker is present but
not debug-named — the same gap the cache ops show for SUNDA. The op is a **core maintained `// Y` op**,
not a deprecated stub. `[HIGH/OBSERVED; SUNDA worker presence INFERRED from {opcode+struct defined +
SUNDA ships a DVE RELEASE image}; MAVERICK interior INFERRED — header-OBSERVED only]`

---

## 10. Reimplementation checklist

For a Vision-Q7-compatible GPSIMD rebuild, TensorScalarSelect requires:

1. **Decode** the 64-B `S3D3_TS_SELECT` (header `@0`, events `@4`, `reserved0` `@12` = 0, `src0` TENSOR3D
   `@16`, `reversed_pred` `@32`, `num_active_channels` `@33`, `dtype` `@34`, `pred_imm_dtype` `@35`,
   `reserved1` `@36` = 0, `src1_imm_src` `@38`, `pred_imm_src` `@39`, `src1_imm` `@40`, `pred_imm` `@44`,
   `dst` TENSOR3D `@48`); reject if `reserved0`/`reserved1` ≠ 0.
2. **Validate**: `pred_imm ∈ {0,1}` when inst-imm; `pred_imm_dtype` ∈ INT datapath and
   `width(pred_imm_dtype) == width(dtype)`; `dtype` ∈ broad set exc `{FP32R, UINT64, INT64, FP4}`;
   `reversed_pred ∈ {0,1}`; `same_element_count(src0, dst)`; `num_active_channels ∈ 1..128`.
3. **Resolve the predicate** per `pred_imm_src`: inst-imm (one global `0`/`1`) → broadcast vbool;
   pointer-imm → **per-lane** vbool load (a different value per DVE partition); reg-ptr-imm → register
   pointer. **No compare** — load the vbool directly.
4. **Resolve `src1_imm`** per `src1_imm_src` (inst/ptr/reg-ptr) and **broadcast** it to dst shape.
5. **Blend (copy-only, no cast):** for each lane/element, `dst = reversed_pred ? (pred?src1:src0) :
   (pred?src0:src1)`, implemented as a vbool-gated `ivp_*sel*/mov*t` at the lane width implied by
   `dtype` (8 / 16 / 32 b). Move bits — never convert.
6. **Engine:** route to **DVE** (`DVE_NUM_CHANNELS = 128`), **not** POOL; one-pass, no reduce.

---

## 11. Honesty ledger

**HIGH / OBSERVED** — opcode `0x98` `// SortMerge wip 0x97 // Y` byte-identical four gens incl SUNDA
(common.h); the `0x96 SORT` / unallocated-`0x97` / `0x98 SELECT` adjacency (the SortMerge-slot
**CORRECTION**); `S3D3_TS_SELECT` `sizeof 64` + all offsets, four gens (gcc `offsetof`); the verbatim
blend pseudocode + `is_valid_tensor_scalar_select` + `has_valid_pred_inst_imm_value` +
`has_same_imm_dtype_width` + `is_valid_int_dtype_datapath` + `is_valid_dtype(…,AllowFP32R::False)` +
`has_valid_reversed_pred` + `same_element_count_t3d`; the struct-vs-`S3D3_TS` diff (no AluOp / no
accumulator / single copy-only dtype); the MOVE-path DRAM strings + the vbool-gated select/move IVP
vocabulary; DVE three ways (`DVE_NUM_CHANNELS`, 4× self-name, POOL-absent); the worker funcVAs
(CAY `0xc540` / MAR `0xcb24` / MAV `0xcaec`, `entry a1,96` + LOG loader); the MARIANA registration stub
`@0x2084`; the single compute edge (1, vs CacheReduce 4 vs CacheCumulative ≫4); SUNDA defines the op while
RangeSelect does not.

**MED / INFERRED** — the bind of a *specific* PERF `ivp_dsel/mov` bundle to the `0x98` worker (PERF
strips the `"S:"` names; vocabulary is family-level); that the single compute edge `0xfffbdca0` IS the
move/select compute (from the move.cpp/move_shape DRAM strings); the per-lane pointer-imm predicate
fetch (from the `IMM_SRC` contract); the CAY/MAV LOG-fn VAs.

**LOW / UNRECOVERED** — the `opcode-0x98 → funcVA` descriptor bytes (stub `l32r` literals
`0xfffc81fc`/`0xfffc377c` resolve **out-of-carve**, runtime-bound); the move/select compute body
(`call0 0xfffbdca0` target out-of-carve; the edge is *reached* but not byte-walked end-to-end).

**FLIX-desync flag** — the DEBUG worker bodies desync under stock objdump on the recurring
`.byte 0x2f/0x8f/0x4f/0x5f/0x6f` literal-pool lead bytes; the Select `entry a1,96` (`36 c1 00`) renders
as `mul16u` until re-aligned at the entry byte (verified by hexdump + bounded re-disasm). The entry
prologue, log loader, call-edge census, and registration stub are byte-clean; the PERF image (cleaner
FLIX) supplies the predicated-select/move IVP vocabulary.

---

## See also

* [Tensor-Scalar + Tensor-Scalar-PTR](tensor-scalar.md) — the base `S3D3_TS` op and the
  `IMM_SRC {Inst/Ptr/RegPtr}` ptr-immediate mechanism this op reuses for the per-lane predicate.
* [RangeSelect](rangeselect.md) — the *comparison*-select contrast (runtime two-comparator predicate).
* [AffineSelect (TensorScalarAffineSelect)](affineselect.md) — the `0x92` POOL affine-mask select.
* [TensorScalarCacheCumulative](ts-cache-cumulative.md) / [TensorScalarCacheReduce](ts-cache-reduce.md)
  — the AluOp/accumulator siblings on the *other* (`S3D3_TS`) struct.
* [ISA Batch 21 — Select / Shuffle / Compress](../../isa/ref/b21-select-shuffle.md) — the vbool-gated
  `SEL`/`SHFL`/`DSEL` crossbar that realizes the blend datapath.
