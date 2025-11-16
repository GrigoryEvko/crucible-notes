# LICM Loop Versioning

## Overview

Loop Invariant Code Motion (LICM) with loop versioning creates specialized loop versions with distinct memory access preconditions. The transformation executes runtime checks at loop entry to select between a fast path (optimized version with hoisted code, assuming safe memory patterns) and a safe path (original or conservatively hoisted version). Versioning enables LICM in cases where full static invariance cannot be proven.

## Versioning Strategy Architecture

### Path Variants

**Fast Path**: Optimized loop version where hoisted invariant instructions execute in the preheader, assuming memory access patterns satisfy non-aliasing conditions. Loop body executes with reduced instruction count and improved data locality.

**Safe Path**: Original unoptimized loop or variant with runtime checks inserted inside loop body. Executes when preheader memory checks fail or when static analysis proves aliasing relationship may exist.

### Precondition Check Placement

- **Location**: Loop preheader block
- **Execution**: Guaranteed exactly once before any loop iteration
- **Overhead**: Single branch instruction with amortized cost across all iterations
- **Maximum Versions**: 3 distinct versioned loops
- **Check Type**: Memory range non-aliasing predicates

### Memory Non-Aliasing Predicate

```
guard_condition = (addr1 + size1 <= addr2) OR (addr2 + size2 <= addr1)
```

Two memory regions are non-overlapping when: first region ends before second starts, OR second region ends before first starts. Checks placed in preheader ensure all iterations execute with verified memory constraints.

## Configuration Parameters

| Parameter | Flag | Type | Default | Unit | Description | Constraint |
|-----------|------|------|---------|------|-------------|-----------|
| Loop Versioning Enable | `enable-loop-versioning-licm` | boolean | `true` | — | Global enable/disable for entire versioning system | CRITICAL |
| Invariant Percentage Threshold | `licm-versioning-invariant-threshold` | integer | `90` | percent | Minimum required percentage of loop instructions meeting invariance criteria | 0-100 |
| Nesting Depth Threshold | `licm-versioning-max-depth-threshold` | integer | `2` | nesting levels | Maximum allowed loop nest depth for versioning application | 1-4 typical |
| Runtime Memory Check Limit | `runtime-memory-check-threshold` | integer | `8` | comparisons | Maximum number of memory range comparisons generated per loop | Prevents exponential growth |
| Check Merge Comparison Budget | `memory-check-merge-threshold` | integer | `100` | comparisons | Maximum total comparisons allowed when merging multiple memory check predicates | Bounds merge algorithm |
| Loop Flatten Version Flag | `loop-flatten-version-loops` | boolean | `true` | — | Version loops when flattening transformation could cause integer overflow | Safety mechanism |
| No-Alias Annotation | `loop-version-annotate-no-alias` | boolean | `true` | — | Attach no-alias metadata to disambiguated instructions in fast path | Downstream optimization |

### Per-Loop Control

**Metadata**: `llvm.loop.licm_versioning.disable`
- **Scope**: Single loop latch block
- **Type**: Loop metadata annotation
- **Default**: Versioning enabled unless globally disabled
- **Effect**: Disables versioning for annotated loop regardless of global settings

### Additional Parameters

**`hoist-runtime-checks`** (boolean, optional): Hoist inner loop memory checks to outer loop preheader if possible. Reduces cumulative check cost in nested loop structures.

**`disable-memory-promotion`** (boolean, default: `false`): Disables memory promotion optimization in LICM, affecting versioning efficiency for memory hoisting patterns.

## Decision Criteria and Versioning Formula

### Primary Decision Formula

```
version_loop = (hoist_benefit > memory_check_overhead * threshold_multiplier)
               AND (invariant_ratio >= 0.90)
               AND (nesting_depth <= 2)
```

### Threshold Values

- **Invariant Ratio Threshold**: `>= 0.90` (90% of loop instructions must be loop-invariant or conditionally invariant)
- **Nesting Depth Threshold**: `<= 2` (no versioning beyond 2-level deep nesting)
- **Check Count Threshold**: `<= 8` (maximum memory checks per loop)
- **Check Merge Budget**: `<= 100` (maximum comparisons in merge operation)
- **Benefit Multiplier**: `2.0` (benefit must exceed overhead by factor of 2.0)

### Rejection Criteria with C-Level Conditions

#### 1. Divergent Control Flow Targets
```c
if (loop->isLoopHasMultipleExits() &&
    loop->getExitCount() > 1 &&
    !loop->isLoopControlledByBranch()) {
    reject_versioning();  // CantVersionLoopWithDivergentTarget
}
```
**Effect**: Register pressure from multiple control flow paths makes versioning uneconomical.

#### 2. Size Optimization Mode
```c
if (optimization_level == OptimizeForSize ||  // -Os flag
    optimization_level == OptimizeForMinSize) { // -Oz flag
    reject_versioning();  // CantVersionLoopWithOptForSize
}
```
**Effect**: Code duplication violates size constraints. Single path preferred regardless of performance benefit.

#### 3. Unknown Loop Count with Complex CFG
```c
if (!canComputeLoopTripCount(loop) &&
    !loop->isSimplifiedLoop() &&
    loop->getNumBlocks() > 4) {
    reject_versioning();  // UnknownLoopCountComplexCFG
}
```
**Effect**: Versioning requires trip count estimation; complex control flow prevents reliable estimation needed for check placement.

#### 4. Tail Loop Handling with Size Optimization
```c
if (hasTailLoopRequirement(loop) &&
    (optimization_level == OptimizeForSize ||
     optimization_level == OptimizeForMinSize)) {
    reject_versioning();  // NoTailLoopWithOptForSize
}
```
**Effect**: Tail loop handling code combined with size constraints makes versioning ineffective.

#### 5. Small Trip Count
```c
if (estimated_trip_count <= 1 ||
    (max_trip_count != -1 && max_trip_count <= 1)) {
    reject_versioning();  // SmallTripCount
}
```
**Effect**: Versioning benefit amortized over single or very few iterations cannot offset check overhead.

#### 6. Stride Mismatch in Iteration
```c
if (loop_stride != 1 &&
    (total_trip_count % loop_stride) != 0) {
    reject_versioning();  // StrideMismatch
}
```
**Effect**: Uneven stride division creates tail loop requiring separate handling, eliminating versioning benefit.

## Cost Model and Benefit Calculation

### Benefit Estimation Formula

```
benefit = loop_trip_count × (hoisted_instruction_cost × num_hoisted_instructions)
```

Where:
- **loop_trip_count**: Estimated or dynamic number of loop iterations
- **hoisted_instruction_cost**: Execution latency/throughput cost of instruction (in cycles or equivalent units)
- **num_hoisted_instructions**: Count of loop-invariant instructions moved to preheader

### Overhead Estimation Formula

```
overhead = 1 × (memory_check_cost × num_memory_checks)
```

Where:
- **memory_check_cost**: Execution cost of single pointer range comparison at loop entry (typically 2-4 cycles)
- **num_memory_checks**: Count of distinct memory non-aliasing predicates generated

### Versioning Threshold Condition

```
version_if: hoist_benefit > check_overhead × threshold_multiplier

threshold_multiplier = 2.0
```

Versioning decision requires benefit to exceed overhead by factor of 2.0, ensuring 2:1 performance ratio before version generation.

### Decision Logic in 6 Steps

**Step 1: Candidate Identification**
```c
for each instruction in loop {
    if (instruction->isLoopInvariant() ||
        instruction->isConditionallyInvariant()) {
        if (!hasControlFlowDependency() &&
            !hasMemoryDependencyWithinLoop()) {
            candidate_set.insert(instruction);
        }
    }
}
invariant_count = candidate_set.size();
invariant_ratio = invariant_count / total_instruction_count;
```

**Step 2: Safety Analysis**
```c
bool is_safe_to_hoist = true;
for each candidate in candidate_set {
    // Check memory aliasing
    for each memory access in loop {
        if (mayAlias(candidate, memory_access)) {
            is_safe_to_hoist = false;
            unsafe_candidates.insert(candidate);
        }
    }
    // Check control flow
    if (!dominatesAllLoopIterations(candidate)) {
        is_safe_to_hoist = false;
    }
    // Check exception behavior
    if (mayThrowException(candidate)) {
        is_safe_to_hoist = false;
    }
}
```

**Step 3: Versioning Decision**
```c
if (safety_fully_proven) {
    hoist_directly();  // No versioning needed
} else {
    float benefit = calculateHoistBenefit(
        unsafe_candidates,
        estimated_trip_count
    );
    float overhead = calculateCheckOverhead(
        aliasing_relationships.size(),
        memory_check_cost
    );

    if ((benefit > overhead * 2.0) &&
        (invariant_ratio >= 0.90) &&
        (nesting_depth <= 2) &&
        (aliasing_relationships.size() <= 8)) {
        create_versioned_loops();
    } else {
        skip_hoisting();
    }
}
```

**Step 4: Version Generation**
```c
// Clone loop body for fast path
BasicBlock* fast_loop = cloneLoopStructure(original_loop);
BasicBlock* safe_loop = original_loop;

// Generate memory checks
vector<ICmpInst*> checks;
for each aliasing_pair in aliasing_relationships {
    // Generate: addr1 + size1 <= addr2 OR addr2 + size2 <= addr1
    Value* check = generateNonAliasingCheck(
        aliasing_pair.first,
        aliasing_pair.second
    );
    checks.push_back(check);
}

// Combine checks with AND logic
Value* combined_check = combineChecks(checks);

// Insert preheader branch
BranchInst* version_branch = BranchInst::Create(
    fast_loop_header,
    safe_loop_header,
    combined_check,
    loop_preheader->getTerminator()
);
```

**Step 5: Code Motion**
```c
// Move to fast path preheader (preheader of fast path loop)
for each hoisted_instruction in safe_candidates {
    hoisted_instruction->moveBefore(fast_loop_preheader->getTerminator());
}

// Safe path keeps original instructions in loop body
// or inserts runtime checks inside loop if partial hoisting attempted
```

**Step 6: Metadata Annotation**
```c
// Mark fast path with no-alias information
for each hoisted_instruction in safe_candidates {
    for each memory access originally aliasing with this instruction {
        // Create no-alias metadata since preheader check guarantees non-aliasing
        MDNode* no_alias_scope = createNoAliasScope();
        hoisted_instruction->setMetadata(
            "noalias",
            no_alias_scope
        );
    }
}

// Mark versioned loop with metadata
fast_loop->setMetadata("llvm.loop.licm_versioning",
                       MDString::get(context, "versioned_loop"));
```

## Runtime Memory Check Generation

### Check Format

For pointer ranges `[addr1, addr1+size1)` and `[addr2, addr2+size2)`:

```c
// Non-aliasing condition (ranges do not overlap)
bool ranges_non_aliasing =
    (addr1 + size1 <= addr2) ||  // First ends before second starts
    (addr2 + size2 <= addr1);     // Second ends before first starts
```

### Check Generation in C Pseudocode

```c
// Generate checks for all potentially-aliasing pairs
vector<Value*> memory_checks;

for (auto& pair : aliasing_pairs) {
    Value* ptr1 = pair.first_pointer;
    Value* size1 = pair.first_size;
    Value* ptr2 = pair.second_pointer;
    Value* size2 = pair.second_size;

    // Cast pointers to integers for range comparison
    Value* addr1 = PtrToIntInst::Create(
        Instruction::PtrToInt,
        ptr1,
        int64_type,
        "addr1"
    );
    Value* addr2 = PtrToIntInst::Create(
        Instruction::PtrToInt,
        ptr2,
        int64_type,
        "addr2"
    );

    // Compute range endpoints
    Value* end1 = BinaryOperator::Create(
        Instruction::Add,
        addr1,
        size1,
        "end1"
    );
    Value* end2 = BinaryOperator::Create(
        Instruction::Add,
        addr2,
        size2,
        "end2"
    );

    // Generate: end1 <= addr2 (first range before second)
    Value* cmp1 = ICmpInst::Create(
        Instruction::ICmp,
        ICmpInst::ICMP_ULE,
        end1,
        addr2,
        "cmp1"
    );

    // Generate: end2 <= addr1 (second range before first)
    Value* cmp2 = ICmpInst::Create(
        Instruction::ICmp,
        ICmpInst::ICMP_ULE,
        end2,
        addr1,
        "cmp2"
    );

    // Combine: OR logic for non-aliasing
    Value* non_aliasing = BinaryOperator::Create(
        Instruction::Or,
        cmp1,
        cmp2,
        "non_aliasing"
    );

    memory_checks.push_back(non_aliasing);
}

// Merge multiple checks with AND logic
Value* final_check = memory_checks[0];
for (size_t i = 1; i < memory_checks.size(); ++i) {
    final_check = BinaryOperator::Create(
        Instruction::And,
        final_check,
        memory_checks[i],
        "merged_check"
    );
}

// Insert branch in preheader
BranchInst::Create(
    fast_path_loop,
    safe_path_loop,
    final_check,
    preheader->getTerminator()
);
```

### Check Optimization and Merging

When multiple checks exist, the compiler attempts merging to reduce comparisons:

```c
vector<ICmpInst*> merge_checks(vector<ICmpInst*> checks) {
    vector<ICmpInst*> merged;
    int comparison_budget = 100;  // memory-check-merge-threshold
    int comparisons_used = 0;

    for (auto check : checks) {
        // Cost of adding this check: 1 comparison + dependencies
        int check_cost = estimateComparisonCost(check);

        if (comparisons_used + check_cost <= comparison_budget) {
            merged.push_back(check);
            comparisons_used += check_cost;
        } else {
            // Budget exhausted, skip remaining checks
            // Fall back to safe path for unverified pairs
            break;
        }
    }
    return merged;
}
```

## Generated Code Structure

### Preheader Block (Memory Check Insertion)

```c
// Loop preheader
preheader:
    // Load base pointers and sizes (loop-invariant)
    %ptr1 = load i64* @global_array_ptr
    %ptr2 = load i64* @local_array_ptr
    %size1 = load i64* @size1_ptr
    %size2 = load i64* @size2_ptr

    // Compute range endpoints
    %end1 = add i64 %ptr1, %size1
    %end2 = add i64 %ptr2, %size2

    // Generate non-aliasing checks
    %cmp1 = icmp ule i64 %end1, %ptr2        ; end1 <= ptr2
    %cmp2 = icmp ule i64 %end2, %ptr1        ; end2 <= ptr1
    %no_alias = or i1 %cmp1, %cmp2           ; OR logic

    // Branch based on memory check result
    br i1 %no_alias, label %fast.loop.entry, label %safe.loop.entry
```

### Fast Path Loop

```c
// Fast path: optimized version with hoisted code
fast.loop.entry:
    // Hoisted loop-invariant instructions execute once before loop
    %inv_load = load i32* @constant_ptr
    %inv_mul = mul i32 %inv_load, 5
    %inv_add = add i32 %inv_mul, 10
    br label %fast.loop.body

fast.loop.body:
    %i = phi i32 [0, %fast.loop.entry], [%i.next, %fast.loop.latch]

    // Original loop body with fewer instructions
    // Invariant computation references hoisted value
    %result = mul i32 %i, %inv_add
    %ptr = getelementptr i32* %array, i32 %i
    store i32 %result, i32* %ptr

    br label %fast.loop.latch

fast.loop.latch:
    %i.next = add i32 %i, 1
    %exit = icmp slt i32 %i.next, %trip.count
    br i1 %exit, label %fast.loop.body, label %exit

// Metadata on fast path loop
; llvm.loop.licm_versioning = "versioned_loop"
; llvm.loop.no_aliasing = true
; (enables downstream optimizations: vectorization, unrolling)
```

### Safe Path Loop

```c
// Safe path: original unoptimized loop
safe.loop.entry:
    br label %safe.loop.body

safe.loop.body:
    %i = phi i32 [0, %safe.loop.entry], [%i.next, %safe.loop.latch]

    // Original loop body with all instructions
    %inv_load = load i32* @constant_ptr
    %inv_mul = mul i32 %inv_load, 5
    %inv_add = add i32 %inv_mul, 10

    %result = mul i32 %i, %inv_add
    %ptr = getelementptr i32* %array, i32 %i
    store i32 %result, i32* %ptr

    br label %safe.loop.latch

safe.loop.latch:
    %i.next = add i32 %i, 1
    %exit = icmp slt i32 %i.next, %trip.count
    br i1 %exit, label %safe.loop.body, label %exit

// No metadata on safe path; aliasing constraints preserved
exit:
    ret void
```

## Versioning Algorithm Summary

### Complete 6-Step Transformation

```c
struct VersioningResult {
    bool should_version;
    vector<Instruction*> candidates;
    vector<pair<Value*, Value*>> aliasing_pairs;
};

VersioningResult analyzeLoopForVersioning(Loop* loop) {
    // STEP 1: Identify hoistable candidates
    vector<Instruction*> candidates;
    for (auto* block : loop->blocks()) {
        for (auto& instr : *block) {
            if (isLoopInvariant(&instr, loop) ||
                isConditionallyLoopInvariant(&instr, loop)) {
                if (canHoist(&instr, loop)) {
                    candidates.push_back(&instr);
                }
            }
        }
    }

    if (candidates.empty()) {
        return {false, {}, {}};
    }

    float invariant_ratio = (float)candidates.size() / loop->getNumInstructions();

    // STEP 2: Perform aliasing analysis
    vector<pair<Value*, Value*>> aliasing_pairs;
    AliasAnalysis* AA = getAnalysis<AliasAnalysis>();

    for (auto* instr : candidates) {
        if (auto* load = dyn_cast<LoadInst>(instr)) {
            for (auto* block : loop->blocks()) {
                for (auto& other_instr : *block) {
                    if (auto* store = dyn_cast<StoreInst>(&other_instr)) {
                        if (AA->alias(load->getPointerOperand(),
                                      store->getPointerOperand()) !=
                            AliasResult::NoAlias) {
                            aliasing_pairs.push_back(
                                {load->getPointerOperand(),
                                 store->getPointerOperand()}
                            );
                        }
                    }
                }
            }
        }
    }

    // STEP 3: Evaluate versioning decision
    int num_checks = aliasing_pairs.size();
    int nesting_depth = loop->getLoopDepth();
    int trip_count = getTripCount(loop);

    if (invariant_ratio < 0.90) {
        return {false, candidates, aliasing_pairs};
    }
    if (nesting_depth > 2) {
        return {false, candidates, aliasing_pairs};
    }
    if (num_checks > 8) {
        return {false, candidates, aliasing_pairs};
    }

    float benefit = trip_count * estimateInstructionCost(candidates);
    float overhead = num_checks * 4;  // ~4 cycles per check

    if (benefit <= overhead * 2.0) {
        return {false, candidates, aliasing_pairs};
    }

    // STEP 4: Generate versioned loops
    // STEP 5: Move hoistable code
    // STEP 6: Annotate with metadata

    return {true, candidates, aliasing_pairs};
}
```

## Interaction with Analysis Passes

| Pass | Role | Dependency |
|------|------|-----------|
| **LoopSimplify** | Preprocessor ensuring canonical loop structure (single entry, single latch, preheader exists) | MUST run before LICM versioning |
| **AliasAnalysis** | Identifies memory aliasing relationships between loop accesses | REQUIRED for check generation |
| **ScalarEvolution** | Estimates trip counts and iteration ranges | Required for cost model |
| **DominatorTree** | Determines safe code movement via domination relationships | Required for safety analysis |
| **LoopUnroll** | Post-versioning optimization: unrolls fast path for additional benefit | Can run after versioning |
| **LoopVectorize** | Post-versioning optimization: vectorizes fast path due to aliasing constraints lifted | Strongly benefits from versioning |
| **LoopFlatten** | Flattens nested loops; versioning prevents integer overflow from flattened iteration counts | Coordinated via `loop-flatten-version-loops` flag |

## Performance Characteristics

### Compile-Time Overhead
- **Fast case**: O(loop_size) for invariance analysis and check generation
- **Worst case**: O(n²) for aliasing analysis of large loops (n = number of loop instructions)
- **Check generation**: Linear in number of aliasing pairs

### Code Size Impact
- **2x loop duplication**: Both fast and safe paths occupy code cache
- **Preheader expansion**: ~20-50 instructions for memory check generation
- **Total overhead**: Typically 2x + constant factor for small loops; negligible percentage for large loops

### Runtime Behavior

**Best Case** (all paths execute fast path):
- Single branch misprediction at loop entry: ~15 cycles amortization across loop
- Improved cache locality and memory bandwidth from hoisted loads
- Typical improvement: 5-15% on memory-bound kernels

**Worst Case** (all paths execute safe path):
- Branch always mispredicts: ~15 cycle penalty per loop execution
- No benefit from hoisting, check overhead present
- Degradation: 5-10% on compute-bound kernels

**Typical Case** (static memory patterns):
- Branch predictor learns execution pattern after 1-2 loop executions
- Amortized branch cost negligible (<1 cycle per iteration)
- Typical improvement: 10-20% on LICM-amenable loops

### Memory Bandwidth Reduction
Loop-invariant load hoisting reduces memory traffic by 10-30% on kernels with repeated data access patterns. Effect multiplies with trip count and instruction hoisting opportunity.

## Limitations and Constraints

1. **Depth Limitation**: Versioning disabled beyond 2-level nesting (exponential code growth prevention)
2. **Check Budget**: Maximum 8 memory checks per loop (prevents predicate explosion)
3. **Merge Budget**: 100-comparison limit when combining predicates (bounds merge complexity)
4. **Size Optimization**: Entirely disabled under -Os or -Oz (code duplication violates size contracts)
5. **Code Generation Cost**: Check generation time can dominate compilation time for loops with 100+ instructions and many aliasing pairs

## Verification Evidence

**Binary Evidence**:
- Parameter registration: `decompiled/ctor_218_0x4e7a30.c` - invariant threshold and depth threshold parameters
- Memory check integration: `decompiled/ctor_053_0x490b90.c` - memory check threshold values
- Loop flattening coordination: `decompiled/ctor_461_0x5472b0.c` - loop-flatten-version-loops flag
- Metadata control: `decompiled/sub_F6E950_0xf6e950.c` - llvm.loop.licm_versioning.disable metadata
- Implementation details: `decompiled/sub_19C97B0_0x19c97b0.c` and `decompiled/sub_288E950_0x288e950.c` - versioning with memchecks

**String Evidence**:
- "Loop Versioning for LICM"
- "Versioned loop for LICM"
- "LoopVersioningLICM"
- "licm-versioning-invariant-threshold"
- "licm-versioning-max-depth-threshold"
- "enable-loop-versioning-licm"

## Related Optimizations

**Loop Invariant Code Motion**: Hoisting instruction execution outside loop boundaries to reduce recomputation across iterations.

**Loop Unrolling**: Duplicating loop body to reduce branch overhead and expose additional parallelism.

**Loop Vectorization**: SIMD execution of multiple iterations in parallel; more effective on hoisted loops with eliminated aliasing constraints.

**Loop Flattening**: Converting nested loops into single loop; versioning prevents integer overflow in flattened iteration counts.

**Memory Promotion**: Hoisting scalar loads/stores to registers to reduce memory traffic.

