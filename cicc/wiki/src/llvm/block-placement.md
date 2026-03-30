# MachineBlockPlacement for GPU

MachineBlockPlacement decides the physical ordering of basic blocks in a MachineFunction. On CPU, it is primarily an I-cache optimization. On GPU, block ordering has deeper consequences: PTX is a structured ISA where every taken branch stalls the SM instruction fetch pipeline, warp divergence must reconverge at post-dominators, and instruction cache capacity is measured in tens of kilobytes per SM partition. cicc carries two separate instances of this pass -- a stock LLVM copy for internal use and an NVPTX-pipeline copy at `sub_3521FF0` that participates in GPU-specific analysis. The NVPTX instance queries a divergence flag on the MachineFunction to decide whether tail duplication is profitable, and adds an alternative layout proposal path (`sub_34BEDF0` / `sub_34C7080`) that is absent from upstream LLVM.

## Key Facts

| Property | Value |
|---|---|
| Entry point | `sub_3521FF0` (82 KB decompiled, 2435 lines) |
| Pass name | `"Branch Probability Basic Block Placement"` |
| Pass ID | `"block-placement"` |
| Registration (NVPTX) | `sub_350FE30` (pass), `sub_350FEE0` (stats) |
| Registration (generic) | `sub_1DE8060` (pass), `sub_1DE8500` (stats) |
| Stats pass ID | `"block-placement-stats"`, callback `sub_3517680` |
| Knob constructor | `ctor_671_0` at `0x5A0470` |
| Required analyses | MachineBlockFrequencyInfo, MachineBranchProbabilityInfo, MachinePostDominatorTree, MachineLoopInfo, TargetPassConfig |

## Why Block Placement Matters on GPU

Three properties of GPU execution make block ordering non-trivial.

**Instruction fetch pipeline.** GPU SMs fetch instructions sequentially. A taken branch introduces a fetch bubble -- the warp scheduler cannot issue from the new target until the instruction cache services the request. Every fall-through edge is free; every taken branch costs at least one cycle of fetch latency. The `misfetch-cost` (default 1) and `jump-inst-cost` (default 1) knobs model this cost. Maximizing fall-through sequences directly reduces warp stall cycles at branch points.

**Instruction cache pressure.** GPU instruction caches are small (typically 32-128 KB per SM partition). Code duplication through tail-dup increases I-cache working set. The `tail-dup-placement-penalty` (default 2%) penalizes code copies that improve fall-through at the expense of I-cache pressure. The ext-TSP model, when enabled, explicitly optimizes for I-cache utilization by modeling forward/backward reference distances.

**Warp divergence.** When a branch is divergent (different lanes take different paths), all paths must execute serially, and the warp reconverges at the post-dominator. Block ordering cannot eliminate the divergence cost, but it determines which side of the branch falls through vs. takes a jump. The divergence flag at `MF+8+688 bit 0` gates whether tail duplication is even attempted: duplicating a tail block that sits below a divergent branch wastes code size because divergent warps execute both paths regardless of which one falls through.

## Pass Object Layout

The pass object at `a1` is populated during `runOnMachineFunction`:

| Offset | Type | Content |
|---|---|---|
| `+488` | ptr | Loop chain working data (cleared by `sub_35142F0`) |
| `+520` | `MachineFunction*` | Current function being processed |
| `+528` | ptr | `MachineBlockFrequencyInfo*` (adjusted +169 from raw analysis pointer) |
| `+536` | ptr | `MachineBranchProbabilityInfo*` (40-byte struct at +200) |
| `+544` | ptr | `MachinePostDominatorTree*` (+200) |
| `+552` | u64 | Working state (cleared to 0) |
| `+560` | ptr | `TargetInstrInfo*` (nullptr if default vtable) |
| `+568` | ptr | `TargetRegisterInfo*` (nullptr if default vtable) |
| `+576` | ptr | `TailDuplicator*` (from `unk_50209DC` analysis, +200) |
| `+584` | ptr | `MachineLoopInfo*` |
| `+592` | ptr | `TargetPassConfig*` |
| `+600` | inline | Chain-builder state (initialized by `sub_2FD5DC0`) |
| `+776` | u64 | Profile-derived hot threshold |
| `+784` | i32 | Tail-dup threshold (2 or 4) |
| `+788` | bool | Profile count was explicitly provided |
| `+792` | ptr | Bump allocator base (for chain node allocation) |
| `+800` | u64 | Bump allocator capacity |
| `+872` | u64 | Bump allocator total allocation counter |
| `+888` | struct | Chain-map (BB-to-chain DenseMap, queried via `sub_3515040`) |

Chain nodes are 64 bytes each, allocated from the bump allocator:

```
struct ChainNode {          // 64 bytes
    MachineBasicBlock** bb_array;   // +0:  pointer to BB array (initially +16)
    uint32_t count;                 // +8:  number of BBs in chain
    uint32_t capacity;              // +12: capacity (initial: 1)
    MachineBasicBlock* inline_bb;   // +16: inline storage for single-BB chain
    uint8_t  padding[24];           // +24: space for up to 3 more inline BBs
    void*    chain_map;             // +48: pointer to parent chain-map
    uint64_t flags;                 // +56: chain flags
};
```

## Algorithm Overview

The entry point `sub_3521FF0` dispatches to one of two layout algorithms: the standard chain-based placement, or the ext-TSP layout when explicitly enabled. The overall flow:

```
runOnMachineFunction(MF):
    if MF.empty(): return 0

    // Fetch analyses
    MBFI  = getAnalysis<MachineBlockFrequencyInfo>()
    MBPI  = getAnalysis<MachineBranchProbabilityInfo>()
    MPDT  = getAnalysis<MachinePostDominatorTree>()
    MLI   = getAnalysis<MachineLoopInfo>()
    TPC   = getAnalysis<TargetPassConfig>()
    TII   = MF.getSubtarget().getInstrInfo()
    TRI   = MF.getSubtarget().getRegisterInfo()

    // Compute tail-dup threshold
    threshold = computeTailDupThreshold(optLevel, TII)

    // Decide layout algorithm
    if enable-ext-tsp-block-placement AND MF.size() fits:
        applyExtTsp(MF)
    else:
        buildChains(MF)           // sub_3521900
        tailDupPlacement(MF)      // sub_35185B0 (if enabled + not divergent)
        tryAlternativeLayout(MF)  // sub_34BEDF0 + sub_34C7080 (NVIDIA addition)

    // Post-placement
    optimizeBranches()            // flip branches for fall-through
    alignBlocks()                 // sub_3516980
    cleanup()
    return 1
```

### Chain-Based Placement (Standard Path)

`sub_3521900` (`buildChains`) is the workhorse. It operates in four steps:

**Step 1 -- Initial chain construction.** For every BB in the MachineFunction, allocate a 64-byte chain node from the bump allocator and register it in the chain-map. Then attempt to extend the chain by querying `TII->analyzeBranch()` (vtable+344) to identify the fall-through successor. If the successor is valid (not already in a different chain, verified by `sub_2E32580`), append it to the current chain and continue walking.

**Step 2 -- Loop chain merging.** Read MachinePostDominatorTree's sorted loop list. For each loop from innermost outward, call `sub_351EBB0` (`buildLoopChains`), which merges chains within the loop body to form a contiguous sequence. This includes loop rotation via `sub_351C710` (`rotateLoop`).

**Step 3 -- Global successor ordering.** Call `sub_35157A0` (`selectBestSuccessor`) for each BB to find the globally best successor chain ordering. Then `sub_351D700` (`buildChainForBlock`) performs a greedy walk from the function entry, building the top-level chain.

**Step 4 -- Commit.** Walk the final chain's BB array and splice each BB into position using intrusive-list pointer swaps on the MachineFunction's BB list.

### Ext-TSP Layout (Optional Path)

When `enable-ext-tsp-block-placement` is true (default: false), the pass uses the Extended Travelling Salesman Problem formulation from LLVM's `CodeLayout.h`. This is a profile-guided model that explicitly optimizes I-cache utilization by penalizing backward references and rewarding fall-through edges.

The ext-TSP path builds a BB index hash-map using LLVM's DenseMap pattern (hash: `(ptr >> 9) ^ (ptr >> 4)`, 75% load factor), computes block frequencies and edge weights, then runs three solver functions:

| Function | Role |
|---|---|
| `sub_29BAF70` | `calcExtTspScore()` -- score the original layout |
| `sub_29BAC40` | `calcExtTspScore()` -- score the alternative layout |
| `sub_29BB2B0` | `computeExtTspLayout()` -- reorder chains by ext-TSP objective |

The pass compares original vs. reordered cost and commits the better ordering via `sub_3519A10` (`applyBlockOrder`). Additional ext-TSP tuning knobs (registered in `ctor_492` at `0x5545a0`):

| Knob | Description |
|---|---|
| `ext-tsp-forward-weight-cond` / `uncond` | Weight for conditional/unconditional forward jumps |
| `ext-tsp-backward-weight-cond` / `uncond` | Weight for conditional/unconditional backward jumps |
| `ext-tsp-fallthrough-weight-cond` / `uncond` | Weight for fall-through edges |
| `ext-tsp-forward-distance` / `backward-distance` | Distance thresholds for cache modeling |
| `ext-tsp-max-chain-size` | Maximum chain size for ext-TSP merging |
| `ext-tsp-chain-split-threshold` | Threshold for splitting chains |
| `ext-tsp-max-merge-density-ratio` | Density ratio cap for chain merges |
| `ext-tsp-apply-without-profile` | Run ext-TSP even without PGO data |
| `cdsort-cache-entries` / `cache-size` | CDSort cache model parameters |
| `cdsort-max-chain-size` | CDSort chain size limit |
| `cdsort-distance-power` / `frequency-scale` | CDSort cost model tuning |

## NVIDIA-Specific Modifications

### Divergence-Gated Tail Duplication

The most significant GPU-specific behavior is the divergence check before tail duplication. At step (G) in the algorithm, the pass reads `MF+8+688 bit 0` -- a flag set by earlier divergence analysis passes indicating the function contains warp-divergent branches. When this bit is set, `sub_35185B0` (`tailDupPlacement`) is skipped entirely.

The rationale: tail duplication creates an additional copy of a basic block to convert a diamond-shaped CFG into a straight-line fall-through. On CPU, this eliminates a taken branch on the hot path. On GPU with divergent branches, both sides of the diamond execute regardless (the warp mask simply toggles), so duplicating the tail block doubles code size for zero fall-through benefit. The divergence flag is a conservative gate -- it disables tail-dup for the entire function, not per-branch.

### Alternative Layout Proposal

When the standard chain-based path is selected (not ext-TSP), and the function has more than 3 basic blocks with profile data and is not marked divergent, the pass runs an additional layout evaluation:

```
if (byte_503C568 is set AND MF.size() > 3):
    alt_layout = sub_34BEDF0(pass)     // construct alternative proposal
    alt_cost   = sub_34C7080(pass)     // evaluate via cost model
    if alt_cost < chain_cost:
        commit(alt_layout)
```

`sub_34BEDF0` and `sub_34C7080` are not present in upstream LLVM. They appear to be an NVIDIA addition that provides a second opinion on block ordering, using a cost model tuned for GPU instruction fetch characteristics. The alternative proposal can override the standard chain-based result when it produces a lower-cost layout. This path writes to the TailDuplicator state at `a1+576` if tail-dup is active.

### Dual Pass Registration

The binary contains two complete instances of MachineBlockPlacement:

| Instance | Registration | Purpose |
|---|---|---|
| `sub_350FE30` (NVPTX) | NVPTX backend pipeline | GPU-specific analysis results, divergence-aware |
| `sub_1DE8060` (generic) | Default LLVM pipeline | Standard pass for any non-GPU path |

Having a separate NVPTX instance allows NVIDIA to control pass ordering independently. The NVPTX version is inserted at a specific point in the backend pipeline where divergence analysis results are available.

### Target Tail-Dup Threshold Override

The tail-dup threshold (how many instructions a tail block can have before duplication is rejected) is determined by a multi-level decision:

```
default_threshold = 2                               // tail-dup-placement-threshold
aggressive_threshold = 4                            // tail-dup-placement-aggressive-threshold

if TII->getTailDupThreshold(optLevel) overrides:    // vtable+1488
    threshold = TII_override                        // NVPTX can take full control
elif optLevel > 2 (-O3):
    threshold = aggressive_threshold                // 4
else:
    threshold = default_threshold                   // 2
```

The default stub at `sub_2FDC800` returns `2 * ((optLevel > 2) + 1)`, i.e., 2 at `-O2` and 4 at `-O3`. If NVPTX's `TargetInstrInfo` overrides this (the pass explicitly checks whether the vtable slot points to `sub_2FDC800`), the override takes full control. This allows the NVPTX backend to set a different tail-dup aggressiveness based on SM generation or kernel properties.

## Loop Rotation and Header Placement

Loop rotation (`sub_351C710`, called from `buildLoopChains`) determines whether the loop header is placed at the top or bottom of the loop chain. The goal is to place the exiting block at the bottom so the back-edge is a fall-through and the exit is a taken branch (or vice versa, whichever is more profitable).

Two rotation strategies exist:

**Basic rotation** (default): Place the exiting block last. Skip rotation if the header already has a viable fall-through from outside the loop, unless the exit edge frequency exceeds the fall-through frequency. This avoids introducing an unnecessary branch at loop entry.

**Profile-guided rotation** (`precise-rotation-cost`): Enumerate all possible rotations, compute fall-through cost for each (missed fall-through from loop entry, missed fall-throughs at exit points, missed back-edge fall-through), and select the rotation with minimum total cost. Controlled by two knobs:

- `precise-rotation-cost` (default false): enable profile-guided rotation cost model
- `force-precise-rotation-cost` (default false): force it even without good profile data

For GPU kernels where loops are the dominant compute pattern, correct loop rotation determines whether the loop body executes as a straight fall-through sequence or requires a taken back-edge branch every iteration. Since the `misfetch-cost` is low (default 1), the benefit is modest per iteration but accumulates over millions of iterations typical in GPU compute.

## Hot/Cold Splitting

cicc does **not** perform function-level hot/cold splitting. This is expected: GPU kernels are designed for all threads in a warp to execute the same path. There is no equivalent of a CPU "cold" exception handler that should be placed far from hot code. The `loop-to-cold-block-ratio` knob (default 5) does enable outlining individual cold blocks from loop chains -- moving them to the end of the function -- but this is intra-function block reordering, not function splitting.

The knob `force-loop-cold-block` (default false) forces cold block outlining from loops regardless of the frequency ratio. When `loop_freq / block_freq > loop-to-cold-block-ratio`, the block is moved out of the loop chain to reduce the loop body's I-cache footprint.

## Post-Placement Passes

After layout is committed, two post-processing steps run:

**Branch optimization.** Walk the final BB ordering. For each analyzable branch with profile info, check whether reversing the branch direction would improve fall-through. Call `TII->reverseBranchCondition()` (vtable+880) to flip the condition, then update the branch targets via vtable+360/368. This is controlled by `sub_2EE6AD0` which checks profitability by comparing edge costs with `sub_2E441D0` (`getEdgeProbability`).

**Block alignment** (`sub_3516980`). Walk each BB and set alignment based on block frequency, loop depth, and whether the block is a fall-through target. Controlled by:

- `align-all-blocks` (default 0): force log2 alignment on every block
- `align-all-nofallthru-blocks` (default 0): force alignment on blocks without fall-through predecessors
- `max-bytes-for-alignment` (default 0): cap padding bytes

On GPU, block alignment is generally not useful -- PTX does not expose alignment constraints on basic blocks, and the hardware instruction fetch unit does not benefit from aligned block boundaries the way a CPU I-cache line does.

## Configuration Knobs

All knobs are LLVM-standard with stock defaults. The NVIDIA delta is behavioral, not configurational.

| Knob | Type | Default | Effect |
|---|---|---|---|
| `disable-block-placement` | bool | false | Disable the pass entirely |
| `enable-block-placement-stats` | bool | false | Collect placement statistics |
| `tail-dup-placement` | bool | true | Enable tail duplication during placement |
| `tail-dup-placement-threshold` | int | 2 | Max instructions for tail-dup candidate |
| `tail-dup-placement-aggressive-threshold` | int | 4 | Aggressive threshold at -O3 |
| `tail-dup-placement-penalty` | int | 2 | I-cache pressure penalty (percent) |
| `tail-dup-profile-percent-threshold` | int | 50 | Min hot-count percentage for profile-guided tail-dup |
| `triangle-chain-count` | int | 2 | Consecutive triangles before triangle heuristic activates |
| `branch-fold-placement` | bool | true | Fold branches during placement |
| `misfetch-cost` | int | 1 | Taken-branch fetch penalty |
| `jump-inst-cost` | int | 1 | Cost of a jump instruction |
| `block-placement-exit-block-bias` | int | 0 | Frequency percentage for loop exit replacement |
| `loop-to-cold-block-ratio` | int | 5 | Ratio threshold for cold block outlining |
| `force-loop-cold-block` | bool | false | Force outlining cold blocks from loops |
| `precise-rotation-cost` | bool | false | Profile-guided loop rotation cost |
| `force-precise-rotation-cost` | bool | false | Force precise rotation cost |
| `align-all-blocks` | int | 0 | Force block alignment (log2) |
| `align-all-nofallthru-blocks` | int | 0 | Force alignment on non-fall-through blocks |
| `max-bytes-for-alignment` | int | 0 | Max padding for alignment |
| `enable-ext-tsp-block-placement` | bool | false | Enable ext-TSP layout algorithm |
| `ext-tsp-block-placement-max-blocks` | int | -1 | Max BB count for ext-TSP (unlimited) |
| `apply-ext-tsp-for-size` | bool | false | Use ext-TSP for code size optimization |
| `renumber-blocks-before-view` | bool | false | Renumber BBs before dot-graph output |

## DenseMap Implementation Pattern

The pass uses LLVM's DenseMap for BB-to-chain and BB-to-index lookups. The open-addressing hash-map pattern appears 20+ times in the decompiled code:

```c
// Hash function for pointer keys
size_t hash = ((ptr >> 9) ^ (ptr >> 4)) & (bucket_count - 1);

// Probing: linear with increment counter
// Empty sentinel:   0xFFFFFFFFFFFFF000 (-4096)
// Deleted sentinel: 0xFFFFFFFFFFFFE000 (-8192)
// Rehash trigger:   4 * (count + 1) >= 3 * bucket_count  (75% load)
// Rehash function:  sub_2E3E470(map, new_capacity)
```

## Function Map

| Address | Identity | Notes |
|---|---|---|
| `sub_3521FF0` | `runOnMachineFunction` | Entry point, 82 KB |
| `sub_3521900` | `buildChains` | Initial chain construction |
| `sub_35185B0` | `tailDupPlacement` | Tail-dup-aware chain merging |
| `sub_3519A10` | `applyBlockOrder` | Commit final BB ordering to MF |
| `sub_3516980` | `alignBlocks` | Post-placement alignment |
| `sub_351EBB0` | `buildLoopChains` | Loop-aware chain merging |
| `sub_351D700` | `buildChainForBlock` | Greedy successor chain walk |
| `sub_35157A0` | `selectBestSuccessor` | Pick best fall-through successor |
| `sub_3515040` | `chainLookup` | DenseMap BB-to-chain lookup |
| `sub_351C710` | `rotateLoop` | Loop rotation heuristic |
| `sub_351A710` | `mergeTails` | Chain tail merge logic |
| `sub_35161F0` | `lowerChain` | Final lowering of chain to BB list |
| `sub_3515CB0` | (helper) | Chain cost model |
| `sub_3515280` | (helper) | Chain building iteration |
| `sub_3516000` | (helper) | Chain length query |
| `sub_34BEDF0` | (NVIDIA addition) | Alternative layout proposal construction |
| `sub_34C7080` | (NVIDIA addition) | Alternative layout cost evaluation |
| `sub_350FE30` | (NVPTX) | Pass registration |
| `sub_350FEE0` | (NVPTX) | Stats pass registration |
| `sub_1DE8060` | (generic) | Generic LLVM pass registration |
| `sub_1DE8500` | (generic) | Generic LLVM stats registration |
| `sub_3511770` | cleanup | Chain-map teardown |
| `sub_35142F0` | cleanup | Loop chain data teardown |
| `sub_3510940` | cleanup | Bump allocator teardown |
| `sub_29BAF70` | `calcExtTspScore` | Ext-TSP score (original layout) |
| `sub_29BAC40` | `calcExtTspScore` | Ext-TSP score (alternative layout) |
| `sub_29BB2B0` | `computeExtTspLayout` | Ext-TSP chain reordering solver |
| `sub_2EE6520` | (helper) | Ext-TSP enable decision |
| `sub_2EE6AD0` | (helper) | Branch redirect profitability check |
| `sub_2E441D0` | `getEdgeProbability` | Edge probability query |
| `sub_2FDC800` | (default stub) | Default `getTailDupThreshold` implementation |

## Cross-References

- **[StructurizeCFG](structurizecfg.md)** -- runs before block placement; produces the structured CFG that constrains which block orderings are legal. Structured regions must remain contiguous.
- **[BranchFolding](branch-folding.md)** -- runs after placement; performs tail merging and branch folding on the committed layout. See `sub_2F336B0`.
- **[Instruction Scheduling](scheduling.md)** -- block ordering affects scheduling windows. Post-placement scheduling operates within the committed layout.
- **[Register Allocation](register-allocation.md)** -- register pressure is affected by block ordering through live range extent.
- **[AsmPrinter](../infra/asmprinter.md)** -- emits PTX from the final block ordering, generating `bra` instructions for taken branches and fall-through for sequential blocks.
