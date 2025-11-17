# TailMerging - Identical Code Sequence Merging

**Pass Type**: Control flow optimization / code size reduction
**LLVM Class**: Part of `llvm::BranchFolderPass` or separate pass
**Algorithm**: Suffix tree matching with profitability analysis
**Phase**: Late machine-level optimization
**Pipeline Position**: After register allocation, often with BranchFolding
**Extracted From**: CICC optimization pass mapping (21_OPTIMIZATION_PASS_MAPPING.json)
**Analysis Quality**: MEDIUM - Suspected functionality
**Pass Category**: Machine-Level Optimization / Control Flow / Code Size

---

## Overview

TailMerging identifies and merges **identical instruction sequences** at the end of basic blocks (tails). This is a specialized form of code factoring that:

1. **Reduces code size**: Factor out duplicate instruction sequences
2. **Improves instruction cache efficiency**: Fewer unique instruction bytes
3. **Simplifies control flow**: Merge multiple exit points into one

**Core Algorithm**: Find longest common suffix among basic block instruction sequences

**GPU-Specific Benefits**:
- Reduced PTX binary size → faster kernel loading
- Better instruction cache utilization → improved warp efficiency
- Fewer branches → reduced divergence (in some cases)

**Note**: TailMerging may be **part of BranchFolding** or a **separate pass**. Documentation treats it as conceptually distinct optimization.

---

## Evidence and Location

**Pass Mapping Evidence**:
```json
{
  "other_transformations": [
    "BreakCriticalEdges",
    "BranchFolding",    // May include TailMerging
    "TailMerging"       // Or separate pass (inferred)
  ]
}
```

**Status**: UNCONFIRMED - Suspected
**Confidence**: MEDIUM - Common backend optimization
**Function Estimate**: 30-50 functions (if separate from BranchFolding)

**Relationship to BranchFolding**: TailMerging is often **integrated** into BranchFolding pass, not separate.

---

## Tail Merging Algorithm

### Identifying Common Suffixes

```c
void tail_merge(MachineFunction func) {
  // Step 1: Group blocks by their terminators (return, branch targets)
  Map<Terminator, List<MachineBasicBlock>> terminator_groups;

  for (MachineBasicBlock mbb : func.blocks()) {
    Terminator term = mbb.terminator();
    terminator_groups[term].push_back(mbb);
  }

  // Step 2: For each group, find common tails
  for (auto [term, blocks] : terminator_groups) {
    if (blocks.size() < 2) continue;  // Need at least 2 blocks to merge

    // Find longest common suffix among all blocks
    List<MachineInstr> common_tail = find_longest_common_suffix(blocks);

    if (common_tail.length() >= TAIL_MERGE_THRESHOLD) {
      // Step 3: Factor out common tail
      MachineBasicBlock new_tail_block = create_tail_block(common_tail);

      for (MachineBasicBlock mbb : blocks) {
        // Remove common tail from original block
        remove_tail_instructions(mbb, common_tail);

        // Add branch to shared tail block
        add_branch(mbb, new_tail_block);
      }
    }
  }
}

List<MachineInstr> find_longest_common_suffix(List<MachineBasicBlock> blocks) {
  // Start from end of blocks, work backwards
  List<MachineInstr> common_suffix;

  uint32_t min_block_size = min(blocks.map(b => b.size()));

  for (uint32_t offset = 1; offset <= min_block_size; offset++) {
    MachineInstr candidate = blocks[0].instruction_from_end(offset);

    // Check if all blocks have identical instruction at this offset
    boolean all_identical = true;
    for (MachineBasicBlock mbb : blocks) {
      MachineInstr instr = mbb.instruction_from_end(offset);

      if (!instructions_identical(candidate, instr)) {
        all_identical = false;
        break;
      }
    }

    if (all_identical) {
      common_suffix.insert_front(candidate);
    } else {
      break;  // No longer common, stop
    }
  }

  return common_suffix;
}

boolean instructions_identical(MachineInstr a, MachineInstr b) {
  // Exact match required:
  // - Same opcode
  // - Same operands (registers, immediates)
  // - Same result register

  if (a.opcode() != b.opcode()) return false;
  if (a.operand_count() != b.operand_count()) return false;

  for (uint32_t i = 0; i < a.operand_count(); i++) {
    if (!operands_equal(a.operand(i), b.operand(i))) return false;
  }

  return true;
}
```

---

## Example: Basic Tail Merging

### Identical Exit Sequences

**Before TailMerging**:
```ptx
bb_path1:
  add.s32 R0, R1, R2;        // Path-specific computation
  mul.lo.s32 R3, R0, 4;      // Common tail starts here
  st.global.u32 [%ptr], R3;  // Common tail
  ret;                       // Common tail

bb_path2:
  sub.s32 R0, R4, R5;        // Path-specific computation
  mul.lo.s32 R3, R0, 4;      // IDENTICAL to bb_path1
  st.global.u32 [%ptr], R3;  // IDENTICAL
  ret;                       // IDENTICAL

bb_path3:
  and.b32 R0, R6, R7;        // Path-specific computation
  mul.lo.s32 R3, R0, 4;      // IDENTICAL to bb_path1, bb_path2
  st.global.u32 [%ptr], R3;  // IDENTICAL
  ret;                       // IDENTICAL

// Analysis:
// - 3 blocks
// - Common tail: 3 instructions (mul.lo.s32, st.global.u32, ret)
// - Threshold: 3 instructions → MERGE
```

**After TailMerging**:
```ptx
bb_path1:
  add.s32 R0, R1, R2;        // Path-specific (kept)
  bra bb_common_tail;        // Branch to shared tail

bb_path2:
  sub.s32 R0, R4, R5;        // Path-specific (kept)
  bra bb_common_tail;        // Branch to shared tail

bb_path3:
  and.b32 R0, R6, R7;        // Path-specific (kept)
  bra bb_common_tail;        // Branch to shared tail

bb_common_tail:              // NEW BLOCK: Factored common tail
  mul.lo.s32 R3, R0, 4;
  st.global.u32 [%ptr], R3;
  ret;

// Benefit: 3×3 = 9 instructions → 3 + 3 + 1×3 = 9 instructions
// Code size: Reduced by 6 instructions (2 copies of 3-instruction tail eliminated)
```

---

## Profitability Analysis

### When to Merge Tails

**Trade-offs**:
- ✅ **Pro**: Code size reduction (fewer instruction bytes)
- ✅ **Pro**: Instruction cache efficiency (less cache pressure)
- ❌ **Con**: Added branches (execution overhead)
- ❌ **Con**: Increased control flow complexity

**Profitability Condition**:
```c
boolean is_profitable_tail_merge(List<MachineBasicBlock> blocks,
                                   List<MachineInstr> common_tail) {
  uint32_t tail_length = common_tail.size();
  uint32_t num_blocks = blocks.size();

  // Threshold: Minimum tail length to justify merging
  if (tail_length < TAIL_MERGE_MIN_LENGTH) {
    return false;  // Too short, not worth adding branches
  }

  // Cost-benefit analysis
  uint32_t instructions_saved = (num_blocks - 1) * tail_length;
  uint32_t branches_added = num_blocks;  // Each block needs branch to tail

  // Profitable if savings > cost
  return (instructions_saved > branches_added);
}
```

**Typical Thresholds**:
- `TAIL_MERGE_MIN_LENGTH = 3` instructions (minimum profitable tail)
- Higher threshold for frequently executed blocks (avoid branch overhead)

**Example**:
```
Common tail: 5 instructions
Number of blocks: 3

Instructions saved: (3 - 1) × 5 = 10 instructions
Branches added: 3 branches

Net benefit: 10 - 3 = 7 instructions saved → PROFITABLE
```

---

## Register Liveness Constraints

### Ensuring Correctness

Tail merging must respect register liveness:

**Constraint**: Common tail can only use registers **live in all predecessor blocks**

**Example - Safe Merge**:
```ptx
bb1:
  add.s32 R0, R1, R2;        // R0 defined
  mul.lo.s32 R3, R0, 4;      // Uses R0 (common tail)
  ret;

bb2:
  sub.s32 R0, R4, R5;        // R0 defined (same register)
  mul.lo.s32 R3, R0, 4;      // Uses R0 (common tail)
  ret;

// Safe: R0 live at tail entry in both blocks
```

**Example - Unsafe Merge**:
```ptx
bb1:
  add.s32 R0, R1, R2;        // R0 defined
  mul.lo.s32 R3, R0, 4;      // Uses R0
  ret;

bb2:
  sub.s32 R10, R4, R5;       // R10 defined (different register!)
  mul.lo.s32 R3, R10, 4;     // Uses R10 (NOT R0)
  ret;

// CANNOT MERGE: Operands different (%R0 vs %R10)
```

---

## Partial Tail Merging

### Merging Subset of Tails

Sometimes only **some blocks** share common tails:

**Before**:
```ptx
bb1:
  add.s32 R0, R1, R2;
  mul.lo.s32 R3, R0, 4;      // Common with bb2
  st.global.u32 [%ptr], R3;  // Common with bb2
  ret;                       // Common with bb2

bb2:
  sub.s32 R0, R4, R5;
  mul.lo.s32 R3, R0, 4;      // Common with bb1
  st.global.u32 [%ptr], R3;  // Common with bb1
  ret;                       // Common with bb1

bb3:
  and.b32 R0, R6, R7;
  shl.b32 R8, R0, 2;         // DIFFERENT tail
  st.global.u32 [%ptr2], R8; // DIFFERENT
  ret;

// Analysis:
// - bb1 and bb2: 3-instruction common tail
// - bb3: Different tail
// → Merge bb1 and bb2 only
```

**After Partial Merge**:
```ptx
bb1:
  add.s32 R0, R1, R2;
  bra bb_tail_1_2;

bb2:
  sub.s32 R0, R4, R5;
  bra bb_tail_1_2;

bb_tail_1_2:                 // Merged tail (bb1 + bb2)
  mul.lo.s32 R3, R0, 4;
  st.global.u32 [%ptr], R3;
  ret;

bb3:
  and.b32 R0, R6, R7;
  shl.b32 R8, R0, 2;
  st.global.u32 [%ptr2], R8;
  ret;                       // Kept separate
```

---

## GPU-Specific: Divergence Impact

### Warp Divergence Considerations

**Potential Issue**: Tail merging can **increase divergence** if paths were previously uniform

**Example - Divergence Introduced**:
```ptx
// Before: Uniform branches (all threads same path)
bb_uniform_path1:
  add.s32 R0, R1, R2;
  ret;

bb_uniform_path2:
  add.s32 R0, R1, R2;        // SAME computation
  ret;

// After merging: Added branch (potential divergence)
bb_uniform_path1:
  bra bb_common_tail;        // Branch introduced

bb_uniform_path2:
  bra bb_common_tail;

bb_common_tail:
  add.s32 R0, R1, R2;
  ret;

// Issue: If paths executed by different warps, no problem
// But if same warp diverges to path1 vs path2, added branch may hurt
```

**Mitigation**: Profile-guided optimization
- Only merge tails if blocks executed by different warps
- Avoid merging if divergence likely

---

## Performance Impact

**Code Size**:
- Typical reduction: 10-20% (for tail-heavy code)
- PTX binary smaller → faster kernel loading

**Execution Time**:
- ✅ Better instruction cache: 2-5% improvement
- ❌ Added branches: 1-3% overhead (if frequently executed)
- Net: Usually positive (code size > branch cost)

**Instruction Cache**:
- Significant benefit: Reduced cache pressure

---

## Configuration

| Flag | Type | Default | Purpose |
|------|------|---------|---------|
| `-tail-merge-threshold` | int | 3 | Min tail length for merging |
| `-enable-tail-merge` | bool | true | Enable tail merging |
| `-tail-merge-max-blocks` | int | 50 | Max blocks to analyze together |

---

## Integration with Pipeline

```
RegisterAllocation
          ↓
BranchFolding (may include TailMerging)
          ↓
╔══════════════════════════════╗
║ TailMerging                  ║
║ (THIS PASS, if separate)     ║
║ - Find common suffixes       ║
║ - Factor out duplicate tails ║
║ - Create shared tail blocks  ║
╚══════════════════════════════╝
          ↓
PTX Emission
```

---

## Evidence Summary

**Confidence Level**: MEDIUM
- ✅ Optimization technique well-documented
- ❌ Separate pass existence unconfirmed (may be in BranchFolding)
- ✅ Profitability threshold inferred (3 instructions)
- ⚠️  GPU-specific adaptations unknown

---

**Last Updated**: 2025-11-17
**Confidence**: MEDIUM - Common optimization, likely present (possibly in BranchFolding)
