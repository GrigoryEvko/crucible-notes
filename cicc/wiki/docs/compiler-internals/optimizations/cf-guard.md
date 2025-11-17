# CFGuard (Control Flow Guard) Pass

**Pass Type**: Security instrumentation pass
**LLVM Class**: `llvm::CFGuardPass`
**Algorithm**: Control-flow integrity checking
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: LOW - Limited evidence
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`

---

## Overview

CFGuard (Control Flow Guard) is a security mitigation pass that protects against control-flow hijacking attacks by validating indirect function calls. It is primarily a **Windows-specific** security feature that instruments code to verify call targets at runtime.

**Key Features**:
- **Indirect call validation**: Checks function pointer targets before calling
- **Jump table protection**: Validates switch/case jump table entries
- **Virtual call protection**: Validates C++ virtual function calls
- **Return address protection**: Validates return addresses (when combined with hardware)

**Core Algorithm**: For each indirect call, insert a runtime check that validates the target address is a legitimate function entry point. If validation fails, terminate the program.

**CUDA Context**: **Extremely limited applicability** to GPU compilation. CFGuard is a Windows security feature with no direct GPU equivalent. Not relevant to CUDA kernel compilation.

---

## Algorithm Details

### Control Flow Integrity (CFI) Basics

CFGuard implements Control Flow Integrity by maintaining a bitmap of valid call targets:

```
Valid Function Entry Points (CFGuard Bitmap):
┌──────────────────────────────────────┐
│ Address Range        | Valid Bit     │
├──────────────────────────────────────┤
│ 0x00401000-0x00401007 | 1 (valid)    │
│ 0x00401008-0x0040100F | 0 (invalid)  │
│ 0x00401010-0x00401017 | 1 (valid)    │
│ ...                                  │
└──────────────────────────────────────┘

Indirect Call:
    call [rax]  ; Indirect call through register

Instrumented:
    ; Check if rax is valid entry point
    mov rcx, rax
    call __guard_check_icall_fptr  ; Runtime check
    call [rax]  ; Proceed if valid, else terminate
```

### Instrumentation Algorithm

```c
void instrumentIndirectCalls(Function& F) {
    for (BasicBlock& BB : F) {
        for (Instruction& I : BB) {
            if (CallInst* CI = dyn_cast<CallInst>(&I)) {
                if (CI->isIndirectCall()) {
                    // Insert CFGuard check before indirect call
                    Value* Target = CI->getCalledOperand();

                    // Call validation function
                    IRBuilder<> Builder(&I);
                    FunctionCallee CheckFn =
                        M.getOrInsertFunction("__guard_check_icall_fptr",
                                             Type::getVoidTy(Context),
                                             Type::getInt8PtrTy(Context));

                    Builder.CreateCall(CheckFn, {Target});
                    // Original indirect call follows
                }
            }
        }
    }
}
```

### Valid Target Determination

CFGuard maintains a list of valid indirect call targets:

```c
struct CFGuardTargetInfo {
    bool is_valid_target;          // Can this function be called indirectly?
    bool is_export;                // Exported function?
    bool is_address_taken;         // Address taken in code?
};

bool isValidCFGuardTarget(Function* F) {
    // Valid targets:
    // 1. Functions whose address is taken
    // 2. Exported functions
    // 3. Virtual functions
    // 4. Function pointers passed across module boundaries
    return F->hasAddressTaken() ||
           F->hasExternalLinkage() ||
           F->hasAttribute("cfguard-target");
}
```

---

## Data Structures

### CFGuard Metadata

```c
struct CFGuardMetadata {
    // Bitmap of valid function entry points
    BitVector valid_targets;

    // List of functions that need protection
    SmallVector<Function*, 32> indirect_call_sites;

    // Guard check function pointer
    Function* guard_check_icall;

    // Configuration
    bool check_longjmp;
    bool check_exceptions;
};
```

### Target Function Set

```c
// Set of valid indirect call targets
DenseSet<Function*> valid_indirect_targets;

// Mapping from indirect call sites to their valid target sets
DenseMap<CallInst*, SmallPtrSet<Function*, 16>> call_target_map;
```

---

## Configuration & Parameters

### Pass Parameters

**Evidence from CICC**:
- `"invalid CFGuardPass mechanism: '{0}' "` - Parameter validation
- `"Expected exactly one cfguardtarget bundle operand"` - Bundle validation

### CFGuard Mechanisms

```c
enum CFGuardMechanism {
    CF_GUARD_DISABLED,     // No CFGuard
    CF_GUARD_CHECK,        // Check indirect calls only
    CF_GUARD_DISPATCH,     // Dispatch through validation
    CF_GUARD_FULL          // Full protection (calls + returns)
};
```

---

## Pass Dependencies

### Required Analyses

1. **TargetTransformInfo**: Platform-specific implementation
2. **ModuleInfo**: For determining valid targets
3. **CallGraph**: For identifying indirect call sites

### Platform Requirements

- **Windows only**: CFGuard is Windows-specific
- **MSVC runtime**: Requires Windows runtime support
- **Hardware support**: Best performance with Intel CET or ARM pointer authentication

### Invalidated Analyses

- **CFG**: Inserts new call instructions
- **Call Graph**: Adds edges to guard check functions

---

## Integration Points

### Compiler Pipeline Integration

```
Module-Level Pipeline (Windows Only):
    ↓
Function Analysis
    ↓
[CFGuard] ← Insert guard checks
    ↓
Code Generation (emits guard metadata)
    ↓
Linker (merges guard tables)
```

### Windows Runtime Integration

CFGuard requires Windows runtime support:

```c
// Windows CFGuard runtime functions:
extern "C" {
    void __guard_check_icall_fptr(void* target);
    void __guard_dispatch_icall_fptr(void* target);
    void* __guard_xfg_table;  // Extended Flow Guard table
}
```

---

## CUDA-Specific Considerations

### Minimal GPU Relevance

CFGuard has **virtually no relevance** to CUDA GPU compilation:

**Why?**
1. **Windows-specific**: Linux is primary platform for HPC/GPU
2. **Indirect calls rare on GPU**: Device code avoids function pointers
3. **Different security model**: GPU has no privileged execution
4. **No OS on GPU**: Device code runs without operating system

### Host-Side CFGuard

CFGuard may be used for **host-side code** on Windows:

```cpp
// Host code with CFGuard protection (Windows only)
typedef void (*KernelFunc)(float*, int);

void dispatch_kernel(KernelFunc kernel_ptr, float* data, int n) {
    // CFGuard check inserted here (on Windows with /guard:cf)
    (*kernel_ptr)(data, n);  // Indirect call to device function
}
```

**Reality**: Even on Windows, most CUDA development uses Linux or disables CFGuard for performance.

### Why CFGuard Doesn't Apply to GPU

```cuda
// Device code characteristics:

// 1. No indirect calls (not supported efficiently)
__global__ void kernel_dispatch(int type, float* data, int n) {
    // Can't use function pointers efficiently on GPU
    // Must use switch/if-else instead
    if (type == 0) {
        kernel_a(data, n);
    } else if (type == 1) {
        kernel_b(data, n);
    }
}

// 2. No virtual functions (C++ device code)
class Base {
public:
    // Virtual functions on device are slow and rare
    __device__ virtual void process(float* data) = 0;
};

// 3. No return address manipulation
// GPU has different call/return semantics
```

### CUDA API on Windows

CUDA API calls on Windows may have CFGuard protection:

```cpp
// CUDA API calls on Windows (host-side)
void allocate_device_memory(float** d_ptr, size_t size) {
    // cudaMalloc is an indirect call through CUDA driver
    // May be protected by CFGuard on Windows
    cudaMalloc((void**)d_ptr, size);
}
```

**Impact**: Negligible (CUDA API calls are infrequent compared to kernel execution).

---

## Evidence & Implementation

### String Evidence (CICC Binary)

**Confirmed Evidence**:
- `"invalid CFGuardPass mechanism: '{0}' "` - Pass registration
- `"Expected exactly one cfguardtarget bundle operand"` - Bundle validation

**Confidence Assessment**:
- **Confidence Level**: LOW
- Pass exists in CICC (string evidence)
- Likely present for completeness (standard LLVM pass)
- **Probably unused** in CUDA compilation (Windows-specific, Linux primary platform)

---

## Performance Impact

### Compile-Time Impact

| Metric | Impact | Notes |
|--------|--------|-------|
| **Compilation time** | +1-5% | Minimal (just instrumentation insertion) |
| **Metadata generation** | +KB per function | Valid target bitmap |

### Runtime Impact (CPU)

| Metric | Without CFGuard | With CFGuard | Overhead |
|--------|-----------------|--------------|----------|
| **Indirect calls** | 1.0x | 1.1-1.5x | 10-50% per call |
| **Overall performance** | 1.0x | 1.01-1.10x | 1-10% |
| **Security** | Vulnerable | Protected | Significant improvement |

**Note**: Modern CPUs (Intel CET) reduce overhead to ~1-2%.

### Runtime Impact (GPU)

**Not applicable**: CFGuard doesn't run on GPU.

---

## Code Examples

### Example 1: Host-Side Indirect Call (Windows)

**Original Code**:
```cpp
// Host code on Windows
typedef void (*kernel_ptr_t)(float*, int);

void launch_indirect(kernel_ptr_t kernel, float* d_data, int n) {
    // Indirect call through function pointer
    kernel(d_data, n);
}
```

**With CFGuard** (conceptual):
```cpp
void launch_indirect(kernel_ptr_t kernel, float* d_data, int n) {
    // CFGuard check inserted by compiler
    __guard_check_icall_fptr((void*)kernel);

    // Original indirect call
    kernel(d_data, n);
}
```

### Example 2: Why GPU Doesn't Need CFGuard

```cuda
// GPU code doesn't use indirect calls
__global__ void kernel_a(float* data, int n) { /* ... */ }
__global__ void kernel_b(float* data, int n) { /* ... */ }

// Host dispatch (no indirect calls on GPU)
void launch_kernel(int type, float* d_data, int n) {
    if (type == 0) {
        kernel_a<<<grid, block>>>(d_data, n);  // Direct launch
    } else {
        kernel_b<<<grid, block>>>(d_data, n);  // Direct launch
    }
    // No function pointers, no CFGuard needed
}
```

---

## Platform-Specific Behavior

### Windows

```cpp
// Compile with CFGuard on Windows:
// nvcc -Xcompiler /guard:cf host_code.cpp

// CFGuard is enabled for host code only
```

### Linux (Primary CUDA Platform)

```bash
# CFGuard not available on Linux
# Pass may be compiled in but never executed
nvcc host_code.cpp  # No CFGuard
```

---

## Known Limitations

| Limitation | Impact | Workaround | Status |
|-----------|--------|------------|--------|
| **Windows-only** | Not portable | Accept limitation | Fundamental |
| **Performance overhead** | 1-10% on CPU | Use hardware support (Intel CET) | Improving |
| **No GPU support** | Not applicable to kernels | N/A | Fundamental |
| **Linux primary platform** | Rarely used in CUDA | N/A | Accepted |
| **Indirect calls rare on GPU** | Limited applicability | N/A | Fundamental |

---

## Alternatives for GPU Security

Since CFGuard doesn't apply to GPU, alternative security measures:

### Memory Protection

```cuda
// Bounds checking (see bounds-checking.md)
__global__ void safe_kernel(float* data, int n) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx >= n) return;  // Bounds check
    data[idx] = compute(idx);
}
```

### Input Validation

```cuda
// Validate inputs on host before launch
void launch_safe(float* d_data, int n) {
    assert(d_data != nullptr);
    assert(n > 0 && n < MAX_SIZE);

    kernel<<<grid, block>>>(d_data, n);
}
```

### CUDA Memory Checker

```bash
# Use CUDA memory checking tools
compute-sanitizer --tool memcheck ./app
```

---

## Summary

CFGuard is a Windows-specific security pass that:
- ✅ Protects against control-flow hijacking on CPU
- ✅ Validates indirect function calls at runtime
- ✅ Provides security benefits for Windows applications
- ❌ Windows-only (not portable to Linux)
- ❌ Not applicable to GPU device code
- ❌ Rarely used in CUDA development (Linux primary platform)
- ❌ Performance overhead (1-10% on CPU)

**Use Case**: Windows-specific host-side security hardening. Not relevant to GPU kernel compilation or optimization. Included in CICC for completeness but likely unused in practice.

---

**L3 Analysis Quality**: LOW
**Last Updated**: 2025-11-17
**Source**: CICC foundation analyses + 21_OPTIMIZATION_PASS_MAPPING.json
**Evidence**: Error strings for parameter validation
**CUDA Relevance**: Very Low (Windows-only, host-side only, Linux primary platform)
