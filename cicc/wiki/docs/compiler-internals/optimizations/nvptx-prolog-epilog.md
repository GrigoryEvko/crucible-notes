# NVPTX Prolog/Epilog Insertion Pass

**Pass Type**: Machine-level code generation pass
**LLVM Class**: `llvm::NVPTXPrologEpilogPass`
**Category**: Code Generation / Frame Management
**String Evidence**: "NVPTX Prolog Epilog Pass" (optimization_passes.json:26605)
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: HIGH - String evidence confirmed
**Pass Index**: Listed in optimization_pass_mapping.json

---

## Overview

NVPTXPrologEpilogPass inserts function prologs and epilogs in NVIDIA PTX code, handling:
- Stack frame setup and teardown (for local memory)
- Callee-saved register spilling and restoration
- Dynamic stack allocation (if needed)
- Frame pointer management (rare in GPU code)

**Key Difference from CPU**: GPUs have different calling conventions and minimal stack usage, so this pass is simpler than CPU equivalents but must handle GPU-specific concerns like local memory allocation.

**Critical Function**: Finalizes the function's execution frame before PTX emission.

---

## GPU-Specific Frame Layout

### PTX Memory Model for Functions

PTX functions can use three types of "frame" memory:

| Memory Type | PTX Space | Usage | Managed By |
|-------------|-----------|-------|------------|
| **Registers** | `.reg` | Primary storage | Register allocator |
| **Local Memory** | `.local` | Spills, large locals | This pass |
| **Parameters** | `.param` | Arguments, returns | Calling convention |

**Key Point**: Unlike CPUs, GPUs rarely use traditional stack frames. Most data stays in registers.

### Minimal Prolog/Epilog

**Simple GPU Function** (no local memory):
```ptx
.func (.param .u32 ret) add(.param .u32 a, .param .u32 b) {
    .reg .u32 %r<4>;

    // NO PROLOG NEEDED - just load parameters
    ld.param.u32 %r0, [a];
    ld.param.u32 %r1, [b];

    // Function body
    add.u32 %r2, %r0, %r1;

    // NO EPILOG NEEDED - just store result
    st.param.u32 [ret], %r2;
    ret;
}
```

**Complex Function** (with local memory):
```ptx
.func kernel() {
    .local .align 16 .b8 __local_depot[1024];  // Frame setup
    .reg .u32 %r<32>;

    // PROLOG: Initialize local memory if needed
    mov.u64 %rd0, __local_depot;  // Frame base pointer

    // Function body
    // ...

    // EPILOG: Cleanup (minimal - PTX handles most automatically)
    ret;
}
```

---

## Algorithm

### Phase 1: Analyze Frame Requirements

**Scan function to determine**:

```
TotalLocalMemory = 0
SpillSlots = []
DynamicAlloca = false

FOR each MachineBasicBlock MBB:
    FOR each MachineInstr MI:
        IF MI is stack alloc:
            TotalLocalMemory += allocSize(MI)
        IF MI is register spill:
            SpillSlots.add(getSpillSlot(MI))
        IF MI is dynamic alloca:
            DynamicAlloca = true
```

**Calculations**:
- Total local memory needed (bytes)
- Alignment requirements (typically 4, 8, or 16 bytes)
- Spill slot offsets
- Callee-saved registers to preserve (rare in GPU code)

### Phase 2: Allocate Local Memory Frame

**Determine frame layout**:

```
FrameLayout:
  [0...N]       : Spill slots for registers
  [N...M]       : Local allocas
  [M...K]       : Dynamic allocations (if any)
  Alignment: max(4, requiredAlignment)

TotalFrameSize = K
```

**Example**:
```
Function needs:
  - 128 bytes for spills (32 registers * 4 bytes)
  - 256 bytes for local array
  - Align to 16 bytes

Frame Layout:
  [0-127]:   Spill slots (16-byte aligned)
  [128-383]: Local array (256 bytes)
  Total: 384 bytes
```

### Phase 3: Insert Prolog Code

**At function entry**:

```
IF TotalFrameSize > 0:
    // Declare local memory space
    .local .align A .b8 __local_depot[TotalFrameSize];

    // Optional: Initialize frame pointer
    IF function uses frame pointer:
        mov.u64 %frame_ptr, __local_depot;
```

**PTX Example**:
```ptx
.func kernel() {
    // PROLOG INSERTION
    .local .align 16 .b8 __local_depot[384];
    .reg .u64 %rd<8>;
    .reg .u32 %r<32>;

    // Frame pointer (if needed)
    mov.u64 %rd0, __local_depot;

    // ... function body ...
}
```

### Phase 4: Insert Epilog Code

**Before each return**:

```
FOR each Return instruction:
    // In PTX, most cleanup is automatic
    // Only need to ensure proper return value handling

    IF function returns value:
        st.param [ret_param], %return_val

    ret  // PTX runtime handles local memory cleanup
```

**Key Difference**: PTX automatically reclaims `.local` memory on return - no explicit deallocation needed.

### Phase 5: Fixup Spill Instructions

**Update spill/reload offsets**:

```
FOR each Spill instruction:
    Offset = getSpillOffset(SpillSlot)
    ReplaceWith: st.local [%frame_ptr + Offset], %reg

FOR each Reload instruction:
    Offset = getSpillOffset(SpillSlot)
    ReplaceWith: ld.local %reg, [%frame_ptr + Offset]
```

---

## Transformation Examples

### Example 1: Register Spills

**Before Prolog/Epilog**:
```
; Machine IR (abstract)
BB0:
  %vreg0 = LOAD ...
  SPILL %vreg0 -> FrameIndex#0    ; Abstract spill
  ...
  %vreg0 = RELOAD FrameIndex#0    ; Abstract reload
  USE %vreg0
```

**After Prolog/Epilog**:
```ptx
.func kernel() {
    // PROLOG: Allocate local memory for spills
    .local .align 4 .b8 __local_depot[128];  // Enough for spills
    .reg .u64 %rd_frame;
    .reg .u32 %r<16>;

    mov.u64 %rd_frame, __local_depot;

    // Function body
    ld.global.u32 %r0, [...];
    st.local.u32 [%rd_frame + 0], %r0;  // SPILL at offset 0
    // ...
    ld.local.u32 %r0, [%rd_frame + 0];  // RELOAD from offset 0
    // use %r0

    ret;
}
```

### Example 2: Local Array Allocation

**LLVM IR**:
```llvm
define void @kernel() {
entry:
  %arr = alloca [64 x i32], align 16  ; 256 bytes
  %ptr = getelementptr [64 x i32], [64 x i32]* %arr, i32 0, i32 10
  store i32 42, i32* %ptr
  ret void
}
```

**After Prolog/Epilog** (PTX):
```ptx
.func kernel() {
    // PROLOG: Allocate local memory
    .local .align 16 .b8 __local_depot[256];
    .reg .u64 %rd<4>;
    .reg .u32 %r<4>;

    mov.u64 %rd0, __local_depot;  // Base pointer

    // Access arr[10]
    mov.u64 %rd1, %rd0;
    add.u64 %rd1, %rd1, 40;  // Offset: 10 * 4 bytes
    mov.u32 %r0, 42;
    st.local.u32 [%rd1], %r0;

    // EPILOG: Implicit cleanup
    ret;
}
```

### Example 3: Function with No Frame

**LLVM IR**:
```llvm
define i32 @simple(i32 %a, i32 %b) {
  %sum = add i32 %a, %b
  ret i32 %sum
}
```

**After Prolog/Epilog** (no changes needed):
```ptx
.func (.param .u32 ret) simple(.param .u32 a, .param .u32 b) {
    .reg .u32 %r<3>;

    // NO PROLOG - no local memory needed
    ld.param.u32 %r0, [a];
    ld.param.u32 %r1, [b];
    add.u32 %r2, %r0, %r1;
    st.param.u32 [ret], %r2;

    // NO EPILOG - automatic cleanup
    ret;
}
```

---

## Calling Convention Integration

### Parameter Passing

**Prolog must handle incoming parameters**:

```ptx
.func (.param .u32 ret) foo(
    .param .u32 p0,
    .param .u32 p1,
    .param .align 16 .b8 p2[64]  // Aggregate parameter
) {
    .reg .u32 %r<8>;
    .reg .u64 %rd<4>;
    .local .align 16 .b8 __local_depot[64];  // Copy for aggregate

    // PROLOG: Load parameters
    ld.param.u32 %r0, [p0];
    ld.param.u32 %r1, [p1];

    // Copy aggregate parameter to local memory
    mov.u64 %rd0, __local_depot;
    // ... copy loop or memcpy ...

    // ... function body ...
}
```

### Return Value Handling

**Epilog must store return value**:

```ptx
.func (.param .u32 ret) compute() {
    .reg .u32 %r<4>;

    // ... computation ...
    mov.u32 %r0, 42;  // Result

    // EPILOG: Store return value
    st.param.u32 [ret], %r0;
    ret;
}
```

---

## Spill Slot Management

### Spill Slot Allocation

**Algorithm for assigning spill slots**:

```
SpillSlots = {}
NextOffset = 0

FOR each VirtualReg in SpilledRegisters:
    Size = getRegisterSize(VirtualReg)
    Align = getRegisterAlignment(VirtualReg)

    // Align offset
    NextOffset = alignUp(NextOffset, Align)

    // Assign slot
    SpillSlots[VirtualReg] = {offset: NextOffset, size: Size}
    NextOffset += Size
```

**Example**:
```
Spilled registers:
  %vreg0: i32 (4 bytes, align 4)
  %vreg1: i64 (8 bytes, align 8)
  %vreg2: i32 (4 bytes, align 4)

Allocation:
  %vreg0 -> offset 0 (size 4)
  NextOffset = 4
  NextOffset aligned to 8 = 8
  %vreg1 -> offset 8 (size 8)
  NextOffset = 16
  %vreg2 -> offset 16 (size 4)

Total frame size: 20 bytes (rounded up for alignment)
```

### Coalescing Adjacent Spills

**Optimization**: Merge consecutive spills

```
Before:
  st.local.u32 [%frame + 0], %r0;
  st.local.u32 [%frame + 4], %r1;

After (if possible):
  // Pack into vector store
  st.local.v2.u32 [%frame + 0], {%r0, %r1};
```

**Benefit**: Fewer memory transactions, better performance.

---

## Dynamic Stack Allocation

### Handling Dynamic Alloca

**LLVM IR**:
```llvm
define void @dynamic(i32 %n) {
  %size = mul i32 %n, 4
  %arr = alloca i8, i32 %size  ; Dynamic size!
  ; ...
}
```

**Challenge**: PTX requires static `.local` declarations.

**Solution**: Pre-allocate maximum or use alternative strategy

**Strategy 1: Conservative Allocation**:
```ptx
.func dynamic(.param .u32 n) {
    .local .align 4 .b8 __local_depot[4096];  // Max possible
    .reg .u32 %r<8>;
    .reg .u64 %rd<4>;

    ld.param.u32 %r0, [n];
    mul.u32 %r1, %r0, 4;  // Actual size needed

    mov.u64 %rd0, __local_depot;
    // Use only first %r1 bytes of __local_depot
}
```

**Strategy 2: Global Memory Fallback**:
```ptx
// For very large or unbounded dynamic allocations,
// fall back to malloc/free from global memory
call (%ptr), malloc, (%size);
// ... use %ptr ...
call free, (%ptr);
```

---

## Interaction with Register Allocation

### Frame Size Affects Occupancy

**Occupancy Formula**:
```
MaxThreads = min(
    HardwareLimit,
    RegisterLimit,
    LocalMemoryLimit
)

LocalMemoryLimit = TotalLocalMemory / LocalMemoryPerThread
```

**Example** (SM 8.0):
- Total local memory per SM: 164 KB
- Function uses 1 KB local memory per thread
- Max threads = 164 KB / 1 KB = 164 threads
- **Severe occupancy limitation!**

**Prolog/Epilog Impact**: Accurate frame size calculation is critical for occupancy.

### Register Spilling Trade-off

**Dilemma**:
- More spills → larger local memory frame → lower occupancy
- Fewer spills → more registers → lower occupancy

**Prolog/Epilog Role**: Reports accurate frame size to help make this trade-off visible.

---

## Optimization Opportunities

### Shrink-Wrapping (Advanced)

**Concept**: Only allocate frame where needed, not entire function

**Standard Approach**:
```ptx
.func foo() {
    .local .b8 frame[256];  // Allocated for entire function

    if (rare_condition) {
        // Use frame
    }
    // Frame wasted for common path
}
```

**Shrink-Wrapped** (advanced):
```ptx
.func foo() {
    // No frame initially

    if (rare_condition) {
        .local .b8 frame[256];  // Allocate only here
        // Use frame
    }
    // No frame overhead on common path
}
```

**Status in NVPTX**: Limited support - PTX requires static `.local` declarations.

### Dead Slot Elimination

**Remove unused spill slots**:

```
Allocated slots: [0-63] for 16 registers
After optimization: Only 8 registers actually spilled

Eliminate dead slots → reduce frame to [0-31]
```

---

## Interaction with Other Passes

### Run After

1. **Register Allocation**: Determines which registers are spilled
2. **NVPTXProxyRegisterErasure**: Reduces register pressure
3. **Register Coalescing**: Minimizes copies and spills
4. **Frame Index Elimination**: Replaces abstract frame indices with concrete offsets

### Run Before

1. **PTX Emission**: Needs final `.local` declarations
2. **Final Code Optimization**: Some peephole optimizations
3. **Debug Info Emission**: Frame layout needed for debugger

### Preserved Analyses

- Control Flow Graph (CFG)
- Dominator Tree
- Loop Information

---

## Performance Considerations

### Local Memory Latency

**PTX Local Memory Performance**:
- Latency: ~400-800 cycles (similar to global memory)
- Cached in L1 (if enabled) - reduces latency to ~80 cycles
- **Much slower than registers** (~1 cycle)

**Implication**: Minimize frame size to reduce spills.

### Metrics

| Frame Size | Impact |
|------------|--------|
| 0 bytes | Ideal - no local memory overhead |
| 1-256 bytes | Acceptable - small overhead |
| 256-1024 bytes | Moderate - may affect occupancy |
| > 1024 bytes | Severe - significant occupancy loss |

---

## Debugging and Diagnostics

### PTX Inspection

**View generated PTX**:
```bash
nvcc -ptx -o kernel.ptx kernel.cu
cat kernel.ptx
```

**Look for**:
```ptx
.local .align A .b8 __local_depot[SIZE];
```

**SIZE indicates frame overhead**.

### NVCC Verbose Output

```bash
nvcc --ptxas-options=-v kernel.cu

# Output:
# ptxas info : Used 48 registers, 256 bytes lmem
#                                  ^^^^ Frame size
```

### CICC Debug Flags

**Hypothetical debug flag**:
```bash
cicc -debug-prolog-epilog kernel.ll -o kernel.ptx

# Debug output:
# Prolog/Epilog: Function 'kernel'
#   Frame size: 256 bytes
#   Alignment: 16 bytes
#   Spill slots: 8
#   Local allocas: 1 (128 bytes)
#   Dynamic alloca: No
```

---

## Edge Cases

### Recursive Functions

**Problem**: PTX does not support recursion well

**Prolog/Epilog Handling**:
- Allocates frame normally
- **BUT**: Stack depth is limited
- Compiler may warn or error on deep recursion

**Example**:
```ptx
.func recursive(.param .u32 n) {
    .local .b8 frame[64];  // Each recursion level allocates
    // WARNING: Deep recursion will exhaust local memory!
}
```

### Exception Handling (Unsupported)

**PTX has no exception handling** - no special epilog code for exceptions.

### Tail Calls

**Optimization**: Eliminate frame for tail calls

```llvm
define i32 @tail_call(i32 %x) {
  %result = tail call i32 @other(i32 %x)
  ret i32 %result
}
```

**Prolog/Epilog**: Can eliminate frame entirely - jump directly to `@other`.

---

## Related Passes

1. **RegisterAllocation**: Determines spill requirements
2. **FrameIndexElimination**: Converts frame indices to concrete addresses
3. **NVPTXLowerAlloca**: Prepares allocas for frame allocation
4. **NVPTXAllocaHoisting**: Moves allocas to entry block (simplifies framing)
5. **RegisterCoalescing**: Reduces register pressure and spills

---

## Summary

NVPTXPrologEpilogPass is the final machine-level pass that:
- ✓ Allocates and manages the local memory frame
- ✓ Inserts `.local` declarations in PTX
- ✓ Assigns spill slot offsets
- ✓ Handles parameter passing and return values
- ✓ Manages dynamic allocations (conservatively)

**Critical for**: Code generation, local memory management, occupancy optimization
**Performance Impact**: Frame size directly affects occupancy
**Reliability**: Well-tested, handles edge cases conservatively

**Key Takeaway**: Unlike CPU compilers, GPU prolog/epilog is minimal - most functions have no frame overhead due to register-heavy execution model.
