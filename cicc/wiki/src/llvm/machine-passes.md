# Machine-Level Passes

Machine-level passes in CICC v13.0 operate on `MachineFunction` / `MachineBasicBlock` / `MachineInstr` representations after SelectionDAG instruction selection has converted LLVM IR into target-specific pseudo-instructions. On a conventional CPU target, these passes ultimately produce native machine code; on NVPTX, they produce PTX assembly -- a virtual ISA with unlimited virtual registers and a structured instruction set. This distinction is fundamental: NVPTX's "machine code" still uses virtual registers (`%r0`, `%f1`, `%p3`), and the final PTX text is consumed by `ptxas` which performs the actual register allocation against the hardware register file. The machine-level passes in CICC therefore serve a different purpose than on CPU: they optimize register pressure (to maximize occupancy), structure control flow (PTX requires structured CFG), compute `.local` memory frame layouts, and prepare clean PTX for `ptxas` to finish.

| | |
|---|---|
| **Pass pipeline parser (MF)** | `sub_235E150` (53KB) |
| **Master pass registry** | `sub_2342890` (102KB) |
| **Codegen pass config** | `ctor_335_0` at `0x507310` (88 strings) |
| **NVPTX target pass config** | `ctor_358_0` at `0x50E8D0` (43 strings) |
| **Total registered MF passes** | 51 (stock LLVM) + 13 (NVIDIA custom) |
| **Total MF analyses** | 14 registered |

## Why Machine Passes Matter on GPU

In upstream LLVM for x86 or AArch64, the machine pass pipeline assigns physical registers, inserts spill code, schedules instructions for pipeline hazards, and emits relocatable object code. On NVPTX, none of this maps directly:

1. **No physical register file.** PTX registers are virtual. The greedy register allocator in CICC does not assign physical registers -- it tracks register pressure per class and enforces the `-maxreg` limit (default 70) that controls SM occupancy. When the allocator "spills," it moves values to `.local` memory rather than to stack slots addressed by `%rsp`.

2. **No prolog/epilog in the traditional sense.** There is no call stack with push/pop sequences. `PrologEpilogInserter` in CICC computes `.local` frame offsets for spilled virtual registers and inserts `ld.local`/`st.local` pairs.

3. **Structured control flow is mandatory.** PTX requires structured control flow (`bra`, `@p bra`, `bra.uni`). The `StructurizeCFG` pass runs before instruction selection, and `BranchFolding` must preserve the structured property.

4. **Instruction scheduling targets `ptxas`, not hardware.** Machine scheduling optimizes the instruction stream that `ptxas` will consume. Since `ptxas` performs its own scheduling against the actual hardware pipeline, CICC's scheduling focuses on register pressure reduction (`nvptx-sched4reg`) and exposing parallelism that `ptxas` can exploit.

5. **Two peephole levels.** CICC runs both the stock LLVM `PeepholeOptimizer` (operates on generic `MachineInstr` patterns) and the NVIDIA-specific `NVPTXPeephole` (`sub_21DB090`) which handles PTX-specific patterns like redundant `cvta` instructions, predicate folding, and address space conversions.

## Pipeline Flow

```
SelectionDAG ISel
    │
    ▼
FinalizeISel ─── expand pseudo-instructions from ISel
    │
    ▼
┌─────────────────────────────────────┐
│  Pre-RA Optimization                │
│  ┌─ EarlyTailDuplicate             │
│  ├─ EarlyMachineLICM               │
│  ├─ MachineCSE (RP-aware)          │
│  ├─ MachineSink (gated by knob)    │
│  ├─ PeepholeOptimizer              │
│  ├─ NVPTXPeephole             ★    │
│  ├─ DeadMachineInstrElim           │
│  └─ MachineCopyPropagation         │
└─────────────────────────────────────┘
    │
    ▼
TwoAddressInstruction ─── convert 3-addr to 2-addr form
    │
    ▼
PHIElimination (CSSA/deSSA) ─── lower MachineInstr PHIs to copies
    │
    ▼
┌─────────────────────────────────────┐
│  Register Allocation                │
│  ┌─ LiveIntervals + SlotIndexes    │
│  ├─ RegisterCoalescing             │
│  ├─ RAGreedy (pressure-driven)     │
│  ├─ NVPTXBlockRemat           ★    │
│  └─ StackSlotColoring              │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  Post-RA Optimization               │
│  ┌─ ExpandPostRAPseudos            │
│  ├─ MachineLICM (post-RA)          │
│  ├─ MachineSink (post-RA, gated)   │
│  ├─ MachineCopyPropagation         │
│  ├─ BranchFolding / TailMerge      │
│  ├─ MachineBlockPlacement          │
│  └─ MachinePipeliner (SMS)         │
└─────────────────────────────────────┘
    │
    ▼
PrologEpilogInserter ─── .local frame layout
    │
    ▼
MachineOutliner ─── OUTLINED_FUNCTION_ stub creation
    │
    ▼
NVPTXProxyRegErasure ★ ─── remove redundant cvta.to.local
    │
    ▼
AsmPrinter ─── PTX text emission
```

Passes marked with ★ are NVIDIA-custom. The exact ordering varies by optimization level; at `-O0`, most pre-RA and post-RA optimization passes are skipped and `RegAllocFast` replaces `RAGreedy`.

## Machine Pass Inventory

### NVIDIA-Custom Machine Passes

| Pass ID | Class / Address | Pipeline Position | Description |
|---|---|---|---|
| `nvptx-peephole` | `sub_21DB090` | Pre-RA | PTX-specific peephole: folds redundant address space conversions (`cvta`), optimizes predicate patterns, simplifies PTX-specific instruction sequences. Controlled by `enable-nvvm-peephole` (default: on). |
| `nvptx-remat-block` | `sub_217DBF0` | During RA | Machine-level block rematerialization. Iterative "pull-in" algorithm that recomputes values near their use rather than loading from spill slots. Two-phase candidate selection with a "second-chance" heuristic. See [Rematerialization](../passes/rematerialization.md). |
| `machine-rpa` | `sub_21EAA00` | Analysis (pre-RA) | Machine Register Pressure Analysis. Provides per-basic-block pressure data consumed by `MachineCSE`, scheduling, and rematerialization. |
| `extra-machineinstr-printer` | `sub_21E9E80` | Diagnostic | Prints per-function register pressure statistics. Debug-only pass for tuning pressure heuristics. |
| `nvptx-mem2reg` | `sub_21F9920` | Pre-RA | Machine-level mem2reg: promotes `.local` memory loads/stores back to virtual registers when profitable. Conditional on `byte_4FD25C0` (`nv-disable-mem2reg` inverts). |
| `ldgxform` | `sub_21F2780` | Pre-RA | Transforms qualifying global memory loads into `ld.global.nc` (LDG -- load through read-only data cache). Splits wide vector loads for hardware constraints. |
| `nvptx-prolog-epilog` | `sub_21DB5F0` | Post-RA | NVPTX-specific PrologEpilog pass. Works alongside or replaces the stock PEI to handle PTX frame semantics where there is no traditional stack pointer. |
| `nvptx-proxy-reg-erasure` | `sub_21DA810` | Late post-RA | Removes redundant `cvta.to.local` instructions left by address space lowering. |
| `nvptx-assign-valid-global-names` | `sub_21BCD80` | Pre-emission | Sanitizes symbol names to comply with PTX naming rules (no `@`, `$`, or other characters illegal in PTX identifiers). |
| `nvptx-replace-image-handles` | `sub_21DBEA0` | Pre-emission | Replaces IR-level texture/surface handle references with PTX-level `.tex` / `.surf` declarations. |
| `nvptx-image-optimizer` | `sub_21BCF10` | Pre-emission | Texture/surface instruction optimization: coalesces related texture operations, validates image type consistency for `tex`, `suld`, `sust`, `suq`. |
| `alloca-hoisting` | `sub_21BC7D0` | Early post-ISel | Hoists alloca instructions to the entry basic block, enabling the frame layout pass to assign fixed offsets. |
| `generic-to-nvvm` | `sub_215DC20` | Early post-ISel | Converts generic address space (0) references to global address space (1). Runs before instruction selection on some pipelines, but also present as a machine-level fixup. |

### Stock LLVM Machine Passes (NVPTX Configuration)

| Pass ID | Class | NVIDIA Modification | Notes |
|---|---|---|---|
| `finalize-isel` | `FinalizeISelPass` | None | Expands ISel pseudo-instructions; mandatory first MF pass. |
| `early-tailduplication` | `EarlyTailDuplicatePass` | None | Pre-RA tail duplication. Can be disabled via `disable-early-taildup`. |
| `early-machinelicm` | `EarlyMachineLICMPass` | Gated | Controlled by `enable-mlicm`. Hoists loop-invariant machine instructions before register allocation. |
| `machine-cse` | `MachineCSEPass` | **Modified** | NVIDIA adds register-pressure-aware CSE (`rp-aware-mcse`, `pred-aware-mcse`, `copy-prop-mcse`). Uses MRPA (`sub_2E5A4E0`) for incremental pressure tracking. See [Instruction Scheduling](./scheduling.md). |
| `machine-sink` | `MachineSinkingPass` | Gated | Disabled by default on NVPTX; enabled via `nvptx-enable-machine-sink`. When active, sinks instructions closer to uses to reduce register pressure. |
| `peephole-opt` | `PeepholeOptimizerPass` | None | Stock LLVM peephole: folds redundant copies, simplifies compare-and-branch patterns, optimizes sub-register operations. Can be disabled via `disable-peephole`. |
| `dead-mi-elimination` | `DeadMachineInstrElimPass` | None | Eliminates dead machine instructions. Can be disabled via `disable-machine-dce`. |
| `machine-cp` | `MachineCopyPropagationPass` | None | Propagates copies to reduce move instructions. Can be disabled via `disable-copyprop`. |
| `machinelicm` | `MachineLICMPass` | Gated | Post-RA variant. Controlled by `disable-postra-machine-licm`. NVIDIA adds `sink-insts-to-avoid-spills` to trade hoisting for spill reduction. |
| `two-address-instruction` | `TwoAddressInstructionPass` | None (stock) | Converts three-address instructions to two-address form by inserting copies. `sub_1F53550` (79KB, 2470 lines). Shared between cicc and libNVVM (twin at `sub_F4EA80`). |
| `phi-node-elimination` | `PHIEliminationPass` | **Modified** | NVIDIA's CSSA/deSSA method selection via `usedessa` (default 2). Controls how machine-level PHI nodes are lowered to copies; affects register allocation quality. See `cssa-coalesce`, `cssa-verbosity`. |
| `register-coalescer` | `RegisterCoalescerPass` | **Custom NVPTX variant** | The NVPTX backend has its own register coalescing framework at `0x349`--`0x34B` (separate from LLVM's stock coalescer at `0xB40000`). Uses interference oracle `sub_349D6E0`, open-addressing hash with `(reg >> 9) ^ (reg >> 4)`. See [Register Coalescing](./register-coalescing.md). |
| `greedy` | `RAGreedyPass` | **Modified** | Pressure-driven rather than assignment-driven. Dual instances (legacy + new PM). Core at `sub_2F49070` (82KB). See [Register Allocation](./register-allocation.md). |
| `stack-coloring` | `StackColoringPass` | None | Colors stack slots to reduce `.local` memory usage by sharing slots with non-overlapping lifetimes. |
| `stack-slot-coloring` | `StackSlotColoringPass` | None | Secondary stack slot optimization. Can be disabled via `disable-ssc`. |
| `post-ra-pseudos` | `ExpandPostRAPseudosPass` | None | Expands post-RA pseudo-instructions (e.g., `COPY` to actual move). |
| `post-RA-sched` | `PostRASchedulerPass` | Gated | Post-RA instruction scheduling. Controlled by `disable-post-ra`. |
| `machine-scheduler` | `MachineSchedulerPass` | **Modified** | NVIDIA adds `nvptx-sched4reg` mode for register-pressure-driven scheduling. Pre-RA scheduling variant. |
| `postmisched` | `PostMachineSchedulerPass` | None | Post-RA machine scheduling with `ScheduleDAGMILive` (`sub_355F610`, 64KB). Controlled by `misched-postra`. |
| `early-ifcvt` | `EarlyIfConverterPass` | None | If-conversion before register allocation. Can be disabled via `disable-early-ifcvt`. |
| `machine-combiner` | `MachineCombinerPass` | None | Combines machine instructions using target-defined patterns. Knob: `machine-combiner-inc-threshold`. |
| `block-placement` | `MachineBlockPlacement` | None (stock) | Profile-guided basic block ordering. `sub_3521FF0` (82KB). Uses ext-TSP and chain-based algorithms. See [Block Placement](./block-placement.md). |
| `machine-outliner` | `MachineOutliner` | None | Creates `OUTLINED_FUNCTION_` stubs for repeated instruction sequences. `sub_3537010` (77KB). See [MachineOutliner](./machine-outliner.md). |
| `prologepilog` | `PrologEpilogInserter` | **Modified** | NVIDIA's PEI (`sub_35B1110`, 68KB) computes `.local` memory frame offsets. Frame objects are 40-byte records with offset, size, alignment, and spill-slot flags. See [PrologEpilogInserter](./prolog-epilog.md). |
| `opt-phis` | `OptimizePHIsPass` | None | Optimizes machine-level PHI nodes (removes trivially dead or redundant PHIs). |
| `tailduplication` | `TailDuplicatePass` | None | Post-RA tail duplication. Controlled by `disable-tail-duplicate`. |
| `detect-dead-lanes` | `DetectDeadLanesPass` | None | Detects unused sub-register lanes; minimal impact on NVPTX since register classes are fully disjoint. |
| `rename-independent-subregs` | `RenameIndependentSubregsPass` | None | Splits sub-register live ranges into independent virtual registers. |
| `localstackalloc` | `LocalStackSlotAllocationPass` | None | Allocates local frame indices for large stack objects. |
| `machine-latecleanup` | `MachineLateInstrsCleanupPass` | None | Late-stage dead instruction cleanup. |
| `machine-pipeliner` | `MachinePipeliner` | None (stock) | Swing Modulo Scheduling for loop bodies. `sub_3563190` (58KB). See below. |

### MachinePipeliner (SMS) Detail

The Swing Modulo Scheduler at `sub_3563190` performs software pipelining -- overlapping successive loop iterations to hide latency. It operates on a single loop body at the MachineInstr level:

1. **DAG construction**: builds a data dependency graph with `sub_2F97F60`, computes latencies via `sub_3559990`, adds edges via `sub_3542B20`.
2. **MII computation**: `RecMII` (recurrence-based) via `sub_354CBB0`, `ResMII` (resource-based) via `sub_35449F0`. `MII = max(RecMII, ResMII)`.
3. **Early exits**: MII == 0 is invalid; MII > `SwpMaxMii` (default 27, `-pipeliner-max-mii`) aborts.
4. **Schedule construction**: ASAP/ALAP times, topological sort, core SMS node placement, then finalization.
5. **Kernel generation**: Three code generation backends selected by priority -- annotation-only (`pipeliner-annotate-for-testing`), MVE-based (`pipeliner-mve-cg`, default enabled), and experimental peeling (`pipeliner-experimental-cg`).

The pipeliner stores its schedule context as a 616-byte (`0x268`) structure with four SmallVectors and per-BB data at 256-byte stride. Maximum pipeline stages: `SwpMaxStages` (default 3, `-pipeliner-max-stages`).

## Machine-Level Analysis Infrastructure

Machine passes depend on a set of analysis passes that compute liveness, dominance, and frequency information over the `MachineFunction` representation.

| Analysis ID | Class | Description |
|---|---|---|
| `slot-indexes` | `SlotIndexesAnalysis` | Assigns a dense integer index to every instruction slot in the function. All liveness computations reference slot indexes rather than instruction pointers, enabling O(log n) interval queries. |
| `live-intervals` | `LiveIntervalsAnalysis` | Computes live ranges for every virtual register as a set of `[start, end)` slot-index intervals. The `LiveRangeCalc` engine (`sub_2FC4FC0`, 12.9KB) manages 296-byte segment entries with inline small-object buffers for endpoint, register mask, kill-set, and use-def chain data. See [LiveRangeCalc](./live-range-calc.md). |
| `live-reg-matrix` | `LiveRegMatrixAnalysis` | Tracks physical register unit interference. On NVPTX, used primarily for register-class-level pressure tracking rather than physical unit assignment. |
| `machine-dom-tree` | `MachineDominatorTreeAnalysis` | Dominance tree over `MachineBasicBlock` graph. Required by LICM, CSE, sinking, and register allocation. |
| `machine-post-dom-tree` | `MachinePostDominatorTreeAnalysis` | Post-dominance tree. Used by block placement (`sub_3521FF0` stores at `this+544`). |
| `machine-loops` | `MachineLoopAnalysis` | Loop detection on the machine CFG. Used by LICM, block placement, and the pipeliner. |
| `machine-block-freq` | `MachineBlockFrequencyAnalysis` | Block frequency estimates (profile-guided or static). Block placement uses this at `this+528` to drive chain construction. |
| `machine-branch-prob` | `MachineBranchProbabilityAnalysis` | Branch probability data. Block placement stores at `this+536`. |
| `machine-trace-metrics` | `MachineTraceMetricsAnalysis` | Trace-based metrics (critical path length, resource depth). Used by `MachineCombiner` and if-conversion. |
| `machine-opt-remark-emitter` | `MachineOptRemarkEmitterAnalysis` | Optimization remark emission for machine passes. |
| `edge-bundles` | `EdgeBundlesAnalysis` | Groups CFG edges into bundles for spill placement. |
| `spill-code-placement` | `SpillPlacementAnalysis` | Determines optimal spill/reload points using edge bundles and frequency data. |
| `regalloc-evict` | `RegAllocEvictionAdvisorAnalysis` | Advises the greedy allocator on which live range to evict. |
| `regalloc-priority` | `RegAllocPriorityAdvisorAnalysis` | Assigns allocation priority to live ranges. |
| `virtregmap` | `VirtRegMapAnalysis` | Maps virtual registers to their assigned physical registers (or spill slots). |
| `machine-rpa` ★ | `sub_21EAA00` | NVIDIA-custom machine register pressure analysis. Provides per-BB pressure data consumed by RP-aware MCSE, scheduling, and rematerialization. |

## Machine Pass Knobs Summary

### NVIDIA Target Pass Enable/Disable

| Knob | Type | Default | Effect |
|---|---|---|---|
| `enable-nvvm-peephole` | bool | true | Enable NVPTX-specific peephole optimizer |
| `nvptx-enable-machine-sink` | bool | false | Enable MachineSink on NVPTX (off by default due to pressure concerns) |
| `enable-mlicm` | bool | (opt-level dependent) | Enable MachineLICM on NVPTX |
| `enable-mcse` | bool | (opt-level dependent) | Enable MachineCSE on NVPTX |
| `nv-disable-mem2reg` | bool | false | Disable machine-level mem2reg |
| `nv-disable-remat` | bool | false | Disable all NVIDIA rematerialization passes |
| `enable-new-nvvm-remat` | bool | (varies) | Enable new NVVM remat, disable old |
| `usedessa` | int | 2 | Select deSSA method for PHI elimination |
| `cssa-coalesce` | int | (varies) | Controls PHI operand coalescing aggressiveness |

### Stock LLVM Codegen Controls

| Knob | Type | Default | Effect |
|---|---|---|---|
| `disable-machine-dce` | bool | false | Disable dead machine instruction elimination |
| `disable-machine-licm` | bool | false | Disable pre-RA MachineLICM |
| `disable-postra-machine-licm` | bool | false | Disable post-RA MachineLICM |
| `disable-machine-cse` | bool | false | Disable MachineCSE |
| `disable-machine-sink` | bool | false | Disable MachineSink (NVPTX also gates via `nvptx-enable-machine-sink`) |
| `disable-postra-machine-sink` | bool | false | Disable post-RA MachineSink |
| `disable-branch-fold` | bool | false | Disable BranchFolding / tail merge |
| `disable-tail-duplicate` | bool | false | Disable post-RA tail duplication |
| `disable-early-taildup` | bool | false | Disable pre-RA tail duplication |
| `disable-block-placement` | bool | false | Disable MachineBlockPlacement |
| `disable-copyprop` | bool | false | Disable MachineCopyPropagation |
| `disable-ssc` | bool | false | Disable Stack Slot Coloring |
| `disable-post-ra` | bool | false | Disable post-RA scheduler |
| `disable-early-ifcvt` | bool | false | Disable early if-conversion |
| `disable-peephole` | bool | false | Disable stock LLVM peephole optimizer |
| `enable-machine-outliner` | enum | (varies) | `disable` / `enable` / `guaranteed beneficial` |
| `misched-postra` | bool | false | Run MachineScheduler post-RA |
| `optimize-regalloc` | bool | true | Enable optimized register allocation path |
| `verify-machineinstrs` | bool | false | Run MachineVerifier after each pass |

### NVIDIA RP-Aware MachineCSE Knobs

| Knob | Type | Default | Effect |
|---|---|---|---|
| `rp-aware-mcse` | bool | (varies) | Enable register-pressure-aware MachineCSE |
| `pred-aware-mcse` | bool | (varies) | Enable predicate-register-pressure-aware MCSE |
| `copy-prop-mcse` | bool | (varies) | Enable copy propagation within MachineCSE |
| `verify-update-mcse` | bool | false | Debug: verify incremental MRPA updates against full recomputation |
| `cta-reconfig-aware-mrpa` | bool | (varies) | CTA reconfiguration aware machine RP analysis |

### Pipeliner Knobs

| Knob | Type | Default | Effect |
|---|---|---|---|
| `pipeliner-max-mii` | int | 27 | Maximum Minimal Initiation Interval before abort |
| `pipeliner-max-stages` | int | 3 | Maximum pipeline stages |
| `pipeliner-ignore-recmii` | bool | false | Zero out RecMII, use only ResMII |
| `pipeliner-annotate-for-testing` | bool | false | Annotate schedule without modifying code |
| `pipeliner-experimental-cg` | bool | false | Use experimental peeling code generator |
| `pipeliner-mve-cg` | bool | true | Use MVE code generator (default path) |
| `outliner-benefit-threshold` | int | 1 | Minimum size in bytes for outlining candidate |

## Cross-References

- [SelectionDAG](./selectiondag.md) -- the ISel pass that produces MachineInstrs consumed by machine passes
- [Register Allocation](./register-allocation.md) -- pressure-driven greedy allocator with NVPTX register classes
- [Register Coalescing](./register-coalescing.md) -- NVPTX-custom copy elimination framework
- [PrologEpilogInserter & Frame Layout](./prolog-epilog.md) -- `.local` memory frame computation
- [MachineOutliner](./machine-outliner.md) -- suffix-tree-based code size reduction
- [Block Placement](./block-placement.md) -- profile-guided basic block ordering
- [Instruction Scheduling](./scheduling.md) -- MRPA, MachinePipeliner, ScheduleDAGMILive
- [Rematerialization](../passes/rematerialization.md) -- NVIDIA's custom machine-level remat
- [NVVM Peephole](../passes/nvvm-peephole.md) -- IR-level NVVM peephole (distinct from machine-level `nvptx-peephole`)
- [AsmPrinter & PTX Emission](../infra/asmprinter.md) -- final pass: MachineInstr to PTX text
- [Code Generation](../pipeline/codegen.md) -- pipeline overview including ISel and DAG infrastructure
- [StructurizeCFG](./structurizecfg.md) -- mandatory CFG structurization (runs before ISel, feeds machine passes)
