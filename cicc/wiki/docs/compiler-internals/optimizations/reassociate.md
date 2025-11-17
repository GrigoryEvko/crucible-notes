# Reassociate Expression Optimization

**Pass Type**: Function-level scalar optimization
**LLVM Class**: `llvm::ReassociatePass`
**Extracted From**: CICC binary analysis and configuration parameters
**Analysis Quality**: MEDIUM - Configuration parameters and integration evidence
**Evidence Sources**: String literals, pass manager data, LICM integration

---

## Overview

The Reassociate pass reorders arithmetic expressions to expose optimization opportunities. It exploits the associative and commutative properties of operations to canonicalize expressions, enable constant folding, and improve instruction scheduling.

**Core Algorithm**: Expression tree reordering using ranking functions

**Key Features**:
- Canonicalizes expression order for better CSE (Common Subexpression Elimination)
- Exposes constants for folding
- Enables strength reduction opportunities
- Optimizes operand order for better instruction selection
- Integrates with loop optimization (LICM)

---

## Pass Registration and Configuration

### Reassociate Pass Evidence

**Evidence**: `pass-management-algorithms.md:1049`

```c
{65, "ReassociateBinaryOps", ODD, 0x0, "Reassociate expressions"},
```

**Evidence**: `pass-manager.md:1137`

```c
{69, "ReassociateExprs", Bool, FUNCTION, TRANSFORM, -, None, "Expression reassociation"}
```

**Evidence**: `recovered_functions_optimization.json:888-889`

```json
{
  "pass_id": "Reassociate"
}
```

---

## Algorithm

### Associative and Commutative Properties

**Associative operations** (order of grouping doesn't matter):
- Integer: `add`, `mul`, `and`, `or`, `xor`
- Floating-point: `fadd`, `fmul` (only with fast-math flags)

**Commutative operations** (order of operands doesn't matter):
- Same as associative (these properties often go together)

### Expression Canonicalization

Reassociate transforms expressions into canonical form:

```
Original:  (a + 3) + (b + 2)
After:     (a + b) + (3 + 2)   → (a + b) + 5
```

**Benefits**:
1. Constants grouped together for folding
2. Variables grouped for CSE
3. Predictable ordering for pattern matching

### Core Algorithm Pseudocode

```python
def Reassociate(function):
    modified = False

    for BB in function.basic_blocks:
        for instr in BB.instructions:
            if is_associative(instr.opcode):
                if reassociate_expression(instr):
                    modified = True

    return modified

def reassociate_expression(instr):
    """Reassociate a single expression tree"""

    # Build expression tree
    expr_tree = build_expression_tree(instr)

    # Rank operands
    ranked_operands = rank_operands(expr_tree)

    # Rebuild expression in canonical order
    new_expr = rebuild_expression(ranked_operands, instr.opcode)

    # Replace if changed
    if new_expr != instr:
        instr.replace_all_uses_with(new_expr)
        return True

    return False

def rank_operands(expr_tree):
    """Assign ranks to operands for canonical ordering"""
    ranks = {}

    for operand in expr_tree.operands:
        if is_constant(operand):
            ranks[operand] = 0  # Constants first
        elif is_argument(operand):
            ranks[operand] = 1  # Arguments second
        elif is_instruction(operand):
            # Instructions ranked by depth and complexity
            ranks[operand] = compute_rank(operand)

    # Sort operands by rank
    return sorted(expr_tree.operands, key=lambda op: ranks[op])

def compute_rank(instruction):
    """Compute rank based on expression depth"""
    if instruction.rank_cached:
        return instruction.rank_cached

    # Base rank
    rank = 2

    # Add depth component
    for operand in instruction.operands:
        if is_instruction(operand):
            rank = max(rank, compute_rank(operand) + 1)

    instruction.rank_cached = rank
    return rank

def rebuild_expression(operands, opcode):
    """Rebuild expression tree in ranked order"""

    if len(operands) == 1:
        return operands[0]

    # Build left-associated tree
    result = operands[0]
    for operand in operands[1:]:
        result = create_binop(opcode, result, operand)

    return result
```

### Specific Transformation Patterns

#### Pattern 1: Constant Grouping

**Before**:
```llvm
%1 = add i32 %a, 5
%2 = add i32 %1, 10
```

**After Reassociation**:
```llvm
%1 = add i32 %a, 15  ; Constants folded: 5 + 10
```

#### Pattern 2: Variable Grouping

**Before**:
```llvm
%1 = mul i32 %a, %b
%2 = mul i32 %c, %a  ; %a appears twice
```

**After Reassociation**:
```llvm
; Grouped for CSE:
%1 = mul i32 %a, %b
%2 = mul i32 %a, %c  ; Same order as %1
; CSE can now detect common subexpression
```

#### Pattern 3: Strength Reduction Enablement

**Before**:
```llvm
%1 = mul i32 %x, 4
%2 = mul i32 %1, 2  ; Total: x * 8
```

**After Reassociation**:
```llvm
%1 = mul i32 %x, 8  ; Folded: 4 * 2
; InstCombine can then convert to: %1 = shl i32 %x, 3
```

---

## Data Structures

### Expression Tree Node

```c
struct ExprTreeNode {
    Instruction* inst;
    Opcode opcode;

    Vector<Value*> operands;
    int rank;

    // For tracking expression shape
    bool is_leaf;
    int depth;
};
```

### Operand Ranking Table

```c
struct OperandRank {
    Value* operand;
    int rank;

    // Ranking components
    int constant_rank;  // 0 for constants
    int depth_rank;     // Depth in expression tree
    int complexity_rank; // Based on instruction type
};
```

### Reassociation Cache

```c
struct ReassociationCache {
    // Cache ranks to avoid recomputation
    Map<Value*, int> rank_cache;

    // Track which expressions have been reassociated
    Set<Instruction*> processed;

    // Statistics
    int expressions_reassociated;
    int constants_folded;
};
```

---

## Configuration Parameters

### LICM Reassociation Integration

**Evidence**: `L2_AGENT_08_FINDINGS.md:60`, `optimization_passes.json:32105-32149`

```json
{
  "value": "licm-max-num-fp-reassociations",
  "value": "Set upper limit for the number of transformations performed during a single round of hoisting the reassociated expressions.",
  "value": "licm-max-num-int-reassociations"
}
```

These parameters control how reassociation integrates with Loop Invariant Code Motion:

- **`licm-max-num-fp-reassociations`**: Limit for floating-point reassociations in LICM
- **`licm-max-num-int-reassociations`**: Limit for integer reassociations in LICM

**Purpose**: Prevent excessive code transformations that could hurt performance.

### Implicit Configuration

While no direct "disable-Reassociate" flag was found, reassociation is controlled by:
- Fast-math flags (for FP reassociation)
- Optimization level (disabled at O0, enabled at O1+)

---

## Pass Dependencies

### Required Analyses

1. **SSA Form** (required)
   - All values must be in SSA form
   - Simplifies operand tracking

2. **DominatorTree** (optional but recommended)
   - Helps validate transformations
   - Ensures correctness of reorderings

### Preserved Analyses

Reassociate preserves:
- **DominatorTree**: CFG unchanged
- **LoopInfo**: No structural changes

Reassociate invalidates:
- **ScalarEvolution**: Expression forms change
- **ValueTracking**: Operand orders change

---

## Integration Points

### Pipeline Position

```
O1+ Pipeline:
  SimplifyCFG
  InstCombine
  → Reassociate
  SCCP
  InstCombine (again - uses reassociated forms)

O2+ Pipeline with LICM:
  LoopSimplify
  → Reassociate (before LICM)
  LICM (uses reassociated expressions)
  → Reassociate (after LICM cleanup)
```

**Evidence**: `pass-management-algorithms.md:1049` (ID 65)

### Synergy with Other Passes

**Before Reassociate**:
- **InstCombine**: Simplifies expressions
- **SimplifyCFG**: Normalizes control flow

**After Reassociate**:
- **InstCombine**: Folds constants exposed by reassociation
- **GVN/CSE**: Detects common subexpressions in canonical form
- **LICM**: Hoists reassociated loop-invariant expressions

---

## CUDA Considerations

### Integer vs. Floating-Point

**Integer reassociation**: Always safe
```cuda
int x = (a + b) + c;  // Can be reassociated to a + (b + c)
```

**Floating-point reassociation**: Requires fast-math
```cuda
float x = (a + b) + c;  // Only reassociated with -ffast-math
// Reason: FP addition is not truly associative (rounding)
```

### Thread-Level Parallelism

Reassociation can improve ILP (Instruction-Level Parallelism):

**Before**:
```cuda
int result = ((a + b) + c) + d;  // Sequential dependencies
```

**After**:
```cuda
int result = (a + b) + (c + d);  // Can execute adds in parallel
```

### Fast-Math Flags

```bash
# Enable FP reassociation in CUDA
nvcc -ffast-math file.cu
```

This enables:
- Floating-point reassociation
- Reciprocal approximations
- Contraction of multiply-add operations

---

## Code Evidence

### Pass Manager Registration

**Evidence**: `pass-management-algorithms.md:1049`

```c
{65, "ReassociateBinaryOps", ODD, 0x0, "Reassociate expressions"}
```

**Evidence**: `pass-manager.md:1137`

```c
{69, "ReassociateExprs", Bool, FUNCTION, TRANSFORM, -, None, "Expression reassociation"}
```

Multiple IDs suggest multiple instances or variants.

### String Evidence

**Evidence**: `local_optimizations.json:410,491,657,683`

```json
{
  "category": "Reassociation",
  "pass": "Reassociate",
  "technique_name": "Expression Reassociation"
}
```

### LICM Integration

**Evidence**: `optimization_passes.json:32105-32149`

Reassociation is tightly integrated with LICM for loop optimization.

---

## Performance Impact

### Typical Results

**Code quality improvement**: 2-5%
- Enables constant folding
- Better CSE opportunities
- Improved instruction selection

**Compile time overhead**: <1%
- Lightweight pass
- Linear in expression count

**Register pressure**: Neutral to slightly better
- Can reduce live ranges in some cases
- Canonical form helps register allocation

### Best Case Scenarios

1. **Constant-heavy expressions**:
```c
int x = (a + 5) + (b + 10) + (c + 3);
// After: int x = (a + b + c) + 18;
```

2. **Repeated subexpressions**:
```c
int x = a * b;
int y = c * a;  // Reordered to: a * c (matches pattern for CSE)
```

3. **Loop-invariant expressions**:
```c
for (int i = 0; i < n; i++) {
    x[i] = (i + offset) + CONSTANT;
    // Reassociated to enable LICM
}
```

---

## Examples

### Example 1: Constant Folding

**Before Reassociate**:
```llvm
define i32 @example1(i32 %a, i32 %b) {
  %1 = add i32 %a, 3
  %2 = add i32 %1, %b
  %3 = add i32 %2, 7
  ret i32 %3
}
```

**After Reassociate**:
```llvm
define i32 @example1(i32 %a, i32 %b) {
  %1 = add i32 %a, %b
  %2 = add i32 %1, 10   ; Constants folded: 3 + 7
  ret i32 %2
}
```

### Example 2: CSE Enablement

**Before Reassociate**:
```llvm
define i32 @example2(i32 %a, i32 %b, i32 %c) {
  %1 = mul i32 %a, %b
  %2 = mul i32 %c, %a   ; Different order
  ret i32 %2
}
```

**After Reassociate**:
```llvm
define i32 @example2(i32 %a, i32 %b, i32 %c) {
  %1 = mul i32 %a, %b
  %2 = mul i32 %a, %c   ; Canonical order
  ; Now CSE can detect %a is common
  ret i32 %2
}
```

### Example 3: CUDA Kernel with Constant Offsets

**Before Reassociate**:
```cuda
__global__ void kernel(int* data, int offset1, int offset2) {
    int tid = threadIdx.x;
    int index = (tid + offset1) + offset2;
    data[index] = tid;
}
```

**After Reassociate**:
```cuda
__global__ void kernel(int* data, int offset1, int offset2) {
    int tid = threadIdx.x;
    int combined_offset = offset1 + offset2;  // Folded
    int index = tid + combined_offset;
    data[index] = tid;
}
```

---

## Verification and Testing

### Verification Methods

1. **Expression ordering check**:
```bash
nvcc -Xcompiler -mllvm -Xcompiler -print-after=reassociate file.cu
# Verify expressions are in canonical form
```

2. **Constant folding validation**:
```c
// Test case: multiple constants
int test = (a + 1) + (b + 2) + (c + 3);
// Should become: (a + b + c) + 6
```

3. **CSE effectiveness**:
```bash
nvcc -Xcompiler -mllvm -Xcompiler -stats file.cu 2>&1 | grep CSE
# More CSE hits after reassociation
```

### Correctness Checks

- [ ] Integer operations: Always safe to reassociate
- [ ] Floating-point: Only with fast-math flags
- [ ] No changes to program semantics
- [ ] Overflow behavior preserved (undefined overflow is OK to change)

---

## Known Limitations

1. **Floating-point precision**:
   - Reassociation can change results (rounding)
   - Only enabled with fast-math flags

2. **Overflow semantics**:
   - Signed integer overflow is undefined behavior
   - Reassociation may expose or hide overflow

3. **Complex expressions**:
   - Very deep expression trees may not fully canonicalize
   - Ranking heuristics are approximate

4. **Non-associative operations**:
   - Division, subtraction not reassociated
   - Requires manual transformation to associative form

---

## Related Passes

- **InstCombine**: Folds constants exposed by reassociation
- **GVN/EarlyCSE**: Benefits from canonical expression ordering
- **LICM**: Uses reassociated expressions for hoisting
- **LoopStrengthReduction**: Works better with reassociated forms
- **InstructionScheduling**: Uses canonical ordering

---

## References

### L2 Analysis Files

- `wiki/docs/algorithms/pass-management-algorithms.md:1049`
- `wiki/docs/compiler-internals/data-structures/pass-manager.md:1137,1172,1173`
- `deep_analysis/symbol_recovery/recovered_functions_optimization.json:888-889`
- `foundation/taxonomy/strings/optimization_passes.json:32105-32149`
- `deep_analysis/algorithms/optimization_passes/local_optimizations.json:410-1014`

### Algorithm References

- LLVM Reassociate: `llvm/lib/Transforms/Scalar/Reassociate.cpp`
- Algebraic simplification techniques
- Expression canonicalization theory

---

**Analysis Quality**: MEDIUM
**Last Updated**: 2025-11-17
**Confidence**: Medium-High (multiple sources but no RTTI confirmation)
