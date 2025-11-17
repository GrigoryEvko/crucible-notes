# NVPTX Backend Passes - Overview

## Overview

The **NVPTX** passes are backend-specific transformations that operate during the lowering phase from NVVM IR to PTX assembly. These passes prepare code for final PTX emission and handle architecture-specific constraints.

---

## NVPTX Pass Categories

### 1. Function/Global Setup Passes

- **NVPTXSetFunctionLinkages**: Configure function visibility and linkage for PTX
- **NVPTXSetGlobalArrayAlignment**: Ensure global arrays meet alignment requirements
- **NVPTXSetLocalArrayAlignment**: Optimize local array alignment for performance

### 2. ABI and Lowering Passes

- **NVPTXCopyByValArgs**: Handle by-value function parameter passing
- **NVPTXCtorDtorLowering**: Lower global constructors/destructors for GPU
- **NVPTXLowerArgs**: Transform function arguments to PTX calling convention

### 3. Specialized Optimization Passes

- **NVPTXImageOptimizer**: Optimize texture and surface memory operations
- **NVVMIntrRange**: Propagate value range information for NVVM intrinsics

---

## Execution Order in Backend

```
NVVM IR (from NVVMOptimizer)
    ↓
┌─────────────────────────────────┐
│ NVPTX Backend Entry             │
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ 1. NVPTXSetFunctionLinkages     │ Set linkage (entry, device, etc.)
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ 2. NVPTXSetGlobalArrayAlignment │ Align global arrays
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ 3. NVPTXSetLocalArrayAlignment  │ Align local arrays
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ 4. NVPTXCopyByValArgs           │ Copy by-value parameters
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ 5. NVPTXCtorDtorLowering        │ Lower global ctors/dtors
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ 6. NVPTXLowerArgs               │ Lower function arguments
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ 7. NVVMIntrRange                │ Propagate intrinsic ranges
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ 8. NVPTXImageOptimizer          │ Optimize texture/surface ops
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ Instruction Selection           │
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ Register Allocation             │
└─────────────────────────────────┘
    ↓
┌─────────────────────────────────┐
│ PTX Emission                    │
└─────────────────────────────────┘
    ↓
PTX Assembly Output
```

---

## Common Characteristics

All NVPTX passes share these characteristics:

1. **Backend-Specific**: Operate on LLVM IR but with PTX-specific semantics
2. **Architecture-Aware**: Adapt behavior based on target SM version
3. **ABI Compliance**: Ensure PTX calling convention requirements
4. **Performance-Critical**: Directly impact generated PTX quality

---

## Pass Details

### Individual Pass Documentation

1. [NVPTXSetFunctionLinkages](nvptx-set-function-linkages.md)
2. [NVPTXSetGlobalArrayAlignment](nvptx-set-global-array-alignment.md)
3. [NVPTXSetLocalArrayAlignment](nvptx-set-local-array-alignment.md)
4. [NVPTXCopyByValArgs](nvptx-copy-byval-args.md)
5. [NVPTXCtorDtorLowering](nvptx-ctor-dtor-lowering.md)
6. [NVPTXLowerArgs](nvptx-lower-args.md)
7. [NVVMIntrRange](nvvm-intr-range.md)
8. [NVPTXImageOptimizer](nvptx-image-optimizer.md)

---

## Binary Evidence

**Location**: `cicc/foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json` (lines 339-345)

```json
{
    "nvidia_specific": [
        "NVPTXSetFunctionLinkages",
        "NVPTXSetGlobalArrayAlignment",
        "NVPTXSetLocalArrayAlignment",
        "NVPTXCopyByValArgs",
        "NVPTXCtorDtorLowering",
        "NVPTXLowerArgs",
        "NVPTXImageOptimizer"
    ]
}
```

---

**Analysis Date**: 2025-11-17
**Confidence Level**: MEDIUM (listed, not fully decompiled)
**Priority**: HIGH (critical backend passes)
