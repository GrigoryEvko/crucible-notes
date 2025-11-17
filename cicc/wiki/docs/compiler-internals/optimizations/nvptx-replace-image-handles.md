# NVPTX Replace Image Handles

**Pass Type**: Texture/surface operation lowering pass
**LLVM Class**: `llvm::NVPTXReplaceImageHandles`
**Category**: Texture/Surface Optimization
**String Evidence**: "NVPTX Image Optimizer" (optimization_passes.json:26535)
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: MEDIUM - Inferred from texture operation patterns
**Pass Index**: Listed in optimization_pass_mapping.json

---

## Overview

NVPTXReplaceImageHandles transforms high-level CUDA texture and surface references into low-level PTX texture/surface handles suitable for hardware texture units. This pass manages the translation between CUDA's abstract texture objects and PTX's explicit handle-based texture operations.

**Key Purpose**: Convert CUDA texture/surface objects into PTX-compatible 64-bit handles for hardware texture unit access.

**Critical for**: Texture and surface memory operations, bindless textures.

---

## CUDA Texture/Surface Model

### CUDA Texture Objects

**CUDA API**:
```cuda
// Create texture object
cudaTextureObject_t texObj;
cudaCreateTextureObject(&texObj, &resDesc, &texDesc, NULL);

// Use in kernel
__global__ void kernel(cudaTextureObject_t tex) {
    float val = tex2D<float>(tex, x, y);
}
```

**LLVM IR** (abstract):
```llvm
define void @kernel(i64 %tex) {
  %val = call float @llvm.nvvm.tex.unified.2d.f32.s32(
      i64 %tex, i32 %x, i32 %y
  )
}
```

**PTX** (explicit handle):
```ptx
.func kernel(.param .u64 tex_param) {
    .reg .u64 %tex_handle;
    .reg .f32 %val;
    .reg .s32 %x, %y;

    ld.param.u64 %tex_handle, [tex_param];
    tex.2d.v4.f32.s32 {%val, _, _, _}, [%tex_handle, {%x, %y}];
}
```

---

## Texture/Surface Handle Types

### Texture Handles

**64-bit opaque handles**:
```cpp
typedef unsigned long long cudaTextureObject_t;  // 64-bit
```

**Represents**:
- Texture descriptor (format, filtering, addressing)
- Memory resource (array, linear memory)
- Sampling parameters (wrap mode, filter mode)

### Surface Handles

**64-bit opaque handles**:
```cpp
typedef unsigned long long cudaSurfaceObject_t;  // 64-bit
```

**Represents**:
- Surface descriptor (format, dimensions)
- Memory resource (read/write access)

---

## Algorithm

### Phase 1: Identify Image Operations

```
ImageOps = []

FOR each CallInst CI in Function:
    IF isTextureIntrinsic(CI):
        ImageOps.add({CI, type: TEXTURE})
    ELSE IF isSurfaceIntrinsic(CI):
        ImageOps.add({CI, type: SURFACE})
```

**Texture Intrinsics**:
- `llvm.nvvm.tex.1d.*`
- `llvm.nvvm.tex.2d.*`
- `llvm.nvvm.tex.3d.*`
- `llvm.nvvm.tex.cubemap.*`

**Surface Intrinsics**:
- `llvm.nvvm.suld.1d.*` (surface load)
- `llvm.nvvm.sust.1d.*` (surface store)
- `llvm.nvvm.suld.2d.*`, `llvm.nvvm.sust.2d.*`
- `llvm.nvvm.suld.3d.*`, `llvm.nvvm.sust.3d.*`

### Phase 2: Extract Handle Parameters

```
FOR each ImageOp IO in ImageOps:
    Handle = getHandleOperand(IO)

    IF Handle is constant:
        // Static texture/surface reference
        ReplaceWithDirectHandle(IO, Handle)
    ELSE:
        // Dynamic handle (bindless texture)
        EnsureHandleInRegister(IO, Handle)
```

### Phase 3: Replace Intrinsic Calls

```
FOR each ImageOp IO in ImageOps:
    NewIntrinsic = selectPTXIntrinsic(IO)
    NewCall = createCall(NewIntrinsic, IO.Args)

    IO.replaceAllUsesWith(NewCall)
    IO.eraseFromParent()
```

---

## Transformation Examples

### Example 1: 2D Texture Read

**CUDA Source**:
```cuda
__global__ void kernel(cudaTextureObject_t tex) {
    int x = threadIdx.x;
    int y = threadIdx.y;
    float val = tex2D<float>(tex, x, y);
}
```

**Before Handle Replacement** (Generic NVVM):
```llvm
define void @kernel(i64 %tex) {
  %x = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()
  %y = call i32 @llvm.nvvm.read.ptx.sreg.tid.y()

  ; Generic texture call
  %val = call float @llvm.nvvm.tex.unified.2d.f32.s32(
      i64 %tex, i32 %x, i32 %y
  )

  ; use %val...
}
```

**After Handle Replacement** (PTX-ready):
```llvm
define void @kernel(i64 %tex) {
  %x = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()
  %y = call i32 @llvm.nvvm.read.ptx.sreg.tid.y()

  ; Explicit handle-based texture intrinsic
  %result_vec = call <4 x float> @llvm.nvvm.tex.2d.v4f32.s32(
      i64 %tex, i32 %x, i32 %y
  )
  %val = extractelement <4 x float> %result_vec, i32 0

  ; use %val...
}
```

**PTX**:
```ptx
.func kernel(.param .u64 tex_param) {
    .reg .u64 %tex_handle;
    .reg .f32 %f<4>;
    .reg .s32 %x, %y;

    ld.param.u64 %tex_handle, [tex_param];

    mov.u32 %x, %tid.x;
    mov.u32 %y, %tid.y;

    tex.2d.v4.f32.s32 {%f0, %f1, %f2, %f3}, [%tex_handle, {%x, %y}];
    ; Use %f0 (red channel)
}
```

### Example 2: Surface Write

**CUDA Source**:
```cuda
__global__ void kernel(cudaSurfaceObject_t surf) {
    int x = threadIdx.x;
    surf1Dwrite(42, surf, x);
}
```

**Before**:
```llvm
define void @kernel(i64 %surf) {
  %x = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()

  call void @llvm.nvvm.suq.unified.1d.i32(
      i64 %surf, i32 %x, i32 42
  )
}
```

**After**:
```llvm
define void @kernel(i64 %surf) {
  %x = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()

  call void @llvm.nvvm.sust.b.1d.i32.trap(
      i64 %surf, i32 %x, i32 42
  )
}
```

**PTX**:
```ptx
.func kernel(.param .u64 surf_param) {
    .reg .u64 %surf_handle;
    .reg .s32 %x, %val;

    ld.param.u64 %surf_handle, [surf_param];
    mov.u32 %x, %tid.x;
    mov.s32 %val, 42;

    sust.b.1d.b32.trap [%surf_handle, {%x}], {%val};
}
```

---

## Handle Management

### Static vs Bindless Textures

**Static Texture** (legacy):
```cuda
texture<float, 2> tex_ref;  // File scope

__global__ void kernel() {
    float val = tex2D(tex_ref, x, y);
}
```

**Bindless Texture** (modern):
```cuda
__global__ void kernel(cudaTextureObject_t tex) {
    float val = tex2D<float>(tex, x, y);
}
```

**Handle Replacement**: Bindless textures pass handles as parameters.

### Handle Validation

**Runtime Checks** (optional):
```ptx
ld.param.u64 %tex_handle, [tex_param];

; Optional validation
setp.eq.u64 %p, %tex_handle, 0;
@%p bra ERROR;  ; Handle is null

; Use handle
tex.2d.v4.f32.s32 {%f0, %f1, %f2, %f3}, [%tex_handle, {%x, %y}];
```

---

## Texture Intrinsic Mapping

### Common Mappings

| CUDA Function | NVVM Intrinsic | PTX Instruction |
|---------------|----------------|-----------------|
| `tex1D<float>` | `llvm.nvvm.tex.1d.v4f32.s32` | `tex.1d.v4.f32.s32` |
| `tex2D<float>` | `llvm.nvvm.tex.2d.v4f32.s32` | `tex.2d.v4.f32.s32` |
| `tex3D<float>` | `llvm.nvvm.tex.3d.v4f32.s32` | `tex.3d.v4.f32.s32` |
| `surf1Dread<int>` | `llvm.nvvm.suld.1d.i32.trap` | `suld.b.1d.b32.trap` |
| `surf2Dwrite<int>` | `llvm.nvvm.sust.2d.i32.trap` | `sust.b.2d.b32.trap` |

---

## Performance Impact

### Texture Cache Efficiency

**Benefit**: Hardware texture units with dedicated cache
- L1 texture cache: ~90% hit rate for spatial locality
- Filtered reads: Hardware bilinear/trilinear interpolation

**No Direct Performance Impact** from this pass - it's a lowering transformation.

---

## Related Passes

1. **NVPTXGenericToNVVM**: Creates initial texture intrinsics
2. **Instruction Selection**: Maps intrinsics to PTX instructions
3. **NVPTXAssignValidGlobalNames**: Handles texture symbol names

---

## Summary

NVPTXReplaceImageHandles converts abstract texture/surface operations into PTX-compatible handle-based intrinsics.

**Critical for**: Texture/surface operations, bindless textures
**Performance Impact**: Indirect - enables hardware texture units
**Reliability**: Essential for texture functionality
