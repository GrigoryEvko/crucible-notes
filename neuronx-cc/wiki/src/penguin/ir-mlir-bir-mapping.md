# Penguin IR ↔ MLIR / BIR Mapping

> *All symbols, strings, and offsets on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310). The cp311/cp312 wheels share the layout. Other versions will differ.*

## Abstract

Penguin is the **tensoriser IR** — the Python-class middle-end that sits between the MLIR front-half (mhlo / stablehlo, lowered by `hlo-opt`) and the C++ Backend IR (BIR) that the simulator and KELF emitter consume. This page is the *seam* page: it does not re-document the Penguin node schema (that is the [node-model](ir-node-model.md) page), nor BIR (Part 7), nor the per-op codegen detail (Part 6). It documents the two boundaries — where MLIR *becomes* Penguin, and where Penguin *becomes* BIR — and the node-for-node correspondence across each.

The two seams are deliberately asymmetric, and that asymmetry is the single most important fact a reimplementer must internalize. The **upward seam (MLIR → Penguin)** is a *textual* one: the back-half of `hlo2penguin` is not a tree-rewriter that builds Penguin objects in-process — it is a **Python source emitter** (`MhloToPythonPrinter` / `StableHLOToPythonPrinter`) that prints Python constructor calls for the Penguin IR classes, which are then `exec`-ed to materialize the object graph. The **downward seam (Penguin → BIR)** is a conventional *visitor*: `BirCodeGenLoop` walks the post-instruction-selection Penguin graph and calls one `codegen<Op>` method per instruction, each emitting the matching `bir::` C++ node. Between the two seams, a per-target `CodeGenFlow` driver runs the ~70 + ~140 optimization passes (see [pass-roster](pass-roster-pipeline.md) / Part 5.x), and one of those passes — **instruction selection** (`SundaISel` / `TongaISel`) — is the internal pivot that turns op-level Penguin nodes into `TongaISAInst`-level Penguin nodes, the form `BirCodeGenLoop` is built to consume.

The placement chain is therefore: **`hlo2penguin` (emits Python ctors) → a Penguin `Module` → `CodeGenFlow.optimize` (prepare → optimize, ISel pivots to ISAInst level) → `BirCodeGenLoop` (→ BIR)**. The IR object class never changes across the middle-end — passes mutate one live Python object graph in place; only the *level* (op → ISAInst) changes, and only the final visitor serializes out.

For reimplementation, the contract is:

- **The upward seam is textual.** A reimplementer who builds a tree-to-tree rewriter for mhlo→Penguin will produce a *correct* IR but the *wrong architecture*: the shipped compiler round-trips Penguin IR as runnable Python (`IRWriter` emits the same ctor surface), and the front-end reuses that surface as its output format. Get this wrong and you cannot replay or cache the tensoriser IR the way the real toolchain does.
- **The node map across the downward seam** — `AffineAxis → BirAxis`, `DynamicAxis → BirDynamicForAxis`, `AffineExpr → BirQuasiAffineExpr`, `AffinePredicate → BirAffinePredicate`, `DependencyEdge → addDependency`, `Access → sNdM AP-struct` — plus the ~100 `codegen<Op>` instruction-selection-to-emission dispatch.
- **The level pivot.** `BirCodeGenLoop`'s own banner pins its input: *"at the TongaISAInst level."* Penguin must already be lowered to per-engine ISA instructions before the BIR visitor runs; the op-level nodes (`TensorContractOp`, `ReduceOp`, …) are not directly emittable.

| | |
|---|---|
| **Upward seam (C++)** | `MhloToPythonPrinter` / `StableHLOToPythonPrinter` in `starfish/bin/hlo2penguin` |
| **Upward seam pass args** | `mhlo-to-py-penguin` / `stablehlo-to-py-penguin` |
| **Front-end emit format** | Python source — ctor calls for `penguin.ir.*` classes (textual, `exec`-ed) |
| **Downward seam (Python)** | `targets/codegen/BirCodeGenLoop.cpython-310-…so` |
| **Downward seam banner** | `"Generate Backend IR from tensoriser IR at the TongaISAInst level"` |
| **Codegen dispatch** | ~100 `codegen<Op>` methods (one per ISA Inst) |
| **Level pivot pass** | `SundaISel` / `TongaISel` (op-level Penguin → ISAInst-level Penguin) |
| **Placement chain** | `hlo2penguin` → `Module` → `CodeGenFlow.optimize` → `BirCodeGenLoop` |
| **Driver** | `targets/sunda/SharedCodeGenFlow.so` + per-target `CodeGenFlow.so` |

---

## The Placement Picture

Penguin sits in the middle of a three-IR stack. The reference frame an LLVM engineer should hold: MLIR is the *front IR* (analogous to a high-level dialect), Penguin is the *mid IR* (analogous to a target-lowered-but-not-yet-MachineInstr SelectionDAG/SDNode level), and BIR is the *back IR* (analogous to `MachineInstr` — concrete engine instructions with allocated buffers and inline dependency edges). Instruction selection (`SundaISel`/`TongaISel`) is literally the SelectionDAG→MachineInstr analog, but here it happens *inside* the same Penguin object graph rather than producing a new IR.

```text
   XLA / mhlo / stablehlo                         [MLIR front IR]
            │
            │  hlo-opt  (Part 4: legalize / fuse / custom-call passes)
            ▼
   ┌─────────────────────────── hlo2penguin (C-strand, Part 4.43/4.44) ───────────────────┐
   │  back-half = MhloToPythonPrinter / StableHLOToPythonPrinter                          │
   │  EMITS PYTHON SOURCE: ctor calls for neuronxcc.starfish.penguin.ir.* classes         │
   │  (printDotOp → TensorContractOp(...), printControlDep → DependencyEdge(...), …)       │
   └──────────────────────────────────────────────┬───────────────────────────────────────┘
            │  exec'd → a live Penguin object graph
            ▼
   penguin.ir.Module  { functions, subgraph_functions, root_function }   [Penguin mid IR]
            │
            │  driver/jobs/HLOToTensorizer → per-target CodeGenFlow.optimize()
            │
            ▼
   ┌─────────────────────────── PENGUIN MIDDLE-END (Part 5) ───────────────────────────────┐
   │  CodeGenFlow.codegen_prepare()      (canonicalize / infer / delinearize / DCE / …)    │
   │  CodeGenFlow.codegen_optimization() (fuse / loop / layout / tile / vectorize / CC)    │
   │     … TritiumFusion (autotuner hook) …                                                 │
   │     ─── SundaISel / TongaISel ──▶  IR now at <Target>ISAInst level  ◀── THE PIVOT      │
   │     SharedCodeGenFlow.codegen_optimization_post_tritium_fusion() / legalize_pe_insts() │
   │     allocators (StackAllocator / ColoringAllocator) / peephole_optimizations()         │
   └──────────────────────────────────────────────┬───────────────────────────────────────┘
            │  post-codegen Penguin comp-unit (TongaISAInst level)
            ▼
   ┌─────────────────────────── targets/codegen/BirCodeGenLoop ───────────────────────────┐
   │  a DotTransform visitor: beforeStmtTransform / afterStmtTransform / addInstToBir       │
   │  ~100 codegen<Op>  →  bir:: C++ nodes                                                  │
   │  AffineAxis→BirAxis  AffineExpr→BirQuasiAffineExpr  Access→sNdM-AP  Dep→addDependency   │
   └──────────────────────────────────────────────┬───────────────────────────────────────┘
            ▼
   BIR (libBIR, C++)  → simulator / scheduler / KELF emit              [BIR back IR]
```

> **NOTE —** the NKI path forks at the bottom of the middle-end: instead of `BirCodeGenLoop`, `SharedCodeGenFlow.nki_codegen()` / `targets/codegen/NkiCodegen.so` re-emits NKI text. Same Penguin object graph, different terminal visitor. This page covers the BIR exit; the NKI exit is the [NKI codegen](../nki/) territory of Part 6.

The *endpoints* of the chain are readable in the binaries; the *edges between them* are inferred. `starfish/bin/hlo2penguin` interns both printer class names and both pass-arg strings; `driver/jobs/HLOToTensorizer.cpython-310-…so` interns `"hlo2penguin"` and `"optimize"`; `targets/sunda/SharedCodeGenFlow.so` interns `peephole_optimizations`, `legalize_pe_insts`, and `codegen_optimization_post_tritium_fusion`.

> **GOTCHA — the chain is not a single binary-traceable call path.** `hlo2penguin`'s string pool names *only* `neuronxcc.starfish.penguin`, `neuronxcc.starfish.penguin.ir.Dependency`, and `neuronxcc.starfish.support` — it does **not** embed `"CodeGenFlow"` or the penguin `"Module"` string. Those modules exist as separate compiled `.so` files (`penguin/ir/Module.so`, `penguin/targets/{sunda,tonga}/CodeGenFlow.so`), but the linkage `hlo2penguin → Module → CodeGenFlow` is a *structural inference* from the package edges and the `HLOToTensorizer` job, **not** a string-level call trace inside any one binary. The endpoints are solid; the inter-stage hand-off is INFERRED.

---

## The Upward Seam — MLIR → Penguin (textual Python emit)

### Purpose

Lower the MLIR graph (mhlo / stablehlo, already legalized and fused by `hlo-opt`) into the Penguin tensoriser IR. The Penguin half of this lowering is documented in detail on the [C-strand pages 4.43/4.44](../hlo-opt/) — *this page does not re-prove the textual-Python finding*; it records the seam so the orientation is complete.

### The Mechanism

The back-half of `hlo2penguin` is a **printer**, not a builder. `MhloToPythonPrinter` (and its stablehlo sibling) walk the MLIR module and emit Python *source* — constructor calls for the `penguin.ir.*` classes plus `IRBuilder` method calls — which downstream is `exec`-ed to construct the object graph. The tell is in the binary: the **only** `penguin.ir` module name the C++ `hlo2penguin` binary needs to embed is `neuronxcc.starfish.penguin.ir.Dependency` — because the printer emits `DependencyEdge(...)` ctors as text, the import string must be present even though no Python ran inside `hlo2penguin`. That string, `neuronxcc.starfish.penguin.ir.Dependency`, is in `hlo2penguin`'s string pool.

```c
// MhloToPythonPrinter — back-half of hlo2penguin (C-strand, see 4.43/4.44)
// Driven by pass-arg "mhlo-to-py-penguin"; sibling "stablehlo-to-py-penguin".
function printModule(mlir::ModuleOp m):
    emit("from neuronxcc.starfish.penguin.ir import *")   // textual import
    for func in m.funcs:
        emit("cu = IRBuilder(...)")                       // IRBuilder
        for op in func.ops:
            switch (op.dialect_opcode):
              case mhlo.dot, dot_general:   printDotOp(op);        // → TensorContractOp(...)
              case mhlo.add/mul/...:        printBinaryTensorOp(op);// → BinaryOp/ElementwiseOp
              case mhlo.reduce:             printReduceOp(op);     // → ReduceOp(...)
              case all_reduce/all_gather:   printCollectiveOp(op); // → AllReduceOp/AllGatherOp
              case AwsNeuronCustomOp:       printNeuronCustomOp(op);// → CustomOp/NativeNkiKernel
              case QuantizeMX/scale-matmul: printQuantizeMX(op);   // → QuantizeMXOperator/ScaledTensorContractOp
              case AwsNeuronControlDep:     printControlDep(op);   // → DependencyEdge(src,dst,kind)
              ...                                                   // printTensor for SSA tensor types
    // result: a .py source blob that, exec'd, builds the Penguin Module
```

### Per-Op Map (MLIR op → Penguin node)

| MLIR op (mhlo / SHLO / Aws*) | Printer emitter | Penguin node | Penguin §ref |
|---|---|---|---|
| `mhlo.dot` / `dot_general` | `printDotOp` | `TensorContractOp` | TensorOp §8.2 |
| elementwise (`add`/`mul`/…) | `printBinary/printUnary/printTernaryTensorOp` | `BinaryOp` / `UnaryOp` / `ElementwiseOp` | §8.1 |
| `mhlo.reduce` / `reduce_window` | `print<ReduceOp>` | `ReduceOp` / `ReduceWindowTensorOp` | §8.2 |
| `AwsNeuronSoftmax(/Backward)` | softmax-family printer | `SoftmaxOp` / `SoftmaxDxOp` | §8.3 |
| `AwsNeuronRmsNorm` | `printRmsNorm` | `RmsNormOp` | §8.3 |
| `all_reduce` / `all_gather` / `reduce_scatter` | `printCollectiveOp<…>` | `AllReduceOp` / `AllGatherOp` / `ReduceScatterOp` | §8.5 |
| `mhlo.scatter` / `select_and_scatter` | `print<ScatterOp>` | `ScatterTensorOp` / `SelectAndScatterTensorOp` | §8.x |
| `mhlo.fusion` / `stablehlo.composite` | `printArbitraryFusionOp` / `printScheduleFusionOp` | `FusionOp` / `ElementwiseFusionOp` / `ReduceFusionOp` | §8.6 |
| `AwsNeuronCustomOp` / NativeKernel | `printNeuronCustomOp` / `printNativeKernel` | `CustomOp` / `NativeNkiKernel` | §8.7 |
| `AwsNeuronControlDep` | `printControlDep` | `DependencyEdge` | Dep §6 |
| `QuantizeMX` / scale matmul (SHLO) | `printQuantizeMX` / `printScaleMatmult` | `QuantizeMXOperator` / `ScaledTensorContractOp` | §8.4 |
| MLIR tensor type (arg/result/const) | `printTensor` / `printConstant` | `Tensor` (+ `TensorType.kind`) | §3 |

Six printer emitters are directly present in `hlo2penguin`'s string pool: `printDotOp`, `printTensor`, `printControlDep`, `printNeuronCustomOp`, `printQuantizeMX`, and `printCollectiveOp`. The remaining emitter names in the table are carried from the hlo-opt roster (4.43/4.44) rather than re-derived here.

> **QUIRK —** the same Python-ctor surface the printer emits is exactly what `IRWriter` (`penguin/ir/IRWriter.so`, `serialize_dep_edge` / ctor-emit methods) writes when dumping live IR. The printer's *output format* and the IR's *serialization format* are one and the same runnable-Python form. A reimplementer who chooses a binary or protobuf serialization for the mid IR has diverged from the architecture: the design intent is that emitted IR is replayable Python source.

---

## The Downward Seam — Penguin → BIR (`BirCodeGenLoop` visitor)

### Purpose

After the middle-end has run instruction selection and allocators, the Penguin graph is at the `TongaISAInst` level — every op-level node has been lowered to a concrete per-engine ISA instruction, every tensor has a `TongaTensor` placement, every access is a tile-level `(partition_ap, free_ap)` descriptor. `BirCodeGenLoop` walks this graph and emits the equivalent `bir::` C++ node tree. Its banner pins the input level verbatim.

### Entry Point

`BirCodeGenLoop` subclasses the shared `DotTransform` pass-visitor base (the same base `peephole`/`legalize` passes use), so it walks the IR with the standard per-node hooks and emits BIR as a side effect instead of mutating in place.

```text
BirCodeGenLoop  (subclass of DotTransform)        ── banner: "Generate Backend IR from
  ├─ beforeStmtTransform / afterStmtTransform      ── tensoriser IR at the TongaISAInst level"
  ├─ addInstToBir                                  ── push one emitted bir Instruction
  ├─ codegenScopeRegion / codegenLoop / codegenWhile  ── scope / loop-nest / structured loop
  ├─ codegenMemoryLoc                              ── buffer placement (TongaTensor → bir mem)
  ├─ add*AP helpers  (addReduceAP, addSeqAccess, addSparseMatmulAP, …)  ── Access → sNdM AP
  └─ ~100 codegen<Op>  (codegenMatMulOp, codegenDMACopyOp, codegenAllReduceOp, …)
```

All of `beforeStmtTransform`, `addInstToBir`, `codegenScopeRegion`, `codegenLoop`, `codegenWhile`, `codegenMatMulOp`, `addReduceAP`, `addSeqAccess`, `addSparseMatmulAP` are all present in `BirCodeGenLoop.so`'s string pool.

### The Node Map (the core of this page)

This is the structural correspondence a reimplementer must reproduce. Each Penguin node lowers to a fixed BIR node; the relationship is the contract of the visitor.

| Penguin node | BIR node | How emitted |
|---|---|---|
| `AffineAxis` (loop axis `{iv,lb,ub,stride}`) | `BirAxis` | `codegenLoop` |
| `DynamicAxis` (runtime-`ub` loop) | `BirDynamicForAxis` | `codegenLoop` (dynamic branch) |
| `AffineExpr` (pelican quasi-affine) | `BirQuasiAffineExpr` (a.k.a. `BirAffineExpr`) | inline during access/predicate lowering |
| `AffinePredicate` (per-Inst guard) | `BirAffinePredicate` | inline during inst lowering |
| `DependencyEdge {FLOW/ANTI/OUTPUT/ORDERED}` | inline `addDependency(target, EdgeKind)` | edge-set emit (see callout) |
| `Access` / `AffineAccess` / `TileAccess` | `bir` access-pattern (sNdM) struct | `add*AP` helpers |
| `Tensor` (placed `TongaTensor`) | `bir` tensor + memory location | `codegenMemoryLoc` |
| each `TongaISAInst` (the ~100 ISA insts) | one `bir::Instruction` | one `codegen<Op>` each |
| `ScopeRegion` / `While` | `bir` scope / structured loop | `codegenScopeRegion` / `codegenWhile` |

The five `Bir*` node-type names (`BirAxis`, `BirDynamicForAxis`, `BirQuasiAffineExpr`, `BirAffinePredicate`, `BirAffineExpr`) are all interned in `BirCodeGenLoop.so`.

> **GOTCHA — the dependency-edge representation flips at this seam.** In Penguin the dep graph lives at the **Function** level: a flat list of first-class `DependencyEdge(src, dst, kind)` objects on `Function.dep_edges`. In BIR the edge is *inline* in the target instruction's dependency set, keyed by the target's **name** (a hash), and a single `(A→B)` pair collapses to the **MAX** `EdgeKind`. `BirCodeGenLoop` performs this conversion — Function-level edge list → per-target inline `addDependency` calls with name-hashing and max-merge. A reimplementer who mirrors BIR's inline model upward, or Penguin's flat-list model downward, will mis-model the dependency layer. The Penguin `EdgeKind` member set `{FLOW, ANTI, OUTPUT, ORDERED}` is present in `ir/Dependency.so`; BIR's twin adds the `Invalid0` sentinel. The MAX-merge itself is a BIR-side representation choice — see Part 7.

### Instruction Selection — the in-place op → ISAInst step

The visitor cannot run until the Penguin graph is already at the ISA-instruction level — `codegenMatMulOp` expects a `MatMulOp` ISA inst, not a `TensorContractOp` op-level node. The conversion is **instruction selection**, run as a pass *inside* `codegen_optimization` (`SundaISel` / `TongaISel`), before the BIR exit. ISel is the in-place rewrite that replaces each high-level `Operator` with its target ISA instruction(s):

```c
// SundaISel / TongaISel — runs in CodeGenFlow.codegen_optimization, BEFORE BirCodeGenLoop
// Pivots the SAME Penguin object graph from op-level to ISAInst-level (in place).
function selectInstructions(function):
    for op in function.all_insts:
        switch op.kind:
          case TensorContractOp:   replaceWith(op, MatMulOp / MatMulMXOp / MatMulSparseOp);
          case ReduceOp:           replaceWith(op, TensorReduceOp / PartitionReduceOp);
          case ElementwiseOp:      replaceWith(op, TensorTensorOp / ActivationOp / TensorScalarOp);
          case BroadcastOp:        replaceWith(op, BroadcastOp / BroadcastPartition);   // engine form
          case AllReduceOp:        replaceWith(op, AllReduceOp / TiledAllReduceOp);     // CC ISA form
          ...                       // ~one ISAInst family per op family
    // post-condition: every node is a TongaISAInst — the level BirCodeGenLoop.codegen<Op> consumes
```

> **NOTE —** the op-name and ISA-inst-name spaces overlap (`AllReduceOp` is both an op-level collective and an ISA collective inst), which is why the `codegen<Op>` roster and the op family roster look similar. They are *not* the same set: the `codegen<Op>` dispatch keys on the **ISA-inst** node that ISel produced, listed under the families below. The `codegen<Op>` count is ~100 distinct methods (the `codegen[A-Z]…` interned-string sweep over `BirCodeGenLoop.so` yields ~69 full canonical names plus the macro/scope variants — the truncated duplicate strings in the pool are Cython interning artifacts, not extra methods). The order of magnitude is firm; the exact integer is not pinnable, because interning truncation blurs the count.

### The `codegen<Op>` Dispatch — by family

Do not enumerate all ~100 here; the per-op detail is [Part 6 / BirCodeGenLoop](../nki/). The dispatch space is best described by its families:

| Family | Representative `codegen<Op>` methods | Engine / role |
|---|---|---|
| Memory / DMA | `codegenSBAtomLoad/Store`, `codegenDMACopyOp`, `codegenDMATranspose`, `codegenMemsetOp`, `codegenTensorCopyOp`, `codegenIndirectCopy` | DMA / pool engines, SBUF↔DRAM |
| Compute (PE/Act/Vector) | `codegenMatMulOp`, `codegenMatMulMXOp`, `codegenMatMulSparseOp`, `codegenActivationOp`, `codegenTensorTensorOp`, `codegenTensorReduceOp`, `codegenPartitionReduceOp` | PE array / Activation / Vector |
| Transpose / shuffle | `codegenTransposeOp`, `codegenTransposeTensorReduceOp`, `codegenStreamShuffleInst` | transpose / stream-shuffle |
| Sunda DVE / BatchNorm | `codegenSundaMax`, `codegenSundaBNStats/BNAggr/BNGradient`, `codegenSundaCustomOp` | Sunda DVE, BN stats |
| Collectives | `codegenAllGatherOp`, `codegenAllReduceOp`, `codegenAlltoAllOp`, `codegenReduceScatterOp`, `codegenCollectivePermuteOp`, `codegenCoreBarrierOp`, `codegenGetGlobalRankId` | LNC / collective engine |
| Kernels / offloaded | `codegenBIRKernel`, `codegenMLPKernel`, `codegenAttentionMMSoftmaxMM`, `codegenBackwardsAttention`, `codegenExternalNativeNkiKernel`, `codegenOffloadedFMA/MemCpy` | embedded NKI / fused kernels |
| Misc | `codegenIndexValueInst`, `codegenInlineASMInst`, `codegenNeuronPrintInst`, `codegenBroadcastOp`, `codegenBroadcastPartition`, `codegenNoOp` | index-value, inline ASM, print, broadcast |
| Scope / region | `codegenScopeRegion`, `codegenLoop`, `codegenWhile`, `codegenMemoryLoc` | control / placement |

Representatives confirmed by grep (`codegenMatMulOp`, `codegenAllGatherOp`, `codegenAllReduceOp`, `codegenAlltoAllOp`, `codegenCollectivePermuteOp`, `codegenCoreBarrierOp`, `codegenGetGlobalRankId`, `codegenBIRKernel`, `codegenAttentionMMSoftmaxMM`, `codegenBackwardsAttention`, `codegenExternalNativeNkiKernel`, `codegenBroadcastOp`, `codegenBroadcastPartition`, `codegenDMACopyOp`, `codegenDMATranspose`, `codegenScopeRegion`, `codegenLoop`, `codegenWhile`, `codegenCustomOp`, `codegenDropoutMaskInst`) are each present in the pool. The remaining members of each family are carried from the BirCodeGenLoop roster rather than individually re-derived.

### The Access → AP-struct helpers

A Penguin operand binding is `(Tensor, Access)`. After tiling, the `Access` is a `TileAccess` carrying a partition-AP (the 128-partition SBUF/PE dimension) plus free-APs (the in-tile contiguous dims). `BirCodeGenLoop`'s `add*AP` helpers turn this into the BIR `sNdM` (sN-source / dM-dest) access-pattern struct that the ISA instruction encoding requires:

| Penguin access shape | `add*AP` helper | BIR form |
|---|---|---|
| reduction access (with reduce axes) | `addReduceAP` | reduce AP |
| sequential / contiguous | `addSeqAccess` | seq AP |
| sparse-matmul operand | `addSparseMatmulAP` | sparse-MM AP |
| complicated multi-dim DMA | `addComplicatedDMAAP` / `codegenNdDMAAP` | N-D DMA AP |
| opaque / data-dependent | `addOpaqueAP` | opaque AP |
| double-row / batch-transpose | `addDoubleRowAP` / `addBatchTransposeAP` | engine-specific AP |
| SB↔SB collective (delinearized) | `add_sb_to_sb_cc_ap` | CC AP |

Confirmed helpers in `BirCodeGenLoop.so`: `addReduceAP`, `addSeqAccess`, `addSparseMatmulAP`, `addComplicatedDMAAP`, `addOpaqueAP`, `addDoubleRowAP`, `addBatchTransposeAP`, `codegenNdDMAAP`, and `add_sb_to_sb_cc_ap`.

---

## Node-Identity Anchors (the serialize formats)

Two textual serialize formats are worth pinning because they fix the *shape* of the two key node types across both seams, and both are verbatim-confirmed in the binary:

```text
AffineAxis.serialize   →   "for ({it}: range({lb}, {ub}, {stride}))"
Tensor.serialize       →   "{attr}{dtype} {shape} {name}{init}"
```

Both appear verbatim in `ir/Axis.so` and `ir/Tensor.so` respectively. The first tells you a Penguin loop axis *is* a Python `for`-over-`range` — which is why `codegenLoop` maps it cleanly to a `BirAxis` with the same `(lb, ub, stride)` triple. The second gives the five core abstract-tensor fields (`attr, dtype, shape, name, init`) that `codegenMemoryLoc` consumes (after the tiler has chosen a `TongaTensor` placement). The `Module` docstring `"…connected functions forming a neural network"` is likewise verbatim in `ir/Module.so`, anchoring the top of the placement chain.

---

## Evidence summary

Three of this page's central claims rest on strings that are unambiguous in the binaries.

**The upward seam is a textual Python emit.** `hlo2penguin` embeds `mhlo-to-py-penguin` / `stablehlo-to-py-penguin` — note the `-to-py-` infix — alongside the `print<Op>` emitter naming and, decisively, the dotted import string `neuronxcc.starfish.penguin.ir.Dependency` baked into a *C++* binary that never runs Python. A tree-builder would link those classes; only a source emitter needs their import path as text.

**`BirCodeGenLoop` consumes Penguin at the `TongaISAInst` level.** The banner string `"Generate Backend IR from tensoriser IR at the TongaISAInst level"` is verbatim in `BirCodeGenLoop.so`.

**The `Bir*` node names are real, not invented.** All five — `BirAxis`, `BirDynamicForAxis`, `BirQuasiAffineExpr`, `BirAffinePredicate`, `BirAffineExpr` — are interned strings in `BirCodeGenLoop.so`.

### Limits of this reading

Two claims are softer than the rest.

The **~100 `codegen<Op>` count** is a magnitude, not a number. A `codegen[A-Z]…` sweep yields roughly 69 full canonical names plus scope and macro variants, and the pool also holds truncated duplicates that are Cython interning artifacts (`codegenActivate2` appearing beside `codegenActivationOp`, for instance). The exact integer is not pinnable from a truncated pool.

The **`addDependency` name and the MAX-merge** are BIR-side behaviour described from the BIR strand, not from this module. `addDependency` does not appear in `BirCodeGenLoop.so`'s pool at all — it is a `bir::` C++ method called across the Python→C++ boundary, so it is never interned as a Python attribute. What *is* readable here is the Penguin side: the Function-level `DependencyEdge` model and the four `EdgeKind` members `{FLOW, ANTI, OUTPUT, ORDERED}`, present in `ir/Dependency.so`.

---

## Related Components

| Name | Relationship |
|---|---|
| `hlo2penguin` (Part 4.43/4.44) | Upward seam — emits the Python ctors that build the Penguin `Module` |
| `CodeGenFlow` / `SharedCodeGenFlow` | The middle-end driver between the two seams; runs ISel (the level pivot) |
| `SundaISel` / `TongaISel` | The in-place op→ISAInst instruction-selection pass |
| `BirCodeGenLoop` (Part 6) | Downward seam — the ~100 `codegen<Op>` visitor → BIR |
| BIR / libBIR (Part 7) | The target of the downward seam; inline-edge, name-keyed dependency model |

## Cross-References

- [Penguin IR Node Model](ir-node-model.md) — the SSA def-use graph this page maps the *seams* of, not the schema
- [Penguin Axis / Loop-Axis Model](axis-loop-model.md) — `AffineAxis`/`DynamicAxis`, the `BirAxis`/`BirDynamicForAxis` sources
- [Penguin AffineExpr Algebra](affine-expr-algebra.md) — the pelican quasi-affine algebra that becomes `BirQuasiAffineExpr`
- [Penguin Dependency Model](dependency-model.md) — `DependencyEdge`/`EdgeKind`, the Function-level edge list that flips to inline at the seam
- [Penguin Tensor / Buffer Node](tensor-buffer-node.md) — `Tensor`→`TongaTensor` placement consumed by `codegenMemoryLoc`
- [hlo-opt + hlo2penguin (Part 4)](../hlo-opt/) — `MhloToPythonPrinter`, the textual-Python upward seam (4.43/4.44)
- [Pipeline Overview (0.2)](../front/pipeline.md) — where Penguin sits in the full MLIR→Penguin→BIR→KELF flow
