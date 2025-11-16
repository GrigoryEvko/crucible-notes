# Loop Optimization Algorithm Research Guide

## Objective
Locate and analyze 12 additional loop optimization passes in CICC binary that are suspected but unconfirmed:
- LoopRotate
- LoopUnrollAndJam
- LoopDeletion
- LoopIdiom
- LoopIdiomVectorize
- LoopSimplifyCFG
- LoopLoadElimination
- LoopSinking
- LoopPredication
- LoopFlatten
- LoopVersioningLICM
- IndVarSimplify

## Research Methodology

### Phase 1: String Evidence Search

#### 1.1 Loop Rotation (`LoopRotate`)
**Purpose**: Convert while loops to do-while form for better optimization

**Search Patterns**:
```
- "rotate" (case insensitive)
- "do-while"
- "while to do"
- "loop rotation"
- "rotation angle"
- "rotate loop"
```

**Expected Characteristics**:
- Transforms branch position within loop
- Creates new basic block structure
- Updates loop control flow
- Referenced before LoopUnroll in pipeline

**Files to Search**:
- /home/grigory/nvopen-tools/cicc/foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
- Binary string tables

---

#### 1.2 Loop Deletion (`LoopDeletion`)
**Purpose**: Remove empty, trivial, or unreachable loops

**Search Patterns**:
```
- "loop deletion"
- "dead loop"
- "infinite loop"
- "trip count" zero
- "unreachable"
- "loop elimination"
```

**Expected Characteristics**:
- Analyzes trip count (determines iteration count)
- Identifies loops that never execute
- Removes unnecessary CFG nodes
- Dead code elimination variant

**Algorithm Hints**:
- Check if trip count is 0 (never executes)
- Check if loop body is empty
- Verify loop is not critical for control flow

---

#### 1.3 Loop Idiom Passes
**Purpose**: Recognize and optimize specific loop patterns

**Search Patterns for LoopIdiom**:
```
- "memcpy"
- "memset"
- "strcpy"
- "loop idiom"
- "pattern match"
- "saxpy"
```

**Search Patterns for LoopIdiomVectorize**:
```
- "idiom vectorize"
- "idiom vector"
- "pattern vector"
- "specialized vectorize"
```

**Expected Characteristics**:
- Matches against standard patterns
- Generates optimized intrinsic calls
- Replaces loop body with library functions
- High-impact optimization for common patterns

---

#### 1.4 Loop Load Elimination (`LoopLoadElimination`)
**Purpose**: Remove redundant loop-invariant loads

**Search Patterns**:
```
- "load elimination"
- "redundant load"
- "loop invariant load"
- "memory forwarding"
- "store-load"
```

**Expected Characteristics**:
- Analyzes loop-invariant memory operations
- Detects redundant loads from same address
- Replaces with register values
- Memory optimization pass

---

#### 1.5 Loop Sinking (`LoopSinking`)
**Purpose**: Move code down to reduce register pressure

**Search Patterns**:
```
- "sinking"
- "loop sink"
- "code sink"
- "register pressure"
- "move down"
```

**Expected Characteristics**:
- Opposite of LICM (moves code DOWN, not UP)
- Reduces number of live registers
- Registers held longer → more pressure
- Complements LICM for fine-tuning

---

#### 1.6 Loop Predication (`LoopPredication`)
**Purpose**: Convert branches to conditional assignments

**Search Patterns**:
```
- "predication"
- "predicate"
- "conditional execution"
- "if convert"
- "branch elimination"
```

**Expected Characteristics**:
- Removes branches from loop body
- Replaces with select/conditional moves
- GPU-relevant: reduces divergence
- Trade: branch elimination vs register pressure

---

#### 1.7 Loop Flattening (`LoopFlatten`)
**Purpose**: Merge nested loops into single loop when possible

**Search Patterns**:
```
- "flatten"
- "loop merge"
- "nest flatten"
- "merge nest"
- "single dimension"
```

**Expected Characteristics**:
- Analyzes nested loop structure
- Computes combined trip count
- Merges loop bounds
- Simplifies nesting

---

#### 1.8 Induction Variable Simplification (`IndVarSimplify`)
**Purpose**: Simplify loop induction variable expressions

**Search Patterns**:
```
- "induction"
- "indvar"
- "iv simplify"
- "induction variable"
- "strength reduction"
```

**Expected Characteristics**:
- Eliminates redundant induction variables
- Simplifies subscript expressions
- Strength reduction (multiply → add)
- SSA-based analysis

---

#### 1.9 Loop CFG Simplification (`LoopSimplifyCFG`)
**Purpose**: Simplify control flow within loop bodies

**Search Patterns**:
```
- "loop simplifycfg"
- "loop cfg"
- "loop control flow"
- "cfg simplify"
- "merge blocks"
```

**Expected Characteristics**:
- Removes unreachable blocks in loops
- Merges redundant branches
- Simplifies control structure
- Works within loop boundaries

---

### Phase 2: Function Identification Strategy

#### 2.1 Call Pattern Analysis

**For each suspected pass, look for functions called by**:
- Pass dispatch mechanism (PassManager)
- Pass initialization/registration
- Pass execution entry point

**Trace pattern**:
```
OptimizationFramework_RunPass
  → PassDispatch_LoopOptimization
    → LoopRotate_Main / LoopUnroll_Main / etc.
      → Loop analysis functions
      → CFG manipulation
      → Code generation
```

#### 2.2 Characteristic Function Patterns

**Loop Analysis Functions**:
- Functions taking `LoopInfo*` parameter
- Functions analyzing `Loop*` structures
- Functions querying trip count
- Functions analyzing memory access patterns

**Data Structure Access Patterns**:
- Accessing `Loop::block_list`, `Loop::subloops`
- Accessing dominator tree from loop
- Accessing induction variable information
- Accessing dependence information

**Code Generation Patterns**:
- Creating new BasicBlock instances
- Inserting/removing instructions
- Updating phi nodes
- Managing SSA form

#### 2.3 Decompilation Hints

**Look for these code patterns in decompiled functions**:

```c
// Loop analysis pattern
for (loop in loopinfo->loops) {
    // Analyze loop structure
    if (canOptimize(loop)) {
        // Apply optimization
    }
}

// CFG pattern (for transformations)
BasicBlock* newBlock = createBasicBlock();
for (auto instr : loop->body) {
    // Duplicate/modify instruction
}

// Cost model pattern
int cost = estimateCost(loop);
if (cost > threshold) {
    // Apply optimization
}
```

---

### Phase 3: Parameter and Configuration Extraction

#### 3.1 Command-Line Parameters

**Search for parameters associated with each pass**:

For LoopRotate:
```
-loop-rotate
-disable-loop-rotate
-rotate-threshold
```

For LoopUnroll:
```
-loop-unroll
-unroll-factor
-unroll-threshold
-unroll-allow-partial
```

For LoopDeletion:
```
-loop-deletion
-trip-count-threshold
```

#### 3.2 Global Configuration Variables

**Search for static/global variables**:
- `g_enable_loop_rotate`
- `g_unroll_factor_default`
- `g_loop_deletion_enabled`

#### 3.3 Pass Registration Code

**Look for**:
- Pass factory functions
- Pass descriptor strings
- Pass constructor calls
- Pass dependency declarations

---

### Phase 4: Cost Model Analysis

#### 4.1 Cost Functions to Find

**For LoopUnroll**:
- `estimateUnrollBenefit()`: Cost reduction from unrolling
- `estimateUnrollCost()`: Code size increase
- `selectUnrollFactor()`: Choose optimal factor

**For LoopVectorize**:
- `estimateVectorizationCost()`: Execution time reduction
- `estimateMemoryBenefit()`: Bandwidth improvement
- `estimateRegisterPressure()`: Register usage increase

**For LoopFusion**:
- `estimateCacheReuse()`: Cache locality improvement
- `estimateFusionBenefit()`: Overall speedup estimate

#### 4.2 Threshold Values

**Common thresholds to find**:
- Loop size threshold (bytes of instructions)
- Unroll factor limits (min/max)
- Register pressure limits
- Memory bandwidth assumptions
- Trip count thresholds

---

### Phase 5: Validation Testing

#### 5.1 Test Kernel Examples

**For LoopRotate**:
```cuda
// while loop that should be rotated
int i = 0;
while (i < N) {
    process(i);
    i++;
}
```

**For LoopUnroll**:
```cuda
// Small loop that should be unrolled
for (int i = 0; i < 100; i++) {
    y[i] = x[i] * 2;
}
```

**For LoopDeletion**:
```cuda
// Dead loop that should be deleted
for (int i = 0; i < 0; i++) {  // Never executes
    unused[i] = 0;
}
```

**For LoopVectorize**:
```cuda
// Data-parallel loop that should be vectorized
for (int i = 0; i < N; i++) {
    y[i] = f(x[i]);  // Independent iterations
}
```

#### 5.2 Compilation and Analysis

1. Compile test kernels with CICC
2. Trace with GDB/instrumentation
3. Verify expected optimizations applied
4. Compare with LLVM reference implementation
5. Analyze generated PTX for optimization evidence

---

## Search Priority Order

### High Priority (Most Likely)
1. **LoopDeletion** - Dead code elimination is fundamental
2. **LoopRotate** - Prerequisite for unrolling
3. **IndVarSimplify** - Critical for induction variable analysis
4. **LoopSinking** - Complements LICM

### Medium Priority (Very Likely)
5. **LoopPredication** - Important for GPU divergence control
6. **LoopLoadElimination** - Memory optimization critical for GPU
7. **LoopIdiom** - High-impact pattern matching

### Lower Priority (Possible)
8. **LoopFlatten** - Useful but less critical
9. **LoopSimplifyCFG** - Specialized variant
10. **LoopIdiomVectorize** - Specialized vectorization

---

## Tools and Commands

### Binary Analysis
```bash
# Search for loop-related strings
strings /home/grigory/nvopen-tools/cicc/bin | grep -i "loop\|rotate\|delete\|idiom"

# Disassemble functions containing specific patterns
objdump -d /home/grigory/nvopen-tools/cicc/bin | grep -A 50 "loop_rotate"

# Use IDA Pro / Ghidra for interactive analysis
# GUI analysis is often better for understanding complex functions
```

### Dynamic Analysis
```bash
# Compile test kernel
/home/grigory/nvopen-tools/cicc/bin -o test.ptx test.cu

# Trace with strace to see function calls
strace -e trace=open,read,write /home/grigory/nvopen-tools/cicc/bin test.cu

# Use instrumentation tools
# GDB with breakpoints on suspected functions
# Pin tool for instruction-level tracing
```

### Static Analysis
```bash
# Grep for function pointers and function calls
# Analyze pass manager dispatch table
# Extract type information from RTTI strings
```

---

## Documentation Standards

For each discovered pass, create JSON file with:
```json
{
  "metadata": {
    "pass_name": "...",
    "confidence": "HIGH|MEDIUM|LOW",
    "status": "CONFIRMED|SUSPECTED|UNKNOWN"
  },
  "discovery": {
    "summary": "...",
    "evidence": [...]
  },
  "algorithm_description": {
    "steps": [...]
  },
  "estimated_function_count": ...,
  "validation_status": {...}
}
```

---

## Expected Findings Summary

### Total Loop Passes Expected
- **Confirmed**: 1 (LICM)
- **Medium Confidence**: 4 (Simplify, Unroll, Vectorize, Interchange)
- **Low Confidence**: 1 (Fusion)
- **Suspected (12)**: Listed above

**Total: 18 loop optimization passes**

### Estimated Function Count
- **LICM**: 150 functions
- **LoopSimplify**: 120 functions
- **LoopUnroll**: 250 functions
- **LoopVectorize**: 400 functions
- **Other passes**: ~1000 functions total

**Total: ~2000 functions for loop optimization framework**

---

## Success Criteria

✓ Locate functions for all 12 unconfirmed loop passes
✓ Document algorithm for each pass with 3+ algorithm steps
✓ Create cost model analysis for 6+ critical passes
✓ Verify GPU-specific adaptations where applicable
✓ Create 12 analysis JSON files matching existing format
✓ Validate with test case compilation and tracing

---

## References

- LLVM Loop Passes: https://llvm.org/docs/Passes/
- Loop Optimization Research Papers
- CICC Foundation Analysis: foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json
- Module Analysis: foundation/analyses/02_MODULE_ANALYSIS.json
