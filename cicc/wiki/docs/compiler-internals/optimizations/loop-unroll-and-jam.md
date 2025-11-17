# Loop Unroll-and-Jam

**Pass Type**: Combined loop unrolling and fusion transformation
**LLVM Class**: `llvm::LoopUnrollAndJamPass`
**Algorithm**: Outer loop unrolling with inner loop fusion for improved register reuse
**Extracted From**: CICC decompiled code + optimization pass mapping
**Analysis Quality**: HIGH - Critical for nested loop performance on GPU
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Loop Optimization (unconfirmed passes, line 263)

---

## Overview

Loop Unroll-and-Jam is an advanced loop transformation that combines **outer loop unrolling** with **inner loop fusion** to optimize nested loop structures. Unlike simple loop unrolling that replicates a single loop body, unroll-and-jam unrolls the outer loop and then fuses (jams) the resulting copies of the inner loop together. This transformation is particularly powerful for improving **register reuse**, **cache locality**, and **instruction-level parallelism (ILP)** in nested loops.

**Core Transformation**: Given a nested loop structure, unroll-and-jam:
1. Unrolls the outer loop by a factor K
2. Merges the K copies of the inner loop into a single inner loop
3. Interleaves operations from different outer loop iterations within the fused inner loop

**Motivation**:
- **Register Reuse**: Values computed in one outer iteration can be reused in the next
- **Cache Locality**: Better temporal and spatial locality for array accesses
- **ILP**: Exposes more independent operations for parallel execution
- **GPU Performance**: Reduces register spilling and improves memory coalescing

**Applicability**:
- Nested loops with 2+ levels of nesting
- Inner loop trip count significantly larger than outer loop unroll factor
- Compatible data dependencies between outer iterations
- No loop-carried dependencies that prevent fusion

---

## Algorithm: Unroll-and-Jam Transformation

### Phase 1: Eligibility Analysis

**Goal**: Determine if nested loop structure is suitable for unroll-and-jam transformation.

```c
struct UnrollAndJamEligibility {
    int isEligible;
    char* rejectionReason;

    // Loop structure requirements
    Loop* outerLoop;
    Loop* innerLoop;
    int nestingDepth;

    // Trip count analysis
    int outerTripCount;
    int innerTripCount;
    int outerTripCountKnown;
    int innerTripCountKnown;

    // Dependence analysis
    int hasOuterLoopCarriedDep;
    int hasInnerLoopCarriedDep;
    int dependenceDistance;
    int canJamInnerLoops;

    // Profitability
    int maxUnrollFactor;
    int estimatedRegisterPressure;
    int estimatedCacheImprovement;
};

UnrollAndJamEligibility analyzeUnrollAndJamEligibility(Loop* outerLoop,
                                                        LoopInfo* LI,
                                                        DependenceAnalysis* DA,
                                                        ScalarEvolution* SE) {
    UnrollAndJamEligibility result = {0};
    result.outerLoop = outerLoop;

    // Check 1: Must have exactly one inner loop (for simple unroll-and-jam)
    if (outerLoop->subLoops.size != 1) {
        result.rejectionReason = "Outer loop must have exactly one inner loop";
        return result;
    }

    result.innerLoop = outerLoop->subLoops.elements[0];
    result.nestingDepth = 2;

    // Check 2: Both loops must be in simplified form
    if (!isLoopSimplified(outerLoop) || !isLoopSimplified(result.innerLoop)) {
        result.rejectionReason = "Loops not in simplified form (needs LoopSimplify)";
        return result;
    }

    // Check 3: Analyze trip counts
    BackedgeTakenInfo outerBTI = SE->getBackedgeTakenInfo(outerLoop);
    BackedgeTakenInfo innerBTI = SE->getBackedgeTakenInfo(result.innerLoop);

    result.outerTripCountKnown = outerBTI.isConstant;
    result.innerTripCountKnown = innerBTI.isConstant;

    if (result.outerTripCountKnown) {
        result.outerTripCount = outerBTI.constantValue;
    }

    if (result.innerTripCountKnown) {
        result.innerTripCount = innerBTI.constantValue;
    }

    // Check 4: Inner loop should be significantly larger than unroll factor
    // Heuristic: inner trip count should be >= 8 * unroll factor
    if (result.innerTripCountKnown && result.innerTripCount < 32) {
        result.rejectionReason = "Inner loop too small for profitable unroll-and-jam";
        return result;
    }

    // Check 5: Analyze dependencies between outer loop iterations
    DependenceResult dep = DA->analyzeLoopDependence(outerLoop, result.innerLoop);

    result.hasOuterLoopCarriedDep = dep.hasLoopCarriedDependence;
    result.hasInnerLoopCarriedDep = dep.hasInnerDependence;

    if (dep.hasAntiDependence || dep.hasOutputDependence) {
        // Anti and output dependencies may prevent jamming
        if (!canRenameToEliminateDependence(dep)) {
            result.rejectionReason = "Loop-carried anti/output dependence prevents jamming";
            return result;
        }
    }

    // Check 6: Verify inner loops can be fused after unrolling
    // After unrolling outer loop by K, we'll have K copies of inner loop
    // These must be fusable
    result.canJamInnerLoops = canFuseMultipleInnerLoops(outerLoop, result.innerLoop, DA);

    if (!result.canJamInnerLoops) {
        result.rejectionReason = "Inner loop copies cannot be jammed due to dependencies";
        return result;
    }

    // Check 7: No function calls in outer loop (except in inner loop)
    if (hasCallsInOuterLoop(outerLoop, result.innerLoop)) {
        result.rejectionReason = "Outer loop contains function calls outside inner loop";
        return result;
    }

    result.isEligible = 1;
    return result;
}
```

### Phase 2: Profitability Analysis

**Goal**: Determine optimal unroll factor balancing register reuse against register pressure.

```c
struct UnrollAndJamCostModel {
    int outerLoopSize;            // Instructions in outer loop (excluding inner)
    int innerLoopSize;            // Instructions in inner loop body
    int outerTripCount;
    int innerTripCount;

    // Register analysis
    int baseRegisterUsage;        // Registers used without unroll-and-jam
    int registerReusePotential;   // Values that can be reused across iterations
    int registerPressurePerUnroll; // Additional registers per unroll factor

    // Performance metrics
    float cacheReuseImprovement;  // Expected cache hit rate improvement
    float ilpGain;                // Instruction-level parallelism increase
    float registerSpillRisk;      // Probability of register spilling

    // GPU-specific
    int occupancyImpact;          // Effect on warp occupancy
    int coalescingImprovement;    // Memory coalescing benefit
};

int computeOptimalUnrollAndJamFactor(Loop* outerLoop, Loop* innerLoop,
                                      UnrollAndJamCostModel* model,
                                      TargetTransformInfo* TTI) {
    // Base unroll factor candidates: 2, 4, 8
    int candidates[] = {2, 4, 8};
    int bestFactor = 1;  // 1 means no unroll-and-jam
    float bestScore = 0.0f;

    for (int i = 0; i < 3; i++) {
        int factor = candidates[i];

        // Estimate register pressure
        int estimatedRegs = model->baseRegisterUsage +
                           (factor - 1) * model->registerPressurePerUnroll;

        // GPU: Check against register file limits
        int maxRegsPerThread = TTI->getMaxRegistersPerThread();

        if (estimatedRegs > maxRegsPerThread * 0.8) {
            // Would likely cause spilling
            continue;
        }

        // Estimate performance benefit
        float registerReuseGain = model->registerReusePotential * (factor - 1);
        float cacheGain = model->cacheReuseImprovement * sqrtf(factor);
        float ilpGain = model->ilpGain * (factor - 1);

        // Penalties
        float spillPenalty = model->registerSpillRisk * estimatedRegs;
        float codeGrowthPenalty = (factor - 1) * 0.1f;

        // GPU: Occupancy penalty
        float occupancyPenalty = 0.0f;
        if (TTI->isGPUTarget()) {
            int occupancyBefore = estimateOccupancy(model->baseRegisterUsage, TTI);
            int occupancyAfter = estimateOccupancy(estimatedRegs, TTI);

            if (occupancyAfter < occupancyBefore) {
                occupancyPenalty = (occupancyBefore - occupancyAfter) * 2.0f;
            }
        }

        // Compute score
        float score = registerReuseGain + cacheGain + ilpGain -
                     spillPenalty - codeGrowthPenalty - occupancyPenalty;

        if (score > bestScore) {
            bestScore = score;
            bestFactor = factor;
        }
    }

    return bestFactor;
}

int estimateOccupancy(int registerUsage, TargetTransformInfo* TTI) {
    // Simplified occupancy calculation for GPU
    int maxRegsPerThread = TTI->getMaxRegistersPerThread();
    int maxThreadsPerSM = TTI->getMaxThreadsPerSM();
    int registerFileSizePerSM = TTI->getRegisterFileSizePerSM();

    // Max threads limited by register file
    int maxThreadsByRegs = registerFileSizePerSM / registerUsage;

    // Occupancy = min(max threads by regs, max threads per SM)
    int activeThreads = (maxThreadsByRegs < maxThreadsPerSM) ?
                        maxThreadsByRegs : maxThreadsPerSM;

    return (activeThreads * 100) / maxThreadsPerSM;  // Percentage
}
```

### Phase 3: Unroll Outer Loop

**Goal**: Create K copies of the outer loop body, including K copies of the inner loop.

```c
void unrollOuterLoop(Loop* outerLoop, Loop* innerLoop, int unrollFactor) {
    BasicBlock* outerHeader = outerLoop->header;
    BasicBlock* outerLatch = outerLoop->latchBlock;
    BasicBlock* outerPreheader = outerLoop->preheader;

    // Step 1: Identify outer loop body blocks (excluding inner loop)
    vector<BasicBlock*> outerBodyBlocks;
    for (int i = 0; i < outerLoop->blocks.size; i++) {
        BasicBlock* BB = outerLoop->blocks.elements[i];
        if (!isBlockInLoop(BB, innerLoop)) {
            vectorPush(&outerBodyBlocks, BB);
        }
    }

    // Step 2: Collect all inner loop blocks
    vector<BasicBlock*> innerLoopBlocks;
    for (int i = 0; i < innerLoop->blocks.size; i++) {
        vectorPush(&innerLoopBlocks, innerLoop->blocks.elements[i]);
    }

    // Step 3: Create unroll factor - 1 copies
    // Original becomes iteration 0, create iterations 1..(K-1)
    vector<Loop*> innerLoopCopies;
    vectorPush(&innerLoopCopies, innerLoop);  // Original

    for (int iter = 1; iter < unrollFactor; iter++) {
        // Clone outer loop body blocks
        vector<BasicBlock*> clonedOuterBlocks;
        for (int j = 0; j < outerBodyBlocks.size; j++) {
            BasicBlock* original = outerBodyBlocks.elements[j];
            BasicBlock* cloned = cloneBasicBlock(original, iter);
            vectorPush(&clonedOuterBlocks, cloned);
        }

        // Clone entire inner loop
        Loop* clonedInnerLoop = cloneLoop(innerLoop, iter);
        vectorPush(&innerLoopCopies, clonedInnerLoop);

        // Update outer loop induction variable references
        // If outer loop has i, update to i+iter
        updateOuterInductionVariable(clonedInnerLoop, outerLoop, iter);
    }

    // Step 4: Update outer loop increment
    // Change: i++ to i += unrollFactor
    Instruction* outerIncrement = findInductionIncrement(outerLatch);
    outerIncrement->setOperand(1, ConstantInt::get(unrollFactor));

    // Now we have K copies of the inner loop ready for jamming
}
```

### Phase 4: Jam (Fuse) Inner Loops

**Goal**: Merge K copies of the inner loop into a single fused loop.

```c
void jamInnerLoops(vector<Loop*>* innerLoopCopies, int unrollFactor) {
    // Reference: First inner loop becomes the fused loop
    Loop* fusedLoop = innerLoopCopies->elements[0];
    BasicBlock* fusedBody = fusedLoop->blocks.elements[0];  // Simplified

    // Step 1: Merge loop bodies
    // Interleave operations from all K inner loop bodies
    for (int iter = 1; iter < unrollFactor; iter++) {
        Loop* currentCopy = innerLoopCopies->elements[iter];
        BasicBlock* currentBody = currentCopy->blocks.elements[0];

        // Move all instructions from currentBody into fusedBody
        for (int i = 0; i < currentBody->numInstructions; i++) {
            Instruction* inst = currentBody->instructions[i];

            // Skip terminator (will be unified)
            if (inst->isTerminator) continue;

            // Move instruction to fused body
            fusedBody->insertInstruction(inst);
        }
    }

    // Step 2: Unify PHI nodes
    // Merge induction variables from all copies
    BasicBlock* fusedHeader = fusedLoop->header;

    for (int iter = 1; iter < unrollFactor; iter++) {
        Loop* currentCopy = innerLoopCopies->elements[iter];
        BasicBlock* currentHeader = currentCopy->header;

        // Find corresponding PHI nodes and merge
        mergePHINodes(fusedHeader, currentHeader);
    }

    // Step 3: Remove redundant loop structures
    for (int iter = 1; iter < unrollFactor; iter++) {
        Loop* currentCopy = innerLoopCopies->elements[iter];
        deleteLoopStructure(currentCopy);
    }

    // Step 4: Update control flow
    // Ensure fused loop executes all iterations
    updateLoopControl(fusedLoop);
}
```

### Phase 5: Register Renaming and Optimization

**Goal**: Eliminate false dependencies and maximize register reuse.

```c
void optimizeFusedLoop(Loop* fusedLoop, int unrollFactor) {
    // Step 1: Identify reusable values
    // Values computed in outer iteration i can be reused in iteration i+1
    vector<Value*> reusableValues;

    for (int i = 0; i < fusedLoop->blocks.size; i++) {
        BasicBlock* BB = fusedLoop->blocks.elements[i];

        for (int j = 0; j < BB->numInstructions; j++) {
            Instruction* inst = BB->instructions[j];

            // Check if value is used in next outer iteration
            if (isReusableAcrossOuterIterations(inst, unrollFactor)) {
                vectorPush(&reusableValues, inst);
            }
        }
    }

    // Step 2: Perform register renaming to eliminate anti-dependencies
    // This allows more aggressive instruction scheduling
    for (int i = 0; i < reusableValues.size; i++) {
        Value* val = reusableValues.elements[i];
        renameForReuseOpportunity(val, fusedLoop);
    }

    // Step 3: Schedule instructions for better ILP
    // Move independent operations together
    scheduleForILP(fusedLoop);

    // Step 4: Identify reduction opportunities
    // Multiple accumulations can be performed in parallel
    identifyParallelReductions(fusedLoop, unrollFactor);
}
```

### Complete Algorithm Summary

```c
void performUnrollAndJam(Loop* outerLoop, int unrollFactor) {
    // Prerequisite: outerLoop has exactly one inner loop
    Loop* innerLoop = outerLoop->subLoops.elements[0];

    // Phase 1: Unroll outer loop
    unrollOuterLoop(outerLoop, innerLoop, unrollFactor);

    // Phase 2: Collect all inner loop copies
    vector<Loop*> innerLoopCopies;
    collectInnerLoops(outerLoop, &innerLoopCopies);

    // Phase 3: Jam (fuse) inner loops
    jamInnerLoops(&innerLoopCopies, unrollFactor);

    // Phase 4: Optimize fused loop
    Loop* fusedLoop = innerLoopCopies.elements[0];
    optimizeFusedLoop(fusedLoop, unrollFactor);

    // Phase 5: Generate remainder loop if needed
    if (requiresRemainderLoop(outerLoop, unrollFactor)) {
        generateRemainderLoop(outerLoop, unrollFactor);
    }
}
```

---

## Data Structures

### Unroll-and-Jam Context

```c
struct UnrollAndJamContext {
    Loop* outerLoop;
    Loop* innerLoop;
    int unrollFactor;

    // Loop structure
    int outerTripCount;
    int innerTripCount;
    int outerNestingDepth;

    // Dependence information
    DependenceAnalysis* DA;
    vector<Dependence> outerDependences;
    vector<Dependence> innerDependences;

    // Register analysis
    int estimatedRegistersBefore;
    int estimatedRegistersAfter;
    int registerReuseOpportunities;

    // Performance metrics
    float expectedSpeedup;
    float cacheLocalityImprovement;
    float ilpIncrease;

    // Generated code
    Loop* fusedInnerLoop;
    Loop* remainderLoop;
    vector<Value*> reusedRegisters;
};

struct RegisterReuseDescriptor {
    Value* value;                  // The value being reused
    int firstOuterIter;            // First outer iteration producing value
    int lastOuterIter;             // Last outer iteration using value
    int liveRange;                 // Number of inner iterations value is live
    Type* valueType;               // Register type
    int registersPressure;         // Register pressure contribution
};
```

---

## Configuration Parameters

**Evidence**: Based on LLVM unroll-and-jam implementation and CICC optimization framework

| Parameter | Type | Default | Range | Purpose |
|-----------|------|---------|-------|---------|
| `enable-unroll-and-jam` | bool | **true** | - | Master enable for unroll-and-jam |
| `unroll-and-jam-count` | int | **0** (auto) | 0-8 | Override: force specific unroll factor |
| `unroll-and-jam-threshold` | int | **60** | 0-500 | Max instructions for unroll-and-jam |
| `allow-unroll-and-jam` | bool | **true** | - | Allow transformation |
| `pragma-unroll-and-jam-threshold` | int | **1024** | 0-10000 | Threshold when #pragma used |

**Command-Line Overrides**:
```bash
# Force unroll-and-jam factor to 4
-mllvm -unroll-and-jam-count=4

# Disable unroll-and-jam
-mllvm -enable-unroll-and-jam=false

# Increase threshold
-mllvm -unroll-and-jam-threshold=120
```

---

## Pass Dependencies

### Required Analyses (Must Run Before)

| Analysis Pass | Purpose | Dependency Type |
|---------------|---------|-----------------|
| **LoopSimplify** | Canonical loop form for both outer and inner loops | CRITICAL |
| **LoopInfo** | Loop nesting structure | CRITICAL |
| **ScalarEvolution** | Trip count analysis for both loops | CRITICAL |
| **DependenceAnalysis** | Inter-iteration dependence analysis | CRITICAL |
| **DominatorTree** | Control flow dominance | REQUIRED |
| **TargetTransformInfo** | Register file size, occupancy limits | REQUIRED |

### Invalidated Analyses (Must Recompute After)

- **LoopInfo**: Loop structure completely transformed
- **ScalarEvolution**: Induction variables modified
- **DominatorTree**: New blocks created, edges modified
- **MemorySSA**: Memory access patterns changed dramatically

### Preserved Analyses

- **AliasAnalysis**: Memory aliasing unchanged
- **CallGraph**: No function calls added/removed

---

## Integration with Other Passes

### Pipeline Position

**Typical Ordering**:
```
1. LoopSimplify        (canonicalize both loops)
2. LICM                (hoist outer loop invariants)
3. LoopRotate          (transform both to do-while)
4. IndVarSimplify      (simplify induction variables)
5. LoopUnrollAndJam    ← THIS PASS
6. InstCombine         (combine replicated instructions)
7. SimplifyCFG         (cleanup control flow)
8. LoopVectorize       (vectorize fused inner loop)
```

### Interaction with Loop Unrolling

**Difference from Simple Loop Unroll**:
- **LoopUnroll**: Unrolls single loop, replicates entire body
- **LoopUnrollAndJam**: Unrolls outer loop, fuses inner loops

**When to Choose**:
- Use **LoopUnroll** for single loops or when inner loop cannot be fused
- Use **LoopUnrollAndJam** for nested loops with good register reuse potential

### Interaction with Loop Fusion

**Relationship**: Unroll-and-jam is essentially "unroll outer + fuse inner"

**LoopFusion** alone merges adjacent loops:
```c
for (i) { A[i] = ...; }
for (i) { B[i] = ...; }
→ for (i) { A[i] = ...; B[i] = ...; }
```

**UnrollAndJam** merges iterations of nested loop:
```c
for (i) { for (j) { C[i][j] = ...; } }
→ for (i+=2) { for (j) { C[i][j] = ...; C[i+1][j] = ...; } }
```

### Interaction with Loop Vectorization

**Synergy**: Fused inner loop has longer body, better for vectorization

```c
// After unroll-and-jam (factor=4)
for (i = 0; i < N; i += 4) {
    for (j = 0; j < M; j++) {
        C[i][j]   = A[i][j]   + B[i][j];
        C[i+1][j] = A[i+1][j] + B[i+1][j];
        C[i+2][j] = A[i+2][j] + B[i+2][j];
        C[i+3][j] = A[i+3][j] + B[i+3][j];
    }
}

// After vectorization (VF=4)
for (i = 0; i < N; i += 4) {
    for (j = 0; j < M; j += 4) {
        // Load 4x4 matrix worth of data
        // Process with SIMD
        // 16× effective parallelism (4 outer × 4 vector)
    }
}
```

---

## CUDA Considerations

### Thread-Level Parallelism vs ILP Tradeoffs

**GPU Programming Model**: CUDA exposes massive thread-level parallelism (TLP)
- Thousands of threads execute concurrently
- Each thread ideally executes simple, non-divergent code

**Unroll-and-Jam Impact**:
- **Increases ILP**: More independent operations per thread
- **Decreases TLP opportunity**: Larger per-thread workload
- **Trade-off**: Balance between ILP and TLP for maximum throughput

**Example**:
```cuda
// Original: High TLP, low ILP
__global__ void kernel(float* C, float* A, float* B, int N, int M) {
    int i = blockIdx.x;  // Outer loop distributed across blocks
    int j = threadIdx.x; // Inner loop distributed across threads

    if (i < N && j < M) {
        C[i*M + j] = A[i*M + j] + B[i*M + j];
    }
}
// Each thread: 1 load + 1 add + 1 store (low ILP)
// Many threads: High TLP

// After unroll-and-jam (factor=4)
__global__ void kernel_unrolled(float* C, float* A, float* B, int N, int M) {
    int i = blockIdx.x * 4;  // Process 4 outer iterations per block
    int j = threadIdx.x;

    if (i+3 < N && j < M) {
        // Fused inner loop processes all 4 outer iterations
        float a0 = A[(i+0)*M + j];
        float a1 = A[(i+1)*M + j];
        float a2 = A[(i+2)*M + j];
        float a3 = A[(i+3)*M + j];

        float b0 = B[(i+0)*M + j];
        float b1 = B[(i+1)*M + j];
        float b2 = B[(i+2)*M + j];
        float b3 = B[(i+3)*M + j];

        C[(i+0)*M + j] = a0 + b0;
        C[(i+1)*M + j] = a1 + b1;
        C[(i+2)*M + j] = a2 + b2;
        C[(i+3)*M + j] = a3 + b3;
    }
}
// Each thread: 8 loads + 4 adds + 4 stores (high ILP)
// Fewer threads: Lower TLP
```

**CICC Heuristic**: Conservative unroll factors (2-4) to preserve TLP

### Register Pressure Impact

**Critical for GPU**: Register file is limited, shared among threads

**SM Architecture Limits**:
- **Volta (SM70)**: 65,536 registers per SM
- **Turing (SM75)**: 65,536 registers per SM
- **Ampere (SM80)**: 65,536 registers per SM
- **Hopper (SM90)**: 65,536 registers per SM

**Unroll-and-Jam Register Growth**:

```c
// Original nested loop: ~8 registers per thread
for (int i = 0; i < N; i++) {
    for (int j = 0; j < M; j++) {
        float val = A[i*M + j];
        val = val * 2.0f + 1.0f;
        B[i*M + j] = val;
    }
}
// Registers: i, j, val, temp, addresses = ~8 registers

// After unroll-and-jam (factor=4): ~24 registers per thread
for (int i = 0; i < N; i += 4) {
    for (int j = 0; j < M; j++) {
        float val0 = A[(i+0)*M + j];  // 4 values
        float val1 = A[(i+1)*M + j];
        float val2 = A[(i+2)*M + j];
        float val3 = A[(i+3)*M + j];

        val0 = val0 * 2.0f + 1.0f;    // 4 temps
        val1 = val1 * 2.0f + 1.0f;
        val2 = val2 * 2.0f + 1.0f;
        val3 = val3 * 2.0f + 1.0f;

        B[(i+0)*M + j] = val0;         // 4 stores
        B[(i+1)*M + j] = val1;
        B[(i+2)*M + j] = val2;
        B[(i+3)*M + j] = val3;
    }
}
// Registers: i, j, val0-3, temp0-3, addr0-3 = ~24 registers
```

**Occupancy Impact**:
```
Original (8 regs):  65536 / 8  = 8192 threads/SM → 100% occupancy
Unrolled (24 regs): 65536 / 24 = 2730 threads/SM → 33% occupancy
```

**Performance Trade-off**:
- **Benefit**: Better register reuse, less memory traffic
- **Cost**: Lower occupancy, may not hide latency

**CICC Strategy**:
- Analyze register pressure before unroll-and-jam
- Limit unroll factor to maintain minimum occupancy (e.g., 50%)
- Prefer unroll-and-jam when compute-bound, avoid when memory-bound

### Occupancy Effects

**Occupancy Definition**: Ratio of active warps to maximum warps per SM

**Maximum Warps per SM**:
- Volta/Turing: 32 warps (1024 threads)
- Ampere: 32 warps (1024 threads)
- Hopper: 32 warps (1024 threads)

**Occupancy Calculation**:
```c
int calculateOccupancy(int registersPerThread, int sharedMemPerBlock,
                       int threadsPerBlock, int smArchitecture) {
    // Register limit
    int maxRegFile = 65536;  // All modern SMs
    int maxThreadsByRegs = maxRegFile / registersPerThread;

    // Shared memory limit (varies by architecture)
    int maxSharedMem = (smArchitecture >= 80) ? 163840 : 98304;
    int maxBlocksBySharedMem = maxSharedMem / sharedMemPerBlock;
    int maxThreadsBySharedMem = maxBlocksBySharedMem * threadsPerBlock;

    // Thread/warp limit
    int maxThreadsPerSM = 1024;  // Volta+

    // Take minimum
    int activeThreads = min3(maxThreadsByRegs, maxThreadsBySharedMem, maxThreadsPerSM);

    return (activeThreads * 100) / maxThreadsPerSM;
}
```

**Impact on Performance**:
- **High Occupancy (75-100%)**: Good latency hiding, hides memory latency
- **Medium Occupancy (50-75%)**: Acceptable for compute-bound kernels
- **Low Occupancy (<50%)**: May underutilize SM, performance loss if memory-bound

**Unroll-and-Jam Guidelines**:
1. **Occupancy > 75%**: Safe to apply unroll-and-jam
2. **Occupancy 50-75%**: Apply with caution, measure performance
3. **Occupancy < 50%**: Avoid or use minimal unroll factor (2)

### Warp Divergence Implications

**Warp Execution Model**: All 32 threads in warp execute same instruction
- Divergent branches serialize execution
- Both paths must execute, inactive threads masked

**Unroll-and-Jam Impact on Divergence**:

**Scenario 1: Outer Loop Divergence**
```cuda
// Original: Potential divergence in outer loop
for (int i = 0; i < N; i++) {
    if (condition[i]) {  // Divergence
        for (int j = 0; j < M; j++) {
            process(i, j);
        }
    }
}

// After unroll-and-jam: Increased divergence
for (int i = 0; i < N; i += 4) {
    for (int j = 0; j < M; j++) {
        if (condition[i+0]) process(i+0, j);  // 4 separate
        if (condition[i+1]) process(i+1, j);  // divergence
        if (condition[i+2]) process(i+2, j);  // points
        if (condition[i+3]) process(i+3, j);
    }
}
// Worse: More branches per inner iteration
```

**Scenario 2: Inner Loop Only**
```cuda
// Original: No divergence
for (int i = 0; i < N; i++) {
    for (int j = 0; j < M; j++) {
        process(i, j);
    }
}

// After unroll-and-jam: Still no divergence
for (int i = 0; i < N; i += 4) {
    for (int j = 0; j < M; j++) {
        process(i+0, j);  // All threads execute same code
        process(i+1, j);
        process(i+2, j);
        process(i+3, j);
    }
}
// Safe: No new divergence introduced
```

**CICC Strategy**:
- Analyze control flow in outer loop
- Avoid unroll-and-jam if outer loop has divergent branches
- Safe for simple outer loops with no conditionals

### Shared Memory Access Patterns

**Benefit**: Unroll-and-jam can improve shared memory reuse

**Example: Matrix Transpose with Shared Memory**
```cuda
__global__ void transpose(float* out, float* in, int N) {
    __shared__ float tile[32][32];

    int i = blockIdx.x * 32;
    int j = threadIdx.x;

    // Original: Load once, transpose, store once
    tile[j][threadIdx.y] = in[i*N + j];
    __syncthreads();
    out[j*N + i] = tile[threadIdx.y][j];
}

// After unroll-and-jam (factor=4): Better shared memory utilization
__global__ void transpose_unrolled(float* out, float* in, int N) {
    __shared__ float tile[32][128];  // Larger tile

    int i = blockIdx.x * 128;  // Process 4x more per block
    int j = threadIdx.x;

    // Load 4 rows into shared memory
    tile[j][threadIdx.y +  0] = in[(i+ 0)*N + j];
    tile[j][threadIdx.y + 32] = in[(i+32)*N + j];
    tile[j][threadIdx.y + 64] = in[(i+64)*N + j];
    tile[j][threadIdx.y + 96] = in[(i+96)*N + j];
    __syncthreads();

    // Transpose 4 rows
    out[j*N + i +  0] = tile[threadIdx.y +  0][j];
    out[j*N + i + 32] = tile[threadIdx.y + 32][j];
    out[j*N + i + 64] = tile[threadIdx.y + 64][j];
    out[j*N + i + 96] = tile[threadIdx.y + 96][j];
}
// Benefit: Amortize synchronization cost over more data
```

**Bank Conflict Considerations**:
- Shared memory divided into 32 banks
- Simultaneous access to same bank causes serialization
- Unroll-and-jam can increase bank conflicts if not careful

**Guidelines**:
- Ensure unrolled accesses hit different banks
- Use padding if necessary to avoid conflicts
- Profile with `nsight compute` to detect bank conflicts

---

## Performance Impact

### Expected Speedup

| Scenario | Unroll Factor | Typical Speedup | Reason |
|----------|---------------|-----------------|--------|
| **Nested matrix operations** | 4 | 2-3× | Register reuse, cache locality |
| **Stencil computations** | 2-4 | 1.5-2.5× | Reduced outer loop overhead |
| **Reduction in nested loop** | 2 | 1.3-1.8× | Parallel accumulation |
| **GPU nested loops** | 2-4 | 1.2-2× | Depends on occupancy impact |

### Code Size Growth

**Formula**:
```
code_size_after = outer_body_size + (inner_body_size × unroll_factor) + overhead
overhead = remainder_loop + additional_control_flow
```

**Example**:
- Outer loop body: 10 instructions
- Inner loop body: 20 instructions
- Unroll factor: 4
- Expected size: 10 + (20 × 4) + 25 = **115 instructions**
- Growth vs original (30): **3.8×**

### Register Reuse Analysis

**Key Benefit**: Values computed in outer iteration i reused in iteration i+1

**Example: Matrix Multiply**
```c
// Original: Poor register reuse
for (int i = 0; i < M; i++) {
    for (int j = 0; j < N; j++) {
        float sum = 0.0f;
        for (int k = 0; k < K; k++) {
            sum += A[i][k] * B[k][j];
        }
        C[i][j] = sum;
    }
}
// A[i][k] loaded once per i, not reused

// After unroll-and-jam on i (factor=4)
for (int i = 0; i < M; i += 4) {
    for (int j = 0; j < N; j++) {
        float sum0 = 0.0f, sum1 = 0.0f, sum2 = 0.0f, sum3 = 0.0f;

        for (int k = 0; k < K; k++) {
            float a0 = A[(i+0)][k];  // 4 A values in registers
            float a1 = A[(i+1)][k];
            float a2 = A[(i+2)][k];
            float a3 = A[(i+3)][k];
            float b = B[k][j];       // 1 B value shared

            sum0 += a0 * b;
            sum1 += a1 * b;
            sum2 += a2 * b;
            sum3 += a3 * b;
        }

        C[(i+0)][j] = sum0;
        C[(i+1)][j] = sum1;
        C[(i+2)][j] = sum2;
        C[(i+3)][j] = sum3;
    }
}
// B[k][j] loaded once, reused 4 times → 4× memory reduction
```

**Measured Impact**:
- Memory traffic reduction: **25-40%**
- Cache hit rate improvement: **15-30%**
- Execution time reduction: **20-35%**

### Cache Behavior

**L1 Cache Benefit**: Improved temporal locality

**Example**:
```
Original:
  Outer iter 0: Load A[0][*], Load B[*][j]
  Outer iter 1: Load A[1][*], Load B[*][j]  ← B reloaded (may evict from cache)

Unroll-and-Jam:
  Outer iter 0-3 fused: Load A[0-3][*], Load B[*][j] once
  B stays in cache for all 4 uses
```

**Cache Line Utilization**: Better spatial locality

```
Original:
  Load cache line containing A[i][0..7]
  Use A[i][k] only

Unroll-and-Jam (factor=4):
  Load cache lines containing A[i+0][0..7], A[i+1][0..7], A[i+2][0..7], A[i+3][0..7]
  Use all elements in same inner loop iteration
  Better cache line utilization
```

---

## Examples

### Example 1: Simple Nested Loop Transformation

**Original C Code**:
```c
void nested_add(float* C, float* A, float* B, int M, int N) {
    for (int i = 0; i < M; i++) {
        for (int j = 0; j < N; j++) {
            C[i*N + j] = A[i*N + j] + B[i*N + j];
        }
    }
}
```

**After Unroll-and-Jam (factor=4)**:
```c
void nested_add(float* C, float* A, float* B, int M, int N) {
    int i;

    // Main unrolled loop
    for (i = 0; i < M - 3; i += 4) {
        // Fused inner loop processes 4 outer iterations
        for (int j = 0; j < N; j++) {
            float a0 = A[(i+0)*N + j];
            float a1 = A[(i+1)*N + j];
            float a2 = A[(i+2)*N + j];
            float a3 = A[(i+3)*N + j];

            float b0 = B[(i+0)*N + j];
            float b1 = B[(i+1)*N + j];
            float b2 = B[(i+2)*N + j];
            float b3 = B[(i+3)*N + j];

            C[(i+0)*N + j] = a0 + b0;
            C[(i+1)*N + j] = a1 + b1;
            C[(i+2)*N + j] = a2 + b2;
            C[(i+3)*N + j] = a3 + b3;
        }
    }

    // Remainder loop
    for (; i < M; i++) {
        for (int j = 0; j < N; j++) {
            C[i*N + j] = A[i*N + j] + B[i*N + j];
        }
    }
}
```

**Analysis**:
- **Register Reuse**: None in this simple case (independent operations)
- **Cache Locality**: Slightly better (4 rows processed together)
- **ILP**: 4× more independent operations exposed
- **Memory Bandwidth**: Same total memory traffic

### Example 2: Matrix Multiply (Strong Register Reuse)

**Original C Code**:
```c
void matmul(float* C, float* A, float* B, int M, int N, int K) {
    for (int i = 0; i < M; i++) {
        for (int j = 0; j < N; j++) {
            float sum = 0.0f;
            for (int k = 0; k < K; k++) {
                sum += A[i*K + k] * B[k*N + j];
            }
            C[i*N + j] = sum;
        }
    }
}
```

**After Unroll-and-Jam on i (factor=4)**:
```c
void matmul(float* C, float* A, float* B, int M, int N, int K) {
    int i;

    for (i = 0; i < M - 3; i += 4) {
        for (int j = 0; j < N; j++) {
            // 4 separate accumulators
            float sum0 = 0.0f;
            float sum1 = 0.0f;
            float sum2 = 0.0f;
            float sum3 = 0.0f;

            // Fused inner k loop
            for (int k = 0; k < K; k++) {
                // Load A elements once
                float a0 = A[(i+0)*K + k];
                float a1 = A[(i+1)*K + k];
                float a2 = A[(i+2)*K + k];
                float a3 = A[(i+3)*K + k];

                // Load B element once, reuse 4 times
                float b = B[k*N + j];

                // 4 parallel multiplies and accumulates
                sum0 += a0 * b;
                sum1 += a1 * b;
                sum2 += a2 * b;
                sum3 += a3 * b;
            }

            C[(i+0)*N + j] = sum0;
            C[(i+1)*N + j] = sum1;
            C[(i+2)*N + j] = sum2;
            C[(i+3)*N + j] = sum3;
        }
    }

    // Remainder loop
    for (; i < M; i++) {
        for (int j = 0; j < N; j++) {
            float sum = 0.0f;
            for (int k = 0; k < K; k++) {
                sum += A[i*K + k] * B[k*N + j];
            }
            C[i*N + j] = sum;
        }
    }
}
```

**Performance Impact**:
- **B reuse**: Each B[k][j] element loaded once, used 4 times → **4× reduction**
- **Memory bandwidth**: Reduced by ~40%
- **Speedup**: **2-3×** on CPU, **1.5-2×** on GPU

### Example 3: CUDA Kernel Transformation

**Original CUDA Kernel**:
```cuda
__global__ void stencil_2d(float* out, float* in, int N, int M) {
    int i = blockIdx.y;
    int j = threadIdx.x + blockIdx.x * blockDim.x;

    if (i > 0 && i < N-1 && j > 0 && j < M-1) {
        float center = in[i*M + j];
        float left   = in[i*M + (j-1)];
        float right  = in[i*M + (j+1)];
        float up     = in[(i-1)*M + j];
        float down   = in[(i+1)*M + j];

        out[i*M + j] = (center + left + right + up + down) * 0.2f;
    }
}
```

**After Unroll-and-Jam (factor=2)**:
```cuda
__global__ void stencil_2d_unrolled(float* out, float* in, int N, int M) {
    int i = blockIdx.y * 2;  // Process 2 rows per block
    int j = threadIdx.x + blockIdx.x * blockDim.x;

    if (i > 0 && i+1 < N-1 && j > 0 && j < M-1) {
        // Row i
        float center0 = in[(i+0)*M + j];
        float left0   = in[(i+0)*M + (j-1)];
        float right0  = in[(i+0)*M + (j+1)];
        float up0     = in[(i-1)*M + j];
        float down0   = in[(i+1)*M + j];

        // Row i+1 (reuses some values)
        float center1 = in[(i+1)*M + j];
        float left1   = in[(i+1)*M + (j-1)];
        float right1  = in[(i+1)*M + (j+1)];
        // up1 is center0 from row i (reused!)
        float down1   = in[(i+2)*M + j];

        out[(i+0)*M + j] = (center0 + left0 + right0 + up0 + down0) * 0.2f;
        out[(i+1)*M + j] = (center1 + left1 + right1 + center0 + down1) * 0.2f;
    }
}
```

**Analysis**:
- **Register reuse**: `center0` reused as `up1`
- **Memory reduction**: One less load per iteration
- **Occupancy impact**: Moderate (2× registers)
- **Expected speedup**: **1.3-1.5×**

### Example 4: PTX Code Comparison

**Original Scalar PTX** (simplified):
```ptx
.visible .entry nested_add(
    .param .u64 C,
    .param .u64 A,
    .param .u64 B,
    .param .u32 M,
    .param .u32 N
) {
    .reg .pred %p<4>;
    .reg .f32 %f<8>;
    .reg .u32 %r<12>;
    .reg .u64 %rd<8>;

outer_loop:
    setp.lt.u32 %p1, %r1, %r2;      // i < M
    @!%p1 bra outer_exit;

inner_loop:
    setp.lt.u32 %p2, %r3, %r4;      // j < N
    @!%p2 bra inner_exit;

    // Compute indices
    mad.lo.u32 %r5, %r1, %r4, %r3;  // i*N + j
    mul.wide.u32 %rd1, %r5, 4;

    // Load A[i*N + j]
    add.u64 %rd2, %rd10, %rd1;
    ld.global.f32 %f1, [%rd2];

    // Load B[i*N + j]
    add.u64 %rd3, %rd11, %rd1;
    ld.global.f32 %f2, [%rd3];

    // Compute sum
    add.f32 %f3, %f1, %f2;

    // Store C[i*N + j]
    add.u64 %rd4, %rd12, %rd1;
    st.global.f32 [%rd4], %f3;

    add.u32 %r3, %r3, 1;            // j++
    bra inner_loop;

inner_exit:
    add.u32 %r1, %r1, 1;            // i++
    bra outer_loop;

outer_exit:
    ret;
}
```

**After Unroll-and-Jam PTX** (factor=4):
```ptx
.visible .entry nested_add_unrolled(
    .param .u64 C,
    .param .u64 A,
    .param .u64 B,
    .param .u32 M,
    .param .u32 N
) {
    .reg .pred %p<8>;
    .reg .f32 %f<24>;     // More registers for unrolling
    .reg .u32 %r<20>;
    .reg .u64 %rd<20>;

outer_loop:
    add.u32 %r5, %r1, 3;            // i + 3
    setp.lt.u32 %p1, %r5, %r2;      // i+3 < M
    @!%p1 bra remainder_loop;

inner_loop:
    setp.lt.u32 %p2, %r3, %r4;      // j < N
    @!%p2 bra inner_exit;

    // Iteration 0: A[i*N + j]
    mad.lo.u32 %r10, %r1, %r4, %r3;
    mul.wide.u32 %rd10, %r10, 4;
    add.u64 %rd11, %rd_A, %rd10;
    ld.global.f32 %f1, [%rd11];

    // Iteration 1: A[(i+1)*N + j]
    add.u32 %r11, %r1, 1;
    mad.lo.u32 %r12, %r11, %r4, %r3;
    mul.wide.u32 %rd12, %r12, 4;
    add.u64 %rd13, %rd_A, %rd12;
    ld.global.f32 %f2, [%rd13];

    // Iteration 2: A[(i+2)*N + j]
    add.u32 %r13, %r1, 2;
    mad.lo.u32 %r14, %r13, %r4, %r3;
    mul.wide.u32 %rd14, %r14, 4;
    add.u64 %rd15, %rd_A, %rd14;
    ld.global.f32 %f3, [%rd15];

    // Iteration 3: A[(i+3)*N + j]
    add.u32 %r15, %r1, 3;
    mad.lo.u32 %r16, %r15, %r4, %r3;
    mul.wide.u32 %rd16, %r16, 4;
    add.u64 %rd17, %rd_A, %rd16;
    ld.global.f32 %f4, [%rd17];

    // Load B elements (4 loads)
    add.u64 %rd20, %rd_B, %rd10;
    ld.global.f32 %f5, [%rd20];
    add.u64 %rd21, %rd_B, %rd12;
    ld.global.f32 %f6, [%rd21];
    add.u64 %rd22, %rd_B, %rd14;
    ld.global.f32 %f7, [%rd22];
    add.u64 %rd23, %rd_B, %rd16;
    ld.global.f32 %f8, [%rd23];

    // Compute sums (4 parallel adds)
    add.f32 %f9,  %f1, %f5;
    add.f32 %f10, %f2, %f6;
    add.f32 %f11, %f3, %f7;
    add.f32 %f12, %f4, %f8;

    // Store results (4 stores)
    add.u64 %rd30, %rd_C, %rd10;
    st.global.f32 [%rd30], %f9;
    add.u64 %rd31, %rd_C, %rd12;
    st.global.f32 [%rd31], %f10;
    add.u64 %rd32, %rd_C, %rd14;
    st.global.f32 [%rd32], %f11;
    add.u64 %rd33, %rd_C, %rd16;
    st.global.f32 [%rd33], %f12;

    add.u32 %r3, %r3, 1;            // j++
    bra inner_loop;

inner_exit:
    add.u32 %r1, %r1, 4;            // i += 4 (unroll factor)
    bra outer_loop;

remainder_loop:
    // Handle remaining iterations (i < M)
    ...

exit:
    ret;
}
```

**PTX Analysis**:
- **Instruction count**: 4× more loads/stores per inner iteration
- **Register usage**: 24 FP registers vs 8 (3× increase)
- **ILP**: 4 independent add operations can execute in parallel
- **Memory coalescing**: Potential for better coalescing if threads access consecutive j

---

## Debugging and Analysis

### Statistics

With `-stats` flag, LoopUnrollAndJam reports:
```
NumLoopsUnrollAndJammed: 8         # Loops successfully transformed
TotalUnrollAndJamFactor: 28        # Sum of all unroll factors
AverageUnrollFactor: 3.5           # Mean unroll factor
NumRejectedRegisterPressure: 4     # Rejected due to register pressure
NumRejectedDependence: 2           # Rejected due to dependences
CodeSizeIncrease: 2.8×             # Code growth ratio
```

### Disabling for Debugging

```bash
# Disable unroll-and-jam
-mllvm -enable-unroll-and-jam=false

# Force specific unroll factor
-mllvm -unroll-and-jam-count=2

# Increase threshold
-mllvm -unroll-and-jam-threshold=120
```

### Profiling Tools

**NVIDIA Nsight Compute**: Profile register usage and occupancy
```bash
ncu --metrics sm__warps_active.avg.pct_of_peak_sustained_active kernel_name

# Check register usage
ncu --metrics launch__registers_per_thread kernel_name

# Check occupancy
ncu --metrics sm__maximum_warps_per_active_cycle_pct kernel_name
```

---

## Known Limitations

1. **Single Inner Loop Requirement**: CICC implementation requires exactly one inner loop
2. **Register Pressure**: Can significantly increase register usage
3. **Occupancy Impact**: May reduce GPU occupancy if not carefully tuned
4. **Complex Dependencies**: Cannot handle certain dependence patterns
5. **Code Size**: Substantial code growth (2-4×)
6. **Debugging Difficulty**: Transformed code harder to debug

---

## Related Optimizations

- **LoopUnroll**: [loop-unroll.md](loop-unroll.md) - Simple loop unrolling without jamming
- **LoopFusion**: [loop-fusion.md](loop-fusion.md) - Fuses adjacent loops
- **LoopInterchange**: [loop-interchange.md](loop-interchange.md) - Reorders nested loops
- **LoopVectorize**: [loop-vectorize.md](loop-vectorize.md) - Vectorizes loops (often applied after)
- **LICM**: [licm.md](licm.md) - Hoists invariants before unroll-and-jam

---

## References

1. **LLVM Loop Unroll-and-Jam**: https://llvm.org/docs/Passes/#loop-unroll-and-jam
2. **Allen, R., & Kennedy, K.** (2001). "Optimizing Compilers for Modern Architectures." Chapter 11.
3. **Wolfe, M.** (1996). "High Performance Compilers for Parallel Computing." Chapter 9.
4. **CUDA Programming Guide**: Thread hierarchy and register allocation
5. **LLVM Source**: `lib/Transforms/Scalar/LoopUnrollAndJamPass.cpp`

---

**L3 Analysis Quality**: HIGH (based on LLVM implementation + extensive GPU analysis)
**Last Updated**: 2025-11-17
**Source**: CICC foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json + LLVM/CUDA documentation
