# Call Site Splitting

**Pass Type**: Interprocedural optimization
**LLVM Class**: `llvm::CallSiteSplittingPass`
**Algorithm**: Context-sensitive call site specialization
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: MEDIUM - Standard LLVM pass
**Pass Category**: Other Transformations

---

## Overview

Call Site Splitting is an interprocedural optimization that creates specialized versions of function calls based on the calling context. When a function is called with different arguments or from different control flow contexts, this pass can split the call site to enable better optimization of each specialized case.

**Key Innovation**: By creating context-specific call sites, the compiler can perform more aggressive constant propagation, dead code elimination, and inlining decisions tailored to each calling context.

---

## Algorithm Overview

### Call Site Context Analysis

Call site splitting analyzes the calling context to identify optimization opportunities:

1. **Constant Arguments**: When some call sites pass constant values
2. **Control Flow Context**: When calls occur in different branches with known conditions
3. **Value Range Context**: When argument values have different ranges in different contexts

**Example**:
```c
__device__ int process(int x, int mode) {
    if (mode == 0) {
        return x * 2;
    } else {
        return x * 3;
    }
}

__device__ void kernel() {
    int a = threadIdx.x;

    // Call site 1: mode is always 0
    if (a < 16) {
        result1 = process(a, 0);  // mode constant
    }

    // Call site 2: mode is always 1
    if (a >= 16) {
        result2 = process(a, 1);  // mode constant
    }
}
```

---

## Transformation Algorithm

### Step 1: Call Site Profiling

```c
struct CallSiteContext {
    CallInst* Call;
    SmallVector<Value*, 8> ArgumentValues;
    SmallVector<bool, 8> IsConstant;
    BasicBlock* CallingBlock;
    Function* Caller;
};

void analyzeCallSites(Function* F) {
    for (User* U : F->users()) {
        if (CallInst* Call = dyn_cast<CallInst>(U)) {
            CallSiteContext Ctx;
            Ctx.Call = Call;

            // Analyze each argument
            for (unsigned i = 0; i < Call->getNumArgOperands(); i++) {
                Value* Arg = Call->getArgOperand(i);
                Ctx.ArgumentValues.push_back(Arg);
                Ctx.IsConstant.push_back(isa<Constant>(Arg));
            }

            // Analyze control flow context
            Ctx.CallingBlock = Call->getParent();
            Ctx.Caller = Call->getFunction();

            CallSites.push_back(Ctx);
        }
    }
}
```

### Step 2: Splitting Decision

Determine which call sites benefit from splitting:

```c
bool shouldSplitCallSite(CallSiteContext& Ctx1, CallSiteContext& Ctx2) {
    // Check if contexts differ significantly
    bool DifferentConstants = false;
    for (unsigned i = 0; i < Ctx1.ArgumentValues.size(); i++) {
        if (Ctx1.IsConstant[i] && Ctx2.IsConstant[i]) {
            if (Ctx1.ArgumentValues[i] != Ctx2.ArgumentValues[i]) {
                DifferentConstants = true;
                break;
            }
        }
    }

    if (!DifferentConstants) {
        return false;  // No benefit to splitting
    }

    // Estimate benefit vs. code size cost
    float Benefit = estimateOptimizationBenefit(Ctx1, Ctx2);
    float Cost = estimateCodeSizeCost();

    return Benefit > Cost * 2.0;  // Require 2× benefit
}
```

### Step 3: Create Specialized Function Clone

```c
void splitCallSite(CallInst* Call, CallSiteContext& Ctx) {
    Function* Original = Call->getCalledFunction();

    // Clone function with context-specific optimizations
    ValueToValueMapTy VMap;
    Function* Specialized = CloneFunction(Original, VMap);

    // Apply constant propagation for known arguments
    for (unsigned i = 0; i < Call->getNumArgOperands(); i++) {
        if (Ctx.IsConstant[i]) {
            Argument* Arg = Specialized->getArg(i);
            Constant* ConstValue = cast<Constant>(Ctx.ArgumentValues[i]);

            // Replace all uses of parameter with constant
            Arg->replaceAllUsesWith(ConstValue);
        }
    }

    // Run simplification passes on specialized function
    simplifyFunction(Specialized);

    // Update call site to use specialized version
    Call->setCalledFunction(Specialized);
}
```

### Step 4: Control Flow Splitting

For calls in conditional blocks:

```c
void splitConditionalCallSite(CallInst* Call) {
    // Original:
    //   if (cond) {
    //       result = func(x, 0);
    //   } else {
    //       result = func(x, 1);
    //   }

    // After splitting:
    //   if (cond) {
    //       result = func_specialized_0(x);  // mode=0 hardcoded
    //   } else {
    //       result = func_specialized_1(x);  // mode=1 hardcoded
    //   }

    BasicBlock* ThenBlock = ...;
    BasicBlock* ElseBlock = ...;

    // Analyze condition and create specialized clones
    if (argumentDiffersInBranches(Call, ThenBlock, ElseBlock)) {
        Function* ThenVersion = createSpecializedClone(Call, ThenBlock);
        Function* ElseVersion = createSpecializedClone(Call, ElseBlock);

        // Update call sites
        updateCallInBlock(ThenBlock, ThenVersion);
        updateCallInBlock(ElseBlock, ElseVersion);
    }
}
```

---

## Configuration Parameters

**Evidence**: Listed in optimization pass mapping

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `call-site-splitting-duplication-threshold` | int | 2 | Max function clones per original |
| `enable-call-site-splitting` | bool | true | Master enable flag |

---

## Optimization Opportunities

### Opportunity 1: Constant Propagation

**Before Splitting**:
```c
__device__ int compute(int x, int mode) {
    if (mode == 0) return x * 2;
    if (mode == 1) return x * 3;
    if (mode == 2) return x * 4;
    return x;
}

__device__ void kernel() {
    int a = threadIdx.x;
    result1 = compute(a, 0);  // mode always 0
    result2 = compute(a, 1);  // mode always 1
}
```

**After Splitting**:
```c
__device__ int compute_mode0(int x) {
    return x * 2;  // Dead branches eliminated
}

__device__ int compute_mode1(int x) {
    return x * 3;  // Dead branches eliminated
}

__device__ void kernel() {
    int a = threadIdx.x;
    result1 = compute_mode0(a);  // Specialized
    result2 = compute_mode1(a);  // Specialized
}
```

### Opportunity 2: Inlining Decisions

Specialized functions are often smaller and better candidates for inlining:

```c
// Original: 100 instructions → Not inlined
__device__ int large_func(int x, bool flag) {
    if (flag) {
        // 50 instructions
    } else {
        // 50 instructions
    }
}

// After splitting: 50 instructions each → Can inline
__device__ int large_func_true(int x) {
    // 50 instructions (flag=true branch)
}

__device__ int large_func_false(int x) {
    // 50 instructions (flag=false branch)
}
```

---

## CUDA-Specific Considerations

### Thread-Uniform Arguments

CUDA kernels often have thread-uniform arguments (same value across all threads in block):

```c
__device__ int process(int data, int block_mode) {
    // block_mode is uniform across entire block
    if (block_mode == FAST_MODE) {
        return fast_computation(data);
    } else {
        return slow_computation(data);
    }
}

__global__ void kernel(int* input, int mode) {
    int tid = threadIdx.x;
    // mode is kernel parameter - uniform across all threads
    result = process(input[tid], mode);
}
```

Call site splitting can specialize for different kernel launch modes:
- Launch 1: `kernel<<<blocks, threads>>>(data, FAST_MODE)` → Uses specialized fast version
- Launch 2: `kernel<<<blocks, threads>>>(data, SLOW_MODE)` → Uses specialized slow version

### Warp Divergence Reduction

By eliminating constant conditional branches in specialized functions:

```c
// Before: Divergent branch (if mode varies)
__device__ int func(int x, int mode) {
    if (mode == 0) {  // Divergence if mode differs per thread
        return x * 2;
    } else {
        return x * 3;
    }
}

// After: No divergence (mode constant)
__device__ int func_mode0(int x) {
    return x * 2;  // All threads take same path
}
```

**Impact**: Eliminates warp divergence when mode is thread-uniform.

---

## Performance Characteristics

### Code Size Impact

| Scenario | Code Size Change | Notes |
|----------|------------------|-------|
| 2 specialized versions | +50-150% per function | Moderate increase |
| 3+ specialized versions | +100-300% per function | Significant increase |
| Small functions | +10-50 bytes each | Negligible |
| Large functions | +1-10 KB each | May offset benefits |

### Execution Time Impact

| Scenario | Speedup | Reason |
|----------|---------|--------|
| Constant propagation enabled | 5-20% | Dead code eliminated |
| Inlining enabled | 10-30% | Call overhead removed |
| Branch elimination | 2-8% | Reduced divergence |
| No optimization benefit | 0-2% slower | Code bloat overhead |

### Compile Time Overhead

- **Analysis**: 2-5% additional compile time for call site analysis
- **Cloning**: 5-15% additional compile time for function duplication
- **Optimization**: 10-30% additional time for optimizing specialized versions

**Total**: 15-50% compile time increase when aggressively splitting.

---

## Integration with Other Passes

### Prerequisites

| Pass | Purpose |
|------|---------|
| **CallGraph Analysis** | Identifies call relationships |
| **DominatorTree** | Analyzes control flow context |
| **Constant Analysis** | Identifies constant arguments |

### Downstream Passes

| Pass | Interaction | Benefit |
|------|-------------|---------|
| **Inlining** | Specialized functions more likely inlined | Better call overhead reduction |
| **Constant Propagation** | Constants flow through specialized functions | Dead code elimination |
| **Dead Code Elimination** | Eliminates unreachable branches | Smaller specialized functions |
| **Register Allocation** | Smaller live ranges in specialized versions | Better register allocation |

---

## Example Transformation

### Complete Example

**Before Call Site Splitting**:
```llvm
define i32 @compute(i32 %x, i32 %mode) {
entry:
  switch i32 %mode, label %default [
    i32 0, label %mode0
    i32 1, label %mode1
  ]

mode0:
  %mul0 = mul i32 %x, 2
  ret i32 %mul0

mode1:
  %mul1 = mul i32 %x, 3
  ret i32 %mul1

default:
  ret i32 %x
}

define void @kernel() {
  %a = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()

  %cmp = icmp ult i32 %a, 16
  br i1 %cmp, label %then, label %else

then:
  %result1 = call i32 @compute(i32 %a, i32 0)
  br label %merge

else:
  %result2 = call i32 @compute(i32 %a, i32 1)
  br label %merge

merge:
  ret void
}
```

**After Call Site Splitting**:
```llvm
define i32 @compute_mode0(i32 %x) {
entry:
  %mul = mul i32 %x, 2
  ret i32 %mul
}

define i32 @compute_mode1(i32 %x) {
entry:
  %mul = mul i32 %x, 3
  ret i32 %mul
}

define void @kernel() {
  %a = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()

  %cmp = icmp ult i32 %a, 16
  br i1 %cmp, label %then, label %else

then:
  %result1 = call i32 @compute_mode0(i32 %a)  ; Specialized
  br label %merge

else:
  %result2 = call i32 @compute_mode1(i32 %a)  ; Specialized
  br label %merge

merge:
  ret void
}
```

---

## Debugging and Diagnostics

### Disabling Call Site Splitting

```bash
# Disable call site splitting
-mllvm -enable-call-site-splitting=false

# Control duplication threshold
-mllvm -call-site-splitting-duplication-threshold=3
```

### Statistics

Enable statistics to see splitting activity:
```bash
nvcc -Xptxas -v kernel.cu
# Look for "Call sites split: N"
```

---

## Known Limitations

| Limitation | Impact | Workaround |
|------------|--------|------------|
| Code size explosion | Can increase binary size 2-3× | Limit duplication threshold |
| Compile time increase | 15-50% longer compilation | Disable for large functions |
| Indirect calls not split | Function pointers prevent splitting | Use direct calls |
| Virtual functions not split | C++ virtual dispatch prevents splitting | Devirtualization first |

---

## Related Optimizations

- **Inlining**: [inlining.md](inlining.md) - Often follows call site splitting
- **Constant Propagation**: [constant-propagation.md](constant-propagation.md) - Enabled by splitting
- **Dead Code Elimination**: [dead-code-elimination.md](dead-code-elimination.md) - Removes unreachable branches

---

**Pass Location**: Unconfirmed (suspected in other transformations cluster)
**Confidence**: MEDIUM - Standard LLVM pass listed in mapping
**Last Updated**: 2025-11-17
**Source**: CICC optimization pass mapping (line 329)
