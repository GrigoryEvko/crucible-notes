# Subsystem Map

This page maps the wiki's subsystems to the compiler responsibilities they implement. It replaces the internal function-address table with a public engineering map: where to look for a behavior, which pages describe it, and which contracts a reimplementation must preserve.

## Driver

The driver owns process-level behavior: command-line parsing, target validation, CUDA installation discovery, tool invocation, output naming, and error reporting. Start with [Driver Overview](driver/overview.md), [CLI Options](driver/cli-options.md), and [Subprocess Harness](driver/subprocess-harness.md).

Public contract:

- reject unsupported GPU targets and malformed host options,
- preserve the documented defaults for output path, optimization, debug, and line-info flags,
- invoke `ptxas` with the target and feature options implied by the compile configuration,
- surface subprocess failures with actionable diagnostics.

## Bytecode Reader

The bytecode reader owns the on-disk TileIR format. It validates the container header, reads sections, reconstructs strings, types, attributes, locations, constants, functions, globals, and operations, then verifies the resulting MLIR module. Start with [MLIR Bytecode Format](bytecode/mlir-bc-format.md).

Public contract:

- accept the TileIR bytecode magic and supported version fields,
- reject malformed varints, invalid string references, and unknown required tags,
- reconstruct `cuda_tile` operations with correct regions, operands, attributes, and result types,
- emit useful parse errors that identify bytecode format failures.

## Dialect Stack

The dialect stack is the core compiler model. Each dialect makes a different semantic layer explicit:

| Dialect | Responsibility |
| --- | --- |
| `cuda_tile` | Public tile input dialect. |
| `nv_tileaa` | Alias-aware memory, token, queue, and pointer layer. |
| `nv_tileas` | Operational async scheduling and TMA-ready memory layer. |
| `cute` | Target-neutral layout algebra. |
| `cute_nvgpu` | NVIDIA architecture atom registry. |
| `cutlass` | CUTLASS pipeline and tile-scheduler abstractions. |
| `nvgpu` | Stock MLIR GPU bridge layer. |
| `nvvm` | PTX-facing intrinsic dialect. |

The overview page for each dialect describes the public contract; child pages describe operations, types, verifiers, folds, and lowering behavior.

## Pass Pipeline

The pass pipeline owns semantic transformation. It starts from `cuda_tile`, moves through TileAA and TileAS, materializes layouts and async pipelines, schedules operations, lowers target atoms, converts to LLVM/NVVM, and hands an LLVM module to the backend. Start with [Pipeline Overview](pipeline/overview.md) and [Lowering Overview](lowering/overview.md).

Public contract:

- run dialect conversion in the documented direction,
- preserve memory-token ordering and region semantics,
- run verifier-sensitive passes before their assumptions are used,
- keep target selection and compute capability visible to target-specific lowerings,
- keep TileAS schedule generation separate from schedule materialization.

## Scheduler

The scheduler owns stage and order assignment. It builds resource constraints, uses an RRT-based modulo scheduler to choose a feasible placement, preserves the result in `ScheduleAnalysis`, and later materializes `Pipe_` and `Mutex_` values. Start with [Scheduler Overview](scheduler/overview.md).

Public contract:

- compute a deterministic `(stage, order)` relation for scheduled operations,
- keep dependence legality and resource legality separate,
- bound constraint refinement with `max-constraint-iterations`,
- use `Schedule::solve` only after placement is fixed,
- emit producer/consumer coordination values that match the chosen schedule.

## Lowering and Code Generation

Lowering and code generation own the boundary to LLVM and NVPTX. They convert MLIR dialects to LLVM/NVVM, build the target module, link libdevice, run backend passes, emit PTX, and assemble through `ptxas`. Start with [Codegen Overview](codegen/overview.md), [NVPTX Backend Passes](nvptx-passes/overview.md), and [libdevice Overview](libdevice/overview.md).

Public contract:

- lower kernel arguments according to the NVPTX ABI,
- preserve address-space and memory-scope semantics,
- lower target atoms to supported NVVM intrinsics or inline PTX,
- verify target-specific operations before PTX emission,
- guarantee no unresolved libdevice or pseudo operations reach final assembly.

## Infrastructure

Infrastructure pages explain the shared MLIR runtime model: operations, storage uniquing, interfaces, TypeIDs, rewrite patterns, diagnostics, containers, and async values. Start with [MLIR Infrastructure Overview](mlir-infra/overview.md).

Public contract:

- operations have stable operands, results, attributes, regions, and parent links,
- types and attributes are immutable uniqued values,
- interfaces expose semantic capabilities rather than concrete implementation names,
- pattern rewrites preserve side effects and region invariants,
- diagnostics explain operation-level failures clearly.

## How to Use This Map

If a page describes an operation mnemonic, start in the dialect section. If it describes a transformation, start in the pass or lowering section. If it describes scheduling, start in the scheduler section. If it describes PTX ABI, libdevice, or target verification, start in code generation or NVPTX backend pages.
