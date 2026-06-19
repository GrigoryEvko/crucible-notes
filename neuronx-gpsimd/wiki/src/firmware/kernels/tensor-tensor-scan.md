# TensorTensorScan — `S: TensorTensorScan` (opcode `0xe5`)

**TensorTensorScan** is the GPSIMD DVE-engine primitive that fuses a **two-tensor combine** with an
**inclusive prefix scan**: it folds two input tensors (`src0`, `src1`) and a broadcast scalar (`imm0`)
element-wise into an intermediate stream `c[]`, then runs a running prefix scan over `c[]` so that
`dst[i]` depends on every `c[0..i]`. It is the **SCAN member of the `S2S2D2_STT` quartet** — the same
64-byte operand struct that carries [Scalar-Tensor-Tensor](scalar-tensor-tensor.md) (`0x9d`/`0x9e`) and
SelectReduce (`0xea`) — and the **two-tensor analogue** of
[TensorScalarCacheCumulative](ts-cache-cumulative.md) (`0xe6`), which scans a *one-tensor scalar fold*
with a persistent cross-tile accumulator. TensorTensorScan, by contrast, carries **no cross-tile
cache**: its accumulator command is forced `Idle`, so the running carry lives only inside the
worker's vector-register window for the duration of one instruction.

Everything below is derived **solely from static analysis** of the shipped GPSIMD device-firmware blob
carved out of `libnrtucode_internal.so` (sha256 `b7c67e89…`, **10,276,288 B**), disassembled with the
shipped Cadence `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) inside the shipped `gpsimd_tools`
package, plus the shipped public ISA C headers (`aws_neuron_isa_tpb_*.h`, `instruction_mapping.json`)
and the host symbol table (`nm`). Every claim carries a confidence tag `HIGH/MED/LOW ×
OBSERVED/INFERRED/CARRIED`. The scan combine math is grounded in the [ALU_OP matrix](alu-op-matrix.md)
and the [scan formal semantics](../../isa/semantics/group-semantics-ii.md).

---

## 1. Executive summary

| property | value | source |
|---|---|---|
| opcode | `NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_SCAN_ARITH` = **`0xe5`** (229), `// Y` maintained | `common.h` (all 4 gens) |
| variant | **ARITH-ONLY** — there is **no** `_SCAN_BITVEC` enumerator anywhere | `rg` over all 4 arch-isa trees |
| operand struct | `NEURON_ISA_TPB_S2S2D2_STT_STRUCT` — **64 B** (`ISA_STATIC_ASSERT == 64`) | compile-verify, all 4 gens |
| inputs | **two tensors** `src0@12`, `src1@24` (`TENSOR2D`) + **one broadcast scalar** `imm0@60` | struct |
| op-selectors | **two** — `op0@36`, `op1@37`, **both `is_general_arith_op`** | `has_valid_tensor_tensor_scan_op` |
| carry model | `accumulator_cmd@44` **forced `Idle`** (`has_zero_accum_cmd`) — **no** cross-tile cache | scan validity fn |
| emit | one out per in (`s2s2d2_stt_dst_element_cnt_check`) — a **scan**, not a collapse | scan validity fn |
| dtypes | full valid set on `src0/src1/imm0` (no FP32R), `out` adds FP32R; inline `imm0` must be FP32 | scan validity fn |
| engine / dispatch | **DVE only**; `S: TensorTensorScan` worker (no Q7_POOL entry) | firmware self-name string |
| per-gen | opcode + struct + validity **byte-identical** on SUNDA/CAYMAN/MARIANA/MAVERICK | diff + compile-verify |

The full per-element semantics, in one line:

```
c[i]   = (src0[i] op0 imm0) op1 src1[i]      // the two-stage STT combine (reverse_operands flips each stage)
dst[i] = SCAN_{k=0..i} c[k]                  // inclusive prefix scan of the combined stream
```

`op0` fuses tensor `src0` with the splatted broadcast scalar `imm0`; `op1` fuses that intermediate with
the **second tensor** `src1`; the scan is the **outer layer** the `0xe5` opcode adds on top of that
combine. The per-element combine and the scan-emit contract are `[HIGH/OBSERVED]`; the exact
inclusive-prefix recurrence (and whether `imm0` doubles as the scan seed) sits in the FLIX-desynced
compute body and is `[MED/INFERRED]` — see §3f.

> **The three "scan/cumulative" ops — do not conflate (read this first).** Three distinct opcodes on
> three distinct structs all carry running-prefix semantics:
> `0xe5 TENSOR_TENSOR_SCAN_ARITH → S2S2D2_STT` (**this op** — two-tensor scan, no cache),
> `0xe6 TENSOR_SCALAR_CACHE_CUMULATIVE → S3D3_TS` ([scalar-fold scan **with** a persistent ACCUM_CMD
> cache](ts-cache-cumulative.md)),
> and `0x4E/0x5E TENSOR_CUMULATIVE_ARITH/BITVEC → S4D4_TR` (the
> [reduce-family standalone cumulative](tensorcumulative.md)). `0xe5` and `0xe6` are **adjacent byte
> values** but **separate ops on separate structs**. `[HIGH/OBSERVED — common.h verbatim]`

---

## 2. The opcode — `TENSOR_TENSOR_SCAN_ARITH = 0xe5` (`// Y`, all four gens) `[HIGH/OBSERVED]`

The enumerator is byte-identical across all four arch-isa `common.h` trees, every one marked `// Y`
(maintained, not deprecated):

| gen | `common.h` line | enumerator | value | maint |
|---|---|---|---|---|
| CAYMAN | 295 | `NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_SCAN_ARITH` | `0xe5` | `// Y` |
| MARIANA | 305 | `NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_SCAN_ARITH` | `0xe5` | `// Y` |
| MAVERICK | 311 | `NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_SCAN_ARITH` | `0xe5` | `// Y` |
| SUNDA | 292 | `NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_SCAN_ARITH` | `0xe5` | `// Y` |

> **QUIRK — ARITH-ONLY, no bitvec twin.** Most `S*`-family arith ops ship an arith/bitvec pair
> (e.g. ScalarTensorTensor `0x9d`/`0x9e`, TensorCumulative `0x4E`/`0x5E`). The scan does **not**: an
> `rg 'SCAN_BITVEC|TENSOR_TENSOR_SCAN_BITVEC'` over all four arch-isa `common.h` trees returns **zero**
> matches. The only scan opcode in the enum is `_SCAN_ARITH`. The prefix scan is therefore an
> arith-datapath-only op — there is no integer-bit-logic scan instruction. `[HIGH/OBSERVED]`

The sibling opcodes that share the struct (and the two cousins it is most confused with) — verbatim
from MARIANA `common.h`, identical values on every gen:

```
0x4E  TENSOR_CUMULATIVE_ARITH_OP        // Y   -> S4D4_TR    (reduce-family cumulative)
0x5E  TENSOR_CUMULATIVE_BITVEC_OP       // Y   -> S4D4_TR
0x9d  SCALAR_TENSOR_TENSOR_ARITH        // Y   -> S2S2D2_STT (the non-scan two-tensor fused op)
0x9e  SCALAR_TENSOR_TENSOR_BITVEC       // Y   -> S2S2D2_STT
0xe5  TENSOR_TENSOR_SCAN_ARITH          // Y   -> S2S2D2_STT  <== THIS op
0xe6  TENSOR_SCALAR_CACHE_CUMULATIVE    // Y   -> S3D3_TS     (scalar-fold cached scan)
0xea  SELECT_REDUCE                     // Y   -> S2S2D2_STT  (op0=Bypass,op1=Max reduce)
```

`instruction_mapping.json` `struct2opcode` binds the `S2S2D2_STT` struct to **exactly four** opcodes —
byte-identical on all four gens (CAYMAN 217, SUNDA 207, MAVERICK 250, MARIANA 232):

```json
"NEURON_ISA_TPB_S2S2D2_STT_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_SCALAR_TENSOR_TENSOR_ARITH",   // 0x9d
    "NEURON_ISA_TPB_OPCODE_SCALAR_TENSOR_TENSOR_BITVEC",  // 0x9e
    "NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_SCAN_ARITH",     // 0xe5  <== THIS op
    "NEURON_ISA_TPB_OPCODE_SELECT_REDUCE"                 // 0xea
]
```

The header banner of `aws_neuron_isa_tpb_s2s2d2_stt.h` names all four members:
`ScalarTensorTensorArith` / `ScalarTensorTensorBitvec` / `TensorTensorScan` / `SelectReduce`.
`[HIGH/OBSERVED — header + json verbatim]`

---

## 3. The operand struct — `NEURON_ISA_TPB_S2S2D2_STT_STRUCT` (64 B), scan role

The scan rides the **same** struct as its three siblings. Compile-verified this page by a real C
program that `#include`s each gen's `aws_neuron_isa_tpb_s2s2d2_stt.h` and prints `offsetof`/`sizeof`;
**all four gens print byte-identical**:

```
sizeof S2S2D2_STT = 64    (ISA_STATIC_ASSERT == 64 on every gen)
header=0 events=4 src0=12 src1=24 op0=36 op1=37 rev=38 imm0_src=39
in0in1=40 out=41 nchan=42 imm0_dt=43 accum=44 reserved=45 dst=48 imm0=60
```

| off | field | C type | width | role for TensorTensorScan (`0xe5`) |
|---|---|---|---|---|
| 0 | `header` | `NEURON_ISA_TPB_HEADER` | 4 | opcode byte = `0xe5` |
| 4 | `events` | `NEURON_ISA_TPB_EVENTS` | 8 | semaphore wait/update |
| 12 | `src0_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | 12 | **input tensor A** (read PSUM/SBUF) |
| 24 | `src1_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | 12 | **input tensor B** (read PSUM/SBUF) |
| 36 | `op0` | `NEURON_ISA_TPB_ALU_OP` | 1 | **stage-0 AluOp** — folds `src0` with `imm0` (`is_general_arith_op`) |
| 37 | `op1` | `NEURON_ISA_TPB_ALU_OP` | 1 | **stage-1 AluOp** — folds that with `src1` (`is_general_arith_op`) |
| 38 | `reverse_operands` | `NEURON_ISA_TPB_TENS_SCALAR_REV_OPS` | 1 | `None0/First1/Second2/Both3` — per-stage operand-order flip |
| 39 | `imm0_src` | `NEURON_ISA_TPB_IMM_SRC` | 1 | scalar source `{Instruction0/Pointer1/RegPtr2}` |
| 40 | `in0_in1_dtype` | `NEURON_ISA_TPB_DTYPE_PAIR` | 1 | `dtype_lo`=src0, `dtype_hi`=src1 (4+4 bit) |
| 41 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` | 1 | output dtype (FP32R allowed) |
| 42 | `num_active_channels` | `uint8_t` | 1 | partition count `1..128` (`DVE_NUM_CHANNELS=128`) |
| 43 | `imm0_dtype` | `NEURON_ISA_TPB_DTYPE` | 1 | scalar dtype (FP32 for inline arith) |
| 44 | `accumulator_cmd` | `NEURON_ISA_TPB_ACCUM_CMD` | 1 | **forced `Idle`(0)** for the scan (`has_zero_accum_cmd`) |
| 45 | `reserved[3]` | `uint8_t[3]` | 3 | must be 0 (`has_s2s2d2_stt_reserved_zero`) |
| 48 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | 12 | **output tensor** — same element count as src |
| 60 | `imm0` | `NEURON_ISA_TPB_IMM_VAL_INST_FIELD` | 4 | **broadcast scalar value** (union) |

`NEURON_ISA_TPB_TENSOR2D` (12 B): `{ ADDR4 start_addr; int16 step_elem[2]; uint16 num_elem[2] }`. The
`ACCUM_CMD` enum is `IDLE=0, ZERO=1, ACCUMULATE=2, ZERO_ACCUMULATE=3, LOAD_ACCUMULATE=4`
(`common.h:871`). `[HIGH/OBSERVED — scan_verify compile output, all 4 gens]`

> **GOTCHA — the header's own offset annotation for `dst` is misleading.** The vendor comment annotates
> `dst_mem_pattern` as `// 12 (48 - 60)`. A 12-byte field at offset 48 spans bytes **48–59**; `imm0`
> begins at byte **60** (`offsetof(imm0)==60`, verified). The `(48 - 60)` is an inclusive-of-the-next
> annotation, **not** a 13-byte field. Read it as `dst@48..59, imm0@60..63`. `[HIGH/OBSERVED — compile
> offsetof]`

### 3a. The validity contract — `is_valid_tensor_tensor_scan` (verbatim, MARIANA 85–111)

```rust
fn is_valid_tensor_tensor_scan(i: Inst) -> bool {
       has_valid_neuron_header(i)
    && has_valid_neuron_events(i)
    && has_tensor_tensor_scan_opcode(i)                                  // opcode == 0xe5 ONLY
    && is_valid_dtype(src0_dtype, DtypeAllowFP32R::False)                // dtype_lo
    && is_valid_dtype(src1_dtype, DtypeAllowFP32R::False)                // dtype_hi
    && is_valid_dtype(imm0_dtype, DtypeAllowFP32R::False)
    && is_valid_dtype(out_dtype,  DtypeAllowFP32R::True)                 // out may be FP32R
    && s2s2d2_stt_imm0_dtype(i)                                         // inline imm0 == FP32 OR const-ptr
    && is_valid_enum(EnumList::AluOp, i.s2s2d2_stt.op0)
    && is_valid_enum(EnumList::AluOp, i.s2s2d2_stt.op1)
    && has_valid_tensor_tensor_scan_op(i)                               // op0,op1 BOTH is_general_arith_op
    && has_valid_s2s2d2_stt_immediate(i)                               // imm0_src ∈ {Inst,Ptr,RegPtr}
    && start_addr_active_channels(src0/src1/dst, num_active_channels)
    && tensor2d_valid(src0, .., WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)
    && tensor2d_valid(src1, .., WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)
    && tensor2d_valid(dst,  .., WriteTensor::True,  AllowedInPSUM::True, AllowedInSBUF::True)
    && check_active_channels(num_active_channels)
    && s2s2d2_stt_src_element_cnt_check(i)                              // src0.elemcount == src1.elemcount
    && s2s2d2_stt_dst_element_cnt_check(i)                              // src0.elemcount == dst.elemcount  (SCAN: one out/in)
    && tt_valid_partitions(src0.start_addr, src1.start_addr)
    && has_zero_accum_cmd(i)                                            // accumulator_cmd == 0 (Idle) FORCED
    && is_valid_enum(EnumList::TensScalarRevOps, i.s2s2d2_stt.reverse_operands)
    && has_s2s2d2_stt_reserved_zero(i)
}
```

`[HIGH/OBSERVED — header verbatim; diffed byte-identical across all four gens (this page)]`

> **CORRECTION — the scan block is *leaner* than the STT block; two STT predicates are absent.**
> Comparing `is_valid_tensor_tensor_scan` (85–111) against the sibling `is_valid_scalar_tensor_tensor`
> (54–83) in the same header, the scan **omits** two predicates that STT requires:
> `s2s2d2_stt_src_dst_dtype` (the STT src/dst-dtype-equality gate) and `s2s2d2_stt_quadrant_check`
> (the NEURON-3759 same-SBUF-quadrant erratum gate). The backing survey listed the scan block but did
> not flag these two omissions. Their absence is real, verbatim: the scan has **no** src/dst dtype
> coupling and **no** quadrant erratum constraint — it is a strictly looser validity surface than its
> STT sibling on those two axes. (It also lacks `has_valid_select_reduce_dtypes` / the int32 exclusion;
> §6.) `[HIGH/OBSERVED — the absence of both predicates in the scan block is verbatim header]`

### 3b. The two op-selectors — `has_valid_tensor_tensor_scan_op` (verbatim, MARIANA 211–214)

```rust
fn has_valid_tensor_tensor_scan_op(i: Inst) -> bool {
       is_general_arith_op(i.s2s2d2_stt.op0)
    && is_general_arith_op(i.s2s2d2_stt.op1)
}
```

`is_general_arith_op(op)` = `is_arith_op(op)` **minus** `{Divide, Pow, Mod, Rsqrt}` **and** not in the
integer-engine band (`!is_valid_int_aluop`) — verbatim `common.h:1910–1917`:

```rust
fn is_general_arith_op(op: AluOp) -> bool {
       is_arith_op(op)
    && (op != AluOp::Divide) && (op != AluOp::Pow) && (op != AluOp::Mod)
    && !is_valid_int_aluop(op)            // excludes the whole 0xC4.. int band (AddInt..ModUint..)
    && (op != AluOp::Rsqrt)
}
```

The resulting **scan combine op set** (both `op0` and `op1` draw from it) — from `is_arith_op`
(`common.h:1859`) minus the four exclusions and the int band:

| AluOp | value | AluOp | value | AluOp | value |
|---|---|---|---|---|---|
| `Bypass` | `0x00` | `IsEQ` | `0x12` | `IsNE` | `0x18` |
| `Add` | `0x04` | `IsGT` | `0x13` | `AbsoluteValue` | `0x19` |
| `Subtract` | `0x05` | `IsGE` | `0x14` | `AbsMax` | `0x20` |
| `Mult` | `0x06` | `IsLE` | `0x15` | `AbsMin` | `0x21` |
| `Max` | `0x08` | `IsLT` | `0x16` | `ReLU` | `0x22` |
| `Min` | `0x09` | `AbsoluteDiff` | `0x17` | `Square` | `0x23` |
| `LogicalAnd` | `0x0D` | `LogicalOr` | `0x0E` | `LogicalXor` | `0x0F` |

> **NOTE — two selectors, *both* unrestricted (the answer to "one selector or two").** TensorTensorScan
> takes **two** AluOp selectors, **both** any `general_arith` op. This is the sharp contrast to
> [TensorScalarCacheCumulative](ts-cache-cumulative.md) (`0xe6`), which restricts its loop-carried
> `op1` to the **five** associative reduction ops `{Add,Sub,Mult,Max,Min}` (`is_valid_cache_reduce_op`).
> Why the difference? In TensorTensorScan, `op1` is the **elementwise tensor-combine stage** (fusing the
> second tensor `src1`), *not* the running-accumulate combiner — so it carries the full `general_arith`
> freedom. The scan's *running* combine (the prefix accumulation, §3d) is a **separate** op realized by
> the rotate-combine datapath, distinct from `op0`/`op1`. `[HIGH/OBSERVED]`

> **GOTCHA — `op0/op1` exclude `Divide` and the int-engine band, but the dtype gate accepts int32.**
> A subtlety worth stating: `op0`/`op1` cannot be `DivideInt`/`AddInt`/etc. (the `0xC4..` integer-engine
> opcodes are filtered by `!is_valid_int_aluop`), yet int32/uint32 *operands* are accepted (§6). The
> integer datapath is reached through the **generic** arith ops (`Add`, `Mult`, …) operating on int
> dtypes, not through the dedicated `*Int` opcodes. `[HIGH/OBSERVED]`

### 3c. The scan-emit contract — one out per in

```rust
fn s2s2d2_stt_src_element_cnt_check(i) =
    shape_from_register(src0.start_addr) || shape_from_register(src1.start_addr)
 || (src0.num_elem[0]*num_elem[1] == src1.num_elem[0]*num_elem[1]);   // the two inputs match

fn s2s2d2_stt_dst_element_cnt_check(i) =
    shape_from_register(src0.start_addr) || shape_from_register(dst.start_addr)
 || (src0.num_elem[0]*num_elem[1] == dst.num_elem[0]*num_elem[1]);     // dst matches src element-for-element
```

`dst` has the **same** element count as each `src` (unless a shape comes from a register) — the scan
emits **one output element per input element**. A reduce emits one element total; a scan emits a running
partial at every index. This is the same scan-emit contract [TensorScalarCacheCumulative](ts-cache-cumulative.md)
enforces; SelectReduce — the other `S2S2D2_STT` member — also reuses these checks but with `op0=Bypass,
op1=Max` constrained, making it the *reduce* member of the quartet. `[HIGH/OBSERVED header]`

### 3d. The combine-then-scan semantics

The `reverse_operands` formula is canonical in `common.h:1383–1387` for the tensor/scalar roster
`(tensor, scalar[0], scalar[1])`:

```
NONE   (0): result = (tensor op0 scalar[0]) op1 scalar[1]
FIRST  (1): result = (scalar[0] op0 tensor) op1 scalar[1]
SECOND (2): result = scalar[1] op1 (tensor op0 scalar[0])
BOTH   (3): result = scalar[1] op1 (scalar[0] op0 tensor)
```

For the `S2S2D2_STT` two-tensor roster (the binding established by the
[Scalar-Tensor-Tensor](scalar-tensor-tensor.md) sibling): **`tensor = src0`**, **`scalar[0] = imm0`**
(the broadcast value), and the **`scalar[1]` slot is supplied by the second tensor `src1`**. The
per-element combined stream `c[]` is therefore:

```
reverse NONE   (0): c[i] = (src0[i] op0 imm0) op1 src1[i]
reverse FIRST  (1): c[i] = (imm0 op0 src0[i]) op1 src1[i]
reverse SECOND (2): c[i] = src1[i] op1 (src0[i] op0 imm0)
reverse BOTH   (3): c[i] = src1[i] op1 (imm0 op0 src0[i])
```

`op0` fuses the splatted broadcast scalar `imm0` with tensor `src0`; `op1` fuses that intermediate with
the **second tensor** `src1` — the identical two-stage combine as STT (same struct, same `op0`/`op1`,
same `reverse` enum). The scan is the **outer layer** the `0xe5` opcode adds on top of that combine:

```
out[i] = INCLUSIVE PREFIX SCAN of c[0..i]
```

so `dst[i]` depends on every `c[0..i]` — the loop-carried partial realized by the rotate-combine
recurrence (§5). The seed is the op-identity at `i = 0` (with `accumulator_cmd` forced `Idle`, **no**
loaded seed and **no** cross-tile carry — the scan restarts per instruction). The per-element combine is
`[HIGH/OBSERVED]` (struct + reverse formula + STT roster); the outer prefix-scan layer is
`[MED/INFERRED]` — see §3f.

### 3e. Reference compute (pseudocode)

```c
// is_valid_tensor_tensor_scan(i)  ->  the 0xe5 worker.
// Symbols are the real recovered names: is_general_arith_op, has_zero_accum_cmd,
// s2s2d2_stt_dst_element_cnt_check, NEURON_ISA_TPB_S2S2D2_STT_STRUCT.
//
// PERF NOTE: per-element work is O(1) for the combine; the prefix scan is O(N) sequential
// in value-dependence but realized in O(log2 N) SIMD passes by the rotate-combine datapath (§5).
void tensor_tensor_scan(const NEURON_ISA_TPB_S2S2D2_STT_STRUCT *i)
{
    // --- validity (forced fields) ---
    assert(i->header.opcode == 0xe5);
    assert(i->accumulator_cmd == NEURON_ISA_TPB_ACCUM_CMD_IDLE);    // has_zero_accum_cmd: NO cross-tile cache
    assert(is_general_arith_op(i->op0) && is_general_arith_op(i->op1));
    const uint32_t N = elemcount(&i->src0_mem_pattern);            // == src1 == dst (element_cnt_check)

    // --- splat the broadcast scalar (FP32 inline, or load via imm0_src ptr/reg) ---
    vreg s = splat_scalar(i->imm0, i->imm0_src, i->imm0_dtype);    // ivp_rep*  (§5)

    // --- stage 1: per-element two-tensor combine into the intermediate stream c[] ---
    for (uint32_t e = 0; e < N; ++e) {
        scalar a = tensor2d_read(&i->src0_mem_pattern, e, dtype_lo(i->in0_in1_dtype));
        scalar b = tensor2d_read(&i->src1_mem_pattern, e, dtype_hi(i->in0_in1_dtype));
        scalar t = apply_alu(i->op0, a, s, i->reverse_operands);  // (src0 op0 imm0), flipped per reverse
        c[e]     = apply_alu(i->op1, t, b, i->reverse_operands);  // op1 fuses with the SECOND tensor src1
    }

    // --- stage 2: inclusive prefix scan over c[]  (the DEFINING scan layer, §3d) ---
    // seed = op-identity at i=0; NO loaded seed (accum forced Idle).
    scalar acc = scan_identity(/* the scan combine op, §5 */);
    for (uint32_t e = 0; e < N; ++e) {                            // realized as rotate-combine SIMD passes (§5)
        acc = scan_combine(acc, c[e]);
        tensor2d_write(&i->dst_mem_pattern, e, acc, i->out_dtype);
    }
}
```

### 3f. CAVEAT — the SCAN layer vs the COMBINE layer (honest boundary)

`[HIGH/OBSERVED]`: (a) the per-element two-tensor combine `c[i]=(src0 op0 imm0) op1 src1[i]` (struct +
reverse formula + STT identity); (b) `same_element_count ⇒ scan-emit` (one out/in); (c)
`has_zero_accum_cmd` forced ⇒ no cross-call ACCUM_CMD cache. `[MED/INFERRED]`: the **exact** outer
prefix-scan recurrence — inclusive vs exclusive, how `c[]` hands off to the rotate-combine, and whether
`imm0` doubles as the scan seed or is only the combine scalar. The `0xe5` opcode-name "Scan" + the
rotate-combine datapath (§5) + the `has_zero_accum_cmd` (an *intra*-instruction loop carry, not a
field-held cache) make "combine-then-inclusive-prefix-scan" the strongest reading, but the per-step emit
index and the seed binding sit in the out-of-carve compute body (§4d). `[MED]`

---

## 4. The DVE dispatch chain — opcode `0xe5` → `S: TensorTensorScan` worker

TensorTensorScan runs on the **DVE** engine. The `S2S2D2_STT`/scan family is **DVE-only** — there is no
Q7_POOL `kernel_info_table` entry for it (it shares the DVE-only dispatch surface with its siblings).
The dispatch was byte-decoded from the carved MARIANA NX_DVE DEBUG IRAM/DRAM (container sha256
`b7c67e89…`). `[self-names + worker + stub HIGH/OBSERVED; opcode→descriptor literal LOW/out-of-carve]`

### 4a. The DVE self-name string `S: TensorTensorScan` `[HIGH/OBSERVED]`

The literal `"S: TensorTensorScan"` is present in the firmware blob **four times** — once per carved
DVE image (the four-generation build) — verified directly against the host binary
(`rg -a -o -b 'S: TensorTensorScan' libnrtucode_internal.so`):

| firmware blob file-offset | adjacent `S: TensorScalarCacheCumulative` offset | Δ |
|---|---|---|
| `0x18DA70` (1,629,296) | `0x18D0DF` (1,626,847) | 2,449 |
| `0x427650` (4,357,968) | `0x426CC0` (4,355,520) | 2,448 |
| `0x6EFF30` (7,273,584) | `0x6EE565` (7,271,141) | 2,449 |
| `0x8AF360` (9,109,536) | `0x8AD75F` (9,107,087) | 2,448 |

Each image places `S: TensorTensorScan` a constant **~2,448 bytes** after its
`S: TensorScalarCacheCumulative` sibling — corroborating that the two DVE scan workers
(`0xe5` and `0xe6`) live in **one** contiguous DVE registration block, repeated per gen. Within the
carved MARIANA DVE DEBUG DRAM, the string sits at DRAM offset **`0x2a30`** (CAYMAN `0x2950`, MAVERICK
`0x2a60`). `[HIGH/OBSERVED — raw-byte verified against the host blob]`

> **NOTE — only the two *scan* workers self-name with the `S:` prefix here.** The other two
> `S2S2D2_STT` members (ScalarTensorTensor `0x9d`/`0x9e`, SelectReduce `0xea`) do **not** appear as
> `S: …` device strings in this blob — an `rg` for `S: ScalarTensorTensor` / `S: SelectReduce` returns
> zero. The `S:`-prefixed self-name is the DVE *scan-worker* convention (shared by `0xe5` and `0xe6`);
> the non-scan STT/reduce members route through different worker naming. `[HIGH/OBSERVED]`

### 4b. The worker funcVA (MARIANA `0xd2fc`) `[HIGH/OBSERVED on the skeleton]`

The worker entry is `0xd2fc` (`36 61 00` = `entry a1, 48`). It LOGs its self-name via a **unique**
`const16 a10, 0x2a30 ; call8 0x188a4` pair at `0xd344` — the only `const16 a10,0x2a30` in the entire
MARIANA DVE IRAM (`rg`-verified unique binding into the `0x2a30` DRAM self-name string). The `entry
a1,48` frame (vs the `a1,80` of the STT-arith / CacheCumulative workers) signals a **conditional
self-name dispatcher**: `0xd31c`/`0xd32c` load descriptor pointers `0x2a50`/`0x2a60` on a config-flag
branch (`bbci a2,0`) before the LOG. `[HIGH/OBSERVED]`

### 4c. The registration stub (MARIANA `0x2244`) `[stub edges HIGH/OBSERVED; descriptor literal LOW]`

```asm
0x2244: entry a1,48
        const16 a2,0
        const16 a2,0xd2fc          ; the worker funcVA
        s32i    a2,[a1+12]         ; write funcVA 0xd2fc into the DVE kernel_info slot
        l32r    a11,0xfffc83bc     ; NEGATIVE pc-rel  -> resolves OUTSIDE the carved IRAM
        l32r    a2,0xfffc393c      ; NEGATIVE pc-rel  -> the runtime opcode-0xe5 -> descriptor table
        call8   0x9920             ; the MARIANA DVE kernel-register routine (== STT/CacheCumulative)
        retw
```

This is the same stub template the STT (`0x2215→0xb0f8`, `0x2231→0xb23c`) and CacheReduce/CacheCumulative
(`0x21c1→0xa7b0`, `0x21dd→0xacbc`) workers use, all in the same clean DVE registration block
(`0x1a67..0x23d5`). The two `l32r` literals are **negative** PC-relative and resolve out-of-carve, so the
final **opcode-`0xe5` → funcVA** descriptor byte is runtime-bound and `[LOW]` — the same limitation that
bounds the STT/CacheCumulative carves. `[HIGH/OBSERVED on the stub edges]`

### 4d. The worker body shape (MARIANA `0xd2fc`, byte-decoded as far as FLIX allows)

```
d2fc: entry a1,48
d303: s32i.n a2,[a10+44]                        ; write a config field
d305: call0  0x1e598                            ; setup helper
d30f: s8i    a2,[a1+12]                          ; single-bit config flag (the self-name selector)
d31c/d32c: const16 a2,0x2a50 / 0x2a60 (branch)   ; pick a descriptor pointer per the flag
d344/d347: const16 a10,0x2a30 ; call8 0x188a4    ; LOG "S: TensorTensorScan" (UNIQUE binding)
d352: bnei a2,2,0xd360 -> d358: call8 0xb218     ; imm0_src == RegPtr(2)? -> reg-fetch sub-handler
                                                 ;   (0xb218 == the STT single-scalar reg-fetch)
d375: call8 0xa430                               ; scalar/shape resolve
d395: call8 0x1891c                              ; DVE setup
d39b..d3b0: s16i a11,[a2/3/4/5 + 0x1f0]          ; FLIX vector-register-WINDOW programming (4 windows)
d3c7/d3d8/d3e8/d404/d433: call8 0x3cd0 / 0x43e0  ; the COMBINE + SCAN compute edges (repeated)
d456: call8 0x13040 ; d464: s16i a11,[a9+0x132]  ; scan-loop window + compute
…                                                ; a LARGE worker — more compute edges than the 2-pass STT worker
```

The flow: log → config → self-name select → **single** `imm0` source resolve (one `bnei a2,2` — **one**
scalar, like STT, *not* two like a TensorScalar op) → program multiple FLIX vector windows → the COMBINE
compute (`op0` fold `src0` with the splatted scalar; `op1` fold with `src1`) → the SCAN compute (the
rotate-combine running prefix over the combined stream, §5) → write `dst`. The entry/log/imm-src/window
edges + the single `bnei` imm test are `[HIGH/OBSERVED]`; the inner FLIX combine/scan bundles desync
(§9) and the per-edge `call8 0x3cd0/0x43e0/0x13040` roles are `[MED]`.

### 4e. The canonical chain (MARIANA)

```
TensorTensorScan : [SEQ opcode 0xe5]
  -> [DVE kernel_info slot, registered by stub 0x2244]
  -> funcVA 0xd2fc  ("S: TensorTensorScan" worker)
  -> single-scalar resolve (imm0_src; 0xb218 reg-fetch on RegPtr)
  -> FLIX window program: splat(imm0) + op0-fold(src0) + op1-fold(src1) + the prefix-scan loop
  -> writes out[i] = inclusive prefix scan of the combined stream c[]
  ; NO cross-call accumulator (accum_cmd Idle-forced)
```

`[funcVA + worker + stub HIGH; opcode→descriptor literal LOW/out-of-carve]`

---

## 5. The scan datapath — rotate ⊕ combine (the shared DVE scan engine)

The prefix-scan recurrence is the **same** rotate-combine SIMD-scan datapath used by
[TensorScalarCacheCumulative](ts-cache-cumulative.md) (`0xe6`) — both are DVE scans on one engine. The
vocabulary is byte-observed in the cleaner-FLIX MARIANA NX_DVE **PERF** IRAM (the PERF image strips the
`S:` self-names, so the scan vocabulary is established at the **DVE-family** level). The recurrence is a
**rotate** op bundled in the **same FLIX VLIW word** as a **combine** op:

```
PERF 0x4916: { … ivp_rotrn_2x32 v20,v4,v11 ; ivp_bmaxn_2x32   vb6,v4,v5,v18 }   (rotate-then-MAX)
PERF 0x29c5: { … ivp_rotrn_2x32 v16,v5,v27 ; ivp_mulpn16xr16  wv1,v12,v0 ; … } (rotate-then-MUL)
PERF 0x939d: { … ivp_rotrn_2x32 v3,v10,v6  ; ivp_babssub2nx8  vb9,v6,v5,v24 }  (rotate-then-AbsSub)
PERF 0xdf03: { … ivp_rotri2nx8  v2,v2,0    ; ivp_babssub2nx8  vb4,v2,v28,v16 } (immediate-rotate variant)
```

**The mechanism.** `ivp_rotrn_2x32` / `ivp_rotri2nx8` **rotate** the partial-accumulator vector by a
lane stride; the bundled combine op folds the rotated partial with the un-rotated one — the
**Hillis-Steele / Kogge-Stone inclusive-scan step**: each pass doubles the stride, so `log2(N)` passes
build the full prefix. For TensorTensorScan the scan input is the per-element two-tensor combined stream
`c[i] = (src0[i] op0 imm0) op1 src1[i]` (§3d): the worker first produces `c[]` (the scalar splat
`ivp_rep*` + `op0` fold + `op1` tensor-combine, the STT two-pass), **then** runs the rotate-combine prefix
scan over `c[]`. The scalar splat (`ivp_rep2nx8t` / `ivp_repnx16t` / `ivp_repn_2x32t`) materializes
`imm0` before the `op0` fold.

```c
// the rotate-combine inclusive-scan kernel (Hillis-Steele), per 512-bit vector register.
// `combine` is the scan op (the running prefix combine, distinct from op0/op1 — §3b NOTE).
// PERF NOTE: O(log2 LANES) bundled passes vs O(LANES) sequential; the rotate hides the carry latency.
vreg dve_prefix_scan(vreg c)            // c[] = the per-element two-tensor combined stream
{
    vreg acc = c;
    for (int stride = 1; stride < LANES; stride <<= 1) {
        vreg shifted = ivp_rotrn_2x32(acc, stride);     // rotate the partial accumulator by `stride`
        acc          = scan_combine(acc, shifted);      // bundled COMBINE in the same FLIX word
    }
    return acc;                                          // acc[i] = inclusive prefix of c[0..i]
}
```

> **GOTCHA — multi-tile scans have no cross-instruction carry.** Because `accumulator_cmd` is forced
> `Idle` (`has_zero_accum_cmd`, §3a), there is **no** persistent accumulator. The prefix scan is
> **self-contained per instruction**: the loop-carried partial (the rotated accumulator vector) lives
> only in the worker's register window for the duration of one scan. A scan that must span multiple tiles
> has to be expressed as a **single instruction** over the full `TENSOR2D` pattern (the address generator
> walks the tile space) — it **cannot** be chained across calls via a cache. This is the sharp contrast
> to [TensorScalarCacheCumulative](ts-cache-cumulative.md), whose ACCUM_CMD field carries the accumulator
> across tiles/calls. `[has_zero_accum_cmd forced HIGH/OBSERVED; the single-instruction-scan consequence
> INFERRED, MED]`

The rotate-combine + splat bundling is `[HIGH/OBSERVED in PERF]` (re-confirmed present this page); the
**bind of these bundles to the `0xe5` worker specifically** is `[MED]` — PERF strips self-names, so the
scan vocabulary is family-level and the rotate-combine ⇒ scan inference is structural. The exact
`op0`/`op1` → single-IVP-combine binding, the `c[]`-to-scan hand-off, and the inclusive-vs-exclusive
boundary sit in the in-carve-but-FLIX-desynced compute edges (`call8 0x3cd0/0x43e0/0x13040`); the
vocabulary and the `has_zero_accum_cmd`/`same_element_count` contracts are OBSERVED, the per-step routing
is reported structurally `[MED]`.

---

## 6. The dtype matrix — FP32-hub arith; no cache-reduce int32/uint32 exclusion

The dtype gate (`is_valid_tensor_tensor_scan`, header 89–93, verbatim):

```rust
   is_valid_dtype(src0_dtype = in0_in1_dtype.dtype_lo, DtypeAllowFP32R::False)
&& is_valid_dtype(src1_dtype = in0_in1_dtype.dtype_hi, DtypeAllowFP32R::False)
&& is_valid_dtype(imm0_dtype,                          DtypeAllowFP32R::False)
&& is_valid_dtype(out_dtype,                           DtypeAllowFP32R::True)    // out may be FP32R
&& s2s2d2_stt_imm0_dtype(i)
```

`s2s2d2_stt_imm0_dtype` (header 230) for the scan (an arith-family op, no bitvec branch):
`is_const_ptr_dve(imm0_src) || (imm0_dtype == FP32)` — i.e. the scalar comes from a **pointer/reg-ptr**
(dtype from memory) **or** an **inline** `imm0` is **always FP32** (the FP32-hub soft-float convention).
`is_const_ptr_dve` = `{PointerImmediate, RegPtrImmediate}` (`common.h:1705`). `[HIGH/OBSERVED]`

**Accepted dtypes** — the full valid set on `src0/src1/imm0` (`AllowFP32R::False`):
`{fp8_e3, fp8_e4, fp8_e5, fp16, bf16, fp32, int8/16/32, uint8/16/32, int64, uint64}` minus FP32R; `out`
adds FP32R. Dtype ordinals (`common.h`):

| dtype | ord | dtype | ord | dtype | ord | dtype | ord |
|---|---|---|---|---|---|---|---|
| `INVALID` | `0x0` | `UINT16` | `0x5` | `INT32` | `0x8` | `INT64` | `0xC` |
| `UINT64` | `0x1` | `BF16` | `0x6` | `UINT32` | `0x9` | `FP8_E3` | `0xD` |
| `INT8` | `0x2` | `FP16` | `0x7` | `FP32` | `0xA` | `FP8_E4` | `0xE` |
| `UINT8` | `0x3` | | | `FP32R` | `0xB` | `FP8_E5` | `0xF` |
| `INT16` | `0x4` | | | | | | |

> **CORRECTION / KEY CONTRAST — the scan does NOT carry the cache-reduce int32 exclusion.**
> `is_valid_tensor_tensor_scan` has **no** `has_valid_select_reduce_dtypes` and **no**
> `!is_valid_32b_int_dtype` guard (those belong to SelectReduce / CacheReduce). The scan's int handling
> is just the generic arith datapath — there is **no accumulator-precision guard**, because there is **no
> persistent cross-tile accumulator** to overflow (`accum_cmd` Idle-forced, §3a/§5). So **int32/uint32
> inputs are ACCEPTED for TensorTensorScan but REJECTED for
> [TensorScalarCacheCumulative](ts-cache-cumulative.md)** — a direct consequence of the different carry
> model. The int32/uint32 acceptance is the **observable fingerprint** of the no-cache scan. `[HIGH/
> OBSERVED — the *absence* of the int32-exclusion predicate in the scan block is verbatim header]`

`INT64`/`UINT64` are in the valid set (`is_valid_dtype` is the gate, not the explicit 64-bit split
predicate of some other families), so a 64-bit scan is **structurally permitted**, routed by the shared
DVE compute (the per-element 64-bit handling is the shared arith path). `[MED]`

---

## 7. Per-generation presence `[HIGH/OBSERVED]`

| gen | opcode `0xe5` | `S2S2D2_STT` | scan validity fn | `S: TensorTensorScan` DRAM | worker | wired? |
|---|---|---|---|---|---|---|
| SUNDA | defined `// Y` | 64 B identical | byte-identical | (no DVE DEBUG img carved) | n/a | defined |
| CAYMAN | defined `// Y` | 64 B identical | byte-identical | `0x2950` | (wired) | WIRED |
| MARIANA | defined `// Y` | 64 B identical | byte-identical | `0x2a30` | `0xd2fc` | WIRED |
| MAVERICK | defined `// Y` | 64 B identical | byte-identical | `0x2a60` | (wired) | WIRED |

Opcode value `0xe5`, the `S2S2D2_STT` struct (`scan_verify` compile output, §3), **and** the
`is_valid_tensor_tensor_scan` validity block are **byte-identical** on all four gens (the `common.h`
opcode diff, the scan-fn block diff — empty diffs after stripping line numbers — and the compile-verify,
all this page). The `S: TensorTensorScan` DVE self-name is present on CAYMAN/MARIANA/MAVERICK (every
DVE-equipped gen), and the firmware blob carries it **four** times (the fourth carved DVE image). The
MARIANA worker funcVA `0xd2fc` + its registration stub `0x2244` are byte-decoded; CAYMAN/MAVERICK
self-names confirm the worker is wired there too. SUNDA defines opcode + struct (no DVE DEBUG image
carved). TensorTensorScan is a **core maintained `// Y`** op, not a deprecated stub.

> **MARIANA_PLUS.** Where a MARIANA_PLUS configuration is present it reuses the MARIANA header and the
> same 64-B struct + scan validity; the self-name is corroborated with the usual image-base delta. The
> v5/MAVERICK-class interiors are header-OBSERVED only — the opcode + struct + validity are byte-grounded,
> but the device-worker *interior* on the newest gen is `[INFERRED]` from the MARIANA worker shape.

---

## 8. Relationship to the scan/reduce family — same engine, different arity

| property | TensorTensorScan (`0xe5`) | [TensorScalarCacheCumulative](ts-cache-cumulative.md) (`0xe6`) |
|---|---|---|
| operand struct | `S2S2D2_STT` (64 B) | `S3D3_TS` (64 B) |
| variant | **ARITH-ONLY** (no bitvec) | `// Y` |
| inputs | **2 tensor** (`src0,src1`) + 1 scalar `imm0` | 1 tensor + **2 scalar** (`imm0,imm1`) |
| tensor pattern dim | `TENSOR2D` (2-D) | `MEM_PATTERN3D` (3-D) |
| op-selectors | `op0,op1` **both** `general_arith` | `op0` general_arith, `op1` ∈ `{Add,Sub,Mult,Max,Min}` |
| per-element pre-scan | `c[i]=(src0 op0 imm0) op1 src1[i]` (two-tensor combine) | `tmp[i]=src[i] op0 scalar0` (scalar fold) |
| scan op | inclusive prefix scan over `c[]` | `acc = acc op1 tmp[i]; out[i]=acc` |
| `accumulator_cmd` | **forced `Idle`** (`has_zero_accum_cmd`) | forced **non-Idle** `{Zero/Accum/LoadAccum}` |
| cross-tile carry | **NONE** (self-contained per instruction) | **YES** (persistent ACCUM_CMD cache) |
| scan seed | op-identity at `i=0` (no loaded seed) | Zero-identity / `imm1` / carried cache |
| `reverse_operands` | `None/First/Second/Both` (full) | forced `None` |
| dtype int32/uint32 | **ACCEPTED** (no exclusion) | **EXCLUDED** (accumulator-overflow guard) |
| scan datapath | rotate ⊕ combine (Hillis-Steele) | rotate ⊕ combine (Hillis-Steele) — **same** |
| dispatch / engine | DVE `S: TensorTensorScan` | DVE `S: TensorScalarCacheCumulative` |

**Bottom line:** the **same** scan *engine* (the rotate-combine SIMD-scan datapath, §5), with **different
operand arity** and a **different carry model**. TensorTensorScan = a **two-tensor-combine** prefix scan
with **no cross-tile cache** (intra-instruction loop carry only); CacheCumulative = a **scalar-fold**
prefix scan **with** a persistent ACCUM_CMD cache (cross-tile carry). The int32/uint32 exclusion is the
**observable fingerprint** of the carry difference: CacheCumulative excludes them (its cache could
overflow), the scan accepts them (no persistent accumulator). `[HIGH/OBSERVED inputs; arity/carry
synthesis direct from the two headers]`

Also distinct: [TensorCumulative](tensorcumulative.md) `0x4E`/`0x5E` → `S4D4_TR` (the reduce-family
standalone cumulative, REDUCE_OP path) — a **third** scan op on yet another struct. And the `S2S2D2_STT`
non-scan siblings: [Scalar-Tensor-Tensor](scalar-tensor-tensor.md) `0x9d`/`0x9e` (the non-scan two-tensor
combine, `accum` Idle/Accum/ZeroAccum) and SelectReduce `0xea` (`op0=Bypass, op1=Max` constrained — a
reduce). **TensorTensorScan is the SCAN member of the `S2S2D2_STT` quartet.** See the
[scan formal semantics](../../isa/semantics/group-semantics-ii.md) for the bit-precise reference-compute.

---

## 9. Honesty ledger — HIGH / MED / LOW + FLIX-desync

**HIGH / OBSERVED** (direct disasm / byte / symtab / header / compile-output):
- Opcode `TENSOR_TENSOR_SCAN_ARITH = 0xe5`, `// Y`, byte-identical on all four gens (`common.h` lines
  CAY 295 / MAR 305 / MAV 311 / SUNDA 292); **no** `_SCAN_BITVEC` enumerator anywhere (`rg`-verified, zero hits).
- `S2S2D2_STT` binds `{STT-Arith 0x9d, STT-Bitvec 0x9e, TensorTensorScan 0xe5, SelectReduce 0xea}` in
  `instruction_mapping.json` (verbatim, all 4 gens); the 64-B struct (`src0@12, src1@24, op0@36, op1@37,
  rev@38, imm0_src@39, in0in1@40, out@41, nchan@42, imm0_dt@43, accum@44, reserved@45, dst@48, imm0@60`;
  `sizeof==64`) **compile-verified byte-identical** on sunda/cayman/mariana/maverick (this page).
- `is_valid_tensor_tensor_scan` read verbatim and diffed byte-identical across all four gens:
  `has_tensor_tensor_scan_opcode` (`0xe5` only); `op0,op1` both `is_general_arith_op`; `has_zero_accum_cmd`
  forced (`accum==Idle`); `same_element_count` `src0==src1==dst`; FP32-hub `imm0_dtype` rule; **no** int32
  exclusion; **and** the absence of `s2s2d2_stt_src_dst_dtype` / `s2s2d2_stt_quadrant_check` that STT has.
- The `reverse_operands` formula (`common.h:1383–1387`) + the STT roster → the per-element two-tensor
  combine `c[i]=(src0 op0 imm0) op1 src1[i]` with the four reverse variants.
- The DVE self-name `S: TensorTensorScan` (CAY `0x2950` / MAR `0x2a30` / MAV `0x2a60`; **4×** in the
  firmware blob at file-offsets `0x18DA70 / 0x427650 / 0x6EFF30 / 0x8AF360`, a constant ~2,448 B after
  the CacheCumulative sibling). MARIANA worker funcVA `0xd2fc` (`entry a1,48` + the **unique**
  `const16 a10,0x2a30` LOG at `0xd344`); registration stub `0x2244` (`const16 a2,0xd2fc; s32i [a1+12];
  call8 0x9920`). The single `bnei a2,2` imm0_src test (`0xd352 → 0xb218`) — **one** scalar source.

**MED / INFERRED:**
- The OUTER prefix-scan layer (combine `c[]` then inclusive scan): the per-element COMBINE is HIGH; the
  inclusive-vs-exclusive boundary, the `c[]`→scan hand-off, and whether `imm0` doubles as the scan seed
  sit in the FLIX-desynced compute edges (`call8 0x3cd0/0x43e0/0x13040`).
- The bind of the PERF rotate ⊕ combine bundles to the `0xe5` worker *specifically* (PERF strips
  self-names) — family-level scan vocabulary.
- The "single-instruction scan / no cross-tile chaining" consequence of `has_zero_accum_cmd`.
- 64-bit scan routing — structural.

**LOW / UNRECOVERED:**
- The opcode-`0xe5` → funcVA descriptor **bytes** (the stub `l32r` literals `0xfffc83bc`/`0xfffc393c`
  resolve out-of-carve; runtime-bound).
- The exact scan compute loop body (the `call8 0x3cd0/0x43e0/0x13040` edges are in-carve calls but their
  bodies FLIX-desync; the combine+scan loop is *reached* but not byte-walked end-to-end).

> **FLIX-DESYNC FLAG.** The DEBUG worker body (`0xd2fc..`) desyncs under stock `xtensa-elf-objdump` on the
> recurring `.byte 0x2f/0xcf/0x4f/0x5f` FLIX literal-pool lead bytes. The mis-decoded bundles are **not**
> reported as real instructions; the entry prologue, the unique `0x2a30` LOG loader, the `bnei` imm-src
> test, the `s16i` window-program writes, and the `call` edges **are** byte-clean. The PERF image (cleaner
> FLIX) supplies the rotate ⊕ combine scan vocabulary.

---

## 10. Reproduction

```sh
export XTENSA_SYSTEM=.../gpsimd_tools/tools/XtensaTools/config XTENSA_CORE=ncore2gp
OBJD=.../XtensaTools/bin/xtensa-elf-objdump ; STR=.../XtensaTools/bin/xtensa-elf-strings
F=extracted/.../custom_op/c10/lib/libnrtucode_internal.so          # sha256 b7c67e89…, 10,276,288 B
H=extracted/.../custom_op/c10/include

# opcode   : rg 'TENSOR_TENSOR_SCAN' $H/neuron_{cayman,mariana,maverick,sunda}_arch_isa/tpb/aws_neuron_isa_tpb_common.h   # -> 0xe5 // Y; NO _BITVEC
# struct   : $H/.../aws_neuron_isa_tpb_s2s2d2_stt.h  (struct @24; is_valid_tensor_tensor_scan @85;
#            has_tensor_tensor_scan_opcode @172; has_valid_tensor_tensor_scan_op @211; has_zero_accum_cmd @251;
#            s2s2d2_stt_imm0_dtype @230; src/dst_element_cnt_check @153/160; src_dst_dtype/quadrant ABSENT from scan block)
# reverse  : common.h:1383-1387 (reverse formula); is_general_arith_op @1910; is_arith_op @1859; is_valid_int_aluop @1654
# binding  : jq '.struct2opcode.NEURON_ISA_TPB_S2S2D2_STT_STRUCT' $H/neuron_mariana_arch_isa/tpb/instruction_mapping.json
# compile  : gcc -I$H/neuron_<gen>_arch_isa/tpb scan_verify.c  -> sizeof 64 + offsets (all 4 gens identical)
# selfname : rg -a -o -b 'S: TensorTensorScan' $F                 # -> 4 hits; MAR DRAM 0x2a30; CAY 0x2950; MAV 0x2a60
# worker   : rg 'const16\ta10, ?0x2a30' MAR_DVE_IRAM.dis          # -> UNIQUE @0xd344; worker entry 0xd2fc
# stub     : rg 'const16\ta2, ?0xd2fc'  MAR_DVE_IRAM.dis          # -> stub body 0x2244, s32i [a1+12]; call8 0x9920
# scan IVP : MARIANA DVE PERF IRAM 0x31f5c0/0x13540: rg 'ivp_rotrn_2x32|ivp_rotri2nx8'
```
