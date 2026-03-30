# BranchFolding & TailMerge

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

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

However, cicc compensates with three safety mechanisms that upstream does not need:

1. **Reserved-register check.** At `0x2F3427B`, the pass calls `sub_2E88A90` with flag `0x200` (`isReservedReg`) on every register live across the proposed merge boundary. NVPTX special registers (`%tid.x`, `%ntid.x`, `%ctaid.x`, etc.) are reserved and cannot be live-in to a newly created shared tail block because their values are implicitly defined by the hardware. If any reserved register is detected, the merge is rejected. See the [Reserved-Register Safety Mechanism](#reserved-register-safety-mechanism) section below for full detail.

2. **Priority ordering for conditional branches.** The pattern `or ecx, 2` at `0x2F33B1C` assigns priority >= 2 to conditional branch terminators and lower priority to unconditional branches. This ensures unconditional-branch tails are merged first, because those merges never alter branch conditions and are always safe within structured CFG. Conditional tail merges are attempted only after unconditional ones are exhausted.

3. **NVPTXInstrInfo vtable validation.** The vtable check at `0x2F337A3` (`cmp rax, offset sub_2DAC790`) verifies that the `TargetInstrInfo` object supports `analyzeBranch` before any merge is attempted. This is a guard against running the pass on a MachineFunction whose `InstrInfo` does not implement branch analysis -- a scenario that cannot occur in the normal NVPTX pipeline but could if the pass were invoked from an unexpected context. The check loads the vtable pointer from `[TII]`, compares against the known `NVPTXInstrInfo` vtable base, and short-circuits to "no merge" if the match fails.

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

#### HashEndOfMBB -- sub_2F26260

The hash function at `sub_2F26260` computes a 32-bit hash of a block's tail for fast merge-candidate matching. The algorithm:

```
HashEndOfMBB(MBB):
    iter = MBB.rbegin()        // last instruction
    // skip debug instructions
    while iter != MBB.rend() && iter.isDebugInstr():
        iter++
    if iter == MBB.rend():
        return 0               // empty block (or all-debug)
    // skip terminator branches -- hash the last non-branch
    while iter != MBB.rend() && iter.isTerminator():
        iter++
    if iter == MBB.rend():
        return 0               // block contains only terminators
    return HashMachineInstr(*iter)
```

`HashMachineInstr` (at `sub_2E89C70`) hashes the instruction's opcode, number of operands, and the first two operands' register/immediate values. It does not hash memory operands or metadata -- this is intentional, because the hash is only used to bucket candidates for pairwise comparison. False collisions are resolved by the subsequent `ComputeCommonTailLength` call. The hash uses a simple multiply-and-XOR scheme:

```
HashMachineInstr(MI):
    h = MI.getOpcode()
    h = h * 37 + MI.getNumOperands()
    if MI.getNumOperands() >= 1:
        h = h * 37 + hashOperand(MI.getOperand(0))
    if MI.getNumOperands() >= 2:
        h = h * 37 + hashOperand(MI.getOperand(1))
    return h
```

The `* 37` constant is standard LLVM hashing (the same multiplier used in `DenseMapInfo`). The hash is deliberately coarse -- it accepts false positives (two different instructions hashing to the same value) but never produces false negatives (two identical instructions hashing differently), which is the correct tradeoff for a merge-candidate filter.

#### ComputeCommonTailLength -- Detailed Binary Walkthrough

The comparison loop at `0x2F33B0F` proceeds as follows:

```
ComputeCommonTailLength(MBB1, MBB2):
    iter1 = MBB1.rbegin()     // walk backwards from end
    iter2 = MBB2.rbegin()
    count = 0

    // skip debug instructions at tails
    skip_debug(iter1, MBB1)
    skip_debug(iter2, MBB2)

    while iter1 != MBB1.rend() && iter2 != MBB2.rend():
        MI1 = *iter1
        MI2 = *iter2

        // extract opcode from [MI + 0x18]
        opc1 = *(uint32_t*)(MI1 + 0x18)
        opc2 = *(uint32_t*)(MI2 + 0x18)

        // reject if either is inline asm (opcode check)
        if is_inline_asm(opc1) || is_inline_asm(opc2):
            break

        // reject if either is a CFI pseudo-instruction
        if is_cfi(opc1) || is_cfi(opc2):
            skip to next non-CFI; continue

        // full comparison: opcode, operand count, each operand
        if !isIdenticalTo(MI1, MI2):
            break

        count++
        iter1++; iter2++
        skip_debug(iter1, MBB1)
        skip_debug(iter2, MBB2)

    return count
```

The `isIdenticalTo` comparison at the binary level extracts fields from the MachineInstr layout:
- `[MI + 0x18]`: opcode (32-bit)
- `[MI + 0x08]`: operand list pointer
- `[MI + 0x10]`: operand count (16-bit at `+0x10`, flags at `+0x12`)
- Each operand at stride 40 bytes: `[operand + 0x00]` = type tag, `[operand + 0x08]` = register/immediate value

Two instructions are identical if and only if: same opcode, same number of operands, and for each operand pair: same type tag and same value. Memory operands (`MachineMemOperand`) are **not** compared -- two loads from different memory locations with the same register operands will compare as identical. This is correct for tail merging because if the instructions are in the tail of two blocks that reach the same successor, their memory operands must be equivalent by construction (they access the same state at the same program point).

#### Merge Candidate Ordering and the Priority System

The `or ecx, 2` pattern at `0x2F33B1C` implements a priority-based ordering within hash groups. When building the `MergePotentials` list, each entry is annotated with a priority value:

| Priority | Condition | Meaning |
|---|---|---|
| 0 | Block ends with unconditional branch only | Safest merge -- no condition changes needed |
| 1 | Block ends with fallthrough (no explicit branch) | Safe -- may need a branch inserted |
| 2+ | Block ends with conditional branch | Riskier -- merge may require condition reversal |

The sort at `TryTailMergeBlocks` sorts first by hash (grouping candidates), then within each hash group by priority (ascending). This ensures that the O(K^2) pairwise comparison within each hash group tries unconditional-only pairs first. If a merge succeeds for a low-priority (safe) pair, the modified block may no longer be a candidate for a higher-priority (conditional) pair, reducing the number of conditional merges attempted.

On NVPTX, this ordering is particularly important because conditional branch reversal (`sub_2E09D00` AnalyzeBranch + condition inversion) can alter the fall-through layout. In a structured CFG, the fall-through direction often corresponds to the "then" path of an if-then-else, and reversing the condition flips which path falls through. While this does not change correctness, it can change the reconvergence point's distance from the branch, affecting I-cache locality. By preferring unconditional-only merges, the pass minimizes layout disruption.

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

## Reserved-Register Safety Mechanism

This section documents the NVIDIA-specific reserved-register check that gates tail merging in cicc. This mechanism has no equivalent in upstream LLVM because upstream disables tail merging entirely for structured-CFG targets.

### Why Reserved Registers Cannot Cross Merge Boundaries

NVPTX "special registers" (`%tid.x`, `%ntid.x`, `%ctaid.x`, `%nctaid.x`, `%laneid`, `%warpid`, and the SM 90+ cluster registers) are not stored in the virtual register file. They are hardware-defined, read-only values whose definitions are implicit -- there is no `MachineInstr` that defines `%tid.x`. Instead, these registers appear as implicit uses on instructions that read thread/block/grid coordinates.

When tail merging creates a new shared tail block `CommonTail`, LLVM's infrastructure computes the live-in set for `CommonTail` from the union of live-outs of the merged predecessors. For a normal virtual register, this is safe: the register has a concrete definition (a `MachineInstr` somewhere in the function), and the live-in annotation tells the downstream passes that the value is available at block entry.

For a reserved register, there is no concrete definition. The value is implicitly available at every point in the function -- it is defined by the hardware thread context, not by any instruction. Creating a new block with a reserved register in its live-in set is semantically meaningless but causes three concrete problems:

1. **LiveIntervals confusion.** The `LiveIntervals` analysis (already computed by this point) has no interval for reserved registers. Adding a live-in for a reserved register to `CommonTail` would require creating a new `LiveInterval` that spans from `CommonTail`'s entry to the last use within `CommonTail`. But reserved registers do not participate in `LiveIntervals` -- they are excluded during interval construction at `sub_2F5A640`. The resulting inconsistency triggers assertions in debug builds and can silently corrupt the interference matrix in release builds.

2. **Register pressure miscounting.** The greedy register allocator tracks pressure per register class. Reserved registers belong to the internal-only class at `off_4A026E0` (the `"!Special!"` class documented in [Register Classes](../reference/register-classes.md)). This class has no encoded ID, no PTX declaration, and is excluded from pressure accounting. If a reserved register appeared as a live-in, the pressure tracker would attempt to look up its class and fail -- or worse, miscount it against one of the nine real classes.

3. **Emission failure.** During PTX emission, `sub_21583D0` (the register encoding function) maps each register to its 4-bit class tag via vtable comparison. Reserved registers use the `off_4A026E0` vtable, which triggers the fatal `"Bad register class"` error. A reserved register in a live-in set could propagate to a point where the emitter attempts to declare it, causing an unconditional abort.

### The hasProperty Check -- sub_2E88A90

`sub_2E88A90` is a multi-purpose property query function used across several subsystems in cicc:

| Call site | Flag | Meaning |
|---|---|---|
| BranchFolding (`0x2F3427B`) | `0x200` | `isReservedReg` -- register is a hardware-defined special register |
| StructurizeCFG (`sub_2E88A90` in structurize) | `0x80000` / `0x100000` | Uniformity/divergence classification |
| InstrEmitter (`sub_2E88A90` in emitter) | `0x1000000000` (bit 36) | NVPTX-specific implicit-use flag |

The function takes three arguments:

```
sub_2E88A90(context_ptr, register_or_operand, flag_mask) -> bool
```

For the BranchFolding call at `0x2F3427B`, the calling convention is:

```
; rdi = TargetRegisterInfo*  (from MachineFunction->getSubtarget().getRegisterInfo())
; esi = register ID           (physical register number from live-in set)
; edx = 0x200                 (isReservedReg flag)
; returns: al = 1 if reserved, 0 if not
```

The function internally indexes into a per-register property table at `[TRI + 0x58]`. This table is initialized during `NVPTXRegisterInfo` construction (`sub_2163AB0` for legacy PM, `sub_30590F0` for new PM) and contains one entry per physical register. Each entry is a 64-bit bitmask of properties. The `0x200` bit (bit 9) is set for every register in the NVPTX special/environment register set.

### Which Registers Are Marked Reserved (flag 0x200)

The following registers have bit 9 (`0x200`) set in the property table and will cause a merge rejection if live across the merge boundary:

| Register Group | PTX Names | Emission Function | Count |
|---|---|---|---|
| Thread ID | `%tid.x`, `%tid.y`, `%tid.z` | `sub_21E86B0` (opcodes `0x26`--`0x28`) | 3 |
| Block dimensions | `%ntid.x`, `%ntid.y`, `%ntid.z` | `sub_21E86B0` (opcodes `0x29`--`0x2B`) | 3 |
| Block ID | `%ctaid.x`, `%ctaid.y`, `%ctaid.z` | `sub_21E86B0` (opcodes `0x2C`--`0x2E`) | 3 |
| Grid dimensions | `%nctaid.x`, `%nctaid.y`, `%nctaid.z` | `sub_21E86B0` (opcodes `0x2F`--`0x31`) | 3 |
| Warp/lane ID | `%warpid`, `%laneid` | `sub_21E86B0` (opcodes `0x5E`--`0x5F`, via `sub_3958DA0`) | 2 |
| Cluster registers (SM 90+) | `%cluster_ctarank`, `%cluster_nctarank`, `%cluster_ctaid.{x,y,z}`, `%cluster_nctaid.{x,y,z}`, `%clusterid.{x,y,z}`, `%nclusterid.{x,y,z}`, `%is_explicit_cluster` | `sub_21E9060` (values 0--14) | 15 |
| Stack pointer | `%SP`, `%SPL` | inline in frame setup | 2 |
| Environment regs | `ENVREG0`--`ENVREG31` | internal (not emitted to PTX) | 32 |

Total: 63 reserved registers. These correspond to the physical register set in NVPTX -- recall that NVPTX has no general-purpose physical registers, so the only physical registers are the special hardware-defined ones plus the stack pointer pair.

The environment registers (`ENVREG0`--`ENVREG31`) are used internally by the CUDA runtime to pass kernel arguments and configuration data. They are read-only from the kernel's perspective and never appear explicitly in emitted PTX. Their presence in the reserved set is a safety measure against internal IR manipulations that might introduce them as explicit operands.

### The Check in Context: Full Merge Decision Sequence

The reserved-register check is the third of four gates in the merge decision path. The complete sequence at `0x2F33B0F`--`0x2F34300` is:

```
MergeDecision(MBB1, MBB2, MinTail):
    // Gate 1: Instruction comparison
    tail_len = ComputeCommonTailLength(MBB1, MBB2)
    if tail_len < MinTail:
        return REJECT

    // Gate 2: Branch analysis feasibility
    ok = AnalyzeBranch(MBB1, ...)
    if !ok:
        return REJECT    // unanalyzable terminator (inline asm, etc.)

    // Gate 3: Reserved-register check (NVPTX-specific)
    for each reg in LiveIns(MBB1[split_point:]) ∪ LiveIns(MBB2[split_point:]):
        if sub_2E88A90(TRI, reg, 0x200):
            return REJECT    // reserved register crosses merge boundary

    // Gate 4: Profitability (code size)
    overhead = 1     // one branch instruction to CommonTail
    if MBB1 needs UpdateTerminator:
        overhead += 1
    if tail_len <= overhead:
        return REJECT    // no net code-size reduction

    return ACCEPT
```

Gate 3 iterates every register that would be live-in to the proposed `CommonTail` block. The live-in set is computed by walking the tail instructions backwards and collecting register uses that have no definition within the tail. If any register in this set has the `0x200` property, the entire merge is rejected -- there is no fallback or partial merge.

### Interaction with computeLiveIns -- sub_2E16F10

After a merge is accepted and the `CommonTail` block is created, `sub_2E16F10` (`computeLiveIns`) populates the new block's live-in set. This function must agree with the pre-merge reserved-register check: if the check passed (no reserved registers), then `computeLiveIns` will produce a live-in set containing only virtual registers and non-reserved physical registers. The function at `sub_2E16F10` performs its own filtering:

```
computeLiveIns(CommonTail):
    for each reg in upward_exposed_uses(CommonTail):
        if isReserved(reg):
            continue    // redundant safety -- already filtered by Gate 3
        addLiveIn(CommonTail, reg)
```

The double-check (once in the merge decision, once in `computeLiveIns`) is a defense-in-depth pattern. The merge decision check prevents the merge from happening at all; the `computeLiveIns` filter prevents a reserved register from entering the live-in set even if the merge decision check were somehow bypassed (e.g., by a future code change that added a new merge path).

## GPU-Specific Considerations

### Tail Merging and Warp Divergence

Tail merging on GPU does not interact with warp divergence in the way that branch *duplication* does. When two blocks `A` and `B` both end with the same instruction sequence and share a common successor `C`, merging the tails into a shared `CommonTail` block that falls through to `C` does not change which warps execute which instructions. Every warp that previously executed the tail of `A` now executes the same instructions in `CommonTail`; similarly for `B`. The branch from `A` (or `B`) to `CommonTail` is unconditional and therefore non-divergent by definition.

However, there is one subtle interaction: if `A` and `B` are the two sides of a divergent branch, and the tail merge creates `CommonTail` between them and their common successor `C`, the reconvergence point may shift. Previously, warps reconverged at `C`'s entry. After the merge, warps reconverge at `CommonTail`'s entry -- which is equivalent but changes the block numbering. StructurizeCFG has already inserted any necessary reconvergence tokens before BranchFolding runs, and those tokens are block-relative. The `UpdateTerminator` call at `sub_2FAD510` and the `ReplaceUsesOfBlockWith` call at `sub_2E0E0B0` update all references, so the reconvergence semantics are preserved.

### Code Size vs. Instruction Cache

On GPU, the primary motivation for tail merging is code size reduction, which translates directly to reduced instruction cache pressure. NVIDIA GPUs have small instruction caches per SM partition (32--128 KB depending on architecture generation). Tail merging reduces the number of unique instructions the I-cache must hold.

The `tail-merge-size` default of 3 reflects the GPU's branch cost: one `bra` instruction to redirect flow to `CommonTail`, plus one additional instruction if the predecessor's terminator needs rewriting. With a minimum tail length of 3, the merge always saves at least one instruction's worth of I-cache footprint. On a GPU where each instruction occupies 8--16 bytes (PTX instructions vary in encoding width, but `ptxas` expands them to fixed-width SASS), a 3-instruction merge saves 24--48 bytes of I-cache per merge site.

The `tail-merge-threshold` of 150 is generous compared to upstream LLVM's default (also 150 in upstream, but upstream disables the entire mechanism for GPU targets). In practice, GPU kernels rarely have blocks with 150+ predecessors -- the threshold exists primarily to prevent pathological compile times on machine-generated code with massive switch tables.

### Structured CFG Preservation Proof

The claim that tail merging preserves structured (reducible) control flow deserves a rigorous argument, since this is the justification for NVIDIA removing the `requiresStructuredCFG()` gate.

**Claim:** If the input CFG is reducible, then the CFG after tail merging is also reducible.

**Proof sketch:** Tail merging performs one operation: it takes two blocks `A` and `B` that share a common tail instruction sequence, creates a new block `T` containing the tail, and replaces the tail portions of `A` and `B` with unconditional branches to `T`. The successors of `A` and `B` in the tail (which were the same for both, by construction) become successors of `T` instead.

Consider the back-edge structure. In a reducible CFG, every cycle has a single entry point (the loop header). Tail merging cannot create a new cycle because:
- `T` is a new block with no incoming edges except from `A` and `B`.
- `T`'s outgoing edges are a subset of the original outgoing edges of `A` and `B`'s tails.
- No edge into `T` can form a back-edge of an existing cycle unless `A` or `B` was already a back-edge target, in which case the cycle's entry point was `A` or `B`, not `T`.
- The only new edges are `A->T` and `B->T` (unconditional). These cannot create a new cycle because `T` does not dominate `A` or `B` (it was just created).

Therefore, no new irreducible cycle is introduced. The `disable-nvptx-require-structured-cfg` knob (at `qword_5022CC8` in `NVPTXTargetMachine`) provides a backdoor to disable the structured-CFG requirement entirely, but it is `false` by default and should never be set in production.

### Interaction with EH and Cleanup Pads

NVPTX does not support C++ exceptions in the traditional sense -- there is no stack unwinding on GPU. However, cicc does handle cleanup semantics for CUDA cooperative groups and destructor calls. The branch folding pass skips blocks that are EH landing pads (`isEHPad()` check at the start of `OptimizeBlock`). On NVPTX, this check is typically a no-op because no blocks are marked as EH pads, but the check remains active because the same binary serves both CUDA and non-CUDA compilation paths.

### Interaction with Convergence Control Tokens

On SM 90+ (Hopper and later), cicc emits convergence control pseudo-instructions (`bra.convergent`, `.pragma "convergent"`) that are consumed by `ptxas` to guide reconvergence behavior. These pseudo-instructions are MachineInstrs with specific opcodes that BranchFolding must not merge or reorder. The `isIdenticalTo` comparison in `ComputeCommonTailLength` considers opcode, operands, and flags, so two convergence control instructions with different target blocks will not compare as identical and will naturally terminate the common-tail scan. This prevents the tail merger from accidentally merging convergence annotations that belong to different reconvergence points.

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

| Address | Size | Identity | Role |
|---|---|---|---|
| `sub_2F36310` | -- | `BranchFolder::OptimizeFunction` | Pass entry; fixed-point loop over TailMerge + OptimizeBranches + HoistCommonCode |
| `sub_2F336B0` | 11,347B | `BranchFolder::OptimizeBlock` / inner logic | Per-block optimization + tail merge core (792-byte stack frame) |
| `sub_2F26260` | -- | `HashEndOfMBB` | Tail hash computation; hashes last non-debug non-terminator instruction |
| `sub_2F31250` | -- | `isBranchFoldable` | Checks if operand represents a foldable branch target |
| `sub_2F33020` | -- | Merge candidate map lookup | Hash table lookup in `MergePotentials` DenseMap |
| `sub_2E2B9F0` | -- | `TryTailMergeBlocks` | Attempts merge across candidate set; calls Gates 1--4 |
| `sub_2E09D00` | -- | `AnalyzeBranch` | NVPTXInstrInfo branch analysis: type, targets, conditions |
| `sub_2E0C3B0` | -- | `RemoveBranch` | Removes terminator branch instructions from MBB |
| `sub_2E0F080` | -- | `InsertBranch` | Inserts new branch instruction to redirect flow |
| `sub_2E0A600` | -- | `ReplaceTailWithBranchTo` | Splices tail into shared block, inserts unconditional redirect |
| `sub_2E0E0B0` | -- | `ReplaceUsesOfBlockWith` | Updates phi nodes and predecessor lists after merge |
| `sub_2E192D0` | -- | `getBlockNumbered` | MBB number to pointer lookup |
| `sub_2FAD510` | -- | `UpdateTerminator` | Fixes terminators after CFG modification |
| `sub_2E790D0` | -- | `RemoveBlock` | Removes dead MBB from function; updates predecessor/successor lists |
| `sub_2E16F10` | -- | `computeLiveIns` | Updates live-in register sets for merged block; filters reserved registers |
| `sub_2EBEE10` | -- | `getVRegDef` | Virtual register definition lookup |
| `sub_2E88A90` | -- | `hasProperty(flag)` | Multi-purpose register/operand property query (flag `0x200` = reserved, `0x80000` = uniform, `0x100000` = divergent) |
| `sub_2E89C70` | -- | `HashMachineInstr` | Instruction hash for merge candidate bucketing (`* 37` multiply-XOR scheme) |
| `sub_2E31080` | -- | `SpliceBlock` | Unlinks MBB from doubly-linked list |
| `sub_2DAC790` | -- | `NVPTXInstrInfo` vtable | Vtable base checked at `0x2F337A3` to validate InstrInfo supports analyzeBranch |
| `sub_3958DA0` | -- | Dynamic special register resolver | Resolves opcodes `0x5E`/`0x5F` to `%warpid`/`%laneid` |
| `sub_21E86B0` | -- | Special register emission | Emits `%tid`, `%ctaid`, `%ntid`, `%nctaid` (opcodes `0x26`--`0x31`) |
| `sub_21E9060` | -- | Cluster register emission (SM 90+) | Emits 15 cluster registers (`%cluster_ctarank`, `%clusterid`, etc.) |

## Interaction with StructurizeCFG

StructurizeCFG runs during the IR-level pipeline (before SelectionDAG), while BranchFolding runs after register allocation at the machine level. By the time BranchFolding executes, all control flow is already structured and reducible. The key interaction:

- StructurizeCFG may insert "Flow" blocks that serve as reconvergence points. These are often empty or contain only an unconditional branch. BranchFolding's empty-block elimination (step 1 of `OptimizeBranches`) can remove these if they have become redundant after code generation.
- Tail merging never introduces irreducible control flow because it only adds unconditional branches to a new shared tail block. The new block post-dominates the merged tails, preserving reducibility.
- The `branch-fold-placement` knob controls a separate invocation of branch folding logic embedded within MachineBlockPlacement. That invocation runs *before* the standalone BranchFolding pass and performs a limited subset of the same transformations during layout decisions.

## Complexity

The hash-based matching makes the typical case efficient. For N blocks and average predecessor count M, the overall complexity is O(N * M) for hash computation, plus O(K^2 * T) for pairwise comparison within hash groups, where K is the number of blocks sharing a hash and T is the common tail length. The `tail-merge-threshold` caps K at 150. The recursive self-call pattern (the pass re-invokes itself when a merge creates new opportunities) means worst-case is O(N^2) iterations, but this is rare in practice -- most functions converge in 2-3 iterations.

## Differences from Upstream LLVM

| Aspect | Upstream LLVM 20 | cicc v13.0 |
|---|---|---|
| **Tail merge for structured-CFG targets** | Disabled (`requiresStructuredCFG()` returns true -> tail merge off) | Enabled -- structured-CFG gate removed |
| **Reserved-register merge gate** | Not present (unnecessary -- tail merge disabled for GPU) | Gate 3: `sub_2E88A90` with flag `0x200` rejects merges when special registers are live across the boundary |
| **Priority ordering** | Candidates sorted by hash only | Additional priority sort within hash groups: unconditional branches first (priority 0), then conditional (priority 2+) |
| **NVPTXInstrInfo vtable check** | Not present | `cmp rax, offset sub_2DAC790` at `0x2F337A3` validates InstrInfo before merge attempts |
| **computeLiveIns filtering** | No reserved-register filter | Double-filters reserved registers (once at merge decision, once at live-in computation) |
| **Convergence control awareness** | Not present (no convergence tokens in upstream) | `isIdenticalTo` naturally prevents merging convergence pseudo-instructions with different targets |
| **MachineInstr stride** | 32-byte operand stride | 40-byte operand stride (extra 8 bytes for NVPTX-specific metadata) |
| **Upstream source** | `llvm/lib/CodeGen/BranchFolding.cpp` | Binary at `0x2F336B0`--`0x2F36310` range |

## Cross-References

- **[Block Placement](block-placement.md)** -- runs before BranchFolding; its `branch-fold-placement` knob triggers inline branch folding during layout.
- **[StructurizeCFG](structurizecfg.md)** -- guarantees structured control flow before BranchFolding runs; inserts Flow blocks that BranchFolding may later eliminate. Uses the same `sub_2E88A90` for divergence queries.
- **[Register Allocation](register-allocation.md)** -- BranchFolding requires `NoPHIs` property, meaning it runs post-regalloc in the NVPTX pipeline. The greedy RA at `sub_2F5A640` excludes reserved registers from pressure tracking.
- **[Instruction Scheduling](scheduling.md)** -- scheduling runs after BranchFolding; the final CFG shape from branch folding determines scheduling regions.
- **[Register Classes](../reference/register-classes.md)** -- documents the internal-only `off_4A026E0` class (`"!Special!"`) that holds reserved/environment registers. The register encoding function `sub_21583D0` fatally aborts on this class.
- **[PTX Emission](../pipeline/emission.md)** -- special register emission functions `sub_21E86B0` and `sub_21E9060` that handle the 63 reserved registers.
- **[NVPTX Target Infrastructure](../infra/nvptx-target.md)** -- the `disable-nvptx-require-structured-cfg` knob that controls the structured-CFG requirement.
- **[Machine-Level Passes](machine-passes.md)** -- pipeline context showing BranchFolding's position after register allocation and before instruction scheduling.
- **[InstrEmitter](instr-emitter.md)** -- another consumer of `sub_2E88A90` that uses flag bit 36 for NVPTX-specific implicit-use detection.
