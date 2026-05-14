# Pass List by Optimization Level

## Abstract

Each optimization level in Tileiras selects a different MLIR-tier pass pipeline. The four levels - `O0`, `O1`, `O2`, `O3` - are arranged as a strict superset chain: each level runs everything the previous level ran and then adds passes that justify their compile-time cost. This page lists the passes that each level schedules, explains what the additions buy, and describes the IR shape at each stage boundary. LLVM IR and MachineIR passes that run after the MLIR pipeline are documented under [NVPTX Backend Passes](../nvptx-passes/overview.md).

The reader's working question is "if I build with `-O2`, what runs, in what order, and what does each pass do?" The tables answer the first two parts; the prose around them answers the third.

## Stage Vocabulary

The MLIR pipeline can be read as four stages regardless of opt level:

1. **Frontend cleanup.** Convert the public `cuda_tile` surface into the alias-aware TileAA form, insert debug scopes, fold trivial operations. The IR after this stage is in TileAA with `cuda_tile` removed.
2. **Architecture-aware lowering.** Lower TileAA into the operationally-scheduled TileAS dialect; emit host-wrapper metadata; bring in NVGPU-compatible forms. The IR after this stage carries explicit pipes, mutexes, and TMA-ready memory.
3. **Standard lowering.** Convert vector, memref, math, and arithmetic dialects toward LLVM; legalize kernel ABIs; canonicalize and CSE. The IR after this stage is in the LLVM and NVGPU dialects.
4. **Target finalization.** Convert NVGPU to NVVM; attach target metadata; synthesize debug-info scopes; clean and finalize for the NVPTX backend. The IR after this stage is ready for MLIR-to-LLVM translation.

`O0` collapses stages 2-4 into a verifier-only path. `O1` exits after stage 1. `O2` exits midway through stage 2. `O3` runs all four stages.

## O0 - Verify Only

`O0` is the validation-only path. No transformation passes run; the pass manager schedules its built-in verifier between every parsed module load and the codegen handoff.

| Order | Pass | Purpose |
| --- | --- | --- |
| 1 | Verifier slots | Check IR validity at pass boundaries. |

The IR shape at `O0` is whatever the bytecode reader produced: `cuda_tile` operations, intact, with no lowering applied. `O0` is appropriate when the user wants to round-trip bytecode through the front end without touching it - for example, to confirm that a TileIR producer's output is well-formed. `O0` is not a valid input to the NVPTX backend; downstream codegen is unreachable from this level because no LLVM dialect ever appears.

## O1 - Minimal Lowering

`O1` performs the minimum useful TileIR lowering. It clears the public surface and produces a TileAA module that is well-formed for inspection but not yet lowered to anything LLVM understands.

| Order | Pass | Purpose |
| --- | --- | --- |
| 1 | `convert-cudatile-to-tileaa` | Translate the public `cuda_tile` surface into TileAA. |
| 2 | Optional snapshot printer | Emit a textual IR snapshot when the selected line-info mode requests it. |
| 3 | `tileir-insert-debug-scope` | Add debug scopes used by later diagnostics and line-info emission. |
| 4 | `canonicalize` | Clean simple folds and canonical forms before deeper lowering. |

What `O0` -> `O1` adds: a single semantic hop (`cuda_tile` -> TileAA), debug-scope annotations, and the cheap canonicalisation pass. The hop is required because every later stage assumes that `cuda_tile` operations have been replaced; without it the rest of the pipeline cannot run. Debug-scope insertion is placed early because later passes rely on its scope tree being present, and because synthesising scopes after lowering would require chasing through rewritten operations to find the original locations. Canonicalisation runs last so that the patterns operate on freshly-lowered TileAA and remove the trivial garbage that direct dialect conversion can leave behind.

Invariant: after `O1`, no `cuda_tile` operation remains in the module. Cost: a single dialect-conversion pass plus a cheap fold-and-clean. Debuggability: preserved end-to-end; the snapshot printer is the explicit hook for that.

## O2 - Default Pipeline

`O2` is the default compilation pipeline. It is the lowest level at which Tileiras produces a module that the NVPTX backend can lower, because it brings the IR into the LLVM and NVGPU dialects.

| Order | Pass | Purpose |
| --- | --- | --- |
| 1 | O1 passes | Establish TileAA and clean the frontend IR. |
| 2 | `convert-tileaa-to-tileas` | Lower architecture-aware TileAA operations to scheduled TileAS forms (see [Modulo Scheduler and Rau-Style Placement](../scheduler/modulo-scheduler-and-rau.md)). |
| 3 | `tileir-emit-host-wrapper` | Build host-side wrapper metadata and launch glue. |
| 4 | `convert-tileas-to-llvm` | Lower TileAS memory, control, and async constructs toward LLVM. |
| 5 | `cse` | Remove redundant values produced by lowering. |
| 6 | Optional snapshot printer | Capture the TileAS/LLVM boundary when the later line-info mode requests it. |
| 7 | `convert-tileas-to-nvgpu` | Lower remaining target GPU operations to NVGPU-compatible forms. |

What `O1` -> `O2` adds: three lowering hops (TileAA -> TileAS, TileAS -> LLVM, TileAS -> NVGPU), host-wrapper emission, and one CSE pass. The TileAA-to-TileAS hop is where the modulo scheduler runs: it builds [resource constraints](../scheduler/resource-constraint-builder-and-rrt.md), computes the [placement](../scheduler/modulo-scheduler-and-rau.md#placement-arms), and stores the result as a `ScheduleAnalysis`. The TileAS-to-LLVM hop materialises pipes and mutexes against that schedule, lowers memory operations to LLVM-dialect ones, and converts async constructs to their LLVM-dialect equivalents. The TileAS-to-NVGPU hop catches the architecture-specific operations (asynchronous copies, TMA descriptors, named barriers) that need NVGPU-dialect shapes before NVVM lowering. Host-wrapper emission produces the launch-side glue the host runtime expects. CSE runs once after the heaviest lowering because lowering patterns frequently produce duplicate index or offset computations.

The order is meaningful: TileAA-to-TileAS must precede every other hop in the stage because everything downstream assumes the schedule already exists. Host-wrapper emission has to land before the TileAS-to-LLVM conversion erases TileAS launch operations. The optional snapshot lands between TileAS-to-LLVM and TileAS-to-NVGPU so users can inspect the intermediate state with both LLVM-style and NVGPU-style operations visible.

Invariants: after `O2`, no TileAA or TileAS operation remains; the module is in the LLVM and NVGPU dialects with the scheduler's decisions baked into pipe and mutex values. Cost: the scheduler is the dominant pass; CSE is cheap. Debuggability: still preserved; the snapshot point is the natural inspection window for users diagnosing lowering bugs.

## O3 - Full Pipeline

`O3` adds the full conversion and finalisation stack. It is the level the production driver uses by default for non-debug builds and the only level that exercises every NVVM target attachment.

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

What `O2` -> `O3` adds: invariant verification, the full standard-dialect-to-LLVM conversion stack, the NVGPU-to-NVVM and target-NVVM hops, the kernel-ABI legalisation pair, debug-info-scope synthesis, and a final cleanup pair. The block from `tileir-verify-ops-analysis` through `tileir-gpu-module-prepare` exists to make late lowering safe: invariants are checked while TileIR-specific operations are still present, asserts are wired so device-side `assert` calls survive lowering, and the `gpu.module` is reshaped to the form the standard MLIR lowering machinery expects. The `convert-to-llvm` block (passes 11 through 17) covers the standard MLIR dialects - vector, memref, math, arith - which `O2` does not touch because the default pipeline does not need them; `O3` includes them to handle the full surface a TileIR producer can generate. The legalize/finalize kernel-ABI pair is what makes the kernel entry function look like a CUDA kernel rather than a generic LLVM function: argument layout, address-space attributes, calling convention, and `nvvm.kernel` metadata all land here. `synthesize-debug-info-scopes` produces line-table-quality debug info (`LineTablesOnly` in the configured mode) that the backend can lift directly into PTX `.loc` directives. `convert-target-to-nvvm` attaches the libNVVM option blob and the target-triple metadata the NVPTX backend reads to choose its subtarget. The closing canonicalise/CSE pair and `tileir-post-nvvm-finalize` ensure the module is in the exact shape the LLVM/NVPTX translator expects.

Order matters in this stage too. `tileir-legalize-llvm-kernel` must precede `convert-to-llvm` because the broad conversion erases the very TileIR markers the legaliser depends on. `convert-vector-to-llvm` must precede `convert-memref-to-llvm` because vector lowering can introduce memref accesses but memref lowering does not introduce vector forms. `arith-expand` runs before `convert-memref-to-llvm` because some memref index expressions become arith operations that the lowering then expects to find in expanded form. `convert-target-to-nvvm` is the final lowering step because it is what binds the module to a specific `sm_*` and to specific libNVVM options - anything that runs after it would have to be target-aware.

Invariants: after `O3`, the module contains only LLVM and NVVM dialect operations, the kernel ABI is in NVPTX form, line-table debug info is present, and target metadata is attached. The TileIR verifier has confirmed pre-lowering invariants. Cost: the dominant passes are the modulo scheduler (inherited from `O2`) and the broad LLVM conversion. Debuggability: degraded relative to `O2` because the kernel ABI has been rewritten and most TileIR operations are gone; the early snapshot printer and the `tileir-verify-ops-analysis` pass are the standard inspection points.

## Warp-Specialised Adders

Warp-specialised scheduling is layered on top of the base tier when `pipeline-strategy=warp-specialize`. The adder replaces the modulo-schedule stage with a warp-specialisation pipeline that partitions the loop body across agents.

| Variant | Trigger | Purpose |
| --- | --- | --- |
| Light | `rrt-size-threshold=0` | Insert boundaries, run light warp-specialization rewrites, and add barriers. |
| Heavy | `rrt-size-threshold` nonzero | Prepare scheduling, specialize agents, check register budgets, and compact layouts. |

The light variant is used when the resource reservation table would dominate compile time; it produces a correct but conservative schedule. The heavy variant is the normal path for kernels where modulo scheduling, register-pressure checks, and layout canonicalisation determine final quality. Both variants slot into stage 2 (architecture-aware lowering); the choice is independent of opt level above `O1`.

## Handoff to LLVM/NVPTX

The pass list above ends at the MLIR-to-LLVM/NVVM boundary. After that, the backend runs LLVM IR and MachineIR passes such as NVVM reflection, address-space optimisation, argument lowering, aggregate-copy lowering, image-handle replacement, and NVPTX instruction cleanup. The LLVM-tier pipeline is documented under [NVPTX Backend Passes](../nvptx-passes/overview.md), which describes each pass at the same level of detail as the entries above.

## Cross-References

[Driver Entry and Optimization Levels](driver-and-opt-levels.md) describes how the requested tier turns into the segments listed above. [Pipeline Options Mapping](options-mapping.md) maps each option a user can set to the consuming pass in this list. [Pipeline Invariants and Verifiers](invariants-and-verifiers.md) covers the verifier passes interleaved between the lowerings.

