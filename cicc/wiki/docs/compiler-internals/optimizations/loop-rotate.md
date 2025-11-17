# Loop Rotation

**Pass Type**: Loop transformation pass
**LLVM Class**: `llvm::LoopRotatePass`
**Algorithm**: Control flow transformation from while-loop to do-while form
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: HIGH - Standard transformation for loop optimization
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (line 261)

---

## Overview

Loop Rotation transforms while-loops into do-while form by moving loop condition to the end (latch block). This transformation enables better optimization by:
- Eliminating branch at loop entry (header becomes simple block)
- Enabling aggressive LICM (no conditional entry)
- Improving branch prediction (single backward branch)
- Exposing more optimization opportunities

**Core Transformation**: Header becomes unconditional entry, condition moves to latch.

**Prerequisites**: LoopSimplify (must have preheader and latch)

---

## Algorithm: Loop Rotation Transformation

### Transformation Strategy

```c
void performLoopRotation(Loop* L) {
    // Step 1: Clone header to create new latch
    BasicBlock* newLatch = cloneBasicBlock(L->header);
    
    // Step 2: Make header unconditional entry
    removeHeaderBranch(L->header);
    
    // Step 3: Connect latch to header (back edge)
    addBackEdge(newLatch, L->header);
    
    // Step 4: Update exit conditions
    updateExitConditions(L, newLatch);
}
```

**Before Rotation** (while-loop):
```c
while (i < N) {      // Condition at header
    body();
    i++;
}
```

**After Rotation** (do-while):
```c
if (i < N) {         // Guard in preheader
    do {
        body();
        i++;
    } while (i < N); // Condition at latch
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `rotation-max-header-size` | int | **16** | Max header instructions to rotate |
| `disable-loop-rotation` | bool | **false** | Disable rotation |
| `rotation-prepare-for-lto` | bool | **false** | More aggressive for LTO |

---

## Benefits

1. **Eliminates Header Branch**: Header becomes fall-through
2. **Better LICM**: Unconditional entry enables more hoisting
3. **Branch Prediction**: Single backward branch easier to predict
4. **Unrolling**: Simplifies unrolling logic
5. **Vectorization**: Cleaner structure for vectorizer

---

## Performance Impact

**Typical Speedup**: 2-8% on loop-intensive code
**Code Size**: +5-15% due to cloned header
**Compile Time**: Minimal overhead

---

## Related Optimizations

- **LoopSimplify**: [loop-simplify.md](loop-simplify.md) - Prerequisite
- **LICM**: [licm.md](licm.md) - Benefits from rotation
- **LoopUnroll**: [loop-unroll.md](loop-unroll.md) - Simplified by rotation

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
