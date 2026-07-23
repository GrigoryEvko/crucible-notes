# SundaISel Deep-Dive — engine selection, intrinsic fusion, partition-reduce legalization, and the DGE selector

This page reconstructs the **instruction-selection logic of the GPSIMD/POOL leg** of
`neuronx-cc`'s **Sunda** code-generation target — the host-side stage that decides *which
engine* an engine-polymorphic arith op runs on, *when* a linear combination is fused into one
compute-cluster FMA, *how* a cross-PARTITION reduce is legalized, and *which* descriptor-
generation engine (DGE) builds an indirect DMA. Every opcode pick here is cross-checked against
the device firmware decode ([opcode catalog ledger](../firmware/kernels/opcode-catalog-ledger.md),
[tensor-tensor](../firmware/kernels/tensor-tensor.md)) and the NKI numpy value oracle.

The target audience is a senior C++/LLVM engineer rebuilding a Vision-Q7-compatible GPSIMD
engine and its compiler leg. The companion pages are the [Compiler Map](compiler-map.md)
(`emit_*`→opcode), the [BIR Inst roster](bir-inst-roster.md), and the
[dtype/engine fan-in synthesis](dtype-engine-fanin-synthesis.md).

> **Scope & provenance.** Every fact derives from static analysis of shipped, redistributable
> artifacts: the `neuronx-cc 2.24.5133.0+58f8de22` wheel (the Cython codegen passes — *not*
> stripped: the `__pyx_pw_`/`__pyx_pf_` method symbols and Pyx docstrings carry the original
> `SundaISel`/`InferIntrinsicOnCC`/`LegalizePartitionReduce` class+method names, plus the native
> C++ backend `libwalrus.so`), the plain-Python NKI frontend (`nki/isa/tensor_ops.py`,
> `enums.py`, the simulator value oracle), and the GPSIMD device firmware ledgers cited inline.
> All addresses are **file-relative** offsets in the `cp310` shared objects (`nm` and the IDA
> `function_addresses.json` agree to the byte) unless noted. Confidence is tagged per claim:
> **HIGH/MED/LOW** × **OBSERVED** / **INFERRED** (reasoned over OBSERVED) /
> **CARRIED** (read in a cited device page, reused).

> **GUARD.** `sunda`/`tonga`/`cayman`/`mariana`/`maverick` are the **per-generation codegen-target
> / arch-ISA axis** (sunda = CoreV2 floor / Trn1–Inf2; tonga = Inf1 legacy; cayman = CoreV3 / Trn2;
> mariana = CoreV4 / Trn3; maverick = CoreV5 / NCFW), **not** new silicon. No silicon fact is
> inferred from a compiler descriptor.

---

## 1. Executive summary — the eight verdicts

1. **SundaISel is a *supplemental* ISel, not the whole ISel.** Its docstring (byte-exact):
   *"SundaISel: Provides lowering for Sunda-specific generic penguin instructions (such as
   batch-norm) into TongaISAInst, in addition to the lowering functionality provided by
   NeuronISel. Uses NeuronIndicesAP."* It owns only the structured / GPSIMD-specific macros
   (batch-norm, softmax, rms-norm, quantize-MX, gather, generic load/store/atomic-RMW,
   collectives, offloaded FMA/memcpy). The **engine-polymorphic arith ops**
   (`tensor_tensor`/`tensor_scalar`/`memset`/`copy`/`reduce`) have **no `SundaISel.transformT*`
   method** — they flow through base `NeuronISel` and the `targets/generated/*Gen` leaf codegen.
   This resolves "*where does `tensor_tensor` get its engine?*": **not in SundaISel.** (§2, §3)

2. **The `tensor_tensor` engine decision is a *frontend* dtype/op/buffer predicate, not a
   compiler cost model.** `nki/isa/tensor_ops.py` resolves the engine *before* IR is built:
   `int32`/`uint32` `add`/`sub`/`mul` with **no PSUM operand** → GpSimd; everything else →
   Vector. There is **no shape- or cost-driven** engine switch. The opcode (`0x41`) is
   engine-invariant; engine is a *parallel* `NeuronEngine` field on the Tonga inst. (§3)

3. **`InferIntrinsicOnCC` fuses `T = c0·T0 + c1·T1 + …` into one `OffloadedFMA`.** The gate is
   `min_nonlocals_to_do_fma` (offload only when enough leaf tensors are **non-local** / spilled);
   the escape is `_internal_disable_fma_on_ios` (no FMA on graph I/O). (§4)

4. **CORRECTION — a cross-PARTITION reduce legalized by `LegalizePartitionReduce` is *not*
   `CrossLaneReduce 0x7c/0x7d`.** `op==add` → a **PE matmul against a ones-vector**
   (`matmultWithOnes`/`LowerMatmultBase`) and/or `insertSundaBNAggr`; `op!=add` → *"Currently we
   lower all non-add PartitionReduce into TransposeTensorReduce"* (a transpose + free-axis Vector
   reduce). `0x7c`/`0x7d` are the **separate, explicit** `nki.isa.tensor_partition_reduce` path
   and the strings `cross_lane`/`crosslane`/`CrossLane` are **absent** (grep count 0) from both
   `LegalizePartitionReduce.so` and `SundaISel.so`. (§5)

5. **The DGE (swdge↔hwdge) and gather-vs-indirect-DMA choices live in the generic load/store
   codegen.** `codegenGenericLoad` runs a 3-way ladder: gather (`PoolGather` → `0x68`/`0xe7`) →
   DGE indirect (`can_use_dge(…, =dge_par_min_size)` → `NeuronIndirect*` carrying
   `DGEType ∈ {SWDGE,HWDGE}`) → non-DGE indirect DMA (`0xbb`). (§6)

6. **CORRECTION/NEW — `assign_hwdge_engine` is a *native C++ backend pass in `libwalrus.so`*, not
   a SundaISel method.** `neuronxcc::backend::AssignHWDGEEngine` (`run @0x117ebe0`) runs *after*
   ISel and picks the *trigger engine* for an `InstDMA` already typed HWDGE: *"HWDGE must be on
   ACT/DVE/SP according to assign_hwdge_engine pass"*, with *"Number of valid engines for HWDGE
   must be greater than or equal to 2"*. The engine pick is **arch-gated at `ArchLevel::core_v5`**:
   below v5 → Activation, at/above v5 → DVE. (§6.5)

7. **NEW — `tensor_scalar_addr 0x74` is a 5-case firmware capability; NKI exposes only 3
   int32/uint32 projections** (the u64/i64 pointer datapath is firmware-only at this NKI
   version). The int32→GpSimd route is **bit-exact** to the device's native-32-bit-wrap add.
   (§3.4, §7)

8. **NEW — the QuantizeMX SBUF scale layout is a ×32 quadrant-strided pattern** placing 16 scales
   at SBUF partitions `0–3 / 32–35 / 64–67 / 96–99` (docstring verbatim, §8).

---

## 2. The pass roster — what SundaISel owns

`SundaISel` is a `CodeGenBase` subclass that *supplements* base `NeuronISel`. Its `transformT<Op>`
methods are BIR-pattern dispatchers; each is a thin Pyx wrapper (`__pyx_pw_`) that calls the
matching codegen class body (`__pyx_pf_`). Method bodies are byte-located via those symbols.

### 2.1 The `transformT*` dispatchers and their codegen bodies (OBSERVED)

| `SundaISel` method | dispatcher addr | codegen body | device opcode |
|---|---|---|---|
| `transformTBNStatsOperator` | — | `BNStatsCodegen.codegenBNStatsOp` | `0x61 BATCH_NORM_STATS2` (**DVE**) |
| `transformTBNAggrOperator` | — | `BNAggrCodegen.codegenBNAggrOp` | `0x62 BATCH_NORM_AGGREGATE` (**DVE**) |
| `transformTParReduceBNMeanVarOperator` | — | `ParReduceBNMeanVarCodegen.codegenBatchNormMeanVarOp` | macro |
| `transformTBNGradOperator` | — | `BNGradientCodeGen.codegenBatchNormGradientOp` | `0x94`/`0x63` (**DVE**) |
| `transformTBNBackpropOperator` | — | `BNBackpropCodeGen.codegenBatchNormBackpropOp` | `0x65 BATCH_NORM_BACK_PROP` (**DVE**) |
| `transformTSIMDOperator` | — | `SundaSIMDCodeGen` (collectives) | PSEUDO Trigger\* |
| `transformTSoftmaxOperator` / `…DxOperator` | — | `SoftmaxCodeGen.codegenSoftmaxOp` / `SoftmaxDxOp` | macro (multi-op) |
| `transformTRmsNormOperator` | — | `RmsNormCodeGen.codegenRmsNormOp` | macro (multi-op) |
| `transformTQuantizeMXOperator` | `0x3ac40` | `QuantizeMXCodeGen.codegenQuantizeMXOperator` `0x609e0` | `0x7b`/`0xe3` (§8) |
| `transformTGatherOperator` | `0x3b710` | `SundaGatherCodegenT.codegenMacro` | `0x68 GATHER` |
| `transformTGenericStoreOperator` | — | `GenericStoreCodegen.codegenGenericStore` `0xa2f10` | DGE store (§6) |
| `transformTGenericAtomicRMWOperator` | — | `ScatterCodegen.codegenGenericAtomicRMW` `0x67220` | `0x79`/`0xca` EMBED (§6.4) |
| `transformAllGatherOp` / `transformReduceScatterOp` / `transformAllReduceOp` | — | `SundaSIMDCodeGen` | PSEUDO Trigger\* (§9) |
| `transformOffloadedFMA` | `0x44f90` | `codegenTiledOffloadedMemInstr` `0x85450` | CC FMA macro (§4) |
| `transformOffloadedMemCpy` | — | `codegenTiledOffloadedMemInstr` | CC memcpy macro |

Free helpers in the same module: `should_shard_offloaded` (`0x60150`),
`codegenTiledCCOpWithRank` / `…WithoutRank`, `replaceLastAPAddrWithIndex`, `_shape`. The generic
load codegen is `SundaGenericLoadCodegen.codegenGenericLoad` (`__pyx_pf_…constprop.0` at
**`0xa6180`**; the Pyx wrapper at `0xdcff0`); `_create_scale_access_pattern` (§8) at `0x501a0`.

> **NOTE — `transformT*` is a pure dispatch thunk.** Each `transformT<Op>` is `__pyx_pw_`-only: it
> binds the BIR operator pattern and tail-calls the codegen class. The *lowering* lives in the
> codegen body, which is what this page decodes. `[HIGH/OBSERVED — symbol bodies]`

> **CORRECTION — batch-norm runs on DVE, not POOL.** The backing report's
> Inst→opcode table placed `BATCH_NORM_STATS2 0x61` / `BATCH_NORM_AGGREGATE 0x62` in a "POOL"
> column. The device opcode ledger and [batchnorm-forward](../firmware/kernels/batchnorm-forward.md)
> are unambiguous: *"the forward half of the DVE (Data/Vector Engine, `engine_idx=3`)"* — the
> BN family (`0x60`–`0x65`, `0x82`, `0x8E`, `0x94`) is **DVE**, not GPSIMD POOL. SundaISel's BN
> transforms are GPSIMD-*adjacent* macros that emit DVE ops; they are in scope here only because
> the cross-quadrant SBUF staging (§5.2) and the `ParReduceBNMeanVar` add-reduce (§5.1) touch the
> GPSIMD/POOL partition geometry. `[HIGH/OBSERVED — opcode ledger + batchnorm-forward.md]`

### 2.2 The inst hierarchy the codegen bodies build (OBSERVED string xrefs)

```
penguin BIR macro op  →  SundaISel.transformT*  →  SundaISAInst  →  TongaISAInst (TongaTensor operands)
```

`SundaISel` references `…targets.sunda.SundaISAInst`, `…targets.tonga.{TongaTensor, TongaInst,
passes.TongaISel}`, and hands legalize work to `…targets.sunda.LegalizeSundaMacro`. The leaf
opcode byte and the descriptor wire-layout come downstream in the `libwalrus` Generator. The
RmsNorm / Softmax / QuantizeMX / BatchNorm transforms are **macro decompositions** (one frontend
macro → several backend ops), not one-to-one opcode picks.

---

## 3. The engine-polymorphic decision — `tensor_tensor` / `tensor_scalar`

The "same opcode, GpSimd↔Vector chosen at ISel" question is, precisely, a **frontend dtype/op/
buffer rule** in `nki/isa/tensor_ops.py`, **plus** an engine-invariant opcode and a parallel
engine field at codegen. **There is no shape/cost ISel switch.**

### 3.1 `tensor_tensor` engine resolution (`tensor_ops.py:86–124`, OBSERVED byte-exact)

```c
// nki/isa/tensor_ops.py : tensor_tensor(dst, data1, data2, op,
//                                       engine = engine.unknown, name=None)
static Inst* tensor_tensor(Tensor dst, Tensor d1, Tensor d2, AluOp op, Engine engine) {
    if (is_bitvec_op(op))                            // and/or/xor/shift…
        return backend.tensor_tensor_bitvec(...);    // → 0x51 TENSOR_TENSOR_BITVEC, Vector
    if (op == opcode.power)                          // nl.power
        return backend.tensor_tensor_power(...);     // ALWAYS GpSimd; fp32 cast; → 0x41 (pow lane)

    // --- the ONLY auto-GpSimd trigger -------------------------------------------------
    const AluOp GPSIMD_ARITH[] = { add, subtract, multiply };
    Engine resolved = engine;                        // caller kwarg first
    if (resolved == engine.unknown
        && op in GPSIMD_ARITH
        && dst.dtype  in {int32, uint32}
        && d1.dtype   in {int32, uint32}
        && d2.dtype   in {int32, uint32}
        && !is_psum(dst.buffer)
        && !is_psum(d1.buffer)
        && !is_psum(d2.buffer))
        resolved = engine.gpsimd;                    // route to POOL
    // ----------------------------------------------------------------------------------
    return backend.tensor_tensor_arith(dst, d1, d2, op, engine=resolved);  // → 0x41
}
```

The predicate is **all-int32/uint32 `add`/`sub`/`mul`, no PSUM operand** → GpSimd; anything else
stays Vector. The caller may force Vector even for int32 via `engine=engine.vector`. Engine enum
(`enums.py:8`, OBSERVED): `tensor=1, scalar=2, gpsimd=3, dma=4, vector=5, sync=6, unknown=0`.

> **QUIRK — the docstring claims shape-based selection; the code does not.** The `engine` kwarg
> doc reads *"unknown (default, let compiler select best engine based on the input tile shape)"*
> — but the only `unknown`→non-unknown resolution in the body is the **dtype/buffer** predicate
> above. No tile shape is read. The "best engine based on shape" wording is aspirational; in this
> NKI version the route is purely dtype+op+buffer. `[HIGH/OBSERVED — code vs docstring]`

> **GOTCHA — the PSUM fallback is explicit and documented.** The docstring states: *"Since GpSimd
> Engine cannot access PSUM … the automatic GpSimd dispatch for int32/uint32 falls back to Vector
> Engine when any operand resides in PSUM."* This `!is_psum(...)` triple-guard is **the** reason a
> `nl.power` (always GpSimd) **cannot** have a PSUM operand at all, while int32 arith merely
> *de-routes* to Vector when PSUM-resident. `[HIGH/OBSERVED]`

### 3.2 `tensor_scalar` routing (`tensor_ops.py:177–290`, OBSERVED)

```c
// nki/isa/tensor_ops.py : tensor_scalar(dst, data, op0, operand0, op1=None,
//                                       operand1=None, engine=engine.unknown, …)
static Inst* tensor_scalar(...) {
    if (_matches_tensor_scalar_addr_pattern(dst, data, op0, operand0, op1, operand1))
        return backend.tensor_scalar_addr(...);      // → 0x74 TENSOR_SCALAR_ADDR  (§7)
    if (is_bitvec_op(op0))
        return backend.tensor_scalar_bitvec(...);    // → 0x53 TENSOR_SCALAR_BITVEC, Vector
    return backend.tensor_scalar_arith(..., engine=engine);  // kwarg straight through → 0x43
}
```

There is **no auto-GpSimd** for `tensor_scalar`. The engine kwarg passes through verbatim, and the
docstring restricts it: arith may run **Vector / Scalar / GpSimd**, but *"`gpsimd` (only allowed
for rsqrt)"*; **Scalar (trn2)** supports only `{mul+add, mul, add}`. So the GpSimd route here is
**opt-in and narrower** than `tensor_tensor`'s auto route.

### 3.3 The engine field is orthogonal to the opcode (codegen, OBSERVED)

`targets/Opcodes.so` defines `ALUOpcode` with **two distinct property members**: `ALUOpcode.opcode`
(getter `0x23660`, the instruction-class byte, e.g. `0x41`) and `ALUOpcode.cc_opcode` (getter
`0x22260`, the compute-cluster math-op *within* the class). `NeuronEngine` is a **separate** field
on the Tonga inst. The base `TensorTensorOpGen.get_isa_opcode` raises *"ISA opcode generation not
implemented for TensorTensorOpGen"* — the opcode comes from `ALUOpcode`. ⇒ **`0x41` is fixed;
Pool(GpSimd) vs DVE(Vector) is the engine field.** The polymorphism is structural, not an opcode
split. The mlir-tracer `_ENGINE_MAP` (`isa.py`) maps `isa_engine.{dma,sync,scalar,vector,tensor,
gpsimd}` → `NisaEngine.{DMA,Sync,Scalar,Vector,Tensor,Gpsimd}` with default fallbacks
`tensor_tensor_arith → Vector`, `memset`/`tensor_copy → Vector`, `dma → DMA`.

### 3.4 The value-model tie — int32→GpSimd is **bit-exact** to the device native-wrap add

The NKI numpy reference sim (`backends/simulator/tensor_ops.py:41–59`, OBSERVED byte-exact) makes
the engine choice **semantically visible**:

```python
def tensor_tensor_arith(dst, data1, data2, op, engine, name):
    np_op = get_numpy_binary_op(op)
    def compute(d1, d2):
        if engine == engine_enum.gpsimd:
            return np_op(d1, d2)                      # NATIVE int — 32-bit wrap, NO cast/mask
        d1_fp32 = d1 if d1.dtype == np.float32 else d1.astype(np.float32)
        d2_fp32 = d2 if d2.dtype == np.float32 else d2.astype(np.float32)
        return np_op(d1_fp32, d2_fp32)                # fp32-cast (the DVE datapath)
    _tensor_tensor_impl(dst, data1, data2, compute)
```

The GpSimd path is `np_op(d1, d2)` on the **native int dtype** — wraps mod 2³² with no mask. This
is exactly the device POOL kernel `TENSOR_TENSOR_ARITH 0x41`'s integer datapath, which the
firmware decode ([tensor-tensor](../firmware/kernels/tensor-tensor.md), the `0xC4..0xE1`
integer-engine band where `bit[7:6]==0x3` selects the int engine) shows performs **no
saturation**. ⇒ NKI-sim native-int wrap == device int-engine wrap, **bit-for-bit**. The Vector
fp32-cast path is the DVE datapath.

> **QUIRK — `tensor_tensor_power` casts even on GpSimd.** `power` is *always* GpSimd (§3.1) but its
> sim oracle **always casts to fp32** (`d1.astype(np.float32)`). So "GpSimd == native int" is the
> rule for the **int32 *arith* route only**, not for every op that lands on GpSimd. `[HIGH/OBSERVED]`

---

## 4. `InferIntrinsicOnCC` — the FMA / cast / broadcast fusion

The compute-cluster (CC = the fused GPSIMD/PE compute region) intrinsic-fusion pass. Pass
docstring (OBSERVED): *"InferIntrinsicOnCC – Find and offload the T = c0 \* T0 + c1 \* T1 … to the
[compute cluster]."* It fuses a linear combination of tensors with constant coefficients into a
single `OffloadedFMA`, rather than a chain of `tensor_scalar(mul)` + `tensor_tensor(add)`.

### 4.1 The method graph (impl addresses, OBSERVED)

| method | impl addr | role |
|---|---|---|
| `transformStmts` | `0x22d50` | drives the pass over the stmt list |
| `inferIntrinsicOnDAG` | `0x17940` | walks one expr DAG, attempts the fusion |
| `checkAndExtractRoot` | `0x1b220` | finds the root store the DAG feeds |
| `enumerate_expr_tree_leaves` | `0x134e0` | enumerates the leaf tensors of the expr tree |
| `is_intrinsic_candidate_tensor` | `0x14fd0` | classifies each leaf (`InputOrOutputOrConstOrNonLocal`) |
| `extract_scale` | `0x21ac0` | pulls the constant coefficients `c_i` out of the tree |
| `infer_fma` | `0x28a50` (pf) / `0x12e40` (pw) | the `c0·T0 + c1·T1 + …` → `OffloadedFMA` builder |
| `infer_cast` | `0x26670` | folds a dtype cast → `OffloadedMemCast` |
| `infer_broadcast_dag` | `0x147d0` (pf) / `0x14b60` (pw) | folds a partition/free broadcast → `BroadcastScalar` / `OffloadedMemCpy` |

### 4.2 The fusion gate (tuning strings, OBSERVED)

- **`min_nonlocals_to_do_fma`** — fuse to an `OffloadedFMA` **only** when at least *N* leaf tensors
  are **non-local** (live across the CC boundary / spilled). A purely local SBUF chain is left for
  Vector. This is **why** small local elementwise stays off the GPSIMD/CC datapath: the
  GPSIMD launch cost (≈150-cycle `GPSIMD_START`, [compiler-map](compiler-map.md)) is amortised only
  when the work is large / non-local.
- **`_internal_disable_fma_on_ios`** (help text, byte-exact: *"Disable generating FMA operations on
  input or output tensors"*; both the underscore form `_internal_disable_fma_on_ios` and the dash
  form `internal-disable-fma-on-ios` are interned) — disables FMA generation on graph
  **INPUT/OUTPUT** tensors.
- Counters emitted: `num_fma_inferred`, `num_src_nonlocal`, `num_dst_nonlocal`,
  `num_broadcast_scalar`, `num_nonlocals`.

`SundaISel.transformOffloadedFMA` / `transformOffloadedMemCpy` + `codegenTiledOffloadedMemInstr` +
`should_shard_offloaded` then lower these `OffloadedFMA`/`MemCast`/`MemCpy` intrinsics into the
actual TPB stream — sharded across cores by `should_shard_offloaded` when large (§9).

> **NOTE — `infer_cast`/`infer_broadcast_dag` are the *non-FMA* products of the same DAG walk.** A
> leaf that is a pure dtype cast folds into `OffloadedMemCast`; a partition/free broadcast folds
> into `BroadcastScalar` or `OffloadedMemCpy`. The three inference paths share
> `enumerate_expr_tree_leaves` + `is_intrinsic_candidate_tensor`. `[HIGH/OBSERVED]`

---

## 5. `LegalizePartitionReduce` — the cross-PARTITION reduce (CORRECTION)

Pass docstring (OBSERVED, byte-exact, stored as three lines): *"Pass that legalizes tensor reduce
and batchnorm reduce instructions that reduce across partitions. / Currently we lower all non-add
PartitionReduce into TransposeTensorReduce, / and we lower all ParReduceBNMeanVar into
TransposeBatchnormStats"* (note the trailing comma on line 2, no period on line 3).

> **CORRECTION.** A cross-PARTITION reduce legalized by this pass is **not** lowered to GpSimd
> `CrossLaneReduce 0x7c/0x7d`. The strings `cross_lane`/`crosslane`/`CrossLane` are **absent**
> (literal grep count **0**, case-insensitive) from both `LegalizePartitionReduce.so` and
> `SundaISel.so`. `0x7c`/`0x7d` are the **separate, explicit** `nki.isa.tensor_partition_reduce` op
> (§5.3). `[HIGH/OBSERVED]`

### 5.1 The dispatch (`transformPartitionReduceOp`, body @ **`0x35020`**, wrapper @ `0x48420`, OBSERVED)

```c
// LegalizePartitionReduce.transformPartitionReduceOp  (pf @0x35020; pw @0x48420)
void transformPartitionReduceOp(Inst* pr) {
    AluOp op = pr.op;                                  // .py line 594
    if (op.opcode == "add") {                          // unicode-eq fast path
        // sum-across-partitions = matmul(ones[P,1]^T, X) on the PE array
        transformPartitionReduceOpAdd(pr);             // pw @0x2b6c0; .py line 584
        //   body fetches base-class attrs `matmultWithOnes` / `LowerMatmultBase`
        //   (name-consts @0x72730 / @0x726c0) and/or insertSundaBNAggr (pw @0x537c0)
    } else {
        // "Currently we lower all non-add PartitionReduce into TransposeTensorReduce"
        build TransposeTensorReduceOp(pr);             // partition↔free transpose (PE/DVE), then
        //   a free-axis TensorReduce on Vector via NeuronReduceMacro /
        //   PartitionReduceVectorizer, spilling to PSUM as needed.
    }
    // "and we lower all ParReduceBNMeanVar into TransposeBatchnormStats"
    //   (transformParReduceBNMeanVar pf @0x56cf0 / pw @0x6c6c0 → TransposeBatchnormStats2)
}
```

> **NOTE — `matmultWithOnes` / `LowerMatmultBase` are *not* methods of this module.** They are
> referenced **name constants** (`__pyx_k_matmultWithOnes` @ `0x72730`, `__pyx_k_LowerMatmultBase`
> @ `0x726c0`) fetched at runtime via `GetAttrStr` from a base class defined in another module. So
> the add-path "sum-across-partitions via PE matmul-vs-ones" is named here but the matmul lowering
> lives in the shared lower-base. `[HIGH/OBSERVED — `transformPartitionReduceOpAdd` body GetAttr]`

The dispatch branch is OBSERVED in the decompile: `op = inst.op` then `op.opcode` compared against
the interned `"add"`; on match → `transformPartitionReduceOpAdd`, else → `TransposeTensorReduceOp`.
Confirmed symbols/flags: `insertSundaBNAggr`, `canVectorizeSundaBNAggr`,
`PartitionReduceVectorizer` (`canVectorize`/`canVectorizeDVETransposeInst`/`vectorizeInst`),
`NeuronReduceMacro`, `enable_transpose_reduce`, `fold_loop_reduce`, `max_partitions`,
`max_coalesce_factor`, `findCoalesceAxes`(+`coalesce_priority`), `tileSrcParAxes`,
`vectorizeRemainingReduceAxes`, `spill_psum_tensor`, `find_psum_spill_point`,
`compute_psum_max_elts_for_transpose`.

### 5.2 The quadrant model (OBSERVED strings)

`crossQuadrantTensorCopy` / `CrossQuadrantTensorCopyOp` (leaf gen `CrossQuadrantTensorCopyOpGen`)
move data across the **32-partition-per-quadrant** SBUF banks before `BNAggr`. The byte-exact
note: *"if quadrant_axis=None, then no cross quadrant memcopy was inserted - we still need BNAggr
in …"*; *"ap_addrs of quadrant dim should be const."* This is the same 32-per-quadrant geometry
the QuantizeMX scale layout exploits (§8).

### 5.3 The two distinct partition-reduce entries — reconciliation

There are **two** ways a partition-axis reduce reaches the device, and they are *different
entries* — this is the nuance the §5 correction sharpens:

| entry | trigger | legalizer | device opcode | engine |
|---|---|---|---|---|
| **explicit NKI** | `nki.isa.tensor_partition_reduce(dst, op, data)` | *none* — direct emit | `0x7c`/`0x7d` `CrossLaneReduce` (`S4D4_CR`) | GpSimd POOL |
| **BIR macro** | `InstPartitionReduce` (structured reduce over the partition axis) | **`LegalizePartitionReduce`** | `0x02` Matmul(vs-ones) + `0x62` (add) / Transpose + `0x42` `TENSOR_REDUCE` (non-add) | PE + DVE/Vector |

The device [tensor-reduce](../firmware/kernels/tensor-reduce.md) page documents the **explicit**
leg (*"`nki.isa.tensor_partition_reduce` → `emit_cross_lane_reduce_arith/_bitvec` → `0x7c`/`0x7d` →
`clr_reduce_local`"*). The compiler `LegalizePartitionReduce` pass handles the **macro** leg only,
and it never touches `0x7c`. There is **no disagreement** between the two pages once the two
entries are separated. `[HIGH — explicit leg CARRIED tensor-reduce.md; macro leg OBSERVED]`

### 5.4 Consequence for the opcode picture

The macro cross-partition reduce produces `{Matmul 0x02 (vs-ones), Transpose, TENSOR_REDUCE 0x42,
BATCH_NORM_AGGREGATE 0x62}`, **not** a GpSimd opcode. `0x42 TENSOR_REDUCE_ARITH_OP` is the
**free-axis** reduce (POOL/DVE), and `0x52` its bitvec sibling — both forbid GpSimd on their engine
predicate (see [tensor-reduce](../firmware/kernels/tensor-reduce.md) §6 engine-predicate note).

---

## 6. The `can_use_dge` / swdge↔hwdge selector + the gather 3:1 split

Both the DGE choice and the gather-vs-indirect-DMA choice live in the **generic load/store
codegen** — `SundaGenericLoadCodegen.codegenGenericLoad` (`0xa6180`) and
`GenericStoreCodegen.codegenGenericStore` (`0xa2f10`). String xrefs `can_use_dge`,
`dge_par_min_size`, `enable_dge_on_indirect_dma`, `can_lower_generic_load_to_gather`,
`get_indirect_dim_and_value`, `must_be_indirect_dma`, `PoolGather`, `IndirectCopy`,
`NeuronIndirect*` all resolve to these two bodies.

### 6.1 The decision ladder (`codegenGenericLoad`, OBSERVED call sequence from the decompile)

```c
// SundaGenericLoadCodegen.codegenGenericLoad  (pf @0xa6180; pw @0xdcff0)
void codegenGenericLoad(Inst* load) {
    auto [indir_dim, idx_tensor, trip, npart] = self.load.get_indirect_dim_and_value(...); // (1)

    // (2) gather eligibility — guarded by an isinstance(self.load.tensor, NeuronLocalTensor)
    //     branch; the bound method is fetched in BOTH arms (if/else clones of one check)
    if (self.cg.target.can_lower_generic_load_to_gather(load, indir_dim, npart)) {
        emit PoolGather(...);  // leaf PoolGatherGen → GATHER 0x68 / INDIRECT_COPY 0xe7
        assert("PoolGather could only support single partition");      // guard
        assert("Incorrect transformation in PoolGather");              // sanity
        return;
    }

    // (3) the INDIRECT-DMA path
    if (self.cg.target.enable_dge_on_indirect_dma                       // feature flag
        && can_use_dge(self.load, ..., =self.cg.target.dge_par_min_size)) {  // ← see NOTE
        emit NeuronIndirectLoad(... , DGEType = SWDGE | HWDGE);         // HWDGE: see §6.5
    } else {
        // must_be_indirect_dma is INTERNED but never invoked — the fall-through IS the
        // non-DGE indirect DMA:
        emit /* non-DGE indirect DMA */ DMA_INDIRECT 0xbb;             // descriptors CPU-precomputed
    }
    // a later, separate gather-codegen block (after self.projectPredicates) RE-READS
    // can_lower_generic_load_to_gather + get_indirect_dim_and_value — those are not new gates.
}
```

> **CORRECTION — `dge_par_min_size` is a *keyword-argument value of* `can_use_dge`, not a separate
> sequential gate.** The backing report's §6.1 listed `dge_par_min_size` as an independent
> ladder step (`enable_dge_on_indirect_dma AND dge_par_min_size AND can_use_dge`). The decompile
> shows `can_use_dge` is a **module-level free function** (resolved by global/builtin lookup), and
> `dge_par_min_size` (`self.cg.target.dge_par_min_size`) is built into its **kwargs dict** at the
> call site. So the real test is `enable_dge_on_indirect_dma && can_use_dge(load, …,
> =dge_par_min_size)` — two gates, with the threshold *inside* the second. The same
> `enable_dge_on_indirect_dma → can_use_dge → dge_par_min_size` triple appears in
> `codegenGenericStore` (`0xa2f10`). `must_be_indirect_dma` is interned in the string table but
> **never called** anywhere in `SundaISel.so` — the non-DGE path is the plain `else` fall-through.
> `[HIGH/OBSERVED — codegenGenericLoad decompile body 0xa6180]`

### 6.2 The DGE enum + backend mapping (OBSERVED across artifacts)

`enums.py:52` `dge_mode`: `unknown=0, swdge=1, hwdge=2, none=3`. The backend `DGEType` lives at
`bir::InstDMA + 0xF8` with the **same** values (`None=0, SWDGE=1, HWDGE=2, Unassigned=3` ctor
sentinel — [dge-builder-qos](../dma/dge-builder-qos.md)); the `libwalrus` asserts reference
`DGEType::{HWDGE, SWDGE, Unassigned}` directly. The mlir-tracer `_DGE_MODE_MAP` (`isa.py`) maps
`none→NoDGE, swdge→SWDGE, hwdge→HWDGE, unknown→Unassigned`. The frontend surface:
`dma_copy(dst, src, oob_is_err, dge_mode, engine)`; `dma_compute` defaults **SWDGE**; the `.pyi`
notes *"HWDGE is only supported for NeuronCore-v3+."* The companion `dma_engine` enum
(`enums.py:85`) is `dma=1` (shared, CoreBarrier-synced, any-engine trigger) / `gpsimd_dma=2`.

The device-side three-way backend select ([dge-backend-selector](../firmware/dge/dge-backend-selector.md),
[dge-builder-qos](../dma/dge-builder-qos.md)) is `Pool (2-dim, fw)` / `RTL (5+2-dim, HW = HWDGE)` /
`software (4-dim, Q7 = SWDGE)`, chosen from a runtime-populated BSS availability table — *"HWDGE
(the RTL backend) and the whole selector arrive with CAYMAN"* (= CoreV3).

### 6.3 The threshold heuristic, grounded at a call site (OBSERVED)

`nki/_pre_prod_kernels/moe_token_gen.py:583` (and `:592`):

```python
nisa.dma_copy(
    src=expert_affinities[i_p_affi, expert_offset + i_f_affi],
    dst=expert_affinities_masked[i_p_affi, i_f_affi],
    dge_mode=dge_mode.unknown if T % 16 == 0 else dge_mode.swdge,   # ← the 16-align gate
)
```

When the partition count `T` is **16-aligned**, the kernel passes `dge_mode.unknown` (→
`Unassigned` → `can_use_dge` resolves HW/SW DGE freely); otherwise it **forces `swdge`**. This is
the human-visible counterpart of the `dge_par_min_size` threshold inside `can_use_dge`: HWDGE wants
16-element-aligned partition tiles; the ragged tail falls back to SWDGE.

> **CORRECTION — the obfuscated `.n` of the backing report is plainly `.swdge`.** An earlier reading
> read the else-branch member as an obfuscated `dge_mode.n`. The plain-Python source shows it is
> literally `dge_mode.swdge`. The scalar single-element copy at `moe_token_gen.py:572` uses
> `dge_mode.none` (no DGE at all). `[HIGH/OBSERVED]`

### 6.4 The gather split — three opcodes from one generic-load BIR inst (OBSERVED)

| ladder outcome | device opcode | struct / kernel | index width |
|---|---|---|---|
| `can_lower_generic_load_to_gather` true, single partition | `0x68 GATHER` | `S4D4_GT` / `pool_gather @0x01002590` | u8/u16/u32 |
| indexed-copy gather (8-core/16-part) | `0xe7 INDIRECT_COPY` | `S4D4_IC` / `pool_indirect_copy @0x01000748` | u16, ≤4096 |
| DGE / non-DGE indirect DMA | `0xbb DMA_INDIRECT` | `DMA_INDIRECT1D` / `dma_memcopy_indirect @0x0100474c` | by-index |

So **a single generic-load BIR inst can legalize to three different opcodes (`0x68`/`0xe7`/`0xbb`)**
by the §6.1 ladder. The scatter-add sibling is reached via `ScatterCodegen.codegenGenericAtomicRMW`
(`0x67220`) → `NeuronIndirectRMW`.

> **CORRECTION — embedding-update is `0x79` (raw SEQ) *or* `0xca` (pseudo-struct), depending on
> stage.** An earlier reading attributed `ScatterCodegen` to `0x79 EMBEDDING_UPDATE`. The device
> [indirection-gather](../firmware/kernels/indirection-gather.md) page corrects the binding: the raw
> SEQ opcode is **`0x79`** (handler `'y'`, `pool_embedding_update @0x01008520`), but the
> customop/compiler-facing struct `PSEUDO_EMBEDDING_UPDATE_STRUCT` maps in `struct2opcode` to
> **`0xca`**, not `0x79`. Both bytes describe the same embedding scatter-reduce at different
> lowering stages. `[HIGH/OBSERVED — indirection-gather.md]`

### 6.5 `assign_hwdge_engine` — the native libwalrus backend pass (NEW)

The task scope named `assign_hwdge_engine`. It is **not** a SundaISel method — it is a **native C++
backend pass in `libwalrus.so`**, `neuronxcc::backend::AssignHWDGEEngine`, running *after* ISel.
SundaISel's job is only to set `DGEType ∈ {SWDGE, HWDGE}` on the `InstDMA` (via `can_use_dge`, §6.1);
`AssignHWDGEEngine` then picks **which trigger engine** issues the HWDGE descriptor.

| symbol | addr | binding |
|---|---|---|
| `neuronxcc::backend::AssignHWDGEEngine::run(bir::Module&)` | `0x117ebe0` | T (pass entry) |
| `neuronxcc::backend::AssignHWDGEEngine::run(std::vector<unique_ptr<Module>>&)` | `0x117fe00` | T (module-vector overload) |
| `neuronxcc::backend::AssignHWDGEEngineImpl::assignEngine(bir::InstDMA&)` | `0x1180360` | W (per-DMA engine pick) |
| `bir::isHWDGEDMAWithEngineSet(const bir::Instruction*)` | (imported, U) | the "already-assigned" predicate |

Recovered constraints (byte-exact strings):

- *"HWDGE must be on ACT/DVE/SP according to assign_hwdge_engine pass"* — the **valid trigger
  engine set** for HWDGE is `{ACT, DVE, SP}` (not GpSimd/PE).
- `ArchLevl < ArchLevel::core_v5 ? Engine.getType() == EngineType::Activation : Engine.getType()
  == EngineType::DVE` — the **engine pick is arch-gated at core_v5**: below v5 → Activation,
  at/above v5 → DVE.
- *"Number of valid engines for HWDGE must be greater than or equal to 2"* — needs ≥2 of those
  engines available to schedule HWDGE.
- *"dma_copy engine can only be specified when HWDGE mode is used."* — the `engine=` kwarg on a
  `dma_copy` is meaningful only under HWDGE.
- *"NumHWDGE <= board.device.core.RtHwdgeQueueLimit"* — the live HWDGE count is capped by the
  runtime HWDGE queue limit.
- `queue->getIsHWDGE() && queue->getParent()->getArchLevel() >= ArchLevel::core_v5` — the HWDGE
  *trigger-engine queue scheduling* (`enable-hwdge-trigger-engine-scheduling`) gate.
- source path: `…/neuronxcc/walrus/assign_hwdge_engine/src/assign_hwdge_engine.cpp:22`.

> **NOTE — two HWDGE arch thresholds, not one.** HWDGE *capability* (the RTL descriptor backend)
> arrives with **CAYMAN = CoreV3** (.pyi *"NeuronCore-v3+"*, the device backend selector). The
> HWDGE *trigger-engine* placement and queue scheduling in `AssignHWDGEEngine` is gated at
> **core_v5** (Activation below v5, DVE at/above v5; the queue predicate above). Conflating them
> would mis-state the minimum arch for plain HWDGE. `[HIGH/OBSERVED — libwalrus strings; CARRIED
> .pyi + selector]`

---

## 7. `tensor_scalar_addr` (`0x74`) — the 5-firmware-case vs 3-NKI-case differential

`tensor_scalar` intercepts an **address-math pattern** before the generic `0x43`: if matched, it
lowers to `TENSOR_SCALAR_ADDR 0x74` (leaf `TensorScalarPtrOpGen` / `TensorScalarGEPOpGen`), the
index→address staging op that feeds gather (device `pool_tensor_scalar_addr @0x01001904`, "HW
gather, 21 sites").

### 7.1 The NKI intercept (`_matches_tensor_scalar_addr_pattern`, `tensor_ops.py:132–169`, OBSERVED — 3 cases)

```c
bool _matches_tensor_scalar_addr_pattern(dst, data, op0, operand0, op1, operand1) {
  // Case A: (int32 * int32) + int32 = int32       (full scale + offset)
  if (src==i32 && dst==i32 && op0==multiply && op1==add
        && is_scalar(operand0) && is_scalar(operand1))   return true;
  // Case B: int32 * int32 = int32                 (scale only)
  if (src==i32 && dst==i32 && op0==multiply && op1==None && is_scalar(operand0)) return true;
  // Case C: (uint32 - uint32) < uint32 = uint8    (range check on 32-bit addr)
  if (src==u32 && dst==u8  && op0==subtract && op1==less
        && is_scalar(operand0) && is_scalar(operand1))   return true;
  return false;
}
```

### 7.2 The firmware capability (CARRIED [opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md), 5 cases A–E over `{u32,i32,u64,i64}`)

| | firmware form | ops |
|---|---|---|
| A | `addr<u64> = base<u64,imm1> + index<i32,src> * elem<i32,imm0>` | mul, add |
| B | `addr<u64> = base<u64,imm0> + scaled_index<i32,src>` | add, bypass |
| C | `scaled<i32> = scale<i32,imm0> * index<i32,src> [+ imm1]` | mul, bypass\|add |
| D | `in_range = (addr<u64,src> - min<u64,imm0>) < size<u64,imm1>` | u64 range |
| E | same as D but u32 addr/imm | u32 range |

### 7.3 The differential

The firmware does **true 64-bit address math** (A/B form u64 pointers; D a u64 range mask). NKI
exposes only the **32-bit projections**: NKI-A ≈ firmware-A typed i32 (no u64 base); NKI-B ≈
firmware-C; NKI-C ≈ firmware-E (u32→u8). The u64/i64 pointer datapath is **firmware-only at this
NKI version** — a host-surface restriction, not a capability gap.

### 7.4 `0x93 TRANSPOSE_TENSOR_SCALAR` (CARRIED)

`0x93 TRANSPOSE_TENSOR_SCALAR_ARITH_OP` shares the `0x43` TensorScalar datapath plus two extra
constraints (a transpose check + 32-aligned channel count), and has **no NKI emit** — a
legalize/macro-only opcode, the transposed sibling of `0x43`. The device `tensor-scalar.md` page
confirms `has_tensor_scalar_opcode` accepts `{0x43, 0x53, 0x93}`. So the tensor_scalar family is
the `0x43`/`0x53`/`0x74`/`0x93` cluster with only `0x43`/`0x53`/`0x74` frontend-reachable.

---

## 8. The QuantizeMX scale-placement layout (the MX leg)

`QuantizeMXCodeGen._create_scale_access_pattern` (impl `0x501a0`) docstring, byte-exact:

> *Create strided access pattern for scale tensor on SBUF. The scale tensor needs a strided layout
> to place scales in the correct hardware quadrants. SBUF has 32 partitions per quadrant, and scales
> must be placed in the quadrant from which their source data originated. For partition_axes =
> [scale_in_quadrant_idx, quadrant_idx]: — Standard linear: scale_in_quadrant \* 1 + quadrant \* 4 —
> Strided for SBUF: scale_in_quadrant \* 1 + quadrant \* 32. This places 16 scales at positions: 0-3,
> 32-35, 64-67, 96-99.*

The backend `QuantizeMXOp` shape contract (docstring): `Output[0]` = quantized data, **FP8_x4** (4
elems packed per `FP8_x4` word; `reduce_free_axes` tripcount = 4); `Output[1]` = Scale, **uint8**.
`par_indices = [reduce_partition_indices…, partition_indices…]` (innermost first): `par_indices[0]`
= pack axis (inner reduction, tripcount = 8, skipped for scale part); `par_indices[1:]` = scale
block axis (tripcount = 16).

⇒ This is the host-side MX block layout the device's `proc_4bit_mx_8` ([mx-dequant](../firmware/kernels/mx-dequant.md))
inverse-dequant (`TensorDequantize 0x7b` / `QuantizeMx 0xe3`) consumes. The **×32 quadrant stride**
(vs the ×4 logical) is the SBUF-bank scatter that keeps each 16-scale block co-resident with its
source quadrant. (Same 32-per-quadrant geometry as §5.2.)

---

## 9. The collective-op ISel (the GPSIMD/SP spine)

`SundaSIMDCodeGen.codegenElementwiseAllReduceOp` / `codegenElementwiseAllGatherOp` build a
`CollectiveComputeOp` (guard: *"expect only single operand and single output for
CollectiveComputeOp!"*); `transformAllGatherOp` / `transformReduceScatterOp` /
`transformAllReduceOp` are the dispatchers; the leaf gens are `TiledAllGatherOpGen` /
`TiledAllReduceOpGen`. Sharding is decided by **`should_shard_offloaded`** (`0x60150`) using
`num_neuroncores_per_sengine` / `num_src_partitions` / `npartitions` (the per-core × partition
geometry). The tiled-elementwise path is restricted (*"Only AllGatherOp and ReduceScatterOp is
supported for now!"*; `allgather_src_free_axes` computes per-core free-axis tiling). The PSEUDO
`TriggerCollective` / CCE / DGE descriptor emission is downstream in the backend Generator; the
ISel leg here handles only the **entry** (`CollectiveComputeOp` + sharding heuristic).

---

## 10. The GPSIMD-relevant BIR Inst\* → SundaISAInst subset

The GPSIMD-touching slice of the penguin BIR `Inst*` set. "→" = the SundaISel transform /
generated-gen that lowers it; opcode CARRIED from the [opcode catalog ledger](../firmware/kernels/opcode-catalog-ledger.md);
engine per the device ledger (POOL = GPSIMD compute core; DVE = Vector; PE = matmul array).

| BIR macro / generic op | SundaISel / gen leg | TPB opcode | engine |
|---|---|---|---|
| `InstTensorTensor` (arith) | base `NeuronISel` + `TensorTensorOpGen`; `ALUOpcode`+engine field | `0x41` | Pool \| DVE |
| `InstTensorTensor` (bitvec) | base + `TensorTensorOpGen` | `0x51` | DVE |
| `InstTensorScalar` (arith) | base + `TensorScalarOpGen` | `0x43` | Pool \| DVE \| ACT |
| `InstTensorScalar` (addr-pattern) | `TensorScalarPtr/GEPOpGen` | `0x74 TENSOR_SCALAR_ADDR` | Pool |
| `InstTensorScalarSelect` (affine) | `AffSelTensorScalarOpGen` | `0x92 AFFINE_SELECT` | Pool |
| `InstGenericLoad` (gatherable) | `codegenGenericLoad` → `PoolGather` | `0x68 GATHER` | Pool |
| `InstGenericLoad` (indirect copy) | `codegenGenericLoad` → `IndirectCopy` | `0xe7 INDIRECT_COPY` | Pool |
| `InstGenericLoad` (DGE indirect) | `codegenGenericLoad` → `NeuronIndirect*` (`DGEType`) | `0xbb DMA_INDIRECT` | NX/DMA |
| `InstGenericAtomicRMW` | `ScatterCodegen` → `NeuronIndirectRMW` | `0x79` (SEQ) / `0xca` (pseudo) EMBED | Pool/DMA |
| `InstPartitionReduce` (add) | `transformPartitionReduceOpAdd` / `insertSundaBNAggr` | `0x02 Matmul`(vs-ones) + `0x62 BN_AGGREGATE` | PE + DVE |
| `InstPartitionReduce` (non-add) | → `TransposeTensorReduceOp` | Transpose + `0x42 TENSOR_REDUCE` | PE/DVE + DVE |
| `InstQuantizeMX` | `QuantizeMXCodeGen` | `0x7b` (dequant) / `0xe3` (fwd) | Pool |
| `InstBatchNormStats` | `BNStatsCodegen` | `0x61 BN_STATS2` | **DVE** |
| `InstBatchNormAggr` | `BNAggrCodegen` | `0x62 BN_AGGREGATE` | **DVE** |
| `InstRmsNorm` / `InstSoftmax` | `RmsNorm`/`SoftmaxCodeGen` | macro (multi-op) | mixed |
| `InstCrossLaneReduce` (explicit) | direct emit (**not** via §5) | `0x7c`/`0x7d CROSS_LANE` | Pool |
| `InstScalarTensorTensor` | base + gen | `0x9d`/`0x9e STT` | Pool |
| `InstSelectReduce` | `SelectReduceGen` | `0xea SELECT_REDUCE` | Pool |
| `InstNonzeroWithCount` | `NonzeroWithCount` | `0xf2 NONZERO_WITH_COUNT` | Pool |
| `InstTensorScalarCacheReduce` / `…Cumulative` | `TensorScalarCacheReduce`/`Cum` | `0x9a` / `0xe6` | Pool |
| `InstTensorTensorScan` | `TensorTensorScanOpGen` | `0xe5 TT_SCAN` | Pool |
| `InstAllReduce`/`AllGather`/`ReduceScatter` | `SundaSIMDCodeGen` → `CollectiveComputeOp` | PSEUDO Trigger\* | SP/CCE |
| `InstOffloadedFMA` (synth §4) | `transformOffloadedFMA` | CC FMA macro | Pool/PE |

`[engine columns: HIGH/OBSERVED for BN=DVE, affine_select=Pool, tt/ts=Pool\|DVE; opcode numerics
CARRIED opcode-catalog-ledger]`

---

## 11. Cross-check ledger — ISel pick vs firmware decode vs value model

| ISel opcode pick | firmware decode | value model |
|---|---|---|
| `0x41 TENSOR_TENSOR_ARITH` (eng=Pool, int32 route) | POOL int-engine band `0xC4..0xE1` (`bit[7:6]==0x3`) | native 32-bit wrap — **bit-exact** to `np_op(int)` |
| `0x43 TENSOR_SCALAR_ARITH` | POOL (`S3D3_TS`) | DVE fp32 / ACT splat / Pool |
| `0x74 TENSOR_SCALAR_ADDR` (NKI 3-of-5 subset) | POOL, 5 cases (`ts_addr_valid`) | integer addr math (no arith leaf; addr stage) |
| `0x92 AFFINE_SELECT` (≠ `0x98 SELECT`) | POOL (Iota ramp → cmp-vs-`0` → vbool blend) | position-masked select predicate |
| `0x68 GATHER` / `0xe7 IND_COPY` / `0xbb DMA_INDIRECT` | POOL family (`pool_gather`/`pool_indirect_copy`/`dma_memcopy_indirect`) | within-part u32 / 8c-16p u16 / by-index |
| `0x79`(SEQ)/`0xca`(pseudo) EMBED_UPDATE | POOL `pool_embedding_update` | scatter-reduce |
| `0x02 Matmul`(vs-ones) | PE | cross-part sum trick |
| `0x42 TENSOR_REDUCE` | POOL/DVE | free-axis reduce (GpSimd-forbidden) |
| `0x7b`/`0xe3 QuantizeMX` | POOL `proc_4bit_mx_8` | 16-scale ×32-quadrant block |
| `DGEType SWDGE/HWDGE` → `AssignHWDGEEngine` (ACT/DVE/SP) | DGE descriptor emit / RTL backend (CAYMAN+; trigger v5) | n/a (descriptor path) |

**Verdict:** every ISel opcode pick is in the POOL/DVE/PE roster with the matching engine; the
int32→GpSimd route is bit-exact to the device native-wrap integer datapath; the §5 partition-reduce
correction and the §2.1 batch-norm-is-DVE correction remove the two engine over-attributions in the
backing report.

---

## 12. Corrections vs the backing report and the device pages

| # | correction | basis |
|---|---|---|
| C1 | `LegalizePartitionReduce` does **not** emit `0x7c/0x7d`; add → matmul-vs-ones / non-add → transpose+reduce; `cross_lane` grep count **0** in both passes | OBSERVED §5 |
| C2 | batch-norm (`0x61`/`0x62`/`0x65`/`0x82`/`0x94`) runs on **DVE**, not POOL; the backing-report table's "POOL" column was wrong | OBSERVED opcode ledger + batchnorm-forward.md |
| C3 | `assign_hwdge_engine` is a **libwalrus C++ backend pass** (`AssignHWDGEEngine @0x117ebe0`), not a SundaISel method; trigger engine ∈ `{ACT,DVE,SP}`, NumHWDGE ≥ 2; **engine pick arch-gated at core_v5** (Activation below v5, DVE at/above) | OBSERVED §6.5 |
| C4 | `dge_par_min_size` is a **keyword-arg value of `can_use_dge`**, not a standalone ladder gate; `can_use_dge` is a module-level free function; `must_be_indirect_dma` is interned but **never called** | OBSERVED §6.1 |
| C5 | the `moe_token_gen.py` else-branch DGE member is plainly `dge_mode.swdge` (not an obfuscated `.n`); scalar copies use `dge_mode.none` | OBSERVED §6.3 |
| C6 | embedding-update binds **`0x79`** (raw SEQ) *or* **`0xca`** (`PSEUDO_EMBEDDING_UPDATE_STRUCT` via `struct2opcode`), by lowering stage | OBSERVED §6.4 + indirection-gather.md |

---

## 13. See also

- [Compiler Map](compiler-map.md) — the `emit_*`→opcode table and the 4-stage codegen map.
- [BIR Inst roster](bir-inst-roster.md) — the full penguin `Inst*` set this page subsets.
- [dtype/engine fan-in synthesis](dtype-engine-fanin-synthesis.md) — the bf16 / wide-int dtype
  routing that parallels the §3.1 int32→GpSimd rule.
- [tensor-tensor](../firmware/kernels/tensor-tensor.md) — the device decode of the `0x41`/`0x51`
  POOL kernel the engine choice lands on.
- [dge-backend-selector](../firmware/dge/dge-backend-selector.md) — the device-side
  Pool/RTL/software DGE backend pick.
