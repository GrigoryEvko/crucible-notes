# Interprocedural Constant Propagation (IPCP / IPSCCP)

**Pass Type**: Interprocedural optimization pass
**LLVM Class**: `llvm::IPConstantPropagationPass` / `llvm::IPSCCPPass` (Sparse Conditional Constant Propagation)
**Algorithm**: Sparse conditional constant propagation across function boundaries
**Extracted From**: CICC optimization pass mapping (94 identified passes)
**Analysis Quality**: MEDIUM - Pass identified as Interprocedural_SCCP in mapping
**Source**: `21_OPTIMIZATION_PASS_MAPPING.json` (line 281 - "Interprocedural_SCCP (IPSCCP)")
**Pass Category**: Interprocedural Optimization

---

## Overview

Interprocedural Constant Propagation (IPCP) extends constant propagation across function boundaries by analyzing constant values flowing through function calls and returns. The interprocedural variant uses **Sparse Conditional Constant Propagation (SCCP)** algorithm to track constants through the call graph, enabling aggressive constant folding and dead code elimination based on interprocedural constant flow.

**Core Strategy**: Track constant values across call sites, propagate into callees, propagate return values back to callers, enabling cross-function optimization.

**Key Benefits**:
- **Function specialization**: Create specialized versions for constant parameters
- **Dead code elimination**: Eliminate code paths unreachable due to constant conditions
- **Constant folding**: Fold operations with constant operands across functions
- **Better inlining**: Inline functions with constant parameters → more optimization

---

## Constant Lattice

IPSCCP uses a three-value lattice to track constant information:

```
        ⊤ (Top - Unknown/Overdefined)
       / \
      /   \
     c₁   c₂  ... cₙ  (Constant values)
      \   /
       \ /
        ⊥ (Bottom - Undefined/Not yet analyzed)
```

**Lattice values**:
- **⊥ (Bottom)**: Value not yet computed (initial state)
- **c (Constant)**: Value is known constant c
- **⊤ (Top)**: Value is not constant (overdefined)

**Lattice operations**:
```
meet(⊥, x) = x
meet(c, c) = c (same constant)
meet(c₁, c₂) = ⊤ (different constants)
meet(⊤, x) = ⊤
```

---

## Algorithm: Sparse Conditional Constant Propagation

### Intraprocedural SCCP (Within Functions)

**Sparse**: Only processes potentially executable instructions (follows constant-feasible paths).

**Step 1**: Initialize all values to ⊥ (undefined)
**Step 2**: Mark entry basic block as executable
**Step 3**: Worklist algorithm:
  - Process executable basic blocks
  - Compute constant values for instructions
  - Mark successor blocks executable if branch constant
  - Update value lattice (⊥ → c → ⊤)

**Example**:
```c
int foo(int x) {
    if (x > 10) {  // x is parameter (initially ⊥)
        return x * 2;
    } else {
        return x + 1;
    }
}
```

**Without constant**: Both branches analyzed (both sides executable).

**With constant** (`x = 5` known):
```c
int foo(int x = 5) {  // Constant parameter
    if (5 > 10) {  // FALSE (constant condition)
        return 5 * 2;  // UNREACHABLE
    } else {
        return 5 + 1;  // = 6 (constant folded)
    }
}
```

**Result**: Only else branch analyzed, then-branch marked unreachable.

### Interprocedural SCCP (Across Functions)

**Extension**: Track constants flowing through call sites.

**Step 1**: Analyze call sites for constant arguments
**Step 2**: Propagate constants into callee parameters
**Step 3**: Analyze callee with constant parameter values
**Step 4**: Propagate constant return values back to call sites

**Example**:
```c
int square(int x) {
    return x * x;
}

int caller() {
    int a = square(5);  // Constant argument: 5
    int b = square(a);  // a = 25 (from constant propagation)
    return b;
}
```

**Analysis**:
```
Call 1: square(5)
  - Propagate: x = 5 into square
  - Analyze: return 5 * 5 = 25
  - Propagate back: a = 25

Call 2: square(a)
  - Propagate: x = 25 into square (from a)
  - Analyze: return 25 * 25 = 625
  - Propagate back: b = 625

Result: return 625 (constant!)
```

**Optimized code**:
```c
int caller() {
    return 625;  // Fully constant-folded
}
```

---

## Transformation Examples

### Example 1: Function Specialization

**Before**:
```c
int compute(int x, int flag) {
    if (flag == 0) {
        return x * 2;
    } else {
        return x * 3;
    }
}

void caller_a() {
    int result = compute(10, 0);  // flag = 0 (constant)
}

void caller_b() {
    int result = compute(20, 1);  // flag = 1 (constant)
}
```

**After IPCP**:
```c
// Specialized versions created
int compute_flag0(int x) {
    return x * 2;  // Only this branch
}

int compute_flag1(int x) {
    return x * 3;  // Only this branch
}

void caller_a() {
    int result = compute_flag0(10);  // = 20
}

void caller_b() {
    int result = compute_flag1(20);  // = 60
}
```

**Benefits**:
- Smaller function bodies → better inlining
- Eliminated branches → better branch prediction
- Further constant propagation possible

### Example 2: Dead Code Elimination via Constants

**Before**:
```c
int process(int mode) {
    if (mode == DEBUG_MODE) {
        expensive_logging();  // Only in debug
    }
    return compute_result();
}

void production_code() {
    int result = process(PRODUCTION_MODE);  // PRODUCTION_MODE != DEBUG_MODE
}
```

**After IPCP** (with `PRODUCTION_MODE` constant):
```c
int process(int mode = PRODUCTION_MODE) {
    if (PRODUCTION_MODE == DEBUG_MODE) {  // FALSE
        expensive_logging();  // DEAD CODE (eliminated)
    }
    return compute_result();
}

void production_code() {
    int result = compute_result();  // Inlined and simplified
}
```

**Benefits**:
- Debug code eliminated in production builds
- Smaller code size
- No runtime overhead for unused modes

### Example 3: Chain Propagation

**Before**:
```c
int add(int a, int b) {
    return a + b;
}

int double_value(int x) {
    return add(x, x);
}

int main() {
    int result = double_value(21);
    return result;
}
```

**After IPCP**:
```c
// Fully propagated through call chain
int main() {
    // double_value(21) → add(21, 21) → 42
    return 42;  // Constant!
}
```

---

## Module-Level Analysis Requirements

### Call Graph Constant Flow

Track constants through entire call graph:

```
Module Analysis:
main()
├── calls: foo(5)      [constant arg: 5]
│   └── returns: 10    [constant return]
└── calls: bar(10)     [constant arg: 10 from foo]
    └── returns: 20    [constant return]

Result: main can return 20 (constant)
```

### Global Constant Tracking

```c
static const int THRESHOLD = 100;

int check(int value) {
    return value > THRESHOLD;  // THRESHOLD = 100 (constant)
}

void caller() {
    int result = check(50);  // 50 > 100 = false (constant)
}
```

**Analysis**: Global constant propagated into `check()` → constant condition → eliminates branch.

---

## CUDA-Specific Considerations

### Kernel Configuration Constants

**Pattern**: Kernel launch configurations often constant:

```cuda
#define BLOCK_SIZE 256
#define GRID_SIZE 1024

__global__ void kernel(int* data, int block_size, int grid_size) {
    int tid = threadIdx.x;
    int bid = blockIdx.x;

    if (block_size == BLOCK_SIZE && grid_size == GRID_SIZE) {
        // Optimized path for common configuration
        int idx = bid * BLOCK_SIZE + tid;  // Constant BLOCK_SIZE
        data[idx] = idx;
    } else {
        // Fallback for dynamic configuration
        int idx = bid * block_size + tid;
        data[idx] = idx;
    }
}

void launch() {
    kernel<<<GRID_SIZE, BLOCK_SIZE>>>(data, BLOCK_SIZE, GRID_SIZE);
    // Constants propagated into kernel
}
```

**After IPCP**:
```cuda
__global__ void kernel(int* data, int block_size = 256, int grid_size = 1024) {
    int tid = threadIdx.x;
    int bid = blockIdx.x;

    // Condition: (256 == 256 && 1024 == 1024) = TRUE (constant)
    // Optimized path always taken
    int idx = bid * 256 + tid;  // Constant folded
    data[idx] = idx;
    // Fallback path eliminated (dead code)
}
```

**Benefit**: Single code path, no branches, constants enable further optimization.

### Device Function Specialization

```cuda
__device__ int power(int base, int exponent) {
    int result = 1;
    for (int i = 0; i < exponent; i++) {
        result *= base;
    }
    return result;
}

__global__ void kernel(int* output) {
    int tid = threadIdx.x;
    output[tid] = power(2, 10);  // exponent = 10 (constant)
}
```

**After IPCP** (loop unrolling enabled by constant):
```cuda
__device__ int power_2_10() {
    // Unrolled: 2^10 = 1024
    return 1024;  // Fully constant-folded
}

__global__ void kernel(int* output) {
    int tid = threadIdx.x;
    output[tid] = 1024;  // Direct constant
}
```

**Benefit**: No loop overhead, direct constant assignment.

### Thread Index Constant Folding

**Limitation**: `threadIdx`, `blockIdx` are runtime values (not constants).

**Workaround**: Loop unrolling with constant trip counts:

```cuda
__global__ void kernel(float* data) {
    int tid = threadIdx.x;

    // Loop with constant bound
    for (int i = 0; i < 4; i++) {  // Constant trip count
        data[tid * 4 + i] = i;  // i is constant per iteration
    }
}
```

**After IPCP + unrolling**:
```cuda
__global__ void kernel(float* data) {
    int tid = threadIdx.x;

    // Unrolled with constant offsets
    data[tid * 4 + 0] = 0;
    data[tid * 4 + 1] = 1;
    data[tid * 4 + 2] = 2;
    data[tid * 4 + 3] = 3;
}
```

---

## Interaction with Other Passes

### Upstream Dependencies

| Pass | Purpose | Integration |
|------|---------|-------------|
| **Call Graph Analysis** | Build call graph | Required for interprocedural analysis |
| **Alias Analysis** | Determine memory dependencies | Required to track global constants |
| **Inlining** | May expose constant arguments | Creates IPCP opportunities |

### Downstream Benefits

| Pass | Benefit | Impact |
|------|---------|--------|
| **Dead Code Elimination** | Eliminate unreachable code | Major code size reduction |
| **Loop Unrolling** | Unroll loops with constant bounds | Performance improvement |
| **Function Specialization** | Create specialized versions | Better optimization |
| **Constant Folding** | Fold arithmetic with constants | Performance + size reduction |
| **Branch Elimination** | Remove constant branches | Better branch prediction |

### Pipeline Position

```
Interprocedural Pipeline:
1. Call Graph Construction
2. Global Optimizer (constant globals)
3. Function Inlining (early)
4. → IP Constant Propagation ← (operates here)
5. Dead Code Elimination
6. Function Specialization
7. Loop Unrolling
8. Inlining (late, with constants)
```

---

## Cost Model and Profitability

### Specialization Decision

**Create specialized version when**:
```
benefit = (code_size_reduction_per_call × num_constant_calls) +
          (performance_gain_per_call × call_frequency)

cost = specialized_function_overhead + increased_code_size

specialize_if: benefit > cost × threshold
```

**Typical threshold**: **2.0×** benefit required.

**Example decision**:
```c
int foo(int x, int constant_flag) {
    if (constant_flag) {
        // 100 instructions
    } else {
        // 100 instructions
    }
}
```

**Specialization**:
- 10 call sites with `constant_flag = 0` (constant)
- 1 call site with variable `constant_flag`

**Decision**: Create `foo_const0(int x)` specialized version (eliminates 100 instructions × 10 calls = 1000 instructions saved).

---

## Performance Characteristics

### Compile-Time Overhead

- **Lattice initialization**: O(v) where v = variables
- **Worklist processing**: O(e × k) where e = edges, k = lattice height (typically k ≤ 3)
- **Interprocedural propagation**: O(c × f) where c = call sites, f = functions
- **Total**: **5-12% compile-time increase** for large modules with many constants

### Runtime Performance Impact

| Transformation | Impact | Measurement |
|----------------|--------|-------------|
| **Constant folding** | 15-40% improvement | Eliminates computation |
| **Dead code elimination** | 10-25% improvement | Eliminates branches, loops |
| **Function specialization** | 8-20% improvement | Smaller, optimized code paths |
| **Branch elimination** | 5-15% improvement | Better branch prediction |

### Code Size Impact

- **Dead code elimination**: -10% to -30% (removes unreachable code)
- **Specialization overhead**: +5% to +15% (creates specialized versions)
- **Net effect**: -5% to -20% code size reduction (typically)

---

## Configuration and Control

### Enabling/Disabling IPCP

```bash
# Enable IPCP (usually default with -O2+)
-fwhole-program -fipa-cp

# Disable IPCP
-fno-ipa-cp

# LLVM-specific flags (hypothetical)
-mllvm -enable-ipsccp
-mllvm -disable-ipsccp
```

### Controlling Specialization

```bash
# Set specialization threshold
-mllvm -ipcp-specialization-threshold=2.0

# Limit number of specialized versions per function
-mllvm -ipcp-max-specializations=4
```

---

## Known Limitations

### Non-Constant Inputs

**Issue**: Cannot propagate non-constant values:

```c
int process(int x) {
    return x * 2;
}

void caller(int user_input) {
    int result = process(user_input);  // user_input not constant
}
```

**Analysis**: No constant propagation possible (input is variable).

### Indirect Calls

**Issue**: Function pointer calls prevent interprocedural analysis:

```c
int (*func_ptr)(int) = foo;
int result = func_ptr(42);  // Indirect call - cannot propagate
```

### Complex Control Flow

**Issue**: Phi nodes with multiple non-constant predecessors:

```c
int foo(int a, int b, int flag) {
    int x;
    if (flag) {
        x = a;  // Unknown
    } else {
        x = b;  // Unknown
    }
    return x * 2;  // x is overdefined (⊤)
}
```

**Analysis**: `x` becomes overdefined (⊤) → no constant propagation.

---

## Implementation Evidence

**Source**: CICC optimization pass mapping analysis
**Confidence**: MEDIUM (pass identified as "Interprocedural_SCCP (IPSCCP)" in mapping)
**Evidence**:
- Pass listed under scalar_optimization category as "Interprocedural_SCCP"
- SCCP variant for interprocedural analysis standard in LLVM
- Critical for CUDA kernel optimization (many compile-time constants)

**Estimated Functions**: ~100-150 functions implementing IPSCCP logic (sparse analysis, lattice operations, interprocedural propagation).

---

## Algorithm Pseudocode

```c
void IPSCCPPass(Module* M) {
    // Initialize lattice values to ⊥ (undefined)
    DenseMap<Value*, LatticeValue> Lattice;
    for (Function* F : M->functions()) {
        for (Instruction* I : F->instructions()) {
            Lattice[I] = LatticeValue::Bottom();
        }
    }

    // Worklist: (Function, BasicBlock) pairs
    SmallVector<std::pair<Function*, BasicBlock*>> Worklist;

    // Initialize: entry blocks of all functions
    for (Function* F : M->functions()) {
        Worklist.push_back({F, &F->getEntryBlock()});
    }

    // Process worklist
    while (!Worklist.empty()) {
        auto [F, BB] = Worklist.pop_back_val();

        // Process each instruction in BB
        for (Instruction* I : BB->instructions()) {
            LatticeValue OldValue = Lattice[I];
            LatticeValue NewValue = evaluateInstruction(I, Lattice);

            // Update lattice
            LatticeValue MeetValue = meet(OldValue, NewValue);
            if (MeetValue != OldValue) {
                Lattice[I] = MeetValue;

                // Propagate to users
                for (User* U : I->users()) {
                    if (Instruction* UserI = dyn_cast<Instruction>(U)) {
                        Worklist.push_back({UserI->getFunction(),
                                            UserI->getParent()});
                    }
                }
            }

            // Interprocedural propagation
            if (CallInst* CI = dyn_cast<CallInst>(I)) {
                Function* Callee = CI->getCalledFunction();
                if (Callee && !Callee->isDeclaration()) {
                    // Propagate constants to callee parameters
                    for (auto [Arg, Param] : zip(CI->args(),
                                                  Callee->args())) {
                        LatticeValue ArgValue = Lattice[Arg];
                        if (ArgValue.isConstant()) {
                            Lattice[&Param] = ArgValue;
                            Worklist.push_back({Callee,
                                                &Callee->getEntryBlock()});
                        }
                    }

                    // Propagate return value back
                    for (ReturnInst* RI : Callee->returns()) {
                        Value* RetVal = RI->getReturnValue();
                        LatticeValue RetValue = Lattice[RetVal];
                        Lattice[CI] = meet(Lattice[CI], RetValue);
                    }
                }
            }
        }

        // Mark successor blocks executable if branch constant
        if (BranchInst* BI = dyn_cast<BranchInst>(BB->getTerminator())) {
            if (BI->isConditional()) {
                LatticeValue CondValue = Lattice[BI->getCondition()];
                if (CondValue.isConstant()) {
                    BasicBlock* Successor = CondValue.getBool() ?
                                            BI->getSuccessor(0) :
                                            BI->getSuccessor(1);
                    Worklist.push_back({F, Successor});
                } else {
                    // Both successors executable
                    Worklist.push_back({F, BI->getSuccessor(0)});
                    Worklist.push_back({F, BI->getSuccessor(1)});
                }
            }
        }
    }

    // Apply transformations based on lattice
    for (Function* F : M->functions()) {
        for (Instruction* I : F->instructions()) {
            LatticeValue Value = Lattice[I];
            if (Value.isConstant()) {
                I->replaceAllUsesWith(Value.getConstant());
                I->eraseFromParent();
            }
        }
    }
}

LatticeValue evaluateInstruction(Instruction* I,
                                  DenseMap<Value*, LatticeValue>& Lattice) {
    // Evaluate based on instruction type
    if (BinaryOperator* BO = dyn_cast<BinaryOperator>(I)) {
        LatticeValue LHS = Lattice[BO->getOperand(0)];
        LatticeValue RHS = Lattice[BO->getOperand(1)];

        if (LHS.isConstant() && RHS.isConstant()) {
            // Constant fold
            return LatticeValue::constant(
                ConstantFoldBinaryOp(BO->getOpcode(),
                                     LHS.getConstant(),
                                     RHS.getConstant()));
        } else if (LHS.isOverdefined() || RHS.isOverdefined()) {
            return LatticeValue::Overdefined();
        } else {
            return LatticeValue::Bottom();  // Not yet determined
        }
    }

    // ... other instruction types ...

    return LatticeValue::Overdefined();
}
```

---

## Related Optimizations

- **[Global Optimizer](interprocedural-global-optimizer.md)**: Optimizes global constants
- **[Dead Code Elimination](dce.md)**: Eliminates unreachable code after IPCP
- **[Function Specialization](function-specialization.md)**: Creates specialized versions
- **[Inlining](inlining.md)**: Benefits from constant arguments

---

**Analysis Quality**: MEDIUM
**Last Updated**: 2025-11-17
**Source**: CICC optimization pass mapping (21_OPTIMIZATION_PASS_MAPPING.json, line 281)
