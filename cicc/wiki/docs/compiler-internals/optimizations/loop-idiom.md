# Loop Idiom Recognition

**Pass Type**: Pattern-based loop transformation pass
**LLVM Class**: `llvm::LoopIdiomRecognizePass`
**Algorithm**: Pattern matching and replacement with optimized library calls
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: HIGH - Recognizes common loop patterns
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (line 265)

---

## Overview

Loop Idiom Recognition identifies common loop patterns and replaces them with optimized library calls or hardware-accelerated instructions. This pass recognizes idioms like memcpy, memset, and other standard patterns.

**Recognized Patterns**:
1. **memset**: Zero/constant initialization loops
2. **memcpy**: Array copying loops
3. **memmove**: Overlapping array copies
4. **Counting loops**: Population count, bit scanning
5. **CUDA patterns**: Warp reductions, coalesced access patterns

---

## Algorithm: Pattern Recognition

### memset Pattern

**Original C**:
```c
for (int i = 0; i < N; i++) {
    A[i] = 0;  // Or any constant
}
```

**Recognized Pattern**:
- Loop iterates over consecutive memory
- Stores constant value to each element
- No dependencies between iterations

**Replacement**:
```c
memset(A, 0, N * sizeof(A[0]));
```

**Cost Model**: Replace if `N * sizeof(element) > threshold` (typically 16 bytes)

---

### memcpy Pattern

**Original C**:
```c
for (int i = 0; i < N; i++) {
    dst[i] = src[i];
}
```

**Recognized Pattern**:
- Copying from one array to another
- Consecutive access in both arrays
- No aliasing between src and dst

**Replacement**:
```c
memcpy(dst, src, N * sizeof(element));
```

---

### CUDA Idioms

**Warp Reduction**:
```cuda
// Original: Manual warp reduction
float val = thread_value;
for (int offset = 16; offset > 0; offset /= 2) {
    val += __shfl_down_sync(0xffffffff, val, offset);
}

// After recognition: Single __reduce_add_sync
float result = __reduce_add_sync(0xffffffff, val);
```

**Coalesced Memory Pattern**:
```cuda
// Recognized: Stride-1 global memory access
for (int i = threadIdx.x; i < N; i += blockDim.x) {
    data[i] = ...;  // Coalesced pattern detected
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-loop-idiom` | bool | **false** | Disable idiom recognition |
| `loop-idiom-min-bytes` | int | **16** | Minimum bytes for memcpy/memset |

---

## Recognized Idioms

| Idiom | Pattern | Replacement | Min Size |
|-------|---------|-------------|----------|
| **memset** | `A[i] = c` | `memset(A, c, N)` | 16 bytes |
| **memcpy** | `dst[i] = src[i]` | `memcpy(dst, src, N)` | 16 bytes |
| **memcmp** | `if (A[i] != B[i])` | `memcmp(A, B, N)` | 8 bytes |
| **strlen** | `while (*p++) cnt++` | `strlen(p)` | Any |
| **popcnt** | `while (x) { cnt += x&1; x>>=1; }` | `__builtin_popcount(x)` | Any |

---

## Performance Impact

**Typical Speedup**: 2-10× for recognized patterns
- memset: 5-10× (hardware-optimized)
- memcpy: 3-8× (wide loads/stores)
- Warp reductions: 2-4× (hardware intrinsics)

**Code Size**: Slightly reduced (library call vs loop body)

---

## Examples

### Example 1: Zero Initialization

**Before**:
```c
for (int i = 0; i < 1024; i++) {
    array[i] = 0;
}
```

**After**:
```c
memset(array, 0, 1024 * sizeof(int));
```

### Example 2: Array Copy

**Before**:
```c
for (int i = 0; i < N; i++) {
    dest[i] = source[i];
}
```

**After**:
```c
memcpy(dest, source, N * sizeof(float));
```

---

## Pass Dependencies

**Required**: LoopInfo, ScalarEvolution, TargetTransformInfo
**Invalidates**: LoopInfo (loop may be deleted)

---

## Related Optimizations

- **LoopVectorize**: [loop-vectorize.md](loop-vectorize.md) - Alternative to idiom replacement
- **SimplifyLibCalls**: Optimizes library call sequences
- **MemCpyOpt**: Further optimizes memory operations

---

**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
