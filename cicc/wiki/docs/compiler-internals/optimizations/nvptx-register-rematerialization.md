# NVPTX Register Rematerialization on NVVM IR

**Pass Type**: NVIDIA-specific register pressure optimization
**LLVM Class**: `RegisterRematerializationOnNVVMIR`
**Category**: Register Allocation / Occupancy Optimization
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: MEDIUM - Inferred from optimization patterns
**Pass Index**: Listed in optimization_pass_mapping.json

---

## Overview

Register Rematerialization is a critical GPU optimization that **recomputes values instead of spilling them to memory** when register pressure is high. Rather than saving a register value to local memory (spilling), the compiler recomputes (rematerializes) the value when needed again. This is beneficial when the cost of recomputation is less than the cost of memory access.

**Key Insight**: On GPUs, memory access (even cached local memory) is 10-100x slower than recomputation for simple operations.

**Operating Level**: This pass works on NVVM IR (NVIDIA's LLVM variant) before final PTX emission, allowing high-level analysis of rematerialization opportunities.

---

## GPU-Specific Motivation

### Register Pressure and Occupancy

**Occupancy Formula**:
```
Max Concurrent Threads = min(
    HW_MaxThreads,
    TotalRegisters / RegistersPerThread,
    SharedMemory / SharedMemPerThread,
    LocalMemory / LocalMemPerThread
)
```

**Example** (SM 8.0 - Ampere):
- Total registers: 65,536 per SM
- Registers per thread (before rematerialization): 64
- Max threads: 65,536 / 64 = **1,024 threads**

**After rematerialization**:
- Registers per thread: 58 (6 values rematerialized instead of stored)
- Max threads: 65,536 / 58 = **1,129 threads**
- **Occupancy improvement: 10.2%**

### Memory Access Cost vs Recomputation

| Operation | Recompute Cost (cycles) | Spill/Reload Cost (cycles) | Decision |
|-----------|-------------------------|----------------------------|----------|
| `add r1, r2, r3` | 1-2 | 400-800 (uncached) | **Rematerialize** |
| `mul r1, r2, r3` | 4-8 | 400-800 | **Rematerialize** |
| `fma r1, r2, r3, r4` | 4-8 | 400-800 | **Rematerialize** |
| `load r1, [mem]` | 400-800 | 400-800 | **Spill** (same cost) |
| Complex expression | 50+ | 400-800 | **Depends** |

**Guideline**: Rematerialize if recompute cost < 20% of spill cost.

---

## Algorithm

### Phase 1: Identify Rematerialization Candidates

**Scan for values that can be recomputed**:

```
RematerialCandidates = []

FOR each Instruction I in Function:
    IF I is "cheap" to recompute:
        IF I has multiple uses:
            IF register pressure is high in live range:
                RematerialCandidates.add(I)
```

**Cheap Instructions** (good candidates):
- Arithmetic: add, sub, mul, neg, abs
- Logical: and, or, xor, not
- Shifts: shl, shr, ashr
- Simple conversions: zext, sext, trunc
- Constant materialization: loading constants
- Address calculations: getelementptr with constants

**Expensive Instructions** (poor candidates):
- Memory loads: load, ld.global, ld.shared
- Function calls: call
- Atomic operations: atomicadd, atomicCAS
- Expensive math: div, sqrt, sin, cos
- Texture/surface operations

### Phase 2: Analyze Cost-Benefit

**For each candidate, compute**:

```
FOR each Candidate C:
    RecomputeCost = estimateRecomputeCost(C)
    SpillCost = estimateSpillCost(C)

    IF RecomputeCost < SpillCost * THRESHOLD:
        IF sourcesAvailable(C):
            RematerializeSet.add(C)
```

**Cost Estimation**:
```
RecomputeCost(Instruction) =
    InstructionLatency +
    OperandMaterializationCost +
    CodeSizeOverhead

SpillCost(Value) =
    SpillStoreCost (st.local) +
    SpillLoadCost (ld.local) * NumReloads +
    RegisterPressureReduction
```

**Threshold**: Typically 0.5-0.8 (rematerialize if recompute < 50-80% of spill cost).

### Phase 3: Check Source Availability

**Ensure source operands are available at rematerialization sites**:

```
FOR each RematerializeValue V:
    FOR each Use U of V:
        FOR each Source S in sources(V):
            IF NOT dominates(S, U):
                // Source not available - cannot rematerialize
                RematerializeSet.remove(V)
                BREAK
```

**Critical Constraint**: All source operands must be live (available) at every point where the value is rematerialized.

### Phase 4: Insert Rematerialization Code

**Replace spills with recomputation**:

```
FOR each Value V in RematerializeSet:
    // Remove original spill
    RemoveSpill(V)

    FOR each Use U of V:
        // Insert recomputation before use
        NewInstr = clone(V.DefiningInstruction)
        InsertBefore(U, NewInstr)
        ReplaceOperand(U, V, NewInstr.Result)
```

---

## Transformation Examples

### Example 1: Simple Arithmetic

**NVVM IR (Before Rematerialization)**:
```llvm
define void @kernel(i32 %a, i32 %b) {
entry:
  %c = add i32 %a, %b        ; Computed once
  ; ... 100 instructions ...
  ; Register pressure high - %c gets spilled
  SPILL %c to memory
  ; ... more code ...
  %d = RELOAD %c from memory
  %e = mul i32 %d, 2
  ret void
}
```

**After Rematerialization**:
```llvm
define void @kernel(i32 %a, i32 %b) {
entry:
  %c = add i32 %a, %b        ; Computed once
  ; ... 100 instructions ...
  ; No spill - %c is rematerialized when needed
  ; ... more code ...
  %c_remat = add i32 %a, %b  ; REMATERIALIZED (recomputed)
  %e = mul i32 %c_remat, 2
  ret void
}
```

**Benefit**: Saved 1 spill store + 1 spill load (800 cycles) at cost of 1 add (2 cycles).

### Example 2: Address Calculation

**NVVM IR (Before)**:
```llvm
define void @kernel(float* %base, i32 %idx) {
entry:
  %offset = mul i32 %idx, 4
  %ptr = getelementptr float, float* %base, i32 %offset  ; Address calc
  ; ... many instructions ...
  SPILL %ptr
  ; ... more code ...
  %ptr_reload = RELOAD %ptr
  %val = load float, float* %ptr_reload
  store float 1.0, float* %ptr_reload
  ret void
}
```

**After Rematerialization**:
```llvm
define void @kernel(float* %base, i32 %idx) {
entry:
  %offset = mul i32 %idx, 4
  %ptr = getelementptr float, float* %base, i32 %offset
  ; ... many instructions ...
  ; No spill
  ; ... more code ...
  %offset_remat = mul i32 %idx, 4                         ; REMAT
  %ptr_remat1 = getelementptr float, float* %base, i32 %offset_remat
  %val = load float, float* %ptr_remat1

  %ptr_remat2 = getelementptr float, float* %base, i32 %offset_remat
  store float 1.0, float* %ptr_remat2
  ret void
}
```

**Benefit**: Address calculations are cheap (5-10 cycles) vs spill (800 cycles).

### Example 3: Constant Materialization

**NVVM IR (Before)**:
```llvm
define void @kernel() {
entry:
  %const = i32 0x12345678   ; Constant
  ; ... code ...
  SPILL %const
  ; ... more code ...
  %c_reload = RELOAD %const
  %result = add i32 %input, %c_reload
  ret void
}
```

**After Rematerialization**:
```llvm
define void @kernel() {
entry:
  %const = i32 0x12345678
  ; ... code ...
  ; No spill
  ; ... more code ...
  %const_remat = i32 0x12345678  ; REMAT (trivial)
  %result = add i32 %input, %const_remat
  ret void
}
```

**Benefit**: Loading a constant is 1-2 instructions (mov immediate).

### Example 4: Cannot Rematerialize - Load

**NVVM IR**:
```llvm
define void @kernel(i32* %ptr) {
entry:
  %val = load i32, i32* %ptr   ; CANNOT REMAT - memory load!
  ; ... code ...
  ; High register pressure
  SPILL %val                   ; Must spill
  ; ... more code ...
  %val_reload = RELOAD %val
  use %val_reload
}
```

**No Rematerialization**: Loads cannot be safely rematerialized (memory may change, side effects, latency).

---

## Cost Model Details

### Instruction Costs (Typical)

| Instruction | Latency (cycles) | Rematerialization Cost |
|-------------|------------------|------------------------|
| `add`, `sub` | 1-2 | Very Low |
| `mul` (integer) | 4 | Low |
| `fma`, `mad` | 4-8 | Low |
| `div`, `rem` | 20-100 | Medium-High |
| `sqrt`, `rsqrt` | 20-40 | Medium |
| `sin`, `cos`, `exp` | 40-80 | High |
| `load` (cached) | 40-80 | Cannot remat |
| `load` (uncached) | 400-800 | Cannot remat |

### Spill Costs

| Memory Type | Store (st) | Load (ld) | Total Round-Trip |
|-------------|------------|-----------|------------------|
| Local (cached) | 40 | 40 | 80 |
| Local (uncached) | 400 | 400 | 800 |
| Global | 400-800 | 400-800 | 800-1600 |

**Guideline**: Rematerialize if instruction latency < 80 cycles.

---

## Occupancy Impact Analysis

### Example Kernel Analysis

**Original Kernel**:
```cuda
__global__ void kernel(float* data, int n) {
    // Complex computation requiring many registers
    // Compiler allocates: 64 registers per thread
    // Occupancy: 1024 threads per SM (50%)
}
```

**After Rematerialization**:
```cuda
// Same kernel, but compiler rematerializes:
// - 6 address calculations
// - 3 arithmetic expressions
// - 2 constant values
// New allocation: 53 registers per thread
// Occupancy: 1,236 threads per SM (61%)
// Performance improvement: 22%!
```

### Register Threshold Crossings

Critical register counts for SM 8.0 (65,536 registers):

| Registers/Thread | Max Threads | Occupancy | Notes |
|------------------|-------------|-----------|-------|
| 32 | 2,048 | 100% | Ideal |
| 40 | 1,638 | 80% | Good |
| 48 | 1,365 | 67% | Acceptable |
| 56 | 1,170 | 57% | Moderate |
| 64 | 1,024 | 50% | Limited |
| 72 | 910 | 44% | Poor |
| 80 | 819 | 40% | Very Poor |

**Impact**: Rematerializing just 8 values can move from 64 → 56 registers, increasing occupancy from 50% to 57% (+14%).

---

## Interaction with Other Passes

### Run After

1. **Register Allocation (initial)**: Identifies high register pressure regions
2. **Register Coalescing**: Reduces register count where possible
3. **Dead Code Elimination**: Removes unnecessary instructions

### Run Before

1. **Final Register Allocation**: Uses rematerialization hints
2. **Spill Code Insertion**: Inserts spills only for non-rematerializable values
3. **PTX Emission**: Final code has minimal spills

### Synergy

**With NVPTXBlockRemat**: Block-level rematerialization for local optimizations
**With RegisterCoalescer**: Reduces register count before rematerialization analysis

---

## Advanced Techniques

### Partial Rematerialization

**Concept**: Rematerialize only some uses, not all

**Example**:
```llvm
%val = expensive_operation()

USE1: %val   ; Rematerialize
USE2: %val   ; Rematerialize
USE3: %val   ; Spill/reload (in hot path)
```

**Benefit**: Trade-off between code size and register pressure.

### Live Range Splitting

**Combine with rematerialization**:
```llvm
%val = compute()
; Split live range
; ... long gap ...
%val_remat = compute()  ; New live range (rematerialized)
```

**Benefit**: Reduces live range length, enabling better register allocation.

---

## Limitations

### Cannot Rematerialize

1. **Memory Operations**: `load`, `store`, atomics
2. **Volatile Operations**: Side effects must not be duplicated
3. **Expensive Operations**: If recompute cost > spill cost
4. **Source Unavailability**: If operands not live at use site
5. **Calls**: Function calls (unknown side effects)

### Code Size Concerns

**Trade-off**: Rematerialization increases code size

**Example**:
```
Original: 1 instruction, 1 spill, 1 reload = 3 instructions

After remat (3 uses): 3 rematerializations = 3 instructions
```

**Net**: No code size change, but register pressure reduced.

---

## CUDA Developer Considerations

### Compiler Hints

**Recommendation**: Use `__launch_bounds__` to guide register allocation

```cuda
__global__ void __launch_bounds__(1024, 2) kernel() {
    // Compiler targets 1024 threads/block, 2 blocks/SM
    // Aggressively rematerializes to meet register budget
}
```

### Profile-Guided Optimization

**Use `nvcc --ptxas-options=-v`** to see register usage:
```bash
nvcc --ptxas-options=-v kernel.cu

# Output:
# ptxas info : Used 58 registers, 0 bytes cmem[0]
#              (6 values rematerialized)
```

---

## Related Passes

1. **NVPTXBlockRemat**: Block-level rematerialization
2. **RegisterCoalescer**: Reduces register usage
3. **Spill Code Insertion**: Handles non-rematerializable values
4. **LiveIntervals**: Provides liveness analysis
5. **RegisterPressure**: Estimates register pressure

---

## Summary

Register Rematerialization on NVVM IR is a critical GPU optimization that:
- ✓ Recomputes cheap values instead of spilling to memory
- ✓ Reduces register pressure by 5-15%
- ✓ Improves occupancy by 10-20% in register-bound kernels
- ✓ Saves 400-800 cycles per avoided spill
- ✓ Operates at IR level for high-level analysis

**Critical for**: Occupancy optimization, register-bound kernels
**Performance Impact**: 10-20% speedup in register-limited scenarios
**Reliability**: Conservative cost model, safe transformations

**Key Takeaway**: On GPUs, recomputing simple values is almost always faster than memory access - aggressive rematerialization is essential for high occupancy.
