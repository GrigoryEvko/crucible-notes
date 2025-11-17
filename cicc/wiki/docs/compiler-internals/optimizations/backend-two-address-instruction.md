# Two-Address Instruction Pass

**Pass Type**: Machine-level instruction transformation
**LLVM Class**: `llvm::TwoAddressInstructionPass`
**Algorithm**: Three-address to two-address instruction lowering
**Extracted From**: Standard LLVM backend pass
**Analysis Quality**: MEDIUM - Standard pattern with PTX adaptations
**Pass Category**: Machine-Level Optimization

---

## Overview

The Two-Address Instruction Pass converts three-address machine instructions (where destination differs from sources) into two-address form (where destination must match one source operand). This pass is essential for architectures with two-address instruction constraints and performs copy insertion, operand commutation, and instruction rescheduling to minimize code size and register pressure.

**Key Innovation**: For PTX/CUDA, this pass adapts three-address SSA form to target-specific instruction constraints while minimizing register moves.

---

## Three-Address vs. Two-Address Form

### Three-Address Form (SSA)

In SSA form, instructions have distinct destination and source operands:

```llvm
; Three-address form (SSA)
%result = add i32 %a, %b    ; result ≠ a, result ≠ b
%result2 = mul i32 %x, %y   ; result2 ≠ x, result2 ≠ y
```

All three operands (destination, source1, source2) are independent.

### Two-Address Form (Machine)

Some architectures require destination to match one source operand:

```asm
; Two-address form (x86 example)
add eax, ebx    ; eax = eax + ebx (destination overwrites source1)

; Two-address form (ARM example)
add r0, r0, r1  ; r0 = r0 + r1 (destination must be r0)
```

**Constraint**: `destination == source1` (or sometimes `destination == source2` for commutative operations)

---

## PTX Three-Address Support

**Important**: PTX is primarily a **three-address** architecture:

```ptx
add.s32 %r0, %r1, %r2;      ; r0 = r1 + r2 (three distinct registers)
mul.s32 %r3, %r4, %r5;      ; r3 = r4 * r5
mad.s32 %r6, %r7, %r8, %r9; ; r6 = r7 * r8 + r9 (four operands!)
```

**However**, certain PTX operations have two-address constraints:

1. **Atomic operations**: Destination must match memory location
2. **Predicate operations**: Some predicate instructions overwrite source
3. **Special registers**: Operations on special registers often destructive

---

## Algorithm Overview

### Step 1: Identify Two-Address Constraints

```c
struct TwoAddressConstraint {
    MachineInstr* Instr;
    unsigned DestReg;
    unsigned SourceReg;
    bool Commutable;
};

void identifyTwoAddressInstrs(MachineFunction& MF) {
    for (MachineBasicBlock& MBB : MF) {
        for (MachineInstr& MI : MBB) {
            const MCInstrDesc& Desc = MI.getDesc();

            // Check if instruction has tied operands
            if (Desc.getNumDefs() > 0) {
                unsigned DestIdx = 0;
                int TiedIdx = Desc.getOperandConstraint(DestIdx, MCOI::TIED_TO);

                if (TiedIdx >= 0) {
                    // This is a two-address instruction
                    // Destination must equal source at TiedIdx
                    TwoAddressConstraint C;
                    C.Instr = &MI;
                    C.DestReg = MI.getOperand(DestIdx).getReg();
                    C.SourceReg = MI.getOperand(TiedIdx).getReg();
                    C.Commutable = Desc.isCommutable();
                    Constraints.push_back(C);
                }
            }
        }
    }
}
```

### Step 2: Operand Commutation

For **commutative** operations (add, mul, and, or, xor), try commuting operands to satisfy constraint:

```c
bool tryCommute(MachineInstr* MI, unsigned DestReg) {
    const MCInstrDesc& Desc = MI->getDesc();

    if (!Desc.isCommutable()) {
        return false;
    }

    // Get source operands
    unsigned Src1Idx = 1;
    unsigned Src2Idx = 2;
    unsigned Src1 = MI->getOperand(Src1Idx).getReg();
    unsigned Src2 = MI->getOperand(Src2Idx).getReg();

    // Check if commuting helps
    if (Src2 == DestReg) {
        // Swap operands: now Src1 == DestReg
        MI->getOperand(Src1Idx).setReg(Src2);
        MI->getOperand(Src2Idx).setReg(Src1);
        return true;  // Constraint satisfied by commutation
    }

    return false;
}
```

**Example**:
```llvm
; Before commutation:
%r0 = add %r1, %r0    ; Dest=%r0, Src1=%r1, Src2=%r0 → constraint not satisfied

; After commutation:
%r0 = add %r0, %r1    ; Dest=%r0, Src1=%r0, Src2=%r1 → constraint satisfied!
```

### Step 3: Copy Insertion

If commutation doesn't work, insert a copy instruction:

```c
void insertCopyForTwoAddress(MachineInstr* MI, unsigned DestReg, unsigned SrcReg) {
    if (DestReg == SrcReg) {
        return;  // Already satisfied
    }

    // Insert copy before instruction
    //   copy DestReg, SrcReg
    //   instr DestReg, DestReg, ...

    MachineBasicBlock* MBB = MI->getParent();
    MachineInstr* Copy = BuildMI(*MBB, MI, MI->getDebugLoc(),
                                  TII->get(TargetOpcode::COPY), DestReg)
                            .addReg(SrcReg);

    // Update instruction to use DestReg as source
    for (unsigned i = 0; i < MI->getNumOperands(); i++) {
        if (MI->getOperand(i).isReg() && MI->getOperand(i).getReg() == SrcReg) {
            MI->getOperand(i).setReg(DestReg);
        }
    }
}
```

**Example**:
```llvm
; Before copy insertion:
%r0 = add %r1, %r2    ; Constraint: Dest must equal Src1

; After copy insertion:
%r0 = copy %r1        ; Copy source to destination
%r0 = add %r0, %r2    ; Now constraint satisfied
```

### Step 4: Instruction Rescheduling

Reorder instructions to reduce copy insertion:

```c
void rescheduleToAvoidCopies(MachineBasicBlock& MBB) {
    // If instruction sequence is:
    //   %r0 = instr1 %r1, %r2
    //   %r3 = instr2 %r0, %r4  (two-address: needs %r3 == %r0)
    //
    // And instr1 result is only used by instr2:
    //   Assign %r0 to %r3 directly
    //   %r3 = instr1 %r1, %r2
    //   %r3 = instr2 %r3, %r4  (no copy needed!)

    for (MachineInstr& MI : MBB) {
        if (isTwoAddressInstr(&MI)) {
            // Try to reschedule producers to use correct register
            rescheduleProducer(&MI);
        }
    }
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `enable-two-addr-hack` | bool | false | Enable experimental two-address heuristics |
| `two-address-reschedule` | bool | true | Enable instruction rescheduling |

---

## CUDA/PTX-Specific Handling

### Three-Address PTX Instructions

Most PTX arithmetic is three-address:

```ptx
add.s32 %r0, %r1, %r2;      ; r0 = r1 + r2
mul.lo.s32 %r3, %r4, %r5;   ; r3 = r4 * r5 (low 32 bits)
mad.lo.s32 %r6, %r7, %r8, %r9; ; r6 = r7*r8 + r9
```

**No two-address constraint** for these operations.

### Two-Address PTX Patterns

Certain PTX operations have implicit two-address semantics:

#### Atomic Operations

```ptx
atom.add.u32 %r0, [%r1], %r2;  ; r0 = old_mem, mem[r1] += r2
atom.cas.b32 %r0, [%r1], %r2, %r3;  ; compare-and-swap
```

**Two-address constraint**: Result register may be tied to memory location.

#### Predicate Operations

```ptx
setp.eq.s32 %p0|%p1, %r0, %r1;  ; Set predicate pair
@%p0 mov.s32 %r2, %r3;           ; Predicated move
```

Some predicate operations have destination tied to predicate source.

#### Special Register Operations

```ptx
mov.u32 %r0, %tid.x;      ; Read thread ID (three-address)
mov.u32 %ctaid.x, %r0;    ; Write to special register (two-address constraint!)
```

**Note**: Writing to special registers often requires destination == special register.

### Register Pressure Considerations

Copy insertion increases register pressure:

```ptx
; Before:
add.s32 %r0, %r1, %r2;    ; 3 registers live

; After (if two-address required):
mov.u32 %r0, %r1;         ; 3 registers live
add.s32 %r0, %r0, %r2;    ; 3 registers live (same)
```

**Impact**: Minimal register pressure increase (temporary copy).

### Warp Divergence

Two-address transformations do not affect divergence:
- Copy instructions execute uniformly if source is uniform
- No additional control flow introduced

---

## Performance Characteristics

### Code Size Impact

| Scenario | Code Size Change | Notes |
|----------|------------------|-------|
| Commutation successful | 0% | No additional instructions |
| Copy insertion required | +1 instruction | ~4 bytes per constraint |
| Heavy constraints | +5-15% | Many two-address instructions |
| PTX (three-address) | 0-2% | Minimal impact |

### Execution Time Impact

| Scenario | Performance Impact | Reason |
|----------|-------------------|--------|
| Commutation | 0% | Same computation, reordered operands |
| Copy insertion | 0-2% overhead | Additional move instructions |
| Register spilling induced | 5-20% overhead | Increased register pressure |

### Compilation Time

- **Analysis**: 1-3% overhead for constraint identification
- **Transformation**: 2-5% overhead for copy insertion and rescheduling
- **Total**: 3-8% compile time increase

---

## Example Transformations

### Example 1: Commutative Operation

**Before**:
```llvm
%r0 = add %r1, %r0    ; Two-address constraint: Dest must equal first source
```

**After Commutation**:
```llvm
%r0 = add %r0, %r1    ; Commute operands → constraint satisfied
```

**PTX Output** (unchanged):
```ptx
add.s32 %r0, %r0, %r1;  ; Still three-address in PTX
```

### Example 2: Non-Commutative Operation

**Before**:
```llvm
%r0 = sub %r1, %r2    ; Two-address constraint, but SUB not commutative
```

**After Copy Insertion**:
```llvm
%r0 = copy %r1        ; Insert copy
%r0 = sub %r0, %r2    ; Now constraint satisfied
```

**PTX Output**:
```ptx
mov.u32 %r0, %r1;     ; Copy instruction
sub.s32 %r0, %r0, %r2; ; Subtraction
```

### Example 3: Instruction Rescheduling

**Before**:
```llvm
%r0 = mul %r1, %r2
%r3 = add %r0, %r4    ; Two-address: needs %r3 == %r0
```

**After Rescheduling** (assign %r0 to %r3 directly):
```llvm
%r3 = mul %r1, %r2    ; Rename result to %r3
%r3 = add %r3, %r4    ; Constraint satisfied, no copy!
```

---

## Integration with Other Passes

### Prerequisites

| Pass | Purpose |
|------|---------|
| **Instruction Selection** | Generates machine instructions |
| **PHI Elimination** | Converts to machine form |
| **Virtual Register Rewriting** | Assigns virtual registers |

### Downstream Passes

| Pass | Interaction |
|------|-------------|
| **Register Coalescing** | May eliminate inserted copies |
| **Register Allocation** | Must respect two-address constraints |
| **Peephole Optimizer** | May remove redundant copies |

---

## Debugging and Analysis

### Disabling the Pass

```bash
# Cannot disable (required for correctness on two-address architectures)
# But can control behavior:

# Disable rescheduling
-mllvm -two-address-reschedule=false

# Enable experimental heuristics
-mllvm -enable-two-addr-hack
```

### Statistics

```bash
# Enable statistics
-mllvm -stats

# Look for:
# - "Two-address instructions converted"
# - "Commutations performed"
# - "Copies inserted"
# - "Instructions rescheduled"
```

---

## Known Limitations

| Limitation | Impact | Workaround |
|------------|--------|------------|
| Copy insertion increases code size | 2-15% size increase | Register coalescing eliminates some |
| Register pressure increase | Potential spilling | Careful instruction scheduling |
| Cannot always commute | Non-commutative ops require copies | None (fundamental) |
| Rescheduling limited by dependencies | May not find optimal schedule | Manual code restructuring |

---

## Related Optimizations

- **PHI Elimination**: [backend-phi-elimination.md](backend-phi-elimination.md) - Produces two-address candidates
- **Register Coalescing**: Eliminates copies inserted by this pass
- **Peephole Optimizer**: [backend-peephole-optimizer.md](backend-peephole-optimizer.md) - Removes redundant copies
- **Register Allocation**: Must respect two-address constraints

---

**Pass Location**: Backend (after PHI elimination, before register allocation)
**Confidence**: MEDIUM - Standard LLVM pass
**Last Updated**: 2025-11-17
**Source**: LLVM backend documentation + PTX ISA constraints
