# Loop Interchange

**Pass Type**: Nested loop reordering transformation
**LLVM Class**: `llvm::LoopInterchangePass`
**Algorithm**: Legality analysis with cache locality optimization
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: HIGH - Improves memory locality
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (line 271)

---

## Overview

Loop Interchange reorders nested loops to improve data locality and enable vectorization. By swapping inner and outer loops, this pass can dramatically improve cache utilization and memory bandwidth.

**Goal**: Transform memory access patterns from cache-unfriendly to cache-friendly.

**Prerequisites**: LoopSimplify, nested loop structure

---

## Algorithm: Interchange Decision

### Profitability Analysis

```c
bool shouldInterchange(Loop* outer, Loop* inner) {
    // Analyze memory access patterns
    AccessPattern outerPattern = analyzeAccesses(outer);
    AccessPattern innerPattern = analyzeAccesses(inner);
    
    // Calculate cache misses before/after
    int missesBeforeSwap = estimateCacheMisses(outerPattern, innerPattern);
    int missesAfterSwap = estimateCacheMisses(innerPattern, outerPattern);
    
    // Interchange if significant improvement
    return (missesAfterSwap < missesBeforeSwap * 0.5);
}
```

### Legality Constraints

**Must satisfy**:
1. No loop-carried dependences preventing reordering
2. Induction variables independent
3. No aliasing preventing reordering
4. Trip counts compatible

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-loop-interchange` | bool | **false** | Disable interchange |

---

## Examples

### Example 1: Matrix Access Pattern

**Before Interchange** (column-major access - cache unfriendly):
```c
for (int j = 0; j < N; j++) {      // Outer: columns
    for (int i = 0; i < M; i++) {  // Inner: rows
        A[i][j] = B[i][j] + 1;     // Stride-N access
    }
}
```

**After Interchange** (row-major access - cache friendly):
```c
for (int i = 0; i < M; i++) {      // Outer: rows
    for (int j = 0; j < N; j++) {  // Inner: columns
        A[i][j] = B[i][j] + 1;     // Stride-1 access (better locality)
    }
}
```

**Impact**: 5-20× speedup depending on N and M (fewer cache misses)

---

### Example 2: Matrix Multiply

**Before**:
```c
for (int i = 0; i < N; i++) {
    for (int j = 0; j < N; j++) {
        for (int k = 0; k < N; k++) {
            C[i][j] += A[i][k] * B[k][j];
        }
    }
}
// B accessed with stride-N (cache unfriendly)
```

**After Interchange (k and j swapped)**:
```c
for (int i = 0; i < N; i++) {
    for (int k = 0; k < N; k++) {
        for (int j = 0; j < N; j++) {
            C[i][j] += A[i][k] * B[k][j];
        }
    }
}
// B accessed with stride-1 (cache friendly)
```

---

## Performance Impact

**Typical Speedup**: 2-20× depending on access patterns and matrix sizes
**Best Case**: Transforms stride-N to stride-1 access
**Cache Miss Reduction**: 80-95% for large matrices

---

## Pass Dependencies

**Required**: LoopInfo, DependenceAnalysis, ScalarEvolution
**Invalidates**: LoopInfo, DominatorTree

---

## Related Optimizations

- **LoopVectorize**: [loop-vectorize.md](loop-vectorize.md) - Benefits from stride-1 access
- **LoopUnroll**: [loop-unroll.md](loop-unroll.md) - Often combined
- **LICM**: [licm.md](licm.md) - Additional opportunities after interchange

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
