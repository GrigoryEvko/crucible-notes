# Induction Variable Simplification

**Pass Type**: Loop induction variable optimization pass
**LLVM Class**: `llvm::IndVarSimplifyPass`
**Algorithm**: Canonical induction variable identification and simplification
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: HIGH - Critical for loop optimization
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (line 277)

---

## Overview

IndVarSimplify identifies and simplifies induction variables in loops, converting them to canonical form. This pass is critical for enabling other loop optimizations like unrolling and vectorization.

**Induction Variable**: Variable that changes by fixed amount each iteration
- **Basic IV**: `i = i + 1`
- **Derived IV**: `j = 4*i + 3`

**Canonical Form**: Single primary induction variable with known start, stride, and bound

---

## Algorithm

```c
void simplifyInductionVariables(Loop* L) {
    // Step 1: Identify all induction variables
    vector<PHINode*> inductionVars;
    for (PHINode* phi : L->header->phis) {
        if (isInductionVariable(phi, L)) {
            inductionVars.push_back(phi);
        }
    }
    
    // Step 2: Select canonical IV (typically i = 0; i < N; i++)
    PHINode* canonicalIV = selectCanonicalIV(inductionVars);
    
    // Step 3: Replace derived IVs with expressions of canonical IV
    for (PHINode* iv : inductionVars) {
        if (iv != canonicalIV) {
            // Express in terms of canonical IV
            // j = 4*i + 3 → replace with computation
            replaceDerivedIV(iv, canonicalIV);
        }
    }
    
    // Step 4: Strengthen exit conditions
    simplifyExitConditions(L, canonicalIV);
    
    // Step 5: Widen narrow induction variables if beneficial
    widenNarrowIVs(L);
}

bool isInductionVariable(PHINode* phi, Loop* L) {
    // Check if phi updates by fixed stride
    Value* increment = phi->getIncomingValueForBlock(L->latchBlock);
    if (BinaryOperator* add = dyn_cast<BinaryOperator>(increment)) {
        if (add->getOpcode() == Instruction::Add) {
            Value* stride = add->getOperand(1);
            return isLoopInvariant(stride, L);
        }
    }
    return false;
}
```

---

## Transformations

### 1. Canonical IV Selection

**Original** (multiple IVs):
```c
for (int i = 0; i < N; i++) {
    int j = 4*i + 3;     // Derived IV
    int k = i*i;         // Non-linear derived
    A[j] = B[k];
}
```

**After Simplification** (single canonical IV):
```c
for (int i = 0; i < N; i++) {  // Canonical IV
    A[4*i + 3] = B[i*i];        // Derived IVs replaced with expressions
}
```

---

### 2. IV Widening

**Original** (narrow IV):
```c
for (int8_t i = 0; i < 100; i++) {  // 8-bit IV (suboptimal)
    A[i] = ...;
}
```

**After Widening**:
```c
for (int64_t i = 0; i < 100; i++) {  // Widened to native type
    A[i] = ...;
}
```

**Benefit**: Avoids unnecessary truncation/extension operations

---

### 3. Exit Condition Strengthening

**Original** (complex exit):
```c
int i = 0;
while (i*4 + 3 < N) {
    A[i*4 + 3] = ...;
    i++;
}
```

**After Strengthening**:
```c
int i = 0;
while (i < (N-3)/4) {  // Simplified condition
    A[i*4 + 3] = ...;
    i++;
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-indvar-simplify` | bool | **false** | Disable pass |
| `indvar-widen-range` | int | **target-width** | Width for widening IVs |

---

## Performance Impact

**Compile Time**: Minimal (O(loop size))
**Runtime**: 
- Eliminates redundant IV computations: 2-8% speedup
- Enables better unrolling/vectorization: 10-30% additional speedup

---

## Pass Dependencies

**Required**: LoopInfo, ScalarEvolution, DominatorTree
**Invalidates**: ScalarEvolution (IV expressions changed)

---

## Examples

### Example 1: Derived IV Elimination

**Before**:
```llvm
loop:
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  %j = phi i32 [ 3, %entry ], [ %j.next, %loop ]  ; Derived IV
  
  %addr = getelementptr i32, i32* %A, i32 %j
  store i32 %i, i32* %addr
  
  %i.next = add i32 %i, 1
  %j.next = add i32 %j, 4  ; j = 4*i + 3
  %cmp = icmp ult i32 %i.next, %N
  br i1 %cmp, label %loop, label %exit
```

**After**:
```llvm
loop:
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]  ; Canonical IV only
  
  %j = add i32 (mul i32 %i, 4), 3  ; Computed from canonical IV
  %addr = getelementptr i32, i32* %A, i32 %j
  store i32 %i, i32* %addr
  
  %i.next = add i32 %i, 1
  %cmp = icmp ult i32 %i.next, %N
  br i1 %cmp, label %loop, label %exit
```

---

## Related Optimizations

- **LoopSimplify**: [loop-simplify.md](loop-simplify.md) - Prerequisite
- **ScalarEvolution**: Provides IV analysis
- **LoopUnroll**: [loop-unroll.md](loop-unroll.md) - Benefits from canonical IVs
- **LoopVectorize**: [loop-vectorize.md](loop-vectorize.md) - Requires canonical IVs

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
