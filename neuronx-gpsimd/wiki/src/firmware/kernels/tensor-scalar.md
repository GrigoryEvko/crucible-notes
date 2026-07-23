# Tensor-Scalar + Tensor-Scalar-PTR (the broadcast-scalar ALU op)

> **Scope.** Tensor-Scalar is the GPSIMD elementwise op that combines a tensor with a single
> **broadcast scalar** — the scalar counterpart to [Tensor-Tensor](tensor-tensor.md). It is **four
> opcodes** sharing **one 64-byte operand struct** (`NEURON_ISA_TPB_S3D3_TS_STRUCT`) and the **same
> `NEURON_ISA_TPB_ALU_OP` enum** Tensor-Tensor uses:
>
> - `0x43` `TensorScalarArithOp` and `0x53` `TensorScalarBitvecOp` — **maintained** (`// Y`), the modern
>   form, dispatched to the DVE **`"S: Tensor-Scalar"`** worker.
> - `0x44` `TensorScalarPtrArithOp` and `0x54` `TensorScalarPtrBitvecOp` — **deprecated** (`// n, use
>   TensorScalar… instead`), dispatched to the DVE **`"S: Tensor-Scalar-PTR"`** worker.
>
> This page decodes the struct (compile-verified `sizeof == 64`); proves the **splat-then-two-AluOp**
> scalar-broadcast datapath (the IVP `ivp_rep*` replicate co-issued in the **same FLIX bundle** as the
> per-lane AluOp); decodes both DVE worker dispatch chains instruction-exact; pins the **reverse-operands
> `{scalar−src vs src−scalar}`** semantics (the verbatim header formula) and the per-immediate **`IMM_SRC`
> selector** (`{INSTRUCTION_IMMEDIATE=0, POINTER_IMMEDIATE=1, REG_PTR_IMMEDIATE=2}`); tabulates the **AluOp
> acceptance set** and the **dtype matrix**; and proves the **Tensor-Scalar vs Tensor-Scalar-PTR** split is
> a *validation-contract* distinction (the immediate source), not a different broadcast mechanism.
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md).

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | Tensor-Scalar is **four opcodes**: `0x43`/`0x53` maintained, `0x44`/`0x54` deprecated; all bind one **64-B** struct `NEURON_ISA_TPB_S3D3_TS_STRUCT`. | `common.h:175–178` + `instruction_mapping.json:286–294` + `ISA_STATIC_ASSERT==64` | HIGH/OBSERVED |
| 2 | It is the **scalar counterpart to [Tensor-Tensor](tensor-tensor.md)** (`0x41`/`0x51`): the **same** `alu_op.cpp` per-lane datapath, with one operand a **broadcast scalar** rather than a second tensor. | shared `alu_op.cpp` asserts @DRAM `0x2c73…0x2d94`; the twin traces `"S: OP=%x R[%d] = OP(R[%d], imm)"` (scalar) vs `…OP(R[%d], R[%d])` (tensor) | HIGH/OBSERVED |
| 3 | The scalar is **splatted across lanes by the IVP REPLICATE family** (`ivp_rep2nx8t`/`ivp_repnx16t`/`ivp_repn_2x32t`), **co-issued in the same FLIX VLIW word** as the per-lane AluOp. | MARIANA `NX_DVE PERF IRAM` bundles `@0x1aa4`/`0x2639`/`0x2872`/`0x34a3` | HIGH/OBSERVED |
| 4 | It is a **two-AluOp composition**: `result = op1(op0(tensor, scalar0), scalar1)`, with **two** scalar immediates (`imm0`,`imm1`) and a **4-valued `reverse_operands`** that flips operand order per AluOp — the `{scalar−src vs src−scalar}` control. | `common.h:1383–1387` verbatim formula comment | HIGH/OBSERVED |
| 5 | Each scalar's **source** is per-immediate `IMM_SRC {InstructionImmediate=0, PointerImmediate=1, RegPtrImmediate=2}`; the DVE worker tests `RegPtr` with `bnei a2,2`. | `common.h:1351–1355` + dispatcher `0xa130` (`66 22 0a`) | HIGH/OBSERVED |
| 6 | **TS vs TS-PTR** is a *contract* split on the **same** struct/datapath: TS allows per-imm `{Inst,Ptr,RegPtr}`; TS-PTR **forces** `imm0_src==imm1_src==0` and both immediates **must** be `PartitionOffset` pointers. | `s3d3_ts.h` `tensor_scalar_immediates_check` vs `tensor_scalar_ptr_imm_src`/`_ptr_immediates` | HIGH/OBSERVED |
| 7 | AluOp accept set: **arith** = `is_general_arith_op` for both `op0`/`op1` (Add/Sub/Mult/Max/Min/cmp/AbsDiff/AbsVal/AbsMax/AbsMin/ReLU/Square, *minus* Div/Pow/Mod/Rsqrt/int-band) + the `Rsqrt`-then-`Bypass` special case; **bitvec** = `is_bitvec_op` for both. | `tensor_scalar_valid_ops` (`s3d3_ts.h:257`) | HIGH/OBSERVED |
| 8 | **Wired on every DVE gen** — CAYMAN, MARIANA, MARIANA_PLUS, MAVERICK (unlike [Exponential](exponential.md), which is CAYMAN/SUNDA-unwired). MAVERICK adds tile-aware channel ranging + an integer-band relaxation. | per-gen DVE strings + workers + stubs; `mariana`↔`maverick` header diff | HIGH/OBSERVED; MAVERICK interior INFERRED |

---

## 2. Provenance / carve anchors

All device-firmware facts derive from static analysis of the shipped device-firmware blob
(disassembled with the Cadence Xtensa toolchain that ships *inside* the gpsimd-tools package,
`XTENSA_CORE=ncore2gp`, Vision-Q7 FLIX/VLIW) plus the shipped host customop-lib **ISA C headers**. No
source was consulted.

| Artifact | Value |
|----------|-------|
| Container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Container sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` (10,276,288 B) |
| Disassembler | `gpsimd_tools/…/bin/xtensa-elf-objdump` (Binutils 2.34.20200201, Xtensa Tools 14.09), `XTENSA_CORE=ncore2gp` |
| MARIANA `NX_DVE` DEBUG IRAM | `MARIANA_NX_DVE_DEBUG_IRAM_get.data` VA `0x408fc0` / size `0x1c560` (`.rodata` VA == file offset) — carve sha256 `4c75ba8e…` |
| MARIANA `NX_DVE` DEBUG DRAM | `MARIANA_NX_DVE_DEBUG_DRAM_get.data` VA `0x425520` / size `0x7000` |
| MARIANA `NX_DVE` PERF IRAM | `MARIANA_NX_DVE_PERF_IRAM_get.data` VA `0x31f5c0` / size `0x13540` (cleaner FLIX; the splat-bundle source) |
| CAYMAN `NX_DVE` DEBUG IRAM/DRAM | VA `0x16f660` / `0x18b320` |
| MAVERICK `NX_DVE` DEBUG IRAM/DRAM | VA `0x8945c0` / `0x8ad5c0` |

The authoritative struct/enum source is the shipped public ISA headers (same package):

- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_ts.h` — the `S3D3_TS` struct + the
  `is_valid_tensor_scalar_op` / `_ptr_op` validators + the immediate/op/dtype checks.
- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h` — the opcode enum; `ALU_OP`; `IMM_SRC`;
  `TENS_SCALAR_REV_OPS`; `ACCUM_CMD`; `IMM_VAL_INST_FIELD`; `TENSOR3D`/`MEM_PATTERN3D`; `DTYPE`;
  `POOLING_NUM_CHANNELS == DVE_NUM_CHANNELS == 128U`.
- `…/neuron_<gen>_arch_isa/tpb/instruction_mapping.json` — the struct→opcode binding (`struct2opcode`).

The `S3D3_TS` struct is **byte-identical** (`sizeof 64`, same field offsets) on cayman/mariana/maverick/sunda
(diff'd).

---

## 3. The opcodes — TensorScalar / TensorScalarPtr (vs Tensor-Tensor)

Read verbatim from `aws_neuron_isa_tpb_common.h` (mariana, identical values on every gen):

```c
NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_ARITH_OP            = 0x41,   // Y      (Tensor-Tensor twin)
NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_BITVEC_OP           = 0x51,   // Y
…
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_ARITH_OP            = 0x43,   // Y                          maintained
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_BITVEC_OP           = 0x53,   // Y                          maintained
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_ARITH_OP        = 0x44,   // n, use TensorScalarArithOp instead   deprecated
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_BITVEC_OP       = 0x54,   // n, use TensorScalarBitvecOp instead   deprecated
…
NEURON_ISA_TPB_OPCODE_TRANSPOSE_TENSOR_SCALAR_ARITH_OP  = 0x93,   // Y      (sibling on this struct)
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_REDUCE        = 0x9a,   // Y      (sibling — see ts-cache-reduce.md)
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_CUMULATIVE    = 0xe6,   // Y      (sibling — see ts-cache-cumulative.md)
```

The arith/bitvec opcode-pair convention is **identical** to Tensor-Tensor (`0x41`/`0x51`). Tensor-Scalar
*is* Tensor-Tensor with `src1` replaced by a broadcast scalar — the **same** family-byte structure
(low nibble `3` = scalar, `1` = tensor; high nibble `4` = arith, `5` = bitvec). The opcode value is the
single fact on the SEQ axis; everything else is the struct + the DVE worker.

> **NOTE — `0x43`/`0x53` are firmware kernel-lane opcodes, not Xtensa ISA mnemonics.** Keep the two
> axes distinct (the same caution [Exponential](exponential.md) §3 raises): the ~140-entry
> `NEURON_ISA_TPB_OPCODE_*` enum is the *firmware kernel-lane* axis; the `ivp_*` roster (the `ivp_rep*`,
> `ivp_bmaxnx16`, … of §5) is the *Xtensa ISA* axis. The Tensor-Scalar opcode dispatches a firmware
> handler; that handler *emits* the IVP bundles.

### 3a. The struct→opcode binding (`instruction_mapping.json`)

The `struct2opcode` table binds **eight** opcodes to `NEURON_ISA_TPB_S3D3_TS_STRUCT` (mariana, verbatim
`jq`-read):

```json
"NEURON_ISA_TPB_S3D3_TS_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_ARITH_OP",          // 0x43  this page
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_BITVEC_OP",         // 0x53  this page
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_ARITH_OP",      // 0x44  this page (deprecated)
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_BITVEC_OP",     // 0x54  this page (deprecated)
    "NEURON_ISA_TPB_OPCODE_TRANSPOSE_TENSOR_SCALAR_ARITH_OP",// 0x93  sibling (transpose-fused)
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_REDUCE",      // 0x9a  ts-cache-reduce.md
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_CUMULATIVE",  // 0xe6  ts-cache-cumulative.md
    "NEURON_ISA_TPB_OPCODE_EXPONENTIAL"                      // 0x30  exponential.md
]
```

The same eight opcodes appear in the `s3d3_ts.h` struct-usage comment (header lines 13–22). The four
**Tensor-Scalar opcodes** of this page (`0x43`/`0x53`/`0x44`/`0x54`) are the first four members.

---

## 4. The operand struct — `NEURON_ISA_TPB_S3D3_TS_STRUCT` (64 B)

`s3d3_ts.h:28`, `ISA_STATIC_ASSERT(sizeof == 64)` (all gens, diff'd byte-identical). The header gives the
byte ranges in trailing comments; read verbatim:

| off | size | field | type | role |
|----:|-----:|-------|------|------|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `opcode = 0x43/0x53/0x44/0x54`, `inst_word_len`, dbg; (NC-v5) `inst_flags` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync |
| 12 | 1 | `accumulator_cmd` | `ACCUM_CMD` | **FORCED `Idle=0`** for the plain TS ops (§4c) |
| 13 | 3 | `reserved0[3]` | `uint8` | **must be 0** (`s3d3_ts_reserved_zero`) |
| 16 | 16 | `src_mem_pattern` | `MEM_PATTERN3D` (`TENSOR3D`) | **the INPUT tensor `x`** |
| 32 | 1 | `in_dtype` | `DTYPE` | input dtype (**`FP32R` NOT allowed**) |
| 33 | 1 | `out_dtype` | `DTYPE` | output dtype (**`FP32R` allowed**) |
| 34 | 1 | `num_active_channels` | `uint8` | partition count `1..128` |
| 35 | 1 | `imm0_src` | `IMM_SRC` | **scalar0 SOURCE** `{Inst0/Ptr1/RegPtr2}` |
| 36 | 1 | `op0` | `ALU_OP` | the **first** AluOp — `(tensor op0 scalar0)` |
| 37 | 1 | `op1` | `ALU_OP` | the **second** AluOp — `(… op1 scalar1)` |
| 38 | 1 | `reverse_operands` | `TENS_SCALAR_REV_OPS` | `{None0/First1/Second2/Both3}` — per-op operand-order flip (§4a) |
| 39 | 1 | `imm1_src` | `IMM_SRC` | **scalar1 SOURCE** `{Inst0/Ptr1/RegPtr2}` |
| 40 | 4 | `imm0` | `IMM_VAL_INST_FIELD` (union) | **scalar0 VALUE** (§4e) |
| 44 | 4 | `imm1` | `IMM_VAL_INST_FIELD` (union) | **scalar1 VALUE** |
| 48 | 16 | `dst_mem_pattern` | `MEM_PATTERN3D` (`TENSOR3D`) | **the OUTPUT tensor** |

`[HIGH/OBSERVED — header text + `ISA_STATIC_ASSERT == 64`]`

`NEURON_ISA_TPB_TENSOR3D` (`common.h:682`, 16 B) = `{ ADDR4 start_addr(4); int16 step_elem[3](6);
uint16 num_elem[3](6) }` — a 3-D strided access pattern. `NEURON_ISA_TPB_MEM_PATTERN3D`
(`common.h:837`) = `union { TENSOR3D t; INDIRECT16B i }` — the indirect arm is the gather/scatter form
(the `indirect_pattern()` gate). Both `src` and `dst` are `AllowedInPSUM::True && AllowedInSBUF::True`, so
a Tensor-Scalar op can read the matmul **PSUM** directly (e.g. a bias-add straight off the PE-array
output).

> **NOTE — CAYMAN exposes the bare `TENSOR3D`, not the union.** On CAYMAN the `src_mem_pattern`/
> `dst_mem_pattern` fields are typed as the bare `NEURON_ISA_TPB_TENSOR3D` (no `MEM_PATTERN3D` union arm);
> the offsets and `sizeof 64` are byte-identical. This is the **only** struct-text divergence across gens
> — it means CAYMAN's S3D3_TS predates the indirect/gather arm but is wire-compatible.

### 4a. The two-AluOp composition + the reverse-operands formula

The op computes a **two-step composition** of `op0` then `op1` against the two scalars. The exact formula
is a **verbatim header comment** (`common.h:1383–1387`, the `// tensor_scalar reverse operands options:`
block):

```text
NONE   (0x00):  result = (tensor op0 scalar[0]) op1 scalar[1]
FIRST  (0x01):  result = (scalar[0] op0 tensor) op1 scalar[1]     <- reverse op0  (scalar−src)
SECOND (0x02):  result = scalar[1] op1 (tensor op0 scalar[0])     <- reverse op1
BOTH   (0x03):  result = scalar[1] op1 (scalar[0] op0 tensor)     <- reverse both
```

So `reverse_operands` **is** the `{scalar−src vs src−scalar}` control the op exposes — per-AluOp. The
canonical example: `op0 = Subtract` with `reverse = None` yields `(tensor − scalar0)` (src−scalar);
with `reverse = First` it yields `(scalar0 − tensor)` (scalar−src). The single-AluOp form is the common
case — `op1 = Bypass` gives `result = (tensor op0 scalar0)`. `[HIGH/OBSERVED — verbatim header comment]`

The annotated C compute (naming the validator + the splat IVP; the per-lane loop is the shared
`alu_op.cpp` datapath of §5c):

```c
// Tensor-Scalar per-element compute. op0/op1 are NEURON_ISA_TPB_ALU_OP; reverse is TENS_SCALAR_REV_OPS.
// alu(): the shared alu_op.cpp per-element ALU dispatcher (the "S: OP=%x R[%d] = OP(R[%d], imm)" path).
// Validity is pre-checked by is_valid_tensor_scalar_op (s3d3_ts.h:60) before this runs.
static inline elem_t ts_compute(elem_t x, scalar_t s0, scalar_t s1,
                                alu_op_t op0, alu_op_t op1, rev_ops_t reverse) {
    // op0 applied to (tensor, scalar0); FIRST/BOTH flip the operand order (scalar−src vs src−scalar).
    elem_t a = (reverse == REV_FIRST || reverse == REV_BOTH)
             ? alu(op0, /*lhs=*/s0, /*rhs=*/x)        // scalar0 op0 tensor   (reversed)
             : alu(op0, /*lhs=*/x,  /*rhs=*/s0);       // tensor  op0 scalar0  (forward)
    if (op1 == ALU_OP_BYPASS) return a;                // single-AluOp form (the common case)
    // op1 applied to (a, scalar1); SECOND/BOTH flip op1's operand order.
    return (reverse == REV_SECOND || reverse == REV_BOTH)
         ? alu(op1, /*lhs=*/s1, /*rhs=*/a)            // scalar1 op1 a        (reversed)
         : alu(op1, /*lhs=*/a,  /*rhs=*/s1);           // a       op1 scalar1  (forward)
}
```

> **GOTCHA — `op1` cannot run alone.** `tensor_scalar_valid_ops` (`s3d3_ts.h:278`) enforces
> `(op0 != Bypass) || (op1 == Bypass)`. You **cannot** set `op0 = Bypass, op1 = Add`: the second op runs
> only if the first one does. To apply a single op, put it in `op0` and set `op1 = Bypass`, **not** the
> other way around. `[HIGH/OBSERVED]`

### 4b. The validation contract (`is_valid_tensor_scalar_op`, verbatim sense)

```c
// fn is_valid_tensor_scalar_op(i: Inst) -> bool {
//       has_valid_neuron_header(i) && has_valid_neuron_events(i)
//    && has_tensor_scalar_opcode(i)                 // opcode ∈ {0x43, 0x53, 0x93(transpose)}
//    && tensor_scalar_valid_ops(i)                  // §6 — op0/op1 accept set + composition rule
//    && tensor_scalar_valid_types(i)                // §6 — bitvec int-only identity in/out
//    && tensor_scalar_immediates_check(i)           // §7 — per-imm Inst/Ptr/RegPtr
//    && tensor_scalar_shift_chk(i)                  // §6 — shift-left+shift-right => 4-byte dtype
//    && tensor_scalar_tensor_chk(i)                 // src/dst mem3d valid + same_element_count
//    && tensor_scalar_reverse_chk(i)                // reverse_operands ∈ valid enum
//    && s3d3_ts_reserved_zero(i)                    // reserved0[3] == 0
//    && s3d3_transpose_check(i)                     // (transpose-only channel/element gate)
//    && is_valid_dtype(i.s3d3_ts.in_dtype,  DtypeAllowFP32R::False)   // input  bars FP32R
//    && is_valid_dtype(i.s3d3_ts.out_dtype, DtypeAllowFP32R::True)    // output admits FP32R
//    && is_valid_aluop(i.s3d3_ts.op0) && is_valid_aluop(i.s3d3_ts.op1)
//    && has_zero_accum_cmd_field(i)                 // §4c — accumulator_cmd == Idle(0)
//    && has_valid_active_channel_range(i.s3d3_ts.num_active_channels, POOLING_NUM_CHANNELS)  // 128
//    && check_m3d_active_channels(src, n) && check_m3d_active_channels(dst, n)
//    && mem3d_valid(src, in_dtype,  WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)
//    && mem3d_valid(dst, out_dtype, WriteTensor::True,  AllowedInPSUM::True, AllowedInSBUF::True)
//    && indirect_quadrant_check_src3d(src)
//    && indirect_quadrant_check_src3d_dst3d(src, dst)
// }
```

In one sentence: **apply the two-AluOp scalar composition to every element of the `src` `TENSOR3D`
(PSUM or SBUF) and write the `dst` `TENSOR3D`**, with the accumulator idle, the dtypes valid, the
op-pair in the accept set, and the immediates correctly sourced. `tensor_scalar_tensor_chk` additionally
enforces `same_element_count_m3d(src, dst)` and `indirect_dst_size_limit_m3d(dst)`.

> **GOTCHA — `TransposeTensorScalarArithOp` (`0x93`) rides the same opcode predicate.**
> `has_tensor_scalar_opcode` accepts `0x43`, `0x53`, **and** `0x93` — so the transpose-fused variant
> shares this validator, gated extra by `s3d3_transpose_check` (`has_transpose_src_element_count_m3d` +
> `has_multiple_32_channels`). It is a *sibling*, out of this page's core scope, but a reimplementer
> dispatching `has_tensor_scalar_opcode` must include `0x93`. `[HIGH/OBSERVED]`

### 4c. The accumulator contract (`has_zero_accum_cmd_field`)

```c
// fn has_zero_accum_cmd_field(i: Inst) -> bool {  (i.s3d3_ts.accumulator_cmd == 0) }   // == Idle
```

`ACCUM_CMD` (`common.h:870`): `IDLE=0, ZERO=1, ACCUMULATE=2, ZERO_ACCUMULATE=3, LOAD_ACCUMULATE=4`. The
**plain Tensor-Scalar ops force `accumulator_cmd == Idle`** — they are pure elementwise transforms with
**no** running accumulation. The DVE per-lane accumulator is engaged **only** for the siblings
[`TensorScalarCacheReduce`/`CacheCumulative`](ts-cache-reduce.md)
(`has_valid_tensor_scalar_cache_reduce_accum_cmd`) and for [Exponential](exponential.md)
(`has_valid_exponential_accum_cmd`).

### 4d. `is_valid_aluop`, the enum-validity gate

`tensor_scalar_valid_ops` opens with `is_valid_enum(EnumList::AluOp, op0)` and `…op1` — the raw byte must
be a defined `ALU_OP` member (the base `0x00..0x23` band or the int band `0xC4..0xE1`; the gaps
`0x1E/0x1F`, `0x24..0xC3` are rejected).

### 4e. The immediate union (`imm0` / `imm1`)

`imm0`/`imm1` overlay `NEURON_ISA_TPB_IMM_VAL_INST_FIELD` (`common.h:959`, 4 B):

```c
typedef union NEURON_ISA_TPB_IMM_VAL_INST_FIELD {
    NEURON_ISA_TPB_PARTITION_OFFSET imm_ptr;             // when src == Ptr / RegPtr (a SBUF/PSUM offset)
    NEURON_ISA_TPB_IMM_REG          imm_reg;             // when src == RegPtr ({regnum:u8, rsvd[3]})
    float                           imm_arith_fp32;      // when src == Inst + ARITH op   (always fp32)
    int32_t                         imm_bitvec_int32;    // when src == Inst + BITVEC op
    uint32_t                        imm_bitvec_uint32;
    uint16_t                        imm_bitvec_uint16[2];
    uint8_t                         imm_bitvec_uint8[4];
} NEURON_ISA_TPB_IMM_VAL_INST_FIELD;
```

One 4-byte slot, reinterpreted by `(imm_src, op-class)`: an **arith** instruction-immediate is **always**
read as `fp32`; a **bitvec** instruction-immediate is a 32/16/8-bit value width-checked by
`ts_imm_32/16/8_chk` (§7).

---

## 5. The scalar-broadcast datapath — splat + two-AluOp + reverse

### 5a. The splat (the scalar broadcast across lanes) — the IVP REPLICATE family

The decisive scalar-broadcast evidence: in the MARIANA `NX_DVE PERF IRAM` (cleaner FLIX), the **replicate
op is emitted in the SAME 16-byte VLIW word as the per-lane AluOp**. Byte-clean bundles (device-local
VAs, disassembled `XTENSA_CORE=ncore2gp`):

```asm
; PERF @0x1aa4 — splat lane32 of v0 across all lanes of v24, then MAX(v10, v24) = tensor MAX scalar
{ … ; ivp_mulpan16xr16 wv2,v6,v16,pr10 ; ivp_rep2nx8t  v24, v0, 32, vb0 ; ivp_bmaxnx16    vb6,v10,v24,v0 }

; PERF @0x2639 — splat lane21 of v22, then abs-sub  = AbsoluteDiff(tensor, scalar)
{ … ; ivp_ultn_2xf32 vb2,v6,v22       ; ivp_rep2nx8t  v19, v22,21, vb6 ; ivp_babssubnx16 vb7,v0,v7,v2 }

; PERF @0x2872 — splat lane0 of v1, then XOR  = the BITVEC path: tensor ^ scalar
{ … ; ivp_mul4t2n8xr8 wv2,v0,v0,pr8    ; ivp_repnx16t  v2,  v1, 0,  vb0 ; ivp_xor2nx8     v0,v27,v24 }

; PERF @0x34a3 — splat lane3 of v5, then signed multiply  = tensor * scalar
{ … ; ivp_mul4tan16xr8 wv2,v16,v26,pr1 ; ivp_repnx16t  v8,  v5, 3,  vb7 ; ivp_mulsgnsnx16 v0,v2,v8 }
```

The replicate signature is `ivp_rep{2nx8t|nx16t|n_2x32t} vDst, vSrc, lane_idx, vbPred` — it broadcasts
`vSrc[lane_idx]` across **all** lanes of `vDst` (lane geometry: `2nx8` = 64×8-bit, `nx16` = 32×16-bit,
`n_2x32` = 32×32-bit), materialising the scalar as a full-width vector. The per-lane AluOp then combines
the **tensor** vector with that **splatted-scalar** vector. This is the "splat-then-per-lane-AluOp"
mechanism. `[HIGH/OBSERVED — five byte-clean co-issue bundles; 28 `ivp_rep*` sites total in PERF]`

> **NOTE — the splat is a HARDWARE broadcast, not a stride-0 address pattern.** Contrast
> [Tensor-Tensor](tensor-tensor.md): it has *no* broadcast flag, so it broadcasts a `src` by setting its
> `TENSOR3D.step_elem` to 0 (a degenerate re-read of one element). Tensor-Scalar's operand is **not a
> tensor at all** — it is a single scalar (`imm0`/`imm1`) splatted by `ivp_rep*` in-datapath. The two
> paths are complementary.

> **CORRECTION — the per-`op0`-code → single-`ivp` binding is not 1:1 byte-pinnable.** The bundles above
> prove the splat **co-issues** with an AluOp and let you read the *structural* binding
> (`bmax`↔Max, `xor`↔BitwiseXor, `babssub`↔AbsoluteDiff, `mulsgns`↔Mult). But the exact `op0`→intrinsic
> selection arithmetic sits in the `alu_op.cpp` compute (§5c), which desyncs under stock objdump
> (the flat DEBUG image ships no `.xt.prop` FLIX table). The **vocabulary** and the **splat↔AluOp
> bundling** are byte-OBSERVED; the per-op routing is `[MED]`.

### 5b. The full IVP datapath vocabulary (PERF IRAM, byte-harvested)

The vector ops the TS worker emits (the same `ivp` families [Tensor-Tensor](tensor-tensor.md) uses,
minus the second tensor load, plus the splat):

| AluOp class | IVP intrinsics observed | Tag |
|-------------|-------------------------|-----|
| **splat** | `ivp_rep2nx8t`, `ivp_repnx16t`, `ivp_repn_2x32t`, `ivp_rep2nx8`, `ivp_mov2nx8t` | HIGH/OBSERVED |
| add/sub | `ivp_addn_2x32t`, `ivp_addsnx16`, `ivp_addnxf16t`, `ivp_add2nx8t`, `ivp_subsnx16t`, `ivp_subnx16t`, `ivp_subn_2xf32t`, `ivp_sub2nx8t` | HIGH/OBSERVED |
| multiply | `ivp_mul4t2n8xr8`, `ivp_mulsupn16xr16`, `ivp_mulpan16xr16`, `ivp_mulpn16xr16`, `ivp_mulsgnsnx16`, `ivp_mulp2n8xr16` | HIGH/OBSERVED |
| max/min | `ivp_bmaxnx16`, `ivp_bmaxunx16`, `ivp_bmax2nx8`, `ivp_bmaxu2nx8`, `ivp_bminn_2x32`, `ivp_bminunx16`, `ivp_bmin2nx8`, `ivp_minunx16t` | HIGH/OBSERVED |
| abs-diff | `ivp_babssubu2nx8`, `ivp_babssubnx16`, `ivp_babssub2nx8`, `ivp_babssubunx16` | HIGH/OBSERVED |
| compare/select | `ivp_eq2nx8`, `ivp_ultn_2xf32`, `ivp_dselnx16t`, `ivp_sel2nx8i`, `ivp_sel2nx8i_s4` | HIGH/OBSERVED |
| bitvec | `ivp_xor2nx8`, `ivp_or2nx8`, `ivp_and2nx8`, `ivp_andb`, `ivp_andnotb` | HIGH/OBSERVED |
| shift | `ivp_lsrnx16_i`, `ivp_lsrn_2x32_i`, `ivp_srln_2x32`, `ivp_sran_2x32`, `ivp_srsnx16`, `ivp_sranx16` | HIGH/OBSERVED |
| load/store/convert | `ivp_l2au2nx8_xp`, `ivp_lanx8s_xp`, `ivp_lvn_2x16{u,s}_i`, `ivp_float16nx16t`, `ivp_dextrprn_2x32` | HIGH/OBSERVED |

`[HIGH/OBSERVED the vocabulary; MED the per-op routing through the desynced compute]`

### 5c. The shared compute — `alu_op.cpp` (the same datapath as Tensor-Tensor)

The per-element compute is the **shared `alu_op.cpp` dispatcher** — proven by the firmware's own embedded
assert source-file strings (xxd-verified in MARIANA `NX_DVE` DEBUG DRAM):

```text
@DRAM 0x2b94: "S: OP=%x R[%d] = OP(R[%d], imm)"        <- the TS scalar-op trace: R[d] = OP(R[d], scalar-imm)
@DRAM 0x2bb5: "S: OP=%x R[%d] = OP(R[%d], R[%d])"      <- the Tensor-Tensor twin (two registers) — see tensor-tensor.md
@DRAM 0x2caa: "…/decode/alu_op.cpp:196 0 && \"not supported op\""
@DRAM 0x2cf7: "…/decode/alu_op.cpp:141 0 && \"not supported op\""
@DRAM 0x2d44: "…/decode/alu_op.cpp:231 0 && \"not supported dtype\""
@DRAM 0x2d94: "…/decode/alu_op.cpp:220 0 && \"not supported op\""
@DRAM 0x2c73: "…/decode/alu_op.cpp:262 0"
```

The two **adjacent** traces — `…OP(R[d], imm)` (scalar) at `0x2b94` and `…OP(R[d], R[d])` (tensor) at
`0x2bb5` — are the byte-level proof that Tensor-Scalar and [Tensor-Tensor](tensor-tensor.md) funnel into
the **same** `alu_op.cpp` per-lane ALU dispatcher, differing only in whether the second operand is a
splatted scalar or a second register. The `"not supported op"`/`"not supported dtype"` asserts are the
C++ `switch` default — structurally identical to Tensor-Tensor's. `[HIGH/OBSERVED strings; the in-datapath
op selection MED through the FLIX desync]`

---

## 6. The AluOp acceptance set + the dtype matrix

### 6a. The `ALU_OP` enum (the same enum Tensor-Tensor uses)

`op0`/`op1` hold `NEURON_ISA_TPB_ALU_OP` (`common.h:1033`). The full enum — base band `0x00..0x23`
(note the `0x1E/0x1F` gap) plus the integer-engine band `0xC4..0xE1` (`bit[7:6]==0x3` selects the
int engine):

```c
0x00 BYPASS         0x01 BITWISE_NOT    0x02 ARITH_SHIFT_LEFT  0x03 ARITH_SHIFT_RIGHT
0x04 ADD            0x05 SUBTRACT       0x06 MULT              0x07 DIVIDE
0x08 MAX            0x09 MIN            0x0A BITWISE_AND       0x0B BITWISE_OR
0x0C BITWISE_XOR    0x0D LOGICAL_AND    0x0E LOGICAL_OR        0x0F LOGICAL_XOR
0x10 LOGICAL_SHIFT_LEFT  0x11 LOGICAL_SHIFT_RIGHT             0x12 IS_EQ   0x13 IS_GT
0x14 IS_GE          0x15 IS_LE          0x16 IS_LT             0x17 ABSOLUTE_DIFF  // abs(src0-src1)
0x18 IS_NE          0x19 ABSOLUTE_VALUE 0x1A POW (Pool only)   0x1B MOD
0x1C CRC32 (Pool only)  0x1D RSQRT (Pool only)
0x20 ABS_MAX        0x21 ABS_MIN        0x22 RE_LU             0x23 SQUARE
// integer engine band:
0xC4 ADD_INT   0xC5 MULT_INT  0xC6 SUBTRACT_INT  0xC7 DIVIDE_INT  0xC8 MOD_INT
0xC9 IS_EQUAL_INT  0xCA IS_NE_INT  0xCB ABS_MAX_INT  0xCC ABS_MIN_INT  0xCD ABS_DIFF_INT
0xCE ABS_VALUE_INT  0xCF MAX_INT  0xD0 MIN_INT  0xD1 IS_GT_INT  0xD2 IS_GE_INT
0xD3 IS_LE_INT  0xD4 IS_LT_INT  0xD5 MAX_UINT  0xD6 MIN_UINT  0xD7 IS_GT_UINT
0xD8 IS_GE_UINT  0xD9 IS_LE_UINT  0xDA IS_LT_UINT  0xDB MULT_UINT  0xDC DIVIDE_UINT
0xDD MOD_UINT  0xDE AMAX_INT  0xDF AMIN_INT  0xE0 AMAX_UINT  0xE1 AMIN_UINT  // argmax/argmin -> u32
```

> **CORRECTION — the `0x20..0x23` extension (`AbsMax/AbsMin/ReLU/Square`) is a per-gen delta.** A CAYMAN-era
> survey of the enum stops at `0x1D RSQRT`; the `0x20..0x23` band (and the int-engine relaxation, §6c) is a
> **mariana/maverick addition**. A reimplementer must read the **target gen's** `common.h`, not assume the
> band is universal. `[HIGH/OBSERVED — per-gen enum diff]`

### 6b. Which AluOps Tensor-Scalar accepts (`tensor_scalar_valid_ops`, `s3d3_ts.h:257`)

Read verbatim (mariana):

```c
// fn tensor_scalar_valid_ops(i: Inst) -> bool {
//       is_valid_enum(EnumList::AluOp, i.s3d3_ts.op0) && is_valid_enum(EnumList::AluOp, i.s3d3_ts.op1)
//    && op0 != Divide && op0 != Pow && op0 != Mod          // common rejects (BOTH ops)
//    && op1 != Divide && op1 != Pow && op1 != Mod
//    && ( !tensor_scalar_bitvec(i) || (is_bitvec_op(op0) && is_bitvec_op(op1)) )       // bitvec ops
//    && (    !tensor_scalar_arith(i)
//         || (is_general_arith_op(op0) && is_general_arith_op(op1))                     // arith ops
//         || ( op0 == Rsqrt && op1 == Bypass                                            // the Rsqrt special
//              && imm0.imm_bitvec_uint32 == 0 && imm0_src == 0
//              && imm1.imm_bitvec_uint32 == 0 && imm1_src == 0 ) )
//    && ( op0 != Bypass || op1 == Bypass )                 // op1 runs only if op0 does (§4a GOTCHA)
// }
```

The op-class predicates (their **C bodies** live in `aws_neuron_isa_tpb_assert.h`; the `common.h` /
`s3d3_ts.h` copies are commented Rust-style spec pseudocode that mirror them):

- `is_arith_op` (`assert.h:1709`) = `{Bypass, Add, Subtract, Mult, Divide, Max, Min, LogicalAnd/Or/Xor,
  IsEQ/GT/GE/LE/LT/NE, AbsoluteDiff, AbsoluteValue, AbsMax, AbsMin, ReLU, Square, Pow, Mod, AddInt,
  MultInt, SubtractInt, DivideInt, ModInt, Rsqrt}`.
- `is_general_arith_op` (`assert.h:1850`) = `is_arith_op(op) && op != Divide && op != Pow && op != Mod &&
  !is_valid_int_aluop(op) && op != Rsqrt`
  ⇒ `{Bypass, Add, Subtract, Mult, Max, Min, LogicalAnd, LogicalOr, LogicalXor, IsEQ, IsGT, IsGE, IsLE,
  IsLT, IsNE, AbsoluteDiff, AbsoluteValue, AbsMax, AbsMin, ReLU, Square}` (the **MARIANA+/MAVERICK**
  21-op form; **SUNDA/CAYMAN** lack the float `AbsMax/AbsMin/ReLU/Square` enumerators (`0x20..0x23`),
  so there the set is **17** — per-gen 17/21, canonical in the [ALU-op matrix §3.1](./alu-op-matrix.md)).
  The `is_arith_op` list above likewise reflects the MARIANA+ enum.
- `is_bitvec_op` (`assert.h:1788`) = `{Bypass, BitwiseNot, ArithShiftLeft, ArithShiftRight,
  LogicalShiftLeft, LogicalShiftRight, BitwiseAnd, BitwiseOr, BitwiseXor, Crc32}`.

So:

| family | op0 / op1 accept set | Tag |
|--------|---------------------|-----|
| **TS ARITH** (`0x43`/`0x44`) | **both** must be `is_general_arith_op` (21 ops on MARIANA+/MAVERICK; 17 on SUNDA/CAYMAN — see above) — **or** the single special case `op0==Rsqrt && op1==Bypass && imm0==imm1==0 && imm0_src==imm1_src==0` | HIGH/OBSERVED |
| **TS BITVEC** (`0x53`/`0x54`) | **both** must be `is_bitvec_op` (And/Or/Xor/Not/shifts/Crc32) | HIGH/OBSERVED |

Common rejects on **both** `op0` and `op1`: `Divide(0x07)`, `Pow(0x1A)`, `Mod(0x1B)`. `Rsqrt(0x1D)`,
`Crc32(0x1C)`, and `Pow` are Pool-engine ops generally; on the DVE Tensor-Scalar path `Rsqrt` is allowed
**only** through the `op0==Rsqrt, op1==Bypass` zero-immediate special case.

> **GOTCHA — the shift-pair rule needs a 4-byte dtype.** `tensor_scalar_shift_chk` →
> `alu_shift_check` (`common.h:1919`): a shift-**left** `op0` paired with a shift-**right** `op1`
> (either arith or logical) requires `type_size_check(dtype, 4)` — i.e. a 4-byte `in_dtype`. Separately,
> `alu_arith_shift_right_chk` requires a 4-byte dtype whenever **either** op is an *arithmetic* shift-right
> (`ArithShiftRight`). A 16-bit bitvec shift-left+shift-right composition is **rejected**. `[HIGH/OBSERVED]`

### 6c. The MAVERICK (NC-v5) integer-band relaxation

On MAVERICK, `tensor_scalar_valid_ops` adds a third arith arm (header diff, verbatim):

```c
//        || (   ( is_valid_int_aluop_dve(op0) || op0 == AluOp::Bypass )
//            && ( is_valid_int_aluop_dve(op1) || op1 == AluOp::Bypass ) )
```

where `is_valid_int_aluop_dve` (**MAVERICK-only**, `maverick/…/aws_neuron_isa_tpb_assert.h:1179`) =
`{Bypass, AddInt, MultInt, SubtractInt, MaxInt, MinInt, AbsMaxInt, AbsMinInt, AbsDiffInt, IsEqualInt,
IsGtInt, IsGeInt, IsLeInt, IsLtInt, IsNeInt}`. So **MAVERICK Tensor-Scalar may use the integer-engine band
`0xC4..` on the DVE; MARIANA cannot.** This predicate **does not exist in the MARIANA (NC-v4) header at
all** — it is the NC-v5 addition. (Note `is_valid_int_aluop_dve` is a strict subset of
`is_valid_int_aluop` — it omits the divide/mod-int and argmax/argmin-int ops.)
`[HIGH/OBSERVED — `mariana`↔`maverick` `s3d3_ts.h` + `assert.h` diff]`

### 6d. The dtype matrix (`tensor_scalar_valid_types` + `is_valid_dtype`)

`DTYPE` ordinals (`common.h:805`): `INVALID 0x0, UINT64 0x1, INT8 0x2, UINT8 0x3, INT16 0x4, UINT16 0x5,
BF16 0x6, FP16 0x7, INT32 0x8, UINT32 0x9, FP32 0xA, FP32R 0xB, INT64 0xC, FP8_EXP3 0xD, FP8_EXP4 0xE,
FP8_EXP5 0xF`.

| family | `in_dtype` | `out_dtype` | rule | Tag |
|--------|-----------|-------------|------|-----|
| **ARITH** (`0x43`/`0x44`) | `is_valid_dtype(.., AllowFP32R=False)` — any valid dtype **except `FP32R`** | `is_valid_dtype(.., AllowFP32R=True)` — same set **+ `FP32R`** | `tensor_scalar_valid_types` returns `true` for arith (unconstrained) | HIGH/OBSERVED |
| **BITVEC** (`0x53`/`0x54`) | must equal `out_dtype` **and** ∈ `{INT8(0x2), UINT8(0x3), INT16(0x4), UINT16(0x5), UINT32(0x9), INT32(0x8)}` | identical to `in_dtype` | `tensor_scalar_valid_types`: bitvec is **integer-only, identity in/out** | HIGH/OBSERVED |

The instruction-immediate width (`ts_imm_32/16/8_chk`, §7): **arith** always uses 32-bit (fp32);
**bitvec** uses the dtype width — 32-bit only for `{u32, i32, fp32}` inputs; a 16-bit immediate must fit
`0xFFFF` (`& 0xFFFF0000 == 0`); an 8-bit must fit `0xFF` (`& 0xFFFFFF00 == 0`). Channels
`num_active_channels ∈ 1..128` (`POOLING_NUM_CHANNELS == DVE_NUM_CHANNELS == 128U`); MAVERICK uses
`has_valid_active_channel_range_with_tile(.., DVE_NUM_CHANNELS, header.inst_flags)`.

---

## 7. The IMM_SRC selector — immediate vs pointer vs register scalar source

Each scalar's **source** is an independent `imm0_src` / `imm1_src` of type `NEURON_ISA_TPB_IMM_SRC`
(`common.h:1351`):

```c
typedef enum NEURON_ISA_TPB_IMM_SRC {
    NEURON_ISA_TPB_IMM_SRC_INSTRUCTION_IMMEDIATE = 0,    // fp32/bitvec value inline in the instruction
    NEURON_ISA_TPB_IMM_SRC_POINTER_IMMEDIATE     = 1,    // PartitionOffset pointer to per-partition immediates
    NEURON_ISA_TPB_IMM_SRC_REG_PTR_IMMEDIATE     = 2,    // PartitionOffset pointer held in a register
} NEURON_ISA_PACKED NEURON_ISA_TPB_IMM_SRC;
```

`tensor_scalar_immediates_check` (`s3d3_ts.h:297`) dispatches **per-immediate**: each of `imm0`/`imm1` is
validated by `ts_imm_chk` (inline), `ts_ptr_imm_chk` (pointer), or `is_valid_imm_reg(imm.imm_reg)`
(register pointer) according to its `*_src`.

> **GOTCHA — there is a *second*, byte-swapped `IMM_SRC_N` enum. Do not cross them.** `common.h:1375`
> defines `NEURON_ISA_TPB_IMM_SRC_N` with **`Pointer=0, Instruction=1, RegPtr=2`** — the Pointer and
> Instruction ordinals are **swapped** vs the `S3D3_TS` `IMM_SRC` used here. The Tensor-Scalar struct's
> `imm0_src`/`imm1_src` fields are `NEURON_ISA_TPB_IMM_SRC` (Inst=0), **not** the `_N` variant. A
> reimplementer decoding `imm_src == 0` as "pointer" (the `_N` ordering) inverts inline vs pointer for
> every Tensor-Scalar op. `[HIGH/OBSERVED]`

### 7a. The on-DVE IMM_SRC dispatcher — `0xa130` (`bnei a2,2`)

The worker resolves the scalar source through a dedicated sub-dispatcher at MARIANA IRAM `0xa130`
(`entry a1,48`), byte-exact — the canonical `bnei a2,2` (== `RegPtrImmediate`) reg-fetch test (the same
`66 22 0a` pattern the BatchNorm `ParamLoad2` reg-fetch uses):

```asm
a130: entry  a1, 48
a133: s32i.n a2,[a1+12] ; a135: s32i.n a3,[a1+8]
a137: l32i.n a2,[a1+12] ; a139: l32i.n a2,[a2+0]      ; load imm0_src
a13b: bnei   a2, 2, 0xa149      ; imm0_src == RegPtrImmediate(2)?  -> a141: call8 0xa160 (imm0 reg-fetch)
a14b: l32i.n a2,[a2+0]                                 ; load imm1_src
a14d: bnei   a2, 2, 0xa15b      ; imm1_src == RegPtrImmediate(2)?  -> a153: call8 0xa184 (imm1 reg-fetch)
```

The annotated C (naming the dispatcher + the two fetch sub-handlers):

```c
// On-DVE IMM_SRC realisation (MARIANA funcVA 0xa130). For EACH scalar, RegPtr(2) calls a reg-fetch
// sub-handler that resolves the register-held PartitionOffset and reads the scalar from SBUF/PSUM;
// otherwise the inline (Inst) value or the (Ptr) PartitionOffset is taken on the normal address path.
static void ts_resolve_imm_src(ts_worker_ctx_t *ctx) {
    if (ctx->inst.imm0_src == IMM_SRC_REG_PTR_IMMEDIATE)        // a13b: bnei a2,2
        ts_imm0_regptr_fetch(ctx);                              // a141: call8 0xa160
    if (ctx->inst.imm1_src == IMM_SRC_REG_PTR_IMMEDIATE)        // a14d: bnei a2,2
        ts_imm1_regptr_fetch(ctx);                              // a153: call8 0xa184
    // Inst / Ptr immediates need no register resolution here.
}
```

The two fetch sub-handlers are `0xa160` (`entry a1,48`, imm0) and `0xa184` (`entry a1,48`, imm1). On
CAYMAN the equivalent reg-fetch helpers are at `0x9d48`/`0x9d6c` — the **same** VAs the BatchNorm
`ParamLoad2` path calls, confirming a **shared** register-pointer immediate-fetch helper pair across the
scalar-immediate op family. `[HIGH/OBSERVED the two `bnei` tests + the two call targets + the entry bytes;
the fetch-body interior MED]`

> **CORRECTION — `0xa184` is the imm1 reg-fetch sub-handler, NOT the Tensor-Scalar worker.** The
> [Exponential](exponential.md) page cites `call8 0xa184 -> "S: Tensor-Scalar" worker`. The byte-exact
> entries show the **worker** is `0xa1a8` (`36 a1 00` = `entry a1,80`); `0xa184` is the `entry a1,48` imm1
> reg-fetch sub-handler. The shift came from the FLIX literal-pool desync merging the `0xa1a8` worker
> body's self-name (at `0xa1b1`) with the `0xa184` sub-handler. The dispatch is correct — Exponential
> *does* hand off to the `"S: Tensor-Scalar"` worker — but the **entry VA is `0xa1a8`, not `0xa184`**.
> `[entry bytes HIGH/OBSERVED]`

---

## 8. Tensor-Scalar vs Tensor-Scalar-PTR — the immediate-source contract

The two opcode **families** share the struct, the splat-then-AluOp datapath, and the `alu_op.cpp`
compute. They differ **only** in the immediate-source contract (`s3d3_ts.h`, verbatim sense):

| | **TensorScalar** (`0x43`/`0x53`, maintained) | **TensorScalarPtr** (`0x44`/`0x54`, deprecated) |
|---|---|---|
| validator | `is_valid_tensor_scalar_op` | `is_valid_tensor_scalar_ptr_op` |
| imm source check | `tensor_scalar_immediates_check` — `imm0_src`/`imm1_src` **each independently** ∈ `{Inst, Ptr, RegPtr}` | `tensor_scalar_ptr_imm_src` **FORCES** `imm0_src == 0 && imm1_src == 0` |
| imm value check | per-src: `ts_imm_chk` (inline) / `ts_ptr_imm_chk` (ptr) / `is_valid_imm_reg` (regptr) | `tensor_scalar_ptr_immediates` — **both** `imm0` **and** `imm1` must pass `ts_ptr_imm_chk` |
| scalar always a pointer? | no — inline, fixed-ptr, or reg-ptr, chosen per-imm | **yes** — always a `PartitionOffset` pointer, never an inline arith fp32 |
| status | `// Y` maintained | `// n, use TensorScalar… instead` |

`ts_ptr_imm_chk` (`s3d3_ts.h:341`): `tpb_addr_active_channels(imm.imm_ptr, num_active_channels)` **and**
(`tensor_scalar_arith` ⇒ `four_byte_aligned(imm.imm_ptr)`) **or** (`tensor_scalar_bitvec` ⇒
`addr_aligned_dtype(imm.imm_ptr, in_dtype)`). So the PTR scalar is a per-partition `PartitionOffset`,
alignment-checked against the op class.

The takeaway: **the PTR opcodes are the legacy "scalar always comes from a pointer" form**; the modern
`TensorScalar` op **subsumes** them by carrying the per-immediate `imm_src` field (which can *select*
pointer sourcing via `IMM_SRC_POINTER_IMMEDIATE`). The PTR worker is a *separate* registered handler
(distinct funcVAs, distinct `"S: Tensor-Scalar-PTR"` self-name) so the engine applies the stricter
pointer-only fetch **without** the per-imm src branch of `0xa130`. Same struct, same datapath, narrower
immediate contract. `[HIGH/OBSERVED — both validity fns read verbatim from `s3d3_ts.h`; distinct strings +
distinct stubs]`

---

## 9. The dispatch chains — SEQ opcode → DVE kernel → funcVA → worker

### 9a. The DVE registration stubs (MARIANA, byte-decoded)

Four stubs register the two TS workers + two TS-PTR workers in the DVE `kernel_info` table (16-byte
stride; the registration template `const16 a2, funcVA ; s32i a2,[a1+12] ; l32r ; l32r ; call8 0x9920`):

```asm
0x2030: entry a1,48 ; const16 a2,0 ; const16 a2,0xa040 ; s32i a2,[a1+12] ; … ; call8 0x9920  -> TS  worker 0xa040
0x204c: entry a1,48 ;               const16 a2,0xa1a8 ; s32i a2,[a1+12] ; … ; call8 0x9920  -> TS  worker 0xa1a8
0x2068: entry a1,48 ;               const16 a2,0xa298 ; s32i a2,[a1+12] ; … ; call8 0x9920  -> TS-PTR worker 0xa298
0x20d8: entry a1,48 ;               const16 a2,0xa310 ; s32i a2,[a1+12] ; … ; call8 0x9920  -> TS-PTR worker 0xa310
```

`s32i a2,[a1+12]` writes the funcVA into the `kernel_info` slot; `call8 0x9920` is the DVE
kernel-register routine. All four `const16 a2, funcVA` literals are byte-confirmed
(`0x2036→0xa040`, `0x2052→0xa1a8`, `0x206e→0xa298`, `0x20de→0xa310`).

> **GOTCHA — the opcode→funcVA descriptor literal is out-of-carve.** Each stub's two `l32r` literals
> (`0xfffc81a8`/`0xfffc3728` …, +`0x1c` per consecutive worker) are **negative** PC-relative — they
> resolve *outside* the carved IRAM, in the runtime-bound kernel-descriptor / opcode-decode table. So the
> `opcode 0x43/0x53/0x44/0x54 → descriptor` binding is a **runtime-bound** table entry, not statically
> pinned in this carve (the same limitation [Exponential](exponential.md) §5a flags). The
> `funcVA → worker body` edge is byte-pinned; the `opcode → funcVA` edge is the template + the stub.
> `[stub edges HIGH/OBSERVED ; opcode→funcVA descriptor LOW/out-of-carve]`

### 9b. The worker function map (entries xxd-verified)

| funcVA | entry | self-name (DRAM) | role | Tag |
|-------:|-------|------------------|------|-----|
| `0xa040` | `entry a1,80` | `"S: Tensor-Scalar"` @`0x1fd4` | TS worker #1 (or-config; `movi a10/a11,0`) | HIGH/OBSERVED |
| `0xa1a8` | `entry a1,80` | `"S: Tensor-Scalar"` @`0x1fd4` | TS worker #2 (xor-config; `movi a10/a11,1`) | HIGH/OBSERVED entry; #1/#2 role INFERRED |
| `0xa298` | `entry a1,64` | `"S: Tensor-Scalar-PTR"` @`0x1fe6` | TS-PTR worker #1 | HIGH/OBSERVED |
| `0xa310` | `entry a1,64` | `"S: Tensor-Scalar-PTR"` @`0x1fe6` | TS-PTR worker #2 | HIGH/OBSERVED |
| `0xa130` | `entry a1,48` | — | the `IMM_SRC` reg-ptr dispatcher (two `bnei a2,2`, §7a) | HIGH/OBSERVED |
| `0xa160` | `entry a1,48` | — | imm0 reg-pointer fetch sub-handler | HIGH/OBSERVED |
| `0xa184` | `entry a1,48` | — | imm1 reg-pointer fetch sub-handler | HIGH/OBSERVED |

The worker entry bytes: `0xa040`/`0xa1a8` = `36 a1 00` (`entry a1,80`); `0xa298`/`0xa310` = `36 81 00`
(`entry a1,64`); `0xa130`/`0xa160`/`0xa184` = `36 61 00` (`entry a1,48`). The TS workers carry a larger
80-byte frame (the two-imm + window-program state); the TS-PTR workers carry 64 (no per-imm src branch).
`[entry bytes HIGH/OBSERVED; the worker#1-vs-#2 = arith(0x43)-vs-bitvec(0x53) split INFERRED from the
or/xor + `movi 0/1` config divergence, MED]`

### 9c. The worker body shape (MARIANA `0xa040`, as far as FLIX allows)

```asm
a040: entry  a1, 80
a046: const16 a10,8 ; a049: const16 a10,0x1fd4 ; a04c: call8 0x188a4   ; LOG "S: Tensor-Scalar"
a06a..a07c: l32i.n a3,[a2+12/8/4/0] -> s32i.n a3,[a1+28/24/20/16]       ; copy the 16-B imm/mem-pattern descriptor
a085..a094: saltu a3,a4,a3 -> s8i [a1+44/40] ; extui a12/a13,*,0,1      ; 2 single-bit config flags (slot12/slot8 != 0)
a0a7: call8 0x9f2c ; a0ad: call8 0x99bc ; a0e0: call8 0x99c8            ; shared DVE setup helpers
a0b1/a0d8/a0ea/a10f: s16i a11,[a3+176] / s16i a11,[a3+0x130] ;          ; FLIX vector-register-WINDOW programming
   extui a11,a2,26,1 ; and a6,a6,<mask> ; slli a8,a8,14 ; add a6,a6,a8  ; (pack control bits at field positions 14/26)
a064/a0b6/a0ef: call0 0x6b288 / 0xfffbb1a8 / 0xfffbf1e0                 ; -> the alu_op.cpp compute (neg literals = out-of-carve)
a12a: call8 0xa130                                                      ; -> the IMM_SRC reg-ptr dispatcher (§7a)
a12d: retw.n                                                            ; worker RETURNS
```

The flow: **log self-name → copy the imm/mem-pattern descriptor block → compute dtype/pattern config
flags → program the FLIX vector window for the splat+AluOp → enter the shared `alu_op.cpp` compute →
dispatch the scalar source (`0xa130`) → return.** `[entry/log/call edges + the `s16i` window-program
SHAPE HIGH/OBSERVED; the exact FLIX compute bundles MED through the desync]`

> **CORRECTION — the worker-body FLIX bundles are not byte-pinnable.** The flat DEBUG image ships no
> `.xt.prop` FLIX property table, so stock `xtensa-elf-objdump` desyncs across the VLIW stream in the
> worker bodies (the recurring `.byte 0x2f/0x8f/0x4f/0x5f/0xcf` literal-pool lead bytes). The mis-decoded
> `.byte`/spurious `call0 0xfffc…` it prints there are **decode artifacts**, not instructions. The
> window-program *shape* (which control fields are packed, that it more extensive than a single
> affine) is `[HIGH/OBSERVED]`; the exact bundles are `[MED]`. The cleaner **PERF** image (§5) is the
> byte-clean source for the splat bundles.

### 9d. The canonical chains

```text
Tensor-Scalar      : [SEQ opcode 0x43/0x53] -> [DVE kernel_info slot  (stub 0x2030/0x204c)]
                     -> funcVA 0xa040 / 0xa1a8 -> "S: Tensor-Scalar" worker
                     -> 0xa130 IMM_SRC dispatch -> alu_op.cpp  splat + two-AluOp compute

Tensor-Scalar-PTR  : [SEQ opcode 0x44/0x54] -> [DVE kernel_info slot  (stub 0x2068/0x20d8)]
                     -> funcVA 0xa298 / 0xa310 -> "S: Tensor-Scalar-PTR" worker
                     -> alu_op.cpp compute, imm0_src/imm1_src FORCED 0, scalar ALWAYS a PartitionOffset ptr
```

`opcode value` = HEADER (HIGH). The `kernel_info → funcVA` edge = the registration stub (HIGH). The
`opcode → descriptor` literal is out-of-carve (LOW). `[HIGH for funcVAs + worker bodies; descriptor LOW]`

---

## 10. Per-generation presence

Three independent streams agree: (A) the `common.h` opcodes + the `S3D3_TS` struct binding; (B) the
`is_valid_tensor_scalar_op` / `_ptr_op` validators; (C) the `"S: Tensor-Scalar"`/`"-PTR"` DVE DEBUG
strings + the worker funcVAs + the registration stubs.

| GEN | opcodes `0x43/53/44/54` | `S3D3_TS` (64 B) | `"S: Tensor-Scalar"` / `-PTR` | workers | wired? | Tag |
|-----|:----------------------:|:----------------:|:-----------------------------:|---------|:------:|-----|
| SUNDA (V1) | defined (`Y/Y/n/n`) | identical | (no NX_DVE DEBUG image carved) | n/a | **defined** | HIGH/OBSERVED |
| CAYMAN (NC-v3) | defined (`Y/Y/n/n`) | identical | DRAM `0x1ef3` / `0x1f05` | `0x9c28`, `0x9ef8`, `0x9f70` | **WIRED** | HIGH/OBSERVED |
| MARIANA (NC-v4) | defined (`Y/Y/n/n`) | identical | DRAM `0x1fd4` / `0x1fe6` | `0xa040`, `0xa1a8`, `0xa298`, `0xa310` | **WIRED** | HIGH/OBSERVED |
| MARIANA_PLUS | (mariana hdr) | identical | (corroborated, +build delta) | (stable) | **WIRED** | HIGH string; body INFERRED |
| MAVERICK (NC-v5) | defined (`Y/Y/n/n`) | identical | DRAM `0x2003` / `0x2015` | `0x9c61`, `0x9f29…` | **WIRED** | header-OBSERVED → interior INFERRED |

> **KEY CONTRAST with [Exponential](exponential.md).** Tensor-Scalar is **WIRED on CAYMAN** (strings +
> workers `0x9c28`/`0x9ef8`/`0x9f70` + stubs all present), whereas Exponential is CAYMAN/SUNDA-**unwired**
> (define-but-unwired). Tensor-Scalar is a **core** op present on every DVE-equipped gen.

> **MAVERICK (NC-v5) — header-OBSERVED → INFERRED.** The MAVERICK `"S: Tensor-Scalar"`/`-PTR` strings
> (DRAM `0x2003`/`0x2015`), the MAVERICK `s3d3_ts.h` validators, and the worker presence are observed;
> the MAVERICK worker **bodies** were not fully disassembled (FLIX-desync'd). Assumed identical-family by
> the byte-stable MARIANA decode + the matching MAVERICK strings. **The MAVERICK interior is INFERRED.**

The MAVERICK deltas vs mariana (diff of the two `s3d3_ts.h`): (a) the channel-range check becomes
`has_valid_active_channel_range_with_tile(.., DVE_NUM_CHANNELS, header.inst_flags)` on **all** the
validators; (b) `tensor_scalar_valid_ops` gains the `is_valid_int_aluop_dve` relaxation (§6c). The struct,
composition, reverse, and imm-src contracts are **byte-identical**. `[HIGH/OBSERVED — header diff]`

---

## 11. Reimplementation checklist

A reimplementer building a Vision-Q7-compatible Tensor-Scalar:

1. **Decode** `0x43`/`0x53`/`0x44`/`0x54` (and the transpose sibling `0x93`) against the **64-byte**
   `S3D3_TS` struct (§4). Read the **target gen's** `common.h` for the `ALU_OP` band (the `0x20..0x23`
   extension + the int band are per-gen, §6a).
2. **Validate** with `is_valid_tensor_scalar_op` (§4b): `accumulator_cmd == Idle`; `reserved0 == 0`;
   `op0`/`op1` in the accept set with the `(op0 != Bypass) || (op1 == Bypass)` rule (§6b); the shift-pair
   4-byte-dtype rule (§6b GOTCHA); `in_dtype` bars `FP32R`, `out_dtype` admits it; bitvec int-only identity
   in/out; channels `1..128` (NC-v5: tile-aware); src/dst valid in PSUM **and** SBUF.
3. **For TS-PTR** (`0x44`/`0x54`) instead use `is_valid_tensor_scalar_ptr_op`: force
   `imm0_src == imm1_src == 0` and require both immediates to pass `ts_ptr_imm_chk` (pointer-only).
4. **Source the scalars** per-immediate by `imm0_src`/`imm1_src` ∈ `{Inst0, Ptr1, RegPtr2}` — using the
   **`IMM_SRC` (Inst=0)** enum, **not** the swapped `IMM_SRC_N` (§7 GOTCHA). `RegPtr` resolves through the
   reg-pointer fetch sub-handler.
5. **Compute** `op1(op0(tensor, scalar0), scalar1)` per element, applying `reverse_operands` to flip
   operand order per AluOp (the `{scalar−src vs src−scalar}` semantics, §4a). The scalar is **splatted**
   across lanes by `ivp_rep*` co-issued with the AluOp (§5a); the per-lane ALU is the shared `alu_op.cpp`
   datapath — the **same** one [Tensor-Tensor](tensor-tensor.md) uses, with a scalar in place of `src1`.

---

## 12. Honesty ledger

**HIGH / OBSERVED** (header read / disasm / byte read / symtab read / arithmetic identity):

- Opcodes `TENSOR_SCALAR_ARITH/BITVEC = 0x43/0x53`, `PTR_ARITH/BITVEC = 0x44/0x54` (`common.h:175–178`,
  all gens; arith/bitvec `// Y`, PTR `// n, use … instead`); the Tensor-Tensor twins `0x41/0x51`.
- The `S3D3_TS` binding (8 opcodes under `struct2opcode`, `instruction_mapping.json:286–294`) + the 64-B
  layout (`accum_cmd@12, src TENSOR3D@16, in_dtype@32, out_dtype@33, num_chan@34, imm0_src@35, op0@36,
  op1@37, reverse@38, imm1_src@39, imm0@40, imm1@44, dst TENSOR3D@48`), `ISA_STATIC_ASSERT==64`,
  byte-identical cayman/mariana/maverick/sunda (CAYMAN bare-`TENSOR3D` text diff only).
- The `reverse_operands` formula (NONE/FIRST/SECOND/BOTH = the `{scalar−src vs src−scalar}` control) — a
  verbatim header comment (`common.h:1383–1387`).
- `IMM_SRC {Inst0/Ptr1/RegPtr2}`; the *separate* `IMM_SRC_N {Ptr0/Inst1/RegPtr2}`; `ACCUM_CMD {Idle0..
  LoadAccum4}`; `REV_OPS {None0..Both3}`; `IMM_VAL_INST_FIELD` 4-B union; `TENSOR3D` 16-B; `DTYPE` ordinals.
- The validity contracts: `tensor_scalar_valid_ops` (op∉{Div,Pow,Mod}; arith=both `general_arith_op` or
  the Rsqrt-bypass special; bitvec=both `bitvec_op`; `(op0!=Bypass)||(op1==Bypass)`);
  `tensor_scalar_immediates_check` (per-imm Inst/Ptr/RegPtr); `tensor_scalar_ptr_imm_src` (PTR forces
  `imm0_src==imm1_src==0`); `tensor_scalar_valid_types` (bitvec int-only identity in/out);
  `alu_shift_check`; `has_zero_accum_cmd_field`; the full `is_arith_op`/`is_general_arith_op`/`is_bitvec_op`
  predicate **bodies** (resident in `aws_neuron_isa_tpb_assert.h`; `common.h`/`s3d3_ts.h` carry only the
  commented spec mirror) and the **MAVERICK-only** `is_valid_int_aluop_dve` body.
- The four DVE workers (`0xa040`, `0xa1a8` TS; `0xa298`, `0xa310` TS-PTR) — entry prologues xxd-verified
  (`36 a1 00`/`36 81 00`); `"S: Tensor-Scalar"`@DRAM `0x1fd4` / `"S: Tensor-Scalar-PTR"`@DRAM `0x1fe6`
  (xxd byte-exact); the four registration stubs `@0x2030/0x204c/0x2068/0x20d8 → 0xa040/0xa1a8/0xa298/0xa310`.
- The `IMM_SRC` reg-ptr dispatcher `0xa130` with two `bnei a2,2` (`66 22 0a`) tests
  (`imm0_src@a13b → call8 0xa160`, `imm1_src@a14d → call8 0xa184`); the CAYMAN reg-fetch `0x9d48`/`0x9d6c`
  == the BatchNorm `ParamLoad2` targets (shared helper pair).
- The splat: `ivp_rep2nx8t`/`ivp_repnx16t` bundled **in the same FLIX VLIW word** as the per-lane AluOp
  (`ivp_bmaxnx16`/`ivp_babssubnx16`/`ivp_xor2nx8`/`ivp_mulsgnsnx16`) — five byte-clean PERF bundles
  `@0x1aa4/0x2639/0x2872/0x34a3` (+ siblings); 28 `ivp_rep*` sites in PERF.
- The shared compute = `alu_op.cpp`: the DRAM asserts `…/alu_op.cpp:141/196/220/231/262`
  ("not supported op"/"dtype") + the adjacent `"S: OP=%x R[%d] = OP(R[%d], imm)"` (scalar) @`0x2b94` and
  `"…OP(R[%d], R[%d])"` (tensor) @`0x2bb5`.
- Per-gen: opcodes/struct all gens; strings + workers + stubs on cayman/mariana/maverick (TS WIRED on
  CAYMAN, unlike Exponential); the MAVERICK NC-v5 tile-aware channel-range + int-aluop-dve relaxation
  (header diff); the NX_DVE DEBUG symbol VAs + the IRAM carve sha256 `4c75ba8e…`.

**MED / INFERRED:**

- The worker#1(`0xa040`)-vs-#2(`0xa1a8`) = arith(`0x43`)-vs-bitvec(`0x53`) assignment — INFERRED from the
  or/xor + `movi a10/a11 = 0/1` config-flag divergence; the exact opcode each registers is the
  out-of-carve descriptor literal.
- The exact `op0`-code → single-`ivp`-intrinsic binding (the per-op switch arm) sits in the desynced
  `alu_op.cpp` compute; the IVP vocabulary + the splat↔AluOp bundling are OBSERVED, the per-op selection is
  reported structurally.
- The worker-body FLIX vector-register-window inner bundles (`s16i [a3+176]/[a3+0x130]`, field positions
  14/26) — not byte-pinnable (no `.xt.prop`); the window-program shape is HIGH.

**CARRIED / LOW:**

- The exact `opcode 0x43/0x53/0x44/0x54 → funcVA` descriptor bytes (runtime-bound, out-of-carve; the stub
  `l32r` literals `0xfffc81a8`/`0xfffc3728`… resolve outside the carved IRAM).
- The `alu_op.cpp` compute body (the `call0` negative-literal targets `0xfffbb1a8`/`0xfffbf1e0` resolve
  out-of-carve; the per-element splat+AluOp loop is reached but not byte-walked end-to-end).
- The MARIANA_PLUS / MAVERICK worker **bodies** beyond the string- + stub-confirmed presence.

All facts read as derived from shipped-artifact static analysis (the disassembly, the `.rodata`/DRAM
bytes, the shipped C headers, the symtab, and `instruction_mapping.json`) — lawful interoperability RE.

---

## Cross-references

- [Tensor-Tensor (the ALU-OP table)](tensor-tensor.md) — the tensor counterpart (`0x41`/`0x51`) sharing
  the **same** `ALU_OP` enum and the **same** `alu_op.cpp` per-lane compute; the adjacent
  `"…OP(R[%d], R[%d])"` (two-register) trace vs this page's `"…OP(R[%d], imm)"` (scalar) is the byte
  proof they share the datapath.
- [ALU-Op Matrix](alu-op-matrix.md) — the consolidated per-op accept/reject matrix across the
  S3D3_TS/S3S3D3_TT families (arith vs bitvec, the int band, the Pool-only ops).
- [Exponential (the EXP transform)](exponential.md) — the S3D3_TS sibling (`0x30`) that **forces**
  `op0`/`op1 = Bypass` and hands off to **this** `"S: Tensor-Scalar"` worker (`0xa1a8`); the worker-entry
  CORRECTION (§7a) reconciles its `0xa184` read.
- [TensorScalarCacheReduce](ts-cache-reduce.md) / [CacheCumulative](ts-cache-cumulative.md) — the S3D3_TS
  siblings (`0x9a`/`0xe6`) that *do* engage the DVE accumulator (`tensor_scalar_cache_reduce_valid_ops`,
  the `op1` ∈ `{Add,Subtract,Mult,Max,Min}` cache-reduce gate).
- [TensorScalarSelect](ts-select.md) / [TensorScalarImmLoad](ts-immld.md) / [TensorScalarPtrMulti](ts-ptrmulti.md)
  — the further TensorScalar-family ops.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `HIGH/MED/LOW ×
  OBSERVED/INFERRED/CARRIED` vocabulary, the FLIX-desync wall, and the out-of-carve descriptor wall every
  claim here carries.
