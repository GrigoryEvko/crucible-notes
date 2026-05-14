# Reading Map

This page is a public reading map for the wiki. It replaces the internal evidence index with a reader-oriented guide: which pages to read for integration, bytecode production, dialect reimplementation, scheduling, lowering, backend behavior, and boundary questions.

## For Tool Users

Start here if you need to run tileiras or diagnose a driver failure:

| Topic | Pages |
| --- | --- |
| Driver behavior | [Driver Overview](driver/overview.md), [Main Entry](driver/main-entry.md), [CLI Options](driver/cli-options.md) |
| Program lifetime | [Program Handle](driver/program-handle.md), [Subprocess Harness](driver/subprocess-harness.md) |
| External tools | [Host Launch and ptxas Knobs](driver/host-launch-and-ptxas-knobs.md), [ptxas Handoff Protocol](boundaries/ptxas-handoff-protocol.md) |
| Environment controls | [Env Vars and Runtime Gates](driver/env-vars-and-runtime-gates.md) |
| CUDA toolchain position | [Position in nvcc 13.1](boundaries/nvcc-13-1-position.md), [cicc Comparison](boundaries/cicc-comparison.md) |

## For Bytecode Producers

Read these pages if you need to produce accepted input:

| Topic | Pages |
| --- | --- |
| Container format | [MLIR Bytecode Format](bytecode/mlir-bc-format.md) |
| Reader/writer coverage | [Dialect Reader/Writer Status](bytecode/dialect-readers-status.md), [AsmPrinter Status](bytecode/asm-printer-status.md) |
| Public input dialect | [cuda_tile Overview](dialects/cuda_tile/overview.md), [cuda_tile Types and Attrs](dialects/cuda_tile/types-and-attrs.md), [cuda_tile Verifiers](dialects/cuda_tile/verifiers.md) |
| Public operation surface | [cuda_tile Op Roster](dialects/cuda_tile/op-roster.md) |

## For Dialect Reimplementation

Read the dialects in pipeline order. Each layer exists to make a different part of the program explicit:

| Layer | What it contributes |
| --- | --- |
| [cuda_tile](dialects/cuda_tile/overview.md) | Public tile-compute input surface. |
| [nv_tileaa](dialects/nv_tileaa/overview.md) | Alias-aware memory, token, pointer, and queue layer. |
| [nv_tileas](dialects/nv_tileas/overview.md) | Operational async scheduling, pipeline regions, layouts, and TMA-ready memory operations. |
| [cute](dialects/cute/overview.md) | Target-neutral layout algebra and tile decomposition. |
| [cute_nvgpu](dialects/cute_nvgpu/overview.md) | NVIDIA architecture atoms for MMA, TMA, copy, and tensor memory. |
| [cutlass](dialects/cutlass/overview.md) | CUTLASS pipeline, tile scheduler, sequence barrier, and block-striped abstractions. |
| [nvgpu](dialects/nvgpu/overview.md) | Stock MLIR GPU bridge before NVVM. |
| [NVVM](dialects/nvvm/overview.md) | PTX-facing intrinsic dialect before LLVM IR. |

## For Scheduling and TileAS Passes

The TileAS pages explain how abstract async and layout intent becomes a scheduled kernel:

| Topic | Pages |
| --- | --- |
| Async pipeline materialization | [Async/Pipeline Family](passes/tileas/async-pipeline-family.md), [nv_tileas Overview](dialects/nv_tileas/overview.md) |
| Layout and buffers | [Layout and Buffer Family](passes/tileas/layout-and-buffer-family.md) |
| TMA and memory operations | [TMA and Memops Family](passes/tileas/tma-and-memops-family.md) |
| CTA and cluster planning | [CTA Cluster Family](passes/tileas/cta-cluster-family.md) |
| Scheduler architecture | [Scheduler Overview](scheduler/overview.md) |
| Modulo scheduling | [Modulo Scheduler and Rau](scheduler/modulo-scheduler-and-rau.md), [Resource Constraint Builder and RRT](scheduler/resource-constraint-builder-and-rrt.md) |
| Pipe materialization | [Schedule::solve and Cost Evaluators](scheduler/schedule-solve-and-cost-evaluators.md) |

## For Lowering and Code Generation

These pages cover the transition from MLIR dialects to LLVM IR, NVVM intrinsics, PTX, and the object file:

| Topic | Pages |
| --- | --- |
| Pipeline shape | [Pipeline Overview](pipeline/overview.md), [Lowering Overview](lowering/overview.md), [Codegen Overview](codegen/overview.md) |
| Dialect conversions | [cuda_tile to tileaa](lowering/cuda-tile-to-tileaa.md), [tileaa to tileas](lowering/tileaa-to-tileas.md), [tileas to LLVM](lowering/tileas-to-llvm.md) |
| GPU/NVVM conversion | [cute and cute_nvgpu to LLVM](lowering/cute-and-cute_nvgpu-to-llvm.md), [nvgpu and gpu to NVVM](lowering/nvgpu-and-gpu-to-nvvm.md) |
| NVPTX backend | [NVPTX Backend Passes](nvptx-passes/overview.md), [NVPTX Target Lowering, Call and Args](codegen/nvptx-target-lowering-call-and-args.md) |
| libdevice | [libdevice Overview](libdevice/overview.md), [NVVMReflect Mechanism](libdevice/nvvm-reflect-mechanism.md) |

## For Infrastructure

Use these pages when a dialect page refers to MLIR object mechanics:

| Topic | Pages |
| --- | --- |
| Operation model | [Operation Layout](mlir-infra/operation-layout.md) |
| Type and attribute uniquing | [StorageUniquer and Context Impl](mlir-infra/storage-uniquer-and-context-impl.md) |
| Pattern rewriting | [Pattern Vtables and Shapes](mlir-infra/pattern-vtables-and-shapes.md) |
| Interfaces and TypeIDs | [Interface Vtables](mlir-infra/interface-vtables.md), [TypeID Sentinels and Anchors](mlir-infra/typeid-sentinels-and-anchors.md) |
| Diagnostics | [Diagnostic ABI and Helpers](mlir-infra/diagnostic-abi-and-helpers.md) |
| Scheduler async values | [AsyncValue and BLAKE3 Interning](mlir-infra/asyncvalue-and-blake3-interning.md) |

## Suggested End-to-End Path

For a full reimplementation pass, read:

```text
index
  -> bytecode/mlir-bc-format
  -> dialects/cuda_tile/overview
  -> dialects/nv_tileaa/overview
  -> dialects/nv_tileas/overview
  -> scheduler/overview
  -> lowering/overview
  -> codegen/overview
  -> nvptx-passes/overview
  -> libdevice/overview
```

Then return to the detailed operation, verifier, and pass-family pages for the subsystem you are implementing.
