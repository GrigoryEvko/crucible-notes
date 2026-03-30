# BranchFolding & TailMerge

BranchFolding is LLVM's post-register-allocation CFG optimizer. It runs after block placement and performs three transformations in a fixed-point loop: tail merging (extracting identical instruction tails from multiple blocks into a shared block), branch optimization (eliminating redundant or unreachable branches, merging single-predecessor blocks into predecessors), and common-code hoisting (lifting identical instructions from successors into a shared predecessor). In cicc v13.0 the pass lives at `sub_2F336B0` (the `OptimizeBlock` / `TailMergeBlocks` core, 11,347 bytes) with pass entry at `sub_2F36310`. The NVPTX version carries one critical divergence from upstream LLVM: tail merging is **not** disabled by `requiresStructuredCFG()`. Instead, cicc keeps tail merging enabled but gates individual merge decisions on a reserved-register check that prevents merging when NVPTX special registers (`%tid.x`, `%ntid.x`, etc.) cross the merge boundary.

## Key Facts

| Property | Value |
|---|---|
| Core function | `sub_2F336B0` (`OptimizeBlock` / `TailMergeBlocks`) |
| Function size | 11,347 bytes (792-byte stack frame) |
| Pass entry point | `sub_2F36310` (iterates all MBBs) |
| Pass ID (upstream) | `"branch-folder"` / `BranchFolderPassID` |
| Pipeline position | After register allocation, after block placement |
| Disable knob | `-disable-branch-fold` (global at `qword_5022CC8`) |
| Tail-merge gate | `enable-tail-merge` (tri-state: unset/true/false) |
| Tail-merge threshold | `-tail-merge-threshold` (default 150) |
| Minimum tail length | `-tail-merge-size` (default 3 instructions) |
| Knob constructor | `ctor_346` |
| Required property | `NoPHIs` -- SSA phi nodes must already be eliminated |

## Upstream vs. NVPTX Behavior

In stock LLVM, `BranchFolderPass::run` checks `requiresStructuredCFG()` on the `TargetMachine` and, if true, disables tail merging entirely:

```cpp
bool EnableTailMerge = !MF.getTarget().requiresStructuredCFG()
                       && PassConfig->getEnableTailMerge();
```

NVPTX returns `true` from `requiresStructuredCFG()`, so upstream LLVM would completely suppress tail merging for GPU targets. cicc removes this gate. The binary evidence is the vtable check at `0x2F337A3` (`cmp rax, offset sub_2DAC790`), which verifies that the `NVPTXInstrInfo` vtable supports `analyzeBranch` -- if it does, tail merging proceeds. The structured-CFG check is absent. This makes sense: StructurizeCFG has already run by this point and guaranteed reducible control flow; tail merging two blocks that share a common successor preserves reducibility because it only introduces a new unconditional branch to the merged tail, which does not create irreducible cycles.

However, cicc compensates with two safety mechanisms that upstream does not need:

1. **Reserved-register check.** At `0x2F3427B`, the pass calls `sub_2E88A90` with flag `0x200` (`isReservedReg`) on every register live across the proposed merge boundary. NVPTX special registers (`%tid.x`, `%ntid.x`, `%ctaid.x`, etc.) are reserved and cannot be live-in to a newly created shared tail block because their values are implicitly defined by the hardware. If any reserved register is detected, the merge is rejected.

2. **Priority ordering for conditional branches.** The pattern `or ecx, 2` at `0x2F33B1C` assigns priority >= 2 to conditional branch terminators and lower priority to unconditional branches. This ensures unconditional-branch tails are merged first, because those merges never alter branch conditions and are always safe within structured CFG. Conditional tail merges are attempted only after unconditional ones are exhausted.

## Algorithm

The pass entry `sub_2F36310` calls `OptimizeFunction`, which runs a fixed-point loop:

```
OptimizeFunction(MF):
    repeat:
        changed  = TailMergeBlocks(MF)
        changed |= OptimizeBranches(MF)
        changed |= HoistCommonCode(MF)
    until !changed
    // clean up dead jump tables
```

### TailMergeBlocks

TailMergeBlocks operates in two phases.

**Phase A -- return/exit blocks.** Collect all blocks with no successors (return blocks, noreturn calls) into `MergePotentials`, capped at `tail-merge-threshold` (150). Hash each block's tail via `sub_2F26260` (`HashEndOfMBB`), which computes `HashMachineInstr` on the last non-debug instruction. If two or more candidates share a hash, call `TryTailMergeBlocks` to attempt the merge.

**Phase B -- multi-predecessor blocks.** For each block `IBB` with >= 2 predecessors, collect the predecessors into `MergePotentials`. For each predecessor `PBB`:
- Skip self-loops (`PBB == IBB`), EH-pad successors, inline-asm-br blocks.
- Call `AnalyzeBranch` (`sub_2E09D00`) on `PBB`. If `PBB` conditionally branches to `IBB`, reverse the condition so the unconditional fall-through to `IBB` is removed, leaving only the conditional branch to the "other" target. This normalization enables tail comparison.
- Hash the tail of the normalized `PBB` and push it into `MergePotentials`.

Then call `TryTailMergeBlocks(IBB, PredBB, MinCommonTailLength)`:

```
TryTailMergeBlocks(SuccBB, PredBB, MinTail):
    sort MergePotentials by hash
    for each group of candidates sharing a hash:
        for each pair (MBB1, MBB2) in the group:
            tail_len = ComputeCommonTailLength(MBB1, MBB2)
            if tail_len >= MinTail:
                // check reserved-register constraint (NVPTX addition)
                for each reg live across merge point:
                    if hasProperty(reg, 0x200):  // isReservedReg
                        reject merge; continue
                // perform the merge
                create new MBB "CommonTail"
                splice tail instructions from MBB1 into CommonTail
                ReplaceTailWithBranchTo(MBB2, CommonTail)
                UpdateTerminator on both blocks
                update live-ins for CommonTail
                merged = true
    return merged
```

`ComputeCommonTailLength` walks backwards from both block ends, comparing instructions via `isIdenticalTo`. It skips debug and CFI instructions. Inline asm is never merged (hard-coded rejection in upstream). The cicc binary performs this comparison at `0x2F33B0F`--`0x2F33BDD`, extracting opcode from `[ptr+18h]` and comparing sub-fields via `sar`/`and` arithmetic on the instruction encoding.

### OptimizeBranches

`OptimizeBranches` (`sub_2F36310` inner loop) walks every MBB and calls `OptimizeBlock` to perform local branch simplifications:

1. **Empty-block elimination.** If MBB contains only debug instructions, redirect all predecessors to the fallthrough successor.
2. **Unconditional-to-same-target folding.** If the previous block's conditional and unconditional branches both target the same block, replace with a single unconditional branch (or fallthrough).
3. **Single-predecessor merge.** If MBB has exactly one predecessor and that predecessor falls through unconditionally, splice MBB's instructions into the predecessor and remove MBB.
4. **Redundant branch removal.** If the previous block branches only to MBB (the natural fallthrough), remove the branch entirely.
5. **Condition reversal.** If the previous block conditionally branches to MBB on true and somewhere else on false, reverse the condition to create a fallthrough.
6. **Tail-block relocation.** If MBB has no successors (return/noreturn) and the predecessor could fall through to the next block instead, move MBB to the end of the function and reverse the predecessor's condition.

Each transformation triggers `goto ReoptimizeBlock` to re-analyze the modified block. Dead blocks (no predecessors after optimization) are removed via `sub_2E790D0` (`RemoveBlock`).

### HoistCommonCode

For each block with exactly two successors, if both successors begin with identical instructions, hoist those instructions into the predecessor. This is the inverse of tail merging -- it reduces code size when two divergent paths start with the same setup sequence. The `EnableHoistCommonCode` flag (always `true` in cicc) controls this phase.

## Data Structures

The `MBBInfo` structure passed via `rdi` to `sub_2F336B0`:

| Offset | Type | Field |
|---|---|---|
| `+0x00` | `MachineFunction*` | Parent function / block list head |
| `+0x08` | `MachineBasicBlock*` | Fallthrough candidate block |
| `+0x10` | `BranchAnalysisResult*` | Cached result from `AnalyzeBranch` |
| `+0x28` | `DenseMap<uint, list>` | Hash-to-candidate-list merge table |

The pass allocates a 792-byte stack frame holding:

| Stack variable | Purpose |
|---|---|
| `var_2E0` | `merge_count` (number of merges performed) |
| `var_309` | `modified` flag |
| `var_30A` | `should_try_fold` flag (initialized to 1) |
| `var_224` | Hash table allocated flag |
| `var_1E4` | Operand table allocated flag |

## Configuration

| Knob | Type | Default | Effect |
|---|---|---|---|
| `disable-branch-fold` | bool | false | Skips the entire pass |
| `enable-tail-merge` | tri-state | unset (uses target default) | Force-enable or disable tail merging |
| `tail-merge-threshold` | unsigned | 150 | Max predecessors considered per merge round; caps `MergePotentials` size |
| `tail-merge-size` | unsigned | 3 | Minimum common tail length (in instructions) to justify a merge |
| `branch-fold-placement` | bool | true | Enables branch folding within MachineBlockPlacement (separate invocation) |
| `ifcvt-branch-fold` | bool | true | Enables branch folding within the if-converter pass |

The `tail-merge-threshold` of 150 exists purely as a compile-time throttle. For a block with N predecessors, the pass performs O(N^2) pairwise comparisons within each hash group. Setting the threshold to 0 effectively disables tail merging for blocks with many predecessors while keeping branch optimization active.

The `tail-merge-size` of 3 is the break-even point: creating a new shared block plus a branch instruction costs roughly 2 instructions of overhead, so merging fewer than 3 common instructions produces no net code-size reduction.

## Function Map

| Address | Identity | Role |
|---|---|---|
| `sub_2F36310` | `BranchFolder::OptimizeFunction` | Pass entry; fixed-point loop |
| `sub_2F336B0` | `BranchFolder::OptimizeBlock` / inner logic | Per-block optimization + tail merge core |
| `sub_2F26260` | `HashEndOfMBB` | Tail hash computation for fast matching |
| `sub_2F31250` | `isBranchFoldable` | Checks if operand represents a foldable branch target |
| `sub_2F33020` | Merge candidate map lookup | Hash table lookup in merge table |
| `sub_2E09D00` | `AnalyzeBranch` | Determines branch type, targets, and conditions |
| `sub_2E0C3B0` | `RemoveBranch` | Removes terminator branch instructions from MBB |
| `sub_2E0F080` | `InsertBranch` | Inserts new branch instruction to redirect flow |
| `sub_2E0A600` | `ReplaceTailWithBranchTo` | Splices tail into shared block, inserts redirect |
| `sub_2E0E0B0` | `ReplaceUsesOfBlockWith` | Updates phi nodes (predecessors) after merge |
| `sub_2E192D0` | `getBlockNumbered` | MBB number to pointer lookup |
| `sub_2FAD510` | `UpdateTerminator` | Fixes terminators after CFG modification |
| `sub_2E790D0` | `RemoveBlock` | Removes dead MBB from function |
| `sub_2E16F10` | `computeLiveIns` | Updates live-in register sets for merged block |
| `sub_2E2B9F0` | `TryTailMergeBlocks` | Attempts merge across candidate set |
| `sub_2EBEE10` | `getVRegDef` | Virtual register definition lookup |
| `sub_2E88A90` | `hasProperty(flag)` | Register/operand property check (flag 0x200 = reserved) |
| `sub_2E89C70` | `HashMachineInstr` / `CompareBlocks` | Instruction hash or block comparison |
| `sub_2E31080` | `SpliceBlock` | Unlinks MBB from doubly-linked list |

## Interaction with StructurizeCFG

StructurizeCFG runs during the IR-level pipeline (before SelectionDAG), while BranchFolding runs after register allocation at the machine level. By the time BranchFolding executes, all control flow is already structured and reducible. The key interaction:

- StructurizeCFG may insert "Flow" blocks that serve as reconvergence points. These are often empty or contain only an unconditional branch. BranchFolding's empty-block elimination (step 1 of `OptimizeBranches`) can remove these if they have become redundant after code generation.
- Tail merging never introduces irreducible control flow because it only adds unconditional branches to a new shared tail block. The new block post-dominates the merged tails, preserving reducibility.
- The `branch-fold-placement` knob controls a separate invocation of branch folding logic embedded within MachineBlockPlacement. That invocation runs *before* the standalone BranchFolding pass and performs a limited subset of the same transformations during layout decisions.

## Complexity

The hash-based matching makes the typical case efficient. For N blocks and average predecessor count M, the overall complexity is O(N * M) for hash computation, plus O(K^2 * T) for pairwise comparison within hash groups, where K is the number of blocks sharing a hash and T is the common tail length. The `tail-merge-threshold` caps K at 150. The recursive self-call pattern (the pass re-invokes itself when a merge creates new opportunities) means worst-case is O(N^2) iterations, but this is rare in practice -- most functions converge in 2-3 iterations.

## Cross-References

- **[Block Placement](block-placement.md)** -- runs before BranchFolding; its `branch-fold-placement` knob triggers inline branch folding during layout.
- **[StructurizeCFG](structurizecfg.md)** -- guarantees structured control flow before BranchFolding runs; inserts Flow blocks that BranchFolding may later eliminate.
- **[Register Allocation](register-allocation.md)** -- BranchFolding requires `NoPHIs` property, meaning it runs post-regalloc in the NVPTX pipeline.
- **[Instruction Scheduling](scheduling.md)** -- scheduling runs after BranchFolding; the final CFG shape from branch folding determines scheduling regions.
