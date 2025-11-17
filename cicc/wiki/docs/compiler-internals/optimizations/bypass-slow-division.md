# BypassSlowDivision

**Pass Type**: Code generation preparation pass (Target-specific optimization)
**LLVM Class**: `llvm::BypassSlowDivisionPass`
**Algorithm**: Fast-path bypass for integer division with small divisors
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: MEDIUM - Pass identified, algorithm inferred from LLVM implementation
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`

---

## Overview

BypassSlowDivision is a specialized optimization pass that creates a fast path for integer division and remainder operations when the divisor is small. Division is one of the slowest arithmetic operations on most processors, including GPUs, taking 20-30 cycles or more. This pass generates code that checks if the divisor is below a threshold and uses a faster alternative (such as reciprocal multiplication or lookup tables) when possible.

**Key Insight**: Division by small integers can be replaced with cheaper operations (multiplication by reciprocal, shift operations, or table lookups), providing significant speedup when the divisor is frequently small.

**Core Algorithm**: Branch-based optimization that tests divisor magnitude and dispatches to fast or slow path.

### Why Division is Slow

**CPU/GPU Division Performance**:

| Operation | Latency (cycles) | Throughput | Relative Cost |
|-----------|------------------|------------|---------------|
| **Integer add** | 1 | 1/cycle | 1x |
| **Integer multiply** | 3-5 | 1/cycle | 3-5x |
| **Integer division** | 20-30 | 1/10 cycles | 20-30x |
| **Floating-point division** | 15-20 | 1/5 cycles | 15-20x |

**GPU-Specific Issues**:
- Division is not pipelined like multiply
- Limited hardware divider units per SM
- Division blocks other operations
- Warp divergence on division-heavy code

### When BypassSlowDivision Applies

The optimization applies when:

1. **Integer division or remainder**: `x / y` or `x % y`
2. **Non-constant divisor**: Divisor is not known at compile time
3. **Likely small divisors**: Profiling or heuristics suggest small values
4. **Cost-effective branch**: Branch prediction overhead < division overhead

**Typical Use Cases**:
- Hash table implementations (bucket = hash % table_size)
- Array indexing with small bounds
- Time calculations (seconds % 60, minutes % 60)
- Color channel extraction (pixel % 256)

### Performance Impact

**Typical Speedup**:
- Small divisor (< threshold): 5-10x faster
- Large divisor: ~5% slower (branch overhead)
- Mixed workload: 2-5x average speedup (when 50%+ are small divisors)

**Trade-off**: Adds branch overhead, so only beneficial when:
- Branch predictor performs well (predictable divisor size)
- Small divisors are common (> 30-40% of cases)

---

## Algorithm Details

### High-Level Algorithm

BypassSlowDivision transforms division operations:

```
Original:
    result = dividend / divisor

Transformed:
    if (divisor <= THRESHOLD) {
        result = fast_divide(dividend, divisor)  // Fast path
    } else {
        result = dividend / divisor              // Slow path (unchanged)
    }
```

**Key Components**:
1. **Threshold determination**: Choose cutoff for "small" divisor
2. **Fast path implementation**: Multiply by reciprocal or use lookup table
3. **Branch generation**: Create conditional dispatch
4. **Result merging**: Combine results from both paths

### Phase 1: Identify Optimization Candidates

```c
bool shouldOptimizeDivision(BinaryOperator* Div) {
    // Only optimize integer division/remainder
    if (!Div->getType()->isIntegerTy()) return false;

    // Check opcode
    if (Div->getOpcode() != Instruction::SDiv &&
        Div->getOpcode() != Instruction::UDiv &&
        Div->getOpcode() != Instruction::SRem &&
        Div->getOpcode() != Instruction::URem) {
        return false;
    }

    // Don't optimize if divisor is constant (handled by constant folding)
    Value* Divisor = Div->getOperand(1);
    if (isa<Constant>(Divisor)) return false;

    // Check if operation is inside a loop (higher benefit)
    bool InLoop = LI->getLoopFor(Div->getParent()) != nullptr;

    // Check target preferences
    const TargetTransformInfo& TTI = getAnalysis<TargetTransformInfo>();
    if (!TTI.enableBypassSlowDivision()) return false;

    return true;
}
```

### Phase 2: Determine Threshold

The threshold for "small divisor" is architecture-dependent:

```c
unsigned getBypassSlowDivisionThreshold(const TargetTransformInfo& TTI) {
    // Target-specific threshold
    unsigned Threshold = TTI.getBypassSlowDivisionThreshold();

    // Typical values:
    // - x86: 32 (division < 32 is much faster with reciprocal)
    // - ARM: 16
    // - GPU: 8-16 (division is very slow, but branches are expensive)

    return Threshold;
}
```

**GPU Threshold Selection**:
- **Lower threshold (8-16)**: Division is very expensive (20-30 cycles)
- **But**: Branch divergence is also expensive
- **Trade-off**: Conservative threshold to avoid branch overhead

### Phase 3: Generate Fast Path

The fast path uses reciprocal multiplication:

**Mathematical Basis**:
```
Division:  x / y = x * (1/y)

For integer division:
    x / y ≈ (x * RECIPROCAL[y]) >> SHIFT

Where:
    RECIPROCAL[y] = (2^SHIFT + y - 1) / y
    SHIFT = bit_width (typically 32 or 64)
```

**Implementation**:
```c
Value* generateFastPath(IRBuilder<>& Builder,
                        Value* Dividend,
                        Value* Divisor,
                        unsigned Threshold,
                        bool IsSigned) {
    Type* Ty = Dividend->getType();
    unsigned BitWidth = Ty->getIntegerBitWidth();

    // Create reciprocal table (precomputed or computed on-the-fly)
    // For small divisors: reciprocal[i] = (2^32) / i

    BasicBlock* FastBB = createBasicBlock("fast_div");
    BasicBlock* SlowBB = createBasicBlock("slow_div");
    BasicBlock* MergeBB = createBasicBlock("div_merge");

    // Check if divisor is small
    Value* IsSmall = Builder.CreateICmpULE(
        Divisor,
        ConstantInt::get(Ty, Threshold));

    Builder.CreateCondBr(IsSmall, FastBB, SlowBB);

    // Fast path: multiply by reciprocal
    Builder.SetInsertPoint(FastBB);
    Value* FastResult = nullptr;

    if (Threshold <= 16) {
        // Small threshold: use switch/lookup table
        FastResult = generateLookupTable(Builder, Dividend, Divisor, Threshold);
    } else {
        // Larger threshold: compute reciprocal
        FastResult = generateReciprocalMultiply(Builder, Dividend, Divisor);
    }

    Builder.CreateBr(MergeBB);

    // Slow path: original division
    Builder.SetInsertPoint(SlowBB);
    Value* SlowResult = IsSigned ?
        Builder.CreateSDiv(Dividend, Divisor) :
        Builder.CreateUDiv(Dividend, Divisor);
    Builder.CreateBr(MergeBB);

    // Merge results
    Builder.SetInsertPoint(MergeBB);
    PHINode* Result = Builder.CreatePHI(Ty, 2);
    Result->addIncoming(FastResult, FastBB);
    Result->addIncoming(SlowResult, SlowBB);

    return Result;
}
```

### Fast Path Implementation Strategies

#### Strategy 1: Lookup Table (Very Small Divisors)

For divisors 2-16, use precomputed reciprocals:

```c
Value* generateLookupTable(IRBuilder<>& Builder,
                           Value* Dividend,
                           Value* Divisor,
                           unsigned MaxDivisor) {
    // Precomputed reciprocals: reciprocal[i] = 2^32 / i
    static const uint32_t reciprocals[] = {
        0,              // divisor 0 (unused)
        0,              // divisor 1 (unused, handled separately)
        0x80000000,     // divisor 2: 2^31
        0x55555556,     // divisor 3: (2^32) / 3
        0x40000000,     // divisor 4: 2^30
        0x33333333,     // divisor 5
        0x2AAAAAAB,     // divisor 6
        0x24924925,     // divisor 7
        0x20000000,     // divisor 8: 2^29
        0x1C71C71C,     // divisor 9
        0x19999999,     // divisor 10
        0x1745D174,     // divisor 11
        0x15555555,     // divisor 12
        0x13B13B13,     // divisor 13
        0x12492492,     // divisor 14
        0x11111111,     // divisor 15
        0x10000000,     // divisor 16: 2^28
    };

    // Create switch table
    SwitchInst* Switch = Builder.CreateSwitch(Divisor, SlowBB, MaxDivisor);

    for (unsigned i = 2; i <= MaxDivisor; i++) {
        BasicBlock* CaseBB = createBasicBlock("div_case_" + std::to_string(i));
        Builder.SetInsertPoint(CaseBB);

        // result = (dividend * reciprocal[i]) >> 32
        Value* Reciprocal = ConstantInt::get(Builder.getInt64Ty(), reciprocals[i]);
        Value* Dividend64 = Builder.CreateZExt(Dividend, Builder.getInt64Ty());
        Value* Product = Builder.CreateMul(Dividend64, Reciprocal);
        Value* Result = Builder.CreateLShr(Product, 32);
        Value* Result32 = Builder.CreateTrunc(Result, Builder.getInt32Ty());

        Builder.CreateBr(MergeBB);
        PHI->addIncoming(Result32, CaseBB);

        Switch->addCase(ConstantInt::get(Ty, i), CaseBB);
    }

    return PHI;
}
```

**Advantage**: No actual division, just multiply + shift.
**Disadvantage**: Large code size for many cases.

#### Strategy 2: Computed Reciprocal (Larger Divisors)

For divisors up to threshold (e.g., 32):

```c
Value* generateReciprocalMultiply(IRBuilder<>& Builder,
                                  Value* Dividend,
                                  Value* Divisor) {
    // Compute reciprocal on-the-fly:
    // reciprocal = (2^32 + divisor - 1) / divisor
    // result = (dividend * reciprocal) >> 32

    Type* I32 = Builder.getInt32Ty();
    Type* I64 = Builder.getInt64Ty();

    // Extend to 64-bit for computation
    Value* Dividend64 = Builder.CreateZExt(Dividend, I64);
    Value* Divisor64 = Builder.CreateZExt(Divisor, I64);

    // Compute reciprocal: (2^32 + divisor - 1) / divisor
    Value* TwoPow32 = ConstantInt::get(I64, 1ULL << 32);
    Value* Numerator = Builder.CreateAdd(TwoPow32, Divisor64);
    Numerator = Builder.CreateSub(Numerator, ConstantInt::get(I64, 1));
    Value* Reciprocal = Builder.CreateUDiv(Numerator, Divisor64);

    // Multiply dividend by reciprocal
    Value* Product = Builder.CreateMul(Dividend64, Reciprocal);

    // Shift right by 32 to get quotient
    Value* Quotient64 = Builder.CreateLShr(Product, 32);
    Value* Quotient = Builder.CreateTrunc(Quotient64, I32);

    return Quotient;
}
```

**Trade-off**: Computes reciprocal at runtime, but avoids large switch table.

#### Strategy 3: Special Cases (Powers of 2)

Powers of 2 can be optimized to shifts:

```c
Value* optimizePowerOfTwo(IRBuilder<>& Builder,
                          Value* Dividend,
                          Value* Divisor) {
    // Check if divisor is power of 2
    // x / 2^n = x >> n

    // Check: (divisor & (divisor - 1)) == 0
    Value* DivisorMinusOne = Builder.CreateSub(Divisor, ConstantInt::get(Divisor->getType(), 1));
    Value* AndResult = Builder.CreateAnd(Divisor, DivisorMinusOne);
    Value* IsPowerOfTwo = Builder.CreateICmpEQ(AndResult, ConstantInt::get(AndResult->getType(), 0));

    // If power of 2, compute log2 and shift
    // log2(divisor) = countTrailingZeros(divisor)
    Value* ShiftAmount = Builder.CreateIntrinsic(
        Intrinsic::cttz,
        {Divisor->getType()},
        {Divisor, Builder.getTrue()});

    Value* ShiftResult = Builder.CreateLShr(Dividend, ShiftAmount);

    // Select between shift and slow division
    Value* Result = Builder.CreateSelect(IsPowerOfTwo, ShiftResult, SlowDiv);

    return Result;
}
```

### Phase 4: Handle Remainder Operations

For remainder (`x % y`), the fast path is slightly different:

```c
Value* generateFastRemainder(IRBuilder<>& Builder,
                              Value* Dividend,
                              Value* Divisor,
                              Value* Quotient) {
    // remainder = dividend - (quotient * divisor)
    Value* Product = Builder.CreateMul(Quotient, Divisor);
    Value* Remainder = Builder.CreateSub(Dividend, Product);
    return Remainder;
}
```

**Combined Division + Remainder**:
Some codes compute both `x / y` and `x % y`. Optimize together:

```llvm
; Original
%quotient = udiv i32 %x, %y
%remainder = urem i32 %x, %y

; Optimized: compute quotient, derive remainder
%quotient = udiv i32 %x, %y
%product = mul i32 %quotient, %y
%remainder = sub i32 %x, %product  ; Cheaper than second division
```

---

## Data Structures

### Division Candidate Information

```c
struct DivisionCandidate {
    BinaryOperator* DivInst;      // Division or remainder instruction
    Value* Dividend;              // Numerator
    Value* Divisor;               // Denominator
    bool IsSigned;                // Signed or unsigned division
    bool IsRemainder;             // Division or remainder
    unsigned Threshold;           // Bypass threshold for this division
    BasicBlock* ParentBB;         // Containing basic block
    Loop* ParentLoop;             // Containing loop (if any)
};
```

### Reciprocal Table (Precomputed)

```c
class ReciprocalTable {
public:
    // Get reciprocal for divisor
    uint64_t getReciprocal(unsigned divisor, unsigned bitWidth) {
        assert(divisor > 0 && "Division by zero");

        if (divisor == 1) return (1ULL << bitWidth);

        // Precomputed for common divisors (2-16)
        if (divisor <= 16 && bitWidth == 32) {
            return precomputed_32bit[divisor];
        }

        // Compute on-demand for larger divisors
        return computeReciprocal(divisor, bitWidth);
    }

private:
    static const uint64_t precomputed_32bit[17];

    uint64_t computeReciprocal(unsigned divisor, unsigned bitWidth) {
        // reciprocal = (2^bitWidth + divisor - 1) / divisor
        uint64_t twoToN = (1ULL << bitWidth);
        return (twoToN + divisor - 1) / divisor;
    }
};
```

---

## Configuration & Parameters

### Threshold Configuration

The bypass threshold is target-dependent:

```bash
# Default threshold (target-specific)
nvcc kernel.cu

# Override threshold (if supported)
nvcc -Xcicc -mllvm=-bypass-slow-div-threshold=16 kernel.cu
```

**Typical Thresholds**:
- **x86 (desktop CPU)**: 32 (division latency ~30 cycles)
- **ARM (mobile)**: 16 (division latency ~15-20 cycles)
- **GPU (CUDA)**: 8-12 (division latency ~20-25 cycles, but branches expensive)

### Enable/Disable Pass

```bash
# Disable BypassSlowDivision
nvcc -Xcicc -mllvm=-disable-bypass-slow-division kernel.cu

# Enable with specific threshold
nvcc -Xcicc -mllvm=-enable-bypass-slow-division -Xcicc -mllvm=-bypass-slow-div-threshold=16 kernel.cu
```

### Optimization Level

BypassSlowDivision typically runs at O2+:

```bash
# O0, O1: Disabled
nvcc -O0 kernel.cu
nvcc -O1 kernel.cu

# O2, O3: Enabled
nvcc -O2 kernel.cu
nvcc -O3 kernel.cu
```

---

## Pass Dependencies

### Required Analyses

BypassSlowDivision depends on:

1. **TargetTransformInfo (TTI)**:
   - Query division latency
   - Get bypass threshold
   - Determine if optimization is beneficial

2. **LoopInfo**:
   - Identify loops (higher benefit for loop-contained divisions)
   - Avoid excessive code bloat outside loops

3. **Dominator Tree**:
   - Safe basic block insertion

4. **Branch Probability Info** (optional):
   - Profile-guided optimization
   - Adjust threshold based on divisor distribution

### Preserved Analyses

BypassSlowDivision **invalidates**:
- Control Flow Graph (creates new basic blocks)
- Dominator Tree
- Loop Info (may affect loop structure)

BypassSlowDivision **preserves**:
- Alias Analysis
- Memory SSA (does not change memory operations)

### Pass Ordering

```
Optimization Pipeline:
    InstCombine, SimplifyCFG (simplify divisions)
    ↓
    SROA, GVN (expose constant divisors)
    ↓
    Loop optimizations
    ↓
    [BypassSlowDivision]  ← Insert fast paths before codegen
    ↓
    CodeGenPrepare
    ↓
    Instruction Selection
```

**Reasoning**:
- **After loop opts**: Don't interfere with loop vectorization
- **Before codegen**: Create fast paths early for better instruction selection

---

## Integration Points

### Integration with InstCombine

InstCombine handles constant divisors:

```llvm
; InstCombine simplifies division by constant
%result = udiv i32 %x, 8

; Optimized to shift (before BypassSlowDivision runs)
%result = lshr i32 %x, 3
```

**BypassSlowDivision complements InstCombine**:
- InstCombine: Constant divisors → direct optimization
- BypassSlowDivision: Variable divisors → conditional fast path

### Integration with Loop Optimizations

BypassSlowDivision increases loop complexity:

```llvm
; Original loop
loop:
    %i = phi i32 [0, %entry], [%i.next, %loop]
    %div = udiv i32 %i, %n
    ; ... use %div
    %i.next = add i32 %i, 1
    %cmp = icmp slt i32 %i.next, 100
    br i1 %cmp, label %loop, label %exit
```

**After BypassSlowDivision**:
```llvm
loop:
    %i = phi i32 [0, %entry], [%i.next, %loop_continue]
    %is_small = icmp ule i32 %n, 16
    br i1 %is_small, label %fast_div, label %slow_div

fast_div:
    %fast_result = call i32 @fast_div_function(i32 %i, i32 %n)
    br label %loop_continue

slow_div:
    %slow_result = udiv i32 %i, %n
    br label %loop_continue

loop_continue:
    %div = phi i32 [%fast_result, %fast_div], [%slow_result, %slow_div]
    ; ... rest of loop
```

**Concern**: Loop becomes more complex, harder to vectorize.

**Mitigation**: Only apply in loops where:
- Division is hot (profiling data)
- Loop is not vectorizable anyway
- Speedup > complexity cost

### Integration with Vectorization

**Problem**: BypassSlowDivision creates branches, preventing vectorization.

**Example**:
```cuda
// Original loop (vectorizable)
for (int i = 0; i < n; i++) {
    result[i] = data[i] / divisor;
}

// After BypassSlowDivision (not vectorizable due to branch)
for (int i = 0; i < n; i++) {
    if (divisor <= 16) {
        result[i] = fast_div(data[i], divisor);
    } else {
        result[i] = data[i] / divisor;
    }
}
```

**Solution**: Don't apply BypassSlowDivision to vectorizable loops.

```c
bool shouldOptimizeDivision(BinaryOperator* Div, LoopInfo& LI) {
    Loop* L = LI.getLoopFor(Div->getParent());
    if (!L) return true;  // Not in loop, safe to optimize

    // Check if loop is vectorizable
    if (L->getLoopVectorizeHint() == LoopVectorizeHint::Force) {
        return false;  // Don't break forced vectorization
    }

    // Check if division is hot enough to justify
    if (getDivisionFrequency(Div) < THRESHOLD) {
        return false;
    }

    return true;
}
```

---

## CUDA-Specific Considerations

### GPU Division Performance

**PTX Division Instructions**:
```ptx
# Signed 32-bit division (slow, ~20-30 cycles)
div.s32 %r1, %r2, %r3;

# Unsigned 32-bit division
div.u32 %r1, %r2, %r3;

# 64-bit division (even slower, ~40-50 cycles)
div.s64 %r1, %r2, %r3;

# Remainder (same latency as division)
rem.s32 %r1, %r2, %r3;
```

**Performance Characteristics**:
- Integer division: 20-30 cycles latency
- Not pipelined (unlike multiply)
- Blocks warp execution
- Throughput: ~1 division per 10-20 cycles per SM

### Fast Path on GPU

**Reciprocal Multiply**:
```ptx
# Fast path for small divisor
# Assumption: divisor <= 16, use precomputed reciprocal

# Load reciprocal from constant memory or register
ld.const.u32 %reciprocal, [reciprocal_table + %divisor * 4];

# Multiply dividend by reciprocal (3-5 cycles)
mul.wide.u32 %product, %dividend, %reciprocal;

# Shift right by 32 to get quotient (1 cycle)
shr.u64 %quotient64, %product, 32;
cvt.u32.u64 %quotient, %quotient64;
```

**Speedup**: ~10x faster (5-8 cycles vs 20-30 cycles).

### Branch Divergence Considerations

**Problem**: Branches cause warp divergence on GPUs.

**Example**:
```cuda
__global__ void kernel(int* results, int* dividends, int* divisors, int n) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n) {
        int divisor = divisors[idx];
        int dividend = dividends[idx];

        // BypassSlowDivision inserts branch here
        if (divisor <= 16) {
            results[idx] = fast_div(dividend, divisor);
        } else {
            results[idx] = dividend / divisor;
        }
    }
}
```

**Divergence**: If some threads have small divisors and others have large divisors, the warp diverges:
- Some threads execute fast path
- Others execute slow path
- **Both paths execute serially** (warp must reconverge)

**Worst Case**: All threads diverge → execute both paths → no speedup!

**Best Case**: All threads agree (all small or all large divisors) → no divergence → full speedup.

### Warp-Uniform Divisors

**Optimization**: If divisor is warp-uniform (same for all threads), no divergence:

```cuda
__global__ void kernel_uniform(int* results, int* dividends, int divisor, int n) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n) {
        int dividend = dividends[idx];

        // Divisor is same for all threads → no divergence
        if (divisor <= 16) {
            results[idx] = fast_div(dividend, divisor);  // All threads execute this
        } else {
            results[idx] = dividend / divisor;           // Or all threads execute this
        }
    }
}
```

**Detection**: BypassSlowDivision should detect warp-uniform divisors:

```c
bool isDivisorWarpUniform(Value* Divisor) {
    // Check if divisor is:
    // 1. Loop invariant (same across iterations)
    // 2. Derived from blockIdx/gridDim (same across threads in warp)
    // 3. Broadcast from lane 0 (__shfl_sync)

    // Heuristic: if divisor is loop-invariant, likely warp-uniform
    if (SE->isLoopInvariant(Divisor, L)) return true;

    // Check if derived from thread/block IDs
    // (complex analysis, often conservative)
    return false;
}
```

**Decision**:
- Warp-uniform divisor: Apply optimization (no divergence penalty)
- Non-uniform divisor: Be conservative (high divergence risk)

### GPU-Specific Threshold

GPU division is expensive, but branches are also expensive:

**Trade-off Analysis**:
```
Division latency: 20-30 cycles
Fast path latency: 5-8 cycles
Branch divergence cost: 2x (execute both paths)

Speedup if uniform: (20-30) / (5-8) = 2.5-6x
Slowdown if divergent: (5-8 + 20-30 + overhead) / (20-30) = 1.2-1.5x
```

**Recommendation**:
- Use **lower threshold** (8-12) to minimize code bloat
- Only apply to **warp-uniform divisors**
- Profile-guided: measure actual divisor distribution

---

## Evidence & Implementation

### Evidence from CICC Binary

**String Evidence**: None found directly.

**Structural Evidence**:
- Listed in `21_OPTIMIZATION_PASS_MAPPING.json` under "code_generation_preparation"
- Standard LLVM pass used by many backends
- Integer division optimization is common in GPU compilers

**Confidence Level**: MEDIUM
- Pass existence: MEDIUM (standard LLVM pass, likely in CICC)
- CICC implementation: MEDIUM (no direct evidence)
- Algorithm details: HIGH (well-documented LLVM pass)

### Implementation Estimate

**Estimated Function Count**: 40-80 functions
- Division candidate identification
- Fast path generation
- Reciprocal computation
- Branch insertion and merging

---

## Performance Impact

### Quantitative Results (Expected)

| Scenario | Division Latency | Fast Path Latency | Speedup |
|----------|------------------|-------------------|---------|
| **Small divisor (uniform)** | 20-30 cycles | 5-8 cycles | 2.5-6x |
| **Small divisor (divergent)** | 20-30 cycles | 30-40 cycles (both paths) | 0.7-1.0x (no benefit) |
| **Large divisor (no bypass)** | 20-30 cycles | 20-30 + branch overhead | 0.95-1.0x (slight overhead) |

### Best Case Scenarios

1. **Warp-uniform small divisors**:
   ```cuda
   // All threads divide by same small value
   __global__ void histogram(int* bins, int* data, int n, int num_bins) {
       int idx = threadIdx.x + blockIdx.x * blockDim.x;
       if (idx < n) {
           int bin = data[idx] % num_bins;  // num_bins typically small (e.g., 16)
           atomicAdd(&bins[bin], 1);
       }
   }
   ```
   **Speedup**: 3-5x for modulo operation.

2. **Loop-invariant divisor**:
   ```cuda
   // Divisor is constant across loop iterations
   for (int i = 0; i < n; i++) {
       result[i] = data[i] / divisor;  // divisor loop-invariant
   }
   ```
   **Speedup**: 2-4x if divisor frequently small.

3. **Time calculations**:
   ```cuda
   // Convert seconds to hours, minutes, seconds
   int hours = seconds / 3600;    // divisor = 3600 (small)
   int minutes = (seconds % 3600) / 60;  // divisor = 60 (small)
   int secs = seconds % 60;       // divisor = 60 (small)
   ```
   **Speedup**: 4-6x for all three operations.

### Worst Case Scenarios

1. **Divergent divisors**:
   ```cuda
   // Each thread has different divisor value
   __global__ void divergent_div(int* results, int* dividends, int* divisors, int n) {
       int idx = threadIdx.x + blockIdx.x * blockDim.x;
       if (idx < n) {
           results[idx] = dividends[idx] / divisors[idx];
           // High divergence risk
       }
   }
   ```
   **Slowdown**: 5-20% due to branch divergence.

2. **Large divisors**:
   ```cuda
   // Divisors typically > threshold
   int result = x / large_divisor;
   ```
   **Overhead**: 1-5% due to unnecessary branch check.

### Typical Results (CUDA Kernels)

**Histogram kernel** (realistic example):
```cuda
__global__ void histogram(int* bins, uint8_t* data, int n) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n) {
        int bin = data[idx] % 256;  // 256 bins
        atomicAdd(&bins[bin], 1);
    }
}
```

**Impact**:
- Modulo by 256: Can be optimized to bitwise AND (InstCombine, not BypassSlowDivision)
- No benefit from BypassSlowDivision (already optimized)

**Better example**:
```cuda
__global__ void variable_bins(int* bins, int* data, int n, int num_bins) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n) {
        int bin = data[idx] % num_bins;  // Variable number of bins
        atomicAdd(&bins[bin], 1);
    }
}
```

**Impact** (if `num_bins` frequently ≤ 16):
- Modulo speedup: 3-4x
- Overall kernel speedup: 10-20% (if modulo is bottleneck)

---

## Code Examples

### Example 1: Simple Division with Small Divisor

**CUDA Code**:
```cuda
__global__ void divide_kernel(int* results, int* dividends, int divisor, int n) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n) {
        results[idx] = dividends[idx] / divisor;
    }
}
```

**LLVM IR (Before BypassSlowDivision)**:
```llvm
%dividend = load i32, i32 addrspace(1)* %dividend_ptr
%result = udiv i32 %dividend, %divisor
store i32 %result, i32 addrspace(1)* %result_ptr
```

**LLVM IR (After BypassSlowDivision)**:
```llvm
%dividend = load i32, i32 addrspace(1)* %dividend_ptr

; Check if divisor is small
%is_small = icmp ule i32 %divisor, 16
br i1 %is_small, label %fast_div, label %slow_div

fast_div:
  ; Fast path: multiply by reciprocal
  %dividend_64 = zext i32 %dividend to i64
  %divisor_64 = zext i32 %divisor to i64
  %reciprocal = udiv i64 4294967296, %divisor_64  ; 2^32 / divisor
  %product = mul i64 %dividend_64, %reciprocal
  %quotient_64 = lshr i64 %product, 32
  %fast_result = trunc i64 %quotient_64 to i32
  br label %div_merge

slow_div:
  ; Slow path: original division
  %slow_result = udiv i32 %dividend, %divisor
  br label %div_merge

div_merge:
  %result = phi i32 [%fast_result, %fast_div], [%slow_result, %slow_div]
  store i32 %result, i32 addrspace(1)* %result_ptr
```

**PTX (Fast Path)**:
```ptx
# Check divisor size
setp.le.u32 %p, %divisor, 16;
@%p bra fast_div;

slow_div:
  div.u32 %r_result, %r_dividend, %r_divisor;  # 20-30 cycles
  bra div_merge;

fast_div:
  # Compute reciprocal: 2^32 / divisor
  cvt.u64.u32 %r_divisor_64, %r_divisor;
  mov.u64 %r_2pow32, 4294967296;
  div.u64 %r_reciprocal, %r_2pow32, %r_divisor_64;

  # Multiply by reciprocal
  cvt.u64.u32 %r_dividend_64, %r_dividend;
  mul.lo.u64 %r_product, %r_dividend_64, %r_reciprocal;

  # Shift right by 32
  shr.u64 %r_quotient_64, %r_product, 32;
  cvt.u32.u64 %r_result, %r_quotient_64;

div_merge:
  # %r_result contains quotient
```

### Example 2: Modulo Operation

**CUDA Code**:
```cuda
__global__ void modulo_kernel(int* results, int* data, int n, int modulus) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n) {
        results[idx] = data[idx] % modulus;
    }
}
```

**LLVM IR (After BypassSlowDivision)**:
```llvm
%value = load i32, i32 addrspace(1)* %data_ptr

; Fast path for modulo
%is_small = icmp ule i32 %modulus, 16
br i1 %is_small, label %fast_mod, label %slow_mod

fast_mod:
  ; Compute quotient using fast path
  %quotient = call i32 @fast_divide(i32 %value, i32 %modulus)
  ; Remainder = value - (quotient * modulus)
  %product = mul i32 %quotient, %modulus
  %fast_remainder = sub i32 %value, %product
  br label %mod_merge

slow_mod:
  ; Slow path: original modulo
  %slow_remainder = urem i32 %value, %modulus
  br label %mod_merge

mod_merge:
  %remainder = phi i32 [%fast_remainder, %fast_mod], [%slow_remainder, %slow_mod]
  store i32 %remainder, i32 addrspace(1)* %result_ptr
```

**Performance**: Modulo benefits even more than division (no native remainder instruction).

### Example 3: Combined Division and Remainder

**CUDA Code**:
```cuda
__global__ void divmod_kernel(int* quotients, int* remainders,
                              int* dividends, int divisor, int n) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n) {
        int dividend = dividends[idx];
        quotients[idx] = dividend / divisor;
        remainders[idx] = dividend % divisor;
    }
}
```

**LLVM IR (Optimized)**:
```llvm
%dividend = load i32, i32 addrspace(1)* %dividend_ptr

; Single division operation (fast or slow)
%is_small = icmp ule i32 %divisor, 16
br i1 %is_small, label %fast_divmod, label %slow_divmod

fast_divmod:
  %quotient_fast = call i32 @fast_divide(i32 %dividend, i32 %divisor)
  %product_fast = mul i32 %quotient_fast, %divisor
  %remainder_fast = sub i32 %dividend, %product_fast
  br label %divmod_merge

slow_divmod:
  %quotient_slow = udiv i32 %dividend, %divisor
  %product_slow = mul i32 %quotient_slow, %divisor
  %remainder_slow = sub i32 %dividend, %product_slow
  br label %divmod_merge

divmod_merge:
  %quotient = phi i32 [%quotient_fast, %fast_divmod], [%quotient_slow, %slow_divmod]
  %remainder = phi i32 [%remainder_fast, %fast_divmod], [%remainder_slow, %slow_divmod]

  store i32 %quotient, i32 addrspace(1)* %quotient_ptr
  store i32 %remainder, i32 addrspace(1)* %remainder_ptr
```

**Benefit**: Compute both quotient and remainder from single division + multiply + subtract.

### Example 4: Loop-Invariant Divisor

**CUDA Code**:
```cuda
__global__ void normalize(float* output, int* input, int n, int scale) {
    for (int idx = threadIdx.x + blockIdx.x * blockDim.x;
         idx < n;
         idx += blockDim.x * gridDim.x) {
        output[idx] = (float)(input[idx] / scale);
    }
}
```

**LLVM IR**:
```llvm
loop:
  %idx = phi i32 [%start_idx, %entry], [%idx_next, %loop]

  %input_val = load i32, i32 addrspace(1)* %input_ptr

  ; Divisor (scale) is loop-invariant
  %is_small = icmp ule i32 %scale, 16
  br i1 %is_small, label %fast_div, label %slow_div

fast_div:
  %quotient_fast = call i32 @fast_divide(i32 %input_val, i32 %scale)
  br label %div_merge

slow_div:
  %quotient_slow = udiv i32 %input_val, %scale
  br label %div_merge

div_merge:
  %quotient = phi i32 [%quotient_fast, %fast_div], [%quotient_slow, %slow_div]
  %quotient_float = sitofp i32 %quotient to float
  store float %quotient_float, float addrspace(1)* %output_ptr

  %idx_next = add i32 %idx, %stride
  %cmp = icmp slt i32 %idx_next, %n
  br i1 %cmp, label %loop, label %exit
```

**Optimization Opportunity**: Hoist divisor check out of loop:
```llvm
entry:
  %is_small = icmp ule i32 %scale, 16
  br i1 %is_small, label %fast_loop, label %slow_loop

fast_loop:
  %idx_fast = phi i32 [%start_idx, %entry], [%idx_next_fast, %fast_loop]
  %input_val_fast = load i32, i32 addrspace(1)* %input_ptr_fast
  %quotient_fast = call i32 @fast_divide(i32 %input_val_fast, i32 %scale)
  ; ... rest of loop body
  br i1 %cmp_fast, label %fast_loop, label %exit

slow_loop:
  %idx_slow = phi i32 [%start_idx, %entry], [%idx_next_slow, %slow_loop]
  %input_val_slow = load i32, i32 addrspace(1)* %input_ptr_slow
  %quotient_slow = udiv i32 %input_val_slow, %scale
  ; ... rest of loop body
  br i1 %cmp_slow, label %slow_loop, label %exit
```

**Benefit**: Check divisor once, then execute uniform loop (no divergence per iteration).

---

## Known Limitations

| Limitation | Impact | Workaround |
|-----------|--------|------------|
| **Branch divergence** | Slowdown on non-uniform divisors | Only apply to uniform divisors |
| **Code bloat** | Larger binary size | Limit to hot paths |
| **Interaction with vectorization** | May prevent SIMD optimization | Disable in vectorizable loops |
| **Threshold selection** | Hard to tune optimally | Profile-guided optimization |
| **Overhead for large divisors** | Slight slowdown (~1-5%) | Use profiling to enable selectively |

---

## References

**LLVM Source Code**:
- `llvm/lib/Transforms/Utils/BypassSlowDivision.cpp`
- `llvm/include/llvm/Transforms/Utils/BypassSlowDivision.h`

**LLVM Documentation**:
- https://llvm.org/docs/Passes.html#bypass-slow-division

**PTX ISA Guide**:
- Integer Arithmetic Instructions: Section 9.7.4 (div, rem)

**Related Optimizations**:
- Strength reduction (division by constant → shift/multiply)
- InstCombine (constant folding for division)

**Research**:
- Granlund & Montgomery, "Division by Invariant Integers using Multiplication" (1994)
- Warren, "Hacker's Delight" (2012) - Chapter on integer division optimization

---

**L3 Analysis Quality**: MEDIUM
**Last Updated**: 2025-11-17
**Source**: CICC foundation analyses + 21_OPTIMIZATION_PASS_MAPPING.json + LLVM documentation
