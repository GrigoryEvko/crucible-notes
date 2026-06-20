# The GPSIMD-Relevant Compiler Map + `emit_*`→opcode

This page is the **compiler-seam opener** for the GPSIMD device guide. It maps the
host compiler stack — **HLO → BIR → SundaISel → Generator → NEFF** — but only along
the legs that reach the Cadence Vision-Q7 NX ("Cairo") **POOL** cluster, and it binds
every host-side `emit_*` IR-builder entry point to the numeric **TPB opcode** the
firmware decodes on the device.

It owns **only the GPSIMD seam**. The full compiler internals (every BIR pass, the
walrus descriptor emitter, the scheduler cost model) live in the separate neuronx-cc
wiki — cross-linked inline as a path, not a hyperlink, at
`neuronx-cc/wiki/src/...`. For the device-side opcode roster this page binds against,
see [../firmware/kernels/opcode-catalog-ledger.md](../firmware/kernels/opcode-catalog-ledger.md);
for the ISel target dialect see [sundaisel.md](sundaisel.md); for the BIR node set see
[bir-inst-roster.md](bir-inst-roster.md); for the dtype→engine routing see
[dtype-engine-fanin-synthesis.md](dtype-engine-fanin-synthesis.md).

**Anchors.** Every claim is tagged `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` and
anchored to a symbol, string, file offset, or opcode. The two corpora:

```
CC  = neuronx-cc/extracted/neuronx_cc-2.24.5133.0+58f8de22-cp311-cp311-linux_x86_64/neuronxcc/
NKI = neuronx-misc/extracted/nki-0.3.0+23928721754.g18aa1271-cp311-cp311-linux_x86_64/nki/
```

cp310/cp311/cp312 wheels are byte-identical in the Python layer; the cp311 copy is
cited throughout. The Cython `.so` are **not stripped** — the class/method names below
are read directly from their symbol tables.

> **NOTE — the `sunda`/`tonga`/`cayman`/`mariana`/`maverick` axis is the codegen-TARGET
> axis, not silicon.** `sunda`=CoreV2 (Trn1/Inf2 floor), `tonga`=Inf1 legacy,
> `cayman`=CoreV3/Trn2, `mariana`=CoreV4/Trn3, `maverick`=CoreV5/NCFW. The shipped
> `isa_tpb` table mined here is the `sunda` floor. No silicon-generation fact is
> inferred from any compiler descriptor. [HIGH/OBSERVED — the target tokens appear as
> directory names under `CC/isa_tpb/` and `CC/starfish/penguin/targets/`.]

---

## 1. The 4-stage lowering — every stage a named artifact

A GPSIMD instruction in the final NEFF is the output of a four-stage host pipeline.
Both frontends (NKI direct, and XLA-HLO) converge on the same penguin BIR and the same
backend, so the opcode set below is the **complete** GPSIMD surface for both paths.

| Altitude | Artifact (OBSERVED symbol/path) | Produces |
|---|---|---|
| HLO frontend | `CC/starfish/bin/hlo2penguin`; `penguin/transforms/{Frontend,CanonicalizeIR,TargetLowering}.so` | XLA-HLO → penguin BIR |
| NKI frontend | `NKI/backends/mlir_tracer/{isa.py,isa_emit.py}` + `NKI/compiler/_internal/_mlir_libs/_nki_irbuilder.*.so`; `CC/.../sunda/passes/NKICodeGenFlow.so`, `InlineNKIKernels.so` | NKI → NISA-MLIR → penguin BIR |
| Penguin BIR (IR) | `CC/starfish/lib/libBIR.so` (`bir::Inst*`); `CC/generated/starfish/birpy/{Opcodes,InstructionOpcodes}.so` | ~119 `Inst*` opcodes |
| Lower / legalize | `penguin/transforms/Lower*.so` (`LowerTensorOp`, `LowerIntrinsics`, `LowerPartitionTile`, `LowerBroadcast`, `LowerTranspose`, `LowerToSendRecv`, `LowerCCOpBlockAxis`, `LowerShardAxis`) | generic BIR → target BIR |
| Sunda ISel | `CC/.../sunda/passes/SundaISel.so`; `targets/sunda/SundaISAInst.so` | BIR → `SundaISAInst` ISA insts |
| Tiling / layout | `SundaSizeTiling.so`; `transforms/{LayoutTilingPipeline,FlattenAxesForTiling,TileCCOps,PGAnalysisForTiling}.so` | tiled, partition-assigned insts |
| Scheduling | `targets/tonga/passes/Scheduler.so`; `InstructionLatencyModel.so`, `KernelLatencyInfo.so` | ordered inst stream |
| Memory planning | `transforms/{AllocateBlocks,ModuloAllocation,MaxLiveSpiller,SpillPSum,NeuronStackLiveInterval,...}.so` | SBUF/PSUM/DRAM alloc + spills |
| Descriptor emit | `CC/starfish/lib/libwalrus.so` `backend::CoreV{2,3,4}GenImpl`; `CC/isa_tpb/sunda/neuron_isa_tpb_pybind.so` `get_bytes` | TPB opcode byte + `NEURON_ISA_TPB_*_STRUCT` |
| Output | `hlo-neff-wrapper` / `walrus_driver` | NEFF (device binary) |

[HIGH/OBSERVED — every `.so` path resolves under `CC/`; the descriptor-emit leg's
`get_bytes` + `NEURON_ISA_TPB_INST_UNION` strings are present in
`CC/isa_tpb/sunda/neuron_isa_tpb_pybind.cpython-311-x86_64-linux-gnu.so`.]

### 1.1 The GPSIMD/POOL-specific passes

These are the legs that target the Q7 POOL cluster specifically. Names read directly
from the unstripped Cython symbol tables.

- **`SundaISel`** (`targets/sunda/passes/SundaISel.cpython-311-...so`) — the
  NISA→TPB-opcode instruction selector. OBSERVED codegen classes/methods (symbol
  strings): `QuantizeMXCodeGen.codegenQuantizeMXOperator`, `RmsNormCodeGen.codegenRmsNormOp`,
  `SoftmaxCodeGen.codegenSoftmaxOp` / `codegenSoftmaxExpOp` / `codegenSoftmaxDxOp`,
  `SundaGenericLoadCodegen.codegenGenericLoad`, and the predicate
  **`can_lower_generic_load_to_gather`** (string present in the `.so`). This is where a
  generic penguin load that *can* be a gather becomes one, where MX quant lowers, and
  where the **DGE** (descriptor-generation-engine) attribute is decided.
  [HIGH/OBSERVED — symbols read this pass.]
- **`InferIntrinsicOnCC`** (`targets/sunda/passes/InferIntrinsicOnCC.so`) — the
  compute-cluster intrinsic-fusion pass. OBSERVED: `InferIntrinsicOnCC.inferIntrinsicOnDAG`,
  `checkAndExtractRoot`, `infer_fma`, `infer_cast`, `infer_broadcast_dag`. It walks the
  BIR expression DAG and folds `{mul+add→fma, dtype-cast, broadcast}` into single CC
  intrinsics that lower to one POOL kernel rather than a chain. [HIGH/OBSERVED.]
- **`SundaSizeTiling`** — the partition-dim tiler; chooses how a tensor's partition axis
  maps onto the 128-partition / 8-GpSimd-core × 16-partition POOL geometry.
  [HIGH/OBSERVED — symbol names `PartitionAxesCandidates`, `partition_max_elements`.]
- **`LegalizePartitionReduce`** — legalizes a cross-partition reduction into
  `CROSS_LANE_REDUCE_ARITH 0x7c` / `CROSS_LANE_REDUCE_BITVEC 0x7d` (the POOL partition-axis
  reduce). [HIGH/OBSERVED — pass name.]
- **`LegalizeSundaMacro` / `LateLegalizeInst`** — macro expansion + post-schedule
  fixups. [HIGH/OBSERVED — pass names.]

> **NOTE — the scheduler and GPSIMD launch cost.** `Scheduler.so` is a list scheduler
> over a data-dependence graph (OBSERVED methods `build_ddg`, `enumerate_ready`,
> `pick_candidate`, `bump_preds_priority`, plus liveness tie-break `def_bytes`/`kill_bytes`).
> The GPSIMD-relevance: the ~150-cycle `GPSIMD_START` launch cost vs the 64-cycle Vector
> `MIN_II` (see neuronx-cc/wiki/src/.../scheduler-cost.md) feeds the latency model the
> scheduler consults, which is *why* the compiler keeps small tiles on Vector and routes
> to GPSIMD only what Vector cannot do. [HIGH/OBSERVED symbols; the latency→schedule tie
> is INFERRED-HIGH — the cost table is not byte-pinned on this page.]

### 1.2 The BIR (penguin IR) layer — observed `Inst*` count

`CC/starfish/lib/libBIR.so` exposes **119 distinct `bir::Inst*` class names** in its
mangled symbol table (counted by matching `_ZN3bir<len><Name>` and keeping roots
beginning `Inst`). About nine of those are non-instruction helpers (`InstBuilder`,
`Instruction`, `InstructionType`, `InstSyncType2string`, …); the rest are real IR nodes.

> **CORRECTION (vs DX-CC-01 / GX-REF-02 §3.1, which CARRIED "73 Inst\*").** The shipped
> `libBIR.so` symbol table shows **~119 `Inst*` classes**, not 73 — the earlier figure
> was carried from a smaller intermediate read of `birpy`, not the full `libBIR.so`
> symbol table. The GPSIMD-relevant node set is directly visible:
> `InstIota`, `InstGather`, `InstIndirectCopy`, `InstTensorScalarAffineSelect`,
> `InstQuantizeMx`, `InstNonzeroWithCount`, `InstGetSequenceBounds`, `InstSelect`,
> `InstCopyPredicated`, `InstGPSIMDSB2SB`, `InstCollective`/`InstCollectiveCompute`/
> `InstCollectiveSend`/`InstCollectiveRecv`, `InstTensorTensor`, `InstTensorScalar`,
> `InstTensorScalarCache`, `InstMatmult`/`InstMatmultMx`, and the raw-byte node
> `InstInlineASMBytes`. [HIGH/OBSERVED — `strings libBIR.so | rg _ZN3bir`.]

The IR/ISA spelling boundary, confirmed from C++ mangled constructors in `libBIR.so`:
BIR **`InstMatmult`** (two-t: `_ZN3bir11InstMatmultC1...`) → TPB `Matmul`; BIR
**`InstTensorTensor`** → TPB `TENSOR_TENSOR_ARITH_OP`/`_BITVEC_OP`; BIR
**`InstInlineASMBytes`** (`_ZN3bir18InstInlineASMBytesC1...`, builder method
`addInlineASMBytes`) → `EXTENDED_INST 0xf0`. [HIGH/OBSERVED — mangled symbols read.]

---

## 2. The NKI → `emit_` → NISA → TPB chain

How one op becomes one device opcode (first three hops are plain Python, OBSERVED in
`NKI/`; the last two are the cc backend in §1):

```c
// Pseudocode of the lowering dispatch — the four host hops to a device opcode.
// Hop 1-3 are NKI Python; hop 4-5 are the neuronx-cc backend (libBIR/SundaISel/walrus).

device_inst lower_nki_op(NkiCall call) {
    // HOP 1 — public API:  nki.isa.<op>(...)  in NKI/isa/<module>.py
    Backend be = get_backend();                 // the active MLIR-tracer backend

    // HOP 2 — dispatch to backend mnemonic
    //   for the dual-engine arith ops, the *engine* is resolved HERE, not at emit time:
    Engine eng = call.engine;                    // default = unknown (let compiler pick)
    if (call.op == TENSOR_TENSOR &&
        eng == Engine_unknown &&
        call.op_fn in {add, subtract, multiply} &&
        all_int32_or_uint32(call.dst, call.d1, call.d2) &&
        none_in_psum(call.dst, call.d1, call.d2))
        eng = Engine_gpsimd;                      // NKI/isa/tensor_ops.py:104-115

    // HOP 3 — emit:  NKI/backends/mlir_tracer/isa_emit.py
    //   emit_<mnemonic>() calls EXACTLY ONE _nki_irbuilder.<mnemonic>(), 1:1, no fan-out.
    MlirOp nisa = irbuilder_emit(call.mnemonic, call.operands, eng);  // builds a NISA MLIR op

    // HOP 4 — SundaISel:  NISA op -> SundaISAInst -> TPB opcode byte
    //   dtype dispatch lives here: a tensor_tensor(bf16) becomes 0x8a/0x8b/0x8f, not 0x41.
    SundaISAInst isa = SundaISel_select(nisa);   // CC/.../sunda/passes/SundaISel.so

    // HOP 5 — walrus Generator:  opcode + NEURON_ISA_TPB_*_STRUCT descriptor bytes
    return CoreVNGenImpl_get_bytes(isa);         // CC/starfish/lib/libwalrus.so + isa_tpb pybind
}
```

[HIGH/OBSERVED — hops 1-3 read in `NKI/`; hops 4-5 from the `SundaISel`/`libwalrus`/
`isa_tpb` symbols.]

> **GOTCHA — the `emit_` layer is jinja-generated and trivially 1:1.** `isa_emit.py`
> carries the header *"Do not edit manually — generated by isa\_emit.py.j2."* Each
> `emit_<mnemonic>` body calls exactly one `_nki_irbuilder.<mnemonic>` with the mnemonic
> equal to the emit-name minus the `emit_` prefix — **no renames, no fan-in, no
> fan-out**. Verified programmatically: 62 emit defs, 62 irbuilder calls, zero
> emit-mnemonics missing an irbuilder call and zero stray irbuilder calls.
> [HIGH/OBSERVED — `rg '^def emit_' isa_emit.py | wc -l` = 62; AST cross-match = perfect
> 1:1.]

### 2.1 The engine tag — set at emit time, refined at ISel

The NKI engine enum (`NKI/isa/enums.py`, OBSERVED numeric): `tensor=1, scalar=2,
gpsimd=3, dma=4, vector=5, sync=6, unknown=0`. The `engine=` **default** in each
`emit_` signature is read directly from `isa_emit.py`:

| Default engine | `emit_*` with that default (line in `isa_emit.py`) |
|---|---|
| `DMA` | `emit_dma_copy` (L37) |
| `Scalar` | `emit_activation_read_accumulator` (L272) |
| `Vector` | `emit_memset` (L178), `emit_tensor_copy` (L442), `emit_tensor_tensor_arith` (L463), `emit_tensor_scalar_arith` (L808), `emit_dve_read_accumulator` (L430) |
| `Gpsimd` | `emit_all_reduce` (L1460), `emit_all_gather` (L1491), `emit_reduce_scatter` (L1522), `emit_all_to_all` (L1553), `emit_all_to_all_v` (L1584) |

[HIGH/OBSERVED — each line cited above is an `engine=_nisa_dialect.Engine.<E>` in
`isa_emit.py`.]

> **CORRECTION (vs DX-CC-01 §2.2's DGEType list).** The `DGEType` enum
> (`NKI/isa/enums.py`) is `unknown=0, swdge=1, hwdge=2, none=3` — DX-CC-01 listed only
> `{swdge=1, hwdge=2, none=3}` and omitted **`unknown=0`** ("let the compiler decide the
> DGE mode", the default). `SundaISel`'s `can_use_dge` is what resolves an `unknown` DGE
> to `swdge`/`hwdge`/`none`. [HIGH/OBSERVED — `class DGEType` in `enums.py`.]

> **NOTE — a second DMA selector exists.** `enums.py` also defines a `dma_engine`
> enum: `dma=1` (shared DMA with CoreBarrier sync, the default, triggerable from any
> engine) and **`gpsimd_dma=2`** ("GPSIMD's internal DMA engine for low-latency SB-to-SB
> swaps in LNC=2 … implies GPSIMD as the trigger engine"). This is distinct from the
> compute-engine `engine` enum and is the host-side selector for the
> `InstGPSIMDSB2SB` / `SB2SB_COLLECTIVE 0xbf` SB-to-SB path. [HIGH/OBSERVED.]

---

## 3. The deliverable — the 62-row `emit_*` → irbuilder → TPB-opcode table

Each row binds one of the 62 `emit_*` entry points to: its irbuilder mnemonic
(`= emit_name − "emit_"`), the TPB opcode (numeric, **cross-checked field-for-field
against the device ledger §2** — see [../firmware/kernels/opcode-catalog-ledger.md](../firmware/kernels/opcode-catalog-ledger.md)),
the **engine as the irbuilder `.so` docstring states it**, the device kernel page, and
a confidence tag. Where the engine differs between the irbuilder docstring and
DX-CC-01's earlier table, a CORRECTION is flagged inline.

The **engine column is authoritative from the binary**: the `_nki_irbuilder.so` embeds,
for every mnemonic, a docstring `"NKI IR builder for <m>. … using <Engine> Engine."`
That string is the engine the IR builder declares; the parenthetical `(route)` notes
where SundaISel may switch.

Legend: opcode numerics CONFIRMED against device ledger §2 [C]; engine from the
`_nki_irbuilder.so` docstring [O]. `*** NEW` = added since GX-REF-03's read of the
surface. `† MAVERICK/MED` = ledger flags the opcode as MAVERICK-only and not byte-pinned.

### 3.1 GPSIMD / POOL-resident (the core deliverable)

| `emit_*` | irbuilder mnemonic | TPB opcode | engine (`.so` docstring) | device kernel page | conf |
|---|---|---|---|---|---|
| `emit_iota` | `iota` | `IOTA 0x7e` | GpSimd | [../firmware/kernels/iota.md](../firmware/kernels/iota.md) | HIGH/O+C |
| `emit_tensor_scalar_affine_select` | `tensor_scalar_affine_select` | `TENSOR_SCALAR_AFFINE_SELECT 0x92` ‡ | GpSimd | [../firmware/kernels/affineselect.md](../firmware/kernels/affineselect.md) | HIGH/O+C |
| `emit_cross_lane_reduce_arith` | `cross_lane_reduce_arith` | `CROSS_LANE_REDUCE_ARITH 0x7c` | Gpsimd | [../firmware/kernels/cross-lane-reduce.md](../firmware/kernels/cross-lane-reduce.md) | HIGH/O+C |
| `emit_cross_lane_reduce_bitvec` | `cross_lane_reduce_bitvec` | `CROSS_LANE_REDUCE_BITVEC 0x7d` | Gpsimd | [../firmware/kernels/cross-lane-reduce.md](../firmware/kernels/cross-lane-reduce.md) | HIGH/O+C |
| `emit_gather` | `gather` | `GATHER 0x68` | GpSimd | [../firmware/kernels/indirection-gather.md](../firmware/kernels/indirection-gather.md) | HIGH/O+C |
| `emit_indirect_copy` | `indirect_copy` | `INDIRECT_COPY 0xe7` | GpSimd | [../firmware/kernels/indirection-gather.md](../firmware/kernels/indirection-gather.md) | HIGH/O+C |
| `emit_nonzero_with_count` | `nonzero_with_count` | `NONZERO_WITH_COUNT 0xf2` | GpSimd | [../firmware/kernels/nonzero-with-count.md](../firmware/kernels/nonzero-with-count.md) | HIGH/O+C |
| `emit_quantize_mx` | `quantize_mx` | `TENSOR_DEQUANTIZE 0x7b` (fwd pack) / `QUANTIZE_MX 0xe3` (device) | Vector ◆ | [../firmware/kernels/mx-dequant.md](../firmware/kernels/mx-dequant.md) | HIGH/O+C |
| `emit_sequence_bounds` | `sequence_bounds` | `GET_SEQUENCE_BOUNDS 0xbe` | GpSIMD | [../firmware/kernels/get-sequence-bounds.md](../firmware/kernels/get-sequence-bounds.md) | HIGH/O+C |
| `emit_tensor_tensor_arith` | `tensor_tensor_arith` | `TENSOR_TENSOR_ARITH 0x41` | Vector **or** GpSimd (route) | [../firmware/kernels/tensor-tensor.md](../firmware/kernels/tensor-tensor.md) | HIGH/O+C |
| `emit_tensor_tensor_power` | `tensor_tensor_power` | `TENSOR_TENSOR_ARITH 0x41` (power) | GpSimd | [../firmware/kernels/tensor-tensor.md](../firmware/kernels/tensor-tensor.md) | HIGH/O+C `***` |
| `emit_tensor_scalar_arith` | `tensor_scalar_arith` | `TENSOR_SCALAR_ARITH 0x43` | Vector, Scalar **or** GpSimd (route) | [../firmware/kernels/tensor-scalar.md](../firmware/kernels/tensor-scalar.md) | HIGH/O+C |
| `emit_tensor_scalar_addr` | `tensor_scalar_addr` | `TENSOR_SCALAR_ADDR 0x74` | Gpsimd | [../firmware/kernels/tensor-scalar.md](../firmware/kernels/tensor-scalar.md) | HIGH/O+C `***` |
| `emit_tensor_scalar_cache_reduce` | `tensor_scalar_cache_reduce` | `TENSOR_SCALAR_CACHE_REDUCE 0x9a` | Vector ◆ | [../firmware/kernels/tensor-scalar.md](../firmware/kernels/tensor-scalar.md) | HIGH/O+C `***` |
| `emit_tensor_scalar_cache_cumulative` | `tensor_scalar_cache_cumulative` | `TENSOR_SCALAR_CACHE_CUMULATIVE 0xe6` | Vector ◆ | [../firmware/kernels/tensor-scalar.md](../firmware/kernels/tensor-scalar.md) | HIGH/O+C `***` |
| `emit_tensor_tensor_scan` | `tensor_tensor_scan` | `TENSOR_TENSOR_SCAN_ARITH 0xe5` | Vector | [../firmware/kernels/tensor-tensor.md](../firmware/kernels/tensor-tensor.md) | HIGH/O+C |
| `emit_scalar_tensor_tensor_arith` | `scalar_tensor_tensor_arith` | `SCALAR_TENSOR_TENSOR_ARITH 0x9d` | Vector | [../firmware/kernels/tensor-tensor.md](../firmware/kernels/tensor-tensor.md) | HIGH/O+C |
| `emit_scalar_tensor_tensor_bitvec` | `scalar_tensor_tensor_bitvec` | `SCALAR_TENSOR_TENSOR_BITVEC 0x9e` | Vector | [../firmware/kernels/tensor-tensor.md](../firmware/kernels/tensor-tensor.md) | HIGH/O+C |
| `emit_select_reduce` | `select_reduce` | `SELECT_REDUCE 0xea` | Vector | [../firmware/kernels/copypredicatedreduce.md](../firmware/kernels/copypredicatedreduce.md) | HIGH/O+C |
| `emit_copy_predicated` | `copy_predicated` | `COPY_PREDICATED 0x72` | (predicate) | [../firmware/kernels/castpredicated.md](../firmware/kernels/castpredicated.md) | HIGH/O+C `***` |
| `emit_memset` | `memset` | `MEMSET 0x49` | Vector **or** GpSimd | [../firmware/kernels/move-dtype.md](../firmware/kernels/move-dtype.md) | HIGH/O+C |
| `emit_tensor_copy` | `tensor_copy` | `COPY 0x46` / `CAST 0x47` | Vector/Scalar/GpSimd | [../firmware/kernels/cast-copy.md](../firmware/kernels/cast-copy.md) | HIGH/O+C |
| `emit_reciprocal` | `reciprocal` | `RECIPROCAL 0x48` | Vector | [../firmware/kernels/move-dtype.md](../firmware/kernels/move-dtype.md) | HIGH/O+C |

‡ `TENSOR_SCALAR_AFFINE_SELECT 0x92` is a **distinct** opcode from
`TENSOR_SCALAR_SELECT 0x98` — see GOTCHA in §4. ◆ engine CORRECTION — see §4.

### 3.2 DVE / Vector-resident (GPSIMD-adjacent; same ISA stream, different engine)

| `emit_*` | irbuilder mnemonic | TPB opcode | engine | device kernel page | conf |
|---|---|---|---|---|---|
| `emit_tensor_tensor_bitvec` | `tensor_tensor_bitvec` | `TENSOR_TENSOR_BITVEC 0x51` | Vector | [../firmware/kernels/tensor-tensor.md](../firmware/kernels/tensor-tensor.md) | HIGH/O+C |
| `emit_tensor_scalar_bitvec` | `tensor_scalar_bitvec` | `TENSOR_SCALAR_BITVEC 0x53` | Vector | [../firmware/kernels/tensor-scalar.md](../firmware/kernels/tensor-scalar.md) | HIGH/O+C |
| `emit_tensor_reduce_arith` | `tensor_reduce_arith` | `TENSOR_REDUCE_ARITH 0x42` | Vector | [../firmware/kernels/cross-lane-reduce.md](../firmware/kernels/cross-lane-reduce.md) | HIGH/O+C |
| `emit_tensor_reduce_bitvec` | `tensor_reduce_bitvec` | `TENSOR_REDUCE_BITVEC 0x52` | Vector | [../firmware/kernels/cross-lane-reduce.md](../firmware/kernels/cross-lane-reduce.md) | HIGH/O+C |
| `emit_dve_read_accumulator` | `dve_read_accumulator` | `DVE_READ_ACCUMULATOR 0x9b` | Vector | [../firmware/kernels/dve-read-state.md](../firmware/kernels/dve-read-state.md) | HIGH/O+C |
| `emit_max8` | `max8` | `MAX8 0x6c` | (Vector) | [../firmware/kernels/avg-max-pool.md](../firmware/kernels/avg-max-pool.md) | HIGH/O+C |
| `emit_find_index8` | `find_index8` | `FIND_INDEX8 0x6e` | (Vector) | [../firmware/kernels/avg-max-pool.md](../firmware/kernels/avg-max-pool.md) | HIGH/O+C |
| `emit_max_index_and_match_replace` | `max_index_and_match_replace` | `MATCH_REPLACE8 0x6f` | Vector | [../firmware/kernels/avg-max-pool.md](../firmware/kernels/avg-max-pool.md) | HIGH/O+C |
| `emit_range_select` | `range_select` | `RANGE_SELECT 0xbc` | Vector | [../firmware/kernels/rangeselect.md](../firmware/kernels/rangeselect.md) | HIGH/O+C |
| `emit_dropout` | `dropout` | `DROPOUT 0x7f` | Vector | [../firmware/kernels/dropout.md](../firmware/kernels/dropout.md) | HIGH/O+C |
| `emit_bn_stats` | `bn_stats` | `BATCH_NORM_STATS2 0x61` | Vector | [../firmware/kernels/batchnorm-forward.md](../firmware/kernels/batchnorm-forward.md) | HIGH/O+C |
| `emit_bn_aggr` | `bn_aggr` | `BATCH_NORM_AGGREGATE 0x62` | Vector | [../firmware/kernels/batchnorm-forward.md](../firmware/kernels/batchnorm-forward.md) | HIGH/O+C |
| `emit_stream_shuffle` | `stream_shuffle` | `STREAM_SHUFFLE 0x6a` | Vector | [../firmware/kernels/decode-pool.md](../firmware/kernels/decode-pool.md) | HIGH/O+C |
| `emit_stream_transpose` | `stream_transpose` | `STREAM_TRANSPOSE 0x6b` | Vector | [../firmware/kernels/decode-pool.md](../firmware/kernels/decode-pool.md) | HIGH/O+C |
| `emit_exponential` | `exponential` | `EXPONENTIAL 0x30` | Vector | [../firmware/kernels/exponential.md](../firmware/kernels/exponential.md) | HIGH/O+C |

### 3.3 Scalar (Activation engine)

| `emit_*` | irbuilder mnemonic | TPB opcode | engine | device kernel page | conf |
|---|---|---|---|---|---|
| `emit_activation` | `activation` | `ACTIVATE 0x21` | Scalar | [../firmware/kernels/activate-pwl.md](../firmware/kernels/activate-pwl.md) | HIGH/O+C |
| `emit_activation_read_accumulator` | `activation_read_accumulator` | `ACTIVATION_READ_ACCUMULATOR 0x24` | Scalar | [../firmware/kernels/activate-pwl.md](../firmware/kernels/activate-pwl.md) | HIGH/O+C |

### 3.4 Tensor (PE systolic; not GPSIMD but in the same NEFF stream)

| `emit_*` | irbuilder mnemonic | TPB opcode | engine | device kernel page | conf |
|---|---|---|---|---|---|
| `emit_matmul` | `matmul` | `MATMUL 0x02` | Tensor | [../firmware/kernels/pe-matmul.md](../firmware/kernels/pe-matmul.md) | HIGH/O+C |
| `emit_matmul_mx` | `matmul_mx` | `MATMUL 0x02` (mx) | Tensor | [../firmware/kernels/pe-matmul.md](../firmware/kernels/pe-matmul.md) | HIGH/O+C |

### 3.5 SDMA / collective (the SP/GPSIMD collective spine)

These do **not** emit a single fixed-opcode hardware op; ISel lowers them to the
**PSEUDO band `0xC1–0xDF`** (compiler-generated, NRT-translated, never directly
device-decoded). Key targets from the ledger: `0xC8 PSEUDO_TRIGGER_COLLECTIVE`,
`0xDC PSEUDO_GID_LOAD`, `0xDE PSEUDO_TENSOR_COMPLETION`, plus the real SB-to-SB
`0xbf SB2SB_COLLECTIVE`.

| `emit_*` | irbuilder mnemonic | lowers to (PSEUDO / real) | engine | conf |
|---|---|---|---|---|
| `emit_all_reduce` | `all_reduce` | PSEUDO TriggerAllReduce + CCE/DGE | Gpsimd | HIGH/O |
| `emit_all_gather` | `all_gather` | `PSEUDO_TRIGGER_COLLECTIVE 0xC8` | Gpsimd | HIGH/O |
| `emit_reduce_scatter` | `reduce_scatter` | `PSEUDO_TRIGGER_COLLECTIVE 0xC8` | Gpsimd | HIGH/O |
| `emit_all_to_all` | `all_to_all` | PSEUDO ALL_TO_ALL | Gpsimd | HIGH/O |
| `emit_all_to_all_v` | `all_to_all_v` | PSEUDO ALL_TO_ALL (variable) | Gpsimd | HIGH/O |
| `emit_collective_permute` | `collective_permute` | PSEUDO PERMUTE | — | HIGH/O |
| `emit_collective_permute_reduce` | `collective_permute_reduce` | PSEUDO PERMUTE_REDUCE | — | HIGH/O |
| `emit_collective_permute_implicit` | `collective_permute_implicit` | PSEUDO PERMUTE_REDUCE_IMPLICIT | — | HIGH/O |
| `emit_collective_permute_implicit_reduce` | `collective_permute_implicit_reduce` | PSEUDO PERMUTE_REDUCE_IMPLICIT | — | HIGH/O |
| `emit_collective_permute_implicit_current_processing_rank_id` | `…current_processing_rank_id` | CurProcessingRankID pseudo (`InstGetCurProcessingRankID`) | — | HIGH/O `***` |
| `emit_send_recv` | `send_recv` | SENDRECV pseudo / `SB2SB_COLLECTIVE 0xbf` | — | HIGH/O |
| `emit_rank_id` | `rank_id` | `PSEUDO_GID_LOAD 0xDC` / ReadVarAddr (`InstGetGlobalRankId`) | — | HIGH/O |
| `emit_rank_id_store` | `rank_id_store` | rank-id store pseudo | — | HIGH/O |
| `emit_dma_copy` | `dma_copy` | `DMAMEMCPY 0xb8` | DMA | HIGH/O+C |
| `emit_dma_copy_indirect` | `dma_copy_indirect` | `DMA_INDIRECT 0xbb` | DMA | HIGH/O+C |
| `emit_dma_transpose` | `dma_transpose` | `DMA_TRANSPOSE 0xbd` | DMA | HIGH/O+C |
| `emit_dma_gather_transpose` | `dma_gather_transpose` | `DMA_GATHER_TRANSPOSE 0xf1` | DMA | HIGH/O+C |

### 3.6 Register / control

| `emit_*` | irbuilder mnemonic | lowers to | conf |
|---|---|---|---|
| `emit_load_register` | `load_register` | `REG_LOAD 0x4a` (ALU/MOVE control) — **dormant** | HIGH/O+C |
| `emit_store_register` | `store_register` | TENSOR_STORE / reg-store control | HIGH/O |
| `emit_device_print` | `device_print` | host-side debug print (no device opcode) | HIGH/O |

**COUNT CHECK.** The five sub-tables hold exactly **62 rows** — equal to the 62
`emit_*` defs and the 62 `_nki_irbuilder.<mnemonic>` calls (programmatic 1:1, §2 GOTCHA).
[HIGH/OBSERVED.]

> **GOTCHA — the irbuilder surface is a SUPERSET of the 62 emitted ops.** The
> `_nki_irbuilder.so` declares **69** `"NKI IR builder for …"` docstrings: the 62 above
> **plus** `rand`, `rand2`, `rand_get_state`, `rand_set_state`, `rng`, `set_rng_seed`,
> `dma_compute`. Those seven are irbuilder methods reachable from other `NKI/` surfaces
> (the RNG family + `dma_compute`), not from `isa_emit.py`'s `emit_*` roster. A
> reimplementation must not assume "irbuilder method ⇒ `emit_*` entry point" — the
> jinja `emit_` layer exposes only 62 of the 69. [HIGH/OBSERVED — `strings _nki_irbuilder.so
> | rg '^NKI IR builder for'` = 69 distinct.]

---

## 4. The differential check — `emit_*` vs firmware decode vs the value model

Direction: for each `emit_*`, does the opcode it eventually produces AGREE with (a) the
device ledger §2 + the SX-FW kernel decode, and (b), for arithmetic ops, the libfiss
per-lane value primitive (see neuronx-cc/wiki/src/.../iss-value-model.md)?

### 4.1 Numeric verdict — ZERO opcode mismatches

All **50** distinct opcode bindings tabulated in §3 are CONFIRMED against the device
ledger §2 (a full field-for-field cross-check this pass). No `emit_*` names, routes to,
or produces a TPB opcode the firmware roster lacks or decodes differently.
[HIGH — opcode CONFIRMED vs ledger; engine OBSERVED from irbuilder `.so`.]

### 4.2 The engine CORRECTIONS (◆ rows)

The irbuilder `.so` docstrings disagree with DX-CC-01 §3 on three engine assignments.
The binary docstring is authoritative for the engine the IR builder *declares*:

> **CORRECTION ◆ — `tensor_scalar_cache_reduce` / `tensor_scalar_cache_cumulative` are
> Vector, not GpSimd.** DX-CC-01 §3 tabulated both as `gpsimd`. The `_nki_irbuilder.so`
> docstrings read: *"…reduce along free dimensions using **Vector Engine**"* and
> *"…perform cumulative reduction using **Vector Engine**."* The opcodes
> (`0x9a`/`0xe6`) are unchanged and ledger-confirmed; only the declared engine differs.
> [HIGH/OBSERVED — irbuilder `.so` docstring strings.]

> **CORRECTION ◆ — `quantize_mx` declares Vector, not GpSimd.** DX-CC-01 §3 tabulated it
> `gpsimd`. The `.so` docstring reads: *"Apply on-the-fly quantization … in OCP
> Microscaling (MX) formats using **Vector Engine**."* The device decode is the inverse
> MX dequant kernel (`TENSOR_DEQUANTIZE 0x7b` / `QUANTIZE_MX 0xe3`); the host forward
> pack is declared on Vector. [HIGH/OBSERVED — irbuilder `.so` docstring.]

(For contrast, `tensor_scalar_addr` *does* declare `Gpsimd` in the `.so` docstring —
*"…64-bit address calculations using Gpsimd Engine"* — confirming DX-CC-01's gpsimd
route for that op.)

### 4.3 The four sharpenings (genuine new data)

- **(S1) The gather 2:1 split.** `emit_gather`→`GATHER 0x68` (within-partition, u32)
  vs `emit_indirect_copy`→`INDIRECT_COPY 0xe7` (8-core/16-part, u16). NKI surface
  `local_gather`→`emit_indirect_copy`; `nc_n_gather`→`emit_gather`. Both POOL kernels
  (see [../firmware/kernels/indirection-gather.md](../firmware/kernels/indirection-gather.md)).
  **The name `local_gather` is a TRAP — it does NOT route to `GATHER 0x68`.** Confirmed
  by the BIR node split `InstGather` vs `InstIndirectCopy` in `libBIR.so`. [HIGH/OBSERVED.]
- **(S2) The affine_select name-vs-opcode trap.** `affine_select`→
  `TENSOR_SCALAR_AFFINE_SELECT 0x92`, a DISTINCT opcode from `TENSOR_SCALAR_SELECT 0x98`.
  A reader matching on "select" mis-binds; the BIR node `InstTensorScalarAffineSelect`
  and the emit name disambiguate. [HIGH/OBSERVED.]
- **(S3) Engine-polymorphic ops.** `emit_tensor_tensor_arith` (`0x41`) and
  `emit_tensor_scalar_arith` (`0x43`) carry an `engine` kwarg; the SAME opcode runs on
  GpSimd OR Vector OR (for `0x43`) Scalar — the opcode does not change with engine; the
  engine selects which hardware datapath decodes it. The irbuilder docstrings confirm:
  `tensor_tensor_arith` = *"using Vector Engine or GpSimd Engine"*; `tensor_scalar_arith`
  = *"using Vector, Scalar or GpSimd Engine."* [HIGH/OBSERVED.]
- **(S4) The bf16 / INT_WIDE cluster is NKI-invisible.** The ledger carries
  `TENSOR_TENSOR_ADD_BF16 0x8a`, `_MULT_BF16 0x8b`, `_SUB_BF16 0x8f` (**SUNDA-only**) and
  the MAVERICK-only `TENSOR_TENSOR_INT_WIDE 0xf3` / `TENSOR_SCALAR_INT_WIDE 0xf4`. **No
  `emit_*` names these directly** — they are reached only via `emit_tensor_tensor_arith`
  with a bf16/wide dtype, lowered by SundaISel's dtype dispatch to the bf16/wide opcode.
  So they are LOWERING TARGETS, not frontend ops. [HIGH/INFERRED from the dtype-routing +
  the absent emit names. The `0xf3`/`0xf4` opcodes are ledger-flagged MAVERICK-only and
  INFERRED/MED, not byte-pinned — see [dtype-engine-fanin-synthesis.md](dtype-engine-fanin-synthesis.md).]

### 4.4 The value-model tie (the differential's deepest leg)

The compiler's GpSimd-routing rule for `tensor_tensor` (OBSERVED,
`NKI/isa/tensor_ops.py:104-115`):

```python
resolved_engine = engine
if (resolved_engine == _engine_enum.unknown
    and op in (add, subtract, multiply)
    and dst.dtype in (int32, uint32)
    and data1.dtype in (int32, uint32)
    and data2.dtype in (int32, uint32)
    and not is_psum(dst.buffer)
    and not is_psum(data1.buffer)
    and not is_psum(data2.buffer)):
    resolved_engine = _engine_enum.gpsimd      # -> TENSOR_TENSOR_ARITH 0x41 on POOL
```

The numpy reference simulator (`NKI/backends/simulator/tensor_ops.py:53-57`) makes the
consequence semantically visible:

```python
def compute(d1, d2):
    if engine == engine_enum.gpsimd:
        return np_op(d1, d2)                    # NATIVE int, 32-bit wrap (no fp cast)
    d1_fp32 = d1 if d1.dtype == np.float32 else d1.astype(np.float32)
    d2_fp32 = d2 if d2.dtype == np.float32 else d2.astype(np.float32)
    return np_op(d1_fp32, d2_fp32)              # Vector path casts to fp32 first
```

So when the compiler routes `int32` add/sub/mul to GpSimd (`0x41` on POOL), the device
runs an Xtensa-FLIX kernel whose per-lane body is the libfiss integer leaf
`module__xdref_add/sub/mul` over int lanes — 32-bit wrap, no saturation — which is
exactly what the native-int numpy branch computes. The two AGREE bit-for-bit. This is
the **only** place the high-level compiler decision and the low-level value oracle meet,
and they are consistent. (For fp ops, the Vector fp32-cast path maps to the DVE
datapath, not the GpSimd xdref leaves.) [HIGH/OBSERVED both ends; the kernel-body
identity is INFERRED-HIGH — the POOL int-tt kernel is not byte-disassembled on this page,
but the firmware tensor-tensor decode and the libfiss xdref-int leaf both describe
32-bit-wrap add/sub/mul.]

### 4.5 Three NKI-side restrictions (frontend policy, NOT firmware limits)

- **(R1)** `cross_lane_reduce` / partition-reduce exposes `{add, max, bitwise_or,
  bitwise_and}` — a SUBSET of the firmware `REDUCE_OP {ADD, AVERAGE, MAX, OR, AND, XOR}`
  (no average/xor at the NKI surface). [CARRIED, re-confirmed.]
- **(R2)** `dma_compute` `reduce_op = add` only (firmware CCE_OP/DGE_COMPUTE_OP carries
  `{Add, Max, Min, Mult, …}`). [CARRIED.]
- **(R3)** `tensor_scalar` GpSimd engine is **"only allowed for rsqrt"** —
  byte-confirmed at `NKI/isa/tensor_ops.py:242`: *"`nki.isa.engine.gpsimd` (only allowed
  for rsqrt)"*. The GpSimd route for `tensor_scalar` is narrower than for
  `tensor_tensor`. A related docstring fact: the **Scalar Engine on trn2** supports only
  `op0=multiply,op1=add` / `op0=multiply,op1=None` / `op0=add,op1=None`
  (`tensor_ops.py:218-220`). [HIGH/OBSERVED.]

None contradict the firmware; each is the frontend exposing less than the hardware.

> **GOTCHA — the PSEUDO 0xC1–0xDF band collides with real opcode names.** The collective
> ops (§3.5) lower into the PSEUDO band, whose enum names *shadow* real ops:
> `0xCA PSEUDO_EMBEDDING_UPDATE` vs real `0x79 EMBEDDING_UPDATE`; `0xCD/0xCE
> PSEUDO_TENSOR_STORE/LOAD` vs real `0xAB/0xAA`; `0xC8 PSEUDO_TRIGGER_COLLECTIVE` vs the
> real SB-to-SB `0xbf SB2SB_COLLECTIVE`. When tracing a collective through ISel, verify
> which axis a name refers to. Separately, **`0x97` is never assigned** to any opcode in
> any gen, and `SortMerge` is a named-but-never-shipped phantom (it survives only as a
> `// SortMerge wip` comment on the `0x98 TENSOR_SCALAR_SELECT` line). [HIGH — ledger §1.2
> + §5.]

---

## 5. The raw-byte / custom-op lane — `EXTENDED_INST 0xf0`

There is a SECOND GPSIMD entry, parallel to the 62 fixed-opcode `emit_*`: the
hand-written-kernel / custom-op path that rides `EXTENDED_INST 0xf0`.

**The generic extended instruction** (`NKI/compiler/kernel_builder/isa.py`):
`extended_inst(opcode, engine, latency_ns, mem_reads, mem_writes, scalars, attrs=None,
simulate=None, name=None)` — *"Generic extended instruction for experimental hardware
ops."* It carries a caller-supplied `simulate` callback (the numpy ground-truth for an
op the sim does not natively know) and a `latency_ns` the scheduler consults.
[HIGH/OBSERVED.]

**The inline-asm-bytes surface** (`NKI/experimental/extended_inst.py`, `n_bytes`):
`asm_bytes` is a literal byte string of underscore-separated two-hex-digit bytes
(the example byte is `"f0"`) plus placeholders parsed by:

```python
_PLACEHOLDER_RE = re.compile(r"\{(events|srcs\[\d+\]|dsts\[\d+\]):(.*?)\}")
_SUPPORTED_SYNC_TYPES = {"datapath"}
```

The three placeholder kinds:
`{events:NEURON_ISA_TBP_EVENTS:DATAPATH}` (the sync-event domain — only `datapath`
engines, i.e. Tensor/Scalar/Vector/GpSIMD, are allowed; DMA and Sequencer sync domains
are explicitly "can be added when extended_inst gains broader engine support"),
`{srcs[N]:DESC}` and `{dsts[N]:DESC}` (operand splice points). The placeholders are
resolved, per the module comment, *"until NCC C++ codegen, which resolves
`{srcs[N]:...}`, `{dsts[N]:...}`, and `{events:...}` using allocated"* descriptors.

This is the literal opcode-byte escape: a hand-assembled Xtensa POOL kernel rides
`EXTENDED_INST 0xf0`, with `{events}` generating the semaphore/sync field and
`{srcs}`/`{dsts}` generating the descriptor access patterns. It is the NKI side of the
C++ custom-op ABI: the BIR node is `InstInlineASMBytes` (builder `addInlineASMBytes`,
`libBIR.so`), the version-handshake is `builtin_custom_op(function_name, lib_file_name,
ulib_to_ucode_version, ulib_to_isa_version, srcs, dsts)`. [HIGH/OBSERVED — the regex,
the `{"datapath"}` set, the `"f0"` example, and the `InstInlineASMBytes` symbol.]

---

## 6. How JAX/XLA and neuronx-distributed reach GPSIMD

The PJRT plugin emits NO TPB opcodes. `libneuronxla`'s
`neuron_cc_wrapper.call_neuron_compiler` builds
`["neuronx-cc", "compile", <input.hlo>, "--framework", "XLA", "--target", …,
<output.neff>] + flags` and runs it either as a subprocess (`subprocess.run`,
`cwd=/tmp/$USER/neuroncc_compile_workdir`) or in-process via
`neuronxcc.cli.Client`. So the JAX→GPSIMD path is **jax/XLA → HLO → libneuronxla PJRT →
`neuronx-cc compile --framework=XLA` → the §1 stack → NEFF** — there is no second
codegen. XLA custom-calls targeting GPSIMD are lowered by neuronx-cc's HLO frontend into
the same penguin BIR. Consequence: the §3 table is the **complete** GPSIMD op surface
for both the NKI path (direct `emit_`) and the XLA path (HLO→BIR→same opcodes).
[HIGH/OBSERVED for the wrapper; HIGH/INFERRED for the single-backend convergence.]

`neuronx-distributed` reaches GPSIMD via `import neuronxcc.ln as nl` kernels
(`kernels/{flash_attn,ring_attention_kernel}.py`), the top-k/argmax family
(`operators/topk.py`→`lnlib.core.topk`; `operators/argmax.py`→`lnlib.core.max.cascaded_max`
over `MAX8 0x6c`/`FIND_INDEX8 0x6e` + GPSIMD partition reduce), and the MX microscaling
MoE path (`experimental/quantization/microscaling/expert_mlps_mx.py`→`quantize_mxfp8`/
`matmul_mx`). The MoE expert MLP is the production driver of the GPSIMD MX path.
[HIGH/OBSERVED — imports.]

---

## 7. Version anchors

| Component | Version | What it pins |
|---|---|---|
| neuronx-cc | `2.24.5133.0+58f8de22` | the compiler wheel; `isa_tpb/sunda` tables, `libBIR`, `SundaISel` |
| nki | `0.3.0+23928721754.g18aa1271` | the `emit_*` / op-library surface |
| neuronx-distributed | `0.18.27753+1cafd54f` | the GPSIMD-reaching kernel imports |
| libneuronxla | `2.2.16408.0+50c26cbd` | the PJRT→neuronx-cc bridge |
| device handshake | `ulib_to_ucode 1.21.1.0` / `ulib_to_isa 1.0.2520.0` | custom-op ABI — DISTINCT from all host versions |

The opcode ledger this page binds against counts **172 union enum values − 31 PSEUDO − 1
INVALID = 140 real HW opcodes** (see [../firmware/kernels/opcode-catalog-ledger.md](../firmware/kernels/opcode-catalog-ledger.md)).
All four host versions are compiler/host op-set releases; none is a firmware version.
[HIGH/OBSERVED.]

---

## Cross-references

- [sundaisel.md](sundaisel.md) — the NISA→TPB instruction selector, op-by-op rewrite rules.
- [bir-inst-roster.md](bir-inst-roster.md) — the ~119 `bir::Inst*` node set.
- [dtype-engine-fanin-synthesis.md](dtype-engine-fanin-synthesis.md) — the bf16/INT_WIDE dtype→opcode dispatch (S4).
- [../firmware/kernels/opcode-catalog-ledger.md](../firmware/kernels/opcode-catalog-ledger.md) — the authoritative device opcode roster.
- neuronx-cc compiler wiki (full internals): `neuronx-cc/wiki/src/...` — the walrus descriptor emitter, the scheduler cost model, the memory planner, and the per-pass details out of scope here.
