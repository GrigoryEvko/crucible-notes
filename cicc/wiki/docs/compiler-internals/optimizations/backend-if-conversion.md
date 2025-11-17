# If-Conversion (Late)

**Pass Type**: Machine-level control flow optimization
**LLVM Class**: `llvm::IfConverter`
**Algorithm**: Predicated execution with profitability analysis
**Extracted From**: Standard LLVM backend pass
**Analysis Quality**: MEDIUM - Standard pattern, runs late in pipeline
**Pass Category**: Machine-Level Optimization

---

## Overview

If-Conversion (late) is a second if-conversion pass that runs after register allocation, converting remaining control flow patterns into predicated execution. Unlike Early If-Conversion, this pass has complete register allocation information and can make more informed profitability decisions.

**Key Innovation**: Runs post-register-allocation, enabling conversion of patterns missed by early if-conversion while considering actual register pressure and instruction scheduling.

---

## Differences from Early If-Conversion

| Aspect | Early If-Conversion | Late If-Conversion |
|--------|---------------------|-------------------|
| **Timing** | Before register allocation | After register allocation |
| **Register info** | Virtual registers | Physical registers |
| **Profitability** | Estimated costs | Actual costs |
| **Patterns** | Simple diamonds only | More complex patterns |
| **Register pressure** | Unknown | Known (can measure) |
| **Block size limit** | 4 instructions | 8 instructions |

---

## Algorithm Steps

### Step 1: Pattern Identification

Late if-conversion handles additional patterns:

```c
enum IfConversionPattern {
    SIMPLE_TRIANGLE,      // If-then (no else)
    SIMPLE_DIAMOND,       // If-then-else
    COMPLEX_DIAMOND,      // Diamond with multiple instructions
    NESTED_IF,            // Nested if statements (limited)
};

bool identifyPattern(MachineBasicBlock* BB, IfConversionPattern& Pattern) {
    if (isSimpleTriangle(BB)) {
        Pattern = SIMPLE_TRIANGLE;
        return true;
    }

    if (isSimpleDiamond(BB)) {
        Pattern = SIMPLE_DIAMOND;
        return true;
    }

    if (isComplexDiamond(BB)) {
        // Allow larger blocks after register allocation
        if (getBlockSize(BB->ThenBlock) <= 8 && getBlockSize(BB->ElseBlock) <= 8) {
            Pattern = COMPLEX_DIAMOND;
            return true;
        }
    }

    return false;
}
```

### Step 2: Register Pressure Analysis

```c
bool checkRegisterPressure(IfConversionCandidate& C) {
    // Count physical registers used in each block
    unsigned ThenRegs = countPhysicalRegs(C.ThenBlock);
    unsigned ElseRegs = countPhysicalRegs(C.ElseBlock);

    // After predication, both blocks execute → registers live simultaneously
    unsigned PredicatedRegs = ThenRegs + ElseRegs;

    // Check if register pressure acceptable
    unsigned MaxRegs = getMaxPhysicalRegs();
    unsigned CurrentPressure = getCurrentRegisterPressure(C.CondBlock);

    if (CurrentPressure + PredicatedRegs > MaxRegs) {
        return false;  // Would cause spilling
    }

    return true;
}
```

### Step 3: Profitability with Actual Costs

```c
bool isProfitableAfterRegAlloc(IfConversionCandidate& C) {
    // Calculate actual instruction costs (not estimates)
    unsigned ThenCost = 0;
    for (MachineInstr& MI : *C.ThenBlock) {
        ThenCost += getActualInstructionCost(&MI);
    }

    unsigned ElseCost = 0;
    for (MachineInstr& MI : *C.ElseBlock) {
        ElseCost += getActualInstructionCost(&MI);
    }

    // Branch cost includes:
    // - Branch instruction
    // - Potential misprediction
    // - Warp divergence (CUDA)
    unsigned BranchCost = 10 + getMispredictionCost() + getDivergenceCost();

    // Convert if: predicated cost < branch cost
    unsigned PredicatedCost = ThenCost + ElseCost;

    return PredicatedCost < BranchCost;
}
```

### Step 4: Conversion with Physical Registers

```c
void convertWithPhysicalRegs(IfConversionCandidate& C) {
    // Generate predicate
    unsigned Pred = generatePhysicalPredicate(C.CondBlock);

    // Predicate THEN block
    for (MachineInstr& MI : *C.ThenBlock) {
        if (canBePredicated(&MI)) {
            MI.addOperand(MachineOperand::CreateReg(Pred, false));
            MI.setPredicateState(true);
        }
    }

    // Predicate ELSE block with negated predicate
    for (MachineInstr& MI : *C.ElseBlock) {
        if (canBePredicated(&MI)) {
            MI.addOperand(MachineOperand::CreateReg(Pred, false));
            MI.setPredicateState(false);  // Negated
        }
    }

    // Merge blocks
    mergeFinalBlocks(C);
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `enable-late-ifcvt` | bool | true | Master enable flag |
| `late-ifcvt-limit` | int | 8 | Max instructions per block |
| `ifcvt-fn-start` | int | 0 | First function to convert (debugging) |
| `ifcvt-fn-stop` | int | -1 | Last function to convert (debugging) |

---

## CUDA/PTX Considerations

### Extended Predication Support

Late if-conversion can handle more complex patterns:

```ptx
; Complex pattern: Multiple instructions per block
setp.lt.s32 %p0, %r0, 10;

; THEN block (4 instructions)
@%p0 add.s32 %r1, %r2, %r3;
@%p0 mul.s32 %r4, %r1, 2;
@%p0 shr.u32 %r5, %r4, 1;
@%p0 and.b32 %r6, %r5, 0xFF;

; ELSE block (4 instructions)
@!%p0 sub.s32 %r1, %r2, %r3;
@!%p0 mul.s32 %r4, %r1, 3;
@!%p0 shl.b32 %r5, %r4, 2;
@!%p0 or.b32 %r6, %r5, 0xFF;

; Merge
st.global [%r7], %r6;
```

**Total**: 8 predicated instructions (would exceed early if-conversion limit of 4).

### Warp Divergence Elimination

Even post-register-allocation, eliminating divergence is beneficial:

```ptx
; Before: Divergent execution
; Warp executes then-path (16 threads), else-path (16 threads) serially
; Total cycles: Then_cycles + Else_cycles

; After: Predicated execution
; All 32 threads execute all instructions in parallel
; Total cycles: max(Then_cycles, Else_cycles)
; Speedup: 2× when paths balanced
```

---

## Performance Characteristics

### When to Convert (Late)

| Scenario | Decision | Reason |
|----------|----------|--------|
| Divergent branch | Always convert | Eliminates divergence |
| Uniform branch, small blocks | Convert if cost < 2× | Branch overhead high |
| Uniform branch, large blocks | Don't convert | Both paths too expensive |
| High register pressure | Don't convert | Would cause spilling |

### Code Size Impact

| Pattern | Size Change | Notes |
|---------|-------------|-------|
| Triangle (if-then) | 0-5% | Minimal overhead |
| Diamond (if-then-else) | +10-30% | Both paths executed |
| Complex diamond | +20-50% | Large blocks duplicated |

### Execution Time

| Scenario | Speedup | Notes |
|----------|---------|-------|
| Warp divergent | +50-200% | Major benefit |
| Uniform, mispredicted | +10-30% | Avoids misprediction |
| Uniform, predicted | -10-20% | Overhead from both paths |

---

## Integration with Other Passes

### Prerequisites

| Pass | Purpose |
|------|---------|
| **Register Allocation** | Provides physical register info |
| **Early If-Conversion** | Handles simple patterns first |
| **Instruction Scheduling** | Initial scheduling |

### Downstream Passes

| Pass | Interaction |
|------|-------------|
| **PTX Emission** | Outputs predicated code |
| **Peephole Optimizer** | May simplify predicated sequences |

---

## Example Transformation

### Complex Diamond (Post-RegAlloc)

**Before**:
```ptx
  setp.eq.s32 %p0, %r0, 0;
  @%p0 bra then;
else:
  ld.global.u32 %r1, [%r2];
  add.s32 %r3, %r1, 5;
  mul.s32 %r4, %r3, 2;
  st.global [%r5], %r4;
  bra merge;
then:
  ld.global.u32 %r1, [%r6];
  sub.s32 %r3, %r1, 5;
  mul.s32 %r4, %r3, 3;
  st.global [%r7], %r4;
merge:
  ret;
```

**After**:
```ptx
  setp.eq.s32 %p0, %r0, 0;

  ; THEN path (predicated)
  @%p0  ld.global.u32 %r1, [%r6];
  @%p0  sub.s32 %r3, %r1, 5;
  @%p0  mul.s32 %r4, %r3, 3;
  @%p0  st.global [%r7], %r4;

  ; ELSE path (predicated)
  @!%p0 ld.global.u32 %r1, [%r2];
  @!%p0 add.s32 %r3, %r1, 5;
  @!%p0 mul.s32 %r4, %r3, 2;
  @!%p0 st.global [%r5], %r4;

  ret;
```

---

## Debugging and Diagnostics

### Disabling Late If-Conversion

```bash
# Disable late if-conversion
-mllvm -enable-late-ifcvt=false

# Adjust block size limit
-mllvm -late-ifcvt-limit=10

# Convert only specific functions (debugging)
-mllvm -ifcvt-fn-start=5 -mllvm -ifcvt-fn-stop=10
```

### Statistics

```bash
# Enable statistics
-mllvm -stats

# Look for:
# - "Late if-conversions performed"
# - "Complex diamonds converted"
# - "Triangles converted"
```

---

## Known Limitations

| Limitation | Impact | Workaround |
|------------|--------|------------|
| Block size limit (8 instrs) | Very large blocks not converted | Manual optimization |
| Register pressure constraints | High-pressure code not converted | None (correct behavior) |
| Memory operations | May not predicate loads/stores | Architecture limitation |
| No loop conversion | Only if-statements | Loop predication is separate |

---

## Related Optimizations

- **Early If-Conversion**: [backend-early-if-conversion.md](backend-early-if-conversion.md) - Runs before register allocation
- **Machine Function Splitter**: [backend-machine-function-splitter.md](backend-machine-function-splitter.md) - Cold code splitting
- **Instruction Scheduling**: Benefits from predicated code

---

**Pass Location**: Backend (late, after register allocation)
**Confidence**: MEDIUM - Standard LLVM pattern
**Last Updated**: 2025-11-17
**Source**: LLVM backend documentation + PTX predication support
