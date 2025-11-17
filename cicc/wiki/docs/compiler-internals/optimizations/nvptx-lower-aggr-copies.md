# NVPTX Lower Aggregate Copies

**Pass Type**: IR lowering pass
**LLVM Class**: `llvm::NVPTXLowerAggrCopies`
**Category**: Memory Operation Lowering
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: MEDIUM - Inferred from lowering patterns
**Pass Index**: Listed in optimization_pass_mapping.json

---

## Overview

NVPTXLowerAggrCopies converts high-level aggregate copy operations (structure copies, array copies, `memcpy`/`memmove` calls) into explicit element-by-element stores or vector operations suitable for PTX code generation. This is necessary because PTX does not have a native "copy structure" instruction.

**Key Purpose**: Transform abstract aggregate copies into explicit memory operations that map directly to PTX load/store instructions.

---

## Problem Statement

### LLVM IR Aggregates

**LLVM supports high-level aggregate operations**:

```llvm
; Structure copy
%dest_struct = load %struct.Point, %struct.Point* %src_struct
store %struct.Point %dest_struct, %struct.Point* %dst_struct

; Array copy
call void @llvm.memcpy.p0i8.p0i8.i64(
    i8* %dest, i8* %src, i64 1024, i1 false
)

; Structure by-value
call void @func(%struct.Point %p)  ; Pass 16-byte struct by value
```

### PTX Limitations

**PTX only has scalar and small vector loads/stores**:

```ptx
ld.global.u32 %r0, [%addr];          // Scalar
ld.global.v4.u32 {%r0,%r1,%r2,%r3}, [%addr];  // Vector (max v4)

// NO native structure copy instruction!
// NO native memcpy instruction!
```

**Solution**: Lower to explicit load/store sequences.

---

## Lowering Strategies

### Strategy 1: Small Structure Copy → Load/Store Sequence

**Small structures (≤ 16 bytes)**: Expand inline

**Before Lowering**:
```llvm
%struct.Point = type { float, float, float, float }  ; 16 bytes

%src = load %struct.Point, %struct.Point* %src_ptr
store %struct.Point %src, %struct.Point* %dest_ptr
```

**After Lowering**:
```llvm
; Expand to field-by-field copies
%gep0 = getelementptr %struct.Point, %struct.Point* %src_ptr, i32 0, i32 0
%val0 = load float, float* %gep0

%gep1 = getelementptr %struct.Point, %struct.Point* %src_ptr, i32 0, i32 1
%val1 = load float, float* %gep1

%gep2 = getelementptr %struct.Point, %struct.Point* %src_ptr, i32 0, i32 2
%val2 = load float, float* %gep2

%gep3 = getelementptr %struct.Point, %struct.Point* %src_ptr, i32 0, i32 3
%val3 = load float, float* %gep3

%dest_gep0 = getelementptr %struct.Point, %struct.Point* %dest_ptr, i32 0, i32 0
store float %val0, float* %dest_gep0

%dest_gep1 = getelementptr %struct.Point, %struct.Point* %dest_ptr, i32 0, i32 1
store float %val1, float* %dest_gep1

%dest_gep2 = getelementptr %struct.Point, %struct.Point* %dest_ptr, i32 0, i32 2
store float %val2, float* %dest_gep2

%dest_gep3 = getelementptr %struct.Point, %struct.Point* %dest_ptr, i32 0, i32 3
store float %val3, float* %dest_gep3
```

**PTX Result**:
```ptx
ld.global.f32 %f0, [%src + 0];
ld.global.f32 %f1, [%src + 4];
ld.global.f32 %f2, [%src + 8];
ld.global.f32 %f3, [%src + 12];

st.global.f32 [%dest + 0], %f0;
st.global.f32 [%dest + 4], %f1;
st.global.f32 [%dest + 8], %f2;
st.global.f32 [%dest + 12], %f3;
```

**Optimization**: May vectorize into `ld.v4.f32` / `st.v4.f32`.

### Strategy 2: Large Structure Copy → Loop

**Large structures (> threshold)**: Generate copy loop

**Before Lowering**:
```llvm
call void @llvm.memcpy.p0i8.p0i8.i64(
    i8* %dest, i8* %src, i64 4096, i1 false  ; 4KB copy
)
```

**After Lowering**:
```llvm
; Generate loop for copy
br label %copy_loop

copy_loop:
  %i = phi i64 [0, %entry], [%i_next, %copy_loop]

  %src_addr = getelementptr i8, i8* %src, i64 %i
  %dest_addr = getelementptr i8, i8* %dest, i64 %i

  %val = load i64, i64* %src_addr, align 8  ; 8-byte chunks
  store i64 %val, i64* %dest_addr, align 8

  %i_next = add i64 %i, 8
  %done = icmp ult i64 %i_next, 4096
  br i1 %done, label %copy_loop, label %copy_end

copy_end:
  ret void
```

**PTX Result** (conceptual):
```ptx
MOV %i, 0;
LOOP:
    LD.GLOBAL.U64 %val, [%src + %i];
    ST.GLOBAL.U64 [%dest + %i], %val;
    ADD %i, %i, 8;
    SETP.LT %p, %i, 4096;
    @%p BRA LOOP;
```

### Strategy 3: Vectorized Copy

**Aligned structures**: Use vector operations

**Example** (16-byte aligned structure):
```llvm
; Before
call void @llvm.memcpy.p0i8.p0i8.i64(
    i8* align 16 %dest, i8* align 16 %src, i64 64, i1 false
)

; After (vectorized)
%src_v4 = bitcast i8* %src to <4 x i32>*
%dest_v4 = bitcast i8* %dest to <4 x i32>*

%vec0 = load <4 x i32>, <4 x i32>* %src_v4, align 16
store <4 x i32> %vec0, <4 x i32>* %dest_v4, align 16

%src_v4_1 = getelementptr <4 x i32>, <4 x i32>* %src_v4, i64 1
%dest_v4_1 = getelementptr <4 x i32>, <4 x i32>* %dest_v4, i64 1
%vec1 = load <4 x i32>, <4 x i32>* %src_v4_1, align 16
store <4 x i32> %vec1, <4 x i32>* %dest_v4_1, align 16

; ... 2 more vector loads/stores for 64 bytes total
```

**PTX Result**:
```ptx
ld.global.v4.u32 {%r0,%r1,%r2,%r3}, [%src + 0];
st.global.v4.u32 [%dest + 0], {%r0,%r1,%r2,%r3};

ld.global.v4.u32 {%r4,%r5,%r6,%r7}, [%src + 16];
st.global.v4.u32 [%dest + 16], {%r4,%r5,%r6,%r7};
```

---

## Algorithm

### Phase 1: Identify Aggregate Copies

```
AggrCopies = []

FOR each Instruction I in Function:
    IF I is aggregate load/store:
        AggrCopies.add(I)
    ELSE IF I is memcpy/memmove intrinsic:
        AggrCopies.add(I)
    ELSE IF I is byval parameter:
        AggrCopies.add(I)
```

### Phase 2: Classify Copy Size

```
FOR each Copy C in AggrCopies:
    Size = getCopySize(C)
    Alignment = getAlignment(C)

    IF Size <= SMALL_THRESHOLD (e.g., 32 bytes):
        Strategy = INLINE_EXPAND
    ELSE IF Size <= MEDIUM_THRESHOLD (e.g., 256 bytes):
        IF Alignment >= 16:
            Strategy = VECTORIZED
        ELSE:
            Strategy = UNROLLED_LOOP
    ELSE:
        Strategy = RUNTIME_LOOP
```

### Phase 3: Perform Lowering

```
FOR each Copy C, Strategy S:
    SWITCH S:
        CASE INLINE_EXPAND:
            ExpandInline(C)
        CASE VECTORIZED:
            VectorizeAndExpand(C)
        CASE UNROLLED_LOOP:
            GenerateUnrolledLoop(C)
        CASE RUNTIME_LOOP:
            GenerateRuntimeLoop(C)

    // Remove original copy
    C.eraseFromParent()
```

---

## Transformation Examples

### Example 1: Structure Assignment

**CUDA Source**:
```cuda
struct Vec3 {
    float x, y, z;
};

__device__ void copy(Vec3* dest, Vec3* src) {
    *dest = *src;  // Structure assignment
}
```

**Before Lowering**:
```llvm
%struct.Vec3 = type { float, float, float }

define void @copy(%struct.Vec3* %dest, %struct.Vec3* %src) {
  %tmp = load %struct.Vec3, %struct.Vec3* %src
  store %struct.Vec3 %tmp, %struct.Vec3* %dest
  ret void
}
```

**After Lowering**:
```llvm
define void @copy(%struct.Vec3* %dest, %struct.Vec3* %src) {
  %x_ptr = getelementptr %struct.Vec3, %struct.Vec3* %src, i32 0, i32 0
  %x = load float, float* %x_ptr

  %y_ptr = getelementptr %struct.Vec3, %struct.Vec3* %src, i32 0, i32 1
  %y = load float, float* %y_ptr

  %z_ptr = getelementptr %struct.Vec3, %struct.Vec3* %src, i32 0, i32 2
  %z = load float, float* %z_ptr

  %dest_x = getelementptr %struct.Vec3, %struct.Vec3* %dest, i32 0, i32 0
  store float %x, float* %dest_x

  %dest_y = getelementptr %struct.Vec3, %struct.Vec3* %dest, i32 0, i32 1
  store float %y, float* %dest_y

  %dest_z = getelementptr %struct.Vec3, %struct.Vec3* %dest, i32 0, i32 2
  store float %z, float* %dest_z

  ret void
}
```

### Example 2: memcpy

**CUDA Source**:
```cuda
__device__ void copyArray(int* dest, int* src) {
    memcpy(dest, src, 64);  // 64 bytes = 16 ints
}
```

**Before Lowering**:
```llvm
define void @copyArray(i32* %dest, i32* %src) {
  %dest_i8 = bitcast i32* %dest to i8*
  %src_i8 = bitcast i32* %src to i8*
  call void @llvm.memcpy.p0i8.p0i8.i64(
      i8* %dest_i8, i8* %src_i8, i64 64, i1 false
  )
  ret void
}
```

**After Lowering** (unrolled):
```llvm
define void @copyArray(i32* %dest, i32* %src) {
  ; Unrolled 16 iterations
  %val0 = load i32, i32* %src, align 4
  store i32 %val0, i32* %dest, align 4

  %src1 = getelementptr i32, i32* %src, i64 1
  %dest1 = getelementptr i32, i32* %dest, i64 1
  %val1 = load i32, i32* %src1, align 4
  store i32 %val1, i32* %dest1, align 4

  ; ... 14 more load/store pairs ...

  ret void
}
```

---

## Performance Impact

### Memory Transaction Efficiency

**Vectorization Benefit**:
- Scalar: 4 transactions for 16 bytes
- Vector (v4): 1 transaction for 16 bytes
- **4x fewer transactions**

### Occupancy Impact

**Reduced Register Pressure**:
- Aggregate copies don't hold intermediate values
- Explicit loads/stores can be optimized
- Better register allocation

---

## Interaction with Other Passes

### Run After

1. **SROA**: May eliminate some aggregate copies
2. **Inlining**: Exposes more copy opportunities
3. **SimplifyCFG**: Simplifies control flow

### Run Before

1. **MemorySpaceOptimization**: Needs explicit loads/stores
2. **Instruction Selection**: Generates PTX instructions
3. **Register Allocation**: Needs scalar operations

---

## Related Passes

1. **SROA**: Breaks apart aggregates
2. **MemCpyOpt**: Optimizes memory copy patterns
3. **NVPTXLowerAlloca**: Lowers allocas to local memory
4. **InstCombine**: Simplifies resulting load/store patterns

---

## Summary

NVPTXLowerAggrCopies lowers aggregate copies into explicit element-wise operations suitable for PTX code generation.

**Critical for**: Structure handling, memcpy support, PTX emission
**Performance Impact**: Enables vectorization, reduces transactions
**Reliability**: Essential for correctness
