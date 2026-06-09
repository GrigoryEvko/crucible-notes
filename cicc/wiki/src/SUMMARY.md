# Summary

[CICC v13.0 — Reverse Engineering Reference](./index.md)

---

# Overview

- [Function Map](./function-map.md)
- [Binary Layout](./binary-layout.md)
- [Methodology](./methodology.md)
- [Version Tracking](./VERSIONS.md)

# Compilation Pipeline

- [Pipeline Overview](./pipeline/overview.md)
- [Entry Point & CLI](./pipeline/entry.md)
- [nvcc-to-cicc Interface](./pipeline/nvcc-interface.md)
- [EDG 6.6 Frontend](./pipeline/edg.md)
- [NVVM IR Generation](./pipeline/ir-generation.md)
  - [Expression & Constant Codegen](./pipeline/irgen-expressions.md)
  - [Statement & Control Flow Codegen](./pipeline/irgen-statements.md)
  - [Function, Call & Inline Asm Codegen](./pipeline/irgen-functions.md)
  - [Type Translation, Globals & Special Vars](./pipeline/irgen-types.md)
- [Pragma & NVVM Annotation Registry](./pipeline/pragma-annotation-registry.md)
- [Libdevice Linking](./infra/libdevice-linking.md)
- [LLVM Optimizer](./pipeline/optimizer.md)
- [OptiX IR Generation](./pipeline/optix-ir.md)
- [Code Generation](./pipeline/codegen.md)
- [PTX Emission](./pipeline/emission.md)
- [Debug Info Pipeline](./pipeline/debug-info-pipeline.md)

# NVIDIA Custom Passes

- [Overview & Inventory](./passes/index.md)
- [NVVM Reflect](./passes/nvvm-reflect.md)
- [NVVM IR Verifier](./passes/nvvm-verify-deep.md)
- [NVVM Intrinsic Lowering](./passes/nvvm-intrinsic-lowering.md)
- [FP128/I128 Emulation](./passes/fp128-emulation.md)
- [Struct/Aggregate Splitting](./passes/struct-splitting.md)
- [Memmove Unrolling](./passes/memmove-unroll.md)
- [Printf Lowering](./passes/printf-lowering.md)
- [IP Memory Space Propagation](./passes/ipmsp.md)
- [MemorySpaceOpt](./passes/memory-space-opt.md)
- [NVVM Peephole](./passes/nvvm-peephole.md)
- [Sinking2](./passes/sinking2.md)
- [Loop Index Split](./passes/loop-index-split.md)
- [Branch Distribution](./passes/branch-distribution.md)
- [Dead Barrier Elimination](./passes/dead-barrier-elim.md)
- [Dead Synchronization Elimination](./passes/dead-sync-elimination.md)
- [Rematerialization](./passes/rematerialization.md)
  - [IV Demotion](./passes/iv-demotion.md)
- [Base Address Strength Reduction](./passes/base-address-sr.md)
- [Common Base Elimination](./passes/common-base-elim.md)
- [CSSA (Conventional SSA)](./passes/cssa.md)
- [Minor NVIDIA Passes](./passes/other.md)

# LLVM Passes

- [Pipeline & Ordering](./llvm/pipeline.md)
- [Patch Catalog](./llvm/patch-catalog.md)

## Scalar

- [SROA, EarlyCSE & JumpThreading](./llvm/scalar-passes.md)
- [SROA Deep Dive](./llvm/sroa.md)
- [EarlyCSE Deep Dive](./llvm/early-cse.md)
- [InstCombine](./llvm/instcombine.md)
- [GVN](./llvm/gvn.md)
- [JumpThreading Deep Dive](./llvm/jump-threading.md)
- [LICM](./llvm/licm-real.md)
- [DSE](./llvm/dse.md)
- [Constant Folding: Math & Intrinsics](./llvm/constant-folding.md)
- [KnownBits & DemandedBits](./llvm/known-bits.md)
- [CodeGenPrepare & SCEV-CGP](./llvm/codegen-prepare.md)

## Loop

- [ScalarEvolution Overview & Construction](./llvm/scev.md)
- [SCEV Range Analysis & Backedge-Taken Counts](./llvm/scev-range-btc.md)
- [SCEV Invalidation & Delinearization](./llvm/scev-invalidation.md)
- [Loop Optimization Passes](./llvm/loop-passes.md)
- [Standard Loop Passes](./llvm/loop-passes-standard.md)
- [Loop Unrolling](./llvm/loop-unroll.md)
- [LoopVectorize & VPlan](./llvm/loop-vectorize.md)
- [SLP Vectorizer](./llvm/slp-vectorizer.md)
- [Loop Strength Reduction (NVIDIA)](./llvm/lsr.md)
- [StructurizeCFG](./llvm/structurizecfg.md)

## Machine-Level

- [Machine-Level Passes](./llvm/machine-passes.md)
- [SelectionDAG](./llvm/selectiondag.md)
- [NVPTXISD Opcodes](./llvm/nvptxisd-opcodes.md)
- [Type Legalization](./llvm/type-legalization.md)
- [ISel Pattern Matching](./llvm/isel-patterns.md)
- [InstrEmitter](./llvm/instr-emitter.md)
- [TwoAddressInstruction](./llvm/two-address.md)
- [Instruction Scheduling](./llvm/scheduling.md)
- [LiveRangeCalc](./llvm/live-range-calc.md)
- [Register Coalescing](./llvm/register-coalescing.md)
- [Register Allocation](./llvm/register-allocation.md)
- [PrologEpilogInserter & Frame Layout](./llvm/prolog-epilog.md)
- [BranchFolding & TailMerge](./llvm/branch-folding.md)
- [Block Placement](./llvm/block-placement.md)
- [MachineOutliner](./llvm/machine-outliner.md)
- [Tensor / MMA Codegen](./llvm/mma-codegen.md)

# NVVM Builtins

- [Builtin Table Structure](./builtins/index.md)
- [Atomics](./builtins/atomics.md)
- [Math Functions](./builtins/math.md)
- [Tensor / MMA](./builtins/tensor-mma.md)
- [Surface & Texture](./builtins/surface-texture.md)
- [Barriers & Sync](./builtins/barriers.md)
- [Warp Operations](./builtins/warp.md)

# GPU Targets

- [Feature Gate Matrix](./targets/index.md)
- [SM 70-89 (Volta -> Ada)](./targets/sm70-89.md)
- [SM 90 — Hopper](./targets/sm90-hopper.md)
- [SM 100 — Blackwell](./targets/sm100-blackwell.md)
- [SM 120 — Consumer Blackwell / RTX 50](./targets/sm120.md)

# Data Structures

- [IR Node Layout](./structs/ir-node.md)
- [Pattern Database](./structs/pattern-db.md)
- [DAG Node](./structs/dag-node.md)
- [Symbol Table](./structs/symbol-table.md)
- [NVVM Container](./structs/nvvm-container.md)

# Infrastructure & Analysis

- [NVPTX Target Infrastructure](./infra/nvptx-target.md)
- [Alias Analysis & NVVM AA](./infra/alias-analysis.md)
- [MemorySSA Builder for GPU](./infra/memoryssa.md)
- [LazyCallGraph & CGSCC](./infra/lazycallgraph.md)
- [AsmPrinter & PTX Body Emission](./infra/asmprinter.md)
- [Debug Info Verification](./infra/debug-verify.md)
- [Bitcode Reader/Writer](./infra/bitcode-io.md)
- [Concurrent Compilation](./infra/concurrent-compilation.md)
- [Diagnostics & Optimization Remarks](./infra/diagnostics.md)
- [EDG Diagnostic Identifier Catalogue](./infra/edg-diagnostic-catalog.md)
- [Hash Table Infrastructure](./infra/hash-infrastructure.md)
- [jemalloc Allocator](./infra/jemalloc.md)

# Coroutines

- [CoroSplit & CoroFrame](./coroutines/coro-split.md)

# OpenMP Support

- [Runtime Declaration Table](./openmp/runtime-table.md)
- [Generic-to-SPMD Transformation](./openmp/spmd-transform.md)

# LTO & Module Optimization

- [LTO & Module Optimization](./lto/index.md)
- [NVModuleSummary Builder](./lto/module-summary.md)
- [Inliner Cost Model](./lto/inliner-cost.md)
- [ThinLTO Function Import](./lto/thinlto-import.md)
- [GlobalOpt for GPU](./lto/globalopt.md)
- [Whole-Program Devirtualization](./lto/devirtualization.md)

# Reference

- [GPU Execution Model](./gpu-execution-model.md)
- [Address Spaces](./reference/address-spaces.md)
- [Register Classes](./reference/register-classes.md)
- [NVPTX Machine Opcodes](./reference/nvptx-opcodes.md)
- [Encrypted Data Tables](./reference/encrypted-data-tables.md)

# Configuration

- [CLI Flags](./config/cli-flags.md)
- [Optimization Levels](./config/optimization-levels.md)
- [NVVMPassOptions (221 Slots)](./config/nvvm-pass-options.md)
- [LLVM Knobs (1,689)](./config/knobs.md)
- [Environment Variables](./config/env-vars.md)
- [EDG Build-Time #define Surface (748)](./config/build-defines.md)
