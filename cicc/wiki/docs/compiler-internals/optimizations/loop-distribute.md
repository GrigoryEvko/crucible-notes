# Loop Distribution (Loop Fission)

**Pass Type**: Loop splitting transformation
**LLVM Class**: `llvm::LoopDistributePass`
**Algorithm**: Statement partitioning with dependence analysis
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: HIGH - Enables vectorization
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (line 270)

---

## Overview

Loop Distribution (also called Loop Fission) splits a single loop into multiple loops, each processing a subset of the original loop's statements. This is the inverse of loop fusion and is primarily used to enable vectorization.

**Goal**: Separate vectorizable statements from non-vectorizable ones

**Core Transformation**: One loop with mixed statements → Multiple specialized loops

---

## Algorithm

```c
void distributeLoop(Loop* L) {
    // Step 1: Partition statements by dependencies
    vector<StatementGroup> groups;
    
    for (Instruction* I : L->instructions) {
        // Find group with compatible dependencies
        StatementGroup* group = findCompatibleGroup(groups, I);
        if (!group) {
            // Create new group
            group = createNewGroup();
            groups.push_back(group);
        }
        group->add(I);
    }
    
    // Step 2: Create separate loop for each group
    for (StatementGroup* group : groups) {
        Loop* newLoop = cloneLoopStructure(L);
        newLoop->setBody(group->statements);
    }
    
    // Step 3: Delete original loop
    deleteLoop(L);
}

bool areCompatible(Instruction* I1, Instruction* I2) {
    // Compatible if no cross-dependencies preventing separation
    return !hasDependence(I1, I2);
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-loop-distribute` | bool | **false** | Disable distribution |
| `enable-loop-distribute` | bool | **false** | Explicit enable (opt-in) |

**Note**: Loop distribution is often opt-in due to code size growth

---

## Examples

### Example 1: Enable Vectorization

**Before Distribution** (not vectorizable due to mixed dependencies):
```c
for (int i = 0; i < N; i++) {
    A[i] = B[i] + C[i];     // Vectorizable
    if (D[i] > 0) {          // Not vectorizable (conditional)
        E[i] = D[i] * 2;
    }
}
```

**After Distribution**:
```c
// Loop 1: Vectorizable
for (int i = 0; i < N; i++) {
    A[i] = B[i] + C[i];     // Can vectorize this loop
}

// Loop 2: Scalar (with conditional)
for (int i = 0; i < N; i++) {
    if (D[i] > 0) {
        E[i] = D[i] * 2;
    }
}
```

**Benefit**: Loop 1 can be vectorized (4-8× speedup), Loop 2 stays scalar

---

### Example 2: Separate Memory Access Patterns

**Before**:
```c
for (int i = 0; i < N; i++) {
    A[i] = B[i] + 1;        // Stride-1 access
    C[i*10] = D[i];         // Stride-10 access (non-coalesced)
}
```

**After Distribution**:
```c
// Loop 1: Good coalescing
for (int i = 0; i < N; i++) {
    A[i] = B[i] + 1;        // Vectorizable with good memory pattern
}

// Loop 2: Stride access
for (int i = 0; i < N; i++) {
    C[i*10] = D[i];         // Separate loop for strided access
}
```

---

### Example 3: Cannot Distribute (Dependence)

**Cannot Distribute**:
```c
for (int i = 1; i < N; i++) {
    A[i] = B[i] + C[i];
    D[i] = A[i] + A[i-1];   // Depends on A[i] from same iteration
}
```

**Reason**: D computation depends on A computation in same loop

---

## Performance Impact

**Code Size**: +30-100% (multiple loop structures)
**Vectorization Benefit**: 2-8× when enabling vectorization
**Cache**: May hurt locality (multiple passes over data)
**Best Case**: Enables vectorization that wasn't possible before

**Trade-off Decision**:
```
if (vectorization_speedup > 2× && code_size_acceptable) {
    distribute();
}
```

---

## Pass Dependencies

**Required**: LoopInfo, DependenceAnalysis, ScalarEvolution
**Invalidates**: LoopInfo (creates new loops)

---

## Related Optimizations

- **LoopVectorize**: [loop-vectorize.md](loop-vectorize.md) - Primary beneficiary
- **LoopFusion**: [loop-fusion.md](loop-fusion.md) - Inverse transformation
- **LoopInterchange**: [loop-interchange.md](loop-interchange.md) - May be combined

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
