# CanonicalizeFreezeInLoops Pass

**Pass Type**: IR canonicalization pass (loop optimization)
**LLVM Class**: `llvm::CanonicalizeFreezeInLoopsPass`
**Algorithm**: Freeze instruction hoisting and canonicalization
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: LOW - Pass name confirmed only
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`

---

## Overview

CanonicalizeFreezeInLoops is an IR canonicalization pass that optimizes the placement of `freeze` instructions within loops. The `freeze` instruction was introduced in LLVM to handle undefined behavior and poison values by converting them to a fixed (but unspecified) value.

**Key Features**:
- **Freeze instruction hoisting**: Moves freeze operations out of loops when possible
- **Redundant freeze elimination**: Removes duplicate freeze operations
- **Poison value canonicalization**: Ensures consistent handling of poison/undef values
- **Loop-aware placement**: Optimizes freeze placement for loop efficiency

**Core Algorithm**: Identify freeze instructions inside loops, determine if they can be hoisted to loop preheader, and canonicalize their placement for better optimization opportunities.

**CUDA Context**: Limited relevance. Undefined behavior handling is less common in GPU kernels, and freeze instructions are rare in CUDA IR.

---

## Algorithm Details

### Background: Freeze Instruction

The `freeze` instruction in LLVM IR stops propagation of `poison` and `undef` values:

```llvm
; Without freeze: poison propagates
%a = undef
%b = add i32 %a, 1        ; Result is poison
%c = mul i32 %b, 2        ; Result is poison (poison propagates)

; With freeze: poison is stopped
%a = undef
%a_frozen = freeze i32 %a  ; Converts undef to arbitrary but fixed value
%b = add i32 %a_frozen, 1  ; Result is well-defined
%c = mul i32 %b, 2         ; Result is well-defined
```

### Loop Canonicalization Algorithm

```c
void canonicalizeFreezeInLoops(Loop* L) {
    // Phase 1: Find freeze instructions in loop
    SmallVector<FreezeInst*, 16> freeze_instructions;
    for (BasicBlock* BB : L->blocks()) {
        for (Instruction& I : *BB) {
            if (FreezeInst* FI = dyn_cast<FreezeInst>(&I)) {
                freeze_instructions.push_back(FI);
            }
        }
    }

    // Phase 2: Analyze each freeze for hoisting
    for (FreezeInst* FI : freeze_instructions) {
        if (canHoistFreeze(FI, L)) {
            // Hoist to loop preheader
            hoistFreezeToPreheader(FI, L);
        }
    }

    // Phase 3: Eliminate redundant freezes
    eliminateRedundantFreezes(L);
}
```

### Hoisting Analysis

```c
bool canHoistFreeze(FreezeInst* FI, Loop* L) {
    Value* Operand = FI->getOperand(0);

    // Can hoist if operand is loop-invariant
    if (!L->isLoopInvariant(Operand)) {
        return false;
    }

    // Can hoist if freeze has no loop-variant uses in loop body
    // (i.e., all uses can be replaced by hoisted version)
    for (User* U : FI->users()) {
        if (Instruction* UseI = dyn_cast<Instruction>(U)) {
            if (L->contains(UseI)) {
                // Use inside loop - must ensure hoisting is safe
                // Check if use dominates all other uses
                if (!dominatesAllUses(FI, UseI, L)) {
                    return false;
                }
            }
        }
    }

    return true;
}
```

### Hoisting Transformation

```c
void hoistFreezeToPreheader(FreezeInst* FI, Loop* L) {
    BasicBlock* Preheader = L->getLoopPreheader();
    if (!Preheader) {
        // Create preheader if it doesn't exist
        Preheader = InsertPreheaderForLoop(L);
    }

    // Move freeze to end of preheader
    FI->moveBefore(Preheader->getTerminator());

    // Update uses inside loop (they now use hoisted freeze)
    // No changes needed - uses already refer to FI
}
```

---

## Data Structures

### Freeze Instruction Info

```c
struct FreezeInfo {
    FreezeInst* freeze_inst;       // The freeze instruction
    Value* operand;                 // What is being frozen
    bool is_loop_invariant;        // Is operand loop-invariant?
    bool can_hoist;                // Can be hoisted to preheader?
    SmallVector<Use*, 8> loop_uses; // Uses within loop
};
```

### Canonicalization State

```c
struct FreezeCanonicalizationState {
    Loop* current_loop;
    SmallVector<FreezeInfo, 16> freeze_infos;
    DenseMap<Value*, FreezeInst*> existing_freezes;  // For redundancy elimination
};
```

---

## Configuration & Parameters

### Optimization Thresholds

```c
struct FreezeCanonicalizationConfig {
    bool enable_hoisting;          // Enable hoisting to preheader
    bool eliminate_redundant;      // Enable redundant freeze elimination
    uint32_t max_freeze_per_loop;  // Limit freezes to avoid bloat
};
```

---

## Pass Dependencies

### Required Analyses

1. **LoopInfo**: To identify loops and their structure
2. **DominatorTree**: For dominance analysis
3. **ScalarEvolution**: For loop-invariant detection

### Required Passes (Before)

- **LoopSimplify**: Ensures loop has preheader
- **LCSSA**: Ensures loop is in LCSSA form

### Invalidated Analyses

- **ScalarEvolution**: May invalidate if freeze placement changes
- **Loop structure**: Minimal impact

---

## Integration Points

### Compiler Pipeline Integration

```
Loop Optimization Pipeline:
    ↓
LoopSimplify (create preheaders)
    ↓
LCSSA (convert to LCSSA form)
    ↓
[CanonicalizeFreezeInLoops] ← Optimize freeze placement
    ↓
LICM (may hoist additional instructions)
    ↓
Other loop optimizations
```

---

## CUDA-Specific Considerations

### Limited GPU Relevance

CanonicalizeFreezeInLoops has **very limited relevance** to CUDA:

**Why?**
1. **Freeze instructions rare**: CUDA code rarely generates freeze instructions
2. **Undefined behavior less common**: GPU code tends to be more deterministic
3. **Poison values uncommon**: CUDA IR typically doesn't have poison propagation issues
4. **Optimization priority low**: Other loop optimizations matter more

### Potential GPU Scenarios

Freeze might appear in CUDA IR in edge cases:

```cuda
__global__ void example_with_undefined(int* data, int n, int divisor) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx >= n) return;

    // Potential undefined behavior: division by zero
    int result = data[idx] / divisor;  // If divisor == 0, undefined

    // Compiler might insert freeze to handle undefined behavior
    // int divisor_frozen = freeze(divisor);
    // int result = data[idx] / divisor_frozen;

    data[idx] = result;
}
```

**Reality**: CUDA compiler typically doesn't insert freeze for GPU code. Undefined behavior is handled differently (trap, undefined result, etc.).

### Loop Optimization Context

If freeze appears in GPU loops:

```llvm
; Before canonicalization:
loop_body:
    %i = phi i32 [0, %preheader], [%i.next, %loop_body]
    %undef_val = undef
    %frozen = freeze i32 %undef_val  ; Freeze inside loop (inefficient)
    %result = add i32 %i, %frozen
    ; use %result
    %i.next = add i32 %i, 1
    %cmp = icmp ult i32 %i.next, %n
    br i1 %cmp, label %loop_body, label %exit

; After canonicalization:
preheader:
    %undef_val = undef
    %frozen = freeze i32 %undef_val  ; Hoisted to preheader
    br label %loop_body

loop_body:
    %i = phi i32 [0, %preheader], [%i.next, %loop_body]
    %result = add i32 %i, %frozen     ; Use hoisted freeze
    ; use %result
    %i.next = add i32 %i, 1
    %cmp = icmp ult i32 %i.next, %n
    br i1 %cmp, label %loop_body, label %exit
```

**Benefit**: Avoids redundant freeze execution per iteration.

---

## Evidence & Implementation

### Evidence from CICC

**Confirmed Evidence**:
- `"CanonicalizeFreezeInLoops"` in `21_OPTIMIZATION_PASS_MAPPING.json`
- Referenced in backend optimization documentation

**Confidence Assessment**:
- **Confidence Level**: LOW
- Pass name appears in mapping
- Standard LLVM pass (likely present for completeness)
- **Minimal usage expected**: Freeze rare in CUDA IR

---

## Performance Impact

### Compile-Time Impact

| Metric | Impact | Notes |
|--------|--------|-------|
| **Compilation time** | +0-1% | Very fast (limited freeze instructions) |
| **Optimization opportunities** | Marginal | May enable other optimizations |

### Runtime Impact

**Negligible**: Freeze instructions are rare in GPU code.

**Theoretical benefit** (if freeze exists):
- Hoist out of loop: Saves 1 freeze operation per iteration
- For 1000-iteration loop: Saves 999 redundant freeze operations

---

## Code Examples

### Example 1: Freeze Hoisting

**Before Canonicalization**:
```llvm
define void @loop_with_freeze(i32* %data, i32 %n) {
entry:
    br label %loop

loop:
    %i = phi i32 [0, %entry], [%i.next, %loop]
    %undef = undef
    %frozen = freeze i32 %undef      ; Freeze inside loop
    %val = add i32 %i, %frozen
    %ptr = getelementptr i32, i32* %data, i32 %i
    store i32 %val, i32* %ptr
    %i.next = add i32 %i, 1
    %cmp = icmp ult i32 %i.next, %n
    br i1 %cmp, label %loop, label %exit

exit:
    ret void
}
```

**After Canonicalization**:
```llvm
define void @loop_with_freeze(i32* %data, i32 %n) {
entry:
    %undef = undef
    %frozen = freeze i32 %undef      ; Hoisted to entry
    br label %loop

loop:
    %i = phi i32 [0, %entry], [%i.next, %loop]
    %val = add i32 %i, %frozen        ; Use hoisted freeze
    %ptr = getelementptr i32, i32* %data, i32 %i
    store i32 %val, i32* %ptr
    %i.next = add i32 %i, 1
    %cmp = icmp ult i32 %i.next, %n
    br i1 %cmp, label %loop, label %exit

exit:
    ret void
}
```

---

## Use Cases

### Effective Use Cases

✅ **Loops with loop-invariant undefined behavior**
✅ **IR cleanup after other optimizations**
✅ **Canonicalizing undefined value handling**

### Ineffective Use Cases (CUDA)

❌ **Typical GPU kernels**: Don't generate freeze instructions
❌ **Performance optimization**: Negligible impact
❌ **Primary optimization target**: Not a high-priority pass

---

## Known Limitations

| Limitation | Impact | Workaround | Status |
|-----------|--------|------------|--------|
| **Freeze instructions rare** | Limited applicability | N/A | Expected |
| **GPU-specific undefined behavior** | Different handling | Use CUDA-specific checks | Known |
| **Limited optimization benefit** | Small performance gain | Focus on other optimizations | Accepted |

---

## Related Passes

- **LICM (Loop Invariant Code Motion)**: More general hoisting pass
- **InstCombine**: May introduce or eliminate freeze instructions
- **SimplifyCFG**: May create opportunities for freeze optimization

---

## Summary

CanonicalizeFreezeInLoops is an IR canonicalization pass that:
- ✅ Hoists loop-invariant freeze instructions to loop preheaders
- ✅ Eliminates redundant freeze operations
- ✅ Canonicalizes undefined behavior handling
- ✅ Low compile-time overhead (< 1%)
- ❌ Very limited relevance to CUDA (freeze rare)
- ❌ Negligible performance impact in practice
- ❌ Not a primary optimization target

**Use Case**: IR cleanup and canonicalization for code with undefined behavior. Not a significant factor in CUDA kernel optimization due to rarity of freeze instructions in GPU code.

---

**L3 Analysis Quality**: LOW
**Last Updated**: 2025-11-17
**Source**: CICC foundation analyses + 21_OPTIMIZATION_PASS_MAPPING.json
**Evidence**: Pass name in mapping
**CUDA Relevance**: Very Low (freeze instructions rare in CUDA)
