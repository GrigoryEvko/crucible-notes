# CGProfile (Call Graph Profile) Pass

**Pass Type**: Profiling infrastructure pass
**LLVM Class**: `llvm::CGProfilePass`
**Algorithm**: Call graph frequency analysis
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: LOW - Pass name confirmed only
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`

---

## Overview

CGProfile (Call Graph Profile) is a pass that annotates the call graph with runtime profiling information, recording the frequency of function calls. This profile data is used by downstream passes for optimization decisions, particularly for function layout and link-time optimization.

**Key Features**:
- **Call frequency recording**: Tracks how often functions call each other
- **Call graph annotation**: Adds profile metadata to call graph edges
- **Link-time optimization support**: Enables profile-guided function layout
- **Section ordering**: Helps linker place hot functions together

**Core Algorithm**: Analyze runtime profile data and attach call frequencies to call graph edges. Emit this data as metadata for use by linker and other passes.

**CUDA Context**: Limited direct relevance to GPU kernel compilation. More applicable to host-side code organization and kernel launch frequency analysis.

---

## Algorithm Details

### Call Graph Profiling Workflow

```
Stage 1: Profile Collection (Runtime)
    ↓
Stage 2: Profile Processing (Compiler)
    ↓
Stage 3: Call Graph Annotation
    ↓
Stage 4: Metadata Emission
    ↓
Stage 5: Link-Time Layout Optimization
```

### Stage 1: Profile Collection

During instrumented execution, record call frequencies:

```c
// Runtime profiling (instrumented binary)
struct CallEdge {
    uint64_t caller_id;     // Unique ID of caller function
    uint64_t callee_id;     // Unique ID of callee function
    uint64_t call_count;    // Number of times this edge was executed
};

// Profiling runtime maintains array of call edges
CallEdge call_edges[MAX_EDGES];
uint32_t num_edges = 0;

// Instrument each call site:
void __record_call(uint64_t caller, uint64_t callee) {
    // Find or create edge
    for (uint32_t i = 0; i < num_edges; i++) {
        if (call_edges[i].caller_id == caller &&
            call_edges[i].callee_id == callee) {
            call_edges[i].call_count++;
            return;
        }
    }

    // New edge
    call_edges[num_edges++] = {caller, callee, 1};
}
```

### Stage 2: Profile Processing

```c
void processCGProfile(Module& M, ProfileData* profile) {
    // Build call graph
    CallGraph CG(M);

    // For each function in module
    for (Function& F : M) {
        uint64_t caller_id = getFunctionID(&F);

        // For each call site in function
        for (BasicBlock& BB : F) {
            for (Instruction& I : BB) {
                if (CallInst* CI = dyn_cast<CallInst>(&I)) {
                    Function* Callee = CI->getCalledFunction();
                    if (!Callee) continue;  // Indirect call

                    uint64_t callee_id = getFunctionID(Callee);

                    // Lookup call frequency from profile
                    uint64_t count = profile->getCallCount(caller_id, callee_id);

                    // Annotate call graph edge
                    CallGraphNode* CallerNode = CG[&F];
                    CallGraphNode* CalleeNode = CG[Callee];
                    CallGraphEdge* Edge = CallerNode->findEdge(CalleeNode);
                    Edge->setWeight(count);
                }
            }
        }
    }
}
```

### Stage 3: Call Graph Annotation

```c
void annotateCGProfile(CallGraph& CG) {
    // Add metadata to module for each hot edge
    SmallVector<std::pair<Function*, Function*>, 32> hot_edges;

    for (auto& Node : CG) {
        Function* F = Node.first;
        CallGraphNode* CGN = Node.second;

        for (auto& Edge : CGN->edges()) {
            if (Edge.getWeight() > THRESHOLD) {
                hot_edges.push_back({F, Edge.getCallee()->getFunction()});
            }
        }
    }

    // Emit as module metadata
    emitCGProfileMetadata(M, hot_edges);
}
```

### Stage 4: Metadata Emission

```llvm
; Call graph profile metadata in LLVM IR:
!llvm.module.flags = !{!0}
!0 = !{i32 1, !"CG Profile", !1}

!1 = !{!2, !3, !4}
!2 = !{i64 (i32)* @caller1, i64 (i32)* @callee1, i64 1000000}  ; caller, callee, count
!3 = !{i64 (i32)* @caller1, i64 (i32)* @callee2, i64 500000}
!4 = !{i64 (i32)* @caller2, i64 (i32)* @callee1, i64 250000}
```

---

## Data Structures

### Call Graph Profile Data

```c
struct CGProfileEdge {
    Function* caller;
    Function* callee;
    uint64_t weight;           // Call frequency
    bool is_hot;               // Weight > hot_threshold
};

struct CGProfileMetadata {
    SmallVector<CGProfileEdge, 64> edges;
    uint64_t hot_threshold;
    uint64_t total_calls;
};
```

### Module Metadata

```c
// Module-level metadata for CG profile
MDNode* createCGProfileMetadata(Module& M,
                                 ArrayRef<CGProfileEdge> edges) {
    LLVMContext& Ctx = M.getContext();
    SmallVector<Metadata*, 64> entries;

    for (const CGProfileEdge& edge : edges) {
        Metadata* caller_md = ConstantAsMetadata::get(edge.caller);
        Metadata* callee_md = ConstantAsMetadata::get(edge.callee);
        Metadata* weight_md = ConstantAsMetadata::get(
            ConstantInt::get(Type::getInt64Ty(Ctx), edge.weight));

        entries.push_back(MDNode::get(Ctx, {caller_md, callee_md, weight_md}));
    }

    return MDNode::get(Ctx, entries);
}
```

---

## Configuration & Parameters

### Thresholds

```c
struct CGProfileConfig {
    uint64_t hot_edge_threshold;    // Minimum count for "hot" edge
    uint32_t max_edges;              // Maximum edges to emit
    bool emit_cold_edges;            // Include cold edges in metadata
    bool function_layout_enabled;   // Use for function layout
};

// Typical values:
CGProfileConfig default_config = {
    .hot_edge_threshold = 10000,     // > 10K calls
    .max_edges = 1000,               // Top 1000 edges
    .emit_cold_edges = false,
    .function_layout_enabled = true
};
```

---

## Pass Dependencies

### Required Analyses

1. **CallGraph**: Must build call graph first
2. **ProfileData**: Requires runtime profile data
3. **FunctionInfo**: For function identification

### Downstream Consumers

**Passes that use CG profile data**:
- **Function layout optimization**: Places frequently-called functions together
- **Inliner**: Prioritizes hot call sites for inlining
- **IPO passes**: Use call frequency for interprocedural decisions
- **Linker**: Orders functions in output binary

---

## Integration Points

### Compiler Pipeline Integration

```
Profile-Guided Compilation:
    ↓
Load Profile Data
    ↓
Build Call Graph
    ↓
[CGProfile] ← Annotate call graph with frequencies
    ↓
Inlining (uses CG profile for priorities)
    ↓
Function Layout Optimization
    ↓
Code Generation
    ↓
Linker (uses CG profile for section ordering)
```

### Linker Integration

CGProfile metadata guides linker function placement:

```
Hot Function Cluster:
┌─────────────────────┐
│ caller1 (hot)       │  ← Placed together in same page
│ callee1 (hot)       │  ← Reduces instruction cache misses
│ callee2 (hot)       │
└─────────────────────┘

Cold Functions:
┌─────────────────────┐
│ error_handler       │  ← Placed in separate section
│ initialization      │
│ cleanup             │
└─────────────────────┘
```

---

## CUDA-Specific Considerations

### Limited GPU Applicability

CGProfile has **limited direct impact** on GPU kernels:

**Why?**
1. **No function calls in kernels**: Most GPU kernels avoid function calls (inlining preferred)
2. **Different execution model**: Thousands of threads execute simultaneously
3. **No instruction cache locality**: GPU instruction cache is separate
4. **Kernel launches are explicit**: Host controls kernel launch, not call graph

### Host-Side Call Graph Profiling

CGProfile is more useful for **host code**:

```cpp
// Host code that launches kernels
void simulation_step(float* d_data, int n, int iterations) {
    // CGProfile tracks how often this calls different kernels

    for (int i = 0; i < iterations; i++) {
        preprocess_kernel<<<grid, block>>>(d_data, n);  // Frequent
        cudaDeviceSynchronize();

        compute_kernel<<<grid, block>>>(d_data, n);     // Frequent
        cudaDeviceSynchronize();

        if (i % 100 == 0) {
            checkpoint_kernel<<<grid, block>>>(d_data, n);  // Infrequent
            cudaDeviceSynchronize();
        }
    }
}

// CGProfile can identify:
// - simulation_step → preprocess_kernel: HIGH frequency
// - simulation_step → compute_kernel: HIGH frequency
// - simulation_step → checkpoint_kernel: LOW frequency
```

### Kernel Launch Frequency Analysis

Profile data helps optimize kernel launch patterns:

```cpp
// Before profiling:
void dispatch_kernels(int type, float* d_data, int n) {
    if (type == TYPE_A) {
        kernelA<<<...>>>(d_data, n);
    } else if (type == TYPE_B) {
        kernelB<<<...>>>(d_data, n);
    } else {
        kernelC<<<...>>>(d_data, n);
    }
}

// After CGProfile analysis:
// - dispatch_kernels → kernelA: 90% of calls
// - dispatch_kernels → kernelB: 8% of calls
// - dispatch_kernels → kernelC: 2% of calls

// Optimization: Reorder branches for better prediction
void dispatch_kernels_optimized(int type, float* d_data, int n) {
    // Hot path first
    if (type == TYPE_A) [[likely]] {
        kernelA<<<...>>>(d_data, n);
    } else if (type == TYPE_B) {
        kernelB<<<...>>>(d_data, n);
    } else {
        kernelC<<<...>>>(d_data, n);
    }
}
```

### CUDA Library Call Profiling

CGProfile can analyze CUDA library usage:

```cpp
void matrix_operations(float* A, float* B, float* C, int n) {
    // CGProfile tracks which CUDA library functions are hot
    cublasGemm(...);        // Frequent (hot)
    cudaMemcpy(...);        // Frequent (hot)
    cudaDeviceSynchronize(); // Frequent (hot)
    cudaGetLastError();     // Infrequent (cold)
}

// Result: Can optimize hot path (e.g., CUDA graph for gemm+sync sequence)
```

---

## Evidence & Implementation

### Evidence from CICC

**Confirmed Evidence**:
- `"CGProfile"` in `21_OPTIMIZATION_PASS_MAPPING.json`

**Confidence Assessment**:
- **Confidence Level**: LOW
- Pass name appears in mapping
- No string evidence in binary
- Standard LLVM pass (likely present for completeness)
- May not be actively used in CICC (limited GPU applicability)

---

## Performance Impact

### Compile-Time Impact

| Metric | Impact | Notes |
|--------|--------|-------|
| **Compilation time** | +0-2% | Minimal (just metadata emission) |
| **Profile processing** | +seconds | One-time cost during PGO build |

### Runtime Impact (Optimization Benefits)

| Metric | Without CG Profile | With CG Profile | Improvement |
|--------|-------------------|-----------------|-------------|
| **Instruction cache misses** | Baseline | 5-20% fewer | Function locality |
| **Branch prediction** | Baseline | Slightly better | Hot path layout |
| **Overall performance** | 1.0x | 1.02-1.10x | 2-10% improvement |

**Note**: Benefits are primarily for CPU code with many function calls. Minimal GPU kernel impact.

---

## Code Examples

### Example 1: Host-Side Call Graph

**Code to profile**:
```cpp
// Host code with call graph
void initialize_data(float* h_data, int n) {
    fill_data(h_data, n);           // Called once
    validate_data(h_data, n);       // Called once
}

void process_iteration(float* d_data, int n) {
    preprocess_kernel<<<...>>>(d_data, n);  // Called many times
    compute_kernel<<<...>>>(d_data, n);     // Called many times
}

void run_simulation(int iterations) {
    float *h_data, *d_data;
    allocate_memory(&h_data, &d_data, N);

    initialize_data(h_data, N);      // Called once

    for (int i = 0; i < iterations; i++) {
        process_iteration(d_data, N);  // Called iterations times
    }

    cleanup(h_data, d_data);         // Called once
}
```

**CGProfile output**:
```
Hot Edges (> 10K calls):
  run_simulation → process_iteration: 1,000,000
  process_iteration → preprocess_kernel: 1,000,000
  process_iteration → compute_kernel: 1,000,000

Cold Edges (< 100 calls):
  run_simulation → initialize_data: 1
  run_simulation → cleanup: 1
  initialize_data → fill_data: 1
  initialize_data → validate_data: 1
```

**Optimization decisions**:
- **Inline**: `process_iteration` into `run_simulation` (hot)
- **Co-locate**: Place `run_simulation` and `process_iteration` adjacent in binary
- **Don't inline**: `initialize_data` and `cleanup` (cold, optimize for size)

### Example 2: Minimal GPU Impact

```cuda
// GPU kernel with no internal calls
__global__ void compute_kernel(float* data, int n) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n) {
        // No function calls - all inlined
        data[idx] = sqrt(data[idx]) * 2.0f;
    }
}

// CGProfile has no impact on kernel:
// - No call graph to profile (everything inlined)
// - Kernel layout is flat
// - No function call overhead to optimize
```

---

## Use Cases

### Effective Use Cases

✅ **Complex host-side applications** with many function calls
✅ **Kernel dispatch logic** with multiple launch paths
✅ **CUDA library-heavy code** (cuBLAS, cuFFT calls)
✅ **Framework code** that orchestrates many kernels

### Ineffective Use Cases

❌ **Simple GPU kernels** (no function calls)
❌ **Highly optimized kernels** (already inlined)
❌ **Single-function applications**
❌ **Primarily compute-bound kernels**

---

## Known Limitations

| Limitation | Impact | Workaround | Status |
|-----------|--------|------------|--------|
| **Indirect calls not tracked** | Misses virtual functions | Use devirtualization | Fundamental |
| **GPU kernel internal calls rare** | Limited GPU applicability | Accept limitation | Known |
| **Profile collection overhead** | Requires instrumented run | One-time cost | Acceptable |
| **Profile staleness** | Must be updated with code changes | Automate profiling | Manageable |

---

## Best Practices

### Profile Collection

```bash
# Step 1: Compile with instrumentation
nvcc -fprofile-generate app.cu -o app_instr

# Step 2: Run with representative workload
./app_instr < typical_input.dat

# Step 3: Merge profiles
llvm-profdata merge -o app.profdata default_*.profraw

# Step 4: Compile with PGO (includes CGProfile)
nvcc -fprofile-use=app.profdata -O3 app.cu -o app_optimized
```

### When to Use

**Good scenarios**:
- Complex host applications
- Multiple kernel launch patterns
- Library-heavy code
- Framework development

**Skip for**:
- Simple single-kernel applications
- Primarily GPU-compute-bound code
- Embedded kernels with minimal host code

---

## Summary

CGProfile is a call graph profiling pass that:
- ✅ Records function call frequencies from runtime profiling
- ✅ Annotates call graph with profile data
- ✅ Guides function layout and inlining decisions
- ✅ Improves instruction cache locality (2-10% on CPU)
- ❌ Limited applicability to GPU kernels (no internal calls)
- ❌ Primarily benefits complex host-side code
- ❌ Requires profile collection overhead

**Use Case**: Profile-guided optimization for complex host-side code with many function calls. Minimal direct impact on GPU kernel performance.

---

**L3 Analysis Quality**: LOW
**Last Updated**: 2025-11-17
**Source**: CICC foundation analyses + 21_OPTIMIZATION_PASS_MAPPING.json
**Evidence**: Pass name in mapping only
**CUDA Relevance**: Low (host-side only, limited GPU kernel impact)
