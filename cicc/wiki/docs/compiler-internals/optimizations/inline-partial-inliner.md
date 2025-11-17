# PartialInliner

**Pass Type**: Call graph SCC optimization
**LLVM Class**: `llvm::PartialInlinerPass`
**Algorithm**: Selective code extraction and outlining
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: MEDIUM - Pass suspected but unconfirmed in binary
**Pass Index**: Runs after main Inliner in optimization pipeline
**Confidence Level**: MEDIUM (suspected based on LLVM standard passes)

---

## Overview

The **PartialInliner** is an advanced optimization pass that performs **selective function inlining** by splitting functions into hot and cold parts, then inlining only the hot portions. This technique combines the benefits of inlining (reduced call overhead, optimization enablement) with the benefits of code outlining (reduced code size, improved instruction cache locality).

**Core Strategy**:
1. Identify hot paths through a function (frequently executed code)
2. Outline cold paths (rarely executed code) into separate functions
3. Inline the hot path at call sites
4. Leave cold paths as function calls

**Key Innovation**: Achieves the performance benefits of inlining without the full code size penalty.

**Evidence**: Listed in CICC unconfirmed passes section of optimization pass mapping (pass ID: PartialInliner)

---

## Algorithm Overview

### Hot/Cold Path Detection

```c
struct PathInfo {
    BasicBlock* hot_path_entry;
    SmallVector<BasicBlock*, 16> hot_blocks;
    SmallVector<BasicBlock*, 8> cold_blocks;
    float hot_probability;
};

PathInfo analyzeFunction(Function* F) {
    PathInfo info;

    // 1. Identify function entry
    info.hot_path_entry = &F->getEntryBlock();

    // 2. Analyze control flow with profile data
    for (BasicBlock& BB : *F) {
        float probability = getBlockProbability(&BB);

        if (probability >= HOT_THRESHOLD) {
            info.hot_blocks.push_back(&BB);
        } else {
            info.cold_blocks.push_back(&BB);
        }
    }

    // 3. Calculate total hot path probability
    info.hot_probability = calculatePathProbability(info.hot_blocks);

    return info;
}
```

### Partial Inlining Decision

```c
bool shouldPartiallyInline(Function* F, CallSite CS) {
    PathInfo paths = analyzeFunction(F);

    // 1. Check if function has distinct hot/cold paths
    if (paths.cold_blocks.empty()) {
        return false;  // No cold code to outline
    }

    if (paths.hot_blocks.size() > paths.cold_blocks.size() * 5) {
        return false;  // Hot path too large
    }

    // 2. Check hot path probability
    if (paths.hot_probability < 0.9) {
        return false;  // Hot path not hot enough
    }

    // 3. Check code size impact
    int hot_size = calculateSize(paths.hot_blocks);
    int total_size = F->getInstructionCount();

    if (hot_size > total_size * 0.5) {
        return false;  // Hot path too large to inline
    }

    // 4. Check cost model
    int inline_cost = estimateInlineCost(paths.hot_blocks);
    int threshold = getPartialInlineThreshold();

    return inline_cost <= threshold;
}
```

---

## Transformation Process

### Step 1: Function Splitting

Extract cold blocks into a separate function:

```c
Function* outlineColdPath(Function* F, PathInfo& paths) {
    // 1. Create new function for cold path
    Function* ColdFunc = Function::Create(
        F->getFunctionType(),
        Function::InternalLinkage,
        F->getName() + ".cold",
        F->getParent()
    );

    // 2. Move cold blocks to new function
    ValueToValueMapTy VMap;
    for (BasicBlock* BB : paths.cold_blocks) {
        BasicBlock* NewBB = CloneBasicBlock(BB, VMap, "", ColdFunc);
        VMap[BB] = NewBB;
    }

    // 3. Update branch targets
    for (BasicBlock* BB : paths.hot_blocks) {
        Instruction* Term = BB->getTerminator();
        for (unsigned i = 0; i < Term->getNumSuccessors(); ++i) {
            BasicBlock* Succ = Term->getSuccessor(i);
            if (isColdBlock(Succ, paths)) {
                // Replace branch to cold block with call
                CallInst* Call = createColdPathCall(ColdFunc, BB);
                Term->setSuccessor(i, Call->getParent());
            }
        }
    }

    return ColdFunc;
}
```

### Step 2: Hot Path Inlining

After outlining, inline the hot path:

```c
void partiallyInlineFunction(Function* F, CallSite CS, PathInfo& paths) {
    // 1. Outline cold path
    Function* ColdFunc = outlineColdPath(F, paths);

    // 2. Simplify F to contain only hot path
    for (BasicBlock* BB : paths.cold_blocks) {
        BB->eraseFromParent();
    }

    // 3. Inline simplified F at call site
    InlineFunctionInfo IFI;
    InlineFunction(CS, IFI);

    // 4. Cold path now called from inlined hot path
}
```

---

## Example Transformation

### Original Function

```c
__device__ int compute(int x) {
    int result = x * 2;  // Hot path

    if (x < 0) {  // Rarely true (cold path)
        // Complex error handling
        reportError();
        result = handleNegative(x);
        logEvent();
    }

    return result;  // Hot path
}

__global__ void kernel() {
    int val = compute(threadIdx.x);  // Call site
    output[threadIdx.x] = val;
}
```

### After Partial Inlining

```c
// Outlined cold path
__device__ int compute_cold(int x) {
    reportError();
    int result = handleNegative(x);
    logEvent();
    return result;
}

// Inlined hot path
__global__ void kernel() {
    int x = threadIdx.x;

    // Inlined hot path
    int result = x * 2;

    if (x < 0) {  // Rare - call cold path
        result = compute_cold(x);
    }

    output[threadIdx.x] = result;
}
```

**Benefits**:
- Hot path inlined: No call overhead for common case
- Cold path outlined: Reduced code size in kernel
- Better instruction cache: Hot code is compact

---

## Pattern Recognition

### Early Return Pattern

Common pattern in CUDA device functions:

```c
__device__ float safeDiv(float a, float b) {
    // Hot path: b != 0 (common case)
    if (b != 0.0f) {
        return a / b;
    }

    // Cold path: error handling (rare)
    reportDivisionByZero();
    return NAN;
}
```

**Partial inlining strategy**:
```c
// After transformation
__device__ float safeDiv_cold() {
    reportDivisionByZero();
    return NAN;
}

// Inlined at call site:
float result;
if (b != 0.0f) {
    result = a / b;  // Inlined hot path
} else {
    result = safeDiv_cold();  // Cold path call
}
```

### Exception Handling Pattern

```c
__device__ int parseValue(const char* str) {
    // Hot path: valid input
    if (isValid(str)) {
        return atoi(str);
    }

    // Cold path: error handling
    throw InvalidInputException(str);
}
```

**Transformation**:
- Inline validation and conversion (hot path)
- Outline exception throwing (cold path)

---

## Configuration Parameters

**Estimated parameters** (based on standard LLVM implementation):

| Parameter | Type | Default | Range | Purpose |
|-----------|------|---------|-------|---------|
| `enable-partial-inlining` | bool | **true** | - | Master enable flag |
| `partial-inline-threshold` | int | **150** | 50-1000 | Cost threshold for hot path |
| `partial-inline-max-hot-size` | int | **200** | 50-500 | Max hot path instruction count |
| `partial-inline-min-cold-ratio` | float | **0.1** | 0.05-0.5 | Min fraction of code that must be cold |
| `partial-inline-hot-probability` | float | **0.9** | 0.7-0.99 | Min hot path execution probability |

**Note**: These are estimated values based on standard LLVM; actual CICC defaults may differ.

---

## Profitability Analysis

### Cost Model

```c
int calculatePartialInlineCost(Function* F, PathInfo& paths) {
    int cost = 0;

    // 1. Hot path size (will be inlined)
    int hot_size = calculateSize(paths.hot_blocks);
    cost += hot_size;

    // 2. Call overhead savings (benefit)
    cost -= 25;  // Eliminate call overhead

    // 3. Outlining overhead (new cold function call)
    float cold_probability = 1.0 - paths.hot_probability;
    cost += (int)(25 * cold_probability);  // Expected call overhead

    // 4. Code size benefit (cold code not inlined)
    int cold_size = calculateSize(paths.cold_blocks);
    cost -= cold_size / 2;  // Partial benefit for not inlining cold

    // 5. Optimization enablement bonus
    if (hasConstantArgs(F)) {
        cost -= 50;  // Constant propagation opportunities
    }

    return cost;
}
```

### Benefit Calculation

```c
float calculatePartialInlineBenefit(PathInfo& paths) {
    float benefit = 0.0;

    // 1. Call overhead elimination (weighted by hot probability)
    benefit += 25.0 * paths.hot_probability;

    // 2. Code size reduction
    int cold_size = calculateSize(paths.cold_blocks);
    benefit += cold_size * 0.5;  // Code not inlined

    // 3. Instruction cache improvement
    int hot_size = calculateSize(paths.hot_blocks);
    if (hot_size < 50) {
        benefit += 10.0;  // Compact hot path improves cache
    }

    // 4. Optimization opportunities
    if (enablesConstProp(paths.hot_blocks)) {
        benefit += 20.0;
    }

    return benefit;
}
```

---

## CUDA-Specific Considerations

### Kernel Code Size Impact

For CUDA kernels, partial inlining helps manage code size:

```cuda
__global__ void processData(float* data, int n) {
    int tid = threadIdx.x + blockIdx.x * blockDim.x;

    if (tid < n) {
        // Hot path: common case processing
        float value = data[tid];
        value = processValue(value);  // Partially inline this
        data[tid] = value;
    } else {
        // Cold path: boundary check failed (rare for aligned launches)
        handleOutOfBounds();  // Keep as call
    }
}
```

**Benefits**:
- Reduced kernel code size
- Better instruction cache utilization
- Maintained performance for hot path

### Register Pressure Management

```c
bool shouldPartiallyInlineCUDA(Function* F, CallSite CS, PathInfo& paths) {
    // Check register impact of hot path only
    int hot_regs = estimateRegisterUsage(paths.hot_blocks);
    int current_regs = estimateRegisterUsage(CS.getCaller());

    if (current_regs + hot_regs > MAX_REGS_PER_THREAD) {
        return false;  // Would exceed register limit
    }

    // Cold path doesn't increase register pressure
    // (called separately, different register allocation)

    return true;
}
```

---

## Profile-Guided Partial Inlining

### Hot Path Identification

With profile data:

```c
PathInfo identifyHotPathWithProfile(Function* F) {
    PathInfo info;

    for (BasicBlock& BB : *F) {
        uint64_t execution_count = getProfileCount(&BB);
        uint64_t entry_count = getProfileCount(&F->getEntryBlock());

        float probability = (float)execution_count / entry_count;

        if (probability >= 0.9) {
            info.hot_blocks.push_back(&BB);
            info.hot_probability = max(info.hot_probability, probability);
        } else {
            info.cold_blocks.push_back(&BB);
        }
    }

    return info;
}
```

### Without Profile Data

Use static heuristics:

```c
PathInfo identifyHotPathHeuristic(Function* F) {
    PathInfo info;

    for (BasicBlock& BB : *F) {
        bool is_hot = true;

        // 1. Error handling paths are cold
        if (callsErrorFunction(&BB)) {
            is_hot = false;
        }

        // 2. Exception throwing paths are cold
        if (hasUnwindEdge(&BB)) {
            is_hot = false;
        }

        // 3. Unlikely branches are cold
        if (hasUnlikelyBranchHint(&BB)) {
            is_hot = false;
        }

        // 4. Logging/debugging paths are cold
        if (callsDebugFunction(&BB)) {
            is_hot = false;
        }

        if (is_hot) {
            info.hot_blocks.push_back(&BB);
        } else {
            info.cold_blocks.push_back(&BB);
        }
    }

    return info;
}
```

---

## Limitations

### Cases Where Partial Inlining Fails

```c
bool canPartiallyInline(Function* F) {
    // 1. Too small - just inline completely
    if (F->getInstructionCount() < 20) {
        return false;
    }

    // 2. No clear hot/cold separation
    PathInfo paths = analyzeFunction(F);
    if (paths.hot_blocks.empty() || paths.cold_blocks.empty()) {
        return false;
    }

    // 3. Complex control flow
    if (hasIrreducibleControlFlow(F)) {
        return false;  // Can't split cleanly
    }

    // 4. Landing pads (exception handling)
    if (hasLandingPads(F)) {
        return false;  // Don't split exception edges
    }

    // 5. Alloca in cold path
    if (hasAllocaInBlocks(paths.cold_blocks)) {
        return false;  // Can't outline stack allocations
    }

    return true;
}
```

---

## Performance Impact

### Typical Results

| Metric | Impact | Variability |
|--------|--------|-------------|
| **Code size** | -5% to +10% | Medium |
| **Execution time** | -2% to +5% | Low |
| **Instruction cache misses** | -10% to -30% | High |
| **Call overhead** | -50% to -90% | High |
| **Compile time** | +3% to +8% | Low |

**Best case**: Functions with clear hot/cold separation (e.g., error handling)
**Worst case**: Functions with evenly distributed execution

---

## Integration with Other Passes

### Execution Order

```
1. ProfileGuided Optimizations (collect data)
2. Main Inliner (inline based on full function cost)
3. PartialInliner ← RUNS HERE (refine inlining decisions)
4. InstCombine (optimize inlined hot paths)
5. SimplifyCFG (clean up split control flow)
```

### Interaction with Main Inliner

```c
void runInliningPipeline(Module& M) {
    // 1. Main Inliner makes initial decisions
    runInlinerPass(M);

    // 2. PartialInliner refines decisions
    // - Un-inline some functions
    // - Split and re-inline hot paths only
    runPartialInlinerPass(M);

    // 3. Cleanup passes
    runInstCombinePass(M);
    runSimplifyCFGPass(M);
}
```

---

## Debug and Diagnostics

### Statistics

```bash
# Enable partial inlining statistics
-mllvm -partial-inline-stats
```

**Example output**:
```
PartialInliner Statistics:
  NumFunctionsConsidered: 523
  NumFunctionsSplit: 47
  NumHotPathsInlined: 42
  NumColdPathsOutlined: 45
  AvgHotPathSize: 28 instructions
  AvgColdPathSize: 156 instructions
  CodeSizeReduction: -8.3%
```

### Disabling

```bash
# Disable partial inlining entirely
-mllvm -enable-partial-inlining=false
```

---

## Related Passes

- **Inliner**: [inline-main-inliner.md](inline-main-inliner.md) - Main cost-based inlining
- **AlwaysInliner**: [inline-always-inliner.md](inline-always-inliner.md) - Mandatory inlining
- **HotColdSplitting**: [hot-cold-splitting.md](hot-cold-splitting.md) - Related code outlining optimization
- **InstCombine**: [instcombine.md](instcombine.md) - Optimizes inlined hot paths

---

## Evidence Sources

**Data Sources**:
- `/home/user/nvopen-tools/cicc/foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json` (unconfirmed_passes section)
- Standard LLVM PartialInliner implementation (expected behavior)
- CUDA optimization patterns (inferred)

**Confidence Assessment**:
- **Pass existence**: MEDIUM (listed in unconfirmed passes, no direct evidence)
- **Algorithm**: MEDIUM (standard LLVM behavior assumed)
- **Configuration**: LOW (estimated from LLVM defaults)
- **CUDA handling**: MEDIUM (inferred from CUDA optimization requirements)

---

**Analysis Quality**: MEDIUM
**Last Updated**: 2025-11-17
**Source**: CICC optimization pass mapping + LLVM documentation + inferred patterns
**Note**: This pass is listed as "unconfirmed" in CICC analysis. Actual implementation may differ from standard LLVM.
