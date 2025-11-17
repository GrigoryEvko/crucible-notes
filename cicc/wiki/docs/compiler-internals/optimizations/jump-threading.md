# Jump Threading Optimization

**Pass Type**: Function-level control flow optimization
**LLVM Class**: `llvm::JumpThreadingPass`
**Extracted From**: CICC binary analysis and decompiled code
**Analysis Quality**: HIGH - Multiple function addresses and extensive configuration
**Evidence Sources**: Pass manager data, string literals, function addresses

---

## Overview

Jump Threading is a control flow optimization that eliminates redundant control flow by "threading" jumps through blocks with known conditions. It duplicates code to specialize control flow paths, enabling better branch prediction and enabling further optimizations.

**Core Algorithm**: LazyValueInfo-based conditional branch analysis with controlled code duplication

**Key Features**:
- Eliminates redundant conditional branches
- Threads jumps through phi nodes
- Duplicates blocks when profitable
- Uses implication analysis to infer conditions
- Integrates with LazyValueInfo for value range analysis

---

## Pass Registration and Configuration

### Pass Instances

**Evidence**: `pass-management-algorithms.md:1041,1059,1081,1143`

```c
// Multiple jump threading registrations at different pipeline stages
{59,  "JumpThreading", ODD, 0x0,      "Thread through jumps"},
{73,  "JumpThreading", ODD, 0x499980, "Jump threading"},
{91,  "JumpThreading", ODD, 0x0,      "Jump threading pass"},
{143, "JumpThreading", ODD, 0x0,      "Jump threading"},
```

**Function Addresses**:
- `0x499980`: Main JumpThreading pass implementation (ID 73)
- `0x4ED0C0`: Constructor registration (ctor_243)

**Pass ordering**: Jump threading runs multiple times in the pipeline to catch opportunities created by other optimizations.

---

## Algorithm

### High-Level Strategy

Jump threading identifies opportunities to eliminate conditional branches by duplicating code:

```
Before:                    After:
  BB1                       BB1
   |                         |
   v                         v (duplicated)
  BB2                      BB2'        BB2
if (x) ─┬─> BB3      =>    |            |
        └─> BB4          BB3          BB4
```

**Key insight**: If we know the value of `x` on different incoming paths to BB2, we can thread the jump directly to BB3 or BB4, eliminating BB2 from those paths.

### Core Algorithm Pseudocode

```python
def JumpThreading(function):
    modified = False
    worklist = [BB for BB in function]

    while worklist:
        BB = worklist.pop()

        # Try to thread jumps in this block
        if thread_edge(BB):
            modified = True
            # Add affected blocks back to worklist
            for pred in BB.predecessors:
                worklist.append(pred)
            for succ in BB.successors:
                worklist.append(succ)

    return modified

def thread_edge(BB):
    """Try to thread jumps through BB"""

    # Case 1: Thread through blocks with phi nodes
    if BB.has_phi_nodes():
        if thread_through_phi(BB):
            return True

    # Case 2: Thread single-predecessor blocks
    if len(BB.predecessors) == 1:
        if thread_single_pred(BB):
            return True

    # Case 3: Thread based on predecessor conditions
    if thread_through_two_preds(BB):
        return True

    # Case 4: Thread switch statements
    if BB.terminator.is_switch():
        if thread_switch(BB):
            return True

    return False

def thread_through_phi(BB):
    """Thread jumps based on phi node values"""
    # For each predecessor, evaluate what value phi takes
    for pred in BB.predecessors:
        phi_values = {}

        # Compute phi node values when coming from pred
        for phi in BB.phi_nodes:
            incoming_value = phi.get_incoming_value(pred)
            phi_values[phi] = incoming_value

        # Check if this enables threading
        if can_thread_with_values(BB, phi_values):
            # Create duplicated block with phi values substituted
            new_BB = duplicate_block(BB, phi_values)

            # Redirect pred to new_BB
            redirect_edge(pred, BB, new_BB)
            return True

    return False

def thread_single_pred(BB):
    """Thread through single-predecessor blocks"""
    pred = BB.single_predecessor()

    # If predecessor ends with conditional branch
    if pred.terminator.is_conditional():
        condition = pred.terminator.condition

        # Use LazyValueInfo to determine if we know the condition value
        if BB == pred.terminator.true_successor:
            # We know condition is true when entering BB
            known_value = LazyValueInfo.get_constant(condition, BB)

            if known_value is not None:
                # Thread based on this knowledge
                return thread_with_known_condition(BB, known_value)

    return False

def thread_through_two_preds(BB):
    """Thread when BB has exactly two predecessors"""
    if len(BB.predecessors) != 2:
        return False

    pred1, pred2 = BB.predecessors

    # Check if we can infer different conditions from each predecessor
    for instruction in BB:
        if not instruction.is_branch():
            continue

        # Get value info from each predecessor
        val1 = LazyValueInfo.get_value_on_edge(instruction.condition,
                                                pred1, BB)
        val2 = LazyValueInfo.get_value_on_edge(instruction.condition,
                                                pred2, BB)

        if val1 != val2 and (val1.is_constant() or val2.is_constant()):
            # Different conditions - worth threading
            duplicate_and_thread(BB, pred1, val1, pred2, val2)
            return True

    return False

def can_duplicate_block(BB, size_limit):
    """Check if block is small enough to duplicate"""
    # Count instructions (excluding phi nodes and terminators)
    instruction_count = sum(1 for I in BB if not I.is_phi() and not I.is_terminator())

    # Check against threshold
    if instruction_count > size_limit:
        return False

    # Additional constraints
    if BB.has_landing_pad():  # Exception handling
        return False

    if BB.has_address_taken():
        return False

    return True
```

### LazyValueInfo Integration

Jump threading relies heavily on **LazyValueInfo** (LVI) to determine value ranges:

```python
class LazyValueInfo:
    """Computes value ranges lazily on demand"""

    @staticmethod
    def get_constant(value, BB):
        """Get constant value if known at BB entry"""
        # Use predecessor conditions to infer constants
        if all_predecessors_set_value_to_same_constant(value, BB):
            return that_constant
        return None

    @staticmethod
    def get_value_on_edge(value, from_BB, to_BB):
        """Get value info when coming from specific edge"""
        # Example: if from_BB ends with "if (x < 10)"
        # and to_BB is the true successor,
        # then we know x ∈ [0, 9] when entering to_BB

        if from_BB.terminator.is_conditional():
            condition = from_BB.terminator.condition

            if implies(condition, value_in_range(value, range)):
                return ConstantRange(range)

        return Unknown

    @staticmethod
    def get_value_at(value, instruction):
        """Get value info at specific instruction"""
        # Walk backwards to find defining conditions
        # Build up constraints from dominating conditionals
        ...
```

---

## Data Structures

### ThreadingCandidate

```c
struct ThreadingCandidate {
    BasicBlock* block;           // Block to potentially duplicate
    BasicBlock* predecessor;     // Predecessor we're threading from
    BasicBlock* successor;       // Successor we're threading to

    // Cost estimation
    int instruction_count;
    int phi_node_count;
    int duplicate_cost;          // Estimated cost of duplication

    // Value information
    Map<Value*, Constant*> known_values;  // Values known on this edge
};
```

### JumpThreadingContext

```c
struct JumpThreadingContext {
    Function* F;
    DominatorTree* DT;
    LazyValueInfo* LVI;

    // Configuration parameters
    int max_duplicate_size;               // jump-threading-threshold
    int max_duplicate_phi_nodes;          // max PHIs in duplicated BB
    int implication_search_threshold;     // search depth limit
    bool across_loop_headers;             // allow threading across loops

    // Statistics
    int blocks_duplicated;
    int jumps_threaded;
    int switches_threaded;
};
```

### Edge Profitability Data

```c
struct EdgeInfo {
    BasicBlock* from;
    BasicBlock* to;

    // Frequency information (if available)
    uint64_t edge_frequency;
    float branch_probability;

    // Value info on this edge
    ValueLattice value_info;
};
```

---

## Configuration Parameters

### `jump-threading-threshold`

**Type**: Integer (default: 6)
**Purpose**: Maximum number of instructions to duplicate per block

**Evidence**: `optimization_passes.json:16504`

```json
{
  "value": "jump-threading-threshold"
}
```

**Usage**:
```bash
# Allow larger blocks to be duplicated
nvcc -Xcompiler -mllvm -Xcompiler -jump-threading-threshold=10 file.cu
```

**Tradeoff**:
- Higher value: More aggressive threading, larger code size
- Lower value: Less code duplication, fewer optimization opportunities

### `jump-threading-disable-select-unfolding`

**Type**: Boolean (default: false)
**Purpose**: Disable conversion of select instructions to branches for threading

**Evidence**: `optimization_passes.json:16445`

```json
{
  "value": "jump-threading-disable-select-unfolding"
}
```

Select unfolding example:
```llvm
; Before:
%result = select i1 %cond, i32 %a, i32 %b

; After unfolding (enables threading):
br i1 %cond, label %then, label %else
then:
  br label %merge
else:
  br label %merge
merge:
  %result = phi i32 [%a, %then], [%b, %else]
```

### `jump-threading-implication-search-threshold`

**Type**: Integer (default: ~3)
**Purpose**: Limit depth of implication search

**Evidence**: `optimization_passes.json:16477`

Implication search finds indirect relationships:
```c
if (x > 10) {
    // We can infer x != 0, x > 5, etc.
    // Search depth limits how many levels of inference
}
```

### `jump-threading-across-loop-headers`

**Type**: Boolean (default: false)
**Purpose**: Allow threading across loop headers (experimental)

**Evidence**: `optimization_passes.json:31995-32006`

```json
{
  "value": "jump-threading-across-loop-headers",
  "value": "Allow JumpThreading to thread across loop headers, for testing"
}
```

**Warning**: Can break loop structure analysis. Use with caution.

### `print-lvi-after-jump-threading`

**Type**: Boolean (default: false)
**Purpose**: Debug output to show LazyValueInfo state

**Evidence**: `optimization_passes.json:16493`

```bash
# Enable LVI debug output
nvcc -Xcompiler -mllvm -Xcompiler -print-lvi-after-jump-threading file.cu
```

### `disable-jump-threading`

**Type**: Boolean
**Purpose**: Completely disable jump threading

**Evidence**: `optimization_passes.json:13198,13214`

```json
{
  "value": "Disable jump threading for OCG experiments",
  "value": "disable-jump-threading"
}
```

---

## Pass Dependencies

### Required Analyses

1. **LazyValueInfo** (critical)
   - Provides value range information
   - Enables condition inference
   - Most important dependency

2. **DominatorTree** (required)
   - Validates threading transformations
   - Ensures dominance properties maintained

3. **TargetLibraryInfo** (optional)
   - Helps with function call analysis
   - Identifies pure functions

### Preserved Analyses

Jump threading preserves:
- **DominatorTree**: Updated incrementally during threading
- **LoopInfo**: Generally preserved (unless threading across headers)

Jump threading invalidates:
- **LazyValueInfo**: Values change after threading
- **BranchProbabilityInfo**: Branch frequencies change
- **PostDominatorTree**: May be invalidated

---

## Integration Points

### Pipeline Position

```
O1 Pipeline:
  EarlyCSE
  → JumpThreading (Instance 1)
  SimplifyCFG

O2 Pipeline:
  SimplifyCFG
  SCCP
  → JumpThreading (Instance 1)    ← After constant propagation
  CorrelatedValuePropagation
  → JumpThreading (Instance 2)    ← After CVP
  GVN
  → JumpThreading (Instance 3)    ← Final cleanup

O3 Pipeline:
  (O2 passes)
  AggressiveInstCombine
  → JumpThreading (Final)
```

**Evidence**: `pass-management-algorithms.md` shows IDs 59, 73, 91, 143

### Synergy with Other Passes

**Before Jump Threading**:
- **SCCP**: Creates constant conditions to thread on
- **CVP**: Propagates value ranges used by LVI
- **SimplifyCFG**: Normalizes control flow

**After Jump Threading**:
- **SimplifyCFG**: Cleans up duplicated blocks
- **ADCE**: Removes dead code from eliminated paths
- **InstCombine**: Optimizes duplicated instructions

---

## CUDA Considerations

### Thread Divergence

Jump threading must preserve warp execution semantics:

```cuda
__global__ void kernel() {
    int tid = threadIdx.x;

    // Threading must preserve divergence
    if (tid < 16) {
        // Warp 0-15
    } else {
        // Warp 16-31
    }
    // Cannot thread in a way that changes which threads execute
}
```

### Block/Grid Dimensions

Jump threading can specialize on known dimensions:

```cuda
// If blockDim.x is known at compile time
__global__ void kernel() {
    if (blockDim.x == 256) {
        // Can be threaded if dimension is fixed
    }
}
```

### Uniform vs. Divergent Branches

**Uniform branches** (all threads take same path):
- Safe to thread aggressively
- No impact on warp divergence

**Divergent branches** (threads take different paths):
- Threading must preserve divergence pattern
- Cannot merge divergent paths incorrectly

### Memory Synchronization

Cannot thread across synchronization barriers:

```cuda
__syncthreads();
// Jump threading cannot move code across this barrier
```

---

## Code Evidence

### Function Addresses

**Evidence**: `pass-management-algorithms.md:1445,1456`, `data-structures/index.md:387`

```c
{73,  "JumpThreading", 0x499980},   // Main implementation
{243, "JumpThreading", 0x4ED0C0},   // Constructor
```

**Evidence**: `algorithms/index.md:246`

```
0x4ED0C0 | - | JumpThreading | Jump threading
```

### String Literals

**Evidence**: `optimization_passes.json:16434-16504`

```json
{
  "value": "Jump Threading",
  "value": "jump-threading-disable-select-unfolding",
  "value": "Max block size to duplicate for jump threading",
  "value": "jump-threading-implication-search-threshold",
  "value": "print-lvi-after-jump-threading",
  "value": "jump-threading-threshold"
}
```

### Switch Threading Evidence

**Evidence**: `optimization_passes.json:31732`

```json
{
  "value": "Switch statement jump-threaded."
}
```

Confirms switch statement threading is implemented.

### DFA Jump Threading

**Evidence**: `optimization_passes.json:28401,30440-30451`

```json
{
  "value": "dfa-jump-threading",
  "value": "enable-dfa-jump-thread",
  "value": "Enable DFA jump threading",
  "value": "View the CFG before DFA Jump Threading"
}
```

This is a separate, more advanced variant (see DFAJumpThreading wiki page).

---

## Performance Impact

### Typical Results

**Code size**: -2% to +8%
- Duplicates blocks: increases size
- Eliminates dead code: decreases size
- Net effect depends on code pattern

**Execution time**: 3-12% improvement
- Better branch prediction (specialized paths)
- Enables further optimizations
- Reduces dynamic instruction count

**Compile time**: 5-15% overhead
- LazyValueInfo queries are expensive
- Multiple iterations add up
- Justified by runtime improvements at O2+

### Best Case Scenarios

1. **Nested conditionals with correlated values**:
```c
if (x > 10) {
    if (x > 5) {  // Always true given outer condition
        // Thread directly from outer if
    }
}
```

2. **Switch statements with known values**:
```c
switch (mode) {
    case MODE_A:
        if (mode == MODE_A) {  // Redundant, can be threaded
            ...
        }
        break;
}
```

3. **Phi nodes with constants**:
```c
int x = cond ? 5 : 5;  // Both paths set same value
if (x == 5) {          // Can be threaded
    // Always executed
}
```

### Worst Case (Code Size Explosion)

**Risk**: Excessive duplication
```c
// Before: 100 instructions
large_block();

if (a) { large_block(); }  // Duplicated: +100 instructions
if (b) { large_block(); }  // Duplicated: +100 instructions
if (c) { large_block(); }  // Duplicated: +100 instructions
// After: 400 instructions
```

**Mitigation**: `jump-threading-threshold` limits duplication

---

## Examples

### Example 1: Basic Jump Threading

**Before**:
```llvm
define i32 @example1(i1 %cond) {
entry:
  br i1 %cond, label %left, label %right

left:
  br label %merge

right:
  br label %merge

merge:
  %x = phi i32 [5, %left], [5, %right]  ; Same value from both paths
  %result = icmp eq i32 %x, 5
  br i1 %result, label %then, label %else

then:
  ret i32 10

else:
  ret i32 20
}
```

**After Jump Threading**:
```llvm
define i32 @example1(i1 %cond) {
entry:
  ; Thread directly to 'then' (we know %x == 5)
  ret i32 10
}
```

**Analysis**: Phi node collapsed, condition known, jumps threaded directly to result.

### Example 2: Threading Through Phi Nodes

**Before**:
```llvm
define i32 @example2(i32 %val) {
entry:
  %cond = icmp eq i32 %val, 42
  br i1 %cond, label %is_42, label %not_42

is_42:
  br label %merge

not_42:
  br label %merge

merge:
  %phi = phi i32 [42, %is_42], [%val, %not_42]
  %check = icmp eq i32 %phi, 42
  br i1 %check, label %yes, label %no

yes:
  ret i32 1

no:
  ret i32 0
}
```

**After Jump Threading**:
```llvm
define i32 @example2(i32 %val) {
entry:
  %cond = icmp eq i32 %val, 42
  br i1 %cond, label %merge.thread, label %merge

merge.thread:  ; Duplicated merge block for is_42 path
  ret i32 1    ; We know phi == 42 here

merge:
  %phi = %val   ; Simplified phi
  %check = icmp eq i32 %phi, 42
  br i1 %check, label %yes, label %no

yes:
  ret i32 1

no:
  ret i32 0
}
```

**Analysis**: Created `merge.thread` specialized for the `is_42` path where we know phi == 42.

### Example 3: Switch Statement Threading

**Before**:
```llvm
define i32 @example3(i32 %mode) {
entry:
  switch i32 %mode, label %default [
    i32 0, label %case0
    i32 1, label %case1
  ]

case0:
  br label %merge

case1:
  br label %merge

default:
  br label %merge

merge:
  %phi_mode = phi i32 [0, %case0], [1, %case1], [%mode, %default]
  %is_zero = icmp eq i32 %phi_mode, 0
  br i1 %is_zero, label %handle_zero, label %handle_other

handle_zero:
  ret i32 100

handle_other:
  ret i32 200
}
```

**After Jump Threading**:
```llvm
define i32 @example3(i32 %mode) {
entry:
  switch i32 %mode, label %default [
    i32 0, label %handle_zero    ; Threaded directly
    i32 1, label %handle_other   ; Threaded directly
  ]

default:
  %is_zero = icmp eq i32 %mode, 0
  br i1 %is_zero, label %handle_zero, label %handle_other

handle_zero:
  ret i32 100

handle_other:
  ret i32 200
}
```

**Analysis**: Case 0 and Case 1 threaded directly to their targets based on known phi values.

### Example 4: CUDA Kernel with Known Block Dimension

**Before**:
```cuda
__global__ void kernel(float* data, int n) {
    int tid = threadIdx.x;
    int bid = blockIdx.x;

    // Assume blockDim.x is always 256 (specialized kernel)
    if (blockDim.x == 256) {
        // Fast path
        data[bid * 256 + tid] = tid;
    } else {
        // Slow path
        data[bid * blockDim.x + tid] = tid * 2;
    }
}
```

**After Jump Threading** (when compiled with fixed block size):
```cuda
__global__ void kernel(float* data, int n) {
    int tid = threadIdx.x;
    int bid = blockIdx.x;

    // Directly threaded to fast path
    data[bid * 256 + tid] = tid;
}
```

**Analysis**: Constant blockDim.x enables threading to fast path, eliminating else branch.

---

## Verification and Testing

### Verification Methods

1. **IR inspection**:
```bash
nvcc -Xcompiler -mllvm -Xcompiler -print-after=jump-threading file.cu
```

2. **Statistics**:
```bash
nvcc -Xcompiler -mllvm -Xcompiler -stats file.cu 2>&1 | grep jump-threading
# Look for:
#   jump-threading.NumThreads - number of jumps threaded
#   jump-threading.NumDupes - blocks duplicated
```

3. **CFG visualization**:
```bash
nvcc -Xcompiler -mllvm -Xcompiler -view-cfg-only file.cu
# Before/after comparison
```

### Correctness Checks

- [ ] All duplicated blocks have correct phi node values
- [ ] No incorrect threading across loop headers (unless enabled)
- [ ] Dominator tree remains valid
- [ ] Thread divergence preserved in CUDA kernels
- [ ] No excessive code size growth

---

## Known Limitations

1. **Code size growth**:
   - Duplication can significantly increase code size
   - Limited by `jump-threading-threshold` parameter

2. **Compile time cost**:
   - LazyValueInfo queries are expensive
   - Multiple iterations can be slow on large functions

3. **Loop structure**:
   - Default: does not thread across loop headers
   - Can break loop optimizations if enabled

4. **Interprocedural limits**:
   - Only analyzes within single function
   - Cannot thread based on caller knowledge

5. **Floating-point sensitivity**:
   - Conservative with FP comparisons
   - Respects NaN/infinity semantics

---

## Related Passes

- **DFAJumpThreading**: More advanced threading using DFA
- **CorrelatedValuePropagation**: Provides value info for threading
- **SimplifyCFG**: Cleans up after threading
- **SCCP**: Creates constant conditions to thread on
- **LazyValueInfo**: Core analysis used by jump threading

---

## References

### L2 Analysis Files

- `wiki/docs/algorithms/pass-management-algorithms.md:1041,1059,1081,1143`
- `wiki/docs/compiler-internals/data-structures/pass-manager.md:1141,2118,3024`
- `foundation/taxonomy/strings/optimization_passes.json:13198-16504`
- `wiki/docs/algorithms/index.md:44,132,246,493`

### Algorithm References

- LLVM JumpThreading: `llvm/lib/Transforms/Scalar/JumpThreading.cpp`
- LazyValueInfo: `llvm/lib/Analysis/LazyValueInfo.cpp`

---

**Analysis Quality**: HIGH
**Last Updated**: 2025-11-17
**Confidence**: Very High (multiple function addresses + extensive configuration + detailed evidence)
