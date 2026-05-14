# Reading Map

This page is curated reader paths. Each path is an ordered sequence of pages with a one-sentence rationale for why the next page follows. Use these when you want to answer "I want to understand X — what do I read in what order?" instead of browsing the SUMMARY.

## Driver and Integration Path

For running tileiras, embedding it, or diagnosing a driver failure:

1. [Driver Overview](driver/overview.md) — what the binary does and which public entry points exist.
2. [Main Entry](driver/main-entry.md) — how `main()` builds the configuration and dispatches the four phases.
3. [Program Handle](driver/program-handle.md) — the 104-byte handle threaded through create / compile / get-output / release.
4. [CLI Options](driver/cli-options.md) — the option surface, separating user-facing flags from internal `cl::opt` plumbing.
5. [Env Vars and Runtime Gates](driver/env-vars-and-runtime-gates.md) — environment-driven knobs that bypass the CLI.
6. [Host Launch and ptxas Knobs](driver/host-launch-and-ptxas-knobs.md) — how the driver shells out to `ptxas`.
7. [ptxas Handoff Protocol](boundaries/ptxas-handoff-protocol.md) — the exact PTX surface `ptxas` accepts.
8. [Position in nvcc 13.1](boundaries/nvcc-13-1-position.md) — where tileiras fits in the larger CUDA toolchain.

## Bytecode Producer Path

For producing valid TileIR bytecode that tileiras will accept:

1. [MLIR Bytecode Format](bytecode/mlir-bc-format.md) — the container grammar and section layout.
2. [Dialect Reader/Writer Status](bytecode/dialect-readers-status.md) — which dialects have custom bytecode readers and what coverage looks like.
3. [AsmPrinter Status](bytecode/asm-printer-status.md) — printer-side companion (the textual round-trip is partial).
4. [cuda_tile Overview](dialects/cuda_tile/overview.md) — the public input dialect.
5. [cuda_tile Op Roster](dialects/cuda_tile/op-roster.md) — every op the public surface accepts.
6. [cuda_tile Types and Attrs](dialects/cuda_tile/types-and-attrs.md) — types and attributes those ops use.
7. [cuda_tile Verifiers](dialects/cuda_tile/verifiers.md) — what gets checked at parse time.
8. [TypeID Sentinel Table](reference/typeid-sentinel-table.md) — lookup table when you need the exact identity of a sentinel.

## Dialect Lowering Chain

For understanding how the IR cascades from public input to LLVM:

1. [cuda_tile](dialects/cuda_tile/overview.md) — public tile-compute surface.
2. [cuda_tile to tileaa](lowering/cuda-tile-to-tileaa.md) — first conversion: introduce alias awareness.
3. [nv_tileaa](dialects/nv_tileaa/overview.md) — alias-aware memory, tokens, queues.
4. [tileaa to tileas](lowering/tileaa-to-tileas.md) — second conversion: make scheduling explicit.
5. [nv_tileas](dialects/nv_tileas/overview.md) — operational async-scheduling dialect.
6. [cute](dialects/cute/overview.md) — target-neutral layout algebra.
7. [cute_nvgpu](dialects/cute_nvgpu/overview.md) — NVIDIA architecture atoms (MMA, TMA, tcgen05).
8. [cutlass](dialects/cutlass/overview.md) — pipeline scheduler, sequence barriers, persistent kernels.
9. [tileas to LLVM](lowering/tileas-to-llvm.md) — final MLIR-side conversion.
10. [cute and cute_nvgpu to LLVM](lowering/cute-and-cute_nvgpu-to-llvm.md) — atom lowering to LLVM intrinsics.
11. [nvgpu and gpu to NVVM](lowering/nvgpu-and-gpu-to-nvvm.md) — bridge to PTX-facing dialect.
12. [Lowering Overview](lowering/overview.md) — top-down summary tying these conversions together.

## Scheduler Deep-Dive

For understanding how TileAS turns dependence graphs into placed schedules:

1. [Scheduler Overview](scheduler/overview.md) — the two-pass GenerateSchedule / MaterializeSchedule split.
2. [Schedule Constraint Attributes](scheduler/schedule-constraint-attributes.md) — the nine `tileas.schedule.constraint.*` attributes that drive placement.
3. [Resource Constraint Builder and RRT](scheduler/resource-constraint-builder-and-rrt.md) — how per-op footprints become RRT bits.
4. [Modulo Scheduler and Rau](scheduler/modulo-scheduler-and-rau.md) — the modulo-scheduling exemplar (read this one carefully).
5. [Modulo Driver and 4-Arm OR-Chain](scheduler/modulo-driver-or-chain.md) — the four placement arms (PERMUTE / FUSE / RETRY / CBS).
6. [Serial vs Cost-Based Generators](scheduler/serial-vs-cost-based-generators.md) — the two generator implementations and when each fires.
7. [Schedule::solve and Cost Evaluators](scheduler/schedule-solve-and-cost-evaluators.md) — the materialization algorithm.
8. [Pipe and Mutex Value Layout](scheduler/pipe-mutex-value-layout.md) — the IR-visible coordination values.
9. [Buffer Assignment and Named Barriers](scheduler/buffer-assignment-and-mbarriers.md) — the 32-slot named-barrier pool and how Mutex_ values consume it.
10. [Blackwell Pipeline 15-Slot Model](scheduler/blackwell-pipeline-15-slot-model.md) — the target pipeline model the scheduler reasons against.

## TileAS Pass Families

For the per-family pass roster running on `nv_tileas` IR:

1. [Async/Pipeline Family](passes/tileas/async-pipeline-family.md) — MaterializeSchedule, AUS vs AWS, agent materialization.
2. [Layout and Buffer Family](passes/tileas/layout-and-buffer-family.md) — layout assignment, slicing, and shared-memory handoffs.
3. [TMA and Memops Family](passes/tileas/tma-and-memops-family.md) — TMA-descriptor and bulk-copy lowering.
4. [CTA Cluster Family](passes/tileas/cta-cluster-family.md) — cluster geometry, DynamicPersistent, PlanCTA, PrepareForScheduling, ResolveAgentBoundary.
5. [Scheduling Glue](passes/tileas/scheduling-glue.md) — the small passes wiring schedule data into surrounding IR.

## Codegen Deep-Dive

For the NVPTX backend that consumes the lowered LLVM IR:

1. [Codegen Overview](codegen/overview.md) — pipeline shape from LLVM IR to PTX.
2. [NVPTX Bring-up and Target Init](codegen/nvptx-bring-up-and-target-init.md) — how the target gets registered and initialized.
3. [NVPTX Subtarget and Feature Matrix](codegen/nvptx-subtarget-and-feature-matrix.md) — per-SM feature gating.
4. [NVPTX Target Lowering, Call and Args](codegen/nvptx-target-lowering-call-and-args.md) — calling convention, parameter space, byval handling.
5. [ISelDAG and MatcherTable](codegen/iseldag-and-matchertable.md) — DAG-to-DAG instruction selection.
6. [Per-SM Emission Templates](codegen/per-sm-emission-templates.md) — emission templates parameterised by SM tier.
7. [AsmPrinter Monster and Windows](codegen/asm-printer-monster-and-windows.md) — final PTX text emission.
8. [tcgen05, WGMMA, mbarrier, Cluster](codegen/tcgen05-wgmma-mbarrier-cluster.md) — emission of the Blackwell-era instruction families.
9. [TMA, Tensormap and cp.async.bulk](codegen/tma-tensormap-and-cp-async-bulk.md) — TMA-descriptor emission.
10. [ldmatrix, stmatrix and Register Class Vtables](codegen/ldmatrix-stmatrix-and-register-class-vtables.md) — matrix-fragment movement.

## NVPTX Custom Pass Family

For the NVIDIA-private passes layered onto the NVPTX backend:

1. [NVPTX Backend Passes Overview](nvptx-passes/overview.md) — pipeline position and shared state.
2. [Kernel, CDP, Inline, Pretreat](nvptx-passes/kernel-cdp-inline-pretreat.md) — entry-side stamping and inline forcing.
3. [Lower-Args, Aggr, Struct](nvptx-passes/lower-args-and-aggr-and-struct.md) — byval lowering and parameter-space pointer materialization.
4. [MemorySpaceOpt and process-restrict](nvptx-passes/memory-space-opt-and-process-restrict.md) — concrete address-space inference and noalias scope generation.
5. [Printf Lowering and vprintf](nvptx-passes/printf-lowering-and-vprintf.md) — printf-to-vprintf rewrite.
6. [DeadSyncElim and CommonBaseElim](nvptx-passes/dead-sync-elim-and-common-base.md) — barrier removal and SCEV-keyed GEP CSE.
7. [Peephole MIR and Image Handles](nvptx-passes/peephole-mir-and-image-handles.md) — post-ISel MIR rewriting.
8. [NVVMIRVerifier](nvptx-passes/nvvm-ir-verifier.md) — kernel-ABI invariants enforced before backend handoff.

## libdevice and NVVM Reflect

For modules that link against `libdevice` math functions:

1. [libdevice Overview](libdevice/overview.md) — the bitcode library and what it covers.
2. [NVVMReflect Mechanism](libdevice/nvvm-reflect-mechanism.md) — how compile-time reflect calls get resolved.
3. [Intrinsic ID Switch and Name Table](libdevice/intrinsic-id-switch-and-name-table.md) — `__nv_*` name to intrinsic ID mapping.
4. [Math Pass Pipeline and Crosswalk](libdevice/math-pass-pipeline-and-crosswalk.md) — pass ordering around the math expansion.

## MLIR Infrastructure Tour

For the MLIR-side mechanics referenced by dialect and lowering pages:

1. [MLIR Infra Overview](mlir-infra/overview.md) — what the infra layer covers.
2. [Operation Layout](mlir-infra/operation-layout.md) — the 48+ byte `Operation` record and its slots.
3. [StorageUniquer and Context Impl](mlir-infra/storage-uniquer-and-context-impl.md) — type and attribute uniquing.
4. [Pattern Vtables and Shapes](mlir-infra/pattern-vtables-and-shapes.md) — rewrite-pattern shapes and dispatch.
5. [Interface Vtables](mlir-infra/interface-vtables.md) — op and type interface mechanics.
6. [TypeID Sentinels and Anchors](mlir-infra/typeid-sentinels-and-anchors.md) — how TypeIDs are interned and addressed.
7. [Container Fingerprints](mlir-infra/container-fingerprints.md) — recognizing MLIR container shapes in the binary.
8. [Diagnostic ABI and Helpers](mlir-infra/diagnostic-abi-and-helpers.md) — diagnostic emission, severity packing.
9. [AsyncValue and BLAKE3 Interning](mlir-infra/asyncvalue-and-blake3-interning.md) — the 808-byte AsyncValue record backing `Pipe_` / `Mutex_`.

## OSS Comparison Tour

For comparing tileiras against the public `cuda-tile` repository:

1. [OSS Comparison Overview](oss/overview.md) — what the public tree covers vs what tileiras adds.
2. [cuda_tile Tree Mapping](oss/cuda-tile-tree-mapping.md) — file-by-file mapping between public source and tileiras behavior.
3. [.td Files Delta](oss/td-files-delta.md) — TableGen differences.
4. [Transforms, FuseFMA, SynthDbg](oss/transforms-fusefma-synthdbg.md) — public transform passes and where they live in tileiras.

## Cross-cutting Infra

For low-level mechanics referenced from multiple pages:

| Topic | Page |
| --- | --- |
| Data section decryption | [Data Section Decryption](infra/data-section-decryption.md) |
| Vtable banks | [Binary Vtable Banks and Static Ctors](infra/binary-vtable-banks-and-static-ctors.md) |
| Threading | [Threading and Synchronization](infra/threading-and-synchronization.md) |
| Allocators | [Allocator BumpPtr and Slab Sizes](infra/allocator-bumpptr-and-slab-sizes.md) |
| String mechanics | [Twine, StringRef, format](infra/twine-stringref-and-format.md) |
| Diagnostic helpers | [Diagnostic Helpers](infra/diagnostic-helpers.md) |
| GlobalValue flags | [GlobalValue Flag Bits](infra/global-value-flag-bits.md) |

## End-to-End Reimplementation Path

For a single linear read through every contract you must reproduce:

```text
index
  -> binary-layout
  -> boundaries/nvcc-13-1-position
  -> pipeline/overview
  -> bytecode/mlir-bc-format
  -> dialects/cuda_tile/overview
  -> lowering/cuda-tile-to-tileaa
  -> dialects/nv_tileaa/overview
  -> lowering/tileaa-to-tileas
  -> dialects/nv_tileas/overview
  -> passes/tileas/scheduling-glue
  -> scheduler/overview
  -> scheduler/modulo-scheduler-and-rau
  -> lowering/tileas-to-llvm
  -> codegen/overview
  -> nvptx-passes/overview
  -> libdevice/overview
```

Then return to the detailed operation, verifier, and pass-family pages for the subsystem you are implementing.
