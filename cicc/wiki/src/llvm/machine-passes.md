# Machine-Level Passes

> **Mixed provenance.** Of the 64 registered MF passes, 51 are stock LLVM 20.0.0 (`MachineScheduler`, `RAGreedy`, `PrologEpilogInserter`, `BranchFolderPass`, `MachineBlockPlacement`, etc.) and 13 are NVIDIA-only. The NVIDIA additions cluster around four areas: PTX structurization (`nvptx-lower-aggr-copies`, `nvptx-lower-args`), pre-RA pressure shaping (MRPA, IV demotion, rematerialization), texture-group merging, and AsmPrinter glue (`nvptx-prolog-epilog`, `nvptx-proxy-reg-erasure`). Each pass on this umbrella links to its dedicated page where the upstream/NVIDIA delta is described.
>
> **Upstream source:** Core codegen drivers in `llvm/lib/CodeGen/*.cpp` (LLVM 20.0.0); NVPTX-specific MF passes in `llvm/lib/Target/NVPTX/NVPTX*.cpp`. Pipeline assembly (`addISelPasses`, `addPreRegAlloc`, `addPostRegAlloc`) follows the stock `TargetPassConfig` interface; NVIDIA overrides via `NVPTXPassConfig`.

Machine-level passes in CICC v13.0 operate on `MachineFunction` / `MachineBasicBlock` / `MachineInstr` representations after SelectionDAG instruction selection has converted LLVM IR into target-specific pseudo-instructions. On a conventional CPU target, these passes ultimately produce native machine code; on NVPTX, they produce PTX assembly — a virtual ISA with unlimited virtual registers and a structured instruction set. This distinction is fundamental: NVPTX's "machine code" still uses virtual registers (`%r0`, `%f1`, `%p3`), and the final PTX text is consumed by `ptxas` which performs the actual register allocation against the hardware register file. The machine-level passes in CICC therefore serve a different purpose than on CPU: they optimize register pressure (to maximize occupancy), structure control flow (PTX requires structured CFG), compute `.local` memory frame layouts, and prepare clean PTX for `ptxas` to finish.

| | |
|---|---|
| **Pass pipeline parser (MF)** | `sub_235E150` (53KB) |
| **Master pass registry** | `sub_2342890` (102KB) |
| **Codegen pass config** | `ctor_335_0` at `0x507310` (88 strings) |
| **NVPTX target pass config** | `ctor_358_0` at `0x50E8D0` (43 strings) |
| **Total registered MF passes** | 51 (stock LLVM) + 13 (NVIDIA custom) |
| **Total MF analyses** | 14 registered |
| **Pipeline configuration** | `sub_2166D20` (addISelPasses), `sub_2166ED0` (addPreRegAlloc), `sub_21668D0` (addPostRegAlloc) |

## Why Machine Passes Matter on GPU

In upstream LLVM for x86 or AArch64, the machine pass pipeline assigns physical registers, inserts spill code, schedules instructions for pipeline hazards, and emits relocatable object code. On NVPTX, none of this maps directly:

1. **No physical register file.** PTX registers are virtual. The greedy register allocator in CICC does not assign physical registers — it tracks register pressure per class and enforces the `-maxreg` limit (default 70) that controls SM occupancy. When the allocator "spills," it moves values to `.local` memory rather than to stack slots addressed by `%rsp`.

2. **No prolog/epilog in the traditional sense.** There is no call stack with push/pop sequences. `PrologEpilogInserter` in CICC computes `.local` frame offsets for spilled virtual registers and inserts `ld.local`/`st.local` pairs.

3. **Structured control flow is mandatory.** PTX requires structured control flow (`bra`, `@p bra`, `bra.uni`). The `StructurizeCFG` pass runs before instruction selection, and `BranchFolding` must preserve the structured property.

4. **Instruction scheduling targets `ptxas`, not hardware.** Machine scheduling optimizes the instruction stream that `ptxas` will consume. Since `ptxas` performs its own scheduling against the actual hardware pipeline, CICC's scheduling focuses on register pressure reduction (`nvptx-sched4reg`) and exposing parallelism that `ptxas` can exploit.

5. **Two peephole levels.** CICC runs both the stock LLVM `PeepholeOptimizer` (operates on generic `MachineInstr` patterns) and the NVIDIA-specific `NVPTXPeephole` (`sub_21DB090`) which handles PTX-specific patterns like redundant `cvta` instructions, predicate folding, and address space conversions.

## Pipeline Flow

```text
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

### Pipeline Configuration Functions

The NVPTX backend configures the machine pass pipeline through three key functions:

**`sub_2166D20` — addISelPasses()**: Configures passes before instruction selection. Diagnostic string: `"\n\n*** Final LLVM Code input to ISel ***\n"`. Adds: alloca hoisting, ISel DAG printer (conditional), `NVPTXProxyRegErasure`, `NVPTXLowerArgs`, NVPTX-specific ISel.

**`sub_2166ED0` — addPreRegAlloc()**: Configures machine passes before register allocation. Diagnostic strings: `"After Pre-RegAlloc TailDuplicate"`, `"After codegen DCE pass"`, `"After Machine LICM, CSE and Sinking passes"`, `"After codegen peephole optimization pass"`. Adds: TailDuplicate, codegen DCE, Machine LICM + CSE + Sinking (conditional on `byte_4FD1980`, `byte_4FD18A0`, `byte_4FD1A60`), codegen peephole.

**`sub_21668D0` — addPostRegAlloc()**: Configures post-register-allocation passes. Diagnostic strings: `"After Machine Scheduling"`, `"After StackSlotColoring"`. Adds: Machine scheduling (2 modes controlled by `dword_4FD26A0` — value 1 selects simple scheduling, otherwise full pipeline), Stack slot coloring, `nvptx-mem2reg` (conditional on `byte_4FD25C0`).

## Machine Pass Inventory

### NVIDIA-Custom Machine Passes

| Pass ID | Class / Address | Pipeline Position | Description |
|---|---|---|---|
| `nvptx-peephole` | `sub_21DB090` | Pre-RA | PTX-specific peephole: folds redundant address space conversions (`cvta`), optimizes predicate patterns, simplifies PTX-specific instruction sequences. Controlled by `enable-nvvm-peephole` (default: on). |
| `nvptx-remat-block` | `sub_217DBF0` | During RA | Machine-level block rematerialization. Iterative "pull-in" algorithm that recomputes values near their use rather than loading from spill slots. Two-phase candidate selection with a "second-chance" heuristic. See [Rematerialization](../passes/rematerialization.md). |
| `machine-rpa` | `sub_21EAA00` | Analysis (pre-RA) | Machine Register Pressure Analysis. Provides per-basic-block pressure data consumed by `MachineCSE`, scheduling, and rematerialization. |
| `extra-machineinstr-printer` | `sub_21E9E80` | Diagnostic | Prints per-function register pressure statistics. Debug-only pass for tuning pressure heuristics. |
| `nvptx-mem2reg` | `sub_21F9920` | Pre-RA | Machine-level mem2reg: promotes `.local` memory loads/stores back to virtual registers when profitable. Conditional on `byte_4FD25C0` (`nv-disable-mem2reg` inverts). |
| `ldgxform` | `sub_21F2780` | Pre-RA | Transforms qualifying global memory loads into `ld.global.nc` (LDG — load through read-only data cache). Splits wide vector loads for hardware constraints. |
| `nvptx-prolog-epilog` | `sub_21DB5F0` | Post-RA | NVPTX-specific PrologEpilog pass. Works alongside or replaces the stock PEI to handle PTX frame semantics where there is no traditional stack pointer. |
| `nvptx-proxy-reg-erasure` | `sub_21DA810` | Late post-RA | Removes redundant `cvta.to.local` instructions left by address space lowering. |
| `nvptx-assign-valid-global-names` | `sub_21BCD80` | Pre-emission | Sanitizes symbol names to comply with PTX naming rules (no `@`, `$`, or other characters illegal in PTX identifiers). |
| `nvptx-replace-image-handles` | `sub_21DBEA0` | Pre-emission | Replaces IR-level texture/surface handle references with PTX-level `.tex` / `.surf` declarations. |
| `nvptx-image-optimizer` | `sub_21BCF10` | Pre-emission | Texture/surface instruction optimization: coalesces related texture operations, validates image type consistency for `tex`, `suld`, `sust`, `suq`. |
| `alloca-hoisting` | `sub_21BC7D0` | Early post-ISel | Hoists alloca instructions to the entry basic block, enabling the frame layout pass to assign fixed offsets. |
| `generic-to-nvvm` | `sub_215DC20` | Early post-ISel | Converts generic address space (0) references to global address space (1). Runs before instruction selection on some pipelines, but also present as a machine-level fixup. |
| `param-opt` | `sub_2203290` | Post-ISel | Optimizes `ld.param` instructions. NVIDIA-custom pass for parameter load coalescing and redundant parameter load elimination. |
| `nvptx-trunc-opts` | `sub_22058E0` | Post-ISel | Optimizes redundant `ANDb16ri` instructions [sic: binary string reads "instrunctions"] generated during i16 truncation patterns. |
| `redundant-move-elim` | `sub_2204E60` | Post-ISel | Removes redundant register-to-register moves left by instruction selection. |

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

---

## Per-Pass Algorithm Descriptions

### NVPTXPeephole (`sub_21DB090`) — PTX-Specific Peephole Optimizer

Registration: `sub_21DB090` at `0x21DB090`, pass ID `"nvptx-peephole"`. Enabled by default; controlled by `enable-nvvm-peephole`.

This pass runs pre-RA and performs pattern-matching rewrites on `MachineInstr` sequences that are specific to the NVPTX target. Unlike the stock LLVM `PeepholeOptimizer` (which operates on generic copy/compare patterns), `NVPTXPeephole` handles PTX address space semantics and predicate register idioms.

**Patterns handled:**

1. **Redundant `cvta` elimination.** When address space lowering inserts `cvta.to.global` or `cvta.to.shared` followed by an operation that already operates in the correct address space, the `cvta` is dead. The pass scans for `cvta` instructions whose result is used only by instructions with matching address space qualifiers, and deletes the `cvta`.

2. **Predicate folding.** PTX predicates (`%p0`, `%p1`, ...) are first-class. The pass identifies patterns where a `setp` instruction produces a predicate that is consumed by exactly one `@p bra` and folds them into a conditional branch with embedded comparison.

3. **Address space conversion simplification.** When `generic-to-nvvm` inserts `addrspacecast` and the consuming instruction directly emits the correct address qualifier (`.global`, `.shared`, `.local`, `.const`), the intermediate cast is redundant.

```rust
// Pseudocode: NVPTXPeephole main loop
fn nvptx_peephole(MF: &mut MachineFunction) -> bool {
    let mut changed = false;
    for mbb in MF.basic_blocks() {
        let mut dead_list = vec![];
        for mi in mbb.instrs() {
            match mi.opcode() {
                NVPTX::CVTAToGeneric | NVPTX::CVTAToGlobal
                | NVPTX::CVTAToShared | NVPTX::CVTAToLocal => {
                    if single_user_in_matching_addrspace(mi) {
                        propagate_operand_and_kill(mi);
                        dead_list.push(mi);
                        changed = true;
                    }
                }
                NVPTX::SETP_* => {
                    if let Some(bra) = single_predicate_consumer(mi) {
                        fold_setp_into_branch(mi, bra);
                        dead_list.push(mi);
                        changed = true;
                    }
                }
                _ => {}
            }
        }
        for mi in dead_list { mi.erase_from_parent(); }
    }
    changed
}
```

### NVPTXBlockRemat (`sub_217DBF0`) — Machine-Level Block Rematerialization

Registration: `sub_217DBF0` at `0x217DBF0`, pass name `"NVPTX Specific Block Remat"`, pass ID `"nvptx-remat-block"`. Knob constructor at `ctor_361_0` (`0x5108E0`). Main engine: `sub_2186D90` (47KB, ~1742 decompiled lines).

This is NVIDIA's custom register-pressure-reduction pass. It re-computes values at their use sites instead of keeping them live across long spans. The algorithm is iterative with a two-phase candidate selection including a "second-chance" heuristic for marginal candidates.

**Knobs (16 total):**

| Global Variable | CLI Flag | Default | Description |
|---|---|---|---|
| `dword_4FD3820` | `nv-remat-block` | 14 | Bitmask controlling remat modes (bits 0-3) |
| `dword_4FD3740` | `nv-remat-max-times` | 10 | Max iterations of the outer remat loop |
| `dword_4FD3660` | `nv-remat-block-single-cost` | 10 | Max cost per single live value pull-in |
| `dword_4FD3580` | `nv-remat-block-map-size-limit` | 6 | Map size limit for single pull-in |
| `dword_4FD3040` | `nv-remat-block-max-cost` | 100 | Max total clone cost per live value reduction |
| `dword_4FD3120` | `nv-remat-block-liveout-min-percentage` | 70 | Min liveout % for special consideration |
| `unk_4FD3400` | `nv-remat-block-loop-cost-factor` | 20 | Loop cost multiplier |
| `unk_4FD3320` | `nv-remat-default-max-reg` | 70 | Default max register pressure target |
| `unk_4FD2EC0` | `nv-remat-block-load-cost` | 10 | Cost assigned to load instructions |
| `unk_4FD3860` | `nv-remat-threshold-for-spec-reg` | 20 | Threshold for special register remat |
| `byte_4FD2E80` | `nv-dump-remat-block` | off | Debug dump toggle |
| `byte_4FD2DA0` | `nv-remat-check-internal-live` | off | Check internal liveness during MaxLive |
| `qword_4FD2C20` | `max-reg-kind` | 0 | Kind of max register pressure info |
| `qword_4FD2BE0` | `no-mi-remat` | (list) | Skip remat for named functions |
| `word_4FD32F0` | `load-remat` | on | Enable load rematerialization |
| `word_4FD3210` | `vasp-fix1` | off | VASP fix (volatile/addsp) |

**Algorithm pseudocode (`sub_2186D90`):**

```rust
fn nvptx_block_remat(MF: &mut MachineFunction) -> bool {
    // (A) INITIALIZATION
    let target = max_reg_override.unwrap_or(nv_remat_default_max_reg);  // default 70
    if MF.block_count() == 1 { return false; }
    if function_name in no_mi_remat_list {
        log("Skip machine-instruction rematerialization on {name}");
        return false;
    }

    // (B) LIVEOUT FREQUENCY COUNTING
    for bb in MF.blocks() {
        for reg in bb.live_out() {
            freq_map[reg] += 1;
        }
    }
    // Normalize: freq_pct = (100 * count) / num_blocks

    // (C) OUTER ITERATIVE LOOP
    let mut iteration = 0;
    let mut overall_changed = false;
    loop {
        iteration += 1;
        if iteration > nv_remat_max_times { break; }  // default 10

        // Phase 1: COMPUTE MAX-LIVE
        let max_live = sub_2186590(MF);  // scan all blocks
        log("Max-Live-Function({num_blocks}) = {max_live}");
        if target >= max_live { break; }  // no pressure problem

        let mut changed = false;
        // Phase 2: FOR EACH OVER-PRESSURE BLOCK
        for bb in blocks_where(pressure > target) {
            let excess = bb.pressure - target;

            // Phase 3: CLASSIFY LIVE-OUT REGISTERS
            let (pullable, non_pullable) = classify_liveout(bb);
            // sub_217E810 (MULTIDEF check) -- must have single unique def
            // sub_2181550 (recursive pullability, depth <= 50)
            log("Pullable: {pullable.len()}");

            // Phase 4: SECOND-CHANCE HEURISTIC (sub_2181870)
            if excess > pullable.len() && second_chance_list.not_empty() {
                second_chance_promote(&mut pullable, &mut non_pullable);
                // Re-evaluates rejected candidates with relaxed criteria
                // Uses visit-count mechanism to prevent infinite loops
                // Hash: h(regID) = 37 * regID, open-addressing
                log("ADD {n} candidates from second-chance");
            }

            log("Total Pullable before considering cost: {pullable.len()}");

            // Phase 5: COST ANALYSIS (sub_2183E30)
            let candidates = pullable.filter_map(|reg| {
                let cost = compute_remat_cost(reg);  // 0 = cannot remat
                (cost > 0).then(|| (reg, cost))
            });

            // Phase 6: SELECT BY COST-BENEFIT (cheapest first)
            candidates.sort_by_key(|(_, cost)| *cost);  // selection sort
            let mut final_list = vec![];
            for (reg, cost) in candidates {
                if cost > nv_remat_block_single_cost { break; } // default 10
                let width = if reg_class_size(reg) > 32 { 2 } else { 1 };
                final_list.push(reg);
                if final_list.len() >= excess { break; }
            }

            log("Really Final Pull-in: {final_list.len()} ({total_cost})");

            // Phase 7: EXECUTE REMATERIALIZATION
            for reg in &final_list {
                clear_from_liveout(bb, reg);            // sub_217F620
            }
            bb.pressure -= final_list.len();
            propagate_backward(bb, &final_list);         // sub_2185250
            // Clone defining instructions at use sites
            // sub_21810D0 replaces register references
            changed = true;
        }

        overall_changed |= changed;
        if !changed { break; }
    }

    // (D) DEAD INSTRUCTION REMOVAL -- cascading deletion
    remove_dead_instructions();  // sub_217DA10
    overall_changed
}
```

**MULTIDEF detection (`sub_217E810`):** Returns the defining instruction if the register has exactly one non-dead, non-debug definition. Rejects instructions with hazardous descriptor flags (`desc->flags & 0x3F80`), opcodes in the non-rematerializable set (memory ops 534-609, texture ops 680-681, atomics 817-832, barriers 2913-2918, surface ops 3281-3287, 3449-3454, large MMA blocks 4423-4447), and instructions with tied extra defs.

**Recursive pullability (`sub_2181550`):** Walks the operand chain up to depth 50, checking each operand register against the non-pullable set and the MULTIDEF oracle. All operands in the chain must be single-def, safe-opcode, and themselves pullable.

**Cost model:** `sub_2183E30` computes the clone cost of rematerializing a register. Load instructions cost `nv-remat-block-load-cost` (default 10). Instructions in loops are penalized by `nv-remat-block-loop-cost-factor` (default 20x). Double-wide registers (class size > 32) count as 2 for pressure and have 2x cost.

### Machine Register Pressure Analysis (`sub_21EAA00`) — MRPA

Registration: `sub_21EAA00` at `0x21EAA00`, pass name `"Register pressure analysis on Machine IRs"`, pass ID `"machine-rpa"`. Main analysis body: `sub_21EEB40` (68KB). Incremental updater: `sub_2E5A4E0` (48KB). Backend variant: `sub_1E00370` (78KB).

MRPA is NVIDIA's custom analysis pass that provides per-basic-block register pressure data. Unlike LLVM's stock `RegisterPressure` tracking (which is tightly coupled to the scheduler), MRPA is consumed by multiple clients: RP-aware MachineCSE, instruction scheduling, and the block rematerialization pass.

**Architecture:**

The MRPA system has two modes:
1. **Full recomputation** (`sub_21EEB40`): Walks every instruction in every basic block, tracking register births (defs) and deaths (last uses), recording the peak pressure per register class per block.
2. **Incremental update** (`sub_2E5A4E0`): When a single instruction is moved or deleted (e.g., by MachineCSE), MRPA updates the affected blocks' pressure without rescanning the entire function.

**Incremental update algorithm (`sub_2E5A4E0`):**

```rust
fn mrpa_incremental_update(context, bb, instruction_delta) {
    // DenseMap hash: (ptr >> 9) ^ (ptr >> 4)
    // Empty sentinel: -8, Tombstone: -16
    // Minimum 64 buckets, always power-of-2

    // 1. Build worklist of affected BBs via DFS
    let worklist = dfs_from(bb, context.visited_set);

    // 2. For each BB: create/update tracking entry
    for bb in worklist {
        let entry = context.pressure_map.get_or_insert(bb);

        // 3. Filter schedulable instructions via sub_2E501D0
        for mi in bb.instrs().filter(schedulable) {
            // 4. For each virtual register operand (40-byte entries):
            for operand in mi.operands() {
                sub_2EBEF70(operand);  // find existing rename mapping
                sub_2EBEE10(operand);  // query register info
                sub_2EBE820(operand);  // attempt rename if profitable
                sub_2EBF120(operand);  // free old register after rename
            }
            // 5. Check register class constraints via sub_E922F0
            // 6. Validate pressure feasibility via sub_2E4F9C0
        }
        // 7. Erase unprofitable instructions via sub_2E88E20
    }
}
```

**Verification:** When `verify-update-mcse` is enabled (`qword_501F8A8`, default OFF), MRPA runs a full recomputation after every incremental update and compares results. Mismatch triggers: `"Incorrect RP info from incremental MRPA update"` via `sub_C64ED0`. The `print-verify` knob (`qword_501F7C8`) controls whether detailed per-register-class diagnostic output is printed on mismatch.

**Diagnostic output (`sub_21E9A60`):** The companion pass `extra-machineinstr-printer` at `sub_21E9E80` prints: `"Max Live RRegs: {n}\tPRegs: {m}\nFunction Size: {s}"` for each function, providing per-function register pressure statistics for tuning.

### LDG Transform (`sub_21F2780`) — Read-Only Data Cache Load Transformation

Registration: `sub_21F2780` at `0x21F2780`, pass name `"Ldg Transformation"`, pass ID `"ldgxform"`. Transformation body: `sub_21F2C80` (19KB). Vector splitting engine: `sub_21F3A20` (44KB).

This pass transforms qualifying global memory loads into `ld.global.nc` (LDG) instructions, routing them through the read-only texture cache (L1 on Kepler+, unified L1/tex on Maxwell+). The transformation is profitable for read-only data because the texture cache has separate bandwidth from the L1 data cache, effectively doubling memory throughput for qualifying loads.

**Algorithm:**

```rust
fn ldgxform(MF: &mut MachineFunction) -> bool {
    let mut changed = false;
    for mi in MF.all_instrs() {
        if !is_global_load(mi) { continue; }
        if is_volatile(mi) { continue; }
        if !pointer_is_readonly(mi.address_operand()) { continue; }

        // Replace ld.global with ld.global.nc (LDG)
        mi.set_opcode(ldg_variant(mi.opcode()));

        // Split wide loads if necessary
        if load_width(mi) > hardware_max_ldg_width() {
            // sub_21F2C80: LDG split transformation
            // Tags: ".ldgsplit", ".load", ".ldgsplitinsert"
            let (lo, hi) = split_wide_load(mi);
            // Insert: lo = ldg.64 [addr]
            //         hi = ldg.64 [addr + 8]
            //         result = INSERT_SUBREG lo, hi
            changed = true;
        }
        changed = true;
    }
    changed
}
```

**Vector splitting (`sub_21F3A20`, 44KB):** This is the third-largest function in the 0x21F range. NVPTX supports limited native vector widths (typically `.v2` and `.v4` of 32-bit elements). When wider vectors (e.g., `v8f32`, `v16f16`) appear, this engine splits them into legal widths. Operations handled:
- `vecBitCast`: bitcast between vector types
- `splitVec`: split a vector into sub-vectors
- `extractSplitVec` / `insertSplitVec`: element access on split vectors
- `splitVecGEP`: GEP computation on split vector elements

The split width depends on `TargetOpt.HasLDG` (stored at target options offset 5, extracted from `p2h-01` analysis). When LDG is available, 128-bit loads (`LDG.128`) are preferred, resulting in `.v4.b32` patterns.

### NVPTXMem2Reg (`sub_21F9920`) — Machine-Level Mem2Reg

Registration: `sub_21F9920` at `0x21F9920`, pass name `"Mem2Reg on Machine Instructions to remove local stack objects"`, pass ID `"nvptx-mem2reg"`. Main body: `sub_21FA880` (22KB), engine: `sub_21FC920` (33KB). Controlled by `byte_4FD25C0` (inverted by `nv-disable-mem2reg`, default: enabled).

Standard LLVM `mem2reg` operates on LLVM IR `alloca` instructions. This NVIDIA-custom pass operates on `MachineInstr` — specifically on `ld.local` / `st.local` pairs that access `__local_depot` frame slots. After register allocation, some values that were spilled to `.local` memory can be promoted back to virtual registers if their access pattern is simple enough (single def, multiple uses, no aliasing stores).

**Algorithm:**

```rust
fn nvptx_machine_mem2reg(MF: &mut MachineFunction) -> bool {
    if nv_disable_mem2reg { return false; }  // byte_4FD25C0

    let mut changed = false;
    for frame_idx in MF.frame_info().stack_objects() {
        if !is_local_depot_slot(frame_idx) { continue; }
        // Collect all loads and stores to this frame slot
        let stores = find_stores_to(MF, frame_idx);
        let loads = find_loads_from(MF, frame_idx);

        if stores.len() != 1 { continue; }  // must be single-def
        let store = stores[0];
        let src_reg = store.source_register();

        // Check: no aliasing stores between def and uses
        // Check: store dominates all loads
        if !dominates_all(store, &loads) { continue; }

        // Promote: replace all ld.local with the source register
        for load in &loads {
            replace_load_with_reg(load, src_reg);
            load.erase_from_parent();
        }
        store.erase_from_parent();
        MF.frame_info().remove_object(frame_idx);
        changed = true;
    }
    changed
}
```

This pass is positioned in `addPostRegAlloc()`, meaning it runs after the greedy register allocator has already assigned slots. It acts as a cleanup: register allocation may have conservatively spilled values that turn out to be unnecessary after coalescing and copy propagation eliminate intermediate uses.

### GenericToNVVM (`sub_215DC20`) — Address Space Normalization

Registration: `sub_215DC20` at `0x215DC20`, pass name `"Ensure that the global variables are in the global address space"`, pass ID `"generic-to-nvvm"`. Pass descriptor: 80-byte allocation. Factory: `sub_215D530` (allocates 320-byte state with two 128-bucket DenseMaps). New PM variant: `sub_305ED20`.

CUDA and LLVM IR use address space 0 (generic) as the default for globals, but NVPTX requires globals in address space 1. This pass rewrites every `GlobalVariable` in address space 0 to address space 1, inserting `addrspacecast` instructions at all use sites.

**Algorithm:**

```rust
fn generic_to_nvvm(M: &mut Module) -> bool {
    let mut gv_map = DenseMap::new(128);     // old -> new Value mapping
    let mut const_map = DenseMap::new(128);  // old -> new Constant mapping

    for gv in M.globals().filter(|g| g.address_space() == 0) {
        // 1. Clone to address space 1
        let new_gv = GlobalVariable::new(
            gv.value_type(), gv.is_constant(), gv.linkage(),
            gv.initializer(), gv.name(), /*addrspace=*/ 1
        );
        new_gv.set_alignment(gv.alignment());

        // 2. Insert addrspacecast(1 -> 0) at each use
        let cast = ConstantExpr::addrspace_cast(new_gv, gv.type());

        // 3. Replace all uses
        gv.replace_all_uses_with(cast);

        // 4. Track in map and erase original
        gv_map.insert(gv, new_gv);
        gv.erase_from_parent();
    }

    // Cleanup: sub_215D780 iterates gv_map, properly ref-counting Values
    cleanup_gv_map(&gv_map);
    !gv_map.is_empty()
}
```

### NVPTXProxyRegErasure (`sub_21DA810`) — Redundant cvta.to.local Removal

Registration: `sub_21DA810` at `0x21DA810`, pass name `"NVPTX optimize redundant cvta.to.local instruction"`.

This late post-RA pass removes `cvta.to.local` instructions that are left over from address space lowering. After frame layout is complete, local memory addresses are known, and `cvta.to.local` (which converts a generic pointer to a `.local` pointer) is redundant when the address is already known to be in `.local` space. The pass is simple: scan for `cvta.to.local` MachineInstrs, verify the source is already a `.local` address, replace uses with the source operand, delete the `cvta`.

### NVPTXAssignValidGlobalNames (`sub_21BCD80`) — PTX Name Sanitization

Registration: `sub_21BCD80` at `0x21BCD80`, pass name `"Assign valid PTX names to globals"`, pass ID `"nvptx-assign-valid-global-names"`.

PTX has stricter naming rules than LLVM IR. Characters like `@`, `$`, `.` (in certain positions), and Unicode are illegal in PTX identifiers. This pass walks all `GlobalValue`s in the module and replaces illegal characters with safe alternatives (typically `_`). It also handles name demangling artifacts and ensures the final names are unique after sanitization.

### NVPTXImageOptimizer (`sub_21BCF10`) — Texture/Surface Optimization

Registration: `sub_21BCF10` at `0x21BCF10`, pass name `"NVPTX Image Optimizer"`. Type validation helper: `sub_21DD1A0` (16KB).

This pre-emission pass optimizes texture and surface access patterns. It validates image type consistency for `tex`, `suld`, `sust`, and `suq` operations, emitting errors for mismatches: `"Invalid image type in .tex"`, `"Invalid image type in .suld"`, `"Invalid image type in suq."`, `"Invalid image type in .sust"`. The pass coalesces related texture operations when they access the same texture handle with compatible coordinates and can be merged into wider vector fetches.

### NVPTXReplaceImageHandles (`sub_21DBEA0`) — Image Handle Lowering

Registration: `sub_21DBEA0` at `0x21DBEA0`, pass name `"NVPTX Replace Image Handles"`.

Replaces IR-level texture/surface handle references (which are LLVM `Value` pointers to `@texture_handle` globals) with PTX-level `.tex` / `.surf` declarations and integer handle indices. This is a pre-emission pass that bridges the gap between LLVM IR's opaque handle model and PTX's explicit texture declaration model.

### AllocaHoisting (`sub_21BC7D0`) — Entry Block Alloca Hoisting

Registration: `sub_21BC7D0` at `0x21BC7D0`, pass name `"Hoisting alloca instructions in non-entry blocks to the entry block"`, pass ID `"alloca-hoisting"`. Registration helper: `sub_21BC5A0`.

PTX requires that all local memory declarations be hoisted to the function entry. This pass scans all basic blocks for `alloca` instructions and moves them to the entry block. This enables the frame layout pass (`PrologEpilogInserter`) to assign fixed offsets to all stack objects — a requirement because PTX emits `.local .align N .b8 __local_depotX[SIZE]` at the function prologue and all local accesses are indexed from this single base.

### ParamOpt (`sub_2203290`) — Parameter Load Optimization

Registration: `sub_2203290` at `0x2203290`, pass name `"Optimize NVPTX ld.param"`, pass ID `"param-opt"`.

NVPTX-custom pass that optimizes `ld.param` instructions generated during kernel argument passing. When a kernel parameter is loaded multiple times (common when the same argument is used in different basic blocks), this pass eliminates redundant loads by propagating the first load's result to subsequent uses. Related knob: `remat-load-param` ("Support remating const ld.param that are not exposed in NVVM IR").

### NVPTXTruncOpts (`sub_22058E0`) — i16 Truncation Optimization

Registration: `sub_22058E0` at `0x22058E0`, pass name `"Optimize redundant ANDb16ri instrunctions"` [sic], pass ID `"nvptx-trunc-opts"`.

When LLVM lowers `trunc i32 to i16` operations, the NVPTX backend emits an `AND.b16` with mask `0xFFFF` to ensure the high bits are zero. In many cases this AND is redundant — the producing instruction already guarantees a 16-bit result. This pass pattern-matches `ANDb16ri` instructions with the `0xFFFF` immediate and removes them when the source provably fits in 16 bits.

### RP-Aware MachineCSE (NVIDIA-Modified `machine-cse`)

Stock LLVM `MachineCSE` eliminates redundant machine instructions by matching instruction patterns within dominance regions. NVIDIA adds three extensions via `ctor_302_0` (`0x4FEB70`, 7.8KB, 14 strings):

**RP-aware CSE (`rp-aware-mcse`):** Before eliminating a common subexpression, queries MRPA (`sub_2E5A4E0`) for the current register pressure. If eliminating the CSE candidate would increase pressure beyond the target (because the shared result must stay live longer), the CSE is suppressed. This prevents the classic GPU problem where CSE reduces instruction count but increases register pressure, reducing occupancy.

**Predicate-aware CSE (`pred-aware-mcse`):** Extends RP awareness to predicate registers (PTX `%p` class). Predicate registers are a scarce resource (maximum 7 per thread on most architectures), so predicate pressure is tracked separately from general-purpose register pressure.

**Copy-prop CSE (`copy-prop-mcse`):** Embeds copy propagation within the CSE framework. When CSE eliminates an instruction, the resulting `COPY` instructions can often be propagated immediately rather than waiting for the separate `MachineCopyPropagation` pass.

**Incremental MRPA integration:** The MCSE pass uses `qword_501F988` (`incremental-update-mcse`, default ON) to incrementally update MRPA as CSE decisions are made, avoiding full recomputation per CSE candidate.

### MachinePipeliner (SMS) Detail

The Swing Modulo Scheduler at `sub_3563190` performs software pipelining — overlapping successive loop iterations to hide latency. It operates on a single loop body at the MachineInstr level:

1. **DAG construction**: builds a data dependency graph with `sub_2F97F60`, computes latencies via `sub_3559990`, adds edges via `sub_3542B20`.
2. **MII computation**: `RecMII` (recurrence-based) via `sub_354CBB0`, `ResMII` (resource-based) via `sub_35449F0`. `MII = max(RecMII, ResMII)`.
3. **Early exits**: MII == 0 is invalid; MII > `SwpMaxMii` (default 27, `-pipeliner-max-mii`) aborts.
4. **II search**: starts at MII, tries up to `pipeliner-ii-search-range` (default 10, `qword_503E428`) consecutive II values. First valid schedule wins.
5. **Schedule construction**: ASAP via `sub_354BFF0`, ALAP via `sub_354BFF0`, topological sort, core SMS node placement via `sub_354C3A0`, then finalization.
6. **Kernel generation**: Three code generation backends selected by priority — annotation-only (`pipeliner-annotate-for-testing`), MVE-based (`pipeliner-mve-cg`, default enabled), and experimental peeling (`pipeliner-experimental-cg`).

The pipeliner stores its schedule context as a 616-byte (`0x268`) structure with four SmallVectors and per-BB data at 256-byte stride. Maximum pipeline stages: `SwpMaxStages` (default 3, `-pipeliner-max-stages`).

**Core scheduling pipeline (10 sequential calls):**

| Step | Function | Purpose |
|---|---|---|
| 1 | `sub_35476E0` | DAG construction / dependency analysis |
| 2 | `sub_35523F0` | Recurrence detection / RecMII computation |
| 3 | `sub_35546F0` | Resource usage / ResMII computation |
| 4 | `sub_3543340` | `MII = max(RecMII, ResMII)` finalization |
| 5 | `sub_35630A0` | Node ordering / priority assignment |
| 6 | `sub_35568E0` | Schedule table initialization |
| 7 | `sub_35433F0` | Pre-scheduling transforms |
| 8 | `sub_3557A10` | Instruction ordering/selection (heuristic) |
| 9 | `sub_354A760` | Schedule finalization / modulo expansion |
| 10 | `sub_355F610` | `ScheduleDAGMILive` integration (64KB) |

**Instruction selection heuristic (`sub_3557A10`):**
Priority ordering: (1) deeper instructions first (offset 240 = latency/depth), (2) target priority table at `a1+3944` (16-byte entries: `[start, end, priority, window_width]`), (3) narrower schedule windows first. Latency recomputation via `sub_2F8F5D0` during comparison.

**Error messages:**
- `"Invalid Minimal Initiation Interval: 0"` — MII computation returned zero
- `"Minimal Initiation Interval too large: MII > SwpMaxMii. Refer to -pipeliner-max-mii."` — loop is too complex
- `"Unable to find schedule"` — no valid II found within search range
- `"No need to pipeline - no overlapped iterations in schedule."` — `numStages == 0`
- `"Too many stages in schedule: numStages > SwpMaxStages. Refer to -pipeliner-max-stages."` — pipeline depth exceeded

### PrologEpilogInserter (`sub_35B1110`) — .local Frame Layout

Address: `sub_35B1110` (68KB, 2388 decompiled lines). Stack frame: `0x490` bytes of local state. This is NVIDIA's monolithic PEI for PTX. Unlike a traditional PEI that emits push/pop sequences and adjusts `%rsp`, this one computes `.local` memory frame offsets.

**10-phase structure:**

| Phase | Lines | Description |
|---|---|---|
| 1 | 443-490 | Target/subtarget retrieval, initial setup |
| 2 | 491-566 | Callee-saved register determination |
| 3 | 567-730 | Pre-pass: collect fixed objects from frame info |
| 4 | 733-1070 | **Stack object offset assignment** (main layout engine) |
| 5 | 1078-1600 | General local variable layout |
| 6 | 1688-1795 | Frame-pointer stack area |
| 7 | 1803-1872 | Prolog/epilog instruction insertion per BB |
| 8 | 1873-2132 | Scavenger / frame-index elimination |
| 9 | 2270-2304 | Stack-size warning & diagnostic reporting |
| 10 | 2305-2388 | Cleanup & deallocation |

**Frame object record (40 bytes):**

| Offset | Size | Field |
|---|---|---|
| +0 | 8 | Byte offset in `.local` memory (assigned by PEI) |
| +8 | 8 | Object size in bytes |
| +16 | 1 | Alignment (log2) |
| +20 | 1 | isDead flag (skip if set) |
| +32 | 1 | isSpillSlot flag |
| +36 | 1 | Category byte (0/1/2/3) |

**Stack layout algorithm (Phase 4):**

```rust
fn assign_frame_offsets(MF: &MachineFunction, frame: &mut FrameInfo) {
    let grows_neg = frame.stack_direction == 1;
    let mut offset = frame.initial_offset;
    let mut max_align = frame.max_alignment;

    // Fixed objects first
    for obj in frame.fixed_objects() {
        if obj.is_dead { continue; }
        let align = 1 << obj.log2_align;
        offset = align_to(offset, align);
        obj.offset = if grows_neg { -offset } else { offset };
        offset += obj.size;
        max_align = max(max_align, align);
    }

    // Callee-saved register region
    for csr in frame.callee_saved_range() {
        if csr.is_dead || csr.size == -1 { continue; }
        let align = 1 << csr.log2_align;
        offset = align_to(offset, align);
        csr.offset = if grows_neg { -offset } else { offset };
        offset += csr.size;
    }

    // General locals: three category buckets, each via sub_35B0830
    for category in [1, 2, 3] {
        for obj in frame.objects_of_category(category) {
            let align = 1 << obj.log2_align;
            offset = align_to(offset, align);
            obj.offset = if grows_neg { -offset } else { offset };
            offset += obj.size;
        }
    }

    frame.stack_size = offset;
}
```

The final PTX emission (`sub_2158E80`) uses these offsets to emit: `.local .align N .b8 __local_depotX[SIZE];` at the function prologue, and `ld.local` / `st.local` instructions reference `[%SPL + offset]` where `%SPL` is the local stack pointer register.

### ScheduleDAGMILive (`sub_355F610`) — Post-RA Instruction Ordering

Address: `sub_355F610` (64KB). This is the post-RA machine instruction scheduler, consuming either the pipeliner's output or standalone scheduling regions.

**Data structures:**
- `SUnit` (Scheduling Unit): 88 bytes per instruction
- Instruction-to-node hash map: 632-byte entries
- RP tracking structure: 112 bytes (offsets 32-48: per-class pressure current, offsets 56-72: per-class pressure limits)

**Scheduling flow:**
1. Initialize RP tracking via `sub_3551AB0` (if `pipeliner-register-pressure` is set)
2. Set per-class pressure defaults via `sub_2F60A40`
3. Walk BB instruction list, build instruction-to-node hash map (632-byte entries)
4. Compute ASAP via `sub_354BFF0` -> earliest cycle per instruction
5. Compute ALAP via `sub_354BFF0` -> latest cycle per instruction
6. Place instructions via `sub_354C3A0` (returns success/failure)
7. Calculate stage count: `(lastCycle - firstCycle) / II`
8. Verify placement via `sub_355C7C0`
9. Build stage descriptors via `sub_355D7E0` (80 bytes per stage)

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
| `incremental-update-mcse` | bool | true | Incrementally update MRPA during MCSE |
| `verify-update-mcse` | bool | false | Debug: verify incremental MRPA updates against full recomputation |
| `print-verify` | bool | false | Debug: print detailed RP mismatch diagnostic |
| `cta-reconfig-aware-mrpa` | bool | (varies) | CTA reconfiguration aware machine RP analysis |

### NVPTXBlockRemat Knobs

| Knob | Type | Default | Effect |
|---|---|---|---|
| `nv-remat-block` | int | 14 | Bitmask controlling remat modes (bits 0-3) |
| `nv-remat-max-times` | int | 10 | Max iterations of the outer remat loop |
| `nv-remat-block-single-cost` | int | 10 | Max cost per single live value pull-in |
| `nv-remat-block-map-size-limit` | int | 6 | Map size limit for single pull-in |
| `nv-remat-block-max-cost` | int | 100 | Max total clone cost per live value reduction |
| `nv-remat-block-liveout-min-percentage` | int | 70 | Min liveout % for special consideration |
| `nv-remat-block-loop-cost-factor` | int | 20 | Loop cost multiplier |
| `nv-remat-default-max-reg` | int | 70 | Default max register pressure target |
| `nv-remat-block-load-cost` | int | 10 | Cost assigned to load instructions |
| `nv-remat-threshold-for-spec-reg` | int | 20 | Threshold for special register remat |
| `nv-dump-remat-block` | bool | false | Debug dump toggle |
| `load-remat` | bool | true | Enable load rematerialization |

### Pipeliner Knobs

| Knob | Type | Default | Effect |
|---|---|---|---|
| `enable-pipeliner` | bool | true | Enable the MachinePipeliner pass |
| `pipeliner-max-mii` | int | 27 | Maximum Minimal Initiation Interval before abort |
| `pipeliner-max-stages` | int | 3 | Maximum pipeline stages |
| `pipeliner-ii-search-range` | int | 10 | Number of consecutive II values to try |
| `pipeliner-register-pressure` | bool | false | Enable RP tracking during pipelining |
| `pipeliner-register-pressure-margin` | int | 5 | RP margin before pipeliner backs off |
| `pipeliner-ignore-recmii` | bool | false | Zero out RecMII, use only ResMII |
| `pipeliner-annotate-for-testing` | bool | false | Annotate schedule without modifying code |
| `pipeliner-experimental-cg` | bool | false | Use experimental peeling code generator |
| `pipeliner-mve-cg` | bool | true | Use MVE code generator (default path) |
| `outliner-benefit-threshold` | int | 1 | Minimum size in bytes for outlining candidate |

### Register Pressure Target Knobs

| Knob | Type | Default | Effect |
|---|---|---|---|
| `reg-target-adjust` | int | 0 | Adjust register pressure target (-10 to +10) |
| `pred-target-adjust` | int | 0 | Adjust predicate register pressure target (-10 to +10) |
| `fca-size` | int | 8 | Max size of first-class aggregates in bytes |
| `remat-load-param` | bool | (varies) | Support remating const `ld.param` not exposed in NVVM IR |
| `cta-reconfig-aware-rpa` | bool | (varies) | CTA reconfiguration aware register pressure analysis |

## Function Address Map

| Address | Size | Function | Role |
|---|---|---|---|
| `sub_215DC20` | — | GenericToNVVM registration | Address space normalization |
| `sub_215D530` | 320B state | GenericToNVVM factory | Allocates pass state with 2 DenseMaps |
| `sub_215D780` | — | GenericToNVVM cleanup | GVMap iteration and Value ref-counting |
| `sub_2166D20` | 1.5KB | addISelPasses | Pre-ISel pass configuration |
| `sub_2166ED0` | 1.6KB | addPreRegAlloc | Pre-RA pass configuration |
| `sub_21668D0` | 1.2KB | addPostRegAlloc | Post-RA pass configuration |
| `sub_217D300` | — | BlockRemat pass name | `"NVPTX Machine Block Level Rematerialization"` |
| `sub_217DBF0` | — | BlockRemat registration | `"nvptx-remat-block"` |
| `sub_217E810` | 5.2KB | MULTIDEF detection | Single-def checker with opcode exclusion table |
| `sub_2181550` | ~3KB | Recursive pullability | Depth-limited chain validation (depth <= 50) |
| `sub_2181870` | 19KB | Second-chance heuristic | Re-evaluates rejected remat candidates |
| `sub_2183E30` | — | Cost evaluator | Computes clone cost for rematerialization |
| `sub_2184890` | 12KB | Remat allocation helper | Simulates pressure after remat |
| `sub_2185250` | 17KB | Liveness propagation | Core instruction cloning/replacement engine |
| `sub_2186590` | — | Max-live computation | Per-block pressure scan |
| `sub_2186D90` | 47KB | **BlockRemat main engine** | Iterative pull-in algorithm (1742 lines) |
| `sub_21810D0` | 9.4KB | Instruction replacement | Replaces register uses after remat |
| `sub_21BC5A0` | — | AllocaHoisting name | Pass name registration |
| `sub_21BC7D0` | — | AllocaHoisting registration | `"alloca-hoisting"` |
| `sub_21BCD80` | — | ValidGlobalNames registration | `"nvptx-assign-valid-global-names"` |
| `sub_21BCF10` | — | ImageOptimizer registration | `"NVPTX Image Optimizer"` |
| `sub_21DA810` | — | ProxyRegErasure | Redundant `cvta.to.local` removal |
| `sub_21DB090` | — | NVPTXPeephole registration | `"nvptx-peephole"` |
| `sub_21DB5F0` | — | NVPTXPrologEpilog registration | `"NVPTX Prolog Epilog Pass"` |
| `sub_21DBEA0` | — | ReplaceImageHandles registration | `"NVPTX Replace Image Handles"` |
| `sub_21DD1A0` | 16KB | Image type validation | `tex`/`suld`/`sust`/`suq` type checking |
| `sub_21E9A60` | 4.9KB | RP stats printer | `"Max Live RRegs: "` / `"PRegs: "` |
| `sub_21E9E80` | — | ExtraMachineInstrPrinter registration | `"extra-machineinstr-printer"` |
| `sub_21EAA00` | — | MRPA registration | `"machine-rpa"` |
| `sub_21EEB40` | 68KB | MRPA full recomputation | Per-BB pressure computation |
| `sub_21F2780` | — | LdgXform registration | `"ldgxform"` |
| `sub_21F2C80` | 19KB | LDG split body | `.ldgsplit` / `.ldgsplitinsert` |
| `sub_21F3A20` | 44KB | Vector splitting engine | `splitVec` / `vecBitCast` / `extractSplitVec` |
| `sub_21F9920` | — | NVPTXMem2Reg registration | `"nvptx-mem2reg"` |
| `sub_21FA880` | 22KB | Mem2Reg body | Machine-level mem2reg driver |
| `sub_21FC920` | 33KB | Mem2Reg engine | Promotion/replacement logic |
| `sub_2200150` | 78KB | DAGToDAG ISel main | Hash-table pattern matching (`h = (37*idx) & (size-1)`) |
| `sub_2203290` | — | ParamOpt registration | `"param-opt"` |
| `sub_2204E60` | — | Redundant move elim | `"Remove redundant moves"` |
| `sub_22058E0` | — | TruncOpts registration | `"nvptx-trunc-opts"` |
| `sub_2E5A4E0` | 48KB | MRPA incremental updater | Incremental RP tracking for MCSE |
| `sub_1E00370` | 78KB | MRPA backend variant | Alternative RP tracker |
| `sub_35B1110` | 68KB | PrologEpilogInserter | `.local` frame layout (2388 lines) |
| `sub_3563190` | 58KB | MachinePipeliner | Swing Modulo Scheduling |
| `sub_355F610` | 64KB | ScheduleDAGMILive | Post-RA instruction ordering |
| `sub_3557A10` | — | SMS instruction selection | Scheduling heuristic |

## Global Variable Reference

| Variable | Type | Default | Role |
|---|---|---|---|
| `byte_4FD1980` | byte | (opt-level) | MachineLICM enable flag |
| `byte_4FD18A0` | byte | (opt-level) | MachineCSE enable flag |
| `byte_4FD1A60` | byte | (opt-level) | MachineSink enable flag |
| `byte_4FD25C0` | byte | (opt-level) | nvptx-mem2reg enable |
| `byte_4FD2160` | byte | — | Extra ISel pass enable |
| `byte_4FD2E80` | byte | off | nv-dump-remat-block |
| `dword_4FD26A0` | dword | — | Scheduling mode (1 = simple, else = full) |
| `dword_4FD3740` | dword | 10 | nv-remat-max-times |
| `dword_4FD3820` | dword | 14 | nv-remat-block mode bitmask |
| `dword_4FD33C0` | dword | 70 | nv-remat-default-max-reg (global) |
| `qword_501F988` | qword | 1 | incremental-update-mcse |
| `qword_501F8A8` | qword | 0 | verify-update-mcse |
| `qword_501F7C8` | qword | 0 | print-verify |

## Cross-References

- [SelectionDAG](./selectiondag.md) — the ISel pass that produces MachineInstrs consumed by machine passes
- [Register Allocation](./register-allocation.md) — pressure-driven greedy allocator with NVPTX register classes
- [Register Coalescing](./register-coalescing.md) — NVPTX-custom copy elimination framework
- [PrologEpilogInserter & Frame Layout](./prolog-epilog.md) — `.local` memory frame computation
- [MachineOutliner](./machine-outliner.md) — suffix-tree-based code size reduction
- [Block Placement](./block-placement.md) — profile-guided basic block ordering
- [Instruction Scheduling](./scheduling.md) — MRPA, MachinePipeliner, ScheduleDAGMILive
- [Rematerialization](../passes/rematerialization.md) — NVIDIA's custom machine-level remat
- [NVVM Peephole](../passes/nvvm-peephole.md) — IR-level NVVM peephole (distinct from machine-level `nvptx-peephole`)
- [AsmPrinter & PTX Emission](../infra/asmprinter.md) — final pass: MachineInstr to PTX text
- [Code Generation](../pipeline/codegen.md) — pipeline overview including ISel and DAG infrastructure
- [StructurizeCFG](./structurizecfg.md) — mandatory CFG structurization (runs before ISel, feeds machine passes)
- [Hash Infrastructure](../infra/hash-infrastructure.md) — DenseMap hash function `(ptr >> 9) ^ (ptr >> 4)` used throughout MRPA
- [Register Classes](../reference/register-classes.md) — NVPTX register class definitions consumed by all machine passes
