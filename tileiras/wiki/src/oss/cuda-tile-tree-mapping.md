# cuda_tile Tree Mapping

## Abstract

The public `cuda-tile` repository contains two C++ source files that describe the dialect as actual code: `Interfaces.cpp`, which is mostly an ODS-generated stub that hosts the interface implementation; and `CudaTileOptimizer.cpp`, the standalone tool that takes TileIR in, runs an optimizer pipeline, and emits TileIR or LLVM bytecode out. Together they cover the dialect contract (what verifiers must check) and the dialect's user-facing entry point (how a developer drives the optimizer).

This page maps both files to their tileiras counterparts. The mapping is not symmetric: `Interfaces.cpp` corresponds to a distributed pattern in tileiras (ODS-generated interface code spread across parser/verifier/printer), and `CudaTileOptimizer.cpp` has no standalone counterpart at all — its role is absorbed into the full compile-to-GPU pipeline.

## Interfaces.cpp and Interfaces.td

The OSS `Interfaces.cpp` is a one-screen stub:

```cpp
// Interfaces.cpp (OSS, abbreviated)
#include "CudaTile/IR/Interfaces.h"
#include "CudaTile/IR/Interfaces.cpp.inc"        // ODS-generated TypeInterface bodies
#include "CudaTile/IR/AttrInterfaces.cpp.inc"    // ODS-generated AttrInterface bodies
```

All real interface code lives in the ODS-generated `.cpp.inc` files. The declarations in `Interfaces.td` are what matter for the comparison.

### AssumePredicateAttrInterface

Upstream declaration:

```tablegen
// Interfaces.td (OSS)
def AssumePredicateAttrInterface : AttrInterface<"AssumePredicateAttrInterface"> {
  let cppNamespace = "::mlir::cuda_tile";
  let methods = [
    InterfaceMethod<
      "Verify that the predicate is well-formed for a given assume op.",
      "::mlir::LogicalResult", "verifyWithAssumeOp",
      (ins "::mlir::Operation *":$assumeOp)
    >,
  ];
}
```

Tileiras carries the same interface with the same method signature. The implementation pattern in tileiras: each concrete predicate attribute (`DivByAttr`, `SameElementsAttr`, `BoundedAttr`) declares `AssumePredicateAttrInterface` in its `interfaces` ODS field; the ODS expansion produces a per-attribute `verifyWithAssumeOp` body that runs the attribute-specific check; the `cuda_tile.assume` op verifier resolves the predicate attribute through the interface and dispatches to the concrete implementation.

| Aspect | OSS | Tileiras | Status |
| --- | --- | --- | --- |
| Interface declaration | `Interfaces.td` | matching ODS declaration | PRESENT |
| ODS-generated dispatch glue | `Interfaces.cpp.inc` | inlined into each concrete attribute's verifier slab | INLINED |
| Per-attribute verifier body | one implementation per predicate attribute | one implementation per predicate attribute | PRESENT |
| Interface TypeID | one interned `TypeID` shared by all implementors | same — single interned `TypeID` per interface | PRESENT |

The divergence is the location of the dispatch glue. OSS keeps it in one `.cpp.inc` that `Interfaces.cpp` includes; tileiras inlines the same dispatch into each concrete attribute's slab during ODS expansion. Both call the same per-attribute `verifyWithAssumeOp` body. The semantic contract is identical.

### TileView TypeInterface

Upstream declaration:

```tablegen
// Interfaces.td (OSS)
def TileView : TypeInterface<"TileView"> {
  let cppNamespace = "::mlir::cuda_tile";
  let methods = [
    InterfaceMethod<
      "Returns the rank of the view's index space.",
      "int64_t", "getViewIndexRank"
    >,
    InterfaceMethod<
      "Returns the tile type produced when the view is fully indexed.",
      "::mlir::Type", "getViewTileType"
    >,
  ];
}
```

Tileiras carries the same two methods on the same interface. The view types implementing it are `cuda_tile.tensor_view` and `cuda_tile.partition_view`.

| Aspect | OSS | Tileiras | Status |
| --- | --- | --- | --- |
| Interface declaration | `Interfaces.td` | matching ODS declaration | PRESENT |
| `getViewIndexRank()` | per-view-type implementation | per-view-type implementation | PRESENT |
| `getViewTileType()` | per-view-type implementation | per-view-type implementation | PRESENT |
| Consumers | view-consuming op verifiers call interface methods | same set of view-consuming ops use the same interface methods | PRESENT |

Same status as `AssumePredicateAttrInterface`: declaration identical, dispatch glue location differs, semantics preserved.

### AllElementTypeMatch Predicate

Upstream declaration:

```tablegen
// Interfaces.td (OSS, predicate trait)
class AllElementTypeMatch<list<int> indices> : PredOpTrait<...> { ... }
```

This is not a runtime-dispatched interface — it is a generated ODS predicate that emits a static check into each consuming op's verifier. OSS centralizes the predicate template in `Interfaces.td` and lets the TableGen expander inline it per use.

Tileiras follows the same model. The predicate is INLINED at every consuming op verifier: the ODS expander emits the same element-type-match check into each verifier body. No central helper exists at runtime in either tree; both spell out the check at every use site.

| Aspect | OSS | Tileiras | Status |
| --- | --- | --- | --- |
| Predicate template | `Interfaces.td` | identical template | PRESENT |
| Runtime helper function | none — generated inline | none — generated inline | INLINED |
| Per-op verifier code | one inlined predicate per consuming op | one inlined predicate per consuming op | PRESENT |

## CudaTileOptimizer.cpp

The OSS driver is a standalone tool. Its `main` function follows a textbook MLIR-tool shape:

```cpp
// CudaTileOptimizer.cpp (OSS, abbreviated)
int main(int argc, char **argv) {
  mlir::registerAllPasses();
  cuda_tile::registerOptimizerPasses();

  mlir::DialectRegistry registry;
  cuda_tile::registerDialects(registry);

  MLIRContext ctx(registry);
  ctx.loadAllAvailableDialects();

  // Parse input — accepts TileIR bytecode or textual MLIR.
  OwningOpRef<Operation *> module = parseSourceFile(input_file, &ctx);
  if (!module) return 1;

  // Build pass manager rooted at cuda_tile::EntryOp.
  PassManager pm(&ctx);
  pm.addNestedPass<cuda_tile::EntryOp>(createFuseFMAPass());
  pm.addPass(createCanonicalizerPass());
  pm.addPass(createCSEPass());
  pm.addPass(createLoopInvariantCodeMotionPass());
  pm.addNestedPass<cuda_tile::EntryOp>(createLoopSplitPass());

  // Accept optional pre/post textual pipeline fragments.
  applyTextualPipelineFragments(pm, pre_fragment, post_fragment);

  if (failed(pm.run(*module))) return 1;

  // Emit TileIR bytecode, memory bytecode, MLIR file, or MLIR stdout.
  return emitOutput(*module, output_kind, output_file);
}
```

Tileiras has no standalone optimizer entry point. The same passes — FMA fusion, canonicalization, CSE, LICM, loop splitting — exist or have replacements in the full compile pipeline, but they are reached as part of `tileiras_compile()`, not as a `cuda_tile-opt`-style tool. The compile pipeline does not stop at `cuda_tile`; it lowers through `nv_tileaa`, `nv_tileas`, `cute_nvgpu`, `cutlass`, and `nvvm`, then runs the NVPTX backend.

| Driver component | OSS behavior | Tileiras behavior | Divergence kind |
| --- | --- | --- | --- |
| Input format | TileIR bytecode or textual MLIR | TileIR bytecode only | Semantic (textual MLIR rejected) |
| Optimizer anchor | `cuda_tile::EntryOp`-nested pass manager | full pipeline; per-pass anchors vary | Anchor-op |
| FMA fusion | `FuseFMA` at `cuda_tile` layer | `tileas-legalize-fma-dot` plus NVPTX `-nvptx-fma-level` | Layering (SUPERSEDED) |
| Canonicalization | `createCanonicalizerPass()` at `cuda_tile` layer | canonicalizer runs after every lowering stage | Granularity (split) |
| CSE | `createCSEPass()` at `cuda_tile` layer | CSE runs at multiple lowering layers | Granularity (split) |
| LICM | `createLoopInvariantCodeMotionPass()` at `cuda_tile` layer | LICM runs at the `nv_tileas` and LLVM layers | Layering (REWRITTEN) |
| Loop splitting | `LoopSplit` at `cuda_tile` layer | no equivalent at any layer | ABSENT |
| Textual pipeline fragments | `applyTextualPipelineFragments()` | no equivalent; pipeline is fixed by opt-level | ABSENT |
| Output: TileIR bytecode | emit bytecode | not exposed as terminal output | ABSORBED |
| Output: TileIR memory bytecode | emit memory bytecode | not exposed as terminal output | ABSORBED |
| Output: MLIR file/stdout | emit textual MLIR | not exposed as terminal output | ABSORBED |
| Output: LLVM bitcode | emit LLVM bitcode | not exposed as terminal output | ABSORBED |
| Terminal output | one of the four above | PTX text or CUBIN binary | Layering |
| Pass registration | one helper that adds the optimizer passes | distributed across dialect and extension installers | Structural |

The driver's anchor — `cuda_tile::EntryOp` — is the structural reason the OSS optimizer cannot be lifted directly into tileiras. Once the pipeline lowers past `cuda_tile`, no `EntryOp` exists to anchor a nested pass manager against. The OSS-style pass scheduling assumes the IR stays in `cuda_tile` for the entire optimizer run; tileiras's pipeline schedules each pass against whichever dialect is current at that point.

### What Survives

The pass concepts survive. FMA fusion is a real concern in tileiras — it just happens at the TileAS and NVPTX backend layers rather than at `cuda_tile`. Canonicalization, CSE, and LICM are real concerns in tileiras — they run between every lowering stage rather than in one batch. Loop splitting is the one OSS pass with no tileiras counterpart at any layer; a reimplementer adding it would be extending tileiras's capabilities rather than reproducing them.

### What Does Not Survive

The standalone `cuda-tile-opt`-style tool does not survive. The four output kinds do not survive at the tool level. The textual pipeline fragments do not survive — tileiras's pipeline is built per opt-level by a fixed builder rather than assembled from caller-supplied textual fragments.

A tileiras-compatible compiler should not expose a `CudaTileOptimizer`-shaped tool unless the goal is to add a tile-level optimizer that does not exist in the released binary. The full compile pipeline is the supported entry point.

## Generated Code Layout

Both files include ODS-generated `.cpp.inc` content. The mapping for the generated pieces:

| Generated artifact | OSS | Tileiras | Status |
| --- | --- | --- | --- |
| `Dialect.cpp.inc` (dialect registration) | included in dialect translation unit | inlined into the dialect ctor slab | INLINED |
| `Ops.cpp.inc` (op classes) | included in ops translation unit | inlined into per-op slabs | INLINED |
| `Types.cpp.inc` (type classes) | included in types translation unit | inlined into per-type slabs | INLINED |
| `AttrDefs.cpp.inc` (attribute classes) | included in attrs translation unit | inlined into per-attribute slabs | INLINED |
| `Interfaces.cpp.inc` | included in `Interfaces.cpp` | inlined into each concrete implementor | INLINED |
| `AttrInterfaces.cpp.inc` | included in `Interfaces.cpp` | inlined into each concrete implementor | INLINED |
| `Passes.cpp.inc` (pass registration helpers) | included in pass-registration TU | spread across dialect and extension installers | REWRITTEN |

The cross-cutting pattern: tileiras's LTO build inlines ODS-generated dispatch into each concrete consumer rather than concentrating it in central includes. The behavior at the source-language level is identical; the build-time factoring differs.

## Reimplementation Guidance

For a tileiras-compatible reimplementation:

- Use OSS `Interfaces.td` as the authoritative declaration of the dialect's interfaces. The three interfaces (`AssumePredicateAttrInterface`, `TileView`, `AllElementTypeMatch`) are unchanged.
- Implement `AssumePredicateAttrInterface` on the three predicate attributes (`DivByAttr`, `SameElementsAttr`, `BoundedAttr`). Implement `TileView` on the two view types (`cuda_tile.tensor_view`, `cuda_tile.partition_view`).
- Do not expose a standalone `cuda-tile-opt`-shaped tool. The driver layer in `tileiras` is `tileiras_create_program` + `tileiras_compile_program` + `tileiras_get_output`; reimplement those, not the OSS optimizer.
- Do not accept textual MLIR input — tileiras consumes TileIR bytecode only.
- Do not register a `LoopSplit` pass for compatibility. It has no tileiras counterpart.
- FMA fusion, canonicalization, CSE, and LICM should run at the tileiras-equivalent layers (TileAS for FMA, between every lowering stage for the others), not at the `cuda_tile` layer with the OSS scheduling.

## Cross-References

- [OSS Comparison Overview](overview.md) — the divergence taxonomy used in the tables above.
- [.td Files Delta](td-files-delta.md) — the TableGen-declared surface that `Interfaces.cpp` consumes.
- [Transforms / FuseFMA / SynthDbg](transforms-fusefma-synthdbg.md) — the OSS optimizer pass set in detail.
- [cuda_tile Verifiers](../dialects/cuda_tile/verifiers.md) — how the interfaces declared here are consumed at verify time inside tileiras.
- [Driver Overview](../driver/overview.md) — the supported entry point that replaces the OSS standalone tool.
