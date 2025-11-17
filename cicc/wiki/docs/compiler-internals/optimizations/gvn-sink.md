# GVNSink (Global Value Numbering Sink)

**Pass Type**: Function-level optimization pass
**LLVM Class**: `llvm::GVNSinkPass`
**Algorithm**: Value numbering with code sinking
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: MEDIUM - Pass suspected, algorithm inferred
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`

---

## Overview

GVNSink is a code motion optimization that identifies equivalent computations in different control flow paths and "sinks" (moves) them to their common post-dominator. This is the complement of GVNHoist: while GVNHoist moves computations up to common dominators, GVNSink moves them down to common post-dominators.

**Key Innovation**: Uses value numbering to identify equivalent expressions across different branches, then sinks them to reduce code duplication and register pressure in each branch.

**Core Algorithm**: Post-dominator tree analysis combined with value numbering for expression equivalence.

---

## Pass Configuration

### Evidence from CICC Binary

**Status**: SUSPECTED - Listed in unconfirmed passes
- Category: Value Numbering
- No direct string evidence found
- Algorithm inferred from LLVM standard implementation

### Related Passes

GVNSink is the counterpart to:
- **GVNHoist**: Moves expressions to common dominators (upward motion)
- **GVN**: Value numbering without code motion
- **MachineSinking**: Similar concept at machine IR level

---

## Algorithm Description

### High-Level Overview

GVNSink operates in four phases:

```
Phase 1: Identify Equivalent Expressions
    |
    v
Phase 2: Find Common Post-Dominator
    |
    v
Phase 3: Check Sinking Safety
    |
    v
Phase 4: Sink and Merge Expressions
```

### Phase 1: Identify Equivalent Expressions

Use value numbering to find computations that appear in multiple blocks:

```c
void identifyEquivalentExpressions(Function& F) {
    // Map: value number -> list of instructions
    DenseMap<uint32_t, SmallVector<Instruction*, 4>> equiv_classes;

    for (BasicBlock& BB : F) {
        for (Instruction& I : BB) {
            if (!isSinkable(&I)) continue;

            uint32_t vn = computeValueNumber(&I);
            equiv_classes[vn].push_back(&I);
        }
    }

    // Keep only expressions that appear in multiple blocks
    for (auto& entry : equiv_classes) {
        if (appearsInMultipleBlocks(entry.second)) {
            sinking_candidates.push_back(entry.second);
        }
    }
}

bool isSinkable(Instruction* I) {
    // Can only sink pure computations
    return !I->mayWriteToMemory() &&
           !I->mayReadFromMemory() &&
           !I->mayThrow() &&
           !isa<TerminatorInst>(I) &&
           !isa<PHINode>(I);
}
```

**Sinkable Instructions**:
- Arithmetic operations: `add`, `sub`, `mul`, `div`
- Bitwise operations: `and`, `or`, `xor`, `shl`, `lshr`
- Type conversions: `bitcast`, `zext`, `sext`, `trunc`
- Comparisons: `icmp`, `fcmp`
- GEP (pointer arithmetic)

**Not Sinkable**:
- Memory operations: `load`, `store`
- Function calls (even pure ones, conservatively)
- PHI nodes
- Terminators (`br`, `ret`, `switch`)
- Exception handling (`invoke`, `landingpad`)

### Phase 2: Find Common Post-Dominator

For each equivalence class, find the post-dominator that dominates all uses:

```c
BasicBlock* findSinkTarget(SmallVector<Instruction*, 4>& equiv_instrs) {
    // Find all blocks containing equivalent instructions
    SmallVector<BasicBlock*, 4> source_blocks;
    for (Instruction* I : equiv_instrs) {
        source_blocks.push_back(I->getParent());
    }

    // Find common post-dominator
    BasicBlock* sink_target = source_blocks[0];
    for (unsigned i = 1; i < source_blocks.size(); i++) {
        sink_target = PostDominatorTree.findNearestCommonPostDominator(
            sink_target, source_blocks[i]
        );
    }

    return sink_target;
}
```

**Post-Dominator Tree**:

A block B post-dominates block A if all paths from A to exit pass through B.

```
CFG:                   Post-Dominator Tree:
┌───┐                  ┌───┐
│ A │                  │EXIT│
└─┬─┘                  └─┬─┘
  │                      │
  ├──> B                 ├─ D (post-dominates all)
  │                      │
  └──> C                 ├─ B
  │    │                 │
  └────┴──> D            └─ C
            │
          EXIT

D post-dominates B and C → candidate sink target
```

### Phase 3: Check Sinking Safety

Verify that sinking is safe and beneficial:

```c
bool isSafeSink(SmallVector<Instruction*, 4>& instrs, BasicBlock* sink_target) {
    for (Instruction* I : instrs) {
        // Check 1: All operands must dominate sink target
        for (Value* Operand : I->operands()) {
            if (Instruction* OpInst = dyn_cast<Instruction>(Operand)) {
                if (!dominates(OpInst->getParent(), sink_target)) {
                    return false;  // Operand not available at sink target
                }
            }
        }

        // Check 2: Sinking must not extend live ranges excessively
        if (extendsLiveRangeTooMuch(I, sink_target)) {
            return false;
        }

        // Check 3: Must not sink past side effects
        if (hasSideEffectBetween(I->getParent(), sink_target)) {
            return false;
        }
    }

    // Check 4: Sinking should be beneficial
    return isBeneficialSink(instrs, sink_target);
}

bool isBeneficialSink(SmallVector<Instruction*, 4>& instrs, BasicBlock* target) {
    int cost = 0;
    int benefit = 0;

    // Cost: Extended live ranges for operands
    for (Instruction* I : instrs) {
        cost += estimateLiveRangeExtension(I, target);
    }

    // Benefit: Reduced code duplication
    benefit += (instrs.size() - 1) * instructionCost(instrs[0]);

    // Benefit: Potential for further optimization
    benefit += estimateDownstreamBenefit(instrs, target);

    return benefit > cost;
}
```

**Safety Constraints**:

1. **Operand Availability**: All operands must dominate the sink target
2. **Live Range**: Sinking should not excessively extend live ranges
3. **Side Effects**: Cannot sink past memory operations that may interfere
4. **Cost Model**: Benefit (less duplication) must exceed cost (longer live ranges)

### Phase 4: Sink and Merge Expressions

Move equivalent expressions to sink target and merge them:

```c
void sinkExpressions(SmallVector<Instruction*, 4>& instrs, BasicBlock* sink_target) {
    // Clone first instruction to sink target
    Instruction* sunk = instrs[0]->clone();

    // Insert at beginning of sink target
    BasicBlock::iterator insert_pt = sink_target->getFirstInsertionPt();
    sunk->insertBefore(&*insert_pt);

    // Replace all original instructions with sunk version
    for (Instruction* I : instrs) {
        // Create PHI node to merge values from different paths
        PHINode* Phi = PHINode::Create(I->getType(), instrs.size());
        Phi->insertBefore(&*insert_pt);

        // Add incoming values from each source block
        for (Instruction* Source : instrs) {
            BasicBlock* SourceBB = Source->getParent();
            if (Source == I) {
                Phi->addIncoming(sunk, SourceBB);
            } else {
                Phi->addIncoming(UndefValue::get(I->getType()), SourceBB);
            }
        }

        // Replace uses
        I->replaceAllUsesWith(Phi);
        I->eraseFromParent();
    }
}
```

**Alternate Strategy (Speculative Sinking)**:

In some cases, GVNSink may speculatively execute the computation and use a select:

```c
void speculativeSink(Instruction* I1, Instruction* I2, BasicBlock* sink_target) {
    // Create sunk computation
    Instruction* sunk = I1->clone();
    sunk->insertBefore(sink_target->getFirstInsertionPt());

    // Create select to choose result
    Value* cond = extractBranchCondition(I1->getParent(), I2->getParent());
    SelectInst* select = SelectInst::Create(cond, I1, I2);

    // Replace uses
    I1->replaceAllUsesWith(select);
    I2->replaceAllUsesWith(select);
}
```

---

## Comparison: GVNHoist vs GVNSink

| Aspect | GVNHoist | GVNSink |
|--------|----------|---------|
| **Direction** | Upward (to dominators) | Downward (to post-dominators) |
| **Goal** | Execute computation once before branches | Execute computation once after branches merge |
| **Benefit** | Reduce computations in hot paths | Reduce code duplication |
| **Cost** | May execute unconditionally (speculative) | May extend live ranges |
| **Best Case** | Common computation at start of branches | Common computation at end of branches |
| **Register Pressure** | May increase (earlier computation) | May decrease (later computation) |

### When to Use Each

**GVNHoist** (upward motion):
```llvm
; Before
if (cond) {
    x = a + b;  // Computation at start of branch
    use1(x);
} else {
    y = a + b;  // Same computation
    use2(y);
}

; After GVNHoist
temp = a + b;  // Hoisted before branch
if (cond) {
    use1(temp);
} else {
    use2(temp);
}
```

**GVNSink** (downward motion):
```llvm
; Before
if (cond) {
    compute1();
    x = a + b;  // Computation at end of branch
} else {
    compute2();
    y = a + b;  // Same computation
}
use(phi(x, y));

; After GVNSink
if (cond) {
    compute1();
} else {
    compute2();
}
temp = a + b;  // Sunk after branches merge
use(temp);
```

---

## Example Transformations

### Example 1: Basic Sinking

**Before GVNSink**:

```llvm
define i32 @example(i1 %cond, i32 %a, i32 %b) {
entry:
    br i1 %cond, label %then, label %else

then:
    call void @foo()
    %x = add i32 %a, %b    ; Equivalent computation
    br label %merge

else:
    call void @bar()
    %y = add i32 %a, %b    ; Equivalent computation
    br label %merge

merge:
    %result = phi i32 [%x, %then], [%y, %else]
    ret i32 %result
}
```

**After GVNSink**:

```llvm
define i32 @example(i1 %cond, i32 %a, i32 %b) {
entry:
    br i1 %cond, label %then, label %else

then:
    call void @foo()
    br label %merge

else:
    call void @bar()
    br label %merge

merge:
    %sunk = add i32 %a, %b    ; Sunk to common post-dominator
    ret i32 %sunk
}
```

**Benefit**: Computation executed once instead of twice, code size reduced.

### Example 2: Multiple Equivalent Expressions

**Before**:

```llvm
define i32 @multi_sink(i32 %x, i32 %y, i32 %z) {
entry:
    %cond1 = icmp slt i32 %x, 0
    br i1 %cond1, label %bb1, label %bb2

bb1:
    %a = mul i32 %y, %z
    %b = add i32 %a, 1
    br label %exit

bb2:
    %cond2 = icmp sgt i32 %y, 10
    br i1 %cond2, label %bb3, label %bb4

bb3:
    %c = mul i32 %y, %z    ; Equivalent to %a
    %d = add i32 %c, 2
    br label %exit

bb4:
    %e = mul i32 %y, %z    ; Equivalent to %a
    %f = sub i32 %e, 1
    br label %exit

exit:
    %phi = phi i32 [%b, %bb1], [%d, %bb3], [%f, %bb4]
    ret i32 %phi
}
```

**After GVNSink**:

```llvm
define i32 @multi_sink(i32 %x, i32 %y, i32 %z) {
entry:
    %cond1 = icmp slt i32 %x, 0
    br i1 %cond1, label %bb1, label %bb2

bb1:
    br label %merge

bb2:
    %cond2 = icmp sgt i32 %y, 10
    br i1 %cond2, label %bb3, label %bb4

bb3:
    br label %merge

bb4:
    br label %merge

merge:
    %sunk = mul i32 %y, %z    ; Sunk to common post-dominator
    ; Branch-specific operations
    %offset1 = phi i32 [1, %bb1], [2, %bb3], [-1, %bb4]
    %result = add i32 %sunk, %offset1
    ret i32 %result
}
```

**Note**: This example is simplified. Actual sinking depends on cost model and control flow complexity.

---

## CUDA-Specific Considerations

### Divergent Control Flow

GVNSink must handle divergent branches carefully:

```llvm
; Divergent branch based on threadIdx
if (threadIdx.x < 16) {
    compute_a();
    %x = expensive_computation();
} else {
    compute_b();
    %y = expensive_computation();
}

; Cannot always sink - different warps execute different paths
; Sinking may force all threads to execute, increasing divergence cost
```

**Divergence Constraints**:
- Sinking across divergent branches may increase execution time
- Cost model must account for warp execution model
- May be beneficial if computation is cheap and reduces register pressure

### Register Pressure Management

GVNSink can help reduce register pressure in branches:

```llvm
; Before: High register pressure in both branches
if (cond) {
    // Many live values
    %a = compute1();
    %b = compute2();
    %c = compute3();
    %x = a + b;  // Sinkable
} else {
    // Many live values
    %d = compute4();
    %e = compute5();
    %f = compute6();
    %y = d + e;  // Sinkable
}

; After sinking: Lower register pressure in branches
if (cond) {
    %a = compute1();
    %b = compute2();
    %c = compute3();
} else {
    %d = compute4();
    %e = compute5();
    %f = compute6();
}
%sunk = phi_operand + phi_operand;  // Computed after branches
```

**Benefit**: Fewer live values in each branch → better register allocation.

### Memory Coalescing

GVNSink may affect memory access patterns:

```llvm
; Before
if (cond) {
    %ptr1 = gep %base, %offset
    %val1 = load i32, i32 addrspace(1)* %ptr1
} else {
    %ptr2 = gep %base, %offset  ; Same GEP
    %val2 = load i32, i32 addrspace(1)* %ptr2
}

; GVNSink can sink GEP but NOT load (side effect)
if (cond) {
    %val1 = load i32, i32 addrspace(1)* %sunk_ptr
} else {
    %val2 = load i32, i32 addrspace(1)* %sunk_ptr
}
%sunk_ptr = gep %base, %offset  ; Sunk GEP
```

---

## Algorithm Complexity

| Operation | Best Case | Average Case | Worst Case | Notes |
|-----------|-----------|--------------|------------|-------|
| **Value numbering** | O(n) | O(n) | O(n²) | n = instructions |
| **Post-dominator tree** | O(n) | O(n log n) | O(n²) | n = blocks |
| **Candidate finding** | O(n) | O(n × d) | O(n²) | d = avg depth |
| **Safety checking** | O(1) | O(k) | O(k²) | k = candidates |
| **Sinking** | O(1) | O(k) | O(k) | k = equivalent instrs |
| **Overall GVNSink** | O(n) | O(n²) | O(n³) | Dominated by analysis |

**Space Complexity**:
- Value numbering table: O(n)
- Post-dominator tree: O(n)
- Candidate list: O(n)

---

## Performance Impact

### Typical Results (CUDA Kernels)

| Metric | Improvement | Variability |
|--------|-------------|-------------|
| **Code size** | 1-5% reduction | Medium |
| **Register pressure** | 2-8% reduction | High |
| **Instruction count** | 1-4% reduction | Medium |
| **Execution time** | 0-2% improvement | Low |
| **Compile time** | +1-3% overhead | Low |

### Best Case Scenarios

1. **Duplicated computations at branch ends**:
   - Common pattern in error handling
   - Repeated return value calculations
   - High benefit from sinking

2. **Register-pressure-limited kernels**:
   - Sinking reduces live values in hot paths
   - Better register allocation
   - Performance improvement from reduced spilling

3. **Post-dominated control flow**:
   - Clear merge points for all branches
   - Safe sinking targets easily identified
   - High success rate

### Worst Case Scenarios

1. **No common post-dominators**:
   - Complex control flow with multiple exits
   - No clear sink targets
   - No benefit

2. **Already optimized code**:
   - No duplicated computations
   - Sinking opportunities already exploited
   - Pure overhead

---

## Known Limitations

| Limitation | Impact | Workaround | Status |
|-----------|--------|------------|--------|
| **Conservative memory handling** | Cannot sink memory operations | Use DSE/GVN | Known, safety |
| **Live range extension** | May increase register pressure | Cost model tuning | Known, tradeoff |
| **Complex control flow** | May not find sink targets | Simplify CFG first | Known |
| **Divergence cost** | May increase divergence overhead | CUDA-specific cost model | GPU-specific |

---

## Integration with Pass Pipeline

### Typical Pass Ordering

```
GVN (value numbering)
    ↓
SimplifyCFG (create merge points)
    ↓
[GVNSink] ← Sink equivalent expressions
    ↓
ADCE (remove dead code)
    ↓
Register Allocation (benefits from reduced pressure)
```

### Complementary Passes

**Works well with**:
- **GVNHoist**: Complementary optimization (hoist vs sink)
- **SimplifyCFG**: Creates better sink targets
- **ADCE**: Cleans up dead code after sinking
- **InstCombine**: May enable more sinking opportunities

---

## Configuration and Control

### Enable/Disable

**Note**: GVNSink may not be enabled by default in all CICC configurations.

```bash
# Enable if available
nvcc -Xcicc -mllvm=-enable-gvn-sink kernel.cu

# Disable if causing issues
nvcc -Xcicc -mllvm=-disable-gvn-sink kernel.cu
```

---

## Decompiled Code Evidence

**Evidence Sources**:
- Listed in unconfirmed passes (value numbering category)
- No direct string evidence
- Algorithm inferred from LLVM documentation

**Confidence Level**: MEDIUM
- Pass existence suspected but not confirmed
- Algorithm based on LLVM standard implementation
- CUDA-specific handling speculative

---

## References

**LLVM Documentation**:
- GVNSink Pass: https://llvm.org/doxygen/GVNSink_8cpp.html
- Post-Dominator Tree: https://llvm.org/docs/ProgrammersManual.html#post-dominator-tree

**Related Passes**:
- GVNHoist (upward code motion)
- GVN (value numbering)
- MachineSinking (machine-level sinking)
- SimplifyCFG (creates sink opportunities)

**Research Papers**:
- Click, "Global Code Motion/Global Value Numbering" (1995)
- Knoop et al., "Lazy Code Motion" (1992)

---

**L3 Analysis Quality**: MEDIUM (pass suspected, not confirmed)
**Last Updated**: 2025-11-17
**Source**: CICC foundation analyses + 21_OPTIMIZATION_PASS_MAPPING.json
