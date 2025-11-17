# CUDA Thread Divergence Analysis and ADCE Integration

## Executive Summary

CICC implements a **forward data-flow divergence analysis** integrated with **Aggressive Dead Code Elimination (ADCE)** to safely optimize CUDA kernel code while respecting thread divergence constraints. The system ensures that code with side effects in divergent regions is never eliminated, even if it appears dead in standard DCE analysis.

## Core Algorithm

### 1. Divergence Source Detection

The compiler identifies the following divergence sources:

| Source | Classification | Rationale |
|--------|-----------------|-----------|
| **threadIdx.x/y/z** | DIVERGENT | Each thread has unique threadIdx - causes warp divergence |
| **blockIdx.x/y/z** | CONTEXT_DEPENDENT | Different blocks have different blockIdx; uniform within block |
| **blockDim.x/y/z** | UNIFORM | Kernel launch parameter, same for all threads |
| **gridDim.x/y/z** | UNIFORM | Kernel launch parameter, same for all threads |
| **warpSize** | UNIFORM | Architecture constant (typically 32) |

**Detection Location**: `sub_920430 @ 0x920430`
- Analyzes operand attributes
- Classifies values against known CUDA built-ins
- Returns classification code (0-4)

### 2. Forward Divergence Propagation

Once divergence sources are identified, the analysis propagates divergence through:

1. **Data Dependencies**: Values computed from divergent values are marked as divergent
2. **Control Dependencies**: Instructions control-dependent on divergent branches are marked as divergent
3. **Through Basic Blocks**: Divergence flows until it reaches a convergence point

**Key Function**: `sub_6A49A0 @ 0x6a49a0`
- Analyzes thread index comparisons in conditionals
- Processes SSA form operand dependencies
- Identifies which conditional branches are divergent

### 3. Convergence Point Detection

Divergence ceases at the following convergence points:

#### A. Explicit Synchronization
- **`__syncthreads()` / `cuda.syncthreads()` calls**
- All threads must reach before proceeding
- Hard guarantee of reconvergence

**Detection**:
- `sub_90AEE0 @ 0x90aee0` - Register __syncthreads intrinsic
- `sub_A91130 @ 0xa91130` - Detect cuda.syncthreads calls

#### B. Post-Dominator Tree Convergence
- Block that post-dominates all predecessors in divergent region
- All threads must pass through the block before diverging further
- Handled by StructurizeCFG pass

#### C. Block Boundaries
- Single predecessor to successor block
- Implicit convergence as threads exit/enter blocks

#### D. Function Return
- Function-level convergence point
- All threads must complete before returning

### 4. Uniformity Pass Integration

The **UniformityPass** computes uniformity information:

```
LLVM Pass: UniformityPass
├── Backward dependence analysis
├── Determines uniform values across all threads
└── Stores uniformity metadata per instruction
```

**Pass Names**:
- `print<uniformity>` - Analyze and print results
- `require<uniformity>` - Dependency declaration
- `invalidate<uniformity>` - Invalidate cached results

## ADCE Integration

### Problem Statement

Standard Dead Code Elimination (DCE) is **unsafe in divergent regions** because:

```
Divergent Code Example:
if (threadIdx.x == 0) {
    globalArray[0] = newValue;  // Only thread 0 executes
}
// This CANNOT be eliminated, even though it "looks dead"
// because thread 0 actually executes it
```

### Solution: Divergence-Aware ADCE

The ADCE pass (Aggressive Dead Code Elimination) respects divergence constraints:

**ADCE Driver**: `sub_2ADCE40 @ 0x2adce40`
**Core Algorithm**: `sub_30ADAE0 @ 0x30adae0`

### ADCE Phases

1. **Liveness Analysis**: Mark all instructions with live side effects
2. **Dependence Computation**: Compute data/control dependencies from live instructions
3. **Divergence-Aware Marking**:
   - Mark dependent instructions as alive
   - **KEY**: If instruction is in divergent region AND has side effects → ALIVE
4. **Dead Code Removal**: Remove all unmarked instructions

### Safety Rules

#### Rule R1: Uniform Execution Requirement
```
✓ Can eliminate: Code ALL threads execute OR code with NO side effects
✗ Cannot eliminate: Code some threads execute AND has side effects
```

#### Rule R2: Memory Operation Preservation
```
Protected Operations:
- Store operations (global, shared, local memory)
- Atomic operations (atomicAdd, atomicCAS, etc.)
- Device memory synchronization operations
```

#### Rule R3: Control Dependence Safety
```
If instruction is control-dependent on divergent branch:
- Cannot eliminate if it has side effects
- Can eliminate if it has NO side effects (with additional checks)
```

#### Rule R4: Side Effect Preservation (Global)
```
ALWAYS PRESERVED:
- Function calls (may have external side effects)
- I/O operations (device communication)
- Volatile memory accesses
- Intrinsic calls (especially convergence control)
```

#### Rule R5: Convergent Operation Constraints
```
Operations marked 'convergent' have special semantics:
- Cannot add control dependencies to convergent operations
- Convergence control tokens have semantic significance
- Used for explicit warp-level synchronization
```

#### Rule R6: Speculative Execution Limits
```
Divergent Targets (branches with divergent condition):
- Restrict speculative execution to divergent targets only
- Option: "only-if-divergent-target"
- Prevents inserting code speculatively in divergent regions
```

## Algorithm Interaction

```
┌─────────────────────────────────────────────────────────┐
│ 1. Uniformity Pass                                      │
│    - Compute uniformity for all instructions            │
│    - Output: UniformityInfo metadata                    │
└─────────────┬───────────────────────────────────────────┘
              │
┌─────────────▼───────────────────────────────────────────┐
│ 2. ADCE Pass (Aggressive Dead Code Elimination)         │
│    Phase 1: Liveness analysis (mark side effects)       │
│    Phase 2: Compute data/control dependencies           │
│    Phase 3: Divergence-aware marking                    │
│             - Query UniformityPass results              │
│             - Check for side effects in divergent code  │
│    Phase 4: Remove unmarked instructions                │
└─────────────┬───────────────────────────────────────────┘
              │
┌─────────────▼───────────────────────────────────────────┐
│ 3. StructurizeCFG (Uniform Region Handling)             │
│    - Enforce structured control flow                    │
│    - Skip/relax uniform region transformations          │
└─────────────────────────────────────────────────────────┘
```

## Key Data Structures

### UniformityInfo
```cpp
struct UniformityInfo {
    bool is_uniform;           // True if uniform across all threads
    Value* divergence_source;  // Which value caused divergence
    BasicBlock* convergence_point;  // Where divergence ends
};
```

### DivergenceMarker
```cpp
struct DivergenceMarker {
    bool is_divergent;
    Value* divergent_condition;
    BasicBlock* convergence_block;
};
```

### SideEffectMap
```cpp
struct SideEffectMap {
    bool has_side_effect;
    enum { STORE, CALL, VOLATILE, ATOMIC } effect_type;
    bool critical_semantics;  // Convergence control, etc.
};
```

## Critical Implementation Details

### Divergence Classification Codes

Function `sub_920430` returns:
- **0**: threadIdx (DIVERGENT)
- **1**: blockDim (UNIFORM)
- **2**: blockIdx (CONTEXT_DEPENDENT)
- **3**: gridDim (UNIFORM)
- **4**: warpSize (UNIFORM)

### Thread Index Comparison Analysis

Function `sub_6A49A0` detects:
- Direct threadIdx comparisons: `if (threadIdx.x == 0)`
- Computed indices: `if ((threadIdx.x % 8) == 0)`
- Multi-dimensional index operations
- Complex conditional expressions with thread indices

### Liveness Propagation

ADCE uses iterative fixed-point algorithm:
```
repeat {
    mark_as_alive(instructions_with_side_effects);
    mark_as_alive(operands_of_alive_instructions);
    mark_as_alive(control_dependencies_of_alive_instructions);
} until no_changes;
```

### Configuration Options

```
ADCE Pass Configuration:
├── adce-remove-control-flow: bool (default=true)
│   └── Enable/disable removal of dead conditional branches
└── adce-remove-loops: bool (default=true)
    └── Enable/disable removal of dead loop structures
```

## Correctness Guarantees

✓ **All threads executing the same code can be optimized normally**

✓ **Code with side effects in divergent regions is preserved**

✓ **Synchronization points enforce convergence guarantees**

✓ **No data corruption from elimination of memory operations**

✓ **Semantic correctness maintained across all optimization levels**

## Edge Cases and Special Handling

### 1. Partial Convergence
Some threads may reconverge while others remain divergent. The analysis tracks this precisely.

### 2. Nested Divergence
Multiple levels of thread divergence are handled through iterative divergence propagation.

### 3. Data-Flow Merges
Values from both divergent and non-divergent paths must be carefully handled at control-flow joins.

### 4. Shared Memory Operations
Shared memory operations in divergent code require special care due to bank conflicts and serialization.

### 5. Function Calls in Divergent Code
Function calls cannot be eliminated even if they appear unused, as side effects may be external.

## Performance Implications

**Cost of Safety**:
- Queries to UniformityPass on every potential elimination
- Conservative marking in uncertain cases
- Slightly slower than unsafe standard DCE

**Benefit**:
- Guarantees semantic correctness
- Enables aggressive optimization safely
- Critical for CUDA kernel correctness

## References and Related Components

### Related LLVM Passes
- **UniformityPass**: Core divergence analysis
- **StructurizeCFG**: Uniform region handling
- **SpeculativeExecutionPass**: Respects divergent target constraints
- **ConvergenceControl**: LLVM intrinsics for explicit convergence semantics

### Intrinsic Functions
- `llvm.experimental.convergence.entry`
- `llvm.experimental.convergence.anchor`
- `llvm.experimental.convergence.loop`
- `llvm.experimental.convergence.control`

### Key CUDA Operations Preserved
- `__syncthreads()` - Warp synchronization
- `atomicAdd()`, `atomicCAS()` - Atomic operations
- Store operations to global/shared memory
- Device function calls
- Volatile memory accesses

## Summary

The divergence analysis algorithm in CICC is a sophisticated system that:

1. **Identifies** which values cause thread divergence
2. **Propagates** divergence information through IR
3. **Detects** where threads reconverge
4. **Integrates** with ADCE to preserve code correctness
5. **Enforces** safety rules preventing data corruption

This enables safe, aggressive optimization of CUDA kernels while maintaining correctness across all execution paths.
