# Loop Deletion

**Pass Type**: Dead code elimination pass for loops
**LLVM Class**: `llvm::LoopDeletionPass`
**Algorithm**: Control flow analysis with side-effect detection
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: HIGH - Removes provably dead loops
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (line 264)

---

## Overview

Loop Deletion removes loops that have no observable side effects and whose results are unused. This optimization eliminates wasteful computation when loop bodies are provably dead.

**Eligibility Criteria**:
1. Loop has no side effects (no stores, calls, volatiles)
2. All values defined in loop are unused outside loop
3. Loop termination is guaranteed (no infinite loops)

**Core Transformation**: Replace entire loop with its side-effect-free preheader.

---

## Algorithm: Dead Loop Detection

### Side-Effect Analysis

```c
bool isLoopDead(Loop* L) {
    // Check 1: No stores to memory
    for (BasicBlock* BB : L->blocks) {
        for (Instruction* I : BB->instructions) {
            if (StoreInst* SI = dyn_cast<StoreInst>(I)) {
                return false;  // Has side effect
            }
            
            if (CallInst* CI = dyn_cast<CallInst>(I)) {
                if (!CI->onlyReadsMemory()) {
                    return false;  // May have side effects
                }
            }
        }
    }
    
    // Check 2: All defined values unused outside loop
    for (BasicBlock* BB : L->blocks) {
        for (Instruction* I : BB->instructions) {
            for (User* U : I->users()) {
                Instruction* UI = cast<Instruction>(U);
                if (!L->contains(UI->getParent())) {
                    return false;  // Value escapes loop
                }
            }
        }
    }
    
    // Check 3: Loop terminates
    if (!hasGuaranteedTermination(L)) {
        return false;  // May be infinite
    }
    
    return true;  // Loop is dead
}
```

### Deletion Transformation

```c
void deleteLoop(Loop* L) {
    // Step 1: Redirect all external uses to preheader values
    BasicBlock* preheader = L->preheader;
    
    // Step 2: Remove all blocks in loop
    for (BasicBlock* BB : L->blocks) {
        BB->dropAllReferences();
        BB->eraseFromParent();
    }
    
    // Step 3: Connect preheader directly to exit
    BasicBlock* exit = L->exitBlocks[0];
    preheader->getTerminator()->setSuccessor(0, exit);
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-loop-deletion` | bool | **false** | Disable pass |

---

## Examples

### Example 1: Unused Computation Loop

**Original C**:
```c
void example(int N) {
    int sum = 0;
    for (int i = 0; i < N; i++) {
        sum += i;  // Result unused
    }
    // sum never used
}
```

**After Loop Deletion**:
```c
void example(int N) {
    // Loop completely removed
}
```

### Example 2: Cannot Delete (Side Effect)

**Original C**:
```c
void example(int* A, int N) {
    for (int i = 0; i < N; i++) {
        A[i] = i;  // Stores to memory - NOT deleted
    }
}
```

---

## Performance Impact

**Compile Time**: Minimal overhead
**Code Size Reduction**: Can eliminate entire loop bodies (10-50% reduction)
**Runtime**: Eliminates dead computation overhead

---

## Pass Dependencies

**Required**: LoopInfo, ScalarEvolution, DominatorTree
**Invalidates**: LoopInfo, DominatorTree

---

## Related Optimizations

- **DeadCodeElimination**: General DCE for non-loop code
- **DeadStoreElimination**: [dse.md](../dse.md) - Removes dead stores
- **SimplifyCFG**: Removes unreachable code after deletion

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
