# Loop Data Prefetch

**Pass Type**: Memory prefetch insertion pass
**LLVM Class**: `llvm::LoopDataPrefetchPass`
**Algorithm**: Memory access pattern analysis with prefetch insertion
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: MEDIUM - Target-specific optimization
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (unconfirmed passes)

---

## Overview

Loop Data Prefetch inserts explicit prefetch instructions to bring data into cache before it's needed, reducing memory latency. This is particularly important for large data structures that don't fit in cache.

**Goal**: Hide memory latency by prefetching data ahead of usage

**Target Architecture**: Both CPU (prefetch instructions) and GPU (cache hinting)

---

## Algorithm

```c
void insertPrefetches(Loop* L) {
    // Step 1: Identify memory access patterns
    vector<MemoryAccess> accesses = analyzeMemoryAccesses(L);
    
    for (MemoryAccess access : accesses) {
        // Step 2: Calculate prefetch distance
        int prefetchDistance = calculatePrefetchDistance(
            access.stride,
            access.latency,
            L->tripCount
        );
        
        // Step 3: Insert prefetch instruction
        if (prefetchDistance > 0 && prefetchDistance < L->tripCount) {
            BasicBlock* preheader = L->preheader;
            
            // Prefetch: A[i + prefetchDistance]
            Value* prefetchAddr = computePrefetchAddress(
                access.baseAddr,
                access.inductionVar,
                prefetchDistance
            );
            
            // Insert prefetch intrinsic
            CallInst* prefetch = CallInst::Create(
                Intrinsic::prefetch,
                {prefetchAddr, locality, rw, cacheType}
            );
            
            insertPrefetchInLoop(L, prefetch);
        }
    }
}

int calculatePrefetchDistance(int stride, int latency, int tripCount) {
    // Prefetch distance = latency / (iterations per access)
    // Goal: Prefetch data just before it's needed
    
    int distance = latency / stride;
    
    // Clamp to reasonable range
    if (distance < 1) distance = 1;
    if (distance > 32) distance = 32;  // Max lookahead
    
    return distance;
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-loop-data-prefetch` | bool | **false** | Disable prefetching |
| `prefetch-distance` | int | **auto** | Override prefetch lookahead |
| `min-prefetch-stride` | int | **1** | Minimum stride to prefetch |
| `max-prefetch-distance` | int | **32** | Maximum lookahead |

---

## Prefetch Types

### CPU Prefetch Intrinsics

```c
// LLVM prefetch intrinsic
declare void @llvm.prefetch(i8* addr, i32 rw, i32 locality, i32 cache_type)

// Parameters:
// rw: 0 = read, 1 = write
// locality: 0-3 (0=no temporal locality, 3=high temporal locality)
// cache_type: 0=data, 1=instruction
```

### GPU Cache Hinting

```cuda
// CUDA prefetch
__builtin_nontemporal_load(ptr);   // Hint: don't cache
__builtin_assume_aligned(ptr, 128); // Hint: alignment for coalescing
```

---

## Examples

### Example 1: Sequential Access Prefetch

**Before**:
```c
for (int i = 0; i < N; i++) {
    sum += A[i];  // Sequential access, may miss cache
}
```

**After Prefetch Insertion**:
```c
for (int i = 0; i < N; i++) {
    __builtin_prefetch(&A[i + 16]);  // Prefetch 16 iterations ahead
    sum += A[i];
}
```

**Benefit**: Memory latency hidden by prefetching

---

### Example 2: Strided Access

**Before**:
```c
for (int i = 0; i < N; i++) {
    result += B[i * 10];  // Stride-10 access
}
```

**After Prefetch**:
```c
for (int i = 0; i < N; i++) {
    __builtin_prefetch(&B[(i + 8) * 10]);  // Prefetch ahead
    result += B[i * 10];
}
```

---

### Example 3: GPU Prefetch (Non-Temporal Hint)

**CUDA Example**:
```cuda
__global__ void kernel(float* A, float* B, int N) {
    int i = threadIdx.x + blockIdx.x * blockDim.x;
    
    if (i < N) {
        // Hint: A[i] used once, don't pollute cache
        float val = __ldg(&A[i]);  // Non-caching load
        B[i] = val * 2.0f;
    }
}
```

---

## Performance Impact

**Best Case**: 2-5× speedup on memory-bound loops (large arrays)
**Typical**: 1.1-1.5× improvement
**Overhead**: Minimal (prefetch is low-cost hint)

**When Beneficial**:
- Large data structures (> L3 cache)
- Sequential or predictable access patterns
- High memory latency workloads

**When Not Beneficial**:
- Data already in cache
- Irregular access patterns
- Short loops (overhead dominates)

---

## Pass Dependencies

**Required**: LoopInfo, ScalarEvolution, TargetTransformInfo
**Invalidates**: None (adds prefetch hints only)

---

## Related Optimizations

- **LoopVectorize**: [loop-vectorize.md](loop-vectorize.md) - Prefetch can help vectorized code
- **LoopInterchange**: [loop-interchange.md](loop-interchange.md) - Better access patterns enable prefetch
- **LICM**: [licm.md](licm.md) - May hoist prefetch computations

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
