# NVPTX Lower Alloca

**Pass Type**: IR lowering pass
**LLVM Class**: `llvm::NVPTXLowerAlloca`
**Category**: Memory Operation Lowering
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: MEDIUM - Inferred from code generation patterns
**Pass Index**: Listed in optimization_pass_mapping.json

---

## Overview

NVPTXLowerAlloca converts LLVM `alloca` instructions (stack allocations) into explicit local memory operations suitable for PTX code generation. GPUs don't have a traditional stack, so allocas must be lowered to `.local` memory space with explicit addressing.

**Key Purpose**: Transform abstract stack allocations into concrete local memory allocations that map to PTX's `.local` address space.

**Critical for**: GPU code generation - GPUs lack hardware stack.

---

## GPU Memory Model

### PTX Local Memory

**PTX `.local` memory**:
- Thread-private address space
- Backed by off-chip DRAM (cached in L1/L2)
- Used for register spills and local arrays
- Much slower than registers (400-800 cycles uncached)

**Syntax**:
```ptx
.local .align 4 .b8 local_array[256];
```

### Why Lower Alloca?

**LLVM IR `alloca`**:
```llvm
%arr = alloca [64 x i32], align 16
```

**PTX has no stack** - must use `.local`:
```ptx
.local .align 16 .b8 __local_depot[256];  ; 64 * 4 bytes
```

---

## Lowering Strategy

### Phase 1: Identify All Allocas

```
Allocas = []

FOR each BasicBlock BB in Function:
    FOR each Instruction I in BB:
        IF I is AllocaInst:
            Allocas.add(I)
```

### Phase 2: Compute Total Local Memory

```
TotalLocalMem = 0

FOR each Alloca A in Allocas:
    Size = getAllocaSize(A)
    Alignment = getAllocaAlignment(A)

    // Align offset
    TotalLocalMem = alignUp(TotalLocalMem, Alignment)

    // Assign offset
    AllocaOffsets[A] = TotalLocalMem

    // Update total
    TotalLocalMem += Size
```

### Phase 3: Create Local Memory Declaration

```
IF TotalLocalMem > 0:
    // Create .local memory allocation
    LocalMemory = createLocalMemory(TotalLocalMem)

    // Emit PTX declaration (done later)
    // .local .align MAX_ALIGN .b8 __local_depot[TotalLocalMem];
```

### Phase 4: Replace Allocas with Offsets

```
FOR each Alloca A in Allocas:
    Offset = AllocaOffsets[A]

    // Create pointer to local memory at offset
    BasePtr = LocalMemory  ; Pointer to __local_depot
    Ptr = getelementptr i8, i8* BasePtr, i64 Offset

    // Replace all uses of alloca with offset pointer
    A.replaceAllUsesWith(Ptr)

    // Remove alloca
    A.eraseFromParent()
```

---

## Transformation Examples

### Example 1: Simple Array

**CUDA Source**:
```cuda
__device__ void func() {
    int arr[64];
    arr[0] = 42;
}
```

**Before Lowering**:
```llvm
define void @func() {
  %arr = alloca [64 x i32], align 4
  %ptr = getelementptr [64 x i32], [64 x i32]* %arr, i64 0, i64 0
  store i32 42, i32* %ptr
  ret void
}
```

**After Lowering**:
```llvm
define void @func() {
  ; Alloca removed - replaced with local memory offset

  ; %arr is now offset 0 in local memory
  %base = i8* @__local_depot
  %arr_ptr = bitcast i8* %base to [64 x i32]*

  %ptr = getelementptr [64 x i32], [64 x i32]* %arr_ptr, i64 0, i64 0
  store i32 42, i32* %ptr, addrspace(5)  ; Address space 5 = .local
  ret void
}
```

**PTX**:
```ptx
.func func() {
    .local .align 4 .b8 __local_depot[256];  ; 64 * 4 bytes
    .reg .u64 %rd<4>;
    .reg .u32 %r<4>;

    mov.u64 %rd0, __local_depot;  ; Base pointer
    mov.u32 %r0, 42;
    st.local.u32 [%rd0 + 0], %r0;  ; arr[0] = 42

    ret;
}
```

### Example 2: Multiple Allocas

**CUDA Source**:
```cuda
__device__ void func() {
    float a[32];     // 128 bytes
    int b[16];       // 64 bytes
    double c[8];     // 64 bytes, align 8
}
```

**Before Lowering**:
```llvm
define void @func() {
  %a = alloca [32 x float], align 4
  %b = alloca [16 x i32], align 4
  %c = alloca [8 x double], align 8
  ; ...
}
```

**After Lowering**:
```
Layout in local memory:
  [0-127]:   %a (128 bytes, align 4)
  [128-191]: %b (64 bytes, align 4)
  [192-255]: %c (64 bytes, align 8 → offset adjusted to 192)

Total: 256 bytes
```

**PTX**:
```ptx
.func func() {
    .local .align 8 .b8 __local_depot[256];

    ; %a at offset 0
    ; %b at offset 128
    ; %c at offset 192 (aligned to 8)
}
```

### Example 3: Dynamic Alloca

**CUDA Source**:
```cuda
__device__ void func(int n) {
    int arr[n];  // Variable-length array (VLA)
    // ...
}
```

**Before Lowering**:
```llvm
define void @func(i32 %n) {
  %arr = alloca i32, i32 %n, align 4  ; Dynamic size
  ; ...
}
```

**Challenge**: PTX requires static `.local` declarations!

**Lowering Strategy**:
1. **Conservative**: Allocate maximum possible size
2. **Fail**: Reject dynamic allocas (error)
3. **Global memory**: Fall back to `malloc`

**Typical Solution** (conservative):
```ptx
.func func(.param .u32 n) {
    .local .align 4 .b8 __local_depot[4096];  ; Max size
    ; Use only first (n * 4) bytes
}
```

---

## Address Space Management

### Address Space Transitions

**Alloca** (LLVM IR) → **Local Memory** (PTX):

```llvm
; Before lowering (address space 0 - generic)
%ptr = alloca i32, align 4

; After lowering (address space 5 - .local)
%ptr = getelementptr i8 addrspace(5)*, i8 addrspace(5)* @__local_depot, i64 0
```

**Address Space Cast**:
```llvm
; If generic pointer needed
%generic_ptr = addrspacecast i8 addrspace(5)* %ptr to i8*
```

---

## Interaction with Other Passes

### Run After

1. **NVPTXAllocaHoisting**: Moves allocas to entry block
2. **SROA**: Breaks apart aggregates (may eliminate allocas)
3. **Mem2Reg**: Promotes allocas to registers (eliminates allocas)

### Run Before

1. **NVPTXPrologEpilogPass**: Needs final local memory layout
2. **Code Generation**: Emits PTX `.local` declarations
3. **Register Allocation**: Needs to know local memory usage

### Preserved

**Control flow**, **loop structure**, **register operations**

---

## Performance Impact

### Local Memory Overhead

**Occupancy Impact**:
```
Max threads per SM = min(
    HW_limit,
    LocalMemory_per_SM / LocalMemory_per_thread
)
```

**Example** (SM 8.0 - Ampere):
- Total local memory: 164 KB per SM
- Function uses 1 KB local memory per thread
- Max threads: 164 KB / 1 KB = **164 threads**
- **Severe occupancy limitation!**

### Best Practices

**Minimize local memory**:
```cuda
// Bad - large local array
__device__ void func() {
    int large_array[1024];  // 4 KB local memory!
}

// Good - use shared memory or global memory
__device__ void func() {
    extern __shared__ int shared_array[];  // No local memory
}
```

---

## Related Passes

1. **NVPTXAllocaHoisting**: Moves allocas to entry
2. **SROA**: Eliminates unnecessary allocas
3. **Mem2Reg**: Promotes allocas to registers
4. **NVPTXPrologEpilogPass**: Finalizes frame layout

---

## Summary

NVPTXLowerAlloca converts stack allocations to GPU local memory, enabling PTX code generation.

**Critical for**: Code generation, GPU stack emulation
**Performance Impact**: Local memory usage affects occupancy
**Reliability**: Essential for correctness
