# NVVM IR Verifier

## Abstract

NVVMIRVerifier enforces NVVM-IR-level invariants the upstream LLVM `Verifier` knows nothing about. It runs after every NVPTX-side pass in the Tileiras pipeline and fires diagnostics on violations such as a kernel launched from a non-kernel function or a parameter buffer that overflows the SM's parameter-space limit. Failure aborts compilation through `signalPassFailure()`. The pass is a regular LLVM `FunctionPass`, not an MLIR `OperationPass`, so it bypasses the `*(self+40) |= 4` failure-flag handshake TileAS passes use.

Two principal procedures do the work. `sub_27D4900` (2 747 bytes) is the launch-argument address-space checker: it walks every `nvvm.launch_call` instruction and verifies that arguments live in an address space the child grid can dereference — typically global or constant. `sub_27D5520` (10 680 bytes) is the parameter-space sizer: it walks each kernel's formal parameter list, sums byte sizes per the NVVM ABI, and compares the total to `ParamSpaceLimit` for the chosen SM.

## Launch-Argument Address-Space Check

`sub_27D4900` iterates the operands of each `nvvm.launch_call` site and resolves the address space of every pointer-typed argument. Global and constant pointers pass unconditionally. A pointer the child grid cannot legally dereference triggers one of two diagnostics.

The first diagnostic fires when the launch target itself is not a kernel:

```text
a function that is not __global__ cannot be launched
```

The second fires when an argument is a generic-AS or local-AS pointer. The child grid runs in a different address-space frame, and dereferencing a parent-thread local pointer or an `addrspace(0)` pointer through it is undefined:

```text
A pointer to local memory or memory in 'addrspace(0)' has been used as a launch argument. Dereferencing this within the launch is undefined
```

Both strings are baked verbatim into the binary and surface through the standard MLIR diagnostic engine; downstream tooling matches on them.

## Parameter-Space Sizer

A 21-case switch on the NVVM type tag stored in the parameter descriptor dominates `sub_27D5520`. Each case returns the parameter's byte footprint; the caller accumulates the running total with natural alignment between fields.

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

## ParamSpaceLimit by SM Family

The accumulated total is checked against a per-SM ceiling. The limit is a step function of the SM major version:

| SM family       | Limit (bytes) |
|-----------------|--------------:|
| sm_20…sm_35     | 440           |
| sm_50…sm_75     | 1 024         |
| sm_80…sm_90     | 32 764        |
| sm_100…sm_121   | 32 768        |

The sm_80–sm_90 ceiling falls 4 bytes short of 32 KiB because the runtime reserves a small trailer for the implicit grid-constant descriptor; sm_100 and later move that descriptor elsewhere and reclaim the full 32 KiB. When the running total exceeds the SM's limit, `sub_27D5520` emits:

```text
Formal parameter space overflowed (X bytes required, max Y bytes allowed) in function Z
```

`X` is the running sum, `Y` is the `ParamSpaceLimit` for the active SM, and `Z` is the demangled kernel name.

## Driver and Failure Handshake

The driver is a thin loop over the module. It selects kernels using the same detector linkage normalization uses, then dispatches to the two checkers:

```c
void run_nvvm_ir_verifier(Module module, TargetInfo target) {
    for (Function fn : module.functions) {
        if (!is_nvvm_kernel(fn)) {
            continue;
        }

        sub_27D5520(fn, target);   // parameter-space sizer
        sub_27D4900(fn);           // launch-argument AS checker
    }
}
```

Any failed check calls `signalPassFailure()` directly. Because NVVMIRVerifier is a `FunctionPass` rather than an MLIR `OperationPass`, it never touches the `*(self+40) |= 4` flag word TileAS-side passes use to surface failure to the pass manager. The LLVM pass manager picks the failure up through the standard `Pass::run` return path and aborts before the next NVPTX pass starts.

## Reimplementation Invariants

- Share the kernel detector with linkage normalization and launch checking; an inconsistency between detectors causes either spurious "non-kernel launched" diagnostics or silent skips.
- Size parameters with the NVVM data layout, never with host `sizeof`. `bool` is one parameter byte regardless of host ABI; `f4e2m1` is half a byte.
- Track `ParamSpaceLimit` as a step function over the SM major version. Hard-coding only the modern 32 768 ceiling silently accepts kernels that the older SMs cannot launch.
- Reject tag 20 (`opaque`) at the parameter boundary; valid NVVM-IR never carries it.
- Emit the three diagnostic strings verbatim — downstream test suites match on them character-for-character.
- Call `signalPassFailure()` on every hard error; do not propagate through a flag word.

## Cross-References

[Modulo Scheduler and Rau-Style Placement](../scheduler/modulo-scheduler-and-rau.md) documents the TileAS-side failure-flag convention this pass deliberately avoids.
