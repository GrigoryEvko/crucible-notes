# Pass List by Optimization Level

## Abstract

This page lists the MLIR-tier passes that Tileiras schedules for each optimization level. It is a
semantic pass list, not an address map. LLVM and MachineIR passes that run inside the downstream NVPTX
backend are documented under [NVPTX Backend Passes](../nvptx-passes/overview.md).

## O0

`O0` is intentionally sparse. It relies on parser checks and pass-manager verifier slots rather than
adding transformation passes.

| Order | Pass | Purpose |
| --- | --- | --- |
| 1 | Verifier slots | Check IR validity at pass boundaries. |

## O1

`O1` performs the minimum useful TileIR lowering.

| Order | Pass | Purpose |
| --- | --- | --- |
| 1 | `convert-cudatile-to-tileaa` | Translate the public `cuda_tile` surface into TileAA. |
| 2 | Optional snapshot printer | Emit a textual IR snapshot when the selected line-info mode requests it. |
| 3 | `tileir-insert-debug-scope` | Add debug scopes used by later diagnostics and line-info emission. |
| 4 | `canonicalize` | Clean simple folds and canonical forms before deeper lowering. |

## O2

`O2` is the default compilation pipeline. It lowers TileAA to TileAS, emits host-wrapper state, and
starts conversion to LLVM/NVGPU.

| Order | Pass | Purpose |
| --- | --- | --- |
| 1 | O1 passes | Establish TileAA and clean the frontend IR. |
| 2 | `convert-tileaa-to-tileas` | Lower architecture-aware TileAA operations to scheduled TileAS forms. |
| 3 | `tileir-emit-host-wrapper` | Build host-side wrapper metadata and launch glue. |
| 4 | `convert-tileas-to-llvm` | Lower TileAS memory, control, and async constructs toward LLVM. |
| 5 | `cse` | Remove redundant values produced by lowering. |
| 6 | Optional snapshot printer | Capture the TileAS/LLVM boundary when the later line-info mode requests it. |
| 7 | `convert-tileas-to-nvgpu` | Lower remaining target GPU operations to NVGPU-compatible forms. |

## O3

`O3` adds the full conversion and finalization stack.

| Order | Pass | Purpose |
| --- | --- | --- |
| 1 | O2 passes | Run the default lowering sequence. |
| 2 | `tileir-verify-ops-analysis` | Check TileIR operation invariants before they are erased. |
| 3 | `host-device-assert-enable` | Enable host/device assertion handling when configured. |
| 4 | O3 debug-scope insertion | Insert the second debug-scope pass used by the full pipeline. |
| 5 | `tileir-gpu-module-prepare` | Prepare the `gpu.module` for final lowering. |
| 6 | `canonicalize` and `cse` | Clean before conversion to LLVM. |
| 7 | `unspecialized-pipeline` | Apply the unspecialized pipeline path when selected. |
| 8 | `test-convert-to-llvm` | Exercise the conversion-interface stack for selected dialects. |
| 9 | `tileir-legalize-llvm-kernel` | Normalize kernel entry ABI before target conversion. |
| 10 | `tileir-finalize-llvm-kernel` | Finalize kernel argument and metadata conventions. |
| 11 | `convert-to-llvm` | Convert standard MLIR dialects to LLVM dialect. |
| 12 | `canonicalize` | Clean after the broad LLVM conversion. |
| 13 | `convert-nvgpu-to-nvvm` | Lower NVGPU operations to NVVM operations. |
| 14 | `convert-vector-to-llvm` | Lower vector dialect operations. |
| 15 | `convert-math-to-funcs` | Route math operations through callable/library forms where required. |
| 16 | `arith-expand` | Expand arithmetic operations unsupported by later conversion. |
| 17 | `convert-memref-to-llvm` | Lower memref types and operations to LLVM-compatible forms. |
| 18 | `synthesize-debug-info-scopes` | Create final debug-info scopes for line tables. |
| 19 | `convert-target-to-nvvm` | Attach NVVM target metadata and libNVVM options. |
| 20 | `canonicalize` and `cse` | Clean the post-NVVM IR. |
| 21 | `tileir-post-nvvm-finalize` | Make the module ready for LLVM/NVPTX serialization. |

## Warp-Specialized Adders

Warp-specialized scheduling is layered on top of the base tier when
`pipeline-strategy=warp-specialize`.

| Variant | Trigger | Purpose |
| --- | --- | --- |
| Light | `rrt-size-threshold=0` | Insert boundaries, run light warp-specialization rewrites, and add barriers. |
| Heavy | `rrt-size-threshold` nonzero | Prepare scheduling, specialize agents, check register budgets, and compact layouts. |

The light variant is useful when schedule resource tables would dominate compile time. The heavy
variant is the normal path for kernels where modulo scheduling, register-pressure checks, and layout
canonicalization determine final quality.

## Handoff to LLVM/NVPTX

The pass list above ends at the MLIR-to-LLVM/NVVM boundary. After that, the backend runs LLVM IR and
MachineIR passes such as NVVM reflection, address-space optimization, argument lowering, aggregate-copy
lowering, image-handle replacement, and NVPTX instruction cleanup.

## Reimplementation Checklist

1. Keep pass order stable within each tier.
2. Treat O2 as the normal default.
3. Keep verifier coverage in every tier, including O0.
4. Run TileIR-specific verifiers before LLVM conversion erases high-level structure.
5. Bracket major conversions with canonicalization and CSE.
6. Add warp-specialized passes only when the strategy requests them.
7. Document whether a pass runs at module, `gpu.module`, or function scope.
8. Keep LLVM/NVPTX backend passes out of the MLIR-tier pass list.
