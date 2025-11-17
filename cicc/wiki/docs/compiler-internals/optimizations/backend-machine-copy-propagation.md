# Machine Copy Propagation

**Pass Type**: Machine-level dataflow optimization
**LLVM Class**: `llvm::MachineCopyPropagationPass`
**Algorithm**: Forward dataflow with def-use chain tracking
**Extracted From**: CICC optimization pass mapping (NVIDIA-specific passes)
**Analysis Quality**: MEDIUM - Standard pattern with CUDA adaptations
**Pass Category**: Machine-Level Optimization

---

## Overview

Machine Copy Propagation eliminates redundant register-to-register copy instructions at the machine instruction level by propagating the source value directly to uses of the destination. This pass operates after register allocation and works with physical registers, making it distinct from earlier SSA-based copy propagation.

**Key Innovation**: Operating on physical machine registers allows elimination of copies introduced by register allocation, PHI elimination, and calling conventions.

---

## Algorithm Overview

### Copy Instruction Detection

A **copy instruction** is a register-to-register move:

```ptx
mov.u32 %r1, %r0;     ; Copy: r1 = r0
mov.f32 %f2, %f1;     ; Copy: f2 = f1
mov.pred %p1, %p0;    ; Copy: p1 = p0 (predicate)
```

### Copy Propagation Transformation

**Goal**: Replace uses of copy destination with copy source, then eliminate the copy.

**Before**:
```ptx
mov.u32 %r1, %r0;     ; Copy: r1 = r0
add.s32 %r2, %r1, %r3; ; Use of r1
mul.s32 %r4, %r1, %r5; ; Another use of r1
```

**After**:
```ptx
; mov.u32 %r1, %r0;   ; Copy eliminated
add.s32 %r2, %r0, %r3; ; Use r0 directly
mul.s32 %r4, %r0, %r5; ; Use r0 directly
```

---

## Algorithm Steps

### Step 1: Identify Available Copies

```c
struct CopyInfo {
    unsigned DestReg;
    unsigned SrcReg;
    MachineInstr* CopyInst;
    SmallPtrSet<MachineInstr*, 8> ValidRange;
};

void findAvailableCopies(MachineBasicBlock& MBB) {
    for (MachineInstr& MI : MBB) {
        if (MI.isCopy()) {
            unsigned Dest = MI.getOperand(0).getReg();
            unsigned Src = MI.getOperand(1).getReg();

            // Record copy
            CopyInfo Info;
            Info.DestReg = Dest;
            Info.SrcReg = Src;
            Info.CopyInst = &MI;

            // Compute valid range (until src/dest redefined)
            computeValidRange(Info, MBB);

            AvailableCopies[Dest] = Info;
        }
    }
}
```

### Step 2: Compute Valid Propagation Range

A copy is valid for propagation from its definition until:
1. Source register is **redefined** (kills availability)
2. Destination register is **redefined** (redundant copy)
3. Source register is **killed** by another instruction

```c
void computeValidRange(CopyInfo& Info, MachineBasicBlock& MBB) {
    MachineInstr* Copy = Info.CopyInst;
    unsigned Src = Info.SrcReg;
    unsigned Dest = Info.DestReg;

    bool Valid = true;
    for (MachineInstr& MI : make_range(std::next(Copy->getIterator()), MBB.end())) {
        // Check if source or dest are redefined
        if (MI.modifiesRegister(Src) || MI.modifiesRegister(Dest)) {
            Valid = false;
            break;
        }

        if (Valid) {
            Info.ValidRange.insert(&MI);
        }
    }
}
```

### Step 3: Propagate Copies

```c
void propagateCopies(MachineBasicBlock& MBB) {
    for (auto& [DestReg, Info] : AvailableCopies) {
        unsigned SrcReg = Info.SrcReg;

        // For each instruction in valid range
        for (MachineInstr* MI : Info.ValidRange) {
            // Replace uses of DestReg with SrcReg
            for (MachineOperand& MO : MI->operands()) {
                if (MO.isReg() && MO.getReg() == DestReg && MO.isUse()) {
                    // Propagate: replace DestReg with SrcReg
                    MO.setReg(SrcReg);
                }
            }
        }
    }
}
```

### Step 4: Eliminate Dead Copies

After propagation, remove copies with no remaining uses:

```c
void eliminateDeadCopies(MachineBasicBlock& MBB) {
    for (MachineInstr& MI : make_early_inc_range(MBB)) {
        if (MI.isCopy()) {
            unsigned Dest = MI.getOperand(0).getReg();

            // Check if dest has any remaining uses
            if (!hasUses(Dest, MBB)) {
                MI.eraseFromParent();  // Remove copy
            }
        }
    }
}
```

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `enable-machine-copy-prop` | bool | true | Master enable flag |
| `machine-copy-prop-limit` | int | 100 | Max copies to track per block |

---

## CUDA/PTX-Specific Considerations

### PTX Copy Instructions

PTX has several copy instruction types:

```ptx
; Integer copies
mov.u32 %r1, %r0;        ; 32-bit unsigned
mov.s64 %rd1, %rd0;      ; 64-bit signed

; Floating-point copies
mov.f32 %f1, %f0;        ; 32-bit float
mov.f64 %fd1, %fd0;      ; 64-bit double

; Predicate copies
mov.pred %p1, %p0;       ; Predicate register

; Special register copies
mov.u32 %r0, %tid.x;     ; Thread ID to register
mov.u32 %r1, %ctaid.x;   ; Block ID to register
```

### Register Class Constraints

Copy propagation must respect register class constraints:

```ptx
; CANNOT propagate across register classes
mov.u32 %r0, %tid.x;     ; Special → General
mov.f32 %f0, %r0;        ; General → Float (bitcast, not propagatable)
```

**Constraint**: Only propagate within same register class.

### Warp-Uniform Copies

Copies of warp-uniform values are cheap (all threads execute same copy):

```ptx
; Uniform copy (blockIdx.x same across warp)
mov.u32 %r0, %ctaid.x;   ; Uniform
add.s32 %r1, %r0, 5;     ; Can propagate: add %r1, %ctaid.x, 5
```

**No divergence penalty** for propagating uniform values.

### Predicate Register Copies

Predicate copies enable branch optimization:

```ptx
; Before propagation:
setp.eq.s32 %p0, %r0, 0;  ; Set predicate
mov.pred %p1, %p0;        ; Copy predicate
@%p1 bra target;          ; Branch on copy

; After propagation:
setp.eq.s32 %p0, %r0, 0;
@%p0 bra target;          ; Branch directly on %p0
```

**Benefit**: Eliminates redundant predicate moves.

---

## Performance Characteristics

### Code Size Impact

| Scenario | Code Size Reduction | Notes |
|----------|---------------------|-------|
| Moderate copies | 2-8% | Typical reduction |
| Heavy PHI elimination | 5-15% | Many copies after SSA elimination |
| Calling convention overhead | 3-10% | Argument/return copies |
| Minimal copies | 0-2% | Already optimized |

### Execution Time Impact

| Scenario | Speedup | Reason |
|----------|---------|--------|
| Register move elimination | 1-5% | Fewer instructions |
| Register pressure reduction | 2-8% | Less spilling |
| Predicate copy elimination | 1-3% | Branch optimization |
| No benefit | 0% | Copies already necessary |

### Register Pressure

Copy propagation **reduces** register pressure:
- Eliminates temporary copy destinations
- Fewer live values simultaneously
- Better register utilization

**Example**:
```ptx
; Before (3 registers live):
mov.u32 %r1, %r0;   ; r0, r1 live
add.s32 %r2, %r1, 5; ; r0, r1, r2 live

; After (2 registers live):
add.s32 %r2, %r0, 5; ; r0, r2 live (r1 eliminated)
```

---

## Integration with Other Passes

### Prerequisites

| Pass | Purpose |
|------|---------|
| **Register Allocation** | Assigns physical registers |
| **PHI Elimination** | Produces copy instructions |
| **Two-Address Lowering** | May insert copies |

### Downstream Passes

| Pass | Interaction |
|------|-------------|
| **Dead Code Elimination** | Removes copies with no uses |
| **Peephole Optimizer** | Further simplifies after propagation |
| **PTX Emission** | Emits final code |

---

## Example Transformations

### Example 1: Simple Copy Propagation

**Before**:
```ptx
mov.u32 %r1, %r0;     ; Copy
add.s32 %r2, %r1, 5;  ; Use copy
mul.s32 %r3, %r1, 2;  ; Another use
```

**After**:
```ptx
; mov.u32 %r1, %r0;   ; Eliminated
add.s32 %r2, %r0, 5;  ; Propagated r0
mul.s32 %r3, %r0, 2;  ; Propagated r0
```

### Example 2: Copy Chain

**Before**:
```ptx
mov.u32 %r1, %r0;     ; Copy 1
mov.u32 %r2, %r1;     ; Copy 2 (chain)
add.s32 %r3, %r2, 5;  ; Use final copy
```

**After** (iterative propagation):
```ptx
; mov.u32 %r1, %r0;   ; Eliminated
; mov.u32 %r2, %r1;   ; Eliminated
add.s32 %r3, %r0, 5;  ; Propagated through chain
```

### Example 3: Copy Invalidation

**Before**:
```ptx
mov.u32 %r1, %r0;     ; Copy
add.s32 %r2, %r1, 5;  ; Can propagate
mov.u32 %r0, %r5;     ; r0 redefined!
mul.s32 %r3, %r1, 2;  ; CANNOT propagate (r0 changed)
```

**After**:
```ptx
mov.u32 %r1, %r0;     ; Copy kept
add.s32 %r2, %r0, 5;  ; Propagated
mov.u32 %r0, %r5;     ; Redefinition
mul.s32 %r3, %r1, 2;  ; Use copy (r0 invalid)
```

### Example 4: Calling Convention Copies

**Before** (function call setup):
```ptx
; Prepare arguments
mov.u32 %r0, %r10;    ; arg1 = r10
mov.u32 %r1, %r11;    ; arg2 = r11
call func, (%r0, %r1);

; After call
mov.u32 %r20, %r0;    ; result = r0
add.s32 %r21, %r20, 1;
```

**After**:
```ptx
; Prepare arguments (cannot eliminate - calling convention)
mov.u32 %r0, %r10;    ; Required by ABI
mov.u32 %r1, %r11;    ; Required by ABI
call func, (%r0, %r1);

; After call
; mov.u32 %r20, %r0;  ; Eliminated
add.s32 %r21, %r0, 1; ; Use r0 directly
```

---

## Limitations and Constraints

### Cannot Propagate If:

1. **Source register redefined**:
```ptx
mov.u32 %r1, %r0;
mov.u32 %r0, %r5;     ; r0 redefined
use %r1;              ; Cannot propagate r0
```

2. **Cross basic block** (conservative):
```ptx
BB1:
  mov.u32 %r1, %r0;
  br BB2;
BB2:
  use %r1;            ; Cannot propagate across blocks
```

3. **Different register class**:
```ptx
mov.u32 %r0, %tid.x;  ; Special → General
mov.f32 %f0, %r0;     ; General → Float (not a simple copy)
```

4. **Aliasing concerns** (rare in PTX):
```ptx
mov.u32 %r1, [%r0];   ; Load, not copy
```

---

## Debugging and Diagnostics

### Disabling Copy Propagation

```bash
# Disable machine copy propagation
-mllvm -enable-machine-copy-prop=false

# Limit copies tracked
-mllvm -machine-copy-prop-limit=50
```

### Statistics

```bash
# Enable statistics
-mllvm -stats

# Look for:
# - "Copies propagated"
# - "Copies eliminated"
# - "Copy chains collapsed"
```

---

## Known Limitations

| Limitation | Impact | Workaround |
|------------|--------|------------|
| Conservative across basic blocks | Misses optimization opportunities | Global copy propagation (more expensive) |
| Register class constraints | Cannot propagate across classes | None (fundamental) |
| Calling convention copies | Cannot eliminate ABI-required copies | None (required by ABI) |
| Limited tracking capacity | May miss copies in large blocks | Increase limit |

---

## Related Optimizations

- **Dead Code Elimination**: [backend-dead-machine-instruction-elim.md](backend-dead-machine-instruction-elim.md) - Removes dead copies
- **Peephole Optimizer**: [backend-peephole-optimizer.md](backend-peephole-optimizer.md) - Removes redundant moves
- **PHI Elimination**: [backend-phi-elimination.md](backend-phi-elimination.md) - Produces copies
- **Register Coalescing**: Eliminates copies during allocation

---

**Pass Location**: Backend (after register allocation)
**Confidence**: MEDIUM - Standard machine-level pass
**Last Updated**: 2025-11-17
**Source**: CICC optimization pass mapping (Machine-level optimization cluster)
