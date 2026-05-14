# OSS Comparison Overview

## Abstract

NVIDIA ships a small open-source preview of the `cuda-tile` dialect: one MLIR dialect declaration, three TableGen files, three transform passes, a standalone optimizer driver, and a thin interface-glue stub. Tileiras is a much larger compiler — twelve dialects, a four-stage lowering cascade, a modulo scheduler, and an NVPTX backend with private peephole passes — but the OSS preview is the only point where parts of the internal IR surface are visible in original source form.

The four OSS pages compare that preview against tileiras. The comparison is not symmetric. The OSS tree is a strict subset of one front-end dialect; tileiras carries the same surface plus six private dialects (`nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, `cutlass`, NVVM) and the lowering pipelines between them. The useful question is: for each artifact in the public tree, what shape does the corresponding behavior take in tileiras?

The comparison methodology, the divergence taxonomy, and the per-page table conventions appear below. The other three OSS pages apply the methodology to TableGen declarations ([.td Files Delta](td-files-delta.md)), interface and optimizer driver source ([cuda_tile Tree Mapping](cuda-tile-tree-mapping.md)), and transform passes ([Transforms / FuseFMA / SynthDbg](transforms-fusefma-synthdbg.md)).

## What the OSS Preview Contains

The public `cuda-tile` repository ships five categories of source:

| Category | Files | What it declares |
| --- | --- | --- |
| Dialect TableGen | `Types.td`, `AttrDefs.td`, `Ops.td` | The `cuda_tile` type, attribute, and operation surface. |
| Interface glue | `Interfaces.cpp`, `Interfaces.td` | The attribute-interface and type-interface declarations consumed by op verifiers. |
| Transform passes | `FuseFMA.cpp`, `LoopSplit.cpp`, `SynthesizeDebugInfoScopes.cpp` | Three optimization passes operating on `cuda_tile` IR. |
| Optimizer driver | `CudaTileOptimizer.cpp` | A standalone tool that loads TileIR, runs an optimizer pipeline, and emits TileIR or LLVM bytecode. |
| Build glue | CMake fragments, pass registration helpers | The supporting infrastructure to compile the preview as a standalone library. |

Everything else in tileiras — the private dialect chain, the NVPTX backend, libdevice integration, the modulo scheduler, the bytecode I/O — has no OSS counterpart. The OSS pages do not attempt to invent one.

## What "Comparison" Means Here

For each public artifact, the comparison answers four questions:

1. Does tileiras carry the same behavior in a recognizable form?
2. If it does, is the implementation structured the same way, or split, merged, or relocated to another layer?
3. If it does not, was the artifact replaced by something else, deleted entirely, or scheduled for a later release?
4. What can a reader infer about the public design from the tileiras shape, and vice versa?

The comparison runs from OSS to tileiras, not the other way around. Asking "what's missing from OSS that tileiras has" is a much larger question and would dominate the page with material that has no public counterpart. The OSS-to-tileiras direction stays bounded by the public surface.

## Divergence Taxonomy

Comparing two implementations of a dialect surface produces seven recurring outcomes. The OSS pages use them as a controlled vocabulary:

| Status | Meaning |
| --- | --- |
| `PRESENT` | The public artifact exists in tileiras with the same role and a recognizable implementation shape. |
| `REWRITTEN` | The role is preserved, but the implementation is split across multiple sites or restructured around a different anchor. |
| `ABSORBED` | A public helper is folded into a larger tileiras driver — the function disappears as a named unit but its work happens inline at the caller. |
| `SUPERSEDED` | A different compiler layer (TileAS, NVPTX backend, libdevice) provides the same semantic effect. |
| `INLINED` | The artifact exists at use sites rather than as an out-of-line helper — common for generated ODS predicates and small verifier templates. |
| `PARTIAL` | Some public behavior matches in tileiras while another part is changed, missing, or relocated. |
| `ABSENT` | The public artifact has no observable counterpart in tileiras — either deleted, replaced by a different mechanism entirely, or scheduled for a later compiler release. |

The seven statuses are not orthogonal — a SUPERSEDED pass is also, by definition, ABSENT at its original layer — but the distinction matters because a reimplementer needs to know whether to look elsewhere in tileiras for the behavior or whether to leave the gap unfilled.

## Divergence Kinds

Cutting the same surface a different way: every concrete delta is one of six kinds.

| Kind | What changes | Example |
| --- | --- | --- |
| Structural | Behavior is preserved, call graph is not. | OSS `Interfaces.cpp` includes generated code in one file; tileiras spreads the same code across parser, verifier, and printer call sites. |
| Semantic | Behavior changes. | `cuda_tile.print` accepts a `cuda_tile.string` type that the OSS dialect does not declare. |
| Granularity | A public unit is folded into a larger driver or split into smaller ones. | The OSS optimizer driver is absorbed into the full compile-to-GPU pipeline rather than exposed as a standalone tool. |
| Anchor-op | A pass is nested under a different MLIR operation. | OSS `FuseFMA` is rooted at `cuda_tile::EntryOp`; the closest tileiras pass is rooted at `nv_tileas` and runs on a different IR. |
| ABI | Parameter or storage layout differs. | OSS `PipelineState` is a C++ template member tuple; tileiras `!cutlass.pipeline_state` is a typed MLIR value with explicit phase/index/count fields. |
| Layering | A public pass is replaced by a lower or higher compiler layer. | OSS `SynthesizeDebugInfoScopes` is replaced by upstream MLIR's `DIScopeForLLVMFuncOp` plus the tileiras `ConvertDebugInfoToLLVM` path. |

Each per-page table identifies which kind applies. Readers implementing a tileiras-compatible compiler can decide on a per-kind basis whether to follow the OSS shape, the tileiras shape, or something else that preserves the same external contract.

## How to Read the Pages

Each of the three detail pages targets one slice of the public tree:

[cuda_tile Tree Mapping](cuda-tile-tree-mapping.md) covers the two C++ source files in the public preview: `Interfaces.cpp` (mostly ODS-generated glue) and `CudaTileOptimizer.cpp` (the standalone driver). Each file gets a per-component table identifying which artifacts are PRESENT, REWRITTEN, ABSORBED, or ABSENT in tileiras, plus prose explaining the structural choices.

[.td Files Delta](td-files-delta.md) covers the three TableGen files. Categories where every declaration is identical get one-line summaries. Categories where tileiras carries a delta get focused tables showing the public declaration shape next to the tileiras-recovered declaration. The known deltas are small in count but each one matters for parser compatibility: one renamed op, one absent op, one added type.

[Transforms / FuseFMA / SynthDbg](transforms-fusefma-synthdbg.md) covers the three transform passes. None of the three survives in `cuda_tile`-dialect form: one is SUPERSEDED by lower layers, one is ABSENT without replacement, and one is replaced by upstream MLIR's debug-scope pass. The page documents each migration target.

## Reimplementation Stance

A tileiras-compatible reimplementation should treat the OSS tree as authoritative for what it covers and the rest of this wiki as authoritative for everything outside the public surface. Specifically:

- Use OSS `Types.td`, `AttrDefs.td`, and `Ops.td` for the `cuda_tile` declaration surface, with the deltas listed in [.td Files Delta](td-files-delta.md) applied.
- Use OSS `Interfaces.cpp` and `Interfaces.td` for the ODS interface shape, but expect that consumers spread across the verifier/parser/printer rather than concentrating in one stub.
- Do not copy the OSS `Transforms/` directory into the lowering pipeline. The three passes have different replacement strategies in tileiras and copying them produces double-firing or anchor-op mismatches.
- Do not expose `CudaTileOptimizer` as a standalone tool unless deliberately adding functionality. Tileiras has no equivalent standalone entry point — the full compile pipeline subsumes the optimizer role.

## Documentation Stance

The OSS pages describe behavior and contracts in prose. They do not depend on raw reverse-engineering notes being visible to readers, and they do not treat internal symbol names as the comparison surface. When the public tree is the relevant reference, the page names the public file or artifact directly; when tileiras-only behavior is described, the page describes the behavior rather than reproducing the implementation.

## Cross-References

- [cuda_tile Tree Mapping](cuda-tile-tree-mapping.md) — the per-file comparison for `Interfaces.cpp` and `CudaTileOptimizer.cpp`.
- [.td Files Delta](td-files-delta.md) — the TableGen-level deltas across types, attributes, and ops.
- [Transforms / FuseFMA / SynthDbg](transforms-fusefma-synthdbg.md) — the three OSS transform passes and their tileiras counterparts.
- [cuda_tile Overview](../dialects/cuda_tile/overview.md) — the dialect as seen from inside tileiras.
