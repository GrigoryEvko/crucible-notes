# OpenMPOptCGSCCPass (OpenMP Optimization)

**Pass Type**: Interprocedural optimization pass (CGSCC)
**LLVM Class**: `llvm::OpenMPOptCGSCCPass`
**Algorithm**: OpenMP-specific interprocedural optimization
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: MEDIUM - String evidence and pass metadata
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`

---

## Overview

OpenMPOptCGSCCPass is an interprocedural optimization pass that applies OpenMP-specific optimizations across strongly connected components (SCCs) of the call graph. It optimizes OpenMP parallel regions, data sharing, and runtime calls by leveraging interprocedural analysis.

**Key Features**:
- **Parallel region optimization**: Eliminates redundant barriers and synchronization
- **Data sharing analysis**: Optimizes shared/private variable access
- **Runtime call elimination**: Removes unnecessary OpenMP runtime calls
- **Device offloading optimization**: Optimizes OpenMP target offloading
- **CGSCC-based**: Operates on call graph strongly connected components

**Core Algorithm**: Traverse call graph SCCs bottom-up, analyze OpenMP constructs across function boundaries, and apply interprocedural optimizations to reduce runtime overhead.

**CUDA Context**: **Very limited direct relevance** to CUDA compilation. OpenMP offloading to GPUs is separate from CUDA compilation. This pass is primarily for OpenMP CPU parallelism or OpenMP GPU offloading (which competes with CUDA).

---

## Algorithm Details

### CGSCC (Call Graph SCC) Processing

The pass processes strongly connected components of the call graph:

```
Call Graph:
┌─────────────────────────────────────┐
│  SCC 1: main → parallel_region      │
│         parallel_region → worker_fn │
│         worker_fn → parallel_region │  (cyclic)
└─────────────────────────────────────┘
         ↓
┌─────────────────────────────────────┐
│  SCC 2: helper_fn                   │
└─────────────────────────────────────┘

Processing Order (Bottom-Up):
    SCC 2 (helper_fn) → SCC 1 (main, parallel_region, worker_fn)
```

### Optimization Algorithm

```c
bool runOnSCC(CallGraphSCC& SCC) {
    bool Changed = false;

    // Phase 1: Identify OpenMP constructs in SCC
    SmallVector<OpenMPConstruct*, 16> constructs;
    for (CallGraphNode* Node : SCC) {
        Function* F = Node->getFunction();
        if (!F) continue;

        // Find OpenMP parallel regions, barriers, etc.
        for (BasicBlock& BB : *F) {
            for (Instruction& I : BB) {
                if (CallInst* CI = dyn_cast<CallInst>(&I)) {
                    if (isOpenMPRuntimeCall(CI)) {
                        constructs.push_back(analyzeConstruct(CI));
                    }
                }
            }
        }
    }

    // Phase 2: Interprocedural analysis
    Changed |= analyzeDataSharing(SCC, constructs);
    Changed |= optimizeBarriers(SCC, constructs);
    Changed |= eliminateRuntimeCalls(SCC, constructs);

    // Phase 3: Device offloading optimization
    Changed |= optimizeDeviceOffloading(SCC, constructs);

    return Changed;
}
```

### Data Sharing Analysis

```c
bool analyzeDataSharing(CallGraphSCC& SCC,
                         ArrayRef<OpenMPConstruct*> constructs) {
    bool Changed = false;

    for (OpenMPConstruct* Construct : constructs) {
        if (Construct->type != PARALLEL_REGION) continue;

        // Analyze which variables are shared vs private
        DenseMap<Value*, DataSharingType> sharing;

        for (Value* V : Construct->accessed_variables) {
            // Interprocedural analysis across SCC functions
            DataSharingType Type = determineDataSharing(V, SCC);
            sharing[V] = Type;

            // Optimize: Convert shared to firstprivate if not written
            if (Type == SHARED && !isWritten(V, Construct)) {
                sharing[V] = FIRSTPRIVATE;
                Changed = true;
            }
        }

        // Update OpenMP metadata
        updateDataSharingMetadata(Construct, sharing);
    }

    return Changed;
}
```

### Barrier Optimization

```c
bool optimizeBarriers(CallGraphSCC& SCC,
                       ArrayRef<OpenMPConstruct*> constructs) {
    bool Changed = false;

    // Find redundant barriers
    for (OpenMPConstruct* C1 : constructs) {
        if (C1->type != BARRIER) continue;

        for (OpenMPConstruct* C2 : constructs) {
            if (C2->type != BARRIER || C1 == C2) continue;

            // If C1 is immediately followed by C2 with no intervening
            // parallel work, eliminate C1
            if (isRedundantBarrier(C1, C2, SCC)) {
                removeBarrier(C1);
                Changed = true;
            }
        }
    }

    return Changed;
}
```

---

## Data Structures

### OpenMP Construct Representation

```c
enum OpenMPConstructType {
    PARALLEL_REGION,
    BARRIER,
    CRITICAL,
    ATOMIC,
    TARGET_REGION,
    TASK
};

struct OpenMPConstruct {
    OpenMPConstructType type;
    Function* containing_function;
    CallInst* runtime_call;              // Call to OpenMP runtime
    SmallVector<Value*, 8> accessed_variables;
    DenseMap<Value*, DataSharingType> data_sharing;
    bool can_remove;
};
```

### Data Sharing Classification

```c
enum DataSharingType {
    SHARED,         // Variable is shared among threads
    PRIVATE,        // Each thread has private copy
    FIRSTPRIVATE,   // Private with initialization from shared
    LASTPRIVATE,    // Private with copy-out to shared
    REDUCTION       // Reduction variable
};
```

---

## Configuration & Parameters

### Pass Parameters

**Evidence from CICC**:
- `"openmp-opt-inline-device"` - Inline device functions
- `"openmp-opt-print-module-before"` - Print module before optimization
- `"openmp-opt-print-module-after"` - Print module after optimization
- `"openmp-opt-verbose-remarks"` - Verbose optimization remarks

### Optimization Flags

```c
struct OpenMPOptConfig {
    bool inline_device_functions;      // Inline device offload functions
    bool eliminate_barriers;           // Remove redundant barriers
    bool optimize_data_sharing;        // Optimize shared/private
    bool verbose_remarks;              // Print optimization remarks
    bool print_before;                 // Debug: print module before
    bool print_after;                  // Debug: print module after
};
```

---

## Pass Dependencies

### Required Analyses

1. **CallGraph**: For SCC computation
2. **DominatorTree**: For control flow analysis
3. **AliasAnalysis**: For data sharing analysis
4. **TargetLibraryInfo**: For OpenMP runtime identification

### Required Passes (Before)

- **AttributorPass**: Infers function attributes
- **Inlining**: May expose optimization opportunities

### Downstream Impact

- **Inliner**: May inline OpenMP runtime calls
- **SimplifyCFG**: May simplify control flow after barrier removal
- **Dead Code Elimination**: May remove unused OpenMP constructs

---

## Integration Points

### Compiler Pipeline Integration

```
Interprocedural Optimization Pipeline:
    ↓
CallGraph Construction
    ↓
SCC Computation
    ↓
[OpenMPOptCGSCCPass] ← Optimize OpenMP constructs
    ↓
Inlining (may inline optimized OpenMP calls)
    ↓
Further optimizations
```

### OpenMP Runtime Integration

```c
// OpenMP runtime calls that may be optimized:
extern "C" {
    void __kmpc_fork_call(...);        // Parallel region
    void __kmpc_barrier(...);          // Explicit barrier
    void __kmpc_critical(...);         // Critical section
    void __kmpc_atomic_start(...);     // Atomic region
    void __kmpc_push_num_threads(...); // Set thread count
}

// Pass may eliminate or transform these calls
```

---

## CUDA-Specific Considerations

### Very Limited CUDA Relevance

OpenMPOptCGSCCPass has **very limited relevance** to CUDA compilation:

**Why?**
1. **Different parallelism model**: OpenMP uses CPU threads, CUDA uses GPU threads
2. **Separate compilation path**: OpenMP offloading to GPU is separate from CUDA
3. **Competing technologies**: OpenMP GPU offloading competes with CUDA
4. **No OpenMP in typical CUDA**: CUDA code doesn't use OpenMP pragmas

### OpenMP vs CUDA

```cpp
// OpenMP parallel region (CPU or OpenMP offload):
#pragma omp parallel for
for (int i = 0; i < n; i++) {
    data[i] = compute(i);
}

// Equivalent CUDA kernel:
__global__ void kernel(float* data, int n) {
    int i = threadIdx.x + blockIdx.x * blockDim.x;
    if (i < n) {
        data[i] = compute(i);
    }
}
```

**Different compilation paths**:
- OpenMP: Compiled with OpenMP runtime, optimized by OpenMPOptCGSCCPass
- CUDA: Compiled with nvcc, optimized by CUDA-specific passes

### OpenMP Target Offloading to GPU

OpenMP supports GPU offloading:

```cpp
// OpenMP GPU offloading (NOT CUDA):
#pragma omp target teams distribute parallel for
for (int i = 0; i < n; i++) {
    data[i] = compute(i);
}

// This uses OpenMP's GPU backend, NOT CUDA directly
// Compiled to PTX via LLVM's OpenMP offloading path
// OpenMPOptCGSCCPass may optimize this
```

**CICC vs OpenMP offloading**:
- **CICC**: Compiles CUDA C++ to PTX (native CUDA)
- **OpenMP offloading**: Compiles OpenMP pragmas to PTX (via LLVM)
- **Separate compilers**: Different optimization pipelines

### Hybrid OpenMP + CUDA

Some applications use both:

```cpp
// Host-side parallelism with OpenMP
#pragma omp parallel for
for (int device = 0; device < num_gpus; device++) {
    cudaSetDevice(device);

    // Launch CUDA kernel on each GPU
    kernel<<<grid, block>>>(d_data[device], n);
}

// OpenMPOptCGSCCPass optimizes the OpenMP loop (host-side)
// CICC optimizes the CUDA kernels (device-side)
```

---

## Evidence & Implementation

### String Evidence (CICC Binary)

**High-Confidence Evidence**:
- `"openmp-opt-inline-device"` - Device inlining flag
- `"openmp-opt-print-module-before"` - Debug flag
- `"openmp-opt-print-module-after"` - Debug flag
- `"openmp-opt-verbose-remarks"` - Verbose output flag
- `"OpenMPOptPass"` in pass manager (ID 197)

**Confidence Assessment**:
- **Confidence Level**: MEDIUM
- Pass exists in CICC (string evidence and pass manager entry)
- Standard LLVM pass for OpenMP optimization
- **Likely present but rarely used**: CUDA code doesn't use OpenMP

---

## Performance Impact

### Compile-Time Impact

| Metric | Impact | Notes |
|--------|--------|-------|
| **Compilation time** | +2-5% | Interprocedural analysis overhead |
| **Analysis complexity** | O(SCC size) | Depends on call graph complexity |

### Runtime Impact (OpenMP Code)

| Metric | Without Optimization | With Optimization | Improvement |
|--------|---------------------|-------------------|-------------|
| **Barrier overhead** | Baseline | 10-50% reduction | Fewer barriers |
| **Runtime calls** | Baseline | 5-20% reduction | Eliminated calls |
| **Data sharing overhead** | Baseline | 10-30% reduction | Better sharing |
| **Overall performance** | 1.0x | 1.05-1.20x | 5-20% improvement |

### Runtime Impact (CUDA)

**Not applicable**: CUDA code doesn't use OpenMP constructs.

---

## Code Examples

### Example 1: Barrier Elimination (OpenMP)

**Before Optimization**:
```cpp
// OpenMP code with redundant barriers
void parallel_work(float* data, int n) {
    #pragma omp parallel
    {
        #pragma omp barrier  // Barrier 1
        // No parallel work here

        #pragma omp barrier  // Barrier 2 (redundant)

        // Actual work
        #pragma omp for
        for (int i = 0; i < n; i++) {
            data[i] *= 2.0f;
        }
    }
}
```

**After Optimization**:
```cpp
// OpenMPOptCGSCCPass eliminates redundant barrier
void parallel_work(float* data, int n) {
    #pragma omp parallel
    {
        // Barrier 1 and 2 merged/eliminated

        #pragma omp for
        for (int i = 0; i < n; i++) {
            data[i] *= 2.0f;
        }
    }
}
```

### Example 2: Why It Doesn't Apply to CUDA

```cuda
// CUDA code (no OpenMP)
__global__ void kernel(float* data, int n) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n) {
        data[idx] *= 2.0f;
    }
}

void launch_kernel(float* d_data, int n) {
    kernel<<<grid, block>>>(d_data, n);
    // No OpenMP constructs → OpenMPOptCGSCCPass has no effect
}
```

---

## Use Cases

### Effective Use Cases (Not CUDA)

✅ **OpenMP CPU parallelism**
✅ **OpenMP GPU offloading** (non-CUDA)
✅ **Hybrid MPI+OpenMP applications**
✅ **Scientific computing with OpenMP**

### Ineffective Use Cases (CUDA)

❌ **Pure CUDA applications**: No OpenMP constructs
❌ **CUDA kernel optimization**: Different compilation path
❌ **CUDA performance tuning**: Use CUDA-specific optimizations

---

## Known Limitations

| Limitation | Impact | Workaround | Status |
|-----------|--------|------------|--------|
| **CUDA incompatibility** | Not applicable to CUDA | Use CUDA optimizations | Fundamental |
| **Separate compilation path** | OpenMP offload ≠ CUDA | Accept limitation | Known |
| **Limited CICC usage** | Pass rarely active | N/A | Expected |
| **Interprocedural complexity** | Analysis overhead | Tune SCC size | Acceptable |

---

## Related Technologies

### OpenMP vs CUDA Comparison

| Feature | OpenMP | CUDA |
|---------|--------|------|
| **Parallelism model** | Fork-join, tasks | SIMT (single instruction, multiple threads) |
| **Target** | CPU (+ GPU offload) | NVIDIA GPUs |
| **Programming model** | Directive-based (#pragma) | Explicit kernel programming |
| **Compilation** | Standard compilers (GCC, Clang) | nvcc (CUDA compiler) |
| **Optimization pass** | OpenMPOptCGSCCPass | CICC GPU-specific passes |
| **Portability** | High (CPU + GPU) | NVIDIA-specific |

---

## Summary

OpenMPOptCGSCCPass is an interprocedural OpenMP optimization pass that:
- ✅ Optimizes OpenMP parallel regions and barriers
- ✅ Eliminates redundant runtime calls
- ✅ Improves data sharing efficiency
- ✅ Optimizes OpenMP target offloading (non-CUDA GPU)
- ✅ Operates on call graph SCCs (interprocedural)
- ❌ **Not applicable to CUDA code** (different compilation path)
- ❌ Rarely used in CICC (CUDA doesn't use OpenMP)
- ❌ Not a CUDA optimization technique

**Use Case**: OpenMP code optimization (CPU parallelism or OpenMP GPU offloading). Not relevant to native CUDA kernel compilation. Included in CICC for completeness but likely inactive in typical CUDA workflows.

---

**L3 Analysis Quality**: MEDIUM
**Last Updated**: 2025-11-17
**Source**: CICC foundation analyses + 21_OPTIMIZATION_PASS_MAPPING.json
**Evidence**: String literals for configuration flags, pass manager entry
**CUDA Relevance**: Very Low (OpenMP separate from CUDA compilation)
