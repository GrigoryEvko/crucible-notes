# Loop Predication

**Pass Type**: Conditional control flow transformation
**LLVM Class**: `llvm::LoopPredicationPass`
**Algorithm**: Branch-to-predicate conversion with guard insertion
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: HIGH - Reduces branch misprediction
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (line 275)

---

## Overview

Loop Predication transforms conditional branches inside loops into predicated operations, reducing branch misprediction penalties. This is especially important for GPU kernels to reduce warp divergence.

**Core Transformation**: Replace `if-then-else` with select/masked operations

**Benefits**:
1. **Reduced Divergence**: GPU warps stay synchronized
2. **Fewer Mispredictions**: Eliminates hard-to-predict branches
3. **Better Vectorization**: Predicated ops easier to vectorize
4. **Pipeline Efficiency**: No branch stalls

---

## Algorithm

```c
void predicateLoop(Loop* L) {
    for (BasicBlock* BB : L->blocks) {
        BranchInst* BI = findConditionalBranch(BB);
        
        if (BI && isPredicatable(BI)) {
            Value* condition = BI->getCondition();
            
            // Transform if-then-else to select
            // if (cond) x = a; else x = b;
            // →
            // x = select(cond, a, b);
            
            convertToSelect(BI, condition);
        }
    }
}

bool isPredicatable(BranchInst* BI) {
    // Can predicate if:
    // 1. Small then/else blocks
    // 2. No side effects
    // 3. Single assignment per branch
    
    return (branchSize(BI) < 4) && !hasSideEffects(BI);
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-loop-predication` | bool | **false** | Disable predication |
| `predication-max-instructions` | int | **4** | Max instructions to predicate |

---

## Examples

### Example 1: Simple Conditional

**Before Predication**:
```c
for (int i = 0; i < N; i++) {
    if (condition[i]) {
        A[i] = B[i];  // Branch
    } else {
        A[i] = C[i];  // Branch
    }
}
```

**After Predication**:
```c
for (int i = 0; i < N; i++) {
    A[i] = condition[i] ? B[i] : C[i];  // Select (no branch)
}
```

**GPU Impact**: All 32 warp threads execute same instruction (no divergence)

---

### Example 2: GPU Warp Divergence Reduction

**Before (Divergent)**:
```cuda
for (int i = threadIdx.x; i < N; i += blockDim.x) {
    if (i % 2 == 0) {
        A[i] = computeEven(i);  // 16 threads
    } else {
        A[i] = computeOdd(i);   // 16 threads (divergent)
    }
}
// Warp executes both branches sequentially (50% utilization)
```

**After (Predicated)**:
```cuda
for (int i = threadIdx.x; i < N; i += blockDim.x) {
    bool even = (i % 2 == 0);
    A[i] = even ? computeEven(i) : computeOdd(i);  // All threads active
}
// Warp executes single predicated instruction (100% utilization)
```

---

## Performance Impact

**CPU**: 1.05-1.2× speedup (reduces misprediction)
**GPU**: 1.5-3× speedup (eliminates divergence)
**Best Case**: Unpredictable branches with balanced then/else

---

## Pass Dependencies

**Required**: LoopInfo, ScalarEvolution, TargetTransformInfo
**Invalidates**: None (modifies instructions in place)

---

## Related Optimizations

- **LoopVectorize**: [loop-vectorize.md](loop-vectorize.md) - Uses predication for masked vectorization
- **SimplifyCFG**: May create predicatable patterns

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
