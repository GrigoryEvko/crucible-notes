# OSS Comparison Overview

## Abstract

The public `cuda-tile` repository is a partial upstream for Tile IR. It exposes the `cuda_tile`
dialect, its TableGen declarations, and a small transform/optimizer layer. Tileiras contains that
slice, but it also contains many NVIDIA-private dialects and lowering pipelines around it:
`nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, `cutlass`, NVVM, and the NVPTX backend.

The useful comparison therefore runs from OSS to tileiras, not from tileiras to OSS. For each
public artifact, the wiki asks whether tileiras carries the same behavior, absorbs it into a
larger driver, replaces it at another layer, or omits it entirely. That keeps the comparison
bounded and prevents the private compiler pipeline from being misread as "missing" from OSS.

## OSS Counterpart

The public tree contains:

- one MLIR dialect, `cuda_tile`
- TableGen sources for operations, types, attributes, interfaces, and passes
- transform passes such as FMA fusion, loop splitting, and debug-scope synthesis
- an optimizer driver around the public `cuda_tile` module shape
- a thin `Interfaces.cpp` stub that hosts generated interface code

All other tileiras dialect clusters are outside the public tree. Reimplementation work should use
the OSS tree for the `cuda_tile` surface, then rely on the rest of this wiki for private dialect
semantics and NVPTX lowering behavior.

## Match Categories

| Category | Meaning |
| --- | --- |
| `PRESENT` | Tileiras carries the same public behavior with a recognizable implementation shape. |
| `REWRITTEN` | The role is preserved, but the implementation is split or structured differently. |
| `ABSORBED` | A public helper is folded into a larger tileiras driver. |
| `SUPERSEDED` | A different compiler layer provides the same semantic effect. |
| `INLINED` | The artifact exists at use sites rather than as an out-of-line helper. |
| `PARTIAL` | Some public behavior matches, while another part is changed or missing. |
| `ABSENT` | The public artifact has no observable counterpart in tileiras. |

The most common divergence types are structural, semantic, granularity, anchor-op, ABI, and
layering differences. Structural divergence keeps behavior but changes the call graph. Semantic
divergence changes behavior. Granularity divergence folds or splits an OSS unit. Anchor-op
divergence means a pass is nested under a different MLIR operation. ABI divergence covers changed
parameter or storage layout. Layering divergence means a public pass is replaced by a lower or
higher compiler layer.

## Documentation Rule

The public wiki describes behavior, contracts, and reimplementation guidance. It does not depend on
raw reverse-engineering notes being visible to readers, and it avoids treating internal symbol
names as the main result. When the OSS tree is relevant, the page names the public file or artifact
and explains the behavior in original prose rather than copying source bodies.
