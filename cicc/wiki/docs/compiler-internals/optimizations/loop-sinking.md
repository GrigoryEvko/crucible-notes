# Loop Sinking

**Pass Type**: Code motion transformation pass
**LLVM Class**: `llvm::LoopSinkingPass`
**Algorithm**: Inverse of LICM - moves code into loops when beneficial
**Extracted From**: CICC optimization pass mapping  
**Analysis Quality**: HIGH - Reduces register pressure
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (line 273)

---

## Overview

Loop Sinking moves computations from before/outside loops into loop bodies when beneficial. This is the inverse of LICM and is primarily used to reduce register pressure, especially critical for GPU kernels.

**When Applied**:
- Value computed before loop but only used once inside
- High register pressure outside loop
- GPU: Improve occupancy by reducing live values

**Trade-off**: Recompute each iteration vs. store in register across loop

---

## Algorithm

```c
bool shouldSinkIntoLoop(Instruction* I, Loop* L) {
    // Sink if:
    // 1. All uses are inside loop
    // 2. Not loop-invariant computation (would be hoisted by LICM)
    // 3. Register pressure high outside loop
    
    if (hasUsesOutsideLoop(I, L)) {
        return false;
    }
    
    if (registerPressureHigh(L->preheader)) {
        return true;  // Sink to reduce pressure
    }
    
    return false;
}

void sinkInstruction(Instruction* I, Loop* L) {
    BasicBlock* sinkTarget = L->header;  // Or earliest use block
    I->moveBefore(sinkTarget->getFirstNonPHI());
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-loop-sink` | bool | **false** | Disable sinking |

---

## Examples

### Example 1: Reduce Register Pressure

**Before Sinking**:
```c
int x = compute();  // Computed before loop, stored in register
for (int i = 0; i < N; i++) {
    A[i] = x + i;   // x kept in register across all iterations
}
```

**After Sinking**:
```c
for (int i = 0; i < N; i++) {
    int x = compute();  // Recomputed each iteration
    A[i] = x + i;       // No register held across iterations
}
```

**Trade-off**: N× more computation, but 1 fewer register

---

## Performance Impact

**GPU Context**: Critical for occupancy
- Reduce registers → increase occupancy → higher throughput
- Typical: 5-15% occupancy improvement

**CPU Context**: Rarely beneficial (favors LICM instead)

---

## Pass Dependencies

**Required**: LoopInfo, DominatorTree
**Invalidates**: ScalarEvolution (def positions change)

---

## Related Optimizations

- **LICM**: [licm.md](licm.md) - Inverse transformation
- **RegisterPressureAnalysis**: Determines when to sink

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
