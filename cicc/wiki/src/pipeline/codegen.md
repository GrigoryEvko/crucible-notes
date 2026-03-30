# Code Generation

NVPTX backend: SelectionDAG lowering, instruction selection, register allocation, and machine-level passes. Address range `0x1700000`–`0x35EFFFF` (~37 MB of code) -- the largest address range in the binary. This page is the hub for the entire code generation pipeline; each stage has a dedicated deep-dive page linked below.

| | |
|---|---|
| **SelectionDAG pipeline** | [SelectionDAG & ISel](../llvm/selectiondag.md) — build, legalize, combine, select |
| **Type legalization** | [Type Legalization](../llvm/type-legalization.md) — 348KB monolithic dispatch |
| **ISel patterns** | [ISel Pattern Matching](../llvm/isel-patterns.md) — three-level dispatch, 900KB |
| **Register allocation** | [Register Allocation](../llvm/register-allocation.md) — pressure-driven greedy RA |
| **Register classes** | [NVPTX Register Classes](../reference/register-classes.md) — nine classes, ID map |
| **Scheduling** | [Instruction Scheduling](../llvm/scheduling.md) — MRPA, pipeliner, post-RA |
| **Machine passes** | [Machine-Level Passes](../llvm/machine-passes.md) — MRPA, remat, LDG, peephole |
| **StructurizeCFG** | [StructurizeCFG](../llvm/structurizecfg.md) — mandatory structured control flow |
| **CodeGenPrepare** | [CodeGenPrepare & SCEV-CGP](../llvm/codegen-prepare.md) — IR-level backend prep |
| **KnownBits** | [KnownBits & DemandedBits](../llvm/known-bits.md) — fused analysis with GPU SR oracle |
| **Tensor core codegen** | [MMA Code Generation](../llvm/mma-codegen.md) — HMMA/IMMA/WGMMA/tcgen05 lowering pipeline |
| **Tensor core builtins** | [Tensor / MMA Builtins](../builtins/tensor-mma.md) — per-ID reference, validation rules |
| **Atomics** | [Atomic Builtins](../builtins/atomics.md) — scope-aware atom lowering |
| **Target infrastructure** | [NVPTX Target Infrastructure](../infra/nvptx-target.md) — TargetMachine, TTI, SubtargetFeatures |
| **Live range calc** | [LiveRangeCalc](../llvm/live-range-calc.md) — dual-bitvector liveness |
| **Rematerialization** | [Rematerialization](../passes/rematerialization.md) — IR-level + machine-level remat |
| **InstrEmitter** | [InstrEmitter](../llvm/instr-emitter.md) — DAG-to-MachineInstr conversion |
| **DAG node layout** | [SelectionDAG Node Structure](../structs/dag-node.md) — 104-byte SDNode |

## Architecture

The code generation pipeline runs after the [LLVM optimizer](optimizer.md) and produces MachineIR that the [PTX emission](emission.md) stage serializes to text. The pipeline follows upstream LLVM's SelectionDAG architecture with NVIDIA-specific passes inserted at key points.

```
LLVM IR
  │
  ├─ CodeGenPrepare (IR-level backend prep)
  │    sub_1D70000-1D7FFFF: sunkaddr, sunk_phi, block splitting
  │
  ├─ SelectionDAG Build
  │    sub_2065D30 (visit dispatcher)
  │    sub_2056920 (major worker, 69KB)
  │    sub_2077400 (NVVM tex/surf handle lowering) ★ NVIDIA
  │    sub_2072590 (NVPTX argument passing, 38KB) ★ NVIDIA
  │
  ├─ LegalizeTypes
  │    sub_20019C0 (348KB main loop)
  │    sub_201E5F0 (opcode dispatch, 81KB)
  │    sub_201BB90 (expand integer, 75KB)
  │
  ├─ LegalizeOp
  │    sub_1FFB890 (169KB, type action dispatch)
  │    sub_1FF6F70 (43KB, atomic target-specific lowering) ★ NVIDIA
  │
  ├─ DAG Combining
  │    sub_F681E0 (65KB, top-level orchestrator)
  │    sub_F20C20 (64KB, visitNode main)
  │
  ├─ Instruction Selection
  │    sub_3090F90 (91KB, NVPTXDAGToDAGISel::Select) ★ NVIDIA
  │    sub_33D4EF0 (complex addressing, calls sub_969240 399×)
  │
  ├─ Instruction Scheduling
  │    sub_355F610 (64KB, ScheduleDAGMILive post-RA)
  │    sub_3563190 (58KB, MachinePipeliner)
  │
  ├─ Register Allocation
  │    sub_2F49070 (82KB, RAGreedy::selectOrSplit)
  │    sub_2F2D9F0 (93KB, LiveRangeSplitter)
  │
  ├─ Machine-Level Passes
  │    MRPA, Block Remat, Mem2Reg, LDG, Peephole, etc.
  │
  └─ StructurizeCFG
       sub_35CC920 (95KB, mandatory for PTX structured control flow)
```

Items marked **★ NVIDIA** are NVIDIA-proprietary additions not present in upstream LLVM.

## Stage Overview

**CodeGenPrepare** ([detail](../llvm/codegen-prepare.md)) -- last IR-level pass before ISel. Sinks address computations, creates PHI nodes for sunk values, and splits critical edges. NVIDIA adds an optional SCEV-CGP extension.

**SelectionDAG Build** ([detail](../llvm/selectiondag.md)) -- converts LLVM IR into a target-independent DAG. NVPTX intercepts for `.param`-space argument passing and texture/surface handle lowering.

**Type Legalization** ([detail](../llvm/type-legalization.md)) -- rewrites every illegal type into legal equivalents via promote, expand, soften, or split-vector actions.

**Operation Legalization** -- processes nodes whose opcodes are illegal for the target. [Atomic operations](../builtins/atomics.md) receive NVIDIA-specific scope-aware lowering (CTA/GPU/SYS) with per-SM feature gates.

**DAG Combining** -- folds redundant operations, canonicalizes patterns, and reduces the DAG before instruction selection. The [KnownBits](../llvm/known-bits.md) analysis feeds into combining decisions.

**Instruction Selection** ([detail](../llvm/isel-patterns.md)) -- matches DAG nodes against PTX instruction patterns via a three-level dispatch hierarchy. A compressed per-SM-variant legality table gates which opcodes exist on which GPU architecture.

**Instruction Scheduling** ([detail](../llvm/scheduling.md)) -- post-RA scheduling plus an optional software pipeliner. NVIDIA's custom MRPA provides incremental register pressure tracking.

**Register Allocation** ([detail](../llvm/register-allocation.md)) -- pressure-driven greedy allocator adapted for PTX's virtual register model. Works with nine typed [register classes](../reference/register-classes.md); [live range splitting](../llvm/live-range-calc.md) and [rematerialization](../passes/rematerialization.md) reduce spill pressure.

**Machine-Level Passes** ([detail](../llvm/machine-passes.md)) -- NVIDIA-proprietary and stock LLVM passes that optimize register pressure, promote stack objects back to registers, and prepare clean PTX for `ptxas`.

**StructurizeCFG** ([detail](../llvm/structurizecfg.md)) -- mandatory pass that converts arbitrary CFGs into the structured form PTX requires, rejecting irreducible CFGs and EH funclets.

## Cross-References

- [NVPTX Subtarget & feature flags](../infra/nvptx-target.md) -- SM processor table, type legality offsets
- [GPU target feature gates](../targets/index.md) -- per-SM architecture feature matrix
- [DAG node structure](../structs/dag-node.md) -- SDNode 104-byte layout, operand stride
- [Pattern database](../structs/pattern-db.md) -- ISel pattern table format
- [NVPTX machine opcodes](../reference/nvptx-opcodes.md) -- opcode reference
- [Address spaces](../reference/address-spaces.md) -- global, shared, local, param encoding
- [PTX emission](emission.md) -- downstream consumer of machine-level output
- [Register coalescing](../llvm/register-coalescing.md) -- pre-RA copy elimination
- [PrologEpilogInserter](../llvm/prolog-epilog.md) -- `.local` frame layout
