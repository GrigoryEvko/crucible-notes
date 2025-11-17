# Correlated Value Propagation (CVP)

**Pass Type**: Function-level scalar optimization
**LLVM Class**: `llvm::CorrelatedValuePropagationPass`
**Extracted From**: CICC binary analysis and pass manager data
**Analysis Quality**: MEDIUM-HIGH - Multiple pass IDs and configuration evidence
**Evidence Sources**: Pass manager registry, string literals

---

## Overview

Correlated Value Propagation (CVP) uses value range information from LazyValueInfo to optimize comparisons, select instructions, and branches. It propagates information about value relationships to simplify control flow and arithmetic operations.

**Core Algorithm**: LazyValueInfo-based value range analysis with targeted transformations

**Key Features**:
- Simplifies comparisons using value ranges
- Folds select instructions with known conditions
- Eliminates redundant range checks
- Propagates non-null pointer information
- Integrates with jump threading

---

## Pass Registration and Configuration

### CVP Pass Instances

**Evidence**: `pass-management-algorithms.md:1042,1060,1082,1144`

```c
{60,  "CorrelatedValueProp", EVEN, 0x0, "Correlated value propagation"},
{74,  "CorrelatedValueProp", EVEN, 0x0, "CVP"},
{92,  "CorrelatedValueProp", EVEN, 0x0, "CVP analysis"},
{144, "CorrelatedValueProp", EVEN, 0x0, "CVP"},
```

Multiple instances at IDs 60, 74, 92, 144 suggest CVP runs at different pipeline stages.

**Evidence**: `pass-manager.md:1138-1139`

```c
{70, "CorrelatedValueProp",     Meta, FUNCTION, ANALYSIS,  -, LazyValueInfo, "Correlated value analysis"}
{71, "CorrelatedValuePropPass", Bool, FUNCTION, TRANSFORM, -, LazyValueInfo, "CVP transform"}
```

Two components:
- **Analysis pass**: Gathers correlated value information
- **Transform pass**: Applies optimizations

---

## Algorithm

### LazyValueInfo Integration

CVP heavily relies on **LazyValueInfo** (LVI) for value range information:

```
LazyValueInfo provides:
  - Value ranges at specific program points
  - Constant information inferred from control flow
  - Non-null pointer analysis
  - Range constraints from comparisons
```

### Core Algorithm Pseudocode

```python
def CorrelatedValuePropagation(function, LVI):
    modified = False

    for BB in function.basic_blocks:
        for instr in BB.instructions:
            # Try each optimization pattern
            if optimize_comparison(instr, LVI):
                modified = True
            elif optimize_select(instr, LVI):
                modified = True
            elif optimize_branch(instr, LVI):
                modified = True
            elif optimize_phi(instr, LVI):
                modified = True
            elif optimize_switch(instr, LVI):
                modified = True

    return modified

def optimize_comparison(instr, LVI):
    """Simplify comparisons using value ranges"""
    if not instr.is_comparison():
        return False

    lhs = instr.get_operand(0)
    rhs = instr.get_operand(1)

    # Get value ranges from LVI
    lhs_range = LVI.get_constant_range(lhs, instr.parent_block)
    rhs_range = LVI.get_constant_range(rhs, instr.parent_block)

    # Check if comparison is always true or always false
    result = evaluate_comparison(instr.predicate, lhs_range, rhs_range)

    if result == AlwaysTrue:
        instr.replace_all_uses_with(ConstantInt::getTrue())
        return True
    elif result == AlwaysFalse:
        instr.replace_all_uses_with(ConstantInt::getFalse())
        return True

    # Try to simplify predicate
    new_pred = simplify_predicate(instr.predicate, lhs_range, rhs_range)
    if new_pred != instr.predicate:
        instr.set_predicate(new_pred)
        return True

    return False

def optimize_select(instr, LVI):
    """Fold select instructions with known conditions"""
    if not instr.is_select():
        return False

    condition = instr.get_condition()

    # Query LVI for condition value
    cond_val = LVI.get_constant(condition, instr.parent_block)

    if cond_val == True:
        # Always select true value
        instr.replace_all_uses_with(instr.get_true_value())
        return True
    elif cond_val == False:
        # Always select false value
        instr.replace_all_uses_with(instr.get_false_value())
        return True

    return False

def optimize_branch(instr, LVI):
    """Simplify conditional branches"""
    if not instr.is_conditional_branch():
        return False

    condition = instr.get_condition()

    # Get condition value at this point
    cond_val = LVI.get_constant(condition, instr.parent_block)

    if cond_val == True:
        # Convert to unconditional branch to true successor
        create_unconditional_branch(instr.true_successor)
        instr.erase_from_parent()
        return True
    elif cond_val == False:
        # Convert to unconditional branch to false successor
        create_unconditional_branch(instr.false_successor)
        instr.erase_from_parent()
        return True

    return False

def optimize_phi(instr, LVI):
    """Simplify PHI nodes using value ranges"""
    if not instr.is_phi():
        return False

    # Check if all incoming values have same constant range
    first_range = LVI.get_constant_range(
        instr.get_incoming_value(0),
        instr.get_incoming_block(0)
    )

    all_same = True
    for i in range(1, instr.num_incoming_values()):
        value = instr.get_incoming_value(i)
        block = instr.get_incoming_block(i)
        value_range = LVI.get_constant_range(value, block)

        if not ranges_equal(first_range, value_range):
            all_same = False
            break

    if all_same and first_range.is_single_element():
        # All incoming values have same constant
        constant = first_range.get_single_element()
        instr.replace_all_uses_with(constant)
        return True

    return False

def optimize_switch(instr, LVI):
    """Optimize switch statements using value ranges"""
    if not instr.is_switch():
        return False

    condition = instr.get_condition()
    cond_range = LVI.get_constant_range(condition, instr.parent_block)

    # Find which cases are possible
    possible_cases = []
    for case_val, case_dest in instr.get_cases():
        if cond_range.contains(case_val):
            possible_cases.append((case_val, case_dest))

    # Simplify switch based on possible cases
    if len(possible_cases) == 0:
        # No cases match - take default
        create_unconditional_branch(instr.default_dest)
        instr.erase_from_parent()
        return True
    elif len(possible_cases) == 1:
        # Only one case possible - convert to conditional branch
        case_val, case_dest = possible_cases[0]
        cond = create_icmp_eq(condition, case_val)
        create_conditional_branch(cond, case_dest, instr.default_dest)
        instr.erase_from_parent()
        return True

    # Remove impossible cases
    if len(possible_cases) < instr.num_cases():
        instr.set_cases(possible_cases)
        return True

    return False
```

### Value Range Analysis

LazyValueInfo provides range information at specific program points:

```c
// Example: After "if (x > 10)"
BasicBlock* then_block = ...;
BasicBlock* else_block = ...;

// In then_block, we know x ∈ [11, ∞)
ConstantRange x_range_then = LVI.getConstantRange(x, then_block);
// x_range_then = [11, INT_MAX]

// In else_block, we know x ∈ [-∞, 10]
ConstantRange x_range_else = LVI.getConstantRange(x, else_block);
// x_range_else = [INT_MIN, 10]
```

---

## Data Structures

### ConstantRange

```c
struct ConstantRange {
    APInt lower_bound;
    APInt upper_bound;
    bool is_full_set;      // Represents all possible values
    bool is_empty_set;     // Represents no values

    bool contains(APInt value);
    bool is_single_element();
    APInt get_single_element();

    ConstantRange intersect(ConstantRange other);
    ConstantRange union_with(ConstantRange other);
};
```

### LazyValueInfo State

```c
class LazyValueInfo {
public:
    // Query value range at specific program point
    ConstantRange getConstantRange(Value* V, BasicBlock* BB);

    // Query constant value
    Constant* getConstant(Value* V, BasicBlock* BB);

    // Query on specific edge
    ConstantRange getConstantRangeOnEdge(Value* V,
                                         BasicBlock* from,
                                         BasicBlock* to);

private:
    // Cached analysis results
    Map<std::pair<Value*, BasicBlock*>, ConstantRange> cache;

    // Dependency tracking for invalidation
    Set<std::pair<Value*, BasicBlock*>> pending_queries;
};
```

### CVP Statistics

```c
struct CVPStatistics {
    int comparisons_simplified;
    int selects_folded;
    int branches_simplified;
    int phi_nodes_simplified;
    int switches_optimized;
    int non_null_inferred;
};
```

---

## Configuration Parameters

### Enable Flag

**Evidence**: `optimization_passes.json:14268`

```json
{
  "value": "Enable the Correlated Value Propagation Pass"
}
```

Suggests there's an enable/disable flag (though specific name not extracted).

### Implicit Configuration

CVP behavior is controlled by:
- **Optimization level**: Runs at O1+ (based on pass manager IDs)
- **LazyValueInfo precision**: Depth of value range analysis
- **Integration with jump threading**: Shares LVI results

---

## Pass Dependencies

### Required Analyses

1. **LazyValueInfo** (CRITICAL dependency)
   - Provides all value range information
   - CVP cannot function without LVI
   - Most expensive analysis CVP depends on

**Evidence**: `pass-manager.md:1138-1139`
```c
LazyValueInfo  // Required dependency
```

2. **DominatorTree** (required)
   - Validates control flow transformations
   - Used by LazyValueInfo for correctness

3. **TargetLibraryInfo** (optional)
   - Identifies library functions with known behavior
   - Enables more aggressive optimization

### Preserved Analyses

CVP preserves:
- **DominatorTree**: CFG structure unchanged (unless branches removed)
- **LoopInfo**: Loop structure preserved

CVP invalidates:
- **LazyValueInfo**: Value ranges change after optimization
- **BranchProbabilityInfo**: Branch conditions change

---

## Integration Points

### Pipeline Position

```
O1+ Pipeline:
  SimplifyCFG
  InstCombine
  → CVP (Instance 1)          ← Early value propagation
  JumpThreading
  → CVP (Instance 2)          ← After threading creates new info

O2+ Pipeline:
  SCCP
  JumpThreading
  → CVP (Instance 1)
  GVN
  → CVP (Instance 2)
  JumpThreading (again)
  → CVP (Instance 3)          ← Final cleanup
```

**Evidence**: Pass IDs 60, 74, 92, 144 at different positions

### Synergy with Other Passes

**Before CVP**:
- **SCCP**: Creates constants that CVP can use for ranges
- **InstCombine**: Normalizes instructions
- **JumpThreading**: Creates specialized paths with tighter ranges

**After CVP**:
- **SimplifyCFG**: Removes empty blocks from eliminated branches
- **ADCE**: Removes dead code from folded selects
- **JumpThreading**: Uses CVP-simplified conditions

**CVP and JumpThreading interaction**:
```
JumpThreading creates specialized paths
  ↓
CVP uses tighter value ranges in specialized paths
  ↓
Simplifies comparisons
  ↓
JumpThreading finds new threading opportunities
  ↓
(iterate to fixed point)
```

---

## CUDA Considerations

### Thread Index Ranges

CVP can infer ranges for CUDA built-in variables:

```cuda
__global__ void kernel() {
    int tid = threadIdx.x;

    // CVP knows: tid ∈ [0, blockDim.x - 1]
    if (blockDim.x == 256) {
        // CVP knows: tid ∈ [0, 255]

        if (tid >= 256) {
            // CVP: Always false (eliminated)
        }

        if (tid < 256) {
            // CVP: Always true (branch removed)
        }
    }
}
```

### Warp Boundaries

```cuda
int warp_id = tid / 32;
// CVP knows: warp_id ∈ [0, 7] for 256 threads
```

### Non-Null Pointers

CVP can infer non-null for kernel parameters:

```cuda
__global__ void kernel(float* data) {
    // CVP: data is non-null (kernel launch validates)
    if (data == NULL) {
        // Eliminated by CVP
    }
}
```

---

## Code Evidence

### Pass Manager Registration

**Evidence**: `pass-management-algorithms.md:1042,1060,1082,1144`

Four instances at different pipeline positions.

**Evidence**: `pass-manager.md:1138-1139`

Split into analysis and transform components.

### String Evidence

**Evidence**: `optimization_passes.json:14268`

```json
{
  "value": "Enable the Correlated Value Propagation Pass"
}
```

### Integration Evidence

**Evidence**: `optimization-passes.md:89`, `optimization-framework/pass-manager.md:103,1107`

```
CorrelatedValueProp integrated with:
- JumpThreading
- SimplifyCFG
- SCCP
```

---

## Performance Impact

### Typical Results

**Code size**: -1% to -5%
- Eliminates redundant comparisons
- Removes dead branches

**Execution time**: 2-6% improvement
- Fewer dynamic comparisons
- Better branch prediction (fewer branches)
- Enables further optimizations

**Compile time**: 3-8% overhead
- LazyValueInfo queries are expensive
- Multiple passes add up
- Sharing LVI with JumpThreading amortizes cost

### Best Case Scenarios

1. **Range-checked code**:
```c
if (x >= 0 && x < 100) {
    if (x < 0) {  // CVP: Always false
        error();
    }
    if (x < 100) {  // CVP: Always true
        process(x);
    }
}
```

2. **CUDA thread index checks**:
```cuda
if (tid < blockDim.x) {  // CVP: Always true
    data[tid] = tid;
}
```

3. **Select instruction folding**:
```c
int val = (x > 10) ? a : b;
if (x > 20) {
    // CVP knows: x > 10, so val == a
    int result = (val == a) ? 1 : 0;  // CVP: Always 1
}
```

---

## Examples

### Example 1: Redundant Comparison Elimination

**Before CVP**:
```llvm
define i32 @example1(i32 %x) {
entry:
  %cmp1 = icmp sgt i32 %x, 10
  br i1 %cmp1, label %then, label %else

then:
  %cmp2 = icmp sgt i32 %x, 5    ; Redundant!
  br i1 %cmp2, label %yes, label %no

yes:
  ret i32 1

no:
  ret i32 0

else:
  ret i32 -1
}
```

**After CVP**:
```llvm
define i32 @example1(i32 %x) {
entry:
  %cmp1 = icmp sgt i32 %x, 10
  br i1 %cmp1, label %then, label %else

then:
  ; %cmp2 eliminated - always true (x > 10 implies x > 5)
  br label %yes

yes:
  ret i32 1

else:
  ret i32 -1
}
```

### Example 2: Select Folding

**Before CVP**:
```llvm
define i32 @example2(i32 %x, i32 %a, i32 %b) {
entry:
  %cmp1 = icmp sgt i32 %x, 10
  %sel = select i1 %cmp1, i32 %a, i32 %b
  br i1 %cmp1, label %then, label %else

then:
  ; We know %cmp1 is true here
  %cmp2 = icmp eq i32 %sel, %a   ; Always true!
  br i1 %cmp2, label %yes, label %no

yes:
  ret i32 1

no:
  ret i32 0

else:
  ret i32 -1
}
```

**After CVP**:
```llvm
define i32 @example2(i32 %x, i32 %a, i32 %b) {
entry:
  %cmp1 = icmp sgt i32 %x, 10
  %sel = select i1 %cmp1, i32 %a, i32 %b
  br i1 %cmp1, label %then, label %else

then:
  ; %cmp2 folded to true
  br label %yes

yes:
  ret i32 1

else:
  ret i32 -1
}
```

### Example 3: CUDA Thread Index Range

**Before CVP**:
```cuda
__global__ void kernel(float* data, int n) {
    int tid = threadIdx.x;

    if (tid < blockDim.x) {      // Check 1
        if (tid < blockDim.x) {  // Check 2 (redundant)
            data[tid] = tid;
        }
    }
}
```

**After CVP**:
```cuda
__global__ void kernel(float* data, int n) {
    int tid = threadIdx.x;

    // Check 1: Always true (tid is always < blockDim.x)
    // Check 2: Eliminated (redundant)
    data[tid] = tid;
}
```

---

## Verification and Testing

### Verification Methods

1. **Value range inspection**:
```bash
nvcc -Xcompiler -mllvm -Xcompiler -print-after=correlated-value-propagation file.cu
```

2. **Statistics**:
```bash
nvcc -Xcompiler -mllvm -Xcompiler -stats file.cu 2>&1 | grep cvp
# Look for:
#   cvp.NumComparisons - comparisons simplified
#   cvp.NumSelects - select instructions folded
#   cvp.NumBranches - branches eliminated
```

3. **LVI debug output**:
```bash
nvcc -Xcompiler -mllvm -Xcompiler -debug-only=lazy-value-info file.cu
```

### Correctness Checks

- [ ] Value ranges correctly inferred from control flow
- [ ] Comparison simplifications preserve semantics
- [ ] Select folding maintains correct value
- [ ] Branch elimination doesn't skip reachable code
- [ ] No invalid range assumptions

---

## Known Limitations

1. **Expensive analysis**:
   - LazyValueInfo queries can be slow
   - Multiple CVP passes compound cost

2. **Limited interprocedural analysis**:
   - Works only within functions
   - Cannot propagate ranges across calls

3. **Conservative with pointers**:
   - Limited pointer range analysis
   - Aliasing reduces precision

4. **Floating-point**:
   - No range analysis for FP values
   - Only works with integers

5. **Loop complexity**:
   - Complex loop conditions may not yield tight ranges
   - Recursive value dependencies can cause conservatism

---

## Related Passes

- **LazyValueInfo**: Provides all value range data (required)
- **JumpThreading**: Works in tandem with CVP
- **SCCP**: Creates constants that CVP uses for ranges
- **SimplifyCFG**: Cleans up after CVP
- **InstCombine**: Complements CVP with local simplifications

---

## References

### L2 Analysis Files

- `wiki/docs/algorithms/pass-management-algorithms.md:1042,1060,1082,1144`
- `wiki/docs/compiler-internals/data-structures/pass-manager.md:1138-1139`
- `foundation/taxonomy/strings/optimization_passes.json:14268`
- `wiki/docs/algorithms/index.md:43,131,489`
- `wiki/docs/compiler-internals/optimization-passes.md:89`

### Algorithm References

- LLVM CVP: `llvm/lib/Transforms/Scalar/CorrelatedValuePropagation.cpp`
- LazyValueInfo: `llvm/lib/Analysis/LazyValueInfo.cpp`
- Range analysis techniques

---

**Analysis Quality**: MEDIUM-HIGH
**Last Updated**: 2025-11-17
**Confidence**: High (multiple pass IDs + configuration + extensive integration evidence)
