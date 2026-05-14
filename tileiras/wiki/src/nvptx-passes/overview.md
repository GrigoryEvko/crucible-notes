# NVPTX Backend Passes Overview

## Abstract

Once TileIR has been lowered to LLVM IR, the NVPTX backend takes over the CUDA device ABI. This layer no longer schedules MLIR dialect conversions; it normalizes LLVM IR and MachineIR so PTX emission sees legal kernel parameters, concrete address spaces, lowered aggregate copies, valid launch calls, resolved image handles, and subtarget-compatible machine instructions.

Several pass names here collide with MLIR-level ones, and the distinction is not cosmetic. The NVVM IR verifier is an LLVM `FunctionPass`, not an MLIR `OperationPass`; its failure path is `Pass::run` returning `failure()`, not the `pass+40 |= 4` flag word the TileAS-side passes use. The NVPTX-MIR peephole and image-handle passes match on `MachineInstr` opcodes rather than MLIR ops; their pattern shapes are MachineIR matchers, not `OpConversionPattern` subclasses. The [lowering](../lowering/overview.md) layer owns the MLIR side; this layer owns everything from LLVM IR through MachineIR to PTX.

Most of these passes carry correctness, not optimization. Even a small kernel needs parameter-space lowering, launch validation, `__restrict__` metadata, device-side `printf` packing, aggregate-copy expansion, and final MachineIR cleanup.

## Pipeline Shape

```text
LLVM IR with NVVM intrinsics
    |
    | NVVM middle-end passes
    v
ABI-normalized LLVM IR
    |
    | SelectionDAG instruction selection
    v
NVPTX MachineIR
    |
    | target MachineFunction passes
    v
PTX assembly
```

The IR stage sees LLVM functions, arguments, metadata, intrinsics, address spaces, and calls. The MachineIR stage sees selected NVPTX opcodes, machine operands, frame indices, memory operands, and subtarget feature bits. Passes that need semantic LLVM values belong before instruction selection; passes that need concrete target opcodes belong after it.

## Pass Families

| Family | Stage | Contract |
| --- | --- | --- |
| Kernel and launch checks | LLVM IR | Select kernel entry points, validate device launches, and normalize linkage. |
| Argument lowering | LLVM IR and MachineIR | Convert by-value and pointer arguments to PTX parameter-space conventions. |
| Address-space inference | LLVM IR | Promote generic pointers only when provenance proves a concrete state space. |
| Restrict processing | LLVM IR | Translate `__restrict__` into alias scopes that downstream AA can consume. |
| Libdevice and math cleanup | LLVM IR | Remove reflection-dead branches and canonicalize math calls before selection. |
| Printf lowering | LLVM IR | Pack varargs into a per-thread local buffer and call `vprintf`. |
| Synchronization cleanup | LLVM IR | Remove provably redundant barriers without crossing visible memory traffic. |
| Aggregate copies | LLVM IR | Expand unsupported `llvm.mem*` operations into explicit loops. |
| Image handles | MachineIR | Rewrite texture and surface parameters to slot-indexed operands. |
| MIR cleanup | MachineIR | Remove target pseudos, fold frame-address casts, and tag invariant loads. |

## Address-Space Contract

NVPTX exposes several disjoint state spaces, and the backend treats them as a semantic partition rather than decorative pointer tags. Generic pointers are legal but expensive; specializing one to the wrong state space is a miscompile.

| Space | Meaning |
| --- | --- |
| Generic | Unknown or mixed provenance. |
| Global | Device memory visible to all CTAs. |
| Shared | CTA-local shared memory. |
| Constant | Read-only constant or grid-constant memory. |
| Local | Per-thread stack and spills. |
| Tensor memory | Blackwell tensor-memory accumulator space. |
| Distributed shared | Cluster-wide distributed shared memory. |

The inference lattice is intentionally flat: a pointer is unknown, one concrete state space, or conflicted. Two concrete spaces meet to generic rather than to either input. This rule is conservative, easy to reimplement, and matters for calls reached from several memory paths.

```c
AddressSpace meet_address_space(AddressSpace lhs, AddressSpace rhs) {
    if (lhs == AS_UNKNOWN) {
        return rhs;
    }
    if (rhs == AS_UNKNOWN) {
        return lhs;
    }
    if (lhs == rhs) {
        return lhs;
    }
    return AS_GENERIC;
}
```

## Argument ABI Contract

Kernel arguments are not ordinary local SSA values in PTX. The formal argument buffer lives in parameter space, and the body usually needs a generic or concrete pointer derived from it. The backend inserts parameter-space storage, casts, copies, or rematerialized loads at the point where each argument form becomes visible.

```c
void lower_kernel_arguments(Function kernel, TargetInfo target) {
    for (Argument arg : kernel.arguments) {
        if (is_grid_constant_byval(arg, target)) {
            Value param_ptr = create_param_slot(arg);
            replace_uses_with_param_pointer(arg, param_ptr);
            continue;
        }

        if (is_byval_aggregate(arg)) {
            Value param_ptr = create_param_slot(arg);
            Value local_copy = copy_param_to_local(arg, param_ptr);
            replace_argument_uses(arg, local_copy);
            continue;
        }

        if (is_pointer_argument(arg)) {
            Value param_ptr = load_param_pointer(arg);
            Value usable = cast_from_param_space(param_ptr, expected_space(arg));
            replace_argument_uses(arg, usable);
        }
    }
}
```

## Verification Contract

Malformed IR should never reach PTX printing. Good diagnostics matter here because the original TileIR is gone by this stage and the user is reading a CUDA-device ABI failure.

A practical verifier rejects:

- launching a function that is not a kernel,
- formal parameter buffers that exceed the target parameter-space limit,
- pointer arguments to child kernels that reference local or shared memory,
- unresolved libdevice calls that should have been linked or folded,
- unsupported memory scopes or synchronization scopes,
- aggregate-copy forms that should have been expanded,
- MachineIR opcodes requiring unavailable SM features,
- image, surface, tensor-memory, or async-copy pseudos that escaped their cleanup pass.

For the shared backend relationship with `cicc`, see [cicc comparison](../boundaries/cicc-comparison.md).
