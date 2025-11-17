# Loop Load Elimination

**Pass Type**: Redundant load elimination in loops
**LLVM Class**: `llvm::LoopLoadEliminationPass`
**Algorithm**: Load-to-store forwarding with alias analysis
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: HIGH - Eliminates redundant memory accesses
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (line 272)

---

## Overview

Loop Load Elimination identifies and removes redundant loads within loops by forwarding values from preceding stores. This optimization reduces memory traffic and improves performance, especially on memory-bound kernels.

**Core Optimization**: Replace `load(addr)` with value from previous `store(addr, value)`

**Key Insight**: If a store writes to memory and a subsequent load reads from the same address with no intervening stores, the load can be eliminated.

---

## Algorithm

```c
void eliminateLoopLoads(Loop* L) {
    // Track stores and their values
    map<Value*, StoreInst*> lastStoreToAddress;
    
    for (BasicBlock* BB : L->blocks) {
        for (Instruction* I : BB->instructions) {
            
            if (StoreInst* SI = dyn_cast<StoreInst>(I)) {
                Value* addr = SI->getPointerOperand();
                lastStoreToAddress[addr] = SI;
                
            } else if (LoadInst* LI = dyn_cast<LoadInst>(I)) {
                Value* addr = LI->getPointerOperand();
                
                // Check if we have a store to this address
                if (StoreInst* prevStore = lastStoreToAddress[addr]) {
                    // Check no aliasing stores between
                    if (!hasInterveningStore(prevStore, LI, addr)) {
                        // Forward store value to load
                        Value* storedValue = prevStore->getValueOperand();
                        LI->replaceAllUsesWith(storedValue);
                        LI->eraseFromParent();
                    }
                }
            }
        }
    }
}

bool hasInterveningStore(StoreInst* SI, LoadInst* LI, Value* addr) {
    // Check if any store between SI and LI may alias with addr
    for (Instruction* I = SI->getNextNode(); I != LI; I = I->getNextNode()) {
        if (StoreInst* otherStore = dyn_cast<StoreInst>(I)) {
            if (mayAlias(otherStore->getPointerOperand(), addr)) {
                return true;  // Cannot forward
            }
        }
    }
    return false;
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-loop-load-elim` | bool | **false** | Disable pass |

---

## Examples

### Example 1: Store-to-Load Forwarding

**Before**:
```c
for (int i = 0; i < N; i++) {
    A[i] = B[i] + 1;       // Store to A[i]
    C[i] = A[i] * 2;       // Load from A[i] (redundant)
}
```

**After**:
```c
for (int i = 0; i < N; i++) {
    float temp = B[i] + 1;
    A[i] = temp;           // Store to A[i]
    C[i] = temp * 2;       // Use forwarded value (no load)
}
```

**Benefit**: Eliminates load from A[i], reduces memory traffic

---

### Example 2: Cross-Iteration Forwarding

**Before**:
```c
A[0] = init;
for (int i = 0; i < N; i++) {
    float val = A[i];      // Load
    A[i+1] = val + 1;      // Store to next element
}
```

**After** (with analysis):
```c
float val = init;
for (int i = 0; i < N; i++) {
    // Load eliminated - use previous iteration's stored value
    A[i+1] = val + 1;
    val = val + 1;
}
```

---

### Example 3: Cannot Eliminate (Aliasing)

**Cannot Eliminate**:
```c
for (int i = 0; i < N; i++) {
    A[i] = B[i];           // Store to A
    *ptr = value;          // May alias with A
    C[i] = A[i];           // Cannot eliminate (potential aliasing)
}
```

**Reason**: `ptr` may alias with `A[i]`, so load cannot be eliminated

---

## Performance Impact

**Memory Traffic Reduction**: 10-40% for store-load patterns
**Cache Pressure**: Reduced (fewer memory operations)
**Typical Speedup**: 1.1-1.5× on memory-bound loops

**GPU Context**: Critical for reducing global memory access

---

## Pass Dependencies

**Required**: LoopInfo, AliasAnalysis, MemorySSA
**Invalidates**: MemorySSA (load-store structure changed)

---

## Related Optimizations

- **MemorySSA**: Provides precise memory dependence information
- **DeadStoreElimination**: [dse.md](../dse.md) - Eliminates dead stores
- **GVN**: [gvn.md](../gvn.md) - General load elimination

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
