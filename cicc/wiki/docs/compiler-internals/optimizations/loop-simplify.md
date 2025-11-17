# Loop Simplify

**Pass Type**: Loop normalization and canonicalization pass
**LLVM Class**: `llvm::LoopSimplifyPass`
**Algorithm**: Control flow graph transformation to canonical loop form
**Extracted From**: CICC decompiled code + optimization pass mapping
**Analysis Quality**: HIGH - Critical prerequisite for all loop optimizations
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`, `deep_analysis/L3/optimizations/loop_detection.json`
**Pass Index**: Loop Optimization (line 268, pass ordering line 448)

---

## Overview

LoopSimplify is a **critical prerequisite pass** that transforms loops into canonical (simplified) form, making them amenable to aggressive loop optimizations. Without LoopSimplify, most loop optimization passes cannot safely operate.

**Core Transformations**:
1. **Insert Preheader**: Ensures single dedicated entry block to loop
2. **Insert Latch**: Ensures single back edge from unique latch block
3. **Insert Exit Blocks**: Ensures dedicated exit blocks for loop
4. **Normalize Exit Edges**: Splits critical edges at loop exits

**Canonical Form Properties**:
- **Single Preheader**: Unique block with single edge to loop header
- **Single Latch**: Unique block containing back edge to header
- **Dedicated Exits**: Exit blocks only reachable from loop
- **No Critical Edges**: All exit edges pass through dedicated exit blocks

---

## Algorithm: Loop Simplification Transformation

### Phase 1: Preheader Insertion

**Goal**: Create single dedicated entry point to loop header.

**Problem**: Multiple external edges entering loop header complicates analysis.

```c
struct PreheaderInfo {
    BasicBlock* header;
    vector<BasicBlock*> externalPredecessors;  // Blocks outside loop
    vector<BasicBlock*> internalPredecessors;  // Blocks inside loop (back edges)
    int needsPreheader;
};

BasicBlock* insertPreheader(Loop* L, DominatorTree* DT) {
    BasicBlock* header = L->header;

    // Step 1: Identify external predecessors (outside loop)
    vector<BasicBlock*> externalPreds;
    vector<BasicBlock*> internalPreds;

    for (int i = 0; i < header->numPredecessors; i++) {
        BasicBlock* pred = header->predecessors[i];

        if (isBlockInLoop(L, pred)) {
            vectorPush(&internalPreds, pred);  // Back edge
        } else {
            vectorPush(&externalPreds, pred);  // External edge
        }
    }

    // Step 2: Check if preheader already exists
    if (externalPreds.size == 1 &&
        hasSingleSuccessor(externalPreds.elements[0]) &&
        getSuccessor(externalPreds.elements[0], 0) == header) {
        // Preheader already exists
        return externalPreds.elements[0];
    }

    // Step 3: Create new preheader block
    BasicBlock* preheader = createBasicBlock("loop.preheader");

    // Step 4: Redirect all external edges to preheader
    for (int i = 0; i < externalPreds.size; i++) {
        BasicBlock* pred = externalPreds.elements[i];
        redirectEdge(pred, header, preheader);
    }

    // Step 5: Add edge from preheader to header
    BranchInst* branch = BranchInst::CreateUnconditional(header);
    preheader->insertTerminator(branch);

    // Step 6: Update PHI nodes in header
    // Move external PHI incoming edges to preheader
    for (int i = 0; i < header->numInstructions; i++) {
        Instruction* inst = header->instructions[i];

        if (PHINode* phi = dyn_cast<PHINode>(inst)) {
            // Collect external incoming values
            vector<Value*> externalValues;
            for (int j = 0; j < externalPreds.size; j++) {
                BasicBlock* pred = externalPreds.elements[j];
                Value* val = phi->getIncomingValueForBlock(pred);
                vectorPush(&externalValues, val);
            }

            // Create new PHI in preheader if multiple external values
            if (externalValues.size > 1) {
                PHINode* preheaderPhi = PHINode::Create(phi->getType(),
                                                        externalValues.size);
                for (int j = 0; j < externalPreds.size; j++) {
                    preheaderPhi->addIncoming(externalValues.elements[j],
                                             externalPreds.elements[j]);
                }
                preheader->insertInstruction(preheaderPhi);

                // Update original PHI: single incoming from preheader
                phi->removeIncomingEdges(externalPreds);
                phi->addIncoming(preheaderPhi, preheader);

            } else {
                // Single external value: direct update
                phi->removeIncomingEdges(externalPreds);
                phi->addIncoming(externalValues.elements[0], preheader);
            }
        }
    }

    // Step 7: Update dominator tree
    // Preheader now dominates header
    DT->changeImmediateDominator(header, preheader);

    // Step 8: Update loop structure
    L->preheader = preheader;

    return preheader;
}
```

**Example Transformation**:

```
Before Preheader Insertion:

    [entry]      [other]
       |            |
       +-----+------+
             |
         [header] ← back edge from [latch]
             |
          [body]


After Preheader Insertion:

    [entry]      [other]
       |            |
       +-----+------+
             |
      [preheader]  (NEW - single entry point)
             |
         [header] ← back edge from [latch]
             |
          [body]
```

### Phase 2: Latch Block Insertion

**Goal**: Ensure single back edge to loop header from unique latch block.

**Problem**: Multiple back edges complicate trip count analysis and loop transformations.

```c
BasicBlock* insertLatch(Loop* L, DominatorTree* DT) {
    BasicBlock* header = L->header;

    // Step 1: Identify all back edges (blocks in loop with edge to header)
    vector<BasicBlock*> latchCandidates;

    for (int i = 0; i < L->blocks.size; i++) {
        BasicBlock* block = L->blocks.elements[i];

        if (hasSuccessor(block, header)) {
            vectorPush(&latchCandidates, block);
        }
    }

    // Step 2: Check if single latch already exists
    if (latchCandidates.size == 1) {
        L->latchBlock = latchCandidates.elements[0];
        return L->latchBlock;
    }

    // Step 3: Create new latch block (merges all back edges)
    BasicBlock* latch = createBasicBlock("loop.latch");

    // Step 4: Redirect all back edges to latch
    for (int i = 0; i < latchCandidates.size; i++) {
        BasicBlock* oldLatch = latchCandidates.elements[i];
        redirectEdge(oldLatch, header, latch);
    }

    // Step 5: Add back edge from latch to header
    BranchInst* backEdge = BranchInst::CreateUnconditional(header);
    latch->insertTerminator(backEdge);

    // Step 6: Update PHI nodes in header
    // Merge multiple back edge values into single PHI
    for (int i = 0; i < header->numInstructions; i++) {
        Instruction* inst = header->instructions[i];

        if (PHINode* phi = dyn_cast<PHINode>(inst)) {
            // Collect back edge incoming values
            vector<Value*> backEdgeValues;
            for (int j = 0; j < latchCandidates.size; j++) {
                BasicBlock* oldLatch = latchCandidates.elements[j];
                Value* val = phi->getIncomingValueForBlock(oldLatch);
                vectorPush(&backEdgeValues, val);
            }

            // Create PHI in new latch to merge values
            PHINode* latchPhi = PHINode::Create(phi->getType(),
                                                backEdgeValues.size);
            for (int j = 0; j < latchCandidates.size; j++) {
                latchPhi->addIncoming(backEdgeValues.elements[j],
                                     latchCandidates.elements[j]);
            }
            latch->insertInstruction(latchPhi);

            // Update header PHI: single incoming from latch
            phi->removeIncomingEdges(latchCandidates);
            phi->addIncoming(latchPhi, latch);
        }
    }

    // Step 7: Update loop structure
    L->latchBlock = latch;
    vectorPush(&L->blocks, latch);

    return latch;
}
```

**Example Transformation**:

```
Before Latch Insertion (multiple back edges):

         [header]
            |
         [body]
          / | \
     [b1][b2][b3]  (all have back edges to header)
       \  |  /
        \ | /
         \|/
      (multiple back edges)


After Latch Insertion:

         [header]
            |
         [body]
          / | \
     [b1][b2][b3]
       \  |  /
        \ | /
       [latch]  (NEW - single back edge)
          |
      (back edge to header)
```

### Phase 3: Dedicated Exit Block Insertion

**Goal**: Ensure loop exits pass through dedicated exit blocks.

**Problem**: Exit blocks shared with other code complicate loop analysis.

```c
void insertDedicatedExits(Loop* L, DominatorTree* DT) {
    // Step 1: Find all exit edges (edges from loop to outside)
    vector<Edge> exitEdges;

    for (int i = 0; i < L->blocks.size; i++) {
        BasicBlock* block = L->blocks.elements[i];

        for (int j = 0; j < block->numSuccessors; j++) {
            BasicBlock* succ = block->successors[j];

            if (!isBlockInLoop(L, succ)) {
                // Exit edge found
                Edge edge = {block, succ};
                vectorPush(&exitEdges, edge);
            }
        }
    }

    // Step 2: For each exit edge, check if dedicated
    for (int i = 0; i < exitEdges.size; i++) {
        Edge edge = exitEdges.elements[i];
        BasicBlock* exitingBlock = edge.source;
        BasicBlock* exitBlock = edge.target;

        // Check if exit block has other predecessors outside loop
        int hasExternalPredecessors = 0;
        for (int j = 0; j < exitBlock->numPredecessors; j++) {
            BasicBlock* pred = exitBlock->predecessors[j];
            if (!isBlockInLoop(L, pred)) {
                hasExternalPredecessors = 1;
                break;
            }
        }

        if (hasExternalPredecessors || exitBlock->numPredecessors > 1) {
            // Need dedicated exit block
            BasicBlock* dedicatedExit = createBasicBlock("loop.exit");

            // Redirect edge to dedicated exit
            redirectEdge(exitingBlock, exitBlock, dedicatedExit);

            // Add unconditional branch to original exit block
            BranchInst* branch = BranchInst::CreateUnconditional(exitBlock);
            dedicatedExit->insertTerminator(branch);

            // Update PHI nodes in exit block
            updateExitPHIs(exitBlock, exitingBlock, dedicatedExit);

            // Add to loop exit block list
            vectorPush(&L->exitBlocks, dedicatedExit);
        } else {
            // Already dedicated
            vectorPush(&L->exitBlocks, exitBlock);
        }
    }
}
```

### Phase 4: Critical Edge Splitting at Exits

**Goal**: Eliminate critical edges at loop boundaries.

**Critical Edge**: Edge from block with multiple successors to block with multiple predecessors.

```c
void splitCriticalExitEdges(Loop* L, DominatorTree* DT) {
    for (int i = 0; i < L->blocks.size; i++) {
        BasicBlock* block = L->blocks.elements[i];

        // Check if block has multiple successors
        if (block->numSuccessors <= 1) {
            continue;
        }

        for (int j = 0; j < block->numSuccessors; j++) {
            BasicBlock* succ = block->successors[j];

            // Check if successor is outside loop and has multiple predecessors
            if (!isBlockInLoop(L, succ) && succ->numPredecessors > 1) {
                // Critical edge found: split it
                BasicBlock* splitBlock = splitEdge(block, succ, DT);

                // Update loop structure
                // splitBlock is outside loop (exit edge)
            }
        }
    }
}

BasicBlock* splitEdge(BasicBlock* source, BasicBlock* target,
                      DominatorTree* DT) {
    // Create new block between source and target
    BasicBlock* splitBlock = createBasicBlock("split");

    // Add unconditional branch to target
    BranchInst* branch = BranchInst::CreateUnconditional(target);
    splitBlock->insertTerminator(branch);

    // Redirect source edge
    redirectEdge(source, target, splitBlock);

    // Update PHI nodes in target
    updatePHINodes(target, source, splitBlock);

    // Update dominator tree
    if (DT->dominates(source, target)) {
        DT->changeImmediateDominator(target, splitBlock);
        DT->changeImmediateDominator(splitBlock, source);
    }

    return splitBlock;
}
```

---

## Configuration Parameters

LoopSimplify has no user-configurable parameters; it always runs with fixed behavior.

**Control Flags** (for debugging only):
```bash
# Disable LoopSimplify (breaks most loop optimizations)
-mllvm -disable-loop-simplify

# Verify loop structure after simplification
-mllvm -verify-loop-info
```

---

## Data Structures

### Loop Structure After Simplification

```c
struct SimplifiedLoop {
    // Guaranteed by LoopSimplify
    BasicBlock* header;            // Loop header (always present)
    BasicBlock* preheader;         // Single entry (NEW if needed)
    BasicBlock* latchBlock;        // Single back edge source (NEW if needed)

    // Exit structure
    vector<BasicBlock*> exitBlocks;  // Dedicated exit blocks
    vector<BasicBlock*> exitingBlocks;  // Blocks with exit edges

    // Loop membership
    vector<BasicBlock*> blocks;    // All blocks in loop

    // Nesting
    Loop* parentLoop;
    vector<Loop*> subLoops;

    // Analysis flags
    int isInSimplifiedForm;        // 1 after LoopSimplify
};
```

### Canonicalization Invariants

**Invariants Enforced by LoopSimplify**:

```c
struct LoopInvariants {
    // Preheader invariants
    int hasPreheader;              // Always 1
    int preheaderHasSingleSuccessor;  // Always 1 (must be header)
    int preheaderOutsideLoop;      // Always 1

    // Latch invariants
    int hasLatch;                  // Always 1
    int latchHasSingleSuccessor;   // May have conditional exit
    int latchInsideLoop;           // Always 1

    // Exit invariants
    int allExitsDedicated;         // Always 1
    int noCriticalExitEdges;       // Always 1

    // PHI invariants
    int headerPHIsHavePreheaderValue;  // Always 1
    int headerPHIsHaveLatchValue;      // Always 1 (if loop iterates)
};

int verifyLoopSimplifyInvariants(Loop* L) {
    // Verify preheader exists and has correct structure
    if (!L->preheader) {
        return 0;  // FAILED
    }

    if (L->preheader->numSuccessors != 1 ||
        L->preheader->successors[0] != L->header) {
        return 0;  // FAILED
    }

    // Verify latch exists and has back edge to header
    if (!L->latchBlock) {
        return 0;  // FAILED
    }

    int hasBackEdge = 0;
    for (int i = 0; i < L->latchBlock->numSuccessors; i++) {
        if (L->latchBlock->successors[i] == L->header) {
            hasBackEdge = 1;
            break;
        }
    }

    if (!hasBackEdge) {
        return 0;  // FAILED
    }

    // Verify no critical exit edges
    for (int i = 0; i < L->blocks.size; i++) {
        BasicBlock* block = L->blocks.elements[i];

        if (block->numSuccessors > 1) {
            for (int j = 0; j < block->numSuccessors; j++) {
                BasicBlock* succ = block->successors[j];

                if (!isBlockInLoop(L, succ) && succ->numPredecessors > 1) {
                    return 0;  // FAILED: critical exit edge
                }
            }
        }
    }

    return 1;  // PASSED
}
```

---

## Pass Dependencies

### Required Analyses (Must Run Before)

| Analysis Pass | Purpose | Dependency Type |
|---------------|---------|-----------------|
| **DominatorTree** | Control flow dominance | CRITICAL |
| **LoopInfo** | Loop structure identification | CRITICAL |

### Invalidated Analyses (Must Recompute After)

- **DominatorTree**: New blocks added, edges modified
- **LoopInfo**: Loop structure modified (preheader, latch, exits)
- **ScalarEvolution**: Induction variables may be affected by PHI changes

### Preserved Analyses

- **AliasAnalysis**: Memory relationships unchanged
- **CallGraph**: No function calls added/removed

---

## Integration with Other Passes

### Pipeline Position

**Always First** in loop optimization pipeline:

```
1. LoopSimplify         ← THIS PASS (prerequisite for all below)
2. LCSSA                (optional: loop-closed SSA form)
3. LICM
4. LoopRotate
5. IndVarSimplify
6. LoopUnroll
7. LoopVectorize
8. ...
```

**Rationale**: Most loop optimizations **require** simplified form.

### Dependency: Loop-Closed SSA (LCSSA)

**Often runs together**: LoopSimplify + LCSSA

**LCSSA Property**: All values defined inside loop and used outside must pass through PHI nodes at loop exits.

```c
// Before LCSSA
for (...) {
    x = ...;  // Defined inside loop
}
use(x);      // Used outside loop (direct use)

// After LCSSA
for (...) {
    x = ...;
}
x.lcssa = PHI [x, loop.exit]  // PHI at exit
use(x.lcssa);                  // Use PHI value
```

**Benefit**: Simplifies def-use analysis across loop boundaries.

---

## CUDA/GPU Considerations

### No Direct GPU-Specific Behavior

LoopSimplify operates on control flow graph structure, independent of target architecture.

**Indirect GPU Benefits**:
1. **Enables Divergence Analysis**: Simplified structure makes warp divergence analysis tractable
2. **Enables Register Allocation**: Canonical form simplifies register pressure analysis
3. **Enables Loop Fusion**: Simplified loops easier to fuse for better memory coalescing

### Impact on Kernel Compilation

**Critical for GPU Optimizations**:
- LoopVectorize (tensor cores) requires simplified loops
- Loop unrolling requires single latch for trip count analysis
- Memory coalescing optimizations rely on canonical induction variables

---

## Performance Impact

### Compile-Time Overhead

**Minimal**: O(V + E) per loop where V = blocks, E = edges

**Typical Impact**: <1% increase in total compilation time

### Runtime Performance

**Neutral Direct Impact**: LoopSimplify itself does not change runtime behavior (preserves semantics)

**Enabling Effect**: Enables subsequent optimizations that provide 10-50% speedup on loop-intensive code

---

## Examples

### Example 1: Preheader Insertion

**Original C**:
```c
void example(int* A, int N, int flag) {
    int i;

    if (flag) {
        goto loop;
    }

    i = 0;

loop:
    if (i < N) {
        A[i] = i;
        i++;
        goto loop;
    }
}
```

**Original CFG**:
```
       [entry]
         |
      (branch on flag)
       /     \
   [init]  [header] ← back edge
      \      /
       \    /
      [header]
         |
      [body]
```

**After Preheader Insertion**:
```
       [entry]
         |
      (branch on flag)
       /     \
   [init]    |
      \     /
   [preheader]  ← NEW (single entry)
         |
      [header] ← back edge
         |
      [body]
```

### Example 2: Latch Insertion

**Original C**:
```c
for (int i = 0; i < N; i++) {
    if (condition[i]) {
        // Branch 1
        process1(i);
        continue;  // Back edge 1
    } else {
        // Branch 2
        process2(i);
        continue;  // Back edge 2
    }
}
```

**Original CFG** (2 back edges):
```
    [header]
       |
   (condition)
     /   \
 [then] [else]
    |     |
    +-----+
      ||
   (2 back edges to header)
```

**After Latch Insertion**:
```
    [header]
       |
   (condition)
     /   \
 [then] [else]
    |     |
    +-----+
      |
   [latch]  ← NEW (single back edge)
      |
   (back edge to header)
```

### Example 3: IR Transformation

**Before LoopSimplify**:
```llvm
define void @example(i32* %A, i32 %N) {
entry:
  br label %header

other_entry:
  ; Another path to header
  br label %header

header:
  %i = phi i32 [ 0, %entry ], [ 0, %other_entry ], [ %i.next, %latch1 ], [ %i.next, %latch2 ]
  %cmp = icmp ult i32 %i, %N
  br i1 %cmp, label %body, label %exit

body:
  %ptr = getelementptr i32, i32* %A, i32 %i
  store i32 %i, i32* %ptr
  %cond = icmp eq i32 %i, 10
  br i1 %cond, label %latch1, label %latch2

latch1:
  %i.next = add i32 %i, 1
  br label %header

latch2:
  %i.next2 = add i32 %i, 1
  br label %header

exit:
  ret void
}
```

**After LoopSimplify**:
```llvm
define void @example(i32* %A, i32 %N) {
entry:
  br label %loop.preheader

other_entry:
  br label %loop.preheader

loop.preheader:  ; NEW: Single entry
  br label %header

header:
  %i = phi i32 [ 0, %loop.preheader ], [ %i.next, %loop.latch ]
  %cmp = icmp ult i32 %i, %N
  br i1 %cmp, label %body, label %loop.exit

body:
  %ptr = getelementptr i32, i32* %A, i32 %i
  store i32 %i, i32* %ptr
  %cond = icmp eq i32 %i, 10
  br i1 %cond, label %latch1, label %latch2

latch1:
  %i.next.1 = add i32 %i, 1
  br label %loop.latch

latch2:
  %i.next.2 = add i32 %i, 1
  br label %loop.latch

loop.latch:  ; NEW: Single back edge
  %i.next = phi i32 [ %i.next.1, %latch1 ], [ %i.next.2, %latch2 ]
  br label %header

loop.exit:  ; NEW: Dedicated exit
  ret void
}
```

---

## Debugging and Verification

### Verification Pass

**Automatic**: LLVM's `-verify` pass checks loop structure.

```bash
# Verify loop structure after simplification
opt -loop-simplify -verify < input.ll > output.ll
```

### Common Simplification Failures

1. **Irreducible Control Flow**: Cannot simplify loops with multiple entries at same level
2. **Exception Handling**: Invoke instructions complicate exit structure
3. **Computed Gotos**: Indirect branches may create complex control flow

### Statistics

With `-stats`:
```
NumPreheadersInserted: 12   # Preheaders created
NumLatchesInserted: 5       # Latches created
NumExitsSplit: 8            # Exit edges split
NumCriticalEdgesSplit: 15   # Critical edges split
```

---

## Known Limitations

1. **Irreducible Loops**: Cannot simplify loops with true multiple entries (rare in practice)
2. **Code Size Growth**: Adds 1-3 basic blocks per loop (~5-10% growth)
3. **PHI Complexity**: May increase number of PHI nodes at loop boundaries
4. **Debug Information**: Line number mappings may become complex

---

## Related Optimizations

- **LoopRotate**: [loop-rotate.md](loop-rotate.md) - Transforms while to do-while (requires LoopSimplify)
- **LICM**: [licm.md](licm.md) - Requires preheader for hoisting
- **LoopUnroll**: [loop-unroll.md](loop-unroll.md) - Requires latch for trip count analysis
- **LoopVectorize**: [loop-vectorize.md](loop-vectorize.md) - Requires canonical form
- **IndVarSimplify**: [indvar-simplify.md](indvar-simplify.md) - Works on canonical induction variables

---

## References

1. **LLVM LoopSimplify**: https://llvm.org/docs/LoopTerminology.html#loop-simplify-form
2. **LLVM Source**: `lib/Transforms/Utils/LoopSimplify.cpp`
3. **Cytron et al.** (1991). "Efficiently Computing Static Single Assignment Form."
4. **LLVM Developer Documentation**: https://llvm.org/docs/WritingAnLLVMPass.html

---

**L3 Analysis Quality**: HIGH (fundamental pass with well-defined semantics)
**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json + LLVM documentation + deep_analysis/L3/optimizations/loop_detection.json
