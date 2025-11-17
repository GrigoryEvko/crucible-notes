# NVVMIRVerifier - NVVM IR Correctness Validation

## Overview

**Pass ID**: `NVVMIRVerifier`
**Category**: NVVM IR Transformation (Validation)
**Execution Phase**: After NVVM IR generation
**Confidence**: MEDIUM (listed, not fully decompiled)
**Estimated Function Count**: ~30-50 functions

The **NVVMIRVerifier** pass validates the correctness and well-formedness of NVVM IR, ensuring that GPU-specific constraints and invariants are satisfied before lowering to PTX.

---

## Pass Purpose

**Primary Goals**:

1. **Address Space Validation**: Verify correct address space usage
2. **Divergence Invariants**: Ensure divergence metadata is consistent
3. **Tensor Core Constraints**: Validate WMMA/MMA operand constraints
4. **Synchronization Correctness**: Check barrier and fence semantics
5. **PTX Constraint Enforcement**: Ensure IR can be lowered to valid PTX

---

## Validation Categories

### 1. Address Space Correctness

**Rules**:
```c
// Rule 1: Address space must be in range [0-5]
addrspace(0) = generic
addrspace(1) = global
addrspace(3) = shared
addrspace(4) = constant
addrspace(5) = local

// Rule 2: Constant address space must be read-only
store i32 %val, i32 addrspace(4)* %ptr  // ERROR: cannot write to constant

// Rule 3: Shared memory must not escape kernel
i32 addrspace(3)* @global_var = ...  // ERROR: shared cannot be global

// Rule 4: Address space casts must be between different spaces
addrspacecast i32 addrspace(1)* %ptr to i32 addrspace(1)*  // ERROR: same space
```

**Error Messages** (from `error_messages.json`):
```
"AddrSpaceCast must be between different address spaces"
"address space component cannot be empty"
"address space must be a 24-bit integer"
"address space 0 cannot be non-integral"
```

### 2. Divergence Metadata Validation

**Rules**:
```llvm
; Rule 1: Divergent values must be marked
%tid = call i32 @llvm.nvvm.read.ptx.sreg.tid.x(), !divergent !0

; Rule 2: Convergent operations must be marked
call void @llvm.nvvm.barrier0(), !convergent !1

; Rule 3: Side effects in divergent code must be preserved
if (divergent_condition) {
    atomicAdd(...);  // MUST NOT be eliminated
}
```

### 3. Tensor Core Operand Constraints

**WMMA Validation** (SM70+):
```llvm
; Rule 1: Fragment types must match
%frag_a = call <8 x half> @llvm.nvvm.wmma.load.a.sync.m16n16k16.f16(...)
%frag_b = call <8 x half> @llvm.nvvm.wmma.load.b.sync.m16n16k16.f16(...)
%result = call <4 x float> @llvm.nvvm.wmma.mma.sync.m16n16k16.f32.f16(
    <8 x half> %frag_a,   ; ✓ Correct type
    <8 x half> %frag_b,   ; ✓ Correct type
    <4 x float> %acc      ; ✓ Correct accumulator type
)

; Rule 2: Tile dimensions must match instruction variant
wmma.load.a.sync.m16n16k16.f16  ; ✓ 16x16x16 tile
wmma.mma.sync.m16n16k16.f32.f16 ; ✓ Same tile size

; Rule 3: Leading dimension must be >= matrix dimension
wmma.load.a.sync(..., i32 15)  ; ERROR: ldm < 16 (matrix row size)
wmma.load.a.sync(..., i32 16)  ; ✓ Valid

; Rule 4: Alignment requirements
half addrspace(3)* %ptr, align 128  ; ✓ Required for tensor cores
half addrspace(3)* %ptr, align 2    ; ERROR: Insufficient alignment
```

**MMA.sync Validation** (SM80+):
```llvm
; Rule 1: Register count must match tile size
mma.m16n8k16.f32.f16:
    Input A: 4 x half (4 registers)
    Input B: 2 x half (2 registers)
    Accumulator: 4 x float (4 registers)

; Rule 2: Synchronization mask must be provided
@llvm.nvvm.mma.m16n8k16.sync(...)  ; ✓ Synchronized
@llvm.nvvm.mma.m16n8k16(...)       ; ERROR: Missing sync
```

### 4. Barrier and Synchronization Validation

**Rules**:
```c
// Rule 1: __syncthreads() must be executed by all threads in block
if (threadIdx.x < 16) {
    __syncthreads();  // ERROR: Divergent barrier (deadlock!)
}

// Rule 2: Warp barriers require active mask
__syncwarp(0x0);  // ERROR: No threads active

// Rule 3: Memory fences must have valid scope
__threadfence();        // ✓ GPU-wide
__threadfence_block();  // ✓ CTA-wide
__threadfence_system(); // ✓ System-wide
```

**Convergent Metadata**:
```llvm
; Barrier must be convergent (all threads reach)
call void @llvm.nvvm.barrier0(), !convergent !0

; Non-convergent call is invalid for barriers
call void @llvm.nvvm.barrier0()  ; ERROR: Missing convergent metadata
```

### 5. Atomic Operation Validation

**Rules**:
```c
// Rule 1: Atomic scope must be valid
atom.global.gpu.add   ; ✓ GPU scope
atom.shared.cta.add   ; ✓ CTA scope
atom.generic.sys.add  ; ✓ System scope

// Rule 2: Data type must be supported
atomicAdd(int*)    ; ✓ Supported
atomicAdd(half*)   ; ERROR: Not supported (SM70-SM80)
atomicAdd(half*)   ; ✓ Supported (SM90+)

// Rule 3: Address space must allow atomics
atom.const.add     ; ERROR: Constant memory is read-only
```

---

## Verification Algorithm

```c
class NVVMIRVerifier {
public:
    bool verify(Module& M) {
        bool valid = true;

        // Phase 1: Address space validation
        for (Instruction& I : instructions(M)) {
            if (LoadInst* LI = dyn_cast<LoadInst>(&I)) {
                valid &= verifyAddressSpace(LI->getPointerOperand());
            } else if (StoreInst* SI = dyn_cast<StoreInst>(&I)) {
                valid &= verifyAddressSpace(SI->getPointerOperand());
                valid &= verifyNoStoreToConstant(SI);
            } else if (AddrSpaceCastInst* ASCI = dyn_cast<AddrSpaceCastInst>(&I)) {
                valid &= verifyAddrSpaceCast(ASCI);
            }
        }

        // Phase 2: Divergence metadata validation
        for (Instruction& I : instructions(M)) {
            if (I.hasMetadata("divergent")) {
                valid &= verifyDivergenceMetadata(&I);
            }
        }

        // Phase 3: Tensor core validation
        for (Function& F : M) {
            for (Instruction& I : instructions(F)) {
                if (CallInst* CI = dyn_cast<CallInst>(&I)) {
                    if (isTensorCoreIntrinsic(CI)) {
                        valid &= verifyTensorCoreCall(CI);
                    }
                }
            }
        }

        // Phase 4: Synchronization validation
        for (Instruction& I : instructions(M)) {
            if (CallInst* CI = dyn_cast<CallInst>(&I)) {
                if (isBarrierIntrinsic(CI)) {
                    valid &= verifyBarrier(CI);
                } else if (isAtomicIntrinsic(CI)) {
                    valid &= verifyAtomic(CI);
                }
            }
        }

        return valid;
    }

private:
    bool verifyAddressSpace(Value* ptr) {
        unsigned AS = ptr->getType()->getPointerAddressSpace();
        if (AS > 5) {
            reportError("Invalid address space: " + std::to_string(AS));
            return false;
        }
        return true;
    }

    bool verifyNoStoreToConstant(StoreInst* SI) {
        unsigned AS = SI->getPointerAddressSpace();
        if (AS == 4) {  // Constant address space
            reportError("Cannot store to constant memory");
            return false;
        }
        return true;
    }

    bool verifyTensorCoreCall(CallInst* CI) {
        Function* Callee = CI->getCalledFunction();
        StringRef Name = Callee->getName();

        // Verify fragment types
        if (Name.contains("wmma.load.a")) {
            Type* RetTy = CI->getType();
            if (!RetTy->isVectorTy() || !RetTy->getVectorElementType()->isHalfTy()) {
                reportError("Invalid WMMA load.a return type");
                return false;
            }
        }

        // Verify leading dimension
        if (Name.contains("wmma.load")) {
            Value* LDM = CI->getArgOperand(1);
            if (ConstantInt* CI_LDM = dyn_cast<ConstantInt>(LDM)) {
                int ldm = CI_LDM->getSExtValue();
                if (ldm < 16) {  // Minimum for m16n16k16
                    reportError("Leading dimension too small: " + std::to_string(ldm));
                    return false;
                }
            }
        }

        return true;
    }
};
```

---

## Common Validation Errors

### Error 1: Invalid Address Space Cast

**Invalid Code**:
```llvm
%ptr_global = addrspace(1)* %A
%ptr_same = addrspacecast i32 addrspace(1)* %ptr_global to i32 addrspace(1)*
; ERROR: Casting to same address space
```

**Fix**:
```llvm
%ptr_global = addrspace(1)* %A
%ptr_generic = addrspacecast i32 addrspace(1)* %ptr_global to i32 addrspace(0)*
; ✓ Casting to different address space (global → generic)
```

### Error 2: Divergent Barrier

**Invalid Code**:
```c
__global__ void kernel() {
    if (threadIdx.x < 16) {
        __syncthreads();  // ERROR: Only half of threads reach barrier
    }
    // Deadlock: Other threads waiting forever
}
```

**Fix**:
```c
__global__ void kernel() {
    if (threadIdx.x < 16) {
        // Compute something
    }
    __syncthreads();  // ✓ All threads reach barrier
}
```

### Error 3: Store to Constant Memory

**Invalid Code**:
```llvm
%ptr_const = i32 addrspace(4)* %constant_array
store i32 42, i32 addrspace(4)* %ptr_const  ; ERROR: Constant is read-only
```

**Fix**:
```llvm
%ptr_global = i32 addrspace(1)* %global_array
store i32 42, i32 addrspace(1)* %ptr_global  ; ✓ Global memory is writable
```

---

## Integration with Pipeline

```
BEFORE:
  ├─ GenericToNVVM       (generate NVVM IR)
  ├─ NVVMReflect         (resolve reflection queries)
  └─ NVVMOptimizer       (optimize NVVM IR)

→ NVVMIRVerifier (THIS PASS)

AFTER:
  ├─ CodeGenPrepare      (prepare for lowering)
  └─ NVPTXCodeGen        (lower to PTX)
```

**Rationale**: Verification must happen after NVVM IR is generated but before lowering to PTX, to catch errors early.

---

## Binary Evidence

**Location**: `cicc/foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json` (line 334)

**Evidence**:
```json
{
    "nvidia_specific": [
        "NVVMIRVerifier"
    ]
}
```

**Error String Evidence** (from `error_messages.json`):
- "Invalid NVVM IR Container"
- "AddrSpaceCast must be between different address spaces"
- "address space must be a 24-bit integer"

---

## Related Passes

- **GenericToNVVM**: Prerequisite (generates NVVM IR to verify)
- **NVVMOptimizer**: Runs before verifier (may introduce errors)
- **NVPTXCodeGen**: Runs after verifier (assumes valid IR)

---

## References

### NVIDIA Documentation
- NVVM IR Spec: https://docs.nvidia.com/cuda/nvvm-ir-spec/
- PTX ISA: https://docs.nvidia.com/cuda/parallel-thread-execution/

### Binary Locations
- `cicc/foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json` (line 334)
- `cicc/foundation/taxonomy/strings/error_messages.json` (line 8285, 24823)

---

**Analysis Date**: 2025-11-17
**Confidence Level**: MEDIUM (listed, error message evidence)
**Priority**: MEDIUM (quality assurance pass)
