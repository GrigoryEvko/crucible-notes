# cuda_tile Tree Mapping (OSS ↔ binary)

## Abstract

This page maps two public `cuda-tile` components to their tileiras roles:
`Interfaces.cpp`, which is mostly generated interface glue, and `CudaTileOptimizer.cpp`, which is
the standalone OSS optimizer driver. Together they define the public dialect contract and the
small OSS optimize-and-emit flow.

Tileiras preserves the interface semantics, but not as a neat `Interfaces.cpp` island. The ODS
interface checks are distributed across parsers, verifiers, printers, and generated constraint
sites. The OSS optimizer driver is more heavily changed: its useful passes are reused, but the
standalone "optimize Tile IR and emit bytecode or MLIR" entry point is absorbed into the full
compile-to-GPU pipeline.

## Interfaces

The public `Interfaces.cpp` file is a stub that includes the TableGen-generated implementation.
The real contract is declared in `Interfaces.td`:

- `AssumePredicateAttrInterface`: an attribute interface used by `cuda_tile.assume`.
- `TileView`: a type interface for view-like tile types.
- `AllElementTypeMatch`: a generated predicate trait used by operation verifiers.

In tileiras, those artifacts appear through normal MLIR/ODS lowering:

| Public artifact | Tileiras role | Reimplementation guidance |
| --- | --- | --- |
| `AssumePredicateAttrInterface` | `assume` parses a `predicate` attribute and verifies that it implements the interface. | Use MLIR's normal attr-interface machinery; call `verifyWithAssumeOp` from the op verifier. |
| `TileView` | View-consuming ops verify that operands implement the tile-view type interface. | Keep `getViewIndexRank()` and `getViewTileType()` on the type interface, not on individual ops. |
| `AllElementTypeMatch` | Element-type equality checks are inlined into generated op verifiers. | It is fine for this to be generated at use sites; no shared helper is required. |

The interface TypeID keys are cached and initialized once, following MLIR's normal interned-TypeID
pattern. Concept keys use the MLIR interface suffix convention, so a reimplementation should not
invent its own identity scheme for these interfaces.

## Optimizer Driver

The public optimizer is a small, standalone Tile IR tool. It builds a pass manager nested under
`cuda_tile::EntryOp`, adds FMA fusion, canonicalization, CSE, LICM, and loop splitting, accepts
optional caller-supplied textual pipeline fragments, and can emit Tile IR bytecode or textual MLIR.

Tileiras does not expose that tool as a standalone output path. The corresponding role is absorbed
into the full compile pipeline:

| OSS behavior | Tileiras behavior |
| --- | --- |
| Parse Tile IR bytecode or textual MLIR. | Input validation expects Tile IR bytecode; textual MLIR fallback is absent. |
| Run an optimizer rooted at `cuda_tile::EntryOp`. | Optimization is nested under the GPU and NVIDIA tile dialect pipeline. |
| Emit Tile IR bytecode, memory bytecode, MLIR file, or MLIR stdout. | The compiler proceeds toward PTX/SASS/CUBIN emission. |
| Accept pre/post textual pipeline fragments. | The production pipeline is built by fixed per-optimization-level builders. |
| Register tile optimizer passes through a small helper. | Pass registration is distributed across dialect and extension installers. |

The reusable pass concepts are still present: FMA handling, canonicalization, CSE, LICM, and loop
splitting all have tileiras counterparts. The difference is packaging. The public tree provides a
developer-facing optimizer utility; tileiras integrates the same class of transformations into a
larger compiler driver.

## Reimplementation Notes

For an open reimplementation, keep the two layers separate:

- Implement the public `cuda_tile` interfaces with ordinary MLIR TableGen and TypeID mechanics.
- Let `AllElementTypeMatch` remain generated verifier code unless there is a strong reason to
  centralize it.
- Provide a standalone optimizer only if you want OSS-tool compatibility.
- For tileiras compatibility, model the production path as bytecode input followed by the full
  GPU/NVIDIA dialect lowering pipeline.
- Do not require textual-pipeline injection or Tile IR bytecode re-emission in the production path
  unless you are deliberately rebuilding the OSS utility surface.
