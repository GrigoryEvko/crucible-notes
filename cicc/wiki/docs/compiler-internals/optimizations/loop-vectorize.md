# Loop Vectorization

**Pass Type**: Loop transformation and vectorization pass
**LLVM Class**: `llvm::LoopVectorizePass`, `llvm::LoopIdiomVectorizePass`
**Algorithm**: Cost-model driven SIMD vectorization with dependence analysis
**Extracted From**: CICC decompiled code + optimization pass mapping
**Analysis Quality**: HIGH - Critical for GPU tensor core utilization
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (unconfirmed passes, line 266-267)

---

## Overview

Loop vectorization transforms scalar loops into vector operations, enabling Single Instruction Multiple Data (SIMD) execution. For CUDA compilation, vectorization maps directly to tensor core operations and warp-level primitives, making it **critical for GPU performance**.

**Core Transformation**: Converts N scalar iterations into ⌈N/VF⌉ vector iterations operating on VF elements simultaneously (VF = vectorization factor).

**CICC Implementation Variants**:
- **Standard Loop Vectorization**: Traditional SIMD vectorization for CPU/GPU
- **Loop Idiom Vectorization**: Pattern-based recognition (memcpy, memset → vector ops)
- **Tensor Core Vectorization**: Maps matrix operations to CUDA tensor cores (Volta+)
- **Warp-Level Vectorization**: Exploits implicit SIMD across warp threads

---

## Algorithm: Vectorization Decision and Transformation

### Phase 1: Legality Analysis

**Goal**: Determine if loop can be safely vectorized without changing semantics.

```c
struct VectorizationLegality {
    int isLegal;
    char* illegalityReason;

    // Dependence analysis results
    int hasLoopCarriedDependence;
    int hasBackwardDependence;
    int dependenceDistance;        // Minimum distance between dependent iterations

    // Memory safety
    int hasPotentialAliasing;      // Unresolvable pointer aliasing
    int requiresRuntimeChecks;     // Need runtime disambiguation
    int numRuntimeChecks;

    // Control flow restrictions
    int hasDivergentControlFlow;   // GPU: warp divergence concern
    int hasComplexBranching;
    int multipleExitBlocks;

    // Data type restrictions
    int hasNonVectorizableTypes;   // e.g., structs, unsupported types
    int hasReductions;             // Reduction operations present
    int reductionKind;             // ADD, MUL, MIN, MAX, etc.
};

VectorizationLegality analyzeVectorizationLegality(Loop* L,
                                                   DependenceAnalysis* DA,
                                                   AliasAnalysis* AA) {
    VectorizationLegality result = {1, NULL};  // Assume legal initially

    // Check 1: Loop must be innermost (or analyze nested structure)
    if (!L->subLoops.empty()) {
        // Has nested loops - only vectorize innermost
        if (!isInnermostLoop(L)) {
            result.isLegal = 0;
            result.illegalityReason = "Not innermost loop";
            return result;
        }
    }

    // Check 2: Single entry and single exit (or handle multi-exit)
    if (!hasSimpleStructure(L)) {
        result.isLegal = 0;
        result.illegalityReason = "Complex control flow structure";
        return result;
    }

    // Check 3: Dependence analysis
    for (int i = 0; i < L->blocks.size; i++) {
        BasicBlock* BB = L->blocks.elements[i];

        for (int j = 0; j < BB->numInstructions; j++) {
            Instruction* I = BB->instructions[j];

            if (isMemoryAccess(I)) {
                // Check for loop-carried dependences
                DependenceResult dep = DA->analyze(I, L);

                if (dep.hasLoopCarriedDependence) {
                    result.hasLoopCarriedDependence = 1;
                    result.dependenceDistance = dep.distance;

                    // Forward dependence with distance ≥ VF: can vectorize
                    // Backward dependence: cannot vectorize
                    if (dep.isBackward) {
                        result.isLegal = 0;
                        result.illegalityReason = "Backward loop-carried dependence";
                        return result;
                    }
                }

                // Check pointer aliasing
                if (mayAlias(I, L, AA)) {
                    result.hasPotentialAliasing = 1;
                    result.requiresRuntimeChecks = 1;
                }
            }
        }
    }

    // Check 4: Reduction detection
    if (containsReduction(L)) {
        result.hasReductions = 1;
        result.reductionKind = detectReductionKind(L);

        // Verify reduction is vectorizable
        if (!isVectorizableReduction(result.reductionKind)) {
            result.isLegal = 0;
            result.illegalityReason = "Non-vectorizable reduction";
            return result;
        }
    }

    // Check 5: Trip count must be known or bounded
    BackedgeTakenInfo BTI = SE->getBackedgeTakenInfo(L);
    if (!BTI.isKnown() && !BTI.hasUpperBound()) {
        // Unknown trip count: may need runtime checks
        result.requiresRuntimeChecks = 1;
    }

    return result;
}
```

### Phase 2: Profitability Analysis (Cost Model)

**Goal**: Estimate performance benefit vs. overhead of vectorization.

```c
struct VectorizationCostModel {
    int loopSize;                  // Instructions in loop body
    int tripCount;                 // Known or estimated iterations
    int vectorizationFactor;       // Chosen VF (2, 4, 8, 16, 32...)

    // Cost components
    float scalarCost;              // Cost of scalar execution
    float vectorCost;              // Cost of vectorized execution
    float overheadCost;            // Cost of vector setup/cleanup

    // GPU-specific
    int targetArchitecture;        // SM version (Volta, Turing, Ampere...)
    int hasTensorCoreOpportunity;  // Can use WMMA instructions
    int warpDivergenceCost;        // Estimated divergence penalty
};

int computeVectorizationFactor(Loop* L, VectorizationCostModel* model,
                                TargetTransformInfo* TTI) {
    // Determine maximum legal vectorization factor

    // Step 1: Architecture-dependent maximum
    int maxVF = TTI->getMaxVectorWidth();  // e.g., 32 for GPU, 8 for AVX-256

    // Step 2: Data type constraints
    Type* scalarType = getPrimaryScalarType(L);
    int typeWidth = scalarType->getPrimitiveSizeInBits();

    int maxVFByType = maxVF * (32 / typeWidth);  // Adjust for element size
    if (maxVFByType < maxVF) {
        maxVF = maxVFByType;
    }

    // Step 3: Memory alignment constraints
    int alignmentVF = getMaxAlignedVectorFactor(L);
    if (alignmentVF < maxVF) {
        maxVF = alignmentVF;
    }

    // Step 4: Dependence distance constraint
    if (model->hasLoopCarriedDependence) {
        int depDistance = model->dependenceDistance;
        if (depDistance < maxVF) {
            maxVF = depDistance;  // VF must not exceed dependence distance
        }
    }

    // Step 5: Register pressure constraint (critical for GPU)
    int registerVF = estimateMaxVFByRegisterPressure(L, TTI);
    if (registerVF < maxVF) {
        maxVF = registerVF;
    }

    // Step 6: Cost-benefit analysis for each candidate VF
    int bestVF = 1;  // Start with scalar (no vectorization)
    float bestSpeedup = 1.0f;

    for (int vf = 2; vf <= maxVF; vf *= 2) {  // Try powers of 2
        float scalarCost = estimateScalarCost(L, model->tripCount);
        float vectorCost = estimateVectorCost(L, vf, model->tripCount, TTI);
        float overhead = estimateVectorizationOverhead(L, vf);

        float totalVectorCost = vectorCost + overhead;
        float speedup = scalarCost / totalVectorCost;

        if (speedup > bestSpeedup && speedup > 1.2) {  // Require 20% improvement
            bestSpeedup = speedup;
            bestVF = vf;
        }
    }

    return bestVF;
}
```

### Phase 3: Vector Code Generation

**Goal**: Transform scalar loop into vectorized form.

```c
void performVectorization(Loop* L, int VF, VectorizationLegality* legal) {
    BasicBlock* preheader = L->preheader;
    BasicBlock* header = L->header;
    BasicBlock* latch = L->latchBlock;

    // Step 1: Create vector loop structure
    BasicBlock* vectorPreheader = createVectorPreheader(L);
    BasicBlock* vectorHeader = createBasicBlock("vector.header");
    BasicBlock* vectorBody = createBasicBlock("vector.body");
    BasicBlock* vectorLatch = createBasicBlock("vector.latch");

    // Step 2: Generate runtime checks if needed
    if (legal->requiresRuntimeChecks) {
        BasicBlock* runtimeCheckBlock = createRuntimeChecks(L, VF);
        vectorPreheader = insertRuntimeCheckBranch(preheader, runtimeCheckBlock,
                                                    vectorHeader, header);
    }

    // Step 3: Compute vector trip count
    // vector_trip_count = ⌈scalar_trip_count / VF⌉
    Value* scalarTripCount = computeTripCount(L);
    Value* VFValue = ConstantInt::get(VF);
    Value* vectorTripCount = UDiv(scalarTripCount, VFValue);

    // Step 4: Widen loop body instructions
    // Transform: scalar_value = op(a, b)
    //        to: vector_value = vector_op(<a0,a1,...,aVF-1>, <b0,b1,...,bVF-1>)

    for (int i = 0; i < L->blocks.size; i++) {
        BasicBlock* scalarBB = L->blocks.elements[i];

        for (int j = 0; j < scalarBB->numInstructions; j++) {
            Instruction* scalarInst = scalarBB->instructions[j];

            if (isVectorizable(scalarInst)) {
                Instruction* vectorInst = widenInstruction(scalarInst, VF);
                vectorBody->insertInstruction(vectorInst);

            } else if (isUniform(scalarInst, L)) {
                // Uniform instruction: replicate once, broadcast to all lanes
                Instruction* uniformInst = cloneInstruction(scalarInst);
                vectorBody->insertInstruction(uniformInst);

            } else {
                // Non-vectorizable: scalarize or handle specially
                scalarizeInstruction(scalarInst, VF, vectorBody);
            }
        }
    }

    // Step 5: Handle reductions
    if (legal->hasReductions) {
        generateVectorReduction(L, VF, legal->reductionKind, vectorBody);
    }

    // Step 6: Generate scalar epilogue loop (remainder)
    // Handles last (trip_count % VF) iterations
    Loop* epilogueLoop = cloneLoop(L);
    BasicBlock* epilogueCheck = createRemainderCheck(vectorLatch, epilogueLoop, VF);

    // Step 7: Update loop metadata
    MDNode* vectorMetadata = createVectorLoopMetadata(VF);
    vectorHeader->setMetadata("llvm.loop.vectorized", vectorMetadata);
}
```

### Phase 4: Memory Access Widening

**Critical for Performance**: Convert scalar loads/stores to vector operations.

```c
Instruction* widenMemoryAccess(Instruction* scalarInst, int VF, Loop* L) {
    if (LoadInst* load = dyn_cast<LoadInst>(scalarInst)) {
        // Scalar: value = load ptr
        // Vector: <v0,v1,...,vVF-1> = load <VF x type>* ptr

        // Check if consecutive access pattern
        if (isConsecutiveAccess(load, L)) {
            // Generate vector load
            Type* scalarType = load->getType();
            VectorType* vecType = VectorType::get(scalarType, VF);
            Value* ptr = load->getPointerOperand();

            // Cast pointer to vector pointer
            PointerType* vecPtrType = PointerType::get(vecType, ptr->getAddressSpace());
            Value* vecPtr = BitCast(ptr, vecPtrType);

            // Create vector load
            LoadInst* vecLoad = new LoadInst(vecType, vecPtr);
            vecLoad->setAlignment(Align(VF * scalarType->getPrimitiveSizeInBytes()));

            return vecLoad;

        } else if (isStridedAccess(load, L)) {
            // Strided access: gather operation
            // value[i] = load(base + i * stride)

            return generateGatherLoad(load, VF, L);

        } else {
            // Irregular access: full scalarization
            return scalarizeLoad(load, VF, L);
        }

    } else if (StoreInst* store = dyn_cast<StoreInst>(scalarInst)) {
        // Similar logic for stores
        if (isConsecutiveAccess(store, L)) {
            return generateVectorStore(store, VF, L);
        } else if (isStridedAccess(store, L)) {
            return generateScatterStore(store, VF, L);
        } else {
            return scalarizeStore(store, VF, L);
        }
    }

    return NULL;
}
```

### Phase 5: Reduction Vectorization

**Patterns Recognized**: Sum, product, min, max, and, or, xor

```c
void generateVectorReduction(Loop* L, int VF, enum ReductionKind kind,
                              BasicBlock* vectorBody) {
    // Example: Sum reduction
    // scalar: sum = 0; for (i) sum += A[i];
    // vector: vsum = <0,0,0,0>; for (i+=VF) vsum += <A[i],A[i+1],A[i+2],A[i+3]>;
    //         sum = vsum[0] + vsum[1] + vsum[2] + vsum[3];

    // Step 1: Initialize vector accumulator
    Type* scalarType = getReductionType(L);
    VectorType* vecType = VectorType::get(scalarType, VF);
    Value* identity = getReductionIdentity(kind, scalarType);  // e.g., 0 for sum
    Value* vecAccum = createVectorSplat(identity, VF);

    // Step 2: Vector accumulation in loop
    for (int i = 0; i < VF; i++) {
        Value* element = extractVectorElement(vecAccum, i);
        Value* newValue = loadElement(i);
        Value* reduced = performReductionOp(kind, element, newValue);
        vecAccum = insertVectorElement(vecAccum, reduced, i);
    }

    // Step 3: Horizontal reduction after loop
    // Reduce <v0, v1, v2, v3> to scalar
    Value* finalValue = horizontalReduce(vecAccum, kind, VF);

    // Example for sum with VF=4:
    // tmp0 = v0 + v1
    // tmp1 = v2 + v3
    // result = tmp0 + tmp1

    return finalValue;
}
```

---

## Configuration Parameters

**Evidence**: Based on LLVM vectorization infrastructure and CICC optimization framework

| Parameter | Type | Default | Range | Purpose |
|-----------|------|---------|-------|---------|
| `vectorize-loops` | bool | **true** | - | Master enable for loop vectorization |
| `force-vector-width` | int | **0** (auto) | 0-64 | Override: force specific VF |
| `force-vector-interleave` | int | **0** (auto) | 0-16 | Interleave factor |
| `vectorize-memory-check-threshold` | int | **128** | 0-512 | Max runtime memory checks |
| `vectorize-num-stores-pred` | int | **1** | 0-8 | Max predicated stores |
| `vectorize-slp-max-store-lookup` | int | **32** | 0-128 | SLP vectorizer store search depth |
| `max-vectorize-factor` | int | **0** (unlimited) | 0-64 | Hard limit on VF |
| `vectorize-minimize-bit-widths` | bool | **true** | - | Minimize types for better VF |
| `enable-interleaved-mem-accesses` | bool | **true** | - | Enable interleave group vectorization |
| `enable-masked-mem-accesses` | bool | **true** | - | Use masked loads/stores (AVX-512, SVE) |
| `prefer-predicate-over-epilogue` | bool | **false** | - | Use predication instead of epilogue loop |

**Command-Line Overrides**:
```bash
# Force vectorization factor to 8
-mllvm -force-vector-width=8

# Disable loop vectorization
-mllvm -vectorize-loops=false

# Increase memory check threshold
-mllvm -vectorize-memory-check-threshold=256

# Enable predication over epilogue
-mllvm -prefer-predicate-over-epilogue=true
```

---

## Data Structures

### Vectorization Context

```c
struct VectorizationContext {
    Loop* originalLoop;
    int vectorizationFactor;       // VF (2, 4, 8, 16, 32...)
    int interleaveFactor;          // Unroll factor for vector loop

    // Legality and cost
    VectorizationLegality* legal;
    VectorizationCostModel* cost;

    // Mapping: scalar instruction → vector instruction
    map<Instruction*, Instruction*> scalarToVector;
    map<Instruction*, vector<Instruction*>> scalarToScalarized;

    // Induction variables
    vector<PHINode*> inductionVariables;
    PHINode* canonicalInductionVar;

    // Reductions
    vector<ReductionDescriptor> reductions;

    // Memory analysis
    vector<MemoryAccessGroup> memoryGroups;
    vector<RuntimePointerCheck> runtimeChecks;

    // Generated code
    Loop* vectorLoop;
    Loop* epilogueLoop;
    BasicBlock* runtimeCheckBlock;
};

struct ReductionDescriptor {
    PHINode* phi;                  // Reduction accumulator PHI
    enum ReductionKind {
        RK_IntegerAdd = 0,
        RK_IntegerMult = 1,
        RK_IntegerMin = 2,
        RK_IntegerMax = 3,
        RK_FloatAdd = 4,
        RK_FloatMult = 5,
        RK_FloatMin = 6,
        RK_FloatMax = 7,
        RK_IntegerAnd = 8,
        RK_IntegerOr = 9,
        RK_IntegerXor = 10
    } kind;
    Value* startValue;             // Initial accumulator value
    Instruction* exitValue;        // Final result
};

struct MemoryAccessGroup {
    vector<LoadInst*> loads;
    vector<StoreInst*> stores;
    int stride;                    // Access stride (1 = consecutive)
    int isInterleaved;             // Interleaved access pattern
};
```

---

## Pass Dependencies

### Required Analyses (Must Run Before)

| Analysis Pass | Purpose | Dependency Type |
|---------------|---------|-----------------|
| **LoopSimplify** | Canonical loop form | CRITICAL |
| **LoopInfo** | Loop structure | CRITICAL |
| **ScalarEvolution** | Induction variable analysis | CRITICAL |
| **DependenceAnalysis** | Loop-carried dependence detection | CRITICAL |
| **AliasAnalysis** | Pointer aliasing disambiguation | CRITICAL |
| **DominatorTree** | Control flow dominance | REQUIRED |
| **TargetTransformInfo** | Architecture-specific costs | REQUIRED |
| **AssumptionCache** | Compiler assumptions | OPTIONAL |
| **DemandedBits** | Live bit analysis | OPTIONAL |

### Invalidated Analyses (Must Recompute After)

- **LoopInfo**: Loop structure modified
- **ScalarEvolution**: New induction variables created
- **DominatorTree**: New blocks and edges added
- **MemorySSA**: Memory access patterns changed

---

## Integration with Other Passes

### Pipeline Position

**Typical Ordering**:
```
1. LoopSimplify
2. LICM (hoist invariants)
3. LoopRotate (do-while form)
4. IndVarSimplify
5. LoopUnroll (may unroll before vectorize)
6. LoopVectorize        ← THIS PASS
7. SLPVectorize (vectorize within basic block)
8. InstCombine (cleanup)
9. SimplifyCFG
```

### Interaction with LoopUnroll

**Strategy 1: Unroll-then-Vectorize**
- Unroll loop by factor 2-4
- Vectorize unrolled loop body
- **Benefit**: Exposes more parallelism, better instruction scheduling

**Strategy 2: Vectorize-then-Unroll**
- Vectorize first
- Unroll vector loop
- **Benefit**: Reduced overhead, better register utilization

**CICC Heuristic**: Typically unroll small amount (2×) before vectorizing

### Interaction with SLPVectorize

**SLP (Superword Level Parallelism)**: Vectorizes within basic blocks

**Complementary**: LoopVectorize handles cross-iteration parallelism, SLP handles within-iteration

```c
// LoopVectorize handles this
for (int i = 0; i < N; i++) {
    A[i] = B[i] + C[i];  // Vectorize across iterations
}

// SLPVectorize handles this
A[0] = B[0] + C[0];
A[1] = B[1] + C[1];
A[2] = B[2] + C[2];
A[3] = B[3] + C[3];
// → Single vector instruction
```

---

## CUDA/GPU Considerations

### Tensor Core Vectorization

**Volta+ Architectures**: Map matrix operations to WMMA (Warp Matrix Multiply-Accumulate)

```cuda
// Original matrix multiply loop
for (int i = 0; i < M; i++) {
    for (int j = 0; j < N; j++) {
        float sum = 0.0f;
        for (int k = 0; k < K; k++) {
            sum += A[i][k] * B[k][j];
        }
        C[i][j] = sum;
    }
}

// After tensor core vectorization
// Map to wmma::fragment and wmma::mma_sync
wmma::fragment<wmma::matrix_a, 16, 16, 16, half, wmma::row_major> a_frag;
wmma::fragment<wmma::matrix_b, 16, 16, 16, half, wmma::col_major> b_frag;
wmma::fragment<wmma::accumulator, 16, 16, 16, float> c_frag;

wmma::load_matrix_sync(a_frag, A, K);
wmma::load_matrix_sync(b_frag, B, N);
wmma::fill_fragment(c_frag, 0.0f);

wmma::mma_sync(c_frag, a_frag, b_frag, c_frag);  // Single WMMA instruction

wmma::store_matrix_sync(C, c_frag, N, wmma::mem_row_major);
```

**Performance Impact**: 8-16× speedup for FP16 matrix operations on Volta/Turing/Ampere

### Warp-Level Vectorization

**Implicit SIMD**: 32 threads in warp execute same instruction

```cuda
// Original: Each thread processes one element
__global__ void kernel(float* A, float* B, int N) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < N) {
        A[idx] = B[idx] * 2.0f;
    }
}

// Warp-level vectorization: Threads process multiple elements
__global__ void kernel_vectorized(float* A, float* B, int N) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;

    // Each thread processes 4 elements (VF=4)
    float4 b = reinterpret_cast<float4*>(B)[idx];
    float4 a;
    a.x = b.x * 2.0f;
    a.y = b.y * 2.0f;
    a.z = b.z * 2.0f;
    a.w = b.w * 2.0f;
    reinterpret_cast<float4*>(A)[idx] = a;
}
// Each warp now processes 32 × 4 = 128 elements
```

### Memory Coalescing

**Critical**: Vectorized memory accesses must be coalesced for GPU efficiency

```cuda
// Good vectorization: Coalesced access
// Threads 0-31 access consecutive memory
for (int i = threadIdx.x; i < N; i += blockDim.x * 4) {
    float4 data = load_float4(&A[i]);  // Coalesced: 128-byte transaction
    // Process...
    store_float4(&B[i], result);
}

// Bad vectorization: Strided access
// Threads access with large stride → multiple transactions
for (int i = threadIdx.x * 4; i < N; i += blockDim.x * 4) {
    float4 data = load_float4(&A[i]);  // Not coalesced
}
```

### Register Pressure and Occupancy

**Critical Trade-off**: Vectorization increases register usage

**Impact on Occupancy**:
```
Scalar version:  8 registers/thread  → 2048 threads/SM (Ampere)
Vector version:  24 registers/thread → 682 threads/SM

Occupancy: 2048 threads → 100%
           682 threads  → 33%

Performance: Lower occupancy may reduce benefit if memory-bound
```

**CICC Heuristic**: Conservative vectorization for GPU kernels with high register pressure

---

## Performance Impact

### Expected Speedup

| Scenario | Vectorization Factor | Typical Speedup | Limiting Factor |
|----------|---------------------|-----------------|-----------------|
| **Simple arithmetic loop** | 4-8× | 3-7× | Memory bandwidth |
| **Reduction** | 4-8× | 2-4× | Horizontal reduction overhead |
| **Strided access** | 4× | 1.5-2.5× | Gather/scatter cost |
| **Complex control flow** | 2-4× | 1.2-1.8× | Predication/masking overhead |
| **Tensor core (GPU)** | 16× | 8-16× | Memory bandwidth |
| **Warp vectorization (GPU)** | 32× | 10-25× | Coalescing efficiency |

### Code Size Impact

**Growth**: Minimal for vector loop itself, but epilogue loop adds ~20-50% code

```
Original loop:    30 instructions
Vector loop:      35 instructions (vector ops slightly larger)
Epilogue loop:    30 instructions (copy of original)
Total:            65 instructions (2.2× growth)
```

---

## Examples

### Example 1: Simple Vector Loop

**Original C**:
```c
void add_arrays(float* A, float* B, float* C, int N) {
    for (int i = 0; i < N; i++) {
        C[i] = A[i] + B[i];
    }
}
```

**After Vectorization (VF=4, assuming AVX/NEON)**:
```c
void add_arrays(float* A, float* B, float* C, int N) {
    int i;
    // Vector loop
    for (i = 0; i < N - 3; i += 4) {
        float4 a = load_float4(&A[i]);
        float4 b = load_float4(&B[i]);
        float4 c = a + b;  // Single vector add
        store_float4(&C[i], c);
    }

    // Scalar epilogue
    for (; i < N; i++) {
        C[i] = A[i] + B[i];
    }
}
```

**IR Transformation**:
```llvm
; Original scalar loop
define void @add_arrays(float* %A, float* %B, float* %C, i32 %N) {
entry:
  br label %loop

loop:
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  %a_ptr = getelementptr float, float* %A, i32 %i
  %a = load float, float* %a_ptr
  %b_ptr = getelementptr float, float* %B, i32 %i
  %b = load float, float* %b_ptr
  %c = fadd float %a, %b
  %c_ptr = getelementptr float, float* %C, i32 %i
  store float %c, float* %c_ptr
  %i.next = add i32 %i, 1
  %cmp = icmp ult i32 %i.next, %N
  br i1 %cmp, label %loop, label %exit

exit:
  ret void
}

; After vectorization (VF=4)
define void @add_arrays(float* %A, float* %B, float* %C, i32 %N) {
entry:
  %N.minus.3 = sub i32 %N, 3
  br label %vector.loop

vector.loop:
  %vi = phi i32 [ 0, %entry ], [ %vi.next, %vector.loop ]

  ; Vector load A[i:i+3]
  %a_ptr = getelementptr float, float* %A, i32 %vi
  %a_vec_ptr = bitcast float* %a_ptr to <4 x float>*
  %a_vec = load <4 x float>, <4 x float>* %a_vec_ptr, align 16

  ; Vector load B[i:i+3]
  %b_ptr = getelementptr float, float* %B, i32 %vi
  %b_vec_ptr = bitcast float* %b_ptr to <4 x float>*
  %b_vec = load <4 x float>, <4 x float>* %b_vec_ptr, align 16

  ; Vector add
  %c_vec = fadd <4 x float> %a_vec, %b_vec

  ; Vector store C[i:i+3]
  %c_ptr = getelementptr float, float* %C, i32 %vi
  %c_vec_ptr = bitcast float* %c_ptr to <4 x float>*
  store <4 x float> %c_vec, <4 x float>* %c_vec_ptr, align 16

  ; Increment by VF
  %vi.next = add i32 %vi, 4
  %cmp = icmp ult i32 %vi.next, %N.minus.3
  br i1 %cmp, label %vector.loop, label %scalar.epilogue

scalar.epilogue:
  ; Original scalar loop for remaining iterations
  ...
}
```

### Example 2: Reduction

**Original C**:
```c
float sum_array(float* A, int N) {
    float sum = 0.0f;
    for (int i = 0; i < N; i++) {
        sum += A[i];
    }
    return sum;
}
```

**After Vectorization**:
```c
float sum_array(float* A, int N) {
    int i;
    float4 vsum = {0.0f, 0.0f, 0.0f, 0.0f};

    // Vector loop
    for (i = 0; i < N - 3; i += 4) {
        float4 a = load_float4(&A[i]);
        vsum = vsum + a;  // Vector accumulation
    }

    // Horizontal reduction
    float sum = vsum[0] + vsum[1] + vsum[2] + vsum[3];

    // Scalar epilogue
    for (; i < N; i++) {
        sum += A[i];
    }

    return sum;
}
```

---

## Debugging and Analysis

### Vectorization Reports

With `-Rpass=loop-vectorize`:
```
loop-vectorize: vectorized loop (VF=4, interleave=2)
loop-vectorize: loop not vectorized: backward dependence detected
loop-vectorize: loop not vectorized: trip count too small
```

### Statistics

With `-stats`:
```
NumVectorizedLoops: 45         # Loops successfully vectorized
TotalVectorizationFactor: 180  # Sum of all VFs
AverageVF: 4.0                 # Mean vectorization factor
NumRejectedDependence: 12      # Rejected due to dependence
NumRejectedCost: 8             # Rejected due to cost model
```

### Disabling for Debugging

```bash
# Disable all vectorization
-mllvm -vectorize-loops=false

# Force specific VF
-mllvm -force-vector-width=4

# Disable epilogue (use predication)
-mllvm -prefer-predicate-over-epilogue=true
```

---

## Pragma Support

### #pragma clang loop vectorize(enable)

```c
#pragma clang loop vectorize(enable)
for (int i = 0; i < N; i++) {
    // Force vectorization
}
```

### #pragma clang loop vectorize_width(N)

```c
#pragma clang loop vectorize_width(8)
for (int i = 0; i < N; i++) {
    // Force VF=8
}
```

### #pragma clang loop vectorize(disable)

```c
#pragma clang loop vectorize(disable)
for (int i = 0; i < N; i++) {
    // Disable vectorization
}
```

---

## Known Limitations

1. **Backward Dependences**: Cannot vectorize with loop-carried backward dependence
2. **Complex Control Flow**: Multiple exits, complex branching limits vectorization
3. **Irregular Memory Access**: Gather/scatter operations have high overhead
4. **Reductions**: Horizontal reduction overhead reduces speedup
5. **Small Trip Counts**: Overhead of setup/epilogue dominates for small N
6. **GPU Register Pressure**: Conservative VF to maintain occupancy

---

## Related Optimizations

- **LoopUnroll**: [loop-unroll.md](loop-unroll.md) - Often combined with vectorization
- **SLPVectorize**: Vectorizes straight-line code within basic blocks
- **LICM**: [licm.md](licm.md) - Hoists invariants before vectorization
- **LoopInterchange**: [loop-interchange.md](loop-interchange.md) - Reorders nested loops for vectorization
- **LoopDistribute**: [loop-distribute.md](loop-distribute.md) - Splits loops to enable vectorization

---

## References

1. **LLVM Loop Vectorization**: https://llvm.org/docs/Vectorizers.html#loop-vectorizer
2. **Allen, R., & Kennedy, K.** (2001). "Optimizing Compilers for Modern Architectures."
3. **CUDA Programming Guide**: Tensor Core programming
4. **Intel AVX Documentation**: https://software.intel.com/content/www/us/en/develop/articles/introduction-to-intel-advanced-vector-extensions.html
5. **LLVM Source**: `lib/Transforms/Vectorize/LoopVectorize.cpp`

---

**L3 Analysis Quality**: HIGH (based on LLVM implementation + GPU considerations)
**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json + LLVM/CUDA documentation
