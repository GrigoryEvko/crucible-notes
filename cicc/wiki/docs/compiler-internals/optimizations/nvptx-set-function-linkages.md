# NVPTXSetFunctionLinkages - PTX Function Visibility Configuration

## Overview

**Pass ID**: `NVPTXSetFunctionLinkages`
**Category**: NVPTX Code Generation
**Execution Phase**: Early backend (before lowering)
**Confidence**: MEDIUM (listed)

The **NVPTXSetFunctionLinkages** pass configures function linkage types to match PTX visibility and calling requirements. This pass determines whether functions are kernel entry points, device functions, or externally visible symbols.

---

## Pass Purpose

**Primary Goals**:

1. **Entry Point Marking**: Identify and mark `__global__` kernel entry points
2. **Device Function Configuration**: Set linkage for `__device__` functions
3. **External Visibility**: Configure externally linkable symbols
4. **Internal Optimization**: Mark internal-only functions for optimization

---

## PTX Linkage Types

### Function Visibility in PTX

| CUDA Qualifier | PTX Linkage | PTX Declaration | Callable From | Visibility |
|----------------|-------------|-----------------|---------------|------------|
| `__global__` | `.entry` | `.visible .entry kernel()` | Host (CPU) | External |
| `__device__` (public) | `.func` | `.visible .func device()` | Device | External |
| `__device__` (private) | `.func` | `.func internal_device()` | Same module | Internal |
| `__host__ __device__` | Special | Compiled twice | Both | Dual |

---

## Transformation Examples

### Example 1: Kernel Entry Point

**CUDA Source**:
```c
__global__ void vector_add(float* A, float* B, float* C, int N) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx < N) {
        C[idx] = A[idx] + B[idx];
    }
}
```

**NVVM IR Before**:
```llvm
define void @vector_add(float addrspace(1)* %A, float addrspace(1)* %B,
                        float addrspace(1)* %C, i32 %N) {
    ; Linkage: default (external)
    ; Calling convention: default
}
```

**NVVM IR After**:
```llvm
define void @vector_add(float addrspace(1)* %A, float addrspace(1)* %B,
                        float addrspace(1)* %C, i32 %N)
       #0 {  ; Attribute: kernel entry point
    ; Linkage: visible
    ; Calling convention: ptx_kernel
}

attributes #0 = { "kernel" "nvptx-entry" }
```

**PTX Output**:
```ptx
.visible .entry vector_add(
    .param .u64 vector_add_param_0,  // float* A
    .param .u64 vector_add_param_1,  // float* B
    .param .u64 vector_add_param_2,  // float* C
    .param .u32 vector_add_param_3   // int N
) {
    // Kernel body
    ret;
}
```

### Example 2: Device Function (Public)

**CUDA Source**:
```c
__device__ float compute_helper(float x, float y) {
    return x * x + y * y;
}

__global__ void kernel(float* data) {
    float val = compute_helper(data[0], data[1]);
    // ...
}
```

**NVVM IR Before**:
```llvm
define float @compute_helper(float %x, float %y) {
    ; Linkage: default
}
```

**NVVM IR After**:
```llvm
define float @compute_helper(float %x, float %y)
       #1 {  ; Attribute: device function
    ; Linkage: visible (externally callable)
    ; Calling convention: ptx_device
}

attributes #1 = { "device-function" }
```

**PTX Output**:
```ptx
.visible .func (.param .b32 func_retval0) compute_helper(
    .param .b32 compute_helper_param_0,  // float x
    .param .b32 compute_helper_param_1   // float y
) {
    .reg .f32 %f<4>;
    ld.param.f32 %f1, [compute_helper_param_0];
    ld.param.f32 %f2, [compute_helper_param_1];
    mul.f32 %f3, %f1, %f1;
    fma.rn.f32 %f4, %f2, %f2, %f3;
    st.param.f32 [func_retval0], %f4;
    ret;
}
```

### Example 3: Device Function (Private/Internal)

**CUDA Source**:
```c
// Static device function (internal linkage)
static __device__ float internal_compute(float x) {
    return x * 2.0f;
}

__global__ void kernel(float* data) {
    data[0] = internal_compute(data[0]);
}
```

**NVVM IR After**:
```llvm
define internal float @internal_compute(float %x)
       #2 {  ; Attribute: internal device function
    ; Linkage: internal (not visible externally)
    ; Can be inlined or optimized away
}

attributes #2 = { "device-function" "internal" }
```

**PTX Output**:
```ptx
// Internal function may be inlined or not emitted if optimized away
.func (.param .b32 func_retval0) internal_compute(
    .param .b32 internal_compute_param_0
) {
    // Function body (if not inlined)
    ret;
}

// Or inlined directly into kernel:
.visible .entry kernel(...) {
    .reg .f32 %f1;
    ld.global.f32 %f1, [data];
    mul.f32 %f1, %f1, 0f40000000;  // x * 2.0f (inlined)
    st.global.f32 [data], %f1;
    ret;
}
```

---

## Linkage Configuration Algorithm

```c
void NVPTXSetFunctionLinkages::run(Module& M) {
    for (Function& F : M) {
        // Check if function is kernel entry point
        if (F.getCallingConv() == CallingConv::PTX_Kernel) {
            F.setLinkage(GlobalValue::ExternalLinkage);
            F.setVisibility(GlobalValue::DefaultVisibility);
            F.addFnAttr("kernel");
            F.addFnAttr("nvptx-entry");
        }
        // Check if function is device function
        else if (F.getCallingConv() == CallingConv::PTX_Device) {
            if (F.hasInternalLinkage() || F.hasPrivateLinkage()) {
                // Keep internal linkage for optimization
                F.setLinkage(GlobalValue::InternalLinkage);
                F.addFnAttr("device-function");
                F.addFnAttr("internal");
            } else {
                // External device function
                F.setLinkage(GlobalValue::ExternalLinkage);
                F.setVisibility(GlobalValue::DefaultVisibility);
                F.addFnAttr("device-function");
            }
        }
        // Check if function is __host__ __device__ (dual target)
        else if (F.hasMetadata("nvvm.annotations")) {
            // Handle dual-target functions
            configureDualTargetFunction(F);
        }
    }
}
```

---

## PTX Calling Convention Impact

### Kernel Calling Convention (`.entry`)

**Characteristics**:
- Called from host (CPU) via CUDA runtime
- Cannot return values
- Cannot be called from device code
- Parameters passed via `.param` space

**PTX Example**:
```ptx
.visible .entry kernel(.param .u64 ptr, .param .u32 size) {
    .reg .u64 %rd<2>;
    .reg .u32 %r<2>;
    ld.param.u64 %rd1, [ptr];
    ld.param.u32 %r1, [size];
    // Kernel body
    ret;  // No return value
}
```

### Device Function Calling Convention (`.func`)

**Characteristics**:
- Called from device code (kernels or other device functions)
- Can return values via `.param` space
- Can be inlined
- Supports recursion (with stack)

**PTX Example**:
```ptx
.func (.param .b32 retval) device_func(.param .b32 arg0, .param .b32 arg1) {
    .reg .f32 %f<3>;
    ld.param.f32 %f1, [arg0];
    ld.param.f32 %f2, [arg1];
    add.f32 %f3, %f1, %f2;
    st.param.f32 [retval], %f3;
    ret;
}

// Calling the function:
.reg .f32 %result;
{
    .param .b32 param0;
    .param .b32 param1;
    .param .b32 retval0;
    st.param.f32 [param0], %input1;
    st.param.f32 [param1], %input2;
    call (retval0), device_func, (param0, param1);
    ld.param.f32 %result, [retval0];
}
```

---

## Performance Implications

### Internal vs External Linkage

| Linkage | Inlining | Optimization | Code Size | Use Case |
|---------|----------|--------------|-----------|----------|
| **Internal** | Aggressive | Full | Smaller (inlined) | Static device functions |
| **External** | Limited | Conservative | Larger (call overhead) | Public device functions |

**Example Impact**:
```c
// Internal: Can be fully inlined
static __device__ float square(float x) { return x * x; }

// External: Preserved as separate function
__device__ float public_square(float x) { return x * x; }
```

**PTX Output** (internal inlined):
```ptx
.entry kernel(...) {
    .reg .f32 %f1, %f2;
    ld.global.f32 %f1, [data];
    mul.f32 %f2, %f1, %f1;  // Inlined square()
    st.global.f32 [result], %f2;
}
```

**PTX Output** (external function call):
```ptx
.visible .func (.param .b32 retval) public_square(.param .b32 arg) {
    .reg .f32 %f1, %f2;
    ld.param.f32 %f1, [arg];
    mul.f32 %f2, %f1, %f1;
    st.param.f32 [retval], %f2;
    ret;
}

.entry kernel(...) {
    .param .b32 param0, retval0;
    st.param.f32 [param0], %input;
    call (retval0), public_square, (param0);  // Function call overhead
    ld.param.f32 %result, [retval0];
}
```

**Call Overhead**: ~10-20 cycles for function call/return

---

## Binary Evidence

**Location**: `cicc/foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json` (line 339)

---

## Related Passes

- **NVPTXLowerArgs**: Lowers function arguments (runs after)
- **NVPTXCopyByValArgs**: Handles by-value parameters (runs after)
- **AlwaysInliner**: May inline internal functions (prerequisite)

---

## References

### NVIDIA Documentation
- PTX ISA: https://docs.nvidia.com/cuda/parallel-thread-execution/index.html#function-declaration

---

**Analysis Date**: 2025-11-17
**Confidence Level**: MEDIUM (listed, PTX semantics known)
**Priority**: HIGH (essential for correct PTX generation)
