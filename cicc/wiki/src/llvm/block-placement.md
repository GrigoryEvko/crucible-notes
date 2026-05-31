# MachineBlockPlacement for GPU

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

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

```c
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

```c
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

`sub_3521900` (`buildChains`) is the workhorse. It operates in four steps.

#### Step 1 -- Initial Chain Construction

For every BB in the MachineFunction (iterated via the doubly-linked intrusive list from `MF+328` to sentinel `MF+320`), the builder:

1. Allocates a 64-byte chain node from the bump allocator at `pass+792`. The node is initialized with `count=1`, `capacity=1`, the inline BB pointer set to the current BB, and the chain-map pointer set to `pass+888`.
2. Inserts the BB-to-chain mapping into the chain-map via `sub_3515040` (DenseMap insert with pointer hash `((ptr >> 9) ^ (ptr >> 4)) & (bucket_count - 1)`).
3. Attempts to extend the chain forward: calls `TII->analyzeBranch()` (vtable+344) on the current BB. If analyzable and a fall-through successor exists, calls `sub_2E32580` to verify the successor is valid for chaining (not already claimed by a different chain, not a landing pad, not the function entry if it would create a cycle). If valid, the successor is appended to the chain's BB array (growing from inline storage to heap allocation via `sub_C7D6A0` when needed), and the walk continues from the successor.

The result is a set of maximal fall-through chains -- each chain represents a sequence of BBs where every transition is a fall-through edge according to `analyzeBranch`.

#### Step 2 -- Loop Chain Merging

Read the MachineLoopInfo structure at `pass+584`. Iterate loops from innermost outward. For each loop, call `sub_351EBB0` (`buildLoopChains`), which:

1. Identifies all chains that contain BBs belonging to the loop.
2. Merges these chains into a single loop chain, ordering them to maximize fall-through within the loop body.
3. Applies loop rotation via `sub_351C710` (`rotateLoop`) to place the exiting block at the bottom, making the back-edge a fall-through and the exit a taken branch (or the reverse, whichever minimizes cost according to the profile data).

Cold blocks within the loop (where `loop_freq / block_freq > loop-to-cold-block-ratio`) are ejected from the loop chain and will be placed at the function's end during the commit step.

#### Step 3 -- Global Successor Ordering

Call `sub_35157A0` (`selectBestSuccessor`) for each BB to find the globally best successor chain ordering. The selection considers:
- Edge probability from `sub_2E441D0` (`getEdgeProbability`)
- Whether the successor is already the fall-through (free) or would require a taken branch (cost = `misfetch-cost` + `jump-inst-cost`)
- Whether chaining the successor would break an existing profitable chain connection

Then `sub_351D700` (`buildChainForBlock`) performs a greedy walk from the function entry, building the top-level chain by repeatedly selecting the best unchained successor and appending it.

#### Step 4 -- Commit

Walk the final chain's BB array and splice each BB into position using intrusive-list pointer swaps on the MachineFunction's BB list (pointer updates at `BB+0` and `BB+8` -- the prev/next pointers of the doubly-linked list).

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

### Alternative Layout Proposal Algorithm

When the standard chain-based path is selected (not ext-TSP), and the function has more than 3 basic blocks with profile data and is not marked divergent, the pass runs a complete alternative layout evaluation through a pipeline absent from upstream LLVM. This is one of cicc's most significant code-layout additions.

#### Activation Gate

```c
if (byte_503C568 is set AND MF.size() > 3):
    evaluator = sub_34BEDF0(state, profile_flag, MBFI, TII, MBPI)
    changed   = sub_34C7080(evaluator, MF, chain_data, ...)
    if changed:
        commit(evaluator_layout)
```

The gate variable `byte_503C568` corresponds to the `branch-fold-placement` knob (default true). When branch-fold-placement is active and the function has enough basic blocks to justify the extra analysis cost, the alternative path fires.

#### State Object Initialization -- `sub_34BEDF0` (321 bytes)

`sub_34BEDF0` is a constructor that initializes a 0x100-byte evaluator state object. It takes six arguments: `(rdi=state, rsi=profile_available, rdx=?, rcx=MBFI*, r8=TII*, r9=MBPI*)`. The initialization zeroes the majority of the structure and sets up internal storage pointers:

```c
struct LayoutEvaluatorState {      // 0x100 bytes, initialized by sub_34BEDF0
    void*    bb_array_ptr;         // +0x00: BB ordering array (initially null)
    uint64_t bb_array_size;        // +0x08: count
    uint64_t bb_array_cap;         // +0x10: capacity
    uint64_t iteration_count;      // +0x18: cleared to 0
    void*    inline_storage_ptr;   // +0x20: points to +0x38 (inline array)
    uint64_t initial_capacity;     // +0x28: set to 2
    uint32_t current_count;        // +0x30: set to 0
    uint8_t  is_fresh;             // +0x34: set to 1 (first-run flag)
    uint8_t  padding[3];           // +0x35
    uint8_t  inline_array[72];     // +0x38: inline storage for small chains
    uint8_t  profile_available;    // +0x80: bit 0 = profile flag
    uint8_t  force_mode;           // +0x81: set from qword_503AD08 knob
    uint8_t  divergence_aware;     // +0x82: set from dl argument
    uint8_t  needs_reconverge;     // +0x83: cleared to 0, set during evaluation
    uint32_t bb_limit;             // +0x84: from stack argument (BB count cap)
    // +0x88..+0xA8: five qword slots, all zeroed
    void*    bb_ptr_array;         // +0xB0: points to +0xC8 (inline)
    uint64_t bb_ptr_array_pad;     // +0xB8: cleared
    uint64_t bb_ptr_array_cap;     // +0xC0: set to 8
    uint8_t  bb_ptr_inline[24];    // +0xC8: inline BB pointer storage
    uint64_t total_cost;           // +0xD8: cleared
    uint32_t cost_flags;           // +0xE0: cleared
    void*    mbfi_ptr;             // +0xE8: MachineBlockFrequencyInfo*
    void*    tii_ptr;              // +0xF0: TargetInstrInfo*
    void*    mbpi_ptr;             // +0xF8: MachineBranchProbabilityInfo*
};
```

The `force_mode` field at offset `+0x81` is set based on the global `qword_503AD08`. When this global equals 0, the force mode takes the `profile_available` argument. When it equals 1, force mode is unconditionally set to 1 (always evaluate). Any other value causes a straight return (skip evaluation). This provides a three-way override: 0=auto, 1=always, other=never.

#### Dispatch Wrapper -- `sub_34C7080` (17 bytes)

`sub_34C7080` is a thin guard:

```c
// sub_34C7080(rdi=evaluator, rsi=MF, rdx=chain_data, rcx=..., r8=..., r9=changed_flag)
if (rdx == NULL) return 0;     // no chain data -> nothing to evaluate
return sub_34C6AF0(rdi, rsi, rdx, rcx, r8, (bool)r9);
```

The NULL check on `rdx` (the chain-data pointer) provides a fast exit when the chain builder produced no intermediate state worth re-evaluating.

#### Core Layout Evaluator -- `sub_34C6AF0` (1419 bytes)

`sub_34C6AF0` is the real body of the alternative layout evaluator. It operates on the evaluator state object (from `sub_34BEDF0`) and the MachineFunction, performing a complete re-evaluation of the chain-based layout against a different cost model. The algorithm proceeds in six steps:

**Step 1 -- Iteration counter and hash table reset.**
Increment the iteration count at `state+0x18`. If the hash table at `state+0x20` is not fresh (byte at `state+0x34` is 0), compute a minimum table size as `max(32, 4 * (capacity - count))`, and if the current table is undersized, fill it with 0xFF sentinels via `memset`. This hash table tracks which BBs have been visited during the current evaluation pass.

**Step 2 -- State initialization from MachineFunction.**
Clear the running cost accumulator at `state+0x2C..+0x30`. Read the first BB from the MachineFunction's chain data. Store the chain data pointer, the iteration limit from `state+0x84`, and the analysis pointers (MBPI at `state+0x98`, TII at `state+0xA0`) into the evaluator's working slots.

Read `MF->getSubtarget()->something` at offset `+0x220` and subtract `0x2A` (decimal 42). This produces an SM-generation index (sm_70=0, sm_75=1, sm_80=2, ..., sm_90=6, sm_100=16 based on this encoding). This index determines which cost table row is used for the fetch-penalty model.

**Step 3 -- Divergence-aware block scanning.**
For the first BB in the chain, check bit 2 of the flags at `BB[0]+0x158`. If set, dispatch to `TII->vtable+0x210` (which is compared against `sub_2FF52D0` -- the default stub). If the target overrides this vtable slot, call the override with the MachineFunction to determine if the block needs special handling. When the default is in use, set `state+0x83` (needs_reconverge) to 1 unconditionally. This appears to be an NVPTX check for whether the block is in a reconvergence region where layout ordering has correctness implications, not just performance.

**Step 4 -- Main evaluation loop.**
Call `sub_34BA1B0` to snapshot the current chain state into a temporary structure on the stack. Then enter the main loop:

```c
while (true):
    status = sub_34C4890(state, MF)   // advance to next BB in evaluation order
    changed_bit = (state->profile_available XOR 1) OR status
    if changed_bit == 0:
        // Reached the evaluation boundary without changes
        if (sm_index <= 1):           // sm_70 or sm_75
            check qword_503AA68 knob  // additional gate for older archs
            if set: call sub_34C0690(state, loop) for each loop in MF
        if state->divergence_aware:
            call sub_34C56D0(state, loop) for each loop in MF
        break if no further changes

    // A change was proposed
    call sub_34C2D70(state, MF)       // apply the proposed reordering step
    accumulate changed flags

    if (sm_index <= 1):               // sm_70/sm_75
        check qword_503AA68 knob
        if set: call sub_34C0690 for each loop
    if state->divergence_aware:
        call sub_34C56D0 for each loop

    if changed_this_iteration:
        continue loop
```

`sub_34C4890` advances through the MachineFunction's basic blocks in frequency-priority order, proposing a reordering when a higher-frequency successor is not the current fall-through. `sub_34C2D70` performs the actual chain manipulation to implement the proposed swap.

**Step 5 -- Loop-level re-evaluation.**
The calls to `sub_34C56D0` (5137 bytes, called from `sub_34C6AF0` via the loop-iteration path at `0x34C6E90`) perform loop-level cost re-evaluation. This function:
- Walks the MachineFunction's loop tree (from `MF+0x148`, the MachineLoopInfo block list)
- For each loop, evaluates whether the proposed layout improves or degrades the loop body's fall-through density
- Calls `sub_34C0EE0` for block-level cost queries
- Calls `sub_34BE7F0` for chain adjacency analysis
- Queries `sub_2E88AF0` (divergence analysis) and `sub_2E88FE0` for convergence properties
- Uses `sub_2FDC710`/`sub_2FDC700` for target-specific cost overrides via the TII vtable
- Calls `sub_3509790` for reconvergence point identification

`sub_34C0690` (called on the sm_70/sm_75 path gated by `qword_503AA68`) is a lighter variant that omits the divergence-aware sub-evaluations, appropriate for older SM architectures where divergence reconvergence is handled differently.

**Step 6 -- Final cost comparison and bitvector scan.**
After the evaluation loop terminates, build a bitvector tracking which BBs changed position. The bitvector uses 64-bit words with word index `= bb_index >> 6` and bit position `= bb_index & 63`. Walk the MachineFunction's loop tree blocks (`MF+0x148` linked list):
- For each block in the loop, walk the instruction list starting at `BB+0x20`
- For each instruction, mask the opcode with `0xFFFFFF` and compute `opcode * 5` as a stride
- If the instruction byte at offset 0 is `0x08` (a branch instruction), set the corresponding bit in the bitvector

Then scan the bitvector against the evaluator's proposed ordering to detect any BB that would need to move. If at least one BB is displaced, set the return flag.

On the final cost-comparison path (at `0x34C6FD3`), the evaluator reads `TII->vtable+0x5D8` and compares against `sub_2FDC810`. If the target overrides this slot, the override is called to provide a final accept/reject decision. Otherwise, a default threshold of 3 is used: the proposed layout is accepted only if the cost reduction exceeds the acceptance threshold. The stat-based knobs at `dword_503AAC8` and `qword_503AB48` provide tuning for the threshold lookup via the `sub_C52410`/`sub_C959E0` statistics infrastructure.

#### Shared Infrastructure with Register Allocation

A surprising discovery: `sub_34BEDF0` and `sub_34C7080`/`sub_34C6AF0` are also called from `sub_34ED530` (RegAllocGreedy, 91KB) via `sub_34F1190`. The register allocator uses the same layout evaluator to assess whether a spill-induced block split would degrade code layout quality. This sharing means the cost model is consistent between register allocation decisions and post-RA block placement, preventing the two passes from working at cross purposes. The evaluator state is separate per invocation (stack-allocated), so there is no state leakage between the two callers.

#### SM-Generation-Dependent Behavior

The SM index computation (`(MF->getSubtarget()+0x220) - 0x2A`) creates generation-dependent behavior:

| SM Generation | Index | Loop Evaluator | Divergence Sub-Eval |
|---|---|---|---|
| sm_70 (Volta) | 0 | `sub_34C0690` if `qword_503AA68` | Only if divergence flag |
| sm_75 (Turing) | 1 | `sub_34C0690` if `qword_503AA68` | Only if divergence flag |
| sm_80+ (Ampere+) | 2+ | Skipped (only `sub_34C56D0`) | Always if divergence flag |

This split reflects the hardware difference: Volta and Turing use a stack-based reconvergence mechanism that benefits from the lighter `sub_34C0690` analysis, while Ampere and later use the uniform warp scheduler where the more thorough `sub_34C56D0` evaluation is worthwhile.

### Dual Pass Registration

The binary contains two complete instances of MachineBlockPlacement:

| Instance | Registration | Purpose |
|---|---|---|
| `sub_350FE30` (NVPTX) | NVPTX backend pipeline | GPU-specific analysis results, divergence-aware |
| `sub_1DE8060` (generic) | Default LLVM pipeline | Standard pass for any non-GPU path |

Having a separate NVPTX instance allows NVIDIA to control pass ordering independently. The NVPTX version is inserted at a specific point in the backend pipeline where divergence analysis results are available.

### Target Tail-Dup Threshold Override

The tail-dup threshold (how many instructions a tail block can have before duplication is rejected) is determined by a multi-level decision:

```c
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

## GPU-Specific Placement Considerations

### Why This Pass Matters More on GPU Than on CPU

On CPU, MachineBlockPlacement is primarily an I-cache optimization -- placing hot blocks contiguously reduces cache misses. On GPU, the stakes are higher for three reasons:

1. **No branch prediction.** GPU SMs do not speculate. Every taken branch is a guaranteed fetch stall. The ratio of taken branches to fall-throughs directly translates to warp scheduler utilization. Optimal block placement can eliminate 10-30% of fetch bubbles in branch-heavy kernels.

2. **Instruction cache is tiny and shared.** A single SM partition has 32-128 KB of instruction cache shared across all active warps. Code duplication (tail-dup, loop unrolling) competes with warp occupancy for this shared resource. The tail-dup-placement-penalty (2%) is conservative -- on kernels with high warp counts, even small code size increases can cause I-cache thrashing.

3. **Reconvergence is layout-sensitive.** On architectures before Ampere (sm_70, sm_75), the stack-based reconvergence mechanism depends on the post-dominator being reachable from both sides of a divergent branch. Block placement that separates a post-dominator from its divergent predecessors can increase the live warp state, consuming scarce convergence stack entries. The alternative layout evaluator's `sub_34C0690` path specifically addresses this by evaluating reconvergence distance.

### Structured Control Flow Constraint

Unlike CPU backends where block placement has complete freedom, the NVPTX backend runs StructurizeCFG **before** MachineBlockPlacement. This means:

- All irreducible control flow has already been eliminated
- Structured regions (loops, if-then-else diamonds) are contiguous in the CFG
- Block placement cannot violate structured region boundaries without re-structurizing

This constraint actually simplifies placement in some cases (fewer valid orderings to consider) but eliminates certain profitable reorderings that would be legal on CPU (e.g., outlining a cold exception handler to a distant location that breaks region contiguity).

### Interaction with PTX Emission

The final block ordering directly determines which branches in the PTX output are `bra` instructions (taken) vs. fall-throughs (implicit). The AsmPrinter (see [AsmPrinter](../infra/asmprinter.md)) emits `bra` only for non-fall-through edges. Since `ptxas` performs its own block scheduling on the PTX input, the cicc block ordering serves as a strong hint rather than a final answer -- but `ptxas` generally respects the input ordering for blocks within the same structured region.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `runOnMachineFunction` | `sub_3521FF0` | -- | Entry point, 82 KB |
| `buildChains` | `sub_3521900` | -- | Initial chain construction |
| `tailDupPlacement` | `sub_35185B0` | -- | Tail-dup-aware chain merging |
| `applyBlockOrder` | `sub_3519A10` | -- | Commit final BB ordering to MF |
| `alignBlocks` | `sub_3516980` | -- | Post-placement alignment |
| `buildLoopChains` | `sub_351EBB0` | -- | Loop-aware chain merging |
| `buildChainForBlock` | `sub_351D700` | -- | Greedy successor chain walk |
| `selectBestSuccessor` | `sub_35157A0` | -- | Pick best fall-through successor |
| `chainLookup` | `sub_3515040` | -- | DenseMap BB-to-chain lookup |
| `rotateLoop` | `sub_351C710` | -- | Loop rotation heuristic |
| `mergeTails` | `sub_351A710` | -- | Chain tail merge logic |
| `lowerChain` | `sub_35161F0` | -- | Final lowering of chain to BB list |
| (helper) | `sub_3515CB0` | -- | Chain cost model evaluation |
| (helper) | `sub_3515280` | -- | Chain building iteration |
| (helper) | `sub_3516000` | -- | Chain length query |
| (NVIDIA addition) | `sub_34BEDF0` | -- | Layout evaluator state constructor (321 bytes) |
| (NVIDIA addition) | `sub_34C7080` | -- | Layout evaluator dispatch wrapper (17 bytes, guards `sub_34C6AF0`) |
| (NVIDIA addition) | `sub_34C6AF0` | -- | Core layout evaluator body (1419 bytes, SM-aware) |
| (NVIDIA addition) | `sub_34C4890` | -- | Frequency-priority BB advancement |
| (NVIDIA addition) | `sub_34C2D70` | -- | Chain swap application |
| (NVIDIA addition) | `sub_34C56D0` | -- | Loop-level cost re-evaluation (5137 bytes, divergence-aware) |
| (NVIDIA addition) | `sub_34C0690` | -- | Lightweight loop evaluator (sm_70/sm_75 path) |
| (NVIDIA addition) | `sub_34BA1B0` | -- | Chain state snapshot |
| (NVIDIA addition) | `sub_34C0EE0` | -- | Block-level cost query |
| (NVIDIA addition) | `sub_34BE7F0` | -- | Chain adjacency analysis |
| (NVPTX) | `sub_350FE30` | -- | Pass registration |
| (NVPTX) | `sub_350FEE0` | -- | Stats pass registration |
| (generic) | `sub_1DE8060` | -- | Generic LLVM pass registration |
| (generic) | `sub_1DE8500` | -- | Generic LLVM stats registration |
| cleanup | `sub_3511770` | -- | Chain-map teardown |
| cleanup | `sub_35142F0` | -- | Loop chain data teardown |
| cleanup | `sub_3510940` | -- | Bump allocator teardown |
| `calcExtTspScore` | `sub_29BAF70` | -- | Ext-TSP score (original layout) |
| `calcExtTspScore` | `sub_29BAC40` | -- | Ext-TSP score (alternative layout) |
| `computeExtTspLayout` | `sub_29BB2B0` | -- | Ext-TSP chain reordering solver |
| (helper) | `sub_2EE6520` | -- | Ext-TSP enable decision |
| (helper) | `sub_2EE6AD0` | -- | Branch redirect profitability check |
| `getEdgeProbability` | `sub_2E441D0` | -- | Edge probability query |
| (default stub) | `sub_2FDC800` | -- | Default `getTailDupThreshold` implementation |
| (default stub) | `sub_2FF52D0` | -- | Default reconvergence-region query |
| (default stub) | `sub_2FDC810` | -- | Default layout-accept threshold query |

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Pass instances** | Single `MachineBlockPlacement` per pipeline | Two instances: stock LLVM copy + NVPTX-pipeline copy at `sub_3521FF0` |
| **Divergence awareness** | No divergence concept; layout optimizes for I-cache locality | Queries warp divergence flag on MachineFunction; divergent branches affect tail duplication profitability |
| **Alternative layout proposal** | Absent; single layout path only | Additional proposal path (`sub_34BEDF0` / `sub_34C7080`) evaluates alternative orderings with SM-aware cost |
| **Tail duplication threshold** | `TailDupPlacementThreshold` (default 2) | GPU-specific threshold via vtable query (`sub_2FDC810`); controlled by reconvergence-region analysis |
| **Loop cost evaluation** | Frequency-weighted chain cost | Divergence-aware loop cost re-evaluation (`sub_34C56D0`, 5137 bytes) considers warp reconvergence overhead |
| **Ext-TSP scoring** | Standard profile-guided layout scoring | Same Ext-TSP solver but gated by NVPTX-specific enable decision (`sub_2EE6520`) |
| **Structured CFG constraint** | No structured CFG requirement (targets like x86 have arbitrary CFG) | Must preserve structured regions from StructurizeCFG; contiguous structured blocks cannot be interleaved |

## Cross-References

- **[StructurizeCFG](structurizecfg.md)** -- runs before block placement; produces the structured CFG that constrains which block orderings are legal. Structured regions must remain contiguous.
- **[BranchFolding](branch-folding.md)** -- runs after placement; performs tail merging and branch folding on the committed layout. See `sub_2F336B0`.
- **[Instruction Scheduling](scheduling.md)** -- block ordering affects scheduling windows. Post-placement scheduling operates within the committed layout.
- **[Register Allocation](register-allocation.md)** -- register pressure is affected by block ordering through live range extent.
- **[AsmPrinter](../infra/asmprinter.md)** -- emits PTX from the final block ordering, generating `bra` instructions for taken branches and fall-through for sequential blocks.
