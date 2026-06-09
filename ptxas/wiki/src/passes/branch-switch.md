# Branch & Switch Optimization

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Four phases in the ptxas pipeline transform branch and switch-statement control flow in the Ori IR. Two phases optimize switch statements (phases 14 and 30), one performs general branch simplification (phase 15), and one flattens nested conditional branches (phase 38). Together they reduce branch count, eliminate unreachable code, and prepare the CFG for downstream passes like predication (phase 63), liveness analysis (phase 16), and loop canonicalization (phase 18).

These phases operate on the Ori IR before register allocation and scheduling. At this pipeline stage, branch instructions use the Ori `OEN` opcode (SASS `BRA`), conditional execution is controlled by predicate registers (P0--P6, PT), and the CFG is a hash-map-based structure with FNV-1a-keyed successor/predecessor edges.

| | |
|---|---|
| **DoSwitchOptFirst** | Phase 14 — vtable at `off_22BD7F8` |
| **OriBranchOpt** | Phase 15 — vtable at `off_22BD820` |
| **DoSwitchOptSecond** | Phase 30 — vtable at `off_22BDA78` |
| **OptimizeNestedCondBranches** | Phase 38 — vtable at `off_22BDBB8` |
| **Phase factory** | `sub_C60D30` cases 14, 15, 30, 38 |
| **Phase object size** | 16 bytes (standard `{vtable_ptr, allocator_ptr}`) |
| **IR level** | Ori — SASS opcodes with virtual registers |
| **Key opcodes** | `OEN` (BRA), `OFFL` (BSSY), `OFLAP` (BSYNC) |
| **CFG infrastructure** | FNV-1a hash maps at Code Object +648 (successors), +680 (backedges) |
| **Related passes** | 31 `OriLinearReplacement`, 63 `OriDoPredication`, 80 `ExpandJmxComputation`, 133/136 `MergeEquivalentConditionalFlow` |

## Pipeline Placement

```text
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

The second pass (phase 30) runs after loop unrolling (phase 22), strength reduction (phase 21), SSA phi insertion (phase 23), and software pipelining (phase 24). These transformations can expose new switch patterns — particularly after loop unrolling duplicates switch bodies, creating opportunities for case clustering that were not visible before.

Despite their names, the two passes use **different dispatch paths**. Phase 14 dispatches through the SM backend's vtable at offset +136 (`*(*(ctx+1584)+136)`), making it a polymorphic, architecture-specific switch optimization. Phase 30 calls the generic switch optimization core (`sub_77CF40` via `sub_791F00`). This means phase 14 runs whatever switch optimization the current SM target provides, while phase 30 always runs the generic algorithm. The two passes share pipeline position semantics (first pass vs. second pass) but not necessarily the same code.

### Phase 14 Vtable Slot 17 — Per-SM Dispatch Resolution

The vtable call at offset +136 (slot 17) resolves to three distinct handlers depending on the SM backend's construction case in `sub_662920`. Resolved from the binary by reading each SM backend vtable at file offset `(vtable_VA + 136 - 0x400000)`:

| SM Backend | Vtable | Slot 17 Handler | Behavior |
|---|---|---|---|
| SM30 (Kepler) | `off_2029DD0` | `sub_661210` (2B) | **NO-OP** (`rep ret`) |
| SM50 (Maxwell) | `off_21B4A50` | `sub_661210` (2B) | **NO-OP** (`rep ret`) |
| SM60 (Pascal) | `off_22B2A58` | `sub_849110` (613B) | Sync-pattern restructuring + branch scan |
| SM70 (Volta/Turing) | `off_21D82B0` | `sub_849110` (613B) | Sync-pattern restructuring + branch scan |
| SM80 (Ampere) | `off_21B2D30` | `sub_729160` (546B) | Per-instruction branch-distance rewriter |
| SM89 (Ada) | `off_21C0C68` | `sub_729160` (546B) | Per-instruction branch-distance rewriter |
| SM90+ (Hopper/Blackwell) | `off_21D6860` | `sub_849110` (613B) | Sync-pattern restructuring + branch scan |

Phase 14 is a **no-op on Kepler and Maxwell** — those legacy architectures rely entirely on the generic phase 30 path. On all active targets (sm_75+), one of two real implementations runs.

**Handler A: `sub_849110` (Pascal, Volta/Turing, Hopper/Blackwell).** Two-phase operation:

1. **Sync-pattern restructuring.** Gated by `ctx+1384` bit 4 AND `ctx+896 == 5`. Queries knob 232 for disable. When enabled, `sub_848F40` iterates over instructions matching opcode 98 (sync-stack entries), identifies sync patterns that can be restructured into simpler branch patterns, rewrites them via `sub_934630` (instruction creation) and `sub_9253C0` (instruction deletion), and collects modified blocks into a worklist.
2. **Branch-target scan.** Gated by `backend+1400 != 0`. Calls `sub_7E6090` (the same branch pattern scanner used by phase 15), then iterates over the instruction linked list. For each instruction with opcode 52 or 97, calls `sub_A9D9B0` which walks operands backward, identifies branch-target operands (type 5, verified by `sub_7DEB50`), and dispatches through `*(backend+1184)` to perform target-specific branch rewriting. Knob 351 limits iteration count.

**Handler B: `sub_729160` (Ampere, Ada).** A single-pass per-instruction classifier. Takes an instruction pointer as argument, extracts the opcode at `instr+72` (masked with `& 0xFFFFCFFF`), and returns a branch-distance category:

| Opcode (masked) | Category | Return value |
|---|---|---|
| 22 (CALL) | Complex control flow | `sub_7E40E0` result |
| 50 (BSSY) | Sync-stack push | Lookup from `xmmword_21B2EC0` table |
| 51 (CAL), 110--111 (BRK/CONT), 289 | Simple control flow | 3 |
| 77 (BRA) | Branch | `sub_7E36C0` (5-field distance metric) |
| 83 (RET) | Return | `sub_7E3640` result |
| 112 (BRX) | Indexed branch | 4 |
| 279 | Specific control flow | 6 |
| 297 (JMXU), 352 (EXIT) | Jump/exit | `sub_7E3790` / `sub_7E3800` result |
| Other | Unhandled | -1 (0xFFFFFFFF) |

After classification, the handler scans forward through operands checking register class membership (class 6 or 3), then optionally invokes `sub_687C00` to rewrite the branch target based on distance thresholds read from knob offsets 44280 and 44208 on the knob container. This replaces near branches with short-encoding variants when the target is within range — an Ampere/Ada-specific optimization that exploits shorter branch encodings available on those architectures.

## DoSwitchOpt — Switch Statement Optimization (Phases 14, 30)

### Overview

`DoSwitchOpt` transforms high-level switch statements from their initial representation as cascading conditional branches into one of three lowered forms, selected based on case density and count. The input is a chain of `ISETP` (integer set-predicate) + `BRA` (conditional branch) instruction pairs that compare the switch selector against successive case constants. The output is one of:

1. **Jump table** — a contiguous array of branch targets indexed by the selector value
2. **Binary search tree** — a balanced tree of comparisons that narrows the target in O(log n)
3. **Cascading if-else chain** — the original form, retained when the switch is small or sparse

### Input: Switch Pattern Recognition

The pass scans each basic block for a characteristic pattern:

```asm
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

#### Scanner Tolerance (`sub_77CF40`, lines 234--411)

The pattern recognizer is not a rigid ISETP+BRA pair matcher. It walks the instruction linked list from the block head at `code_object+280` and tolerates several forms of non-ideal input. The scanning loop in `sub_77CF40` (4698 bytes) processes each instruction through a three-stage filter:

**Stage 1 — Control-flow skip.** `sub_7DF3A0` reads the masked opcode (`*(instr+72) & 0xFFFFCFFF`) and returns a per-opcode property pointer. If bit 0 of that property is set (control-flow instruction), the instruction is skipped without terminating the chain. This is how interleaved `BRA` instructions are consumed: they are classified as control-flow and silently stepped over while the scanner continues looking for comparison instructions.

**Stage 2 — Chain-terminating opcodes.** When parameter `a3` is set (second-pass mode), the scanner reads the first instruction of the successor block (`*(*(block+8))+72`). If that unmasked opcode is 159 (sm_73+ control), 32 (`VABSDIFF4`), or 271 (warpgroup arrive/wait), the chain terminates with a `found_chain` flag set. These mark block boundaries that cannot be part of a switch cascade — convergence points, GMMA synchronization, or SIMT barriers.

**Stage 3 — Accepted comparison opcodes.** Instructions surviving stages 1--2 are collected into the case set. The block number (field +144) is recorded, and the masked opcode determines acceptance:

| Masked opcode | SASS mnemonic | Acceptance condition |
|---------------|---------------|----------------------|
| 96 | `LDG` | Unconditional — any `LDG` in the chain is accepted |
| 190 | (sm_82+ extended) | `sub_745D80`: operand type bits 28--30 == 6, value 1--3 |
| 31 | `VABSDIFF` | Last source operand type bits 5--7 == 2 (immediate constant) |
| (other) | — | Falls through to operand scan without immediate acceptance |

**Operand backward scan.** After collection, the inner loop walks the operand array from index `operand_count - 1` down to 0 (operands at `instr + 8*idx + 84`). Two helpers classify each operand:

- `sub_7DEB50` (14B): returns true when the register descriptor's class == 16 (predicate register). Identifies which predicate each comparison writes.
- `sub_7DEAD0` (19B): returns true for a constant-zero predicate definition — class 16, sub-type bits 10--12 == 4, value == 0. Identifies the PT (always-true) guard operand.

The scan terminates on a negative operand word (sentinel) or operand exhaustion.

**Key tolerance properties:**

1. **Interleaved non-ISETP instructions** do not break the chain. Any non-control-flow instruction is collected as a potential case member; strict ISETP+BRA alternation is not required.
2. **Predicate register changes mid-chain** are permitted. Each comparison's predicate is identified independently by the backward operand scan. The chain can use P0 for one case and P1 for another.
3. **Non-contiguous case constants** are accepted by the scanner. Density evaluation happens downstream in the strategy selector, not in the recognizer.
4. **The chain terminates** when: (a) the instruction list is exhausted (null next pointer), (b) a chain-stopping opcode appears in the successor block (opcodes 159/32/271), or (c) the block-end cleanup at `LABEL_155` fires when `*(block+164) == *(entry_block+164)` (matching block group IDs).

### Decision: Strategy Selection

The strategy selection is not implemented as a simple threshold comparison against hardcoded constants. Instead, `sub_77CF40` uses the bitmap population in the `BitsetRBTree` case collection to determine the lowering strategy. The decision emerges from the structure of the collected cases:

- **Cascading if-else** is retained when the pattern recognizer collects few cases (the bitmap contains a single 256-bit leaf or sparse coverage). The pass checks whether the case set justifies the overhead of an indirect branch structure.
- **Jump table** is selected when the bitmap shows dense coverage within a contiguous value range. The density is implicit in the bitmap layout: if most bits in a range are set, the table-based lowering is preferred.
- **Binary search tree** is the fallback for cases that are numerous but too sparse for a jump table.

Two OCG knobs (registered in `ctor_005`) influence the strategy selection:

| Knob | ROT13 | VA | Type | Semantics |
|------|-------|----|------|-----------|
| `SwitchOptBudget` | `FjvgpuBcgOhqtrg` | `0x21B7CE0` | integer (03) | Limits the optimization effort budget — controls how many cases or how much analysis the pass performs before falling back to the default BST strategy |
| `SwitchOptExtendedPatternMatch` | `FjvgpuBcgRkgraqrqCnggreaZngpu` | `0x21B7CB0` | boolean (02) | When enabled, the pattern recognizer accepts interleaved instruction patterns beyond the simple `ISETP`+`BRA` pair, allowing it to match switches that have been partially transformed by earlier passes |

The exact default values of these knobs are set by the OCG knob initialization system (`sub_79D990`) and are not visible as immediate constants at the registration site. The knob names follow the standard ROT13 obfuscation convention. Neither knob is checked by the `sub_7DDB50` opt-level gate — they are consumed directly within the switch optimization algorithm in `sub_77CF40` and its callees.

**Jump table** is preferred when case values are dense — the selector maps directly to a table index with a bounds check and a subtraction. This produces the fastest code but consumes data memory proportional to the value range (not the case count).

**Binary search tree** is the default for large sparse switches. The pass sorts case constants and generates a balanced BST of `ISETP` + `BRA` pairs. Each comparison eliminates half the remaining candidates, reaching the target in O(log N) branches.

**Cascading if-else** is retained for small switches (typically 4 or fewer cases) where the overhead of a jump table or BST setup exceeds the cost of linear comparison.

### Output: Jump Table Lowering

For jump-table-eligible switches, the pass produces:

```asm
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

Phase 80 (`ExpandJmxComputation`) runs much later (after legalization) to expand the index computation pseudo-instruction into the final `LEA` + `BRX` sequence. The delayed expansion exists because the table base address is unknown until final code layout, and intervening optimization passes (GVN-CSE, strength reduction) may simplify the index computation while it remains a single pseudo-instruction.

#### Jump Table Data Structure

The jump table is embedded inline in the `.text` section, immediately after the `BRX` instruction that references it. `BRX` is a 64-bit (8-byte) format instruction (format code `0x1`), so the table begins at the next 4-byte-aligned position after the instruction word. The `.text` layout around a jump table:

```text
.text offset    contents
────────────    ─────────────────────────────────────────────
base            [ctrl]  LEA Raddr, Rtmp, #table_base, 2
base + 0x08    [ctrl]  BRX Raddr, #table_base
base + 0x10    table[0]   i32 offset -> case min_val target
base + 0x14    table[1]   i32 offset -> case min_val+1 target
  ...
base + 0x10 + 4*(range-1)   i32 offset -> case max_val target
```

**Entry format.** Each entry is a signed 32-bit (4-byte) relative offset. The hardware computes the branch target as `table_base + table[index]`, where `table_base` is the constant immediate encoded in the `BRX` instruction. Offsets are relative to the table base, not to the individual entry position.

**Sparse fill policy.** The table has exactly `range = max_val - min_val + 1` entries. Case values present in the switch map to the offset of their case body. Absent values (gaps in a sparse switch) are filled with the relative offset to the **default target**. The bounds check (`ISETP.GE.U32` + `BRA`) preceding the lookup handles values outside `[min_val, max_val]`; the default fill handles gaps inside the range. Together they guarantee every selector value reaches either its case body or the default.

**Alignment.** No explicit padding is inserted between `BRX` and the table. The table starts at the byte immediately following the 8-byte `BRX` instruction. SASS `.text` instructions are naturally aligned to their encoding width (8 or 16 bytes), so the first table entry is always at least 4-byte aligned.

**Relocation.** The `EIATTR_JUMPTABLE_RELOCS` attribute (code `0x03`) in `.nv.info.<func>` records an array of `u32` byte offsets into `.text` where jump table data begins. Each offset points to `table[0]` of a jump table. The linker (nvlink) uses these offsets to patch table entries when the final code layout changes during cross-module linking. See [EIATTR Reference](../reference/eiattr.md#jump-table-relocations-3).

### Output: Binary Search Tree Lowering

#### Case Sort via BitsetRBTree

Before BST emission, `sub_77CF40` builds a sorted set of case block indices using a red-black tree with 256-bit bitmap leaves (`BitsetRBTree`, managed by `sub_7436F0`). Each RBT node is 64 bytes:

```text
BitsetRBTreeNode (64 bytes):
    +0   left child pointer
    +8   right child pointer
    +16  parent pointer
    +24  key (int32) -- RPO_index >> 8 | color bit in MSB
    +32  bitmap[4]   -- 4 x uint64 = 256 bits, one per low-8-bit position
```

Insertion (`sub_7436F0`) decomposes a case's RPO block index (from block offset +144) into `key = index >> 8` and `bit = index & 0xFF`, locates or creates the RBT node keyed by the high bits, then sets `bitmap[bit >> 6] |= (1 << bit)`. If the node already exists (same key), the bit is OR'd into the existing bitmap. The RBT maintains sorted order by key, and `sub_6887C0` rebalances after insertion.

The BST emission driver (LABEL_46 in `sub_77CF40`) iterates the sorted set in **descending** order. `sub_758210` initializes an iterator pointing to the highest set bit of the last RBT node. Each step extracts the current bit position via `_BitScanReverse64`, computes a composite index `(bit_position) | (rbt_key << 6)`, removes the bit via `sub_7654E0`, and processes the corresponding case block at `SwitchOptContext+104 + 40 * rpo_index`. This yields cases in descending RPO order without an explicit sort pass — the RBT provides O(log N) insertion and inherently sorted traversal.

#### PGO Early-Exit Guard

The entry of `sub_77CF40` checks three code-object fields before proceeding:

```c
if code_obj[1404] != 0        // PGO profile data is attached
   && code_obj[1400] == 0     // instrumentation pass counter = 0 (complete)
   && (code_obj[1377] & 0x08) == 0:   // scheduling-mode bit 3 is clear
    return                     // skip switch optimization entirely
```

When profile-guided optimization data is present and fully processed, the pass exits without modifying any switch statements. PGO-guided code layout has already arranged case blocks by execution frequency, making static BST rebalancing counterproductive — the profiled block ordering already minimizes expected branch depth for the observed workload.

Inside the main loop, PGO also affects the block reachability check (`sub_76ABE0`). When `code_obj[1370] & 0x40` is set (PGO block-frequency data available), the function uses a fast bitmask lookup at `block+176` instead of querying the profile object at `code_obj+1168` via `sub_76AAE0`. This bitmask records which successor blocks were observed as reachable during profiling, allowing unreachable-case pruning before BST construction.

#### BST Emission Algorithm

For BST-eligible switches:

```c
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

## OriBranchOpt — Branch Simplification (Phase 15)

### Overview

`OriBranchOpt` performs four categories of CFG-level simplification on the Ori IR. It runs as a single pass that iterates over all basic blocks and applies the following transformations until no further changes occur:

1. **Unconditional branch folding** — eliminates `BRA` instructions that jump to the immediately following block
2. **Unreachable block elimination** — removes basic blocks with no predecessors (except the entry block)
3. **Conditional branch simplification** — simplifies conditional branches where the condition is provably constant or the true/false targets are identical
4. **Branch chain threading** — redirects branches that target blocks consisting of a single unconditional `BRA`, directly to the final destination

### Transformation 1: Unconditional Branch Folding

When a basic block ends with an unconditional `BRA` to the block that immediately follows in layout order, the branch is redundant and is deleted:

```text
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

The deletion is performed by `sub_BDE6C0` (`CFGHashMap::erase`, 3KB), a recursive function called from `sub_BDFB10` (`CFG::buildBlockMap`, 24KB). The caller first resizes the RPO array (+720) and a parallel array (+744) to `num_blocks + 1` entries, then loops over all block indices `0..num_blocks` invoking the erase function for each.

The erase function operates on a CFG context object that contains three parallel arrays indexed by block index:

| Context Offset | Type | Field |
|----------------|------|-------|
| +16 (`a1[2]`) | `int*` | `pred_count[]` — predecessor count per block |
| +64 (`a1[8]`) | `int*` | `rpo_number[]` — RPO position per block |
| `*a1 + 720` | `int*` | `rpo_to_bix[]` — maps RPO position to block index |

```c
function erase_block(ctx, bix, counter):       // sub_BDE6C0
    if pred_count[bix] == 0:                   // already removed
        return
    pred_count[bix] = 0                        // mark deleted first
    code_obj = ctx[0]
    succ_map = *(code_obj + 648)               // FNV-1a hash map
    if succ_map == NULL or succ_map.total_elements == 0:
        goto finalize

    bucket = fnv1a(bix) & (succ_map.num_buckets - 1)
    node = succ_map.bucket_array[bucket].head
    while node != NULL:
        if node.key != bix:
            node = node.next; continue

        // Found this block's successor edge set.
        // Walk sub-hash buckets for multi-successor blocks:
        for each successor S in node's edge set:
            if S == 0xFFFFFFFF: skip            // sentinel
            if pred_count[S] == 1:              // sole predecessor is bix
                erase_block(ctx, S, counter)    // recursive cascade
        break

    finalize:
    rpo_number[bix] = *counter                  // reassign RPO slot
    rpo_to_bix[*counter] = bix                  // inverse mapping
    (*counter)--                                // consume one slot
```

Key design decisions confirmed from the binary:

1. **Predecessor count zeroed first, successor map walked but not modified.** The erase function clears `pred_count[bix]` immediately (line `*result = 0` at 0xBDE6F2), then reads the successor edge map at +648 to find cascade targets. Successor hash map nodes are never unlinked — the stale entries remain in the bucket chains. The `pred_count == 0` sentinel is the authoritative "deleted" marker.

2. **Recursive cascade for single-predecessor successors.** For each successor `S`, the function checks `pred_count[S] == 1` (at 0xBDE7DF: `cmp dword ptr [rdx+rax*4], 1`). If the sole predecessor of `S` is the block being erased, `S` itself is unreachable and is recursively erased. This propagates deletion forward through chains of single-predecessor blocks without requiring a separate worklist.

3. **RPO arrays updated in deletion order.** After the successor walk, the erased block is assigned the current counter value into both the per-block RPO number array (`rpo_number[bix] = *counter`) and the reverse RPO-to-block-index array (`rpo_to_bix[*counter] = bix`), then the counter is decremented. These writes go to Code Object +720 (via offset 0x2D0 = 720) and the context's RPO number array at offset +64.

4. **Rebuild phase follows.** After `sub_BDFB10` completes the per-block erase loop, it re-walks all blocks, looks up each surviving block's successors in the +648 hash map, and repopulates the backedge map at +680 via `sub_BDF480` and successor entries via `sub_BDED20`. This two-phase approach (erase-then-rebuild) avoids incremental hash map maintenance during the deletion cascade.

### Transformation 3: Conditional Branch Simplification

Two sub-cases:

**Constant condition.** If copy propagation or constant folding (in the preceding `GeneralOptimizeEarly`, phase 13) has determined that a predicate register always holds a known value at the branch point, the conditional branch is replaced:

```text
// Before: condition always true      // After:
BB:                                   BB:
    ISETP.EQ PT, R0, R0              //   (deleted -- tautology)
    @PT BRA target                        BRA target   // unconditional
    BRA fallthrough                   //   (deleted)
```

**Equivalent targets.** If both the taken and not-taken paths of a conditional branch go to the same block, the condition test is dead and the branch becomes unconditional:

```text
// Before: both targets identical     // After:
BB:                                   BB:
    @P0 BRA target                        BRA target   // unconditional
    BRA target                        //   (deleted)
```

### Transformation 4: Branch Chain Threading

When a branch targets a block whose only content is another unconditional branch, the pass redirects the original branch directly to the final target:

```text
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

The execute body (`sub_7917F0`) drives the four transformations through a two-level loop over the RPO block ordering. The algorithm from the decompiled 529-byte body:

```c
function OriBranchOpt_execute(ctx):
    // --- gate checks (knobs 214, 487; CFG validity bit at ctx+1382) ---

    // Phase 1: infrastructure setup
    rebuild_cfg(ctx)                              // sub_785E20(ctx, 0)
    prepare_blocks(ctx, 1)                        // sub_781F80(ctx, 1)
    scan_branch_patterns(ctx, 0, 0, 0, 0)         // sub_7E6090
    init_chain_context(ctx, ...)                   // sub_7E6AD0

    // Phase 2: RPO traversal with per-block convergence
    changed = false
    block_count = *(ctx + 520)                    // number of blocks
    rpo_indices = *(ctx + 512)                    // int32[] RPO permutation
    block_table = *(ctx + 296)                    // ptr[] block array

    for i = 1 to block_count:                     // skip entry block (RPO index 0)
        block = block_table[rpo_indices[i]]

        loop:                                     // inner convergence loop
            matched = try_match(work, block)       // sub_753600: pattern match
            if not matched:
                break

            if not knob_enabled(464):              // convergence-loop gate
                break

            changed = true
            apply_rewrite(work)                    // sub_753B50: CFG rewrite
            // loop back: re-examine same block for newly exposed patterns

    // Phase 3: post-pass CFG rebuild (only if any rewrite fired)
    if changed:
        rebuild_cfg(ctx)                          // sub_785E20(ctx, 0)
```

Key structural properties:

- **Processing order** is strict RPO (reverse post-order). The RPO index array at `ctx+512` is a permutation of block IDs; the outer loop walks indices 1..block_count, skipping index 0 (the entry block, which by definition has no incoming branches to simplify).
- **Per-block re-examination** is the inner `loop`. After each successful match+rewrite on a block, the pass immediately re-tests the same block. This catches cascading opportunities: threading a branch may leave the block ending with a redundant unconditional jump to its layout successor, which the next iteration folds away.
- **Knob 464** gates the inner loop. When disabled, each block is tested at most once (no convergence). When enabled (the default), the inner loop runs until `sub_753600` returns false, meaning no further pattern matches exist for that block.
- **Single RPO pass, not a global fixed point.** The outer loop executes exactly once over all blocks. The pass does not restart from the beginning when a change is made. Global convergence across blocks relies on later re-invocations of the phase by the pass manager (or on the nested conditional optimizer in phase 38 cleaning up remaining patterns).
- **CFG rebuild** (`sub_785E20`) runs unconditionally before the traversal and conditionally after. The post-pass rebuild is gated on the `changed` flag (`r12` in the binary), which latches to true on any successful match and is never reset.

## OptimizeNestedCondBranches — Nested Conditional Flattening (Phase 38)

### Overview

Phase 38 targets nested conditional branches that test related predicates. The pattern commonly arises from C/C++ compound conditions (`if (a && b)`, `if (a || b)`) and from switch-case fall-through after `DoSwitchOpt` lowering. The entry point is `sub_A0F020` (1978 bytes).

The pass runs after `GeneralOptimizeMid` (phase 37), which provides fresh constant folding and copy propagation results. It runs before `OriDoPredication` (phase 63), feeding it simpler CFG patterns that are easier to convert to predicated code.

### Pass Structure

`sub_A0F020` is a three-stage pipeline wrapped in a global fixed-point loop. The outer `while(2)` re-executes the entire pipeline whenever Stage 3 makes progress.

**Stage 1 — Liveness-driven operand simplification.** Iterates blocks in reverse RPO via the priority array at `ctx+792`. For each block, calls `sub_A06A60` with callback `sub_A08250`. The callback scans each instruction's operand array and replaces dead-in operands (not in the live-in bitset at `ctx+832`) with the tombstone value `0xF0000000`. A flag at `ctx+1368` bit 4 enables a two-pass strategy: the first pass is aggressive (`v63=1`), and if it makes no changes, the second pass runs conservatively (`v63=0`). This stage does not combine branches — it prunes operands to expose merging opportunities.

**Stage 2 — Block-pair merging.** Iterates blocks in reverse RPO again, calling `sub_A06A60` with callback `sub_A07DA0`. This callback examines operand liveness within each instruction: for each source operand referencing a virtual register (type tag 1, extracted as `(operand >> 28) & 7 == 1`), if the register index is not in the range 41--44 (the dedicated predicate registers P0--P3, checked as `(operand & 0xFFFFFF) - 41 <= 3`) and the operand is dead-in, it marks the live bit via `sub_BDBC70`. This builds the kill set used by Stage 3.

**Stage 3 — Nested branch combination.** This is the core transformation. It scans basic blocks (at `ctx+296`) looking for a conditional branch (opcode 95 after masking bits 12--13) whose taken target leads to another conditional branch. When it finds such a chain, it walks backward through the intermediate instructions checking each one with `sub_A07940` for combinability, then splices the two branches into one using `sub_91E070`.

### Maximum Nesting Depth

Stage 3 walks the instruction chain without an explicit depth counter. Starting from the last instruction of a candidate block (the conditional branch, opcode 95), it follows the linked-list backward (`next = *(ptr*)current`) examining each instruction until it reaches the block head. Every instruction in the chain must pass the `sub_A07940` eligibility check. The walk terminates at the block boundary, so the effective maximum nesting depth per iteration equals the number of eligible instructions in the intermediate block.

In practice this is bounded by the block structure: each nesting level requires its own basic block, and the pass processes one block-pair at a time. After combining two levels, the global `while(2)` loop re-runs the pipeline, letting the combined block participate in a new round. For a chain of N nested conditions (`if (a && b && c && ...)`), the pass requires N-1 iterations of the outer loop, each collapsing one level.

The guard `sub_7DDB50(ctx) != 1` gates re-iteration: if the function has been reduced to a single block, no further nesting can exist and the outer loop exits.

### Instruction Eligibility (sub_A07940)

`sub_A07940` (396 bytes) determines whether an instruction in the intermediate chain can be absorbed into the combined branch. It rejects:

- Instructions flagged at `sub_7DF3A0` with bit 3 set (convergence barrier) or with a negative byte at offset +28 (predication lock)
- Instructions with a negated-predicate output (bit 5 of the last operand for opcodes 288/183, bit 20 for opcode 16, or a flag in the predicate descriptor at `ctx+416` for opcode 85)
- Four explicitly excluded opcodes after masking: **186** (`BMOV`), **119** (`LEA`), **211** (`PLOP3`), **283** (`VOTE`) — these have side effects or warp-level semantics that prevent reordering
- Opcode **250** (`ISETP`) with a nonzero combined-condition field in its last operand
- Opcode **226** (`FSETP`) with operand count minus modifier adjustment equal to 1 and a non-negative first operand
- Opcode **9** (`BRX`) with a non-negative second operand (resolved indirect branch)
- Opcode **315** (`DSETP`) with bit 2 of the last operand clear
- Instructions where `sub_7E0030` (has live predicate output) AND `sub_7E08E0` (has observable barrier effect) both return true

### LOP3 Truth Table Composition

When the intermediate chain passes eligibility, `sub_91C840` maps each comparison-type code in the predicate-setting instruction to a truth-table index for LOP3. The mapping from the comparison type field (first word of the setp descriptor) to 4-bit LOP3 sub-table entries:

| Type | Index | Semantics | Type | Index | Semantics |
|------|-------|-----------|------|-------|-----------|
| 1  | 16 (0x10) | GT   | 12 | 4  (0x04) | XOR       |
| 2  | 12 (0x0C) | LT   | 13 | 0  (0x00) | FALSE     |
| 4  | 1  (0x01) | EQ   | 14 | 8  (0x08) | AND       |
| 5  | 3  (0x03) | LE   | 15 | 17 (0x11) | XNOR      |
| 6  | 11 (0x0B) | GE   | 16 | 7  (0x07) | NAND      |
| 7,9 | 2 (0x02) | NE  | 0x13 | 15 (0x0F) | TRUE    |
| 0x14 | 6 (0x06) | ordered | 0x15,0x16 | 5 (0x05) | unordered |

The combined truth table for two predicates is assembled by the caller into an 8-bit LOP3 immediate. For AND-chains (`if (a && b)`), the per-input truth tables are ANDed: `TT_combined = TT_outer & TT_inner`, producing `0xC0` for two simple boolean inputs. For OR-chains (`if (a || b)`), they are ORed: `TT_combined = TT_outer | TT_inner`, producing `0xFC`. Mixed chains compose naturally through repeated application:

```text
AND:  if (a && b)        -->  LOP3 Ptmp, P0, P1, 0xC0   // TT = 0b11000000
OR:   if (a || b)        -->  LOP3 Ptmp, P0, P1, 0xFC   // TT = 0b11111100
XOR:  if (a ^ b)         -->  LOP3 Ptmp, P0, P1, 0x3C   // TT = 0b00111100
NAND: if (!(a && b))     -->  LOP3 Ptmp, P0, P1, 0x3F   // TT = 0b00111111
NOR:  if (!(a || b))     -->  LOP3 Ptmp, P0, P1, 0x03   // TT = 0b00000011
Mixed: if (a && (b || c)) --> two rounds: first OR(b,c), then AND(a, b|c)
```

### Safety Constraints

The pass applies these transformations only when:

1. **No side effects** between the nested branches — the intermediate block must contain only the branch and optionally predicate-setting instructions. The four excluded opcodes (186, 119, 211, 283) block combination because they have warp-level or memory side effects.
2. **No live-out values** from the intermediate block other than the predicate — checked via the liveness bitset at `ctx+832`. Stage 1 tombstones dead operands to make this check precise.
3. **Both branches target the same merge point** — the not-taken path of both the outer and inner branches must reach the same basic block.
4. **The predicates are independent** — `sub_A07940` verifies that the predicate output is not negated and not locked by a convergence barrier. If P1's definition depends on P0's branch outcome, the instructions fail the eligibility walk because the shared live range appears in the merge-point liveness bitmap (checked in `sub_7F44D0` via `sub_7DEEB0`; see [predicate independence check](branch-switch.md#predicate-independence-check-sub_7f44d0) above in the GO-19 section).
5. **No already-folded setp** — `sub_7F44D0` checks whether the predicate-defining instruction is itself a LOP3/PLOP3 with 2 inputs (field at descriptor+4 equals 2). This prevents composing a 4-input expression that exceeds LOP3's 3-input limit.

### Relationship to Predication

Phase 38 is a stepping stone toward phase 63 (`OriDoPredication`). By reducing nested branches to single-level branches, it creates more opportunities for if-conversion:

```text
Phase 38: nested {if(a) { if(b) { ... }}}  -->  if(a AND b) { ... }
Phase 63: if(a AND b) { x = y; }           -->  @(a AND b) MOV x, y
```

Without phase 38, the predication pass would see a multi-level branch diamond that exceeds its nesting-depth threshold, and both branches would remain in the output.

## GPU-Specific Considerations

### SIMT Divergence and Reconvergence

On NVIDIA GPUs, branch optimization has a direct impact on warp execution efficiency. Every conditional branch is a potential divergence point where threads in a 32-thread warp may take different paths. Divergence serializes execution: the warp must execute both paths, masking inactive threads.

The `BSSY` (branch sync stack push) / `BSYNC` (branch sync) mechanism on modern NVIDIA architectures (sm_75+) manages reconvergence:

```asm
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

All four phases follow the standard 16-byte phase object model. Each vtable has three methods: `+0` execute, `+8` getPhaseNumber, `+16` isNoOp.

| Phase | Factory case | Vtable address | execute body | isNoOp |
|-------|-------------|----------------|--------------|--------|
| 14 `DoSwitchOptFirst` | case 14 | `off_22BD7F8` | `sub_C5F720` (42B) | returns false |
| 15 `OriBranchOpt` | case 15 | `off_22BD820` | `sub_C5F950` (34B) | returns false |
| 30 `DoSwitchOptSecond` | case 30 | `off_22BDA78` | `sub_C5FC80` (34B) | returns false |
| 38 `OptimizeNestedCondBranches` | case 38 | `off_22BDBB8` | `sub_C5FA70` (34B) | returns false |

All four `isNoOp` methods return false unconditionally — gating is performed inside the execute body, not via `isNoOp`. Each execute body calls `sub_7DDB50` (156B), which reads the optimization level from `compilation_context+2104` and checks knob 499. The guard is `opt_level > 1`, so these phases execute at `-O2` and above. At `-O0` and `-O1`, `sub_7DDB50` returns 1 and the execute body returns without action.

### Execute Body Details

**Phase 14 — `sub_C5F720` (42 bytes).** After the `sub_7DDB50` gate, dispatches through the SM backend object's vtable: `(*(*(ctx+1584) + 136))(*(ctx+1584))`. Offset +136 is vtable slot 17 on the SM backend. This is a **polymorphic call** — each SM target (sm_50, sm_75, sm_89, sm_100, etc.) provides its own switch optimization implementation. The SM backend object at `compilation_context+1584` is documented in [data-structures.md](../ir/data-structures.md#sm-backend-object-at-1584).

**Phase 15 — `sub_C5F950` (34 bytes).** After the gate, calls `sub_7917F0` (529B) directly — no polymorphic dispatch. `sub_7917F0` is the branch simplification core:

1. Checks `context+1382` bit 2 (CFG validity flag) — returns immediately if clear
2. Checks knob 214 via the knob state dispatcher — if set, skips the pass (OriBranchOpt disable switch)
3. Checks knob 487 (general optimization enablement)
4. Calls `sub_785E20` (266B) to rebuild the CFG
5. Calls `sub_781F80` (8335B) for block preparation infrastructure
6. Calls `sub_7E6090` (2614B) to scan branch patterns and `sub_7E6AD0` (33B) for chain setup
7. Iterates over basic blocks in RPO order (block list at `*(ctx+296)`, RPO indices at `*(ctx+512)`). For each block, calls `sub_753600` (1351B) for the transformation, with a convergence loop gated by knob 464
8. After convergence, calls `sub_785E20` again to finalize the CFG

**Phase 30 — `sub_C5FC80` (34 bytes).** After the gate, calls `sub_791F00(ctx, 1)`. The second argument `1` indicates this is the second switch optimization pass. `sub_791F00` (587B) performs lazy initialization of a 152-byte `SwitchOptContext` cached at `code_object+1288`:

```text
SwitchOptContext (152 bytes, allocated at code_object+1288):
    +0   back-pointer to code object
    +8   allocator reference (from code_object+16)
    +16  case collection array (capacity = block_count + 2)
    +56  secondary collection array
    +96  code_object reference copy
    +104 initialized sentinel (0xFFFFFFFF)
    +112 tertiary collection array
```

After setup, `sub_791F00` calls `sub_77CF40` (4698B, 987 instructions) — the main switch optimization algorithm containing pattern matching, strategy selection (jump table vs. BST vs. cascading if-else), and code emission.

**Phase 38 — `sub_C5FA70` (34 bytes).** After the gate, calls `sub_A0F020` (2375B, 563 instructions) directly. `sub_A0F020` implements the nested conditional optimizer as a fixed-point loop. It allocates a 16-byte work context at `code_object+1648` (lazy init), then iterates: scan blocks for nested branch patterns, combine predicates with LOP3, remove eliminated blocks, repeat until stable. The function also accesses code object fields +832 (block hash map) and +856 (edge data) for CFG manipulation.

### Knob Gating Summary

| Knob | Index | Type | Effect | Checked by |
|------|-------|------|--------|------------|
| ConvertUnsupportedOps | 499 | bool | Master opt-level gate (all 4 phases) | `sub_7DDB50` |
| OriBranchOpt disable | 214 | bool | Disables branch simplification | `sub_7917F0` (phase 15) |
| General optimization | 487 | bool | Enables/disables optimizer passes | `sub_7917F0` (phase 15) |
| Convergence loop | 464 | bool | Gates the fixed-point iteration | `sub_7917F0` (phase 15) |
| SwitchOptBudget | — | int | Limits switch optimization effort budget | `sub_77CF40` (phases 14, 30) |
| SwitchOptExtendedPatternMatch | — | bool | Enables extended pattern recognition | `sub_77CF40` (phases 14, 30) |

### Interaction with ExpandJmxComputation (Phase 80)

Phase 80 is the delayed lowering phase for jump table index computations created by `DoSwitchOpt`. The separation exists because:

1. Jump table index computation requires knowing the final table address, which is not available until after legalization
2. Intervening optimization passes (GVN-CSE, strength reduction) may simplify the index computation before it is expanded
3. Register allocation needs to see the index computation as a single pseudo-instruction for accurate pressure estimation

The pseudo-instruction left by `DoSwitchOpt` is expanded by phase 80 into the final `LEA` + `BRX` sequence after all high-level optimizations are complete.

### Interaction with OriLinearReplacement (Phase 31)

Phase 31 runs immediately after `DoSwitchOptSecond` (phase 30). It targets branch-heavy patterns that survived switch optimization and attempts to replace them with branchless (linear) computation sequences using `SEL` (select) and predicated `MOV` instructions. This is a complement to predication (phase 63) — it operates earlier in the pipeline on simpler patterns, while predication handles more complex diamond-shaped control flow later.

### Interaction with MergeEquivalentConditionalFlow (Phases 133, 136)

Two late-pipeline passes perform tail merging — identifying basic blocks with identical instruction sequences that branch to the same targets, and merging them into a single block. This catches redundancy left over after branch optimization, particularly in switch case bodies that perform similar operations on different case values.

## Algorithmic Summary

```text
Pass                           Algorithm                    Complexity    CFG Changes
─────────────────────────────  ───────────────────────────  ────────────  ──────────────────────
DoSwitchOpt (14, 30)           Pattern match + decision     O(N log N)    Rewrites blocks, adds
                               tree for strategy selection   per switch    jump table pseudo-ops

OriBranchOpt (15)              Single RPO pass with per-    O(B + E)      Deletes blocks, removes
                               block inner convergence loop  per iter      edges, threads branches

OptimizeNestedCondBranches     Pattern match on nested      O(B)          Merges blocks, replaces
(38)                           branch diamonds                             branches with LOP3+BRA
```

Where N = number of switch cases, B = number of basic blocks, E = number of CFG edges.

## Function Map

All addresses from ptxas v13.0.88. Vtable entries resolved by reading the ELF `.rodata` section at file offset `VA - 0x400000`. Confidence: HIGH for vtable functions (direct binary read), HIGH for core algorithms (single-caller chains from vtable execute bodies).

### Phase Vtable Functions

| Address | Size | Phase | Vtable slot | Role |
|---------|------|-------|-------------|------|
| `sub_C5F720` | 42B | 14 | `+0` | execute — dispatches to SM backend vtable[17] |
| `sub_C5F4A0` | 6B | 14 | `+8` | getPhaseNumber — returns 14 |
| `sub_C5F4B0` | 3B | 14 | `+16` | isNoOp — returns false |
| `sub_C5F950` | 34B | 15 | `+0` | execute — calls `sub_7917F0` |
| `sub_C5F480` | 6B | 15 | `+8` | getPhaseNumber — returns 15 |
| `sub_C5F490` | 3B | 15 | `+16` | isNoOp — returns false |
| `sub_C5FC80` | 34B | 30 | `+0` | execute — calls `sub_791F00(ctx, 1)` |
| `sub_C5F2A0` | 6B | 30 | `+8` | getPhaseNumber — returns 30 |
| `sub_C5F2B0` | 3B | 30 | `+16` | isNoOp — returns false |
| `sub_C5FA70` | 34B | 38 | `+0` | execute — calls `sub_A0F020` |
| `sub_C5F1A0` | 6B | 38 | `+8` | getPhaseNumber — returns 38 |
| `sub_C5F1B0` | 3B | 38 | `+16` | isNoOp — returns false |

### Core Algorithm Functions

| Address | Size | Callers | Description |
|---------|------|---------|-------------|
| `sub_77CF40` | 4698B | 1 | DoSwitchOpt core — pattern match, strategy select, code emit |
| `sub_7917F0` | 529B | 2 | OriBranchOpt core — single RPO pass with per-block inner convergence loop |
| `sub_A0F020` | 2375B | 11 | OptimizeNestedCondBranches core — predicate combining |
| `sub_791F00` | 587B | 3 | DoSwitchOpt setup — SwitchOptContext init, calls `sub_77CF40` |
| `sub_661210` | 2B | — | SM backend slot 17 NO-OP (`rep ret`) — SM30, SM50 |
| `sub_849110` | 613B | — | SM backend slot 17 handler A — sync restructuring + branch scan (SM60/70/90+) |
| `sub_729160` | 546B | — | SM backend slot 17 handler B — branch-distance rewriter (SM80/89) |
| `sub_848F40` | 472B | 1 | Sync-pattern restructuring helper (called by `sub_849110`) |
| `sub_A9D9B0` | 256B | 2 | Per-instruction branch-target operand rewriter |

### Infrastructure Functions

| Address | Size | Callers | Description |
|---------|------|---------|-------------|
| `sub_7DDB50` | 156B | 180 | Optimization level gate (knob 499 + opt-level check) |
| `sub_781F80` | 8335B | 131 | Block preparation infrastructure (major shared function) |
| `sub_785E20` | 266B | 34 | CFG rebuild after transformation |
| `sub_7E6090` | 2614B | 80 | Branch pattern scanner |
| `sub_7E6AD0` | 33B | 10 | Branch chain setup |
| `sub_753600` | 1351B | 1 | Block-level branch transform (phase 15 inner loop) |
| `sub_753B50` | 598B | 1 | Block transform continuation |

### Factory and Vtable Data

| Symbol | Address | Description |
|--------|---------|-------------|
| `sub_C60D30` | `0xC60D30` | Phase factory — 159-case switch, allocates 16-byte phase objects |
| `off_22BD5C8` | `0x22BD5C8` | Vtable base — 40-byte stride, index = phase number |
| `off_22BD7F8` | `0x22BD7F8` | Phase 14 vtable (base + 14 * 0x28) |
| `off_22BD820` | `0x22BD820` | Phase 15 vtable (base + 15 * 0x28) |
| `off_22BDA78` | `0x22BDA78` | Phase 30 vtable (base + 30 * 0x28) |
| `off_22BDBB8` | `0x22BDBB8` | Phase 38 vtable (base + 38 * 0x28) |

## Cross-References

- [Pass Inventory](index.md) — complete 159-phase table with phase numbers and categories
- [Optimization Pipeline](../pipeline/optimizer.md) — pipeline infrastructure, dispatch loop, phase ordering
- [Ori IR](../ir/overview.md) — instruction layout, opcode table (OEN = BRA, OFFL = BSSY, OFLAP = BSYNC), CFG hash maps
- [GeneralOptimize Bundles](general-optimize.md) — phases 13, 29, 37 that feed constant/copy information to branch passes
- [Liveness Analysis](liveness.md) — phase 16 (DCE cleanup after branch/switch optimization)
- [Predication](predication.md) — phase 63 (if-conversion, consumes simplified CFG from phases 15 and 38)
- [Varying Propagation](varying-propagation.md) — divergence analysis behind `UBRA` (uniform-branch) eligibility and the speculation-safety hand-off in `AnalyzeUniformsForSpeculation`
- [Hot/Cold Partitioning](hot-cold.md) — phases 41, 108, 109 (block placement interacts with branch layout)
- [Synchronization & Barriers](sync-barriers.md) — BSSY/BSYNC reconvergence mechanism
- [Data Structures](../ir/data-structures.md) — SM backend object at +1584 (phase 14 polymorphic dispatch target)
- [Optimization Levels](../config/opt-levels.md) — `sub_7DDB50` opt-level gate, knob 499 interaction
- [Knobs System](../config/knobs.md) — knobs 214, 464, 487, 499 gating branch/switch phases; SwitchOptBudget and SwitchOptExtendedPatternMatch controlling switch lowering strategy
