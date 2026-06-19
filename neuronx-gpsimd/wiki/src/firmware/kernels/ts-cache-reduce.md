# TensorScalarCacheReduce — opcode `0x9a`, the COLLAPSE/reduce twin

> **Scope.** `TensorScalarCacheReduce` (opcode `0x9a`) is the **reduce/COLLAPSE** member of the
> TensorScalar* family — the operation that folds a tensor stream into a **persistent accumulator** under a
> per-element scalar combine and emits the **FINAL** accumulated value (so it produces **fewer outputs than
> inputs**). It is the exact twin of [`TensorScalarCacheCumulative`](ts-cache-cumulative.md) (`0xe6`), the
> **scan** twin that emits the running partial at every step. The two share the **same 64-byte
> [`NEURON_ISA_TPB_S3D3_TS`](tensor-scalar.md#4-the-operand-struct--neuron_isa_tpb_s3d3_ts_struct-64-b)
> operand struct**, the **same** validity machinery, the **same** `alu_op.cpp` per-lane compute, and the
> **same** DVE dispatch surface — differing in exactly **one validity line** (the opcode predicate) and in
> the worker body that streams (scan) vs collapses (reduce).
>
> This page pins the opcode and the `S3D3_TS` binding; establishes the `accumulator_cmd@12` enum that drives
> the **cache** (`{ZeroAccumulate, Accumulate, LoadAccumulate}`, where plain Tensor-Scalar forces
> `Idle`); decodes the **two-AluOp split** unique to the cache pair (`op0` = per-element scalar fold,
> `op1` ∈ `{Add, Subtract, Mult, Max, Min}` = the loop-carried combine); resolves the **scan-vs-reduce
> output-cardinality** question (the descriptor sizes the dst `== src` element count for **both**; the
> collapse is realised purely in the worker body); tabulates the cache-reduce-specific dtype matrix (the
> int32/uint32 exclusion); decodes the DVE dispatch chain instruction-exact; and lists per-gen presence.
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md).

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | `TensorScalarCacheReduce` = **opcode `0x9a`** (154), maintained `// Y`, byte-identical on **all four** gens; binds the **64-B** `NEURON_ISA_TPB_S3D3_TS_STRUCT`. | `common.h` sunda L236 / cayman L233 / mariana L238 / maverick L241 `= 0x9a // Y`; `instruction_mapping.json` `struct2opcode` | HIGH/OBSERVED |
| 2 | It is the **COLLAPSE/reduce twin** of [`CacheCumulative`](ts-cache-cumulative.md) (`0xe6`, scan): the two `is_valid_*` bodies are **character-identical except exactly one line** — `has_tensor_cache_reduce_opcode` (`0x9a`) vs `has_tensor_cache_cumulative_opcode` (`0xe6`). | `s3d3_ts.h:110–130` vs `:132–152` diff | HIGH/OBSERVED |
| 3 | The **"cache"** = the `S3D3_TS` `accumulator_cmd@12` field, **FORCED** ∈ `{ZeroAccumulate=3, Accumulate=2, LoadAccumulate=4}` by `has_valid_tensor_scalar_cache_reduce_accum_cmd` — a **persistent loop-carried / cross-tile** accumulator. Plain Tensor-Scalar forces `Idle=0`. | `s3d3_ts.h:214–218`; `ACCUM_CMD` enum `common.h:870` | HIGH/OBSERVED |
| 4 | The **two-AluOp split** (unique to the cache pair): `op0@36` = `is_general_arith_op` (per-element scalar fold `in[i] op0 scalar0`); `op1@37` = `is_valid_cache_reduce_op` ∈ `{Add, Subtract, Mult, Max, Min}` (the loop-carried combine). Recurrence: `acc = acc op1 (in[i] op0 scalar0)`. | `tensor_scalar_cache_reduce_valid_ops` `s3d3_ts.h:282–287` + `is_valid_cache_reduce_op:289–295` | HIGH/OBSERVED |
| 5 | The **collapse vs scan distinction is NOT in the struct/validity layer** — `tensor_scalar_tensor_chk` sizes `dst.element_count == src.element_count` for **both** ops. The reduce is realised purely in the **opcode-predicate-selected worker body**: the CacheReduce worker (`0xa7b0`, 1124 B) has a **shorter** `alu_op.cpp` compute chain (3 contiguous edges) than CacheCumulative (`0xacbc`, 1372 B, 8 edges). | `tensor_scalar_tensor_chk:410–415` (`same_element_count`); MARIANA NX_DVE IRAM call-edge census | `same_element_count` HIGH/OBSERVED; the body-delta count HIGH/OBSERVED; the reduce-emits-final-only INFERRED |
| 6 | The scalar source = `imm0` (per-imm `IMM_SRC {Inst0/Ptr1/RegPtr2}`); `imm1` = the **seed slot**, gated on `accum_cmd == LoadAccumulate` (else `imm1_src == 0 && imm1 == 0`). `reverse_operands` is **FORCED `None`**. | `tensor_scalar_cache_reduce_immediates_check:312–328`; `tensor_scalar_cache_reduce_reverse_chk:405–408` | HIGH/OBSERVED |
| 7 | Dtype = `tensor_scalar_cache_reduce_valid_types`: fp datapath `{fp8_e3/e4/e5, fp16, bf16, fp32}` **OR** int datapath **minus `{INT32, UINT32}`**. `in_dtype` bars `FP32R`; `out_dtype` admits it. The int32/uint32 exclusion is the **accumulator-precision guard**. | `s3d3_ts.h:390–395` + `is_valid_fp/int_dtype_datapath` `common.h:3003/3023` | HIGH/OBSERVED |
| 8 | **WIRED on every DVE gen** — CAYMAN, MARIANA, (MARIANA_PLUS), MAVERICK — via the DVE **`"S: TensorScalarCacheReduce"`** self-name worker (distinct from `"S: Tensor-Reduce"` and from the CacheCumulative twin). A core maintained op, not a deprecated stub. | per-gen DVE strings + worker funcVAs + registration stubs | HIGH/OBSERVED; MAVERICK interior INFERRED |

---

## 2. Provenance / carve anchors

All device-firmware facts derive from static analysis of the shipped device-firmware blob carved out of the
host customop library (disassembled with the Cadence Xtensa toolchain that ships *inside* the gpsimd-tools
package, `XTENSA_CORE=ncore2gp`, Vision-Q7 FLIX/VLIW) plus the shipped host customop-lib **ISA C headers**.
No source was consulted.

| Artifact | Value |
|----------|-------|
| Container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Disassembler | `gpsimd_tools/…/bin/xtensa-elf-objdump` (Binutils 2.34.20200201, Xtensa Tools 14.09), `XTENSA_CORE=ncore2gp` |
| MARIANA `NX_DVE` DEBUG IRAM | the CacheReduce worker `0xa7b0`; the registration stub `0x21b8` (`.rodata` VA == file offset) |
| MARIANA `NX_DVE` DEBUG DRAM | the `"S: TensorScalarCacheReduce"` self-name `0x202d`; the `alu_op.cpp` trace strings `0x2b94`/`0x2bb5` |
| CAYMAN `NX_DVE` DEBUG | self-name `0x1f4c`; worker `~0xa2e0` (loader `0xa2e9`) |
| MAVERICK `NX_DVE` DEBUG | self-name `0x205c`; worker `0xa438` |

The authoritative struct/enum/validator source is the shipped public ISA headers (same package):

- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_ts.h` — the `S3D3_TS` struct + the
  `is_valid_tensor_scalar_cache_reduce` / `_cache_cumulative` validators + the cache-reduce op/imm/dtype/
  reverse/accum-cmd checks.
- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h` — the opcode enum; `ACCUM_CMD`; `ALU_OP`;
  `IMM_SRC`; `DTYPE`; the `is_valid_fp/int_dtype_datapath` helpers; `is_general_arith_op`;
  `POOLING_NUM_CHANNELS == DVE_NUM_CHANNELS == 128U`.
- `…/neuron_<gen>_arch_isa/tpb/instruction_mapping.json` — the `struct2opcode` binding.

The `S3D3_TS` struct is **byte-identical** (`sizeof 64`, same field offsets) on sunda/cayman/mariana/maverick
(compile-verified). `[HIGH/OBSERVED]`

---

## 3. The opcode — `0x9a`, all four gens, `S3D3_TS`-bound

Read verbatim from `aws_neuron_isa_tpb_common.h` (the value byte `0x9a` is identical on every gen; only the
header line number differs):

```c
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_REDUCE     = 0x9a,   // Y     this page  (sunda L236 / cayman L233 / mariana L238 / maverick L241)
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_CUMULATIVE = 0xe6,   // Y     the scan twin (sunda L293 / cayman L296 / mariana L306 / maverick L312)  -> ts-cache-cumulative.md
```

`0x9a` is maintained (`// Y`) on all four — a **core** op, not a deprecated stub (contrast the deprecated
`0x44`/`0x54` TensorScalarPtr). `[HIGH/OBSERVED]`

> **NOTE — `0x9a` is a firmware kernel-lane opcode, not an Xtensa ISA mnemonic.** As with the rest of the
> TensorScalar family (see [Tensor-Scalar §3 NOTE](tensor-scalar.md#3-the-opcodes--tensorscalar--tensorscalarptr-vs-tensor-tensor)),
> the ~140-entry `NEURON_ISA_TPB_OPCODE_*` enum is the *firmware kernel-lane* axis; the `ivp_*` roster that
> the worker *emits* is the *Xtensa ISA* axis. The `0x9a` opcode dispatches a firmware handler; that handler
> emits the FLIX bundles. `[HIGH/OBSERVED]`

### 3a. The struct→opcode binding (`instruction_mapping.json`)

The `struct2opcode` table binds **eight** opcodes to `NEURON_ISA_TPB_S3D3_TS_STRUCT` (mariana, verbatim
`jq`-read) — `CACHE_REDUCE` is a **first-class** member, alongside its twin and the base TensorScalar:

```json
"NEURON_ISA_TPB_S3D3_TS_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_ARITH_OP",          // 0x43  tensor-scalar.md
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_BITVEC_OP",         // 0x53  tensor-scalar.md
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_ARITH_OP",      // 0x44  tensor-scalar.md (deprecated)
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_BITVEC_OP",     // 0x54  tensor-scalar.md (deprecated)
    "NEURON_ISA_TPB_OPCODE_TRANSPOSE_TENSOR_SCALAR_ARITH_OP",// 0x93  sibling (transpose-fused)
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_REDUCE",      // 0x9a  THIS PAGE
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_CUMULATIVE",  // 0xe6  ts-cache-cumulative.md (the scan twin)
    "NEURON_ISA_TPB_OPCODE_EXPONENTIAL"                      // 0x30  exponential.md
]
```

> **NOTE — `0x9a` is NOT in the Q7_POOL opcode matrix, and that is correct.** `CacheReduce` is a **DVE**
> op (the DVE SEQ-ASCII `"S:"` worker surface), so it does **not** appear in the Q7_POOL `kernel_info_table`
> the way the POOL `0x7c`/`0x7d` CrossLaneReduce ([Tensor-Reduce](tensor-reduce.md)) does. A reimplementer
> indexing the POOL kernel table will not find `0x9a` — it lives on the DVE dispatch surface (§6). The
> separate `0x98` `TensorScalarSelect` binds a **different** struct (`S3D3_TS_SELECT`), not this one.
> `[HIGH/OBSERVED]`

---

## 4. The operand struct — `NEURON_ISA_TPB_S3D3_TS` (64 B), with the cache-reduce field roles

`s3d3_ts.h:28`, `ISA_STATIC_ASSERT(sizeof == 64)` (all gens, compile-verified byte-identical). The layout is
**identical** to plain [Tensor-Scalar](tensor-scalar.md#4-the-operand-struct--neuron_isa_tpb_s3d3_ts_struct-64-b)
— the struct carries **no** reduce-vs-scan discriminator. The byte offsets are read verbatim from the header
(trailing `// size (range)` comments); the **role** column re-annotates each field for cache-reduce:

| off | size | field | type | role for CacheReduce (`0x9a`) |
|----:|-----:|-------|------|-------------------------------|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `opcode = 0x9a`; `inst_word_len`; (NC-v5) `inst_flags` (tile id) |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync |
| 12 | 1 | `accumulator_cmd` | `ACCUM_CMD` | **THE "CACHE"** — `{ZeroAccumulate3, Accumulate2, LoadAccumulate4}` (§4c) |
| 13 | 3 | `reserved0[3]` | `uint8` | **must be 0** (`s3d3_ts_reserved_zero`) |
| 16 | 16 | `src_mem_pattern` | `MEM_PATTERN3D` | **the INPUT tensor `in`** (the stream the accumulator folds along) |
| 32 | 1 | `in_dtype` | `DTYPE` | input dtype (**no `FP32R`, no `INT32/UINT32`**, §5) |
| 33 | 1 | `out_dtype` | `DTYPE` | output dtype (**`FP32R` allowed**) |
| 34 | 1 | `num_active_channels` | `uint8` | partition count `1..128` — **one accumulator per channel** |
| 35 | 1 | `imm0_src` | `IMM_SRC` | **scalar0 SOURCE** `{Inst0/Ptr1/RegPtr2}` |
| 36 | 1 | `op0` | `ALU_OP` | **per-element scalar-fold** AluOp — `is_general_arith_op` |
| 37 | 1 | `op1` | `ALU_OP` | **loop-carried combine** AluOp — `is_valid_cache_reduce_op` ∈ `{Add,Sub,Mult,Max,Min}` |
| 38 | 1 | `reverse_operands` | `TENS_SCALAR_REV_OPS` | **FORCED `None=0`** (§4d) |
| 39 | 1 | `imm1_src` | `IMM_SRC` | **seed SOURCE** — meaningful only when `accum_cmd == LoadAccumulate` |
| 40 | 4 | `imm0` | `IMM_VAL_INST_FIELD` (union) | **scalar0 VALUE** (the per-element fold operand) |
| 44 | 4 | `imm1` | `IMM_VAL_INST_FIELD` (union) | **seed VALUE** — LoadAccumulate init; else **must be 0** |
| 48 | 16 | `dst_mem_pattern` | `MEM_PATTERN3D` | **the OUTPUT tensor** (`same_element_count == src`, §4e) |

`[HIGH/OBSERVED — header text + `ISA_STATIC_ASSERT == 64`; the per-field role column re-annotated from the
cache-reduce validators]`

`NEURON_ISA_TPB_MEM_PATTERN3D` (`common.h`) = `union { TENSOR3D t; INDIRECT16B i }` — a 3-D strided access
pattern (16 B) or its gather/scatter indirect arm. Both `src` and `dst` are `AllowedInPSUM::True &&
AllowedInSBUF::True`, so a CacheReduce can fold the matmul **PSUM** output directly. On SUNDA/CAYMAN the
fields are typed as the bare `NEURON_ISA_TPB_TENSOR3D` (no union arm) — offsets and `sizeof 64` byte-identical
(the same and only struct-text divergence [Tensor-Scalar §4 NOTE](tensor-scalar.md#4-the-operand-struct--neuron_isa_tpb_s3d3_ts_struct-64-b)
records). `[HIGH/OBSERVED]`

### 4a. The recurrence — two AluOps + a persistent accumulator

CacheReduce computes a per-element scalar **fold** followed by a loop-carried **combine** into a persistent
accumulator, then emits only the final accumulator. The recurrence (the central semantic; see §4b/§4c for the
validators that pin each clause):

```c
// TensorScalarCacheReduce per-channel recurrence. acc is the persistent accumulator (the "cache").
//   op0 : per-element scalar fold (s3d3_ts.op0, is_general_arith_op)         — combines in[i] with scalar0
//   op1 : loop-carried combine    (s3d3_ts.op1, is_valid_cache_reduce_op)    — folds into acc
//   alu(): the shared alu_op.cpp per-lane ALU dispatcher (the "S: OP=%x R[%d] = OP(R[%d], imm)" path, §7).
// reverse_operands is FORCED None, so op0 is always (tensor on the left, scalar0 on the right).
static elem_t ts_cache_reduce_channel(const elem_t *in, size_t n,
                                      scalar_t scalar0,                 // imm0
                                      alu_op_t op0, alu_op_t op1,
                                      accum_cmd_t accum_cmd, scalar_t seed) {  // imm1 when LoadAccumulate
    acc_t acc;
    switch (accum_cmd) {                                                // accumulator_cmd@12 (§4c)
        case ACCUM_CMD_ZERO_ACCUMULATE: acc = op1_identity(op1); break; // seed fresh to op1's identity
        case ACCUM_CMD_LOAD_ACCUMULATE: acc = (acc_t)seed;       break; // seed from imm1
        case ACCUM_CMD_ACCUMULATE:      acc = persistent_acc;    break; // CONTINUE the carried accumulator
        default: /* Idle/Zero rejected by has_valid_tensor_scalar_cache_reduce_accum_cmd */ ;
    }
    for (size_t i = 0; i < n; ++i) {                                    // fold along the src inner stream axis
        elem_t tmp = alu(op0, /*lhs=*/in[i], /*rhs=*/scalar0);          // tmp = in[i] op0 scalar0  (the fold)
        acc        = alu(op1, /*lhs=*/acc,   /*rhs=*/tmp);              // acc = acc op1 tmp        (the combine)
    }
    persistent_acc = acc;                                              // the cache carries across calls
    return acc;                                                         // *** CacheReduce emits ONLY the final acc ***
    // CacheCumulative (the scan twin) would instead emit `acc` at EACH i — see ts-cache-cumulative.md.
}
```

The single divergence from the scan twin is the **emit point**: CacheReduce writes the accumulator **once**
(after the fold, per channel); CacheCumulative writes it **every step**. `op1_identity` is the additive/
multiplicative/min/max identity for the chosen reduction op (`0`/`1`/`+∞`/`−∞`). `[recurrence HIGH from the
validators; the per-channel emit-once vs per-step INFERRED from the opcode name + the worker-body delta §6d]`

### 4b. The validation contract — `is_valid_tensor_scalar_cache_reduce` (verbatim)

Read verbatim from `s3d3_ts.h:110–130`. The companion `is_valid_tensor_scalar_cache_cumulative` (`:132–152`)
is **character-identical** except the single opcode-predicate line:

```c
// fn is_valid_tensor_scalar_cache_reduce(i: Inst) -> bool {
//       has_valid_neuron_header(i)
//    && has_valid_neuron_events(i)
//    && has_tensor_cache_reduce_opcode(i)              // opcode == 0x9a   <-- THE ONLY LINE THAT DIFFERS
//                                                      //   (cumulative: has_tensor_cache_cumulative_opcode, 0xe6)
//    && tensor_scalar_cache_reduce_valid_ops(i)        // §4f — op0 general-arith, op1 ∈ {Add,Sub,Mult,Max,Min}
//    && tensor_scalar_cache_reduce_valid_types(i)      // §5  — fp datapath OR int (minus i32/u32)
//    && tensor_scalar_cache_reduce_immediates_check(i) // §4g — imm0 {Inst/Ptr/RegPtr}; imm1 gated on LoadAccumulate
//    && tensor_scalar_tensor_chk(i)                    // §4e — src/dst mem3d valid + same_element_count
//    && tensor_scalar_cache_reduce_reverse_chk(i)      // §4d — reverse_operands == None
//    && s3d3_ts_reserved_zero(i)                       // reserved0[3] == 0
//    && has_valid_tensor_scalar_cache_reduce_accum_cmd(i)  // §4c — accum_cmd ∈ {ZeroAcc,Acc,LoadAcc}
//    && is_valid_dtype(i.s3d3_ts.in_dtype,  DtypeAllowFP32R::False)   // input  bars FP32R
//    && is_valid_dtype(i.s3d3_ts.out_dtype, DtypeAllowFP32R::True)    // output admits FP32R
//    && is_valid_aluop(i.s3d3_ts.op0) && is_valid_aluop(i.s3d3_ts.op1)
//    && has_valid_active_channel_range(i.s3d3_ts.num_active_channels, POOLING_NUM_CHANNELS)  // 128
//    && check_m3d_active_channels(src, n) && check_m3d_active_channels(dst, n)
//    && mem3d_valid(src, in_dtype,  WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)
//    && mem3d_valid(dst, out_dtype, WriteTensor::True,  AllowedInPSUM::True, AllowedInSBUF::True)
// }
```

In one sentence: **fold every element of the `src` `MEM_PATTERN3D` (PSUM or SBUF) into the per-channel
persistent accumulator via the two-AluOp recurrence, and write the result into `dst`** — with the cache
command engaged, the op-pair in the cache-reduce accept set, the dtypes valid, the immediates correctly
sourced, and `reverse` forced `None`. The predicate that selects reduce vs scan:

```c
// fn has_tensor_cache_reduce_opcode(i: Inst)     -> bool { i.header.opcode == Opcode::TensorScalarCacheReduce }      // 0x9a
// fn has_tensor_cache_cumulative_opcode(i: Inst) -> bool { i.header.opcode == Opcode::TensorScalarCacheCumulative }  // 0xe6
```

`[HIGH/OBSERVED — both bodies read & diffed verbatim, all four gens]`

> **CORRECTION — the dispatch fork is NOT a struct/validity fork.** It is tempting to expect reduce and scan
> to differ in a struct field or a validity clause. They do **not**: the two `is_valid_*` functions are
> identical except the opcode byte, and they call the **same** `tensor_scalar_cache_reduce_valid_ops` /
> `_valid_types` / `_immediates_check` / `_reverse_chk` / `_accum_cmd` helpers (note the helper names carry
> the `cache_reduce_` prefix and are **shared** by the cumulative validator — confirmed verbatim at
> `s3d3_ts.h:136–142`). The reduce-vs-scan behaviour is a property of the **opcode-selected worker body**
> alone (§6d). A reimplementer must dispatch on the opcode, not on any struct field. `[HIGH/OBSERVED]`

### 4c. The cache — `accumulator_cmd@12` (`has_valid_tensor_scalar_cache_reduce_accum_cmd`)

The `accumulator_cmd@12` field is what makes this a *cache* op. The validator (`s3d3_ts.h:214–218`, verbatim)
restricts it to the three accumulating commands:

```c
// fn has_valid_tensor_scalar_cache_reduce_accum_cmd(i: Inst) -> bool {
//       (i.s3d3_ts.accumulator_cmd == AccumCmd::ZeroAccumulate)   // 3 — seed FRESH to op1's identity
//    || (i.s3d3_ts.accumulator_cmd == AccumCmd::Accumulate)       // 2 — CONTINUE a carried accumulator (cross-call)
//    || (i.s3d3_ts.accumulator_cmd == AccumCmd::LoadAccumulate)   // 4 — seed FROM imm1
// }
```

`ACCUM_CMD` ordinals (`common.h:870`, verbatim): `IDLE=0, ZERO=1, ACCUMULATE=2, ZERO_ACCUMULATE=3,
LOAD_ACCUMULATE=4`. CacheReduce **rejects `Idle(0)` and `Zero(1)`** — it must accumulate. The contrast pins
the design:

| op | `accum_cmd` contract | function |
|----|----------------------|----------|
| plain **Tensor-Scalar** (`0x43`/`0x53`) | **FORCED `Idle=0`** | `has_zero_accum_cmd_field` — pure elementwise, **no** running accumulation |
| **CacheReduce** (`0x9a`) / **CacheCumulative** (`0xe6`) | **∈ `{ZeroAccumulate, Accumulate, LoadAccumulate}`** | `has_valid_tensor_scalar_cache_reduce_accum_cmd` — a **persistent loop-carried** accumulator |
| **Exponential** (`0x30`) | its own gate | `has_valid_exponential_accum_cmd` |

So the accumulator is a **persistent per-channel register the engine carries across invocations**: seed once
(`ZeroAccumulate` to the op1 identity, or `LoadAccumulate` from `imm1`), then chain more tiles with
`Accumulate`. This is the **cross-tile** reduce primitive — fold a tensor larger than one DVE tile by issuing
a `ZeroAccumulate`/`LoadAccumulate` for the first tile and `Accumulate` for each subsequent tile, reading the
final accumulator once at the end. `[HIGH/OBSERVED — validator + enum verbatim]`

### 4d. `reverse_operands` forced `None` (`tensor_scalar_cache_reduce_reverse_chk`)

```c
// fn tensor_scalar_cache_reduce_reverse_chk(i: Inst) -> bool {
//       is_valid_enum(EnumList::TensScalarRevOps, i.s3d3_ts.reverse_operands)
//    && (i.s3d3_ts.reverse_operands == TensScalarRevOps::None)        // FORCED None
// }
```

The per-element fold is **always** `(in[i] op0 scalar0)` — scalar on the right, never reversible. Plain
Tensor-Scalar allows the 4-valued `{None/First/Second/Both}` operand-order flip
([Tensor-Scalar §4a](tensor-scalar.md#4a-the-two-aluop-composition--the-reverse-operands-formula)); the cache
pair gives that up. `[HIGH/OBSERVED — verbatim]`

> **GOTCHA — `op1` does NOT take a scalar.** Unlike plain Tensor-Scalar's two-scalar composition
> `op1(op0(tensor, scalar0), scalar1)`, the cache pair's `op1` is the **loop-carried combine** `acc op1 tmp`
> — it folds the accumulator against the per-element result, **not** against `scalar1`. There is no
> `scalar1` operand in the recurrence at all: the `imm1` slot is repurposed as the **`LoadAccumulate` seed**
> (§4g), never as a second fold scalar. A reimplementer porting from the plain-TS two-scalar mental model
> must drop `scalar1` from the inner loop. `[HIGH/OBSERVED]`

### 4e. The output cardinality — `tensor_scalar_tensor_chk` sizes `dst == src`

The key nuance for "fewer outputs than inputs": **both** CacheReduce and CacheCumulative call the **same**
`tensor_scalar_tensor_chk` (`s3d3_ts.h:410–415`, verbatim), which sizes the dst to **match** the src element
count:

```c
// fn tensor_scalar_tensor_chk(i: Inst) -> bool {
//       mem3d_valid(i.s3d3_ts.src_mem_pattern, in_dtype,  WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)
//    && mem3d_valid(i.s3d3_ts.dst_mem_pattern, out_dtype, WriteTensor::True,  AllowedInPSUM::True, AllowedInSBUF::True)
//    && indirect_dst_size_limit_m3d(i.s3d3_ts.dst_mem_pattern)
//    && same_element_count_m3d(i.s3d3_ts.src_mem_pattern, i.s3d3_ts.dst_mem_pattern)   // dst.count == src.count
// }
```

(On SUNDA/CAYMAN the inner call is `same_element_count_t3d` — a `t3d`-vs-`m3d` **naming** rename only; the
src-count-`==`-dst-count **semantic** is preserved on all four gens, diffed verbatim.) So **at the
struct/validity layer, the CacheReduce dst is sized `== src` element count** — the descriptor does **not**
encode the "fewer outputs" reduce shape.

The collapse is realised **purely by the worker**: it writes the **final accumulated value** (per channel /
per accumulation segment) into the dst, and the un-emitted dst positions are the slots that the *scan* twin
would stream its running partials into — the reduce simply does not stream them. The **reduce axis** is the
**inner stream axis** the accumulator runs along (one accumulator per channel over the `src_mem_pattern` inner
dimension), collapsing it to the final value; the **partition/channel axis** (`num_active_channels`) is
**preserved** — a separate accumulator per channel.

`[`same_element_count` for BOTH HIGH/OBSERVED (verbatim, all gens); that CacheReduce emits only the final
(vs scan's per-step) HIGH from the opcode name + the worker-body compute-edge delta §6d; the exact dst slot
the final lands in is in the out-of-carve `alu_op.cpp` loop — MED]`

> **QUIRK — "fewer outputs than inputs" is a *runtime* property, not a *descriptor* property.** A reader who
> expects a reduce op to declare a smaller dst tensor will be surprised: both the reduce and the scan share a
> full-sized dst (`dst.count == src.count`). The reduce's "collapse" is the worker emitting *one* meaningful
> value (the final accumulator) where the scan emits *n*. If you size your dst buffer for the reduce's single
> output you will **fail validation** (`same_element_count`); you must allocate the full-sized dst and read
> the final accumulator from its last/segment slot. `[HIGH/OBSERVED for the size contract; the slot MED]`

### 4f. The two-op accept set (`tensor_scalar_cache_reduce_valid_ops`, verbatim)

```c
// fn tensor_scalar_cache_reduce_valid_ops(i: Inst) -> bool {
//       (is_valid_enum(EnumList::AluOp, i.s3d3_ts.op0))    // raw byte is a defined ALU_OP member
//    && (is_valid_enum(EnumList::AluOp, i.s3d3_ts.op1))
//    && (is_general_arith_op(i.s3d3_ts.op0))               // op0 = per-element scalar fold
//    && (is_valid_cache_reduce_op(i.s3d3_ts.op1))          // op1 = loop-carried combine
// }
//
// fn is_valid_cache_reduce_op(op: AluOp) -> bool {
//       (op == AluOp::Add) || (op == AluOp::Subtract) || (op == AluOp::Mult)
//    || (op == AluOp::Max) || (op == AluOp::Min)          // the 5 associative reduction operators
// }
```

`is_general_arith_op` (`common.h:1910–1917`, verbatim) = `is_arith_op(op) && op != Divide && op != Pow &&
op != Mod && !is_valid_int_aluop(op) && op != Rsqrt` ⇒ the float general-arith band `{Bypass, Add, Subtract,
Mult, Max, Min, LogicalAnd, LogicalOr, LogicalXor, IsEQ, IsGT, IsGE, IsLE, IsLT, IsNE, AbsoluteDiff,
AbsoluteValue, AbsMax, AbsMin, ReLU, Square}` (the same 21-op set
[Tensor-Scalar §6b](tensor-scalar.md#6b-which-aluops-tensor-scalar-accepts-tensor_scalar_valid_ops-s3d3_tsh257)
pins). So:

| field | accept set | meaning | Tag |
|-------|-----------|---------|-----|
| `op0@36` (fold) | **`is_general_arith_op`** — the 21-op float general-arith band | the per-element `in[i] op0 scalar0` transform applied **before** the reduce | HIGH/OBSERVED |
| `op1@37` (combine) | **`{Add, Subtract, Mult, Max, Min}`** (exactly 5) | the **associative** loop-carried `acc op1 tmp` reduction | HIGH/OBSERVED |

The `op1` restriction to 5 associative operators is **unique to the cache pair** — plain Tensor-Scalar's
`op1` accepts the full general-arith band. The associativity is what makes the cross-tile `Accumulate` carry
correct: a partial accumulator chained across tiles must combine the same way regardless of tile boundary.
`[HIGH/OBSERVED]`

> **GOTCHA — `op0 = Bypass` is legal, `op1 = Bypass` is NOT.** `Bypass` is in `is_general_arith_op`, so
> `op0 = Bypass` is accepted (a pure reduce of the raw input: `tmp = in[i]`). But `Bypass` is **not** in
> `is_valid_cache_reduce_op` — `op1` **must** be one of `{Add, Subtract, Mult, Max, Min}`. There is no
> "no-op reduction"; the cache pair always combines. (This inverts the plain-TS rule, where `op1 = Bypass`
> is the *common* single-op case.) `[HIGH/OBSERVED]`

### 4g. The immediate sources — `imm0` scalar, `imm1` seed (`tensor_scalar_cache_reduce_immediates_check`)

```c
// fn tensor_scalar_cache_reduce_immediates_check(i: Inst) -> bool {
//       // imm0 = scalar0, the fold operand — per-imm IMM_SRC (identical to plain TS)
//       (   (imm0_src == ImmSrc::InstructionImmediate && ts_imm_chk(i, imm0))     // 0 — inline
//        || (imm0_src == ImmSrc::PointerImmediate     && ts_ptr_imm_chk(i, imm0)) // 1 — PartitionOffset ptr
//        || (imm0_src == ImmSrc::RegPtrImmediate      && is_valid_imm_reg(imm0.imm_reg))) // 2 — register-held ptr
//    && // imm1 = the SEED, used ONLY when accum_cmd == LoadAccumulate
//       (   (   accum_cmd == AccumCmd::LoadAccumulate
//            && (   (imm1_src == InstructionImmediate && ts_imm_chk(i, imm1))
//                || (imm1_src == PointerImmediate     && ts_ptr_imm_chk(i, imm1))
//                || (imm1_src == RegPtrImmediate      && is_valid_imm_reg(imm1.imm_reg))))
//        || (imm1_src == 0 && imm1.imm_bitvec_uint32 == 0))    // ELSE: no seed slot, must be 0
// }
```

So:

- **`imm0`** is the explicit **scalar0** of the per-element fold, sourced per-imm `{Inst0/Ptr1/RegPtr2}` —
  **identical** to plain Tensor-Scalar (`IMM_SRC`, **Inst=0**, not the swapped `IMM_SRC_N`; see the
  [Tensor-Scalar §7 GOTCHA](tensor-scalar.md#7-the-imm_src-selector--immediate-vs-pointer-vs-register-scalar-source)).
- **`imm1`** is the explicit **cache seed**, gated on `accum_cmd == LoadAccumulate`. For `ZeroAccumulate` /
  `Accumulate`, `imm1_src` and `imm1` **must be 0** — there is no seed value (the seed is `ZeroAccumulate`'s
  op1-identity, or `Accumulate`'s carried-over accumulator).

`ts_imm_chk` widths (`s3d3_ts.h:335–373`): arith uses the 32-bit fp32 immediate; bitvec is width-checked
8/16/32 by dtype — though for the cache pair, which is arith-band only (`is_general_arith_op` op0), the
immediate is effectively fp32 (`ts_imm_32_chk` accepts any arith op). `[HIGH/OBSERVED — verbatim]`

---

## 5. The dtype matrix — cache-reduce-specific (the int32/uint32 exclusion)

`tensor_scalar_cache_reduce_valid_types` (`s3d3_ts.h:390–395`, verbatim — byte-identical on mariana and
maverick):

```c
// fn tensor_scalar_cache_reduce_valid_types(i: Inst) -> bool {
//       is_valid_fp_dtype_datapath(i.s3d3_ts.in_dtype, DtypeAllowFP32R::False)   // {fp8e3,fp8e4,fp8e5,fp16,bf16,fp32}
//    || (   is_valid_int_dtype_datapath(i.s3d3_ts.in_dtype)                       // {i8,i16,i32,u8,u16,u32}
//        && (i.s3d3_ts.in_dtype != Dtype::UINT32)                                 //   ... MINUS uint32
//        && (i.s3d3_ts.in_dtype != Dtype::INT32))                                 //   ... MINUS int32
// }
```

with the datapath helpers (`common.h:3003/3023`, verbatim):

```c
// is_valid_fp_dtype_datapath(d, False) = d ∈ {FP8_EXP3, FP8_EXP4, FP8_EXP5, FP16, BFLOAT16, FP32}
//                                        (FP32R only if allow == True; here in_dtype uses False)
// is_valid_int_dtype_datapath(d)       = d ∈ {INT8, INT16, INT32, UINT8, UINT16, UINT32}
```

`DTYPE` ordinals (`common.h`, verbatim): `INVALID 0x0, UINT64 0x1, INT8 0x2, UINT8 0x3, INT16 0x4,
UINT16 0x5, BF16 0x6, FP16 0x7, INT32 0x8, UINT32 0x9, FP32 0xA, FP32R 0xB, INT64 0xC, FP8_EXP3 0xD,
FP8_EXP4 0xE, FP8_EXP5 0xF`.

| | `in_dtype` (accepted) | `in_dtype` (rejected) | `out_dtype` |
|-|----------------------|----------------------|-------------|
| **fp datapath** | `fp8_e3(0xD)`, `fp8_e4(0xE)`, `fp8_e5(0xF)`, `fp16(0x7)`, `bf16(0x6)`, `fp32(0xA)` | `fp32r(0xB)` (via `AllowFP32R::False`) | `is_valid_dtype(.., AllowFP32R::True)` — **admits `FP32R`** |
| **int datapath** | `int8(0x2)`, `uint8(0x3)`, `int16(0x4)`, `uint16(0x5)` | **`int32(0x8)`, `uint32(0x9)`** (explicit `!=` guards), `int64(0xC)`, `uint64(0x1)` | same |

> **NOTE — the int32/uint32 exclusion is the accumulator-precision guard.** The running combine **widens**
> narrow ints into a wider accumulator (`int8 → int16 → int32`); a full 32-bit int has **no headroom to
> widen further** in the int datapath, so cache-reduce/cumulative forbid `in_dtype ∈ {int32, uint32}` to
> avoid silent accumulator overflow. The fp path uses **FP32 as the convert hub**, so the accumulator is
> effectively fp32 (fp inputs) or a widened int (int8/16 inputs). This is a **cache-reduce-specific** rule —
> **plain Tensor-Scalar arith accepts int32/uint32**. The same function is shared by the CacheCumulative
> twin. `[HIGH/OBSERVED — header; the "fp32 accumulator" framing INFERRED from the FP32 hub, MED]`

---

## 6. The DVE dispatch chain — opcode `0x9a` → `"S: TensorScalarCacheReduce"` worker

The dispatch surface is the **DVE SEQ-ASCII `"S: <Name>"`** surface (not the Q7_POOL `kernel_info_table`).

### 6a. The DVE self-name strings (distinct from the twin and from plain Tensor-Reduce)

`"S: TensorScalarCacheReduce"` is its **own** DVE DEBUG DRAM entry, distinct from both the CacheCumulative
twin and the plain DVE `"S: Tensor-Reduce"`:

| GEN | `CacheReduce` str | `CacheCumulative` str | plain `Tensor-Reduce` str |
|-----|:-----------------:|:---------------------:|:-------------------------:|
| CAYMAN | `0x1f4c` | `0x1fbf` | (separate DRAM string table) |
| MARIANA | `0x202d` | `0x20a0` | `0x1ffc` |
| MAVERICK | `0x205c` | `0x20cf` | `0x1ffc` |

The MARIANA DVE self-name surface (observed, in order): `TensorLoad 0x1ecd`, `TensorStore 0x1ef7`,
`Tensor-Cumulative/Copy/Cast/Stream-Shuffle 0x1f84`, `Tensor-Scalar 0x1fd4`, `Tensor-Scalar-PTR 0x1fe6`,
`Tensor-Reduce 0x1ffc`, **`TensorScalarCacheReduce 0x202d`**, `TensorScalarCacheCumulative 0x20a0`,
`Tensor-Tensor 0x2141`, … `Select 0x2921`, `TensorTensorScan 0x2a2f`. `[HIGH/OBSERVED]`

> **NOTE — `"S: Tensor-Reduce"` (`0x1ffc`) is a DIFFERENT worker.** The DVE image carries a plain
> `"S: Tensor-Reduce"` self-name (MARIANA loader `0xa391`), **distinct** from CacheReduce (`0xa7b0`). The
> plain reduce has **neither** a scalar fold **nor** an `accum_cmd` cache — it is the DVE-side sibling of the
> POOL [Tensor-Reduce](tensor-reduce.md). CacheReduce is the cache+fold **superset**, a separate worker.
> `[HIGH/OBSERVED]`

### 6b. The worker funcVAs (entry prologue + LOG self-name loader, byte-verified)

| GEN | CacheReduce funcVA | prologue / log loader |
|-----|:------------------:|------------------------|
| CAYMAN | `~0xa2e0` (loader `0xa2e9`) | `entry a1,80 ; const16 a10,8 ; const16 a10,0x1f4c ; call8 0x18010` (entry straddles a FLIX-desync band; bytes `36 a1 00` present) |
| MARIANA | `0xa7b0` | `entry a1,80 ; const16 a10,8 ; const16 a10,0x202d ; call8 0x188a4` (byte-clean) |
| MAVERICK | `0xa438` | `entry a1,80 ; const16 a10,8 ; const16 a10,0x205c ; call8 0x151d0` (byte-clean) |

The `const16 a10,8 ; const16 a10,strVA ; call8 LOG` prologue is the standard DVE worker LOG template
([Tensor-Scalar §9c](tensor-scalar.md#9c-the-worker-body-shape-mariana-0xa040-as-far-as-flix-allows)); the
`strVA` is the **CacheReduce** string (`0x1f4c`/`0x202d`/`0x205c`), **not** CacheCumulative's — so the worker
binding is unambiguous. These funcVAs are distinct from the CacheCumulative twins (CAY `0xa7c8` / MAR
`0xacbc` / MAV `0xa940`). `[HIGH/OBSERVED — MAR/MAV entries byte-clean `36 a1 00`; CAY entry recovered at byte
level, loader byte-clean → exact CAY VA MED]`

### 6c. The registration stub (MARIANA, byte-decoded — the standard DVE template)

```asm
0x21b8: entry a1,48 ; const16 a2,0 ; const16 a2,0xa7b0 ; s32i a2,[a1+12] ;
        l32r a11,0xfffc8330 ; l32r a2,0xfffc38b0 ; call8 0x9920 ; retw
        ; -> registers CacheReduce worker 0xa7b0 into the DVE kernel_info slot
0x21da: …const16 a2,0xacbc…    ; the CacheCumulative twin stub, +0x1c stride
```

`s32i a2,[a1+12]` writes the funcVA into the kernel_info slot; `call8 0x9920` is the DVE kernel-register
routine (the standard template). `[stub edges HIGH/OBSERVED]`

> **GOTCHA — the opcode→funcVA descriptor literal is out-of-carve.** The stub's two `l32r` literals
> (`0xfffc8330` / `0xfffc38b0`) are **negative** PC-relative — they resolve *outside* the carved IRAM, in the
> runtime-bound opcode-decode / kernel-descriptor table. So the `opcode 0x9a → descriptor` binding is a
> **runtime-bound** table entry, not statically pinned in this carve (the same wall
> [Tensor-Scalar §9a](tensor-scalar.md#9a-the-dve-registration-stubs-mariana-byte-decoded) flags). The
> `funcVA → worker body` edge is byte-pinned; the `opcode → funcVA` edge is the template + the stub. The
> CacheReduce stub literals are `−0x1c` from the CacheCumulative stub literals (`0xfffc834c`/`0xfffc38cc`),
> the consecutive-descriptor stride (the same `0x1c` as the stub entry-to-entry stride).
> `[stub edges HIGH/OBSERVED; descriptor literal LOW/out-of-carve]`

### 6d. The worker body shape — the COLLAPSE evidence (the call-edge census)

The byte-counted call-edge census over the MARIANA CacheReduce body (`0xa7b0..0xac14`, 1124 B) vs the
CacheCumulative body (`0xacbc..`, 1372 B) is the **decisive** reduce-vs-scan structural evidence:

```asm
a7b0: entry a1,80
a7b6/a7b9/a7bc: const16 a10,8 ; const16 a10,0x202d ; call8 0x188a4   ; LOG "S: TensorScalarCacheReduce"
a7d4: call0 0x6b9f8 ; a7d7: call0 0x2ba58                            ; setup helpers (positive VAs, in-carve)
a810: call0 0xfffbb904                                               ; shared DVE setup (negative literal)
a83e: call0 0xfffbd930 ; a870: call0 0xfffbd964 ; a89c: call0 0xfffbd990  ; *** the alu_op.cpp REDUCE compute
                                                                          ;     CHAIN — 3 contiguous edges (Δ +0x34, +0x2c) ***
a8c8: call0 0xfff9408c                                               ; (a further compute/emit edge)
abea: call0 0x6be0c                                                  ; teardown (positive VA)
... retw at next-entry 0xac14                                        ; body span 1124 B
```

The call-edge **count delta**:

| worker | funcVA | body span | total `call0` | negative-literal compute edges | contiguous `alu_op.cpp` band |
|--------|:------:|:---------:|:-------------:|:------------------------------:|:----------------------------:|
| **CacheReduce** (`0x9a`) | `0xa7b0` | **1124 B** | **8** | (≈5) | **3** (`0xfffbd930/964/990`, Δ `+0x34`/`+0x2c`) |
| **CacheCumulative** (`0xe6`) | `0xacbc` | **1372 B** | **15** | (≈11) | **8** (`0xfffbde3c..0xfffbdf44`, stride `0x24`) |

The reduce's `alu_op.cpp` compute chain is **shorter** (3 vs 8 contiguous compute edges; 8 vs 15 total
`call0`; 1124 vs 1372 B) — consistent with the reduce **collapsing to a single final emit** vs the scan
**emitting the running partial at every step**. This is a **direct count** of `call0` targets in each
byte-clean body (independently re-disassembled and reproduced byte-for-byte). The intermediate
negative-literal sub-tally is approximate (≈5 vs ≈11) because a couple of out-of-band negative-literal edges
sit outside the contiguous compute band; the **3-vs-8 contiguous** and **8-vs-15 total** figures are exact.

The canonical chain:

```text
TensorScalarCacheReduce : [SEQ opcode 0x9a] -> [DVE kernel_info slot (registered by MARIANA stub 0x21b8)]
                          -> funcVA 0xa7b0 ("S: TensorScalarCacheReduce" worker)
                          -> FLIX program: splat(scalar0) + op0-fold + op1-accumulate
                          -> alu_op.cpp REDUCE compute (the SHORT 3-edge band, out-of-carve)
                          -> writes the FINAL accumulator (per channel). The cache persists across calls per accum_cmd.
```

`[entry/log/call edges + the count delta HIGH/OBSERVED (a direct count); the exact FLIX compute bundles MED
through the FLIX-desync; the out-of-carve negative-literal compute targets LOW]`

---

## 7. The shared `alu_op.cpp` compute + the per-element trace

The per-element fold + the loop-carried combine funnel into the **same** `alu_op.cpp` per-lane dispatcher as
plain [Tensor-Scalar](tensor-scalar.md#5c-the-shared-compute--alu_opcpp-the-same-datapath-as-tensor-tensor),
[Tensor-Tensor](tensor-tensor.md), and the CacheCumulative twin — proven by the firmware's own embedded
assert/trace source strings (MARIANA NX_DVE DEBUG DRAM):

```text
@DRAM 0x2b94: "S: OP=%x R[%d] = OP(R[%d], imm)"     <- the per-element op0 fold: R[d] = op0(in, scalar0)
@DRAM 0x2bb5: "S: OP=%x R[%d] = OP(R[%d], R[%d])"    <- the register-register form: the op1 combine acc = op1(acc, tmp)
@DRAM 0x2c73: "…/decode/alu_op.cpp:262 0"
@DRAM 0x2caa: "…/decode/alu_op.cpp:196 0 && \"not supported op\""
@DRAM 0x2cf7: "…/decode/alu_op.cpp:141 0 && \"not supported op\""
@DRAM 0x2d44: "…/decode/alu_op.cpp:231 0 && \"not supported dtype\""
@DRAM 0x2d94: "…/decode/alu_op.cpp:220 0 && \"not supported op\""
```

The **two adjacent trace forms** are the cache pair's `op0`/`op1` split: `OP(R[d], imm)` is the per-element
`op0(in, scalar0)` (scalar fold against the splatted immediate); `OP(R[d], R[d'])` is the loop-carried
`op1(acc, tmp)` (register-register accumulate). The `"not supported op"`/`"not supported dtype"` asserts are
the C++ `switch` default arms — the **same** ones every S3D3_TS op shares. The scalar fold splats `scalar0`
across lanes via the `ivp_rep*` REPLICATE family co-issued with the AluOp, exactly as
[Tensor-Scalar §5a](tensor-scalar.md#5a-the-splat-the-scalar-broadcast-across-lanes--the-ivp-replicate-family)
decodes (the cache pair shares the splat datapath). The **only** difference between reduce and scan in this
compute is whether the running `acc` is **streamed** (scan) or **collapsed** (reduce) — a property of the
worker body (§6d), not the ALU dispatcher. `[HIGH/OBSERVED strings; the in-datapath op selection MED through
the FLIX desync]`

---

## 8. The contrast with plain Tensor-Reduce — five structural gulfs

CacheReduce (`0x9a`) is a **fundamentally different** instruction from the plain GPSIMD
[Tensor-Reduce](tensor-reduce.md) (the CrossLaneReduce family). The five structural gulfs, all observed at the
header/struct/dispatch level:

| dimension | plain **Tensor-Reduce** (CrossLaneReduce) | **CacheReduce** (`0x9a`, this page) |
|-----------|-------------------------------------------|-------------------------------------|
| opcode | `0x7c` arith / `0x7d` bitvec | `0x9a` (single, arith-band) |
| engine / surface | **Q7_POOL** `kernel_info_table` (idx → `clr_reduce_local`) | **DVE** SEQ-ASCII `"S:"` worker (funcVA `0xa7b0` MAR; stub `0x21b8`) |
| struct | `S4D4_CR` (64 B): `reduce_op@35`, `reduce_axis@36`, `scale@40` | `S3D3_TS` (64 B): `accum_cmd@12`, `op0@36`, `op1@37`, `imm0@40`, `imm1@44` |
| **scalar fold** | **NONE** — folds the raw input | **`op0` per-element fold** `(in[i] op0 scalar0)` **before** the reduce ← KEY ADD |
| **persistent cache** | **NONE** — one tile → one output, no carry | **`accum_cmd` cross-tile accumulator** `{Zero/Accumulate/Load}` ← KEY ADD |
| reduce axis | **partition/lane** axis (folds the SBUF partition dim onto SIMD lanes) | **inner stream** axis (one accumulator per channel along the src inner dim); channel axis preserved |
| op vocabulary | `reduce_op` ∈ closed `{ADD, AVG, MAX, OR, AND, XOR}` (6) | `op0` ∈ general-arith (21) + `op1` ∈ `{Add, Sub, Mult, Max, Min}` (5) |
| output count | All→1 scalar / Partitions→per-lane (struct-sized smaller) | dst `same_element_count == src` (full-sized; worker emits final acc) |

**The one-sentence answer:** CacheReduce **is "a tensor-SCALAR reduce with a carried accumulator across
tiles"** — it adds, on top of a reduction, (a) a per-element scalar fold (`op0` with `scalar0`/`imm0`) and
(b) a persistent loop-carried accumulator (`accum_cmd`) that chains across invocations. Plain Tensor-Reduce
has **neither**: no scalar fold (folds raw input), no cross-tile accumulator (one tile → one output). They
live on **different engines** (DVE vs POOL), **different structs** (`S3D3_TS` vs `S4D4_CR`), and fold
**different axes** (inner-stream vs partition/lane). They are **not** variants of one kernel — they are
distinct opcodes with distinct dispatch surfaces.

`[HIGH/OBSERVED — both structs + both dispatch chains read; the inner-stream-per-channel axis framing for
CacheReduce INFERRED from {`accum_cmd` per-channel + `same_element_count` + the DVE stream datapath}, MED]`

---

## 9. The scan-vs-reduce distinction, in one frame

CacheReduce and [CacheCumulative](ts-cache-cumulative.md) are the **same op with two emit policies** — the
COLLAPSE and the SCAN of the **same** persistent-accumulator + scalar-fold recurrence:

| | **CacheReduce** (`0x9a`) — COLLAPSE | **CacheCumulative** (`0xe6`) — SCAN |
|-|-------------------------------------|-------------------------------------|
| opcode predicate | `has_tensor_cache_reduce_opcode` | `has_tensor_cache_cumulative_opcode` |
| validity body | character-identical (`s3d3_ts.h:110–130`) | character-identical except the opcode line (`:132–152`) |
| recurrence | `acc = acc op1 (in[i] op0 scalar0)` | **identical** recurrence |
| **emit** | the **FINAL** `acc` only (fewer outputs than inputs) | `acc` at **EACH** `i` (one output per input — the running scan) |
| worker funcVA (MAR) | `0xa7b0` (1124 B, **3**-edge compute) | `0xacbc` (1372 B, **8**-edge compute) |
| dst element count | `== src` (full-sized; un-emitted slots = the scan's partials) | `== src` (full-sized; every slot streamed) |
| self-name | `"S: TensorScalarCacheReduce"` `0x202d` | `"S: TensorScalarCacheCumulative"` `0x20a0` |

The distinction is **entirely** in the opcode-selected worker body — the descriptor and validity layers are
shared. A reduce **collapses** the stream to one value per channel; a scan **streams** the running partial.
`[HIGH/OBSERVED — the validity diff + the worker-VA + the compute-edge delta; the per-step-vs-final emit
INFERRED from the opcode name + the body delta, MED]`

---

## 10. Per-generation presence

| GEN | opcode `0x9a` | `S3D3_TS` (64 B) | `"S:…CacheReduce"` DVE str | worker funcVA | wired? | Tag |
|-----|:------------:|:----------------:|:--------------------------:|:-------------:|:------:|-----|
| SUNDA (V1) | defined `// Y` | identical | (no NX_DVE DEBUG image carved) | n/a | **defined** | HIGH/OBSERVED |
| CAYMAN (NC-v3) | defined `// Y` | identical | DRAM `0x1f4c` | `~0xa2e0` (ldr `0xa2e9`) | **WIRED** | HIGH/OBSERVED |
| MARIANA (NC-v4) | defined `// Y` | identical | DRAM `0x202d` | `0xa7b0` | **WIRED** | HIGH/OBSERVED |
| MARIANA_PLUS | (mariana hdr) | identical | (corroborated, +build delta) | (stable) | **WIRED** | HIGH string; body INFERRED |
| MAVERICK (NC-v5) | defined `// Y` | identical | DRAM `0x205c` | `0xa438` | **WIRED** | header-OBSERVED → interior INFERRED |

Three independent streams agree per gen: (A) the `common.h` opcode + the `S3D3_TS` binding; (B) the
`is_valid_tensor_scalar_cache_reduce` validator (read & diffed verbatim against the cumulative twin); (C) the
`"S: TensorScalarCacheReduce"` DVE DEBUG string + the worker funcVA + the registration stub. The validity
functions are **identical across gens** up to a `same_element_count_t3d` (sunda/cayman) vs `_m3d`
(mariana/maverick) **naming** rename inside `tensor_scalar_tensor_chk` — the src-`==`-dst element-count
**semantic** is preserved on all four. CacheReduce is a **core maintained `// Y` op**, WIRED on every
DVE-equipped gen. `[HIGH/OBSERVED]`

> **CORRECTION — the MAVERICK relaxation does NOT reach the cache-reduce op set.** The MAVERICK (NC-v5)
> [Tensor-Scalar §6c](tensor-scalar.md#6c-the-maverick-nc-v5-integer-band-relaxation) `is_valid_int_aluop_dve`
> integer-band relaxation is added **only** to the plain `tensor_scalar_valid_ops`. The cache pair's
> `tensor_scalar_cache_reduce_valid_ops` body is **byte-identical** on mariana and maverick (verified) — it
> gains **no** int-aluop relaxation. The **only** MAVERICK delta touching CacheReduce is the channel-range
> check becoming `has_valid_active_channel_range_with_tile(num_active_channels, DVE_NUM_CHANNELS,
> header.inst_flags)` (tile-aware) on the validators. The op-pair, dtype, immediate, reverse, and accum-cmd
> contracts are byte-identical mariana↔maverick. (`op1 ∈ {Add,Sub,Mult,Max,Min}` regardless of gen; and
> `tensor_scalar_cache_reduce_valid_types` already accepts the int datapath minus int32/uint32 on **both**
> gens.) `[HIGH/OBSERVED — `s3d3_ts.h` mariana↔maverick diff]`

> **MAVERICK (NC-v5) — header-OBSERVED → INFERRED.** The MAVERICK `"S:…CacheReduce"` string (`0x205c`), the
> MAVERICK `s3d3_ts.h` validators, the worker funcVA `0xa438`, and the byte-clean entry are observed; the
> MAVERICK worker **body** beyond the entry+log was not fully disassembled (FLIX-desync'd). Assumed
> identical-family by the byte-stable MARIANA decode + the matching strings. **The MAVERICK interior is
> INFERRED.** `[header-OBSERVED → interior INFERRED]`

---

## 11. Reimplementation checklist

A reimplementer building a Vision-Q7-compatible `TensorScalarCacheReduce`:

1. **Decode** `0x9a` against the **64-byte** `S3D3_TS` struct (§4) — the **same** struct as plain
   Tensor-Scalar and the CacheCumulative twin. The struct carries **no** reduce-vs-scan discriminator;
   dispatch on the **opcode**.
2. **Validate** with `is_valid_tensor_scalar_cache_reduce` (§4b): `accumulator_cmd ∈ {ZeroAccumulate,
   Accumulate, LoadAccumulate}` (§4c, **not** `Idle`); `op0` is `is_general_arith_op`; `op1 ∈ {Add, Subtract,
   Mult, Max, Min}` (§4f, **not** `Bypass`); `reverse_operands == None` (§4d); `reserved0 == 0`; `in_dtype` ∈
   fp-datapath **or** int-datapath-minus-`{int32,uint32}` and bars `FP32R`, `out_dtype` admits `FP32R` (§5);
   channels `1..128` (NC-v5: tile-aware); src/dst valid in PSUM **and** SBUF with `same_element_count(src,
   dst)`.
3. **Source the scalars** (§4g): `imm0` = the fold scalar, per-imm `IMM_SRC {Inst0, Ptr1, RegPtr2}` (use the
   `IMM_SRC` Inst=0 enum, **not** the swapped `IMM_SRC_N`); `imm1` = the **seed**, present only when
   `accum_cmd == LoadAccumulate` (else `imm1_src == 0 && imm1 == 0`).
4. **Compute** the per-channel recurrence `acc = acc op1 (in[i] op0 scalar0)` (§4a) over the `src`
   `MEM_PATTERN3D` inner stream axis, one accumulator per channel (preserving `num_active_channels`). Splat
   `scalar0` across lanes via `ivp_rep*` co-issued with the AluOp; the per-lane ALU is the shared
   `alu_op.cpp` datapath (§7). Seed per `accum_cmd`: `ZeroAccumulate` → `op1`-identity, `LoadAccumulate` →
   `imm1`, `Accumulate` → continue the carried accumulator.
5. **Emit the FINAL accumulator only** (the COLLAPSE — §4e): write into the full-sized `dst` (sized
   `== src`); the un-emitted positions are the scan's running-partial slots the reduce does not stream. For a
   **scan** instead, dispatch `0xe6` (CacheCumulative) and emit `acc` at every step.

---

## 12. Honesty ledger

**HIGH / OBSERVED** (header read / disasm / byte read / compile output):

- Opcode `TENSOR_SCALAR_CACHE_REDUCE = 0x9a`, `// Y` maintained, byte-identical on all four gens
  (`common.h`); the twin `CACHE_CUMULATIVE = 0xe6`.
- The `S3D3_TS` binding (`instruction_mapping.json` `struct2opcode`, 8 opcodes including `CACHE_REDUCE`) +
  the 64-B layout (`accum_cmd@12`, `src@16`, `in_dtype@32`, `out_dtype@33`, `num_chan@34`, `imm0_src@35`,
  `op0@36`, `op1@37`, `reverse@38`, `imm1_src@39`, `imm0@40`, `imm1@44`, `dst@48`), `ISA_STATIC_ASSERT == 64`,
  byte-identical all four gens.
- The two `is_valid_*` bodies **character-identical except one line** (`has_tensor_cache_reduce_opcode` 0x9a
  vs `has_tensor_cache_cumulative_opcode` 0xe6) — read & diffed verbatim, all gens.
- The shared validators verbatim: `has_valid_tensor_scalar_cache_reduce_accum_cmd` (∈
  `{ZeroAcc, Acc, LoadAcc}`); `tensor_scalar_cache_reduce_valid_ops` (`op0` `is_general_arith_op`, `op1`
  `is_valid_cache_reduce_op` ∈ `{Add, Sub, Mult, Max, Min}`); `tensor_scalar_cache_reduce_reverse_chk`
  (`== None`); `tensor_scalar_cache_reduce_immediates_check` (imm0 per-imm, imm1 LoadAccumulate-gated);
  `tensor_scalar_tensor_chk` (`same_element_count(src, dst)` for BOTH); `tensor_scalar_cache_reduce_valid_types`
  (fp datapath OR int minus i32/u32, out admits FP32R).
- The `ACCUM_CMD` enum (`Idle0..LoadAccumulate4`), the `DTYPE` ordinals, `is_general_arith_op`,
  `is_valid_fp/int_dtype_datapath` bodies — all read verbatim.
- The DVE self-name `"S: TensorScalarCacheReduce"` (CAY `0x1f4c` / MAR `0x202d` / MAV `0x205c`), distinct
  from the twin and from plain `"S: Tensor-Reduce"` `0x1ffc`; the worker funcVAs (MAR `0xa7b0` / MAV `0xa438`
  byte-clean `entry a1,80` + LOG loader; CAY `~0xa2e0` / loader `0xa2e9`); the MARIANA registration stub
  `0x21b8` (`const16 a2,0xa7b0` ; `s32i [a1+12]` ; `call8 0x9920`).
- The worker body shape + the **3-vs-8** contiguous `alu_op.cpp` compute-edge count (**8-vs-15** total
  `call0`; 1124 vs 1372 B) CacheReduce-vs-CacheCumulative — a direct `call0` census, independently
  re-disassembled and reproduced byte-for-byte. (The intermediate negative-literal sub-tally is ≈5 vs ≈11.)
- The `alu_op.cpp` per-element traces `"OP=%x R[%d]=OP(R[%d],imm)"` (op0 fold) + `"…OP(R[%d], R[%d])"` (op1
  combine) + the shared `"not supported op/dtype"` asserts (DRAM strings).
- Per-gen: opcodes + struct all four; strings + workers + stubs on cayman/mariana/maverick; the MAVERICK-only
  tile-aware channel-range delta (and that the int-aluop-dve relaxation does **not** touch the cache-reduce
  op set — mariana↔maverick `s3d3_ts.h` diff).

**MED / INFERRED:**

- That CacheReduce **emits only the final** accumulator (vs the scan's per-step) — HIGH from the opcode name
  + the 3-vs-8 compute-edge delta, but the **exact dst slot** the final lands in is in the out-of-carve
  `alu_op.cpp` loop.
- The "inner-stream per-channel reduce axis" framing — INFERRED from `{accum_cmd` per-channel +
  `same_element_count` + the DVE stream datapath`}`.
- The "fp32 accumulator" framing (from the FP32 datapath hub).
- The CAYMAN worker exact entry VA (`~0xa2e0`): the bytes `36 a1 00` (`entry a1,80`) are present but straddle
  a FLIX-desync band; the loader `0xa2e9` is byte-clean, so the binding is HIGH, the exact entry VA MED.
- The MARIANA_PLUS / MAVERICK worker **bodies** beyond the string- + stub-confirmed presence.

**CARRIED / LOW:**

- The exact `opcode 0x9a → funcVA` descriptor bytes (runtime-bound, out-of-carve; the stub `l32r` literals
  `0xfffc8330`/`0xfffc38b0` resolve outside the carved IRAM — the same wall plain Tensor-Scalar flags).
- The `alu_op.cpp` REDUCE compute body (the `call0` negative-literal targets `0xfffbd930/964/990` resolve
  out-of-carve; the loop is reached but not byte-walked end-to-end).

All facts read as derived from shipped-artifact static analysis (the disassembly, the `.rodata`/DRAM bytes,
the shipped C headers, and `instruction_mapping.json`) — lawful interoperability RE.

---

## Cross-references

- [TensorScalarCacheCumulative (the scan twin)](ts-cache-cumulative.md) — opcode `0xe6`, the **same**
  `S3D3_TS` struct + the **same** cache-reduce validators (`tensor_scalar_cache_reduce_valid_ops` /
  `_valid_types` / `_immediates_check`), differing in exactly the opcode predicate and the worker that
  **streams** the running scan (8-edge body `0xacbc`) instead of **collapsing** to the final value.
- [Tensor-Scalar + Tensor-Scalar-PTR](tensor-scalar.md) — the base `S3D3_TS` op (`0x43`/`0x53`) that defines
  the shared struct (`accum_cmd@12`, `op0@36`, `op1@37`, `imm0@40`, `imm1@44`), the `IMM_SRC` selector, the
  splat-then-AluOp datapath, and the worker-dispatch framing this page builds on. Plain Tensor-Scalar forces
  `accum_cmd == Idle`; CacheReduce engages the accumulator.
- [Tensor-Reduce (the plain reduce)](tensor-reduce.md) — the CrossLaneReduce family (`0x7c`/`0x7d`, `S4D4_CR`,
  **POOL** engine) — **no** scalar fold, **no** cross-tile cache, folds the **partition/lane** axis (§8 is
  the full contrast).
- [The reduce ISA batch](../../isa/ref/b08-reduce.md) — the consolidated reduce-primitive reference (the
  `REDUCE_OP` set, the reduce axes, the scan/reduce family map) that places `0x9a`/`0xe6` among the GPSIMD
  reduce/scan ops.
- [Tensor-Tensor (the ALU-OP table)](tensor-tensor.md) — the `0x41`/`0x51` op sharing the **same**
  `alu_op.cpp` per-lane compute; the `"…OP(R[%d], R[%d])"` register-register trace (§7) is the form the cache
  pair's `op1` combine reuses.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `HIGH/MED/LOW ×
  OBSERVED/INFERRED/CARRIED` vocabulary, the FLIX-desync wall, and the out-of-carve descriptor wall every
  claim here carries.
