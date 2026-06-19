# TensorScalarCacheCumulative (the cached-accumulator running-scan op)

> **Scope.** `TensorScalarCacheCumulative` is the **persistent-accumulator + running-scan** member of the
> TensorScalar\* family — opcode **`0xe6`**, riding the **same** 64-byte operand struct
> (`NEURON_ISA_TPB_S3D3_TS_STRUCT`) as the [base Tensor-Scalar op](tensor-scalar.md) but with the
> `accumulator_cmd@12` field **engaged** (the "Cache"). It is the **inclusive-prefix-scan** twin of
> [`TensorScalarCacheReduce`](ts-cache-reduce.md) (`0x9a`): the two share **every** validity function and
> differ **only** in the opcode predicate and in whether the worker writes the *running* accumulator at each
> step (Cumulative — a scan) or the *final* accumulator once (Reduce — a collapse).
>
> This page decodes: the dispatch chain (SEQ opcode `0xe6` → the DVE `"S: TensorScalarCacheCumulative"`
> worker); the `S3D3_TS` struct with the `accumulator_cmd` enum that drives the cache; the cached-accumulator
> **SCAN** datapath (loop-carried partials carried *across* calls/elements via `ivp_rotrn_2x32`⊕combine FLIX
> bundles, one out-element per in-element); the **two-AluOp scalar-fold** semantics (`op0` per-element fold,
> `op1` ∈ `{Add,Subtract,Mult,Max,Min}` loop-carried combine); the dtype matrix (FP datapath, or int8/16/u8/16
> — `int32`/`uint32` **excluded**); and per-gen presence (wired on every DVE-equipped gen).
>
> This op opens the **extended TensorScalar\* sub-family**: `CacheCumulative`/`CacheReduce` (this struct),
> `Select` (`S3D3_TS_SELECT`), `ImmLd` (`S2_BN`), `PtrMulti` (`S4D4_TSM`) — §10.
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md).

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | `TensorScalarCacheCumulative` = opcode **`0xe6`** (230), `// Y` maintained, **byte-identical** value on sunda/cayman/mariana/maverick; binds the **64-B** `S3D3_TS` struct. | `common.h:306` (4 gens diff'd) + `instruction_mapping.json` `struct2opcode` | HIGH/OBSERVED |
| 2 | It is the **exact twin** of [`TensorScalarCacheReduce`](ts-cache-reduce.md) (`0x9a`): `is_valid_tensor_scalar_cache_cumulative` and `is_valid_tensor_scalar_cache_reduce` are **byte-identical** save the opcode predicate (`has_tensor_cache_cumulative_opcode` vs `has_tensor_cache_reduce_opcode`). | `s3d3_ts.h:132–151` vs `:110–130` verbatim | HIGH/OBSERVED |
| 3 | The **"Cache"** = `accumulator_cmd@12` **forced non-Idle**: `has_valid_tensor_scalar_cache_reduce_accum_cmd` ⇒ `accumulator_cmd ∈ {ZeroAccumulate(3), Accumulate(2), LoadAccumulate(4)}` — a **persistent loop-carried accumulator** carried across calls/tiles. (Plain TS forces `Idle(0)`.) | `s3d3_ts.h:214–219` verbatim + `common.h:870–876` `ACCUM_CMD` | HIGH/OBSERVED |
| 4 | The **two-AluOp split**: `op0@36` = `is_general_arith_op` (per-element scalar fold `in[i] op0 scalar0`); `op1@37` = `is_valid_cache_reduce_op` ∈ `{Add(0x04),Subtract(0x05),Mult(0x06),Max(0x08),Min(0x09)}` (loop-carried accumulate). So `acc = acc op1 (in[i] op0 scalar0)`. | `tensor_scalar_cache_reduce_valid_ops` + `is_valid_cache_reduce_op` (`s3d3_ts.h:282–296`) verbatim | HIGH/OBSERVED |
| 5 | **"Cumulative"** = a running scan: `tensor_scalar_tensor_chk` forces `same_element_count_m3d(src,dst)` (one out-element per in-element), and the worker writes the accumulator **at each step**. ⇒ `out[i]` = inclusive prefix scan of `(in op0 scalar0)` under `op1`, seeded by `accum_cmd`. | `tensor_scalar_tensor_chk` (`s3d3_ts.h`) + the larger worker body (§4d) + the opcode name | HIGH for the contract; the inclusive-vs-exclusive emit-index INFERRED |
| 6 | The **scan datapath** is byte-observed in the PERF IRAM as the SIMD-scan recurrence: a **rotate** bundled in the same FLIX VLIW word as a **cache-reduce combine** — e.g. `{ … ivp_rotrn_2x32 v20,v4,v11 ; ivp_bmaxn_2x32 vb6,v4,v5,v18 }` (rotate-then-MAX = the Hillis-Steele inclusive-scan step for `op1=Max`). | MARIANA PERF IRAM bundles `@0x4916`/`0x29c5`/`0x939d`/`0xdf03` | HIGH/OBSERVED bundling; per-worker bind MED |
| 7 | The **scalar source** = `imm0` (per-imm `IMM_SRC {Inst0/Ptr1/RegPtr2}`, identical to plain TS). `imm1` is the **seed slot**, used **only** when `accumulator_cmd == LoadAccumulate`; otherwise `imm1_src==0 && imm1==0`. `reverse_operands` is **forced `None`**. | `tensor_scalar_cache_reduce_immediates_check` (`s3d3_ts.h:312–328`) + `_reverse_chk` (`:405–408`) verbatim | HIGH/OBSERVED |
| 8 | **Dispatch** = the DVE SEQ-ASCII `"S: TensorScalarCacheCumulative"` worker (not the Q7_POOL table). Worker funcVAs CAYMAN `0xa7c8` / MARIANA `0xacbc` / MAVERICK `0xa940`; MARIANA registration stub `@0x21d4`. **Wired** on every DVE gen. | DVE self-name @DRAM `0x20a0`; worker `0xacbc` entry+log; stub `0x21d4` (byte-decoded) | HIGH/OBSERVED |

---

## 2. Provenance / carve anchors

All device-firmware facts derive from static analysis of the shipped device-firmware blob (disassembled with
the Cadence Xtensa toolchain that ships *inside* the gpsimd-tools package, `XTENSA_CORE=ncore2gp`, Vision-Q7
FLIX/VLIW) plus the shipped host customop-lib **ISA C headers**. No source was consulted. This page builds on
[Tensor-Scalar](tensor-scalar.md) (the base anchor — the `S3D3_TS` struct, the splat-then-AluOp datapath, the
`IMM_SRC` scalar source, the `alu_op.cpp` compute) and re-verifies every carve in-task.

| Artifact | Value |
|----------|-------|
| Container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Container sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` (10,276,288 B) — re-verified in-task (== the [Tensor-Scalar](tensor-scalar.md) container) |
| Disassembler | `gpsimd_tools/…/bin/xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp` |
| MARIANA `NX_DVE` DEBUG IRAM | `MARIANA_NX_DVE_DEBUG_IRAM_get.data` VA `0x408fc0` / size `0x1c560` (`.rodata` VA == file offset) |
| MARIANA `NX_DVE` DEBUG DRAM | `MARIANA_NX_DVE_DEBUG_DRAM_get.data` VA `0x425520` / size `0x7000` (the `"S:"` self-name + `alu_op.cpp` strings) |
| MARIANA `NX_DVE` PERF IRAM | `MARIANA_NX_DVE_PERF_IRAM_get.data` VA `0x31f5c0` / size `0x13540` (cleaner FLIX; the rotate⊕combine scan-bundle source) |
| CAYMAN `NX_DVE` DEBUG IRAM/DRAM | VA `0x16f660` / `0x18b320` |
| MAVERICK `NX_DVE` DEBUG IRAM/DRAM | VA `0x8945c0` / `0x8ad5c0` |

The authoritative struct/enum/validator source is the shipped public ISA headers (same package):

- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_ts.h` — the `S3D3_TS` struct + `is_valid_tensor_scalar_cache_cumulative`/`_cache_reduce` + the `tensor_scalar_cache_reduce_*` checks.
- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h` — the opcode enum; `ALU_OP`; `IMM_SRC`; `ACCUM_CMD`; `DTYPE`; `is_valid_int_dtype_datapath`/`is_valid_fp_dtype_datapath`; `DVE_NUM_CHANNELS == 128U`.
- `…/neuron_<gen>_arch_isa/tpb/instruction_mapping.json` — the `struct2opcode` binding.

The `S3D3_TS` struct is **byte-identical** (`sizeof 64`, same field offsets) on cayman/mariana/maverick/sunda
(diff'd; `ISA_STATIC_ASSERT == 64` present on every gen). `[HIGH/OBSERVED]`

---

## 3. The opcode — `TENSOR_SCALAR_CACHE_CUMULATIVE` (`0xe6`)

Read verbatim from `aws_neuron_isa_tpb_common.h` (mariana; **identical value on every gen**):

```c
NEURON_ISA_TPB_OPCODE_TENSOR_CUMULATIVE_ARITH_OP        = 0x4E,    // Y     (reduce-family cumulative — NOT this op, §3a)
NEURON_ISA_TPB_OPCODE_TENSOR_CUMULATIVE_BITVEC_OP       = 0x5E,    // Y
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_ARITH     = 0x4F,    // n, ucode/kaenadve exists, not maintained/used  -> ts-ptrmulti
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_BITVEC    = 0x5F,    // n, …
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_IMM_LD_ARITH        = 0x70,    // n, …                                            -> ts-immld
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_IMM_LD_BITVEC       = 0x71,    // n, …
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_SELECT              = 0x98,    // SortMerge wip 0x97  // Y                         -> ts-select
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_REDUCE        = 0x9a,    // Y                                                -> ts-cache-reduce (the TWIN)
NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_SCAN_ARITH          = 0xe5,    // Y     (two-tensor scan — NOT this op, §3a)
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_CUMULATIVE    = 0xe6,    // Y     <===== THIS op
```

`0xe6` is `// Y` **maintained** (not a deprecated `// n` stub). It sits **adjacent** to `TensorTensorScan`
`0xe5` in the byte space but is a different op on a different struct (§3a). The opcode value is the only fact
on the SEQ axis; everything else is the `S3D3_TS` struct + the DVE worker. `[HIGH/OBSERVED — common.h verbatim,
4-gen diff confirms `0xe6` is byte-stable]`

> **NOTE — `0xe6` is a firmware kernel-lane opcode, not an Xtensa ISA mnemonic.** Keep the two axes distinct
> (the same caution [Tensor-Scalar](tensor-scalar.md) §3 raises): the `NEURON_ISA_TPB_OPCODE_*` enum is the
> *firmware kernel-lane* axis; the `ivp_*` roster (the `ivp_rotrn_2x32`, `ivp_bmaxn_2x32`, … of §5) is the
> *Xtensa ISA* axis. The opcode dispatches a firmware handler; that handler *emits* the IVP scan bundles.
> `[HIGH/OBSERVED]`

### 3a. Three distinct "cumulative/scan" ops — disambiguation

The ISA has **three** separate scan/cumulative ops; do not conflate them (each is a distinct opcode + struct):

| op | opcode | struct | what scans | page |
|----|--------|--------|-----------|------|
| **`TensorScalarCacheCumulative`** | `0xe6` | `S3D3_TS` | **scalar-fold + cached scan** (`(in op0 scalar) op1`-accumulated, cross-call cache) | **this page** |
| `TensorCumulative` Arith/Bitvec | `0x4E`/`0x5E` | `S4D4_TR` | reduce-family cumulative — scans over the **reduce axis**, the `REDUCE_OP` path | (reduce family) |
| `TensorTensorScan` Arith | `0xe5` | `S2S2D2_STT` | **two-tensor** scan (no scalar fold) | [tensor-tensor-scan.md](tensor-tensor-scan.md) |

`[HIGH/OBSERVED — common.h opcodes + instruction_mapping.json struct2opcode]`

### 3b. The struct→opcode binding (`instruction_mapping.json`)

The `struct2opcode` table binds **eight** opcodes to `NEURON_ISA_TPB_S3D3_TS_STRUCT` (mariana, verbatim
`jq`-read) — `CacheCumulative` and `CacheReduce` are **first-class** members, not aliased onto another struct:

```json
"NEURON_ISA_TPB_S3D3_TS_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_ARITH_OP",          // 0x43  tensor-scalar.md (base)
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_BITVEC_OP",         // 0x53  tensor-scalar.md (base)
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_ARITH_OP",      // 0x44  tensor-scalar.md (deprecated)
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_BITVEC_OP",     // 0x54  tensor-scalar.md (deprecated)
    "NEURON_ISA_TPB_OPCODE_TRANSPOSE_TENSOR_SCALAR_ARITH_OP",// 0x93  sibling (transpose-fused)
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_REDUCE",      // 0x9a  ts-cache-reduce.md (the TWIN)
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_CUMULATIVE",  // 0xe6  THIS page
    "NEURON_ISA_TPB_OPCODE_EXPONENTIAL"                      // 0x30  exponential.md
]
```

`[HIGH/OBSERVED]`

---

## 4. The operand struct — `NEURON_ISA_TPB_S3D3_TS_STRUCT` (64 B), `accumulator_cmd` engaged

`s3d3_ts.h:28`, `ISA_STATIC_ASSERT(sizeof == 64)` (all gens, diff'd byte-identical). This is the **exact same
64-byte struct** as the [base Tensor-Scalar op](tensor-scalar.md) — the layout below is byte-for-byte the same;
the **only** difference for CacheCumulative is which fields are *engaged* (the right column). Read verbatim from
the header's trailing offset comments:

| off | size | field | type | role for CacheCumulative |
|----:|-----:|-------|------|--------------------------|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `opcode = 0xe6` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync |
| 12 | 1 | `accumulator_cmd` | `ACCUM_CMD` | **★ THE "CACHE" — forced `{ZeroAccum(3)/Accum(2)/LoadAccum(4)}`, §4c** |
| 13 | 3 | `reserved0[3]` | `uint8` | **must be 0** (`s3d3_ts_reserved_zero`) |
| 16 | 16 | `src_mem_pattern` | `MEM_PATTERN3D` | **the INPUT tensor `in`** — `TENSOR3D` 3-D strided pattern |
| 32 | 1 | `in_dtype` | `DTYPE` | input dtype — **FP datapath OR int8/16/u8/16; NOT int32/uint32; NOT FP32R** (§7) |
| 33 | 1 | `out_dtype` | `DTYPE` | output dtype — `FP32R` **allowed** |
| 34 | 1 | `num_active_channels` | `uint8` | partition count `1..128` (`DVE_NUM_CHANNELS`) |
| 35 | 1 | `imm0_src` | `IMM_SRC` | **★ scalar0 SOURCE** `{Inst0/Ptr1/RegPtr2}` |
| 36 | 1 | `op0` | `ALU_OP` | **★ per-element scalar-fold AluOp** — `is_general_arith_op` (§4a) |
| 37 | 1 | `op1` | `ALU_OP` | **★ loop-carried accumulate AluOp** — `∈ {Add,Subtract,Mult,Max,Min}` (§4a) |
| 38 | 1 | `reverse_operands` | `TENS_SCALAR_REV_OPS` | **★ forced `None(0)`** (§4d) |
| 39 | 1 | `imm1_src` | `IMM_SRC` | **★ SEED source** — used only when `accum_cmd == LoadAccumulate` (§4e) |
| 40 | 4 | `imm0` | `IMM_VAL_INST_FIELD` (union) | **★ scalar0 VALUE** (4-B union) |
| 44 | 4 | `imm1` | `IMM_VAL_INST_FIELD` (union) | **★ SEED VALUE** — LoadAccumulate init; **else must be 0** |
| 48 | 16 | `dst_mem_pattern` | `MEM_PATTERN3D` | **the OUTPUT tensor `out`** — **same element count as `src`** (§4b) |

`[HIGH/OBSERVED — header text + `ISA_STATIC_ASSERT == 64`; struct byte-identical to tensor-scalar.md §4]`

The `ACCUM_CMD` enum (`common.h:870–876`, all gens):

```c
typedef enum NEURON_ISA_TPB_ACCUM_CMD {
    NEURON_ISA_TPB_ACCUM_CMD_IDLE            = 0,
    NEURON_ISA_TPB_ACCUM_CMD_ZERO            = 1,
    NEURON_ISA_TPB_ACCUM_CMD_ACCUMULATE      = 2,   // CONTINUE a previously-seeded accumulator (cross-tile carry)
    NEURON_ISA_TPB_ACCUM_CMD_ZERO_ACCUMULATE = 3,   // SEED to op1-identity, scan fresh
    NEURON_ISA_TPB_ACCUM_CMD_LOAD_ACCUMULATE = 4,   // SEED from imm1 (the loaded initial value)
} NEURON_ISA_PACKED NEURON_ISA_TPB_ACCUM_CMD;
```

`[HIGH/OBSERVED]`

### 4a. The two-AluOp split — per-element fold (`op0`) vs loop-carried combine (`op1`)

This is the **decisive** structural fact that separates CacheReduce/CacheCumulative from the base op. Read
verbatim from `tensor_scalar_cache_reduce_valid_ops` (`s3d3_ts.h:282–296`):

```c
// fn tensor_scalar_cache_reduce_valid_ops(i: Inst) -> bool {
//       is_valid_enum(EnumList::AluOp, i.s3d3_ts.op0)
//    && is_valid_enum(EnumList::AluOp, i.s3d3_ts.op1)
//    && is_general_arith_op(i.s3d3_ts.op0)        // op0 = PER-ELEMENT fold with scalar0
//    && is_valid_cache_reduce_op(i.s3d3_ts.op1)   // op1 = LOOP-CARRIED accumulate combiner
// }
//
// fn is_valid_cache_reduce_op(op: AluOp) -> bool {
//       (op == AluOp::Add)        // 0x04
//    || (op == AluOp::Subtract)   // 0x05
//    || (op == AluOp::Mult)       // 0x06
//    || (op == AluOp::Max)        // 0x08
//    || (op == AluOp::Min)        // 0x09
// }
```

`op1` is **restricted to the 5 associative reduction operators** (`{Add,Subtract,Mult,Max,Min}`) because it is
the running-accumulate combiner — the loop-carried recurrence must be associative for the SIMD-scan
(`acc op1 partial`). `op0` may be **any** `is_general_arith_op` (the 21-op set the
[Tensor-Scalar page §6b](tensor-scalar.md) enumerates: `Bypass, Add, Subtract, Mult, Max, Min, LogicalAnd/Or/Xor,
IsEQ/GT/GE/LE/LT/NE, AbsoluteDiff, AbsoluteValue, AbsMax, AbsMin, ReLU, Square` — minus `Divide/Pow/Mod/Rsqrt`
and the int band). This `op0`-general / `op1`-restricted role split is **unique** to CacheReduce/CacheCumulative —
the base TS lets **both** `op0` and `op1` be general-arith. `[HIGH/OBSERVED — both functions verbatim header]`

The recurrence per element `i`:

```c
// TensorScalarCacheCumulative per-element compute. op0 = is_general_arith_op (the scalar fold);
// op1 ∈ {Add,Subtract,Mult,Max,Min} (the loop-carried accumulate). alu() = the shared alu_op.cpp dispatcher
// (the "S: OP=%x R[%d] = OP(R[%d], imm)" trace for the op0 fold, "…OP(R[%d], R[%d])" for the op1 combine).
// Validity is pre-checked by is_valid_tensor_scalar_cache_cumulative (s3d3_ts.h:132) before this runs.
static void ts_cache_cumulative_scan(const elem_t *in, elem_t *out, size_t n,
                                     scalar_t s0, alu_op_t op0, alu_op_t op1,
                                     accum_t *acc /* the PERSISTENT cache register */) {
    for (size_t i = 0; i < n; ++i) {
        accum_t tmp = alu(op0, /*lhs=*/in[i], /*rhs=*/s0);   // per-element scalar fold: in[i] op0 scalar0
        *acc = alu(op1, /*lhs=*/*acc, /*rhs=*/tmp);          // loop-carried accumulate: acc = acc op1 tmp
        out[i] = *acc;                                       // CUMULATIVE: emit the running accumulator AT EACH i
    }
    // CacheReduce (the twin) is identical except it writes *acc ONCE after the loop, not out[i] per step.
}
```

`reverse_operands` being forced `None` (§4d) pins the fold to **`(in[i] op0 scalar0)`** (scalar on the right) —
the scan is not operand-reversible. `[op0/op1 split HIGH/OBSERVED; the per-element loop body is the out-of-carve
`alu_op.cpp` compute, the structure INFERRED from the contract + §5 datapath]`

### 4b. The "Cumulative" emit contract — one out-element per in-element

`tensor_scalar_tensor_chk` (the **shared** src/dst check, `s3d3_ts.h`) — read verbatim:

```c
// fn tensor_scalar_tensor_chk(i: Inst) -> bool {
//       mem3d_valid(i.s3d3_ts.src_mem_pattern, i.s3d3_ts.in_dtype,  WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)
//    && mem3d_valid(i.s3d3_ts.dst_mem_pattern, i.s3d3_ts.out_dtype, WriteTensor::True,  AllowedInPSUM::True, AllowedInSBUF::True)
//    && indirect_dst_size_limit_m3d(i.s3d3_ts.dst_mem_pattern)
//    && same_element_count_m3d(i.s3d3_ts.src_mem_pattern, i.s3d3_ts.dst_mem_pattern)
// }
```

`same_element_count_m3d(src, dst)` forces `dst` to have the **same element count as `src`** — the scan emits
**one output element per input element**. Combined with the running accumulator (§4c) and the `op0`/`op1` split
(§4a), `TensorScalarCacheCumulative` computes the **inclusive prefix scan** of `(in op0 scalar0)` under `op1`,
seeded by `accum_cmd`:

```text
out[i] = (((seed) op1 (in[0] op0 s0)) op1 (in[1] op0 s0)) … op1 (in[i] op0 s0)
seed   = op1-identity          (ZeroAccumulate)
       | imm1                  (LoadAccumulate)
       | the carried cache     (Accumulate — cross-tile)
```

> **CORRECTION — `same_element_count` does NOT distinguish Cumulative from Reduce; both use it.**
> `tensor_scalar_tensor_chk` is the **shared** src/dst check, called by `is_valid_tensor_scalar_cache_cumulative`
> **and** `is_valid_tensor_scalar_cache_reduce` (the header's two validators are byte-identical save the opcode
> predicate, §4f). So the scan-emit-per-element vs reduce-collapse-to-one distinction is **not** gated by a
> struct field — it lives entirely in the **worker emit logic** (CacheCumulative writes `out[i]` each step;
> CacheReduce writes once). The byte evidence for the split is the **larger CacheCumulative worker body** —
> 9 compute-edge `call0`s vs the CacheReduce twin's 4 (§4d). A reimplementer cannot tell the two apart from
> the operand struct alone; they are distinguished only by `header.opcode` (`0xe6` vs `0x9a`).
> `[HIGH/OBSERVED — both validators verbatim; the worker-body delta HIGH/OBSERVED]`

`[same_element_count HIGH/OBSERVED; the inclusive-scan formula INFERRED from {accum seed + op0/op1 split +
same_element_count + the "Cumulative" name + the PERF rotate⊕combine recurrence §5}; the exact inclusive-vs-
exclusive boundary MED — it sits in the out-of-carve `alu_op.cpp` loop]`

### 4c. The cache (`has_valid_tensor_scalar_cache_reduce_accum_cmd`)

```c
// fn has_valid_tensor_scalar_cache_reduce_accum_cmd(i: Inst) -> bool {
//       (i.s3d3_ts.accumulator_cmd == AccumCmd::ZeroAccumulate)   // 3 — seed to op1-identity, scan fresh
//    || (i.s3d3_ts.accumulator_cmd == AccumCmd::Accumulate)       // 2 — CONTINUE a previously-seeded accumulator (cache carry)
//    || (i.s3d3_ts.accumulator_cmd == AccumCmd::LoadAccumulate)   // 4 — seed FROM imm1 (loaded init value)
// }
```

The accumulator is a **persistent register the engine carries across invocations**: seed once
(`ZeroAccumulate` to identity, or `LoadAccumulate` from `imm1`), then chain more tiles with `Accumulate` —
each call extends the same running scan, so the prefix continues across a tiled input. `Idle(0)` and `Zero(1)`
are **rejected** (and conversely the base TS ops reject everything **but** `Idle` via `has_zero_accum_cmd_field`,
[tensor-scalar.md §4c](tensor-scalar.md)). This is the same "seeded-once, carried" accumulate idiom the reduce
family uses, here realized on the `S3D3_TS` `accumulator_cmd` field rather than a `REDUCE_OP` path.
`[HIGH/OBSERVED — verbatim header]`

> **GOTCHA — `Accumulate(2)` reads an accumulator this instruction does NOT seed.** With `accum_cmd ==
> Accumulate`, the running partial is whatever a **prior** `ZeroAccumulate`/`LoadAccumulate` instruction left
> in the DVE per-lane accumulator. A reimplementer must preserve the accumulator register **between** SEQ
> instructions for the cross-tile carry to be correct — the "Cache" is *state that outlives a single
> instruction*. Issuing `Accumulate` without a prior seed reads stale lanes. `[INFERRED from the accum_cmd
> semantics + the cross-call worker shape; MED]`

### 4d. `reverse_operands` forced `None`, and the scalar source / seed

`tensor_scalar_cache_reduce_reverse_chk` (`s3d3_ts.h:405–408`) — read verbatim:

```c
// fn tensor_scalar_cache_reduce_reverse_chk(i: Inst) -> bool {
//       is_valid_enum(EnumList::TensScalarRevOps, i.s3d3_ts.reverse_operands)
//    && (i.s3d3_ts.reverse_operands == TensScalarRevOps::None)   // FORCED None
// }
```

Unlike the base TS op (which allows `None/First/Second/Both` to flip the `{scalar−src vs src−scalar}` order,
[tensor-scalar.md §4a](tensor-scalar.md)), the cached scan **forces `reverse_operands == None`** — the fold is
**always** `(in[i] op0 scalar0)` with the scalar on the right. The scan recurrence has no reversed form.
`[HIGH/OBSERVED]`

### 4e. The scalar source (`imm0`) and the seed (`imm1`)

`tensor_scalar_cache_reduce_immediates_check` (`s3d3_ts.h:312–328`) — read verbatim:

```c
// fn tensor_scalar_cache_reduce_immediates_check(i: Inst) -> bool {
//       (   ((i.s3d3_ts.imm0_src == ImmSrc::InstructionImmediate) && ts_imm_chk(i, i.s3d3_ts.imm0))
//        || ((i.s3d3_ts.imm0_src == ImmSrc::PointerImmediate)     && ts_ptr_imm_chk(i, i.s3d3_ts.imm0))
//        || ((i.s3d3_ts.imm0_src == ImmSrc::RegPtrImmediate)      && is_valid_imm_reg(i.s3d3_ts.imm0.imm_reg)))
//    && (   (   (i.s3d3_ts.accumulator_cmd == AccumCmd::LoadAccumulate)         // imm1 IS the seed when LoadAccumulate
//            && (   ((i.s3d3_ts.imm1_src == ImmSrc::InstructionImmediate) && ts_imm_chk(i, i.s3d3_ts.imm1))
//                || ((i.s3d3_ts.imm1_src == ImmSrc::PointerImmediate)     && ts_ptr_imm_chk(i, i.s3d3_ts.imm1))
//                || ((i.s3d3_ts.imm1_src == ImmSrc::RegPtrImmediate)      && is_valid_imm_reg(i.s3d3_ts.imm1.imm_reg))))
//        || (   (i.s3d3_ts.imm1_src == 0)                                       // ELSE imm1 unused — must be 0
//            && (i.s3d3_ts.imm1.imm_bitvec_uint32 == 0)))
// }
```

So:

- **`imm0`** (scalar0) — the per-element fold scalar, sourced exactly as the base TS op: per-imm `IMM_SRC
  {InstructionImmediate=0, PointerImmediate=1, RegPtrImmediate=2}` (inline / `PartitionOffset` pointer /
  register-held pointer), validated by `ts_imm_chk` / `ts_ptr_imm_chk` / `is_valid_imm_reg`. The on-DVE
  `RegPtr` reg-fetch path is the same `bnei a2,2` sub-dispatcher [tensor-scalar.md §7a](tensor-scalar.md)
  documents (`0xa130`).
- **`imm1`** (the seed) — **gated on `accumulator_cmd == LoadAccumulate`**. When loading, `imm1` may be
  Inst/Ptr/RegPtr (the loaded initial accumulator value). **Otherwise** `imm1_src` and `imm1` **must both be 0**
  — `ZeroAccumulate` and `Accumulate` need no explicit seed (the seed is the `op1`-identity, or the carried
  cache). `imm1` is the **explicit cache seed slot**.

> **GOTCHA — do not use the swapped `IMM_SRC_N` enum.** As [tensor-scalar.md §7](tensor-scalar.md) flags,
> `common.h` also defines `NEURON_ISA_TPB_IMM_SRC_N` with `Pointer=0, Instruction=1` — the **swapped**
> ordering. The `S3D3_TS` `imm0_src`/`imm1_src` fields here are `NEURON_ISA_TPB_IMM_SRC` (`Instruction=0`).
> Decoding `imm_src == 0` as "pointer" inverts inline vs pointer for the scalar and the seed.
> `[HIGH/OBSERVED]`

`[HIGH/OBSERVED — verbatim header]`

### 4f. The full validator — byte-identical to CacheReduce save the opcode predicate

`is_valid_tensor_scalar_cache_cumulative` (`s3d3_ts.h:132–151`) is **byte-for-byte identical** to
`is_valid_tensor_scalar_cache_reduce` (`:110–130`) **except** line 1's opcode predicate. Read verbatim:

```c
// fn is_valid_tensor_scalar_cache_cumulative(i: Inst) -> bool {
//       has_valid_neuron_header(i)
//    && has_valid_neuron_events(i)
//    && has_tensor_cache_cumulative_opcode(i)            // <<< THE ONLY DIFFERENCE: opcode == 0xe6
//                                                        //     (CacheReduce uses has_tensor_cache_reduce_opcode == 0x9a)
//    && tensor_scalar_cache_reduce_valid_ops(i)          // op0 general-arith, op1 ∈ {Add,Sub,Mult,Max,Min}  (§4a)
//    && tensor_scalar_cache_reduce_valid_types(i)        // fp datapath OR int8/16/u8/16 (no i32/u32)         (§7)
//    && tensor_scalar_cache_reduce_immediates_check(i)   // imm0 = scalar; imm1 = LoadAccumulate seed         (§4e)
//    && tensor_scalar_tensor_chk(i)                      // src/dst valid + same_element_count                (§4b)
//    && tensor_scalar_cache_reduce_reverse_chk(i)        // reverse_operands == None                          (§4d)
//    && s3d3_ts_reserved_zero(i)                         // reserved0[3] == 0
//    && has_valid_tensor_scalar_cache_reduce_accum_cmd(i)// accum_cmd ∈ {ZeroAccum, Accum, LoadAccum}         (§4c)
//    && is_valid_dtype(i.s3d3_ts.in_dtype,  DtypeAllowFP32R::False)  // input bars FP32R
//    && is_valid_dtype(i.s3d3_ts.out_dtype, DtypeAllowFP32R::True)   // output admits FP32R
//    && is_valid_aluop(i.s3d3_ts.op0) && is_valid_aluop(i.s3d3_ts.op1)
//    && has_valid_active_channel_range(i.s3d3_ts.num_active_channels, POOLING_NUM_CHANNELS)  // 1..128
//    && check_m3d_active_channels(src, n) && check_m3d_active_channels(dst, n)
//    && mem3d_valid(src, in_dtype,  WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)
//    && mem3d_valid(dst, out_dtype, WriteTensor::True,  AllowedInPSUM::True, AllowedInSBUF::True)
// }
//
// fn has_tensor_cache_cumulative_opcode(i) -> bool { i.unknown.header.opcode == Opcode::TensorScalarCacheCumulative }
// fn has_tensor_cache_reduce_opcode(i)     -> bool { i.unknown.header.opcode == Opcode::TensorScalarCacheReduce }
```

In one sentence: **apply the two-AluOp scalar fold-then-accumulate to every element of the `src` `TENSOR3D`
(PSUM or SBUF), carrying a persistent accumulator per `accum_cmd`, and write the running accumulator to the
same-element-count `dst` `TENSOR3D`.** Both `src` and `dst` are valid in **PSUM and SBUF**, so a cached scan
can read straight off the PE-array matmul output. `[HIGH/OBSERVED — both validators verbatim; the
single-predicate delta is the byte proof of the twin relationship]`

---

## 5. The loop-carried scan datapath — rotate ⊕ combine (PERF IRAM byte-observed)

The MARIANA `NX_DVE PERF IRAM` (VA `0x31f5c0` / size `0x13540`; carved + disassembled `XTENSA_CORE=ncore2gp`,
31,786 lines) is the **cleaner-FLIX** datapath source (PERF strips the `"S:"` log strings, so it disassembles
without the literal-pool desync of the DEBUG bodies — the [Tensor-Scalar §5](tensor-scalar.md) method). It shows
the SIMD-scan recurrence as **rotate ⊕ combine** FLIX bundles (device-local VAs, byte-clean):

```asm
; PERF @0x4916 — rotate the partial-accumulator vector by stride v11, then MAX with the un-rotated = op1=Max scan step
{ bbci.w15 a5,3,0x4935 ; ivp_lvn_2x16s_i v24,a9,96 ; addi.a a12,a8,-80 ; ivp_rotrn_2x32 v20,v4,v11 ; ivp_bmaxn_2x32 vb6,v4,v5,v18 }

; PERF @0x29c5 — rotate + multiply-partial + select-blend = op1=Mult scan step
{ bnei.w15 a5,16,0x544 ; ivp_lvnx8u_i v10,a14,0x16a0 ; ivp_mulpn16xr16 wv1,v12,v0,pr9 ; ivp_rotrn_2x32 v16,v5,v27 ; ivp_sel2nx8i_s4 v10,v3,v25,0 }

; PERF @0x939d — rotate + abs-sub = op0=AbsoluteDiff fold inside the scan window
{ bnei.w15 a13,-1,0x945a ; l16si a1,a10,0 ; ivp_cvt24unx32l wv2,v16,v24 ; ivp_rotrn_2x32 v3,v10,v6 ; ivp_babssub2nx8 vb9,v6,v5,v24 }

; PERF @0xdf03 — immediate-rotate variant of the same shift-then-combine
{ … ; subx2 a1,a0,a15 ; ivp_rotri2nx8 v2,v2,0 ; ivp_babssub2nx8 vb4,v2,v28,v16 }
```

**The mechanism.** `ivp_rotrn_2x32` / `ivp_rotri2nx8` **rotates** the accumulator vector by a lane stride; the
bundled cache-reduce **combine** op (`ivp_bmaxn_2x32` / `ivp_mulpn16xr16` / `ivp_addn_2x32t` for `op1 ∈
{Max, Mult, Add}`) folds the rotated partial with the un-rotated vector — the **Hillis-Steele / Kogge-Stone
inclusive-scan step** (each pass doubles the stride; `log2(N)` passes give the full prefix over a 32/64-lane
register). The rotated accumulator **is** the loop-carried carry the cumulative scan needs. Annotated:

```c
// One Hillis-Steele inclusive-scan pass over a SIMD accumulator vector (op1 = the cache-reduce combine).
// ivp_rotrn_2x32(acc, stride) shifts the partials by `stride` lanes; combine(op1) folds rotated⊕original.
// Bundled in ONE FLIX word so the rotate and the combine co-issue (no separate carry register spill).
static vec_t scan_pass(vec_t acc, int stride, alu_op_t op1) {
    vec_t shifted = ivp_rotrn_2x32(acc, stride);   // the loop-carried carry: partials from `stride` lanes back
    return combine(op1, acc, shifted);             // ivp_bmaxn_2x32 / ivp_mulpn16xr16 / ivp_addn_2x32t
}
// log2(lanes) passes (stride = 1,2,4,…) produce the inclusive prefix over the vector;
// the cross-vector carry threads the last lane of vector k into vector k+1 (the persistent `accum_cmd` cache).
```

In-task counts on the PERF disassembly (re-grounded to the live disasm, not the decompile): **9** rotate ops
(`ivp_rotrn_2x32`/`ivp_rotri2nx8`), **28** scalar-splat ops (`ivp_rep2nx8t`/`ivp_repnx16t`/`ivp_repn_2x32t` —
materialising `scalar0` before the `op0` fold, identical to [tensor-scalar.md §5a](tensor-scalar.md)), **15**
lane-fold reduce ops (`ivp_rbmaxn_2x32`/`ivp_rbminnumnxf16t` — the **CacheReduce-twin collapse**, read all lanes
→ one value). `[rotate⊕combine + splat bundling HIGH/OBSERVED in PERF; counts re-grounded to `rg -c` on the
live disasm]`

> **HONEST CAVEAT — the rotate⊕combine vocabulary binds to the family, not byte-pinned to the 0xe6 worker.**
> PERF strips the `"S:"` self-names, so the scan vocabulary is established at **family level**; the bind of
> *these specific* bundles to the `0xe6` worker is `[MED]`. The exact `op1`-code → single-IVP-combine selection
> and the inclusive-vs-exclusive scan boundary sit in the **out-of-carve** `alu_op.cpp` compute (the 9 negative-
> literal `call0` edges of §4d, `0xfffbde3c..0xfffbdf44` resolve outside the carved IRAM). The rotate⊕combine
> **vocabulary** and the `same_element_count` scan-emit contract are OBSERVED; the per-step routing is reported
> **structurally** (MED).

---

## 6. The dispatch chain — opcode `0xe6` → `"S: TensorScalarCacheCumulative"` worker

The op dispatches on the **DVE SEQ-ASCII `"S:"` surface** (not the Q7_POOL `kernel_info_table`). Three streams
agree: the self-name string, the worker funcVA, and the registration stub.

### 6a. The DVE self-name strings (carved MARIANA `NX_DVE` DEBUG DRAM)

`xtensa-elf-strings -t x` on the carved DRAM — the whole extended family self-names (these VAs are the
`const16` loader operands in §6c):

| GEN | CacheReduce | **CacheCumulative** | ImmLd Arith/Bitvec | PtrMulti Arith/Bitvec | Select | TensorTensorScan |
|-----|-------------|---------------------|--------------------|-----------------------|--------|------------------|
| CAYMAN | `0x1f4c` | **`0x1fbf`** | `0x275f`/`0x277a` | `0x27c0`/`0x27de` | `0x2841` | `0x2950` |
| MARIANA | `0x202d` | **`0x20a0`** | `0x283f`/`0x285a` | `0x28a0`/`0x28be` | `0x2921` | `0x2a2f` |
| MAVERICK | `0x205c` | **`0x20cf`** | `0x286f`/`0x288a` | `0x28d0`/`0x28ee` | `0x2951` | `0x2a5f` |

`"S: TensorScalarCacheCumulative"` is present on **all three DVE images** (re-verified in-task: MARIANA carve
yields `0x20a0`). `[HIGH/OBSERVED]`

### 6b. The worker funcVAs (entry prologue + LOG self-name loader byte-verified)

| GEN | CacheCumulative funcVA | prologue / log loader |
|-----|-----------------------:|------------------------|
| CAYMAN | `0xa7c8` | `entry a1,80 ; const16 a10,8 ; const16 a10,0x1fbf ; call8 0x18010` |
| MARIANA | `0xacbc` | `entry a1,80 ; const16 a10,8 ; const16 a10,0x20a0 ; call8 0x188a4` |
| MAVERICK | `0xa940` | `entry a1,80 ; const16 a10,8 ; const16 a10,0x20cf ; call8 0x151d0` |

The CacheReduce twin worker is the **consecutive** entry (MARIANA `0xa7b0` / log `0x202d`). The
`const16 a10,8 ; const16 a10,strVA ; call8 LOG` prologue is the [Tensor-Scalar](tensor-scalar.md) worker LOG
template verbatim. Re-verified in-task on the MARIANA carve:

```asm
acbc: entry   a1, 80
acc2: const16 a10, 8
acc5: const16 a10, 0x20a0          ; load the "S: TensorScalarCacheCumulative" self-name VA
acc8: call8   0x188a4              ; the DVE LOG routine
```

`[HIGH/OBSERVED — xxd/disasm of the prologue]`

### 6c. The registration stub (MARIANA `0x21d4`, byte-decoded)

```asm
21d4: entry   a1, 48
21d7: const16 a2, 0
21da: const16 a2, 0xacbc           ; the CacheCumulative worker funcVA
21dd: s32i    a2, a1, 12           ; write funcVA into the DVE kernel_info slot [a1+12]
21e1: l32r    a11, 0xfffc834c      ; NEGATIVE literal -> out-of-carve descriptor table
21e4: l32r    a2,  0xfffc38cc      ; NEGATIVE literal -> out-of-carve
…     call8   0x9920               ; the DVE kernel-register routine (== tensor-scalar.md)
```

`s32i a2,[a1+12]` writes the funcVA into the slot; `call8 0x9920` is the shared DVE kernel-register routine. The
**consecutive twin** stub registers CacheReduce: `0x21be: const16 a2,0xa7b0` (the `0xa7b0` worker). The two
`l32r` literals (`0xfffc834c`/`0xfffc38cc`) are **negative** PC-relative — they resolve **outside** the carved
IRAM (the runtime-bound `opcode 0xe6 → descriptor` table), so the `opcode → funcVA` byte is out-of-carve, the
same [Tensor-Scalar §9a](tensor-scalar.md) limitation. `[stub edges HIGH/OBSERVED; descriptor literal
LOW/out-of-carve]`

### 6d. The worker body shape (MARIANA `0xacbc`, as far as FLIX allows)

```asm
acbc: entry   a1, 80
acc2/acc5/acc8: const16 a10,8 ; const16 a10,0x20a0 ; call8 0x188a4    ; LOG "S: TensorScalarCacheCumulative"
acde: s32i.n  a2,[a10+44]                                              ; write a config field
ace0: call0   0x6bf04 ; ace3: call0 0x2bf64                            ; setup helpers (in-carve)
ace8..acf9: saltu a2,a3,a2 -> s8i [a1+28/24] ; extui a12/a13,*,0,1     ; 2 single-bit config flags (the TS worker shape)
ad06: call8   0x9f2c ; ad0c: call8 0x99bc                             ; shared DVE setup
ad17/ad3e/ad45/…: s16i a11,[a4+176] / s16i a11,[a4+0x130]             ; FLIX vector-register-WINDOW programming
       extui a11,a2,26,1 ; and a6,a6,<mask> ; add a6,a6,a8            ; (pack control bits at field 14/26)
ad1c/ad4a/ad6f/ad93/adb8/ade1/ae03/ae27/ae51: call0 0xfffbbe10, 0xfffbde3c..0xfffbdf44  ; -> the alu_op.cpp scan/accumulate compute (9 NEG literals, OUT-OF-CARVE)
…     retw  (next function at 0xb0d0)
```

**Flow:** log self-name → write config field → compute config flags → program the FLIX vector window for the
splat+fold+scan → enter the shared `alu_op.cpp` compute (a chain of **9** `call0` compute edges) → return.

> **NOTE — the 9-vs-4 compute-edge delta is the byte signature of SCAN vs COLLAPSE.** The CacheCumulative body
> has a **9-edge** `call0` compute chain (`0xfffbbe10` + the contiguous `0xfffbde3c/de60/de84/deac/ded4/def4/
> df18/df44`); the CacheReduce twin (`0xa7b0`, smaller body) has only **4**. This is a **direct count** of
> `call0` targets in each body, re-verified in-task. The longer chain is consistent with the scan **emitting
> running partials at each step** vs the reduce's single final collapse. `[the 9-edge count HIGH/OBSERVED
> (direct count); the exact FLIX compute bundles MED through the literal-pool desync; the out-of-carve targets
> LOW]`

### 6e. The canonical chain

```text
TensorScalarCacheCumulative : [SEQ opcode 0xe6]
  -> [DVE kernel_info slot  (registered by MARIANA stub 0x21d4)]
  -> funcVA 0xacbc ("S: TensorScalarCacheCumulative" worker)
  -> FLIX window program for splat(scalar0) + op0-fold + op1-scan
  -> alu_op.cpp scan/accumulate compute (the 9 call0 edges, out-of-carve)
  -> writes out[i] = the running accumulator at each step.
  The "Cache" accumulator persists across calls per accum_cmd (Zero/Load seed, Accumulate carry).
```

`opcode value` = HEADER (HIGH). The `kernel_info → funcVA` edge = the registration stub (HIGH). The
`opcode → descriptor` literal is out-of-carve (LOW). `[funcVA+worker+stub HIGH; descriptor LOW]`

---

## 7. The shared `alu_op.cpp` compute + the per-element trace

The per-element fold is the **same** `alu_op.cpp` dispatcher as the base [Tensor-Scalar op](tensor-scalar.md).
The carved MARIANA `NX_DVE` DEBUG DRAM embeds (xtensa-elf-strings, re-verified in-task):

```text
@DRAM 0x2b94: "S: OP=%x R[%d] = OP(R[%d], imm)"      <- the per-element scalar fold (op0 with scalar0)
@DRAM 0x2bb5: "S: OP=%x R[%d] = OP(R[%d], R[%d])"    <- the register-register form (the op1 accumulate combine)
@DRAM 0x2caa: "…/decode/alu_op.cpp:196 0 && \"not supported op\""
@DRAM 0x2cf7: "…/decode/alu_op.cpp:141 0 && \"not supported op\""
@DRAM 0x2d44: "…/decode/alu_op.cpp:231 0 && \"not supported dtype\""
@DRAM 0x2d94: "…/decode/alu_op.cpp:220 0 && \"not supported op\""
@DRAM 0x2c73: "…/decode/alu_op.cpp:262 0"
```

The **two** trace forms are the byte-level proof of the `op0`/`op1` split: `OP(R[d], imm)` = the per-element
`op0(in, scalar0)`; `OP(R[d], R[d'])` = the loop-carried `op1(acc, tmp)` register-register accumulate. The
`"not supported op"`/`"not supported dtype"` asserts are the **same** default-arm switch the base Tensor-Scalar
and Tensor-Tensor compute pin — CacheCumulative funnels into the **same** per-lane AluOp dispatcher.
`[HIGH/OBSERVED strings — re-verified the byte-offsets in-task]`

---

## 8. The dtype matrix — cache-reduce-specific (FP-hub; NO int32/uint32)

`tensor_scalar_cache_reduce_valid_types` (`s3d3_ts.h:390–394`) — read verbatim:

```c
// fn tensor_scalar_cache_reduce_valid_types(i: Inst) -> bool {
//       is_valid_fp_dtype_datapath(i.s3d3_ts.in_dtype, DtypeAllowFP32R::False)   // {fp8_e3,fp8_e4,fp8_e5, fp16, bf16, fp32}
//    || (   is_valid_int_dtype_datapath(i.s3d3_ts.in_dtype)                      // {int8,int16,int32,uint8,uint16,uint32}
//        && (i.s3d3_ts.in_dtype != Dtype::UINT32)                               //   MINUS uint32
//        && (i.s3d3_ts.in_dtype != Dtype::INT32))                              //   MINUS int32
// }
```

The two datapath predicates (`common.h:3003`, `:3023`, verbatim):

- `is_valid_int_dtype_datapath` = `{INT8(0x2), INT16(0x4), INT32(0x8), UINT8(0x3), UINT16(0x5), UINT32(0x9)}`.
- `is_valid_fp_dtype_datapath(dtype, AllowFP32R)` = `{FP8_EXP3(0xD), FP8_EXP4(0xE), FP8_EXP5(0xF), FP16(0x7),
  BFLOAT16(0x6), FP32(0xA)}` (+ `FP32R(0xB)` only when `AllowFP32R::True`).

| family | accepted `in_dtype` | rejected `in_dtype` | Tag |
|--------|---------------------|---------------------|-----|
| **CacheCumulative / CacheReduce** | `{fp8_e3, fp8_e4, fp8_e5, fp16, bf16, fp32, int8, int16, uint8, uint16}` | **`int32`, `uint32`** (+ `fp32r`, `int64`, `uint64`) | HIGH/OBSERVED |

`out_dtype` admits `FP32R` (`is_valid_dtype(out_dtype, AllowFP32R::True)`, §4f); `in_dtype` bars it
(`AllowFP32R::False` in both the datapath predicate and the trailing `is_valid_dtype`). Channels
`num_active_channels ∈ 1..128` (`POOLING_NUM_CHANNELS == DVE_NUM_CHANNELS == 128U`). `DTYPE` ordinals
(`common.h:806–821`): `INVALID 0x0, UINT64 0x1, INT8 0x2, UINT8 0x3, INT16 0x4, UINT16 0x5, BF16 0x6, FP16 0x7,
INT32 0x8, UINT32 0x9, FP32 0xA, FP32R 0xB, INT64 0xC, FP8_E3 0xD, FP8_E4 0xE, FP8_E5 0xF`. `[HIGH/OBSERVED]`

> **NOTE — why `int32`/`uint32` are excluded (the accumulator-precision guard).** The running accumulate
> **widens** narrow ints into a wider accumulator (`int8 → int16 → int32`), but a full 32-bit int has **no
> headroom to widen further** in the int datapath — so cache-reduce/cumulative forbids `in = int32/uint32` to
> avoid silent accumulator overflow across the scan. The FP path uses **FP32 as the convert hub** (bf16/fp8
> have no native accumulate; all fp accumulate routes through fp32), so the accumulator is effectively fp32
> (fp inputs) or a widened int (int8/16 inputs). This is a **cache-reduce-specific** rule — the base
> Tensor-Scalar arith op accepts `int32`/`uint32` (it does not accumulate). `[exclusion HIGH/OBSERVED; the
> "fp32 accumulator" framing INFERRED from the FP-hub, MED]`

---

## 9. Per-generation presence

Three independent streams agree: (A) the `common.h` opcode `0xe6` + the `S3D3_TS` struct binding; (B) the
`is_valid_tensor_scalar_cache_cumulative` validator; (C) the `"S: TensorScalarCacheCumulative"` DVE DEBUG
string + the worker funcVA + the registration stub.

| GEN | opcode `0xe6` | `S3D3_TS` (64 B) | `"S:…CacheCumulative"` DVE str | worker funcVA | wired? | Tag |
|-----|:-------------:|:----------------:|:------------------------------:|:-------------:|:------:|-----|
| SUNDA (V1) | defined `// Y` | identical | (no NX_DVE DEBUG image carved) | n/a | **defined** | HIGH/OBSERVED |
| CAYMAN (NC-v3) | defined `// Y` | identical | DRAM `0x1fbf` | `0xa7c8` | **WIRED** | HIGH/OBSERVED |
| MARIANA (NC-v4) | defined `// Y` | identical | DRAM `0x20a0` | `0xacbc` | **WIRED** | HIGH/OBSERVED |
| MARIANA_PLUS | (mariana hdr) | identical | (corroborated, +build delta) | (stable) | **WIRED** | HIGH string; body INFERRED |
| MAVERICK (NC-v5) | defined `// Y` | identical | DRAM `0x20cf` | `0xa940` | **WIRED** | header-OBSERVED → interior INFERRED |

The opcode value `0xe6` and the `S3D3_TS` struct are **byte-identical** on all four gens. `CacheCumulative` is a
**core** maintained `// Y` op (not a deprecated stub like the [PtrMulti](ts-ptrmulti.md)/[ImmLd](ts-immld.md)
`// n` siblings): the `"S: TensorScalarCacheCumulative"` worker + its registration stub are present on every
DVE-equipped gen (CAYMAN/MARIANA/MAVERICK). `[HIGH/OBSERVED]`

> **MAVERICK (NC-v5) — header-OBSERVED → INFERRED interior.** The MAVERICK self-name (DRAM `0x20cf`), the
> MAVERICK `s3d3_ts.h` validators, and the worker presence (`0xa940`) are observed; the MAVERICK worker
> **body** was not fully byte-walked (FLIX-desync'd). Assumed identical-family by the byte-stable MARIANA
> decode + the matching MAVERICK string. MAVERICK inherits the same NC-v5 relaxations the
> [Tensor-Scalar §6c/§10](tensor-scalar.md) notes — `has_valid_active_channel_range_with_tile` and the
> `is_valid_int_aluop_dve` arm — on the **shared** `S3D3_TS` validity functions (these are
> family-wide header deltas, not specific to `CacheCumulative`). **The MAVERICK interior is INFERRED.**

---

## 10. The extended TensorScalar\* family — what CacheCumulative opens

`CacheCumulative` opens the **extended** TensorScalar\* sub-family. The family is **scattered** across the
opcode space (not contiguous) and fans across **four** structs:

| op | opcode | struct | maint | relation | page |
|----|--------|--------|-------|----------|------|
| **CacheCumulative** | `0xe6` | `S3D3_TS` | `// Y` | **this page** — the scan | this page |
| **CacheReduce** | `0x9a` | `S3D3_TS` | `// Y` | the **exact twin** — same struct, same validity fns, COLLAPSE not SCAN | [ts-cache-reduce.md](ts-cache-reduce.md) |
| Select | `0x98` | `S3D3_TS_SELECT` | `// Y` | 64-B sibling struct, predicate-driven blend datapath | [ts-select.md](ts-select.md) |
| ImmLd Arith/Bitvec | `0x70`/`0x71` | `S2_BN` (shared w/ BatchNormParamLoad) | `// n` | the scalar-immediate-LOAD form on the BN-param struct | [ts-immld.md](ts-immld.md) |
| PtrMulti Arith/Bitvec | `0x4F`/`0x5F` (+Dual) | `S4D4_TSM` | `// n` | the multi-pointer scalar form (legacy of the deprecated PTR ops) | [ts-ptrmulti.md](ts-ptrmulti.md) |

`CacheReduce` (`0x9a`) is the **exact twin** of this op — same 64-B struct, **same** validity functions
(`tensor_scalar_cache_reduce_valid_ops` / `_valid_types` / `_immediates_check` / `_reverse_chk` /
`_accum_cmd`), same `op0`(general-arith) + `op1`(cache-reduce-op) split, same `ACCUM_CMD` cache, same imm0-scalar
/ imm1-seed, same `None`-reverse, same dtype rule — differing **only** in the opcode predicate (`0x9a` vs
`0xe6`) and in COLLAPSE (writes final acc) vs SCAN (writes running acc). Decoding `CacheCumulative` decodes
`CacheReduce` to ~90% by construction. The `// n` ImmLd/PtrMulti ops read "ucode/kaenadve exists, not
maintained/used"; Select/CacheReduce/CacheCumulative are `// Y`. The adjacent
[`TensorTensorScan`](tensor-tensor-scan.md) `0xe5` (`S2S2D2_STT`) and `TensorCumulative` `0x4E`/`0x5E`
(`S4D4_TR`) are **distinct** scan ops on **different** structs (§3a). `[HIGH/OBSERVED — instruction_mapping.json
struct2opcode + common.h maint flags + the six DVE self-name strings]`

---

## 11. Reimplementation checklist

A reimplementer building a Vision-Q7-compatible `TensorScalarCacheCumulative`:

1. **Decode** `0xe6` against the **64-byte** `S3D3_TS` struct (§4) — **the same struct** as the base
   [Tensor-Scalar](tensor-scalar.md) op; only the engaged fields differ.
2. **Validate** with `is_valid_tensor_scalar_cache_cumulative` (§4f): `accumulator_cmd ∈ {ZeroAccum(3),
   Accum(2), LoadAccum(4)}` (NOT `Idle`); `op0 = is_general_arith_op`; `op1 ∈ {Add,Subtract,Mult,Max,Min}`;
   `reverse_operands == None`; `imm1 = 0` unless `LoadAccumulate`; `in_dtype` in the fp datapath OR
   `{int8,int16,uint8,uint16}` (NOT `int32`/`uint32`, NOT `FP32R`); `out_dtype` admits `FP32R`;
   `same_element_count(src, dst)`; `reserved0 == 0`; channels `1..128`; src/dst valid in PSUM **and** SBUF.
3. **Seed the cache** per `accum_cmd`: `ZeroAccumulate` → seed to the `op1`-identity and scan fresh;
   `LoadAccumulate` → seed from `imm1`; `Accumulate` → **continue the accumulator a prior instruction left**
   (the cross-tile carry — preserve the per-lane accumulator register between SEQ instructions, §4c GOTCHA).
4. **Source the scalar** `imm0` per `imm0_src ∈ {Inst0, Ptr1, RegPtr2}` (the `IMM_SRC`, `Instruction=0` enum —
   **not** the swapped `IMM_SRC_N`, §4e GOTCHA). `RegPtr` resolves through the reg-pointer fetch sub-handler.
5. **Compute** the inclusive prefix scan `out[i] = ((seed) op1 (in[0] op0 s0)) op1 … op1 (in[i] op0 s0)`:
   the scalar is **splatted** by `ivp_rep*`; the per-element fold `(in op0 scalar0)` is the shared `alu_op.cpp`
   datapath; the loop-carried accumulate is the **rotate ⊕ combine** SIMD-scan recurrence (`ivp_rotrn_2x32`
   bundled with `ivp_bmaxn_2x32`/`ivp_mulpn16xr16`/`ivp_addn_2x32t`, §5). Emit the running accumulator
   **at each element** (CacheReduce would emit once).

---

## 12. Honesty ledger

**HIGH / OBSERVED** (header read / disasm / byte read / symtab read / compile-output):

- Opcode `TENSOR_SCALAR_CACHE_CUMULATIVE = 0xe6` (`common.h:306`), `// Y` maintained, **byte-identical** on
  sunda/cayman/mariana/maverick (4-gen diff); the whole extended family opcode block (§3/§10) + maint flags
  verbatim; the disambiguation of `TensorCumulative 0x4E/0x5E` and `TensorTensorScan 0xe5` as distinct ops.
- `S3D3_TS` binds `CacheCumulative` + `CacheReduce` (`instruction_mapping.json struct2opcode`, 8 opcodes); the
  64-B struct (`accum_cmd@12, src@16, in_dtype@32, out_dtype@33, num_chan@34, imm0_src@35, op0@36, op1@37,
  reverse@38, imm1_src@39, imm0@40, imm1@44, dst@48`), `ISA_STATIC_ASSERT == 64`, byte-identical to
  [tensor-scalar.md §4].
- `is_valid_tensor_scalar_cache_cumulative` (`s3d3_ts.h:132`) is **byte-identical** to
  `is_valid_tensor_scalar_cache_reduce` (`:110`) save the opcode predicate — read both verbatim.
- The "Cache" = `accum_cmd ∈ {ZeroAccum, Accum, LoadAccum}` (`has_valid_tensor_scalar_cache_reduce_accum_cmd`,
  `:214`) vs base-TS `Idle`-only; `ACCUM_CMD` enum `{Idle0, Zero1, Accumulate2, ZeroAccumulate3,
  LoadAccumulate4}` (`common.h:870`).
- The `op0`(`is_general_arith_op`) + `op1`(`is_valid_cache_reduce_op ∈ {Add,Subtract,Mult,Max,Min}`) split
  (`tensor_scalar_cache_reduce_valid_ops` + `is_valid_cache_reduce_op`, `:282`/`:289`) — both verbatim.
- `reverse_operands` forced `None` (`tensor_scalar_cache_reduce_reverse_chk`, `:405`); `imm0` per-imm
  Inst/Ptr/RegPtr, `imm1` = the LoadAccumulate seed (`tensor_scalar_cache_reduce_immediates_check`, `:312`);
  `same_element_count(src,dst)` (`tensor_scalar_tensor_chk`); the dtype rule (fp datapath OR int8/16/u8/16,
  NO i32/u32 — `tensor_scalar_cache_reduce_valid_types`, `:390`, + the datapath predicates `common.h:3003`/
  `:3023`) — all verbatim.
- The DVE self-name `"S: TensorScalarCacheCumulative"` on CAY `0x1fbf` / MAR `0x20a0` / MAV `0x20cf` (+ the
  whole family's self-names §6a); the worker funcVAs (CAY `0xa7c8` / MAR `0xacbc` / MAV `0xa940`, `entry a1,80`
  + the `const16 a10,0x20a0 ; call8 0x188a4` LOG loader — re-disassembled in-task); the MARIANA registration
  stub `@0x21d4` (`const16 a2,0xacbc ; s32i [a1+12] ; l32r 0xfffc834c/0xfffc38cc ; call8 0x9920`).
- The worker body shape (log → config → FLIX window program → `alu_op.cpp` compute); the **9-vs-4** `call0`
  compute-edge count CacheCumulative-vs-CacheReduce (direct count, re-verified: `0xfffbbe10` +
  `0xfffbde3c..0xfffbdf44`).
- The `alu_op.cpp` per-element traces `"OP=%x R[%d]=OP(R[%d],imm)"` (op0 fold) + `"…OP(R[%d],R[%d])"` (op1
  accumulate) + the `"not supported op/dtype"` asserts (DRAM strings `@0x2b94/0x2bb5/0x2caa…`, byte-offsets
  re-verified).
- The PERF rotate⊕combine scan bundles (`ivp_rotrn_2x32`/`ivp_rotri2nx8` ⊕ `ivp_bmaxn_2x32`/`ivp_mulpn16xr16`/
  `ivp_babssub2nx8` at PERF `0x4916`/`0x29c5`/`0x939d`/`0xdf03`, re-disassembled in-task) + the scalar splat
  (`ivp_rep*`, 28 sites) + the lane-fold reduce (`ivp_rb*`, 15 sites) — counts re-grounded with `rg -c`.
- Per-gen: opcode + struct all four; WIRED (string + worker + stub) on CAY/MAR/MAV; container sha256
  `b7c67e89…` (10,276,288 B, == [tensor-scalar.md]).

**MED / INFERRED:**

- The exact **inclusive-vs-exclusive** scan boundary + the per-step `out[i]` emit index: INFERRED from
  `{accum seed + op0/op1 split + same_element_count + the "Cumulative" name + the PERF rotate⊕combine
  recurrence}`; the per-element loop is the out-of-carve `alu_op.cpp` body. The reduce-COLLAPSE vs
  cumulative-SCAN distinction itself is **HIGH** (opcode name + the worker body-size / 9-vs-4 compute-edge
  delta), but the exact emit index is MED.
- The bind of the PERF rotate⊕combine bundles to the `0xe6` worker **specifically** (PERF strips self-names) —
  family-level scan vocabulary, MED for the per-worker binding.
- The "fp32 accumulator" framing (from the FP-hub) and the `op1` → single-IVP-combine binding — structural, MED.
- The cross-tile `Accumulate(2)` carry requiring a preserved accumulator register between instructions —
  INFERRED from the accum_cmd semantics + the cross-call worker shape, MED.
- The MAVERICK worker **body** (header + string + stub OBSERVED; interior FLIX-desync'd, INFERRED).

**CARRIED / LOW:**

- The `opcode 0xe6 → funcVA` descriptor **bytes** (the stub `l32r` literals `0xfffc834c`/`0xfffc38cc` resolve
  out-of-carve; runtime-bound; same [tensor-scalar.md §9a] limitation).
- The `alu_op.cpp` scan/accumulate compute **body** (the 9 `call0` negative-literal targets
  `0xfffbbe10`/`0xfffbde3c..0xfffbdf44` resolve out-of-carve; the loop is reached but not byte-walked
  end-to-end through the FLIX desync).

**FLIX-DESYNC FLAG.** The DEBUG worker bodies desync under stock objdump on the recurring `.byte 0x2f/0x8f/0x4f/
0x5f/0x6f` literal-pool lead bytes (the documented SX-FW-00 limit). The mis-decoded `.byte`/spurious bundles
are **not** reported as real instructions; the entry prologue + LOG loader + `call0` edges + the `s16i`
window-program shape **are** byte-clean. The cleaner **PERF** image is the source for the rotate⊕combine scan
datapath vocabulary.

All facts read as derived from shipped-artifact static analysis (the disassembly, the `.rodata`/DRAM bytes, the
shipped C headers, the symtab, and `instruction_mapping.json`) — lawful interoperability RE.

---

## Cross-references

- [Tensor-Scalar (the base op + the S3D3_TS struct)](tensor-scalar.md) — the `0x43`/`0x53`/`0x44`/`0x54` base
  op this page shares the **64-B `S3D3_TS` struct**, the splat-then-AluOp datapath, the `IMM_SRC` scalar
  source, and the `alu_op.cpp` compute with. The base op forces `accumulator_cmd == Idle`; this op engages it.
- [TensorScalarCacheReduce (the COLLAPSE twin)](ts-cache-reduce.md) — the `0x9a` exact twin: same struct, same
  validity functions, writes the **final** accumulator (collapse) instead of the **running** accumulator
  (scan). The byte signature of the split is the 9-vs-4 `call0` compute-edge delta (§6d).
- [TensorTensorScan (the two-tensor scan)](tensor-tensor-scan.md) — the adjacent `0xe5` scan op on the
  `S2S2D2_STT` struct (no scalar fold) — a **distinct** op, not part of this family (§3a).
- [TensorScalarSelect](ts-select.md) / [TensorScalarImmLoad](ts-immld.md) / [TensorScalarPtrMulti](ts-ptrmulti.md)
  — the further extended-family ops this page opens (§10): `S3D3_TS_SELECT`, `S2_BN`, `S4D4_TSM`.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `HIGH/MED/LOW ×
  OBSERVED/INFERRED/CARRIED` vocabulary, the FLIX-desync wall, and the out-of-carve descriptor wall every claim
  here carries.
