# Branch & Switch Optimization

Four phases in the ptxas pipeline transform branch and switch-statement control flow in the Ori IR. Two phases optimize switch statements (phases 14 and 30), one performs general branch simplification (phase 15), and one flattens nested conditional branches (phase 38). Together they reduce branch count, eliminate unreachable code, and prepare the CFG for downstream passes like predication (phase 63), liveness analysis (phase 16), and loop canonicalization (phase 18).

These phases operate on the Ori IR before register allocation and scheduling. At this pipeline stage, branch instructions use the Ori `OEN` opcode (SASS `BRA`), conditional execution is controlled by predicate registers (P0--P6, PT), and the CFG is a hash-map-based structure with FNV-1a-keyed successor/predecessor edges.

| | |
|---|---|
| **DoSwitchOptFirst** | Phase 14 -- vtable at `off_22BD7F8` |
| **OriBranchOpt** | Phase 15 -- vtable at `off_22BD820` |
| **DoSwitchOptSecond** | Phase 30 -- vtable at `off_22BDA78` |
| **OptimizeNestedCondBranches** | Phase 38 -- vtable at `off_22BDBB8` |
| **Phase factory** | `sub_C60D30` cases 14, 15, 30, 38 |
| **Phase object size** | 16 bytes (standard `{vtable_ptr, allocator_ptr}`) |
| **IR level** | Ori -- SASS opcodes with virtual registers |
| **Key opcodes** | `OEN` (BRA), `OFFL` (BSSY), `OFLAP` (BSYNC) |
| **CFG infrastructure** | FNV-1a hash maps at Code Object +648 (successors), +680 (backedges) |
| **Related passes** | 31 `OriLinearReplacement`, 63 `OriDoPredication`, 80 `ExpandJmxComputation`, 133/136 `MergeEquivalentConditionalFlow` |

## Pipeline Placement

```
Phase   3  AnalyzeControlFlow              ── builds CFG (predecessors, successors, RPO, dominators)
Phase   6  SetControlFlowOpLastInBB        ── ensures branches are last in each block
Phase  13  GeneralOptimizeEarly            ── const fold + copy prop (feeds branch info)
Phase  14  DoSwitchOptFirst                ── SWITCH OPTIMIZATION (1st pass)
Phase  15  OriBranchOpt                    ── BRANCH SIMPLIFICATION
Phase  16  OriPerformLiveDeadFirst         ── DCE cleanup of dead branches
    ...
Phase  29  GeneralOptimize                 ── const fold after loop transforms
Phase  30  DoSwitchOptSecond               ── SWITCH OPTIMIZATION (2nd pass)
Phase  31  OriLinearReplacement            ── branchless replacement
    ...
Phase  37  GeneralOptimizeMid              ── const fold + copy prop (feeds nested cond info)
Phase  38  OptimizeNestedCondBranches      ── NESTED CONDITIONAL FLATTENING
    ...
Phase  63  OriDoPredication                ── if-conversion (converts short branches to predicates)
    ...
Phase  80  ExpandJmxComputation            ── expands jump-table index computations
    ...
Phase 133  MergeEquivalentConditionalFlow  ── tail merging
Phase 136  LateMergeEquivalentConditionalFlow
```

### Why Two DoSwitchOpt Passes?

The first pass (phase 14) runs immediately after the initial `GeneralOptimizeEarly` compound pass. At this point, constant folding and copy propagation have resolved many switch selector values, enabling the optimizer to determine case density and choose a lowering strategy.

The second pass (phase 30) runs after loop unrolling (phase 22), strength reduction (phase 21), SSA phi insertion (phase 23), and software pipelining (phase 24). These transformations can expose new switch patterns -- particularly after loop unrolling duplicates switch bodies, creating opportunities for case clustering that were not visible before.

Both passes share the same optimization logic. They differ only in their vtable addresses and pipeline position.

## DoSwitchOpt -- Switch Statement Optimization (Phases 14, 30)

### Overview

`DoSwitchOpt` transforms high-level switch statements from their initial representation as cascading conditional branches into one of three lowered forms, selected based on case density and count. The input is a chain of `ISETP` (integer set-predicate) + `BRA` (conditional branch) instruction pairs that compare the switch selector against successive case constants. The output is one of:

1. **Jump table** -- a contiguous array of branch targets indexed by the selector value
2. **Binary search tree** -- a balanced tree of comparisons that narrows the target in O(log n)
3. **Cascading if-else chain** -- the original form, retained when the switch is small or sparse

### Input: Switch Pattern Recognition

The pass scans each basic block for a characteristic pattern:

```
// Input: cascading if-else for switch(x)
BB0:
    ISETP.EQ P0, x, #case_0      // compare selector against constant
    @P0 BRA target_0              // conditional branch to case body
    ISETP.EQ P0, x, #case_1
    @P0 BRA target_1
    ISETP.EQ P0, x, #case_2
    @P0 BRA target_2
    ...
    BRA default_target            // fallthrough to default case
```

The recognizer collects:
- The **selector register** (the common first operand of all `ISETP` instructions)
- The set of **case constants** (immediate operands of each `ISETP`)
- The **branch targets** (one per case, plus the default target)
- The **case count** N

### Decision: Strategy Selection

The strategy is selected by evaluating case density and count:

```
function select_switch_strategy(cases[], N, min_val, max_val):
    range = max_val - min_val + 1
    density = N / range                    // fraction of range covered by cases

    if N <= SMALL_SWITCH_THRESHOLD:        // observed: ~4 cases
        return CASCADING_IF_ELSE           // keep original form

    if density >= JUMP_TABLE_DENSITY:      // observed: ~0.4 (40%)
        if range <= MAX_JUMP_TABLE_SIZE:   // observed: ~1024 entries
            return JUMP_TABLE

    return BINARY_SEARCH_TREE
```

The thresholds are not configurable via the knob system. They are hardcoded constants in the phase execute function.

**Jump table** is preferred when case values are dense -- the selector maps directly to a table index with a bounds check and a subtraction. This produces the fastest code but consumes data memory proportional to the value range (not the case count).

**Binary search tree** is the default for large sparse switches. The pass sorts case constants and generates a balanced BST of `ISETP` + `BRA` pairs. Each comparison eliminates half the remaining candidates, reaching the target in O(log N) branches.

**Cascading if-else** is retained for small switches (typically 4 or fewer cases) where the overhead of a jump table or BST setup exceeds the cost of linear comparison.

### Output: Jump Table Lowering

For jump-table-eligible switches, the pass produces:

```
// Output: jump table lowering
BB_switch:
    IADD3 Rtmp, Rselector, -min_val, RZ    // normalize to 0-based index
    ISETP.GE.U32 P0, Rtmp, #range          // bounds check (unsigned)
    @P0 BRA default_target                  // out-of-range -> default
    // The jump table index computation is left as a pseudo-instruction
    // that phase 80 (ExpandJmxComputation) expands later into:
    //   LEA Raddr, Rtmp, #table_base, 2    // Raddr = table_base + index * 4
    //   BRX Raddr, #table_base             // indexed branch
```

The actual `BRX` (branch indexed) instruction is a SASS-level indirect branch through a table embedded in the `.text` section. Each table entry is a 4-byte relative offset. Phase 80 (`ExpandJmxComputation`) runs much later (after legalization) to expand the index computation pseudo-instruction into the final `LEA` + `BRX` sequence.

### Output: Binary Search Tree Lowering

For BST-eligible switches:

```
function emit_bst(cases[], lo, hi, selector, default_target):
    if lo > hi:
        emit: BRA default_target
        return

    mid = (lo + hi) / 2

    if lo == hi:
        emit: ISETP.EQ P0, selector, #cases[mid].value
        emit: @P0 BRA cases[mid].target
        emit: BRA default_target
        return

    emit: ISETP.LT P0, selector, #cases[mid].value
    emit: @P0 BRA left_subtree_label

    // Right subtree (selector >= cases[mid].value)
    emit: ISETP.EQ P0, selector, #cases[mid].value
    emit: @P0 BRA cases[mid].target
    emit_bst(cases, mid+1, hi, selector, default_target)

    // Left subtree (selector < cases[mid].value)
    left_subtree_label:
    emit_bst(cases, lo, mid-1, selector, default_target)
```

This produces a balanced tree with depth ceil(log2(N+1)). Each internal node performs at most two comparisons (less-than and equality), though the pass may optimize nodes with consecutive case values to use range checks.

### GPU-Specific: SIMT Divergence Impact

Switch optimization interacts directly with SIMT execution. On a GPU, when threads in a warp take different switch cases, the warp diverges and each case executes serially. The optimizer considers this:

- **Jump tables** produce a single divergence point at the `BRX` instruction. All threads that pick the same case reconverge naturally. The hardware `BSSY`/`BSYNC` (branch sync stack push/pop) mechanism ensures reconvergence after the switch.
- **BST lowering** produces O(log N) potential divergence points. Threads that agree on the BST path stay converged; threads that disagree at each BST node split into independently masked sub-warps.
- **Cascading if-else** produces N potential divergence points. Each comparison can split the warp.

For GPU code, jump tables are strongly preferred when density permits, because they minimize the number of divergence points to exactly one (the `BRX`), regardless of case count.

## OriBranchOpt -- Branch Simplification (Phase 15)

### Overview

`OriBranchOpt` performs four categories of CFG-level simplification on the Ori IR. It runs as a single pass that iterates over all basic blocks and applies the following transformations until no further changes occur:

1. **Unconditional branch folding** -- eliminates `BRA` instructions that jump to the immediately following block
2. **Unreachable block elimination** -- removes basic blocks with no predecessors (except the entry block)
3. **Conditional branch simplification** -- simplifies conditional branches where the condition is provably constant or the true/false targets are identical
4. **Branch chain threading** -- redirects branches that target blocks consisting of a single unconditional `BRA`, directly to the final destination

### Transformation 1: Unconditional Branch Folding

When a basic block ends with an unconditional `BRA` to the block that immediately follows in layout order, the branch is redundant and is deleted:

```
// Before:                        // After:
BB_A:                             BB_A:
    ...                               ...
    BRA BB_B                          // fallthrough
BB_B:                             BB_B:
    ...                               ...
```

This is the most common transformation. It arises frequently after switch optimization introduces new blocks and after loop unrolling creates copies of loop bodies that end with unconditional jumps back to the next iteration.

### Transformation 2: Unreachable Block Elimination

After other branch simplifications may redirect branches away from certain blocks, those blocks lose all predecessors and become unreachable. The pass deletes them:

```
function eliminate_unreachable(func):
    for each block B in func (excluding entry):
        if predecessor_count(B) == 0:
            // Remove B from successor lists of all blocks
            // Delete all instructions in B
            // Remove B from the block list
            // Update CFG hash maps
```

The CFG hash maps at Code Object offsets +648 (successors) and +680 (backedges) must be updated atomically with block deletion to maintain consistency for downstream passes.

### Transformation 3: Conditional Branch Simplification

Two sub-cases:

**Constant condition.** If copy propagation or constant folding (in the preceding `GeneralOptimizeEarly`, phase 13) has determined that a predicate register always holds a known value at the branch point, the conditional branch is replaced:

```
// Before: condition always true      // After:
BB:                                   BB:
    ISETP.EQ PT, R0, R0              //   (deleted -- tautology)
    @PT BRA target                        BRA target   // unconditional
    BRA fallthrough                   //   (deleted)
```

**Equivalent targets.** If both the taken and not-taken paths of a conditional branch go to the same block, the condition test is dead and the branch becomes unconditional:

```
// Before: both targets identical     // After:
BB:                                   BB:
    @P0 BRA target                        BRA target   // unconditional
    BRA target                        //   (deleted)
```

### Transformation 4: Branch Chain Threading

When a branch targets a block whose only content is another unconditional branch, the pass redirects the original branch directly to the final target:

```
// Before:                            // After:
BB_A:                                 BB_A:
    @P0 BRA BB_B                          @P0 BRA BB_C   // threaded
BB_B:                                 // BB_B may become unreachable
    BRA BB_C                          BB_C:
BB_C:                                     ...
    ...
```

The pass applies threading iteratively, following chains of single-branch blocks until a non-trivial block is reached. A depth limit prevents infinite loops on pathological CFGs with cycles of empty blocks (which should not exist in well-formed IR but are guarded against defensively).

### Fixed-Point Iteration

The four transformations are applied in a worklist-driven loop. Each transformation can enable others:
- Threading can make intermediate blocks unreachable (enables transformation 2)
- Unreachable block elimination can make remaining branches target the immediately following block (enables transformation 1)
- Folding can expose equivalent-target conditionals (enables transformation 3)

The pass terminates when a full iteration over all blocks produces no changes.

## OptimizeNestedCondBranches -- Nested Conditional Flattening (Phase 38)

### Overview

Phase 38 targets a specific control flow pattern: nested conditional branches that test related predicates. This pattern commonly arises from C/C++ code with compound conditions (`if (a && b)`, `if (a || b)`) and from switch-case fall-through after `DoSwitchOpt` lowering.

The pass runs after `GeneralOptimizeMid` (phase 37), which provides fresh constant folding and copy propagation results. It runs before `OriDoPredication` (phase 63), feeding it simpler CFG patterns that are easier to convert to predicated code.

### Pattern: Nested If-Then

```
// Before: nested conditional
BB_outer:
    @P0 BRA BB_inner
    BRA BB_merge
BB_inner:
    @P1 BRA BB_body
    BRA BB_merge
BB_body:
    ... body instructions ...
    BRA BB_merge
BB_merge:
    ...

// After: flattened with combined predicate
BB_entry:
    LOP3 Ptmp, P0, P1, 0xC0          // Ptmp = P0 AND P1
    @Ptmp BRA BB_body
    BRA BB_merge
BB_body:
    ... body instructions ...
    BRA BB_merge
BB_merge:
    ...
```

The `LOP3` (3-input logic) instruction with truth table `0xC0` computes AND. This combines two branch tests into one, eliminating a basic block and a divergence point.

### Pattern: Nested If-Or

```
// Before: short-circuit OR
BB_test1:
    @P0 BRA BB_body                   // first condition true -> body
    BRA BB_test2
BB_test2:
    @P1 BRA BB_body                   // second condition true -> body
    BRA BB_merge                      // both false -> merge
BB_body:
    ...

// After: flattened with OR predicate
BB_entry:
    LOP3 Ptmp, P0, P1, 0xFC          // Ptmp = P0 OR P1
    @Ptmp BRA BB_body
    BRA BB_merge
BB_body:
    ...
```

### Safety Constraints

The pass applies these transformations only when:

1. **No side effects** between the nested branches -- the intermediate block must contain only the branch instruction (and optionally predicate-setting `ISETP`/`FSETP` instructions)
2. **No live-out values** from the intermediate block other than the predicate -- if the intermediate block defines registers used after the merge, the transformation would change semantics
3. **Both branches target the same merge point** -- the not-taken path of both the outer and inner branches must reach the same merge block
4. **The predicates are independent** -- P0 and P1 must not be related by a def-use chain within the nested pattern (otherwise folding changes the evaluation order)

### Relationship to Predication

Phase 38 is a stepping stone toward phase 63 (`OriDoPredication`). By reducing nested branches to single-level branches, it creates more opportunities for if-conversion -- the predication pass can then convert the single remaining branch into a fully predicated (branchless) instruction sequence.

The transformation pipeline for a `if (a && b) { x = y; }` pattern is:

```
Phase 38: nested {if(a) { if(b) { ... }}}  -->  if(a AND b) { ... }
Phase 63: if(a AND b) { x = y; }           -->  @(a AND b) MOV x, y
```

Without phase 38, the predication pass would see a multi-level branch diamond that exceeds its nesting-depth threshold, and both branches would remain in the output.

## GPU-Specific Considerations

### SIMT Divergence and Reconvergence

On NVIDIA GPUs, branch optimization has a direct impact on warp execution efficiency. Every conditional branch is a potential divergence point where threads in a 32-thread warp may take different paths. Divergence serializes execution: the warp must execute both paths, masking inactive threads.

The `BSSY` (branch sync stack push) / `BSYNC` (branch sync) mechanism on modern NVIDIA architectures (sm_75+) manages reconvergence:

```
BSSY B0, reconvergence_point     // push reconvergence point onto sync stack
@P0 BRA taken_path               // diverge
    ... not-taken path ...
    BSYNC B0                     // threads arriving here wait
taken_path:
    ... taken path ...
    BSYNC B0                     // all threads reconverge here
reconvergence_point:
    ...                          // continue with full warp
```

Branch optimization directly reduces the number of `BSSY`/`BSYNC` pairs needed:
- **Branch folding** (phase 15) eliminates unconditional branches that do not cause divergence but still consume `BSSY`/`BSYNC` bookkeeping
- **Nested conditional flattening** (phase 38) reduces two nested `BSSY`/`BSYNC` regions to one, cutting sync-stack depth by one level
- **Jump table lowering** (phases 14/30) collapses N divergence points into one `BRX` instruction

### Reconvergence Stack Depth

The hardware branch sync stack has finite depth (varies by architecture, typically 16--32 entries on sm_75+). Deeply nested branches can overflow the stack, causing hardware serialization or requiring the compiler to restructure control flow. Branch optimization reduces sync-stack pressure by flattening nesting.

### Uniform Branches

When all threads in a warp evaluate a branch condition identically (uniform branch), no divergence occurs. The optimizer detects uniform branches via the `AnalyzeUniformsForSpeculation` pass (phase 27) and the `OriPropagateVarying` passes (phases 53, 70). Uniform branches are cheaper because:
- No `BSSY`/`BSYNC` is needed (the warp stays converged)
- On sm_75+, uniform branches can use the `UBRA` (uniform branch) encoding, which has lower latency

Branch optimization interacts with uniformity analysis: simplifications that eliminate branches also eliminate divergence-point metadata, and conversely, branches proven uniform may not need optimization because their execution cost is already minimal.

### Switch Tables and Warp Divergence

A switch with K active cases in a 32-thread warp incurs at most K serialized case executions (one per unique case value across threads). Jump table lowering does not change this thread-level divergence cost, but it does change the instruction-level cost:

| Strategy | Instructions (worst case) | Divergence points | Sync-stack entries |
|----------|--------------------------|-------------------|-------------------|
| Cascading if-else (N cases) | 2N (ISETP + BRA per case) | N | N |
| BST (N cases) | 2 * ceil(log2(N)) | ceil(log2(N)) | ceil(log2(N)) |
| Jump table | 3 (IADD3 + ISETP + BRX) | 1 | 1 |

The jump table is strongly preferred for GPU execution because it minimizes sync-stack entries to exactly 1, regardless of case count.

## Implementation Details

### Phase Vtable Structure

All four phases follow the standard 16-byte phase object model:

| Phase | Factory case | Vtable address | isNoOp behavior |
|-------|-------------|----------------|-----------------|
| 14 `DoSwitchOptFirst` | case 14 | `off_22BD7F8` | Active at -O1 and above |
| 15 `OriBranchOpt` | case 15 | `off_22BD820` | Active at -O1 and above |
| 30 `DoSwitchOptSecond` | case 30 | `off_22BDA78` | Active at -O1 and above |
| 38 `OptimizeNestedCondBranches` | case 38 | `off_22BDBB8` | Active at -O1 and above |

At `-O0`, all four phases return `isNoOp() = true`. Branch and switch optimization is entirely disabled at `-O0` to preserve source-level debugging correspondence.

### Interaction with ExpandJmxComputation (Phase 80)

Phase 80 is the delayed lowering phase for jump table index computations created by `DoSwitchOpt`. The separation exists because:

1. Jump table index computation requires knowing the final table address, which is not available until after legalization
2. Intervening optimization passes (GVN-CSE, strength reduction) may simplify the index computation before it is expanded
3. Register allocation needs to see the index computation as a single pseudo-instruction for accurate pressure estimation

The pseudo-instruction left by `DoSwitchOpt` is expanded by phase 80 into the final `LEA` + `BRX` sequence after all high-level optimizations are complete.

### Interaction with OriLinearReplacement (Phase 31)

Phase 31 runs immediately after `DoSwitchOptSecond` (phase 30). It targets branch-heavy patterns that survived switch optimization and attempts to replace them with branchless (linear) computation sequences using `SEL` (select) and predicated `MOV` instructions. This is a complement to predication (phase 63) -- it operates earlier in the pipeline on simpler patterns, while predication handles more complex diamond-shaped control flow later.

### Interaction with MergeEquivalentConditionalFlow (Phases 133, 136)

Two late-pipeline passes perform tail merging -- identifying basic blocks with identical instruction sequences that branch to the same targets, and merging them into a single block. This catches redundancy left over after branch optimization, particularly in switch case bodies that perform similar operations on different case values.

## Algorithmic Summary

```
Pass                           Algorithm                    Complexity    CFG Changes
─────────────────────────────  ───────────────────────────  ────────────  ──────────────────────
DoSwitchOpt (14, 30)           Pattern match + decision     O(N log N)    Rewrites blocks, adds
                               tree for strategy selection   per switch    jump table pseudo-ops

OriBranchOpt (15)              Worklist-driven CFG          O(B + E)      Deletes blocks, removes
                               simplification (fixed-point)  per iter      edges, threads branches

OptimizeNestedCondBranches     Pattern match on nested      O(B)          Merges blocks, replaces
(38)                           branch diamonds                             branches with LOP3+BRA
```

Where N = number of switch cases, B = number of basic blocks, E = number of CFG edges.

## Cross-References

- [Pass Inventory](index.md) -- complete 159-phase table with phase numbers and categories
- [Optimization Pipeline](../pipeline/optimizer.md) -- pipeline infrastructure, dispatch loop, phase ordering
- [Ori IR](../ir/overview.md) -- instruction layout, opcode table (OEN = BRA, OFFL = BSSY, OFLAP = BSYNC), CFG hash maps
- [GeneralOptimize Bundles](general-optimize.md) -- phases 13, 29, 37 that feed constant/copy information to branch passes
- [Liveness Analysis](liveness.md) -- phase 16 (DCE cleanup after branch/switch optimization)
- [Predication](predication.md) -- phase 63 (if-conversion, consumes simplified CFG from phases 15 and 38)
- [Hot/Cold Partitioning](hot-cold.md) -- phases 41, 108, 109 (block placement interacts with branch layout)
- [Synchronization & Barriers](sync-barriers.md) -- BSSY/BSYNC reconvergence mechanism
