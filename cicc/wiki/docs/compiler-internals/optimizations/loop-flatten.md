# Loop Flattening

**Pass Type**: Nested loop collapsing transformation
**LLVM Class**: `llvm::LoopFlattenPass`
**Algorithm**: Multi-dimensional iteration space to single dimension
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: HIGH - Simplifies nested loops
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`, `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json` (line 461, loop-flatten-version-loops flag)
**Pass Index**: Loop Optimization (line 276)

---

## Overview

Loop Flattening collapses nested loops with simple structure into a single flat loop. This reduces loop overhead and can expose better optimization opportunities.

**Core Transformation**: `for i: for j:` → `for k:`

**Requirements**:
- Loops must be perfectly nested (inner loop is only statement in outer)
- Iteration counts known or bounded
- No overflow in index computation

---

## Algorithm

```c
void flattenLoop(Loop* outer, Loop* inner) {
    // Transform:
    // for (i = 0; i < M; i++)
    //     for (j = 0; j < N; j++)
    //         A[i*N + j] = ...
    //
    // To:
    // for (k = 0; k < M*N; k++)
    //     A[k] = ...  (with i = k/N, j = k%N if needed)
    
    // Step 1: Compute flattened trip count
    Value* flatCount = Mul(outer->tripCount, inner->tripCount);
    
    // Step 2: Create single induction variable k
    PHINode* k = PHINode::Create(flatCount->getType());
    
    // Step 3: Replace i and j with k/N and k%N
    replaceInductionVar(outer->inductionVar, Div(k, inner->tripCount));
    replaceInductionVar(inner->inductionVar, Rem(k, inner->tripCount));
    
    // Step 4: Merge loop bodies
    mergeLoops(outer, inner);
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `loop-flatten-version-loops` | bool | **true** | Version loops to prevent overflow (from LICM evidence) |
| `flat-loop-tripcount-threshold` | int | **5000** | Max trip count for flattening (from unroll evidence) |

---

## Examples

### Example 1: Simple Nested Loop

**Before Flattening**:
```c
for (int i = 0; i < M; i++) {
    for (int j = 0; j < N; j++) {
        A[i*N + j] = B[i] + C[j];
    }
}
// Loop overhead: M + M*N branches
```

**After Flattening**:
```c
for (int k = 0; k < M*N; k++) {
    int i = k / N;
    int j = k % N;
    A[k] = B[i] + C[j];
}
// Loop overhead: M*N branches (reduced)
```

---

### Example 2: With Versioning (Overflow Protection)

**After Flattening with Versioning**:
```c
if (M * N does not overflow) {
    // Flattened fast path
    for (int k = 0; k < M*N; k++) {
        A[k] = ...;
    }
} else {
    // Original nested loops (safe path)
    for (int i = 0; i < M; i++) {
        for (int j = 0; j < N; j++) {
            A[i*N + j] = ...;
        }
    }
}
```

---

## Performance Impact

**Typical Speedup**: 1.1-1.3× (reduced loop overhead)
**Best Case**: Small inner loops (high overhead ratio)
**Code Size**: +10-30% with versioning

---

## Pass Dependencies

**Required**: LoopInfo, ScalarEvolution
**Invalidates**: LoopInfo (nesting structure changed)

---

## Related Optimizations

- **LoopUnroll**: [loop-unroll.md](loop-unroll.md) - Often combined
- **LICM**: [licm.md](licm.md) - May version flattened loops
- **LoopInterchange**: [loop-interchange.md](loop-interchange.md) - May enable flattening

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
