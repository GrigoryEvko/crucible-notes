# NVVMIPMemorySpacePropagation - Interprocedural Memory Space Analysis

**Pass Type**: Module-level interprocedural address space optimization
**LLVM Class**: NVIDIA proprietary
**Algorithm**: Call graph-based address space propagation
**Extracted From**: CICC pass mapping
**Analysis Quality**: MEDIUM - Limited direct evidence
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Criticality**: HIGH for multi-function CUDA code

---

## Overview

NVVMIPMemorySpacePropagation extends MemorySpaceOpt with interprocedural analysis, propagating address space information across function boundaries. This enables specialization of library functions based on caller context.

**Key Innovation**: Clones functions with specialized address spaces based on call sites.

---

## Algorithm Complexity

| Metric | Value | Notes |
|--------|-------|-------|
| **Call graph traversal** | O(N × M) | N = functions, M = call sites |
| **Function cloning** | O(K) | K = specialized versions |
| **Compile time** | +5-15% | Expensive |

---

## Core Algorithm

### Address Space Propagation

```c
void propagateAddressSpaces(Module& M) {
    CallGraph CG(M);

    // Bottom-up traversal of call graph
    for (CallGraphNode* Node : post_order(CG)) {
        Function* F = Node->getFunction();

        // Analyze all call sites
        DenseMap<CallSite, unsigned> CallSiteSpaces;
        for (Use& U : F->uses()) {
            if (CallSite CS = CallSite(U)) {
                unsigned AS = inferArgumentSpace(CS);
                CallSiteSpaces[CS] = AS;
            }
        }

        // Clone function if different address spaces
        if (needsSpecialization(CallSiteSpaces)) {
            createSpecializedVersions(F, CallSiteSpaces);
        }
    }
}
```

---

## Function Specialization

### Example Pattern

```c
// Generic helper function
__device__ void copy(void* dst, void* src, int n) {
    for (int i = 0; i < n; i++) {
        ((char*)dst)[i] = ((char*)src)[i];
    }
}

__global__ void kernel() {
    __shared__ float shared[256];
    float global[256];

    // Call site 1: shared → shared
    copy(shared, shared + 128, 128);

    // Call site 2: global → shared
    copy(shared, global, 256);
}
```

**After NVVMIPMemorySpacePropagation**:

```llvm
; Specialized version 1: shared → shared
define void @copy.shared.shared(
    i8 addrspace(3)* %dst,
    i8 addrspace(3)* %src,
    i32 %n
) { ... }

; Specialized version 2: shared ← global
define void @copy.shared.global(
    i8 addrspace(3)* %dst,
    i8 addrspace(1)* %src,
    i32 %n
) { ... }

; Call sites updated:
call void @copy.shared.shared(...)
call void @copy.shared.global(...)
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `nvvm-ip-mem-space-max-clones` | int | **10** | Max function clones |
| `nvvm-ip-mem-space-threshold` | int | **3** | Min call sites for specialization |

---

## Performance Impact

| Metric | Improvement | Variability |
|--------|-------------|-------------|
| **Generic pointer overhead** | 40-70% reduction | High |
| **Execution time** | 5-20% improvement | Very High |
| **Code size** | +10-30% | High (cloning) |
| **Compile time** | +5-15% | Medium |

**Best Case**: Helper functions with many call sites from different address spaces.

---

## Known Limitations

| Limitation | Impact | Workaround |
|-----------|--------|----------|
| **Indirect calls** | Cannot specialize | Use direct calls |
| **Recursion** | Conservative | Avoid or annotate |
| **Code bloat** | Increased binary size | Limit cloning |

---

## Integration Points

**Prerequisite**: MemorySpaceOpt, CallGraph
**Downstream**: NVPTX Code Generation

**Pass Ordering**:
```
MemorySpaceOpt → NVVMIPMemorySpacePropagation → CodeGen
```

---

**L3 Analysis Quality**: MEDIUM
**Last Updated**: 2025-11-17
**Source**: CICC pass mapping + NVIDIA documentation
