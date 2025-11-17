# Loop Rerolling

**Pass Type**: Loop reconstruction from unrolled code
**LLVM Class**: `llvm::LoopRerollPass`
**Algorithm**: Pattern recognition and loop reconstruction
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: MEDIUM - Inverse of loop unrolling
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (unconfirmed passes)

---

## Overview

Loop Rerolling is the inverse of loop unrolling: it recognizes replicated code patterns and reconstructs them into loops. This reduces code size while preserving functionality.

**Use Cases**:
1. Hand-unrolled loops in source code
2. Macro-generated repetitive code
3. Code size optimization (-Os/-Oz)
4. Reducing I-cache pressure

**Core Transformation**: Replicated statements → Loop structure

---

## Algorithm

```c
bool canRerollStatements(vector<Instruction*> stmts) {
    // Check if statements follow pattern suitable for rerolling
    // Example: A[0] = ...; A[1] = ...; A[2] = ...; → loop
    
    if (stmts.size < 3) return false;  // Need multiple iterations
    
    // Check structural similarity
    for (int i = 1; i < stmts.size; i++) {
        if (!areStructurallyEquivalent(stmts[0], stmts[i])) {
            return false;
        }
    }
    
    // Check if index pattern exists
    if (!hasLinearIndexProgression(stmts)) {
        return false;
    }
    
    return true;
}

void rerollStatements(vector<Instruction*> stmts) {
    // Create loop structure
    Loop* newLoop = createLoop();
    
    // Create induction variable
    PHINode* iv = PHINode::Create(Type::getInt32Ty());
    iv->addIncoming(ConstantInt::get(0), preheader);
    
    // Replace constant indices with induction variable
    Instruction* template = stmts[0];
    replaceConstantIndices(template, iv);
    
    // Set loop bounds
    setLoopTripCount(newLoop, stmts.size);
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-loop-reroll` | bool | **false** | Disable rerolling |
| `reroll-num-tolerated-failed-matches` | int | **2** | Tolerance for pattern matching |

---

## Examples

### Example 1: Unrolled to Rolled

**Before Rerolling** (hand-unrolled):
```c
void process(int* A) {
    A[0] = A[0] * 2 + 1;
    A[1] = A[1] * 2 + 1;
    A[2] = A[2] * 2 + 1;
    A[3] = A[3] * 2 + 1;
    A[4] = A[4] * 2 + 1;
    A[5] = A[5] * 2 + 1;
    A[6] = A[6] * 2 + 1;
    A[7] = A[7] * 2 + 1;
}
```

**After Rerolling**:
```c
void process(int* A) {
    for (int i = 0; i < 8; i++) {
        A[i] = A[i] * 2 + 1;
    }
}
```

**Benefit**: 8× code size reduction

---

### Example 2: Macro Expansion

**Before** (macro-generated):
```c
#define PROCESS(i) result += data[i] * weights[i]

void compute() {
    PROCESS(0);  // Expands to: result += data[0] * weights[0]
    PROCESS(1);
    PROCESS(2);
    PROCESS(3);
    PROCESS(4);
}
```

**After Rerolling**:
```c
void compute() {
    for (int i = 0; i < 5; i++) {
        result += data[i] * weights[i];
    }
}
```

---

### Example 3: Cannot Reroll (No Pattern)

**Cannot Reroll**:
```c
A[0] = B[0] * 2;
A[1] = B[1] + 3;      // Different operation
A[2] = B[2] * 2;
```

**Reason**: Statements not structurally equivalent

---

## Performance Impact

**Code Size**: 50-90% reduction for rerolled code
**Execution**: Slightly slower (loop overhead added)
**I-Cache**: Improved (smaller footprint)
**Best Use**: -Os/-Oz optimization, embedded systems

**Trade-off**: Code size vs. execution speed (opposite of unrolling)

---

## Pass Dependencies

**Required**: None (operates on straight-line code)
**Invalidates**: May create new LoopInfo

---

## Related Optimizations

- **LoopUnroll**: [loop-unroll.md](loop-unroll.md) - Inverse transformation
- **SimplifyCFG**: May create rerollable patterns
- **InstCombine**: Simplifies before rerolling

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
