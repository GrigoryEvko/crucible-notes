# Summary

[CICC v13.0 — Reverse Engineering Reference](./index.md)

---

# Overview

- [Function Map](./function-map.md)
- [Binary Layout](./binary-layout.md)
- [Methodology](./methodology.md)

# Compilation Pipeline

- [Entry Point & CLI](./pipeline/entry.md)
- [EDG 6.6 Frontend](./pipeline/edg.md)
- [LLVM Optimizer](./pipeline/optimizer.md)
- [Code Generation](./pipeline/codegen.md)
- [PTX Emission](./pipeline/emission.md)

# NVIDIA Custom Passes

- [Overview & Inventory](./passes/index.md)
- [MemorySpaceOpt](./passes/memory-space-opt.md)
- [Rematerialization](./passes/rematerialization.md)
- [Branch Distribution](./passes/branch-distribution.md)
- [Sinking2](./passes/sinking2.md)
- [Loop Index Split](./passes/loop-index-split.md)
- [NVVM Peephole](./passes/nvvm-peephole.md)
- [Dead Barrier Elimination](./passes/dead-barrier-elim.md)
- [Printf Lowering](./passes/printf-lowering.md)
- [IP Memory Space Propagation](./passes/ipmsp.md)
- [Other NVIDIA Passes](./passes/other.md)

# LLVM Passes

- [Pipeline & Ordering](./llvm/pipeline.md)
- [InstCombine](./llvm/instcombine.md)
- [GVN](./llvm/gvn.md)
- [DSE](./llvm/dse.md)
- [LICM](./llvm/licm.md)
- [SelectionDAG](./llvm/selectiondag.md)
- [Register Allocation](./llvm/register-allocation.md)
- [Instruction Scheduling](./llvm/scheduling.md)

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
- [SM 70–89 (Volta → Ada)](./targets/sm70-89.md)
- [SM 90 — Hopper](./targets/sm90-hopper.md)
- [SM 100 — Blackwell](./targets/sm100-blackwell.md)
- [SM 120 — Blackwell (sm120)](./targets/sm120.md)

# Data Structures

- [IR Node Layout](./structs/ir-node.md)
- [Pattern Database](./structs/pattern-db.md)
- [DAG Node](./structs/dag-node.md)
- [Symbol Table](./structs/symbol-table.md)

# Configuration

- [CLI Flags](./config/cli-flags.md)
- [Optimization Levels](./config/optimization-levels.md)
- [LLVM Knobs (1,689)](./config/knobs.md)
- [Environment Variables](./config/env-vars.md)
