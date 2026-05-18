# NVVM IR Verifier

## Abstract

NVVMIRVerifier enforces NVVM-IR-level invariants the upstream LLVM `Verifier` knows nothing about. It runs after every NVPTX-side pass in the Tileiras pipeline and fires diagnostics on violations such as a kernel launched from a non-kernel function or a parameter buffer that overflows the SM's parameter-space limit. Failure aborts compilation through `signalPassFailure()`. The pass is a regular LLVM `FunctionPass`, not an MLIR `OperationPass`, so it never touches the failure-flag handshake TileAS passes use; the LLVM pass manager picks up its failure through the standard `Pass::run` return path and aborts before the next NVPTX pass starts.

Two principal procedures do the work. The launch-argument address-space checker walks every `nvvm.launch_call` instruction and verifies that arguments live in an address space the child grid can dereference — typically global or constant. The parameter-space sizer walks each kernel's formal parameter list, sums byte sizes per the NVVM ABI, and compares the total to the per-SM parameter-space ceiling.

## Launch-Argument Address-Space Check

The launch checker iterates the operands of each `nvvm.launch_call` site and resolves the address space of every pointer-typed argument. Global and constant pointers pass unconditionally. A pointer the child grid cannot legally dereference triggers one of two diagnostics.

The first diagnostic fires when the launch target itself is not a kernel:

```text
a function that is not __global__ cannot be launched
```

The second fires when an argument is a generic-AS or local-AS pointer. The child grid runs in a different address-space frame, and dereferencing a parent-thread local pointer or an `addrspace(0)` pointer through it is undefined:

```text
A pointer to local memory or memory in 'addrspace(0)' has been used as a launch argument. Dereferencing this within the launch is undefined
```

Both strings surface through the standard MLIR diagnostic engine; downstream tooling matches on them.

## Parameter-Space Sizer

A 21-case switch on the NVVM type tag stored in the parameter descriptor dominates the sizer. Each case returns the parameter's byte footprint; the caller accumulates the running total with natural alignment between fields.

| Tag | Type            | Size formula                  |
|-----|-----------------|-------------------------------|
| 0   | i1              | 1 byte (padded)               |
| 1   | i8              | 1                             |
| 2   | i16             | 2                             |
| 3   | i32             | 4                             |
| 4   | i64             | 8                             |
| 5   | f16             | 2                             |
| 6   | bf16            | 2                             |
| 7   | f32             | 4                             |
| 8   | f64             | 8                             |
| 9   | tf32            | 4                             |
| 10  | f8e4m3          | 1                             |
| 11  | f8e5m2          | 1                             |
| 12  | f4e2m1          | 0.5 (packed pair)             |
| 13  | ptr_global      | 8                             |
| 14  | ptr_constant    | 8                             |
| 15  | ptr_shared      | 4 (sm32 ABI) or 8             |
| 16  | ptr_generic     | 8                             |
| 17  | array<elem, N>  | size(elem) × N                |
| 18  | struct{fields…} | aligned sum                   |
| 19  | vector<elem, N> | size(elem) × N (no padding)   |
| 20  | opaque          | error                         |

Tag 12 (`f4e2m1`) is the only sub-byte case — two values share a byte, so the sizer treats it as half a byte and only commits a whole byte when the parameter count rounds up. Tag 15 (`ptr_shared`) is the only case where the result depends on the ABI flavor: the legacy sm32 shared-memory pointer is 32 bits, every modern SM uses 64. Tag 20 (`opaque`) is unreachable in valid NVVM-IR; if it appears, the verifier emits a hard error pointing at an upstream type-lowering bug rather than user code.

Aggregate tags recurse. A `struct{i32, f64, i8}` aligns the `f64` to 8 and pads the trailing `i8` so the next parameter starts aligned. A `vector<f32, 4>` consumes 16 bytes flat with no inter-element padding — that's what distinguishes it from `array<f32, 4>` at the ABI boundary.

```c
uint64_t size_of_param(ParamDesc *p, TargetInfo *target) {
    switch (p->tag) {
        case TAG_I1:      return 1;
        case TAG_I8:      return 1;
        case TAG_I16:     return 2;
        case TAG_I32:     return 4;
        case TAG_I64:     return 8;
        case TAG_F16:     return 2;
        case TAG_F32:     return 4;
        case TAG_F64:     return 8;
        case TAG_PTR_SHARED:  return target->sm == 32 ? 4 : 8;
        case TAG_PTR_GLOBAL:
        case TAG_PTR_CONSTANT:
        case TAG_PTR_GENERIC: return 8;
        case TAG_ARRAY:   return p->elem_count * size_of_param(p->elem, target);
        case TAG_VECTOR:  return p->elem_count * size_of_param(p->elem, target);
        case TAG_STRUCT:  return size_struct(p, target);
        case TAG_OPAQUE:  fatal("opaque parameter type"); return 0;
        ...
    }
}

uint64_t size_struct(ParamDesc *p, TargetInfo *target) {
    uint64_t off = 0;
    for (size_t i = 0; i < p->field_count; ++i) {
        ParamDesc *f = p->fields[i];
        off = round_up(off, align_of(f, target));
        off += size_of_param(f, target);
    }
    return round_up(off, align_of(p, target));
}
```

## ParamSpaceLimit by SM Family

The accumulated total is checked against a per-SM ceiling. The limit is a step function of the SM major version:

| SM family       | Limit (bytes) |
|-----------------|--------------:|
| sm_20…sm_35     | 440           |
| sm_50…sm_75     | 1 024         |
| sm_80…sm_90     | 32 764        |
| sm_100…sm_121   | 32 768        |

The sm_80–sm_90 ceiling falls 4 bytes short of 32 KiB because the runtime reserves a small trailer for the implicit grid-constant descriptor; sm_100 and later move that descriptor elsewhere and reclaim the full 32 KiB. When the running total exceeds the SM's limit, the sizer emits:

```text
Formal parameter space overflowed (X bytes required, max Y bytes allowed) in function Z
```

`X` is the running sum, `Y` is the parameter-space ceiling for the active SM, and `Z` is the demangled kernel name.

## Worked Example: Parameter-Space Overflow on sm_75

Take the kernel

```c
struct Heavy {
    double  scale;       //   8 B
    char    tag;         //   1 B (+ 7 B padding to align the next field)
    int     data[10000]; //  40000 B
};

__global__ void big_kernel(struct Heavy h) { /* ... */ }
```

`LowerStructArgs` has already promoted the by-value `h` into a parameter-space pointer that the verifier walks through. The sizer descends into the struct in declaration order:

| Field | Tag | Offset (B) | Size (B) | Running total (B) |
|---|---|---:|---:|---:|
| `scale` | f64 | 0 | 8 | 8 |
| `tag` | i8 | 8 | 1 | 9 |
| (padding to 4-byte alignment for `int`) | — | 9 | 3 | 12 |
| `data[10000]` | array<i32, 10000> | 12 | 40000 | 40012 |
| (trailing pad to 8-byte struct alignment) | — | 40012 | 4 | 40016 |

The struct sizes to 40016 bytes. The active SM is sm_75, so the ceiling is 1024 bytes. The running total exceeds the ceiling at the very first call to `size_struct`, and the sizer emits:

```text
Formal parameter space overflowed (40016 bytes required, max 1024 bytes allowed) in function big_kernel
```

`signalPassFailure()` fires, the LLVM pass manager picks the failure up on the `Pass::run` return path, and the pipeline aborts before instruction selection runs. The same kernel compiles on sm_80 (40016 < 32764), and on sm_100 the ceiling rises to 32768 — still too small for this struct, but enough room for `data[8000]` to fit. The verifier is the canonical place where the kernel ABI's parameter-space ceiling becomes a hard error rather than a silent truncation.

## What This Catches That Upstream LLVM Doesn't

Upstream LLVM ships a generic `Verifier` that validates LLVM IR independent of any target. It checks instruction operand counts, type compatibility across def-use chains, terminator shapes, intrinsic signatures against their declarations, and metadata-node well-formedness. None of those checks is aware of the NVPTX ABI, the per-SM intrinsic introduction matrix, or the address-space rules that govern device-side launches. Four NVPTX-specific bug classes pass upstream LLVM's verifier unconditionally and surface only here.

### Parameter-Space Overflow

```llvm
%struct.Heavy = type { double, i8, [40000 x i32] }

define void @big_kernel(%struct.Heavy %h) !nvvm.kernel !0 {
  ...
}
```

Upstream LLVM accepts the function: the by-value struct argument is a well-formed LLVM type, the def-use chain is consistent, and the kernel-marker metadata is well-formed. The function would lower through the NVPTX backend and emit a `.entry big_kernel` directive whose `.param` declarations name a struct that exceeds the SM's parameter-space ceiling. On sm_75 the ceiling is 1 024 bytes; the struct sizes to 40 016 bytes. The hardware-side consequence of a kernel whose parameter buffer exceeds the ABI limit is undefined: the runtime either truncates the parameter copy or rejects the launch with an opaque cuLaunchKernel error far from the source. The NVVM verifier reads the per-SM ceiling from the resolved `#nvvm.target` attribute and emits the diagnostic shown in the worked example above. Upstream LLVM has no concept of parameter space, so it cannot reach the check.

### SM-Versioned Intrinsic Used Below Its Introducing SM

```llvm
%cluster_addr = call ptr @llvm.nvvm.cp.async.bulk.tensor.g2s.tile.2d(...)
```

`nvvm.cp.async.bulk.tensor.*` is the Hopper TMA tile-load intrinsic family, introduced at sm_90. Upstream LLVM's verifier checks the intrinsic signature against the declaration in `Intrinsics.td` and accepts the call as well-formed. It does not check the active target. When the compile-target is sm_80 (Ampere, no TMA hardware), the NVVM verifier consults the intrinsic-to-introducing-SM table, compares against the resolved target's chip field, and emits a diagnostic naming the intrinsic and the minimum SM it requires. Without this check the call would lower to a PTX `cp.async.bulk.tensor` instruction that ptxas would reject with an architecture-mismatch error far from the source; the NVVM verifier surfaces the bug at the IR site that introduced the call.

The same check fires for the Blackwell-only `nvvm.tcgen05.mma` intrinsic family when the target is below sm_100, and for the SM_103-only block-scaled MMA intrinsics when the target is below sm_103.

### Launch-Argument Address-Space Mismatch

```llvm
%local = alloca i32, addrspace(0)
call void @llvm.nvvm.launch(ptr @child_kernel, ptr %local)
```

The `nvvm.launch_call` operand is a pointer to an `alloca` in the generic address space — the parent thread's local storage. Upstream LLVM's verifier accepts the call: the argument is a well-formed pointer, the launch intrinsic signature accepts a generic-AS pointer, and the def-use chain is consistent. The child grid runs in a different address-space frame, however, and a generic or local-AS pointer the parent passed is undefined to dereference from the child. The NVVM verifier walks each launch call's operand list, resolves the address space of every pointer-typed argument, and emits:

```text
A pointer to local memory or memory in 'addrspace(0)' has been used as a launch argument. Dereferencing this within the launch is undefined
```

The closely related diagnostic for the launch target itself fires when the launched function is not marked as a kernel:

```text
a function that is not __global__ cannot be launched
```

Upstream LLVM has no notion of NVPTX address-space rules or of the `nvvm.kernel` predicate. Both diagnostics surface only here.

### Kernel-Required Metadata Missing on Launchable Function

```llvm
define void @child(...) {            ; missing nvvm.kernel marker
  ...
}
%launched = call ... @llvm.nvvm.launch(ptr @child, ...)
```

Upstream LLVM accepts both definitions and the call. The NVVM verifier walks every `nvvm.launch_call` site, follows the called-symbol operand to its definition, and consults the `isKernelFunction` predicate documented in [Kernel Identity](kernel-cdp-inline-pretreat.md#kernel-identity). A function the launch reaches that does not satisfy the predicate fails the launch-target check above. The diagnostic carries the call-site location so the user can locate the missing `__global__` declaration in the source.

## Driver

The driver is a thin loop over the module. It selects kernels using the canonical `isKernelFunction` predicate (see [Kernel Identity](kernel-cdp-inline-pretreat.md#kernel-identity)) and dispatches to the two checkers:

```c
void run_nvvm_ir_verifier(Module *module, TargetInfo *target) {
    for (Function &fn : *module) {
        if (!is_nvvm_kernel(fn)) continue;

        check_parameter_space(fn, target);     // sizer + ceiling
        check_launch_arguments(fn);            // address-space check
    }
}
```

Any failed check calls `signalPassFailure()` directly.

## Cross-References

[LowerStructArgs Rewrite Shape](lower-args-and-aggr-and-struct.md#rewrite-shape) is what leaves the parameter list this pass sizes. [Kernel Identity](kernel-cdp-inline-pretreat.md#kernel-identity) defines the `isKernelFunction` predicate the driver consults. The [NVPTX Backend Passes Overview](overview.md) shows where the verifier sits in the cluster pipeline.
