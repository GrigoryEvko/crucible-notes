# NVPTX Backend Passes Overview

## Abstract

Once TileIR has been lowered to LLVM IR, the NVPTX backend normalizes that IR and the post-selection MachineIR so PTX emission sees legal kernel parameters, concrete address spaces, expanded aggregate copies, valid device launches, resolved image handles, and subtarget-compatible machine instructions. This page covers what is shared across the cluster: where each pass sits in the pipeline, what state it hands the next pass, and which globals it has to agree on. Per-pass mechanics live in the dedicated pages.

The cluster spans two IR levels. The LLVM-IR passes consume `Function`, `Argument`, `Instruction`, `Metadata`, address spaces, and intrinsics. The MachineIR passes consume `MachineFunction`, `MachineInstr`, machine operands, frame indices, and subtarget feature bits. SelectionDAG sits between the two. Passes that need semantic SSA-level information run on the IR side; passes that need concrete target opcodes run on the MachineIR side.

## Pipeline Position

```text
LLVM IR with NVVM intrinsics
    |
    |   Pretreat          (canonicalize frontend forms)
    |   KernelAttrPass    (stamp nvvm.kernel)
    |   InlineMustPass    (force AlwaysInline)
    |   CDPLaunchExpander (rewrite cudaLaunchDevice -> __cudaCDP*V2)
    |   LowerStructArgs   (byval -> parameter-space pointer + scalar LDPARAM)
    |   MemorySpaceOpt    (concrete AS inference, cvta folding)
    |   ProcessRestrict   (noalias / alias-scope materialization)
    |   PrintfLowering    (vprintf packing buffer)
    |   DeadSyncElim      (barrier removal)
    |   CommonBaseElim    (SCEV-keyed GEP CSE)
    |   NVVMIRVerifier    (kernel-ABI invariants, parameter-space ceiling)
    |
    v
SelectionDAG instruction selection
    |
    |   BASR                  (post-ISel address-arithmetic peephole)
    |   Image-handle rewrite  (parametric -> slot opcode)
    |   Prolog/Epilog, proxy-reg erase, invariant-load tagging
    |
    v
PTX assembly
```

The order above is the ordering the rest of this cluster's pages assume. Two pages call out specific ordering constraints explicitly: ProcessRestrict must follow MemorySpaceOpt so it sees concrete address-space tags on derived pointers, and BASR must follow instruction selection so it sees the final `MachineInstr` opcodes rather than IR-level GEPs.

## Cross-Pass Invariants

The pages in this cluster share three pieces of state that have to agree across pass boundaries. Getting any of them wrong produces either silent miscompiles or a downstream verifier abort.

### Kernel identity

`KernelAttrPass`, `KernelAttrTransplanter`, `InlineMustPass`, `CDPLaunchExpander`, `KernelArgEliminator`, `NVVMIRVerifier`, and the parameter-space ceiling check all consult a single `isKernelFunction` predicate. The predicate is a four-way disjunction over `CallingConv::PTX_Kernel` (`0x47`), the `nvvm.kernel` attribute, the `nvvm.annotations_transplanted` attribute, and the legacy `"kernel"` string attribute. Forking this check across passes is how older NVPTX backends produced inconsistent answers between argument elimination and the inliner. See [Kernel Identity](kernel-cdp-inline-pretreat.md#kernel-identity) for the canonical definition.

### Shared parameter-space enable flag

`LowerStructArgs` and `MemorySpaceOpt` both read the same boolean enable flag at startup. When the flag is set, `LowerStructArgs` rewrites each by-value struct argument to a parameter-space pointer plus per-field `LDPARAM` (MI opcode `101`) loads, and `MemorySpaceOpt` then seeds its lattice on those parameter-space pointers and folds the resulting `CVT_PARAM_TO_GENERIC` / `CVT_PARAM_TO_GLOBAL` casts (MI opcodes `49` / `50`). A mismatch — one pass enabled, the other disabled — produces by-value pointers `MemorySpaceOpt` cannot classify, and `NVVMIRVerifier` then rejects the function with a "pointer-to-local-or-generic launch argument" diagnostic. Reimplementations have to gate both passes on the same flag.

### Pass-to-pass attribute hand-off

| Producer | Attribute or metadata | Consumer |
| --- | --- | --- |
| `KernelAttrPass`, `KernelAttrTransplanter` | `nvvm.kernel`, `nvvm.annotations_transplanted` | Every later kernel-aware pass |
| `LowerStructArgs` | parameter-space `LDPARAM` SSA chain on byval args | `MemorySpaceOpt` |
| `MemorySpaceOpt` | concrete address-space tag on every pointer SSA value | `ProcessRestrict`, NVPTX alias analysis |
| `ProcessRestrict` | `nvvm.restrict_scope` per pointer, `nvvm.restrict_processed` per function | NVPTX alias analysis |
| `PrintfLowering` | `%vprintfBuffer.local` alloca, `call @vprintf(...)` | None (terminal) |
| `CDPLaunchExpander` | `call @__cudaCDP{1,2}LaunchDeviceV2` | `NVVMIRVerifier` (re-checks the callee is a kernel) |
| `KernelAttrPass` + `LowerStructArgs` | byval-aware parameter list | `NVVMIRVerifier` (parameter-space ceiling) |

The verifier reads everything in the right column: parameter-space sizes for the byval-aware list, address spaces for launch arguments, and the kernel attribute for the launch-target sanity check. Running the verifier before any producer in the table has fired leads to a false-positive abort.

## Routing

| Page | Covers |
| --- | --- |
| [Kernel, CDP, Force-Inline, and Pretreat](kernel-cdp-inline-pretreat.md) | Pretreat, kernel attribute stamping, `InlineMustPass`, CDP launch and parameter-buffer expansion, `isKernelFunction`. |
| [LowerStructArgs](lower-args-and-aggr-and-struct.md) | Bare-pointer ABI translation for by-value struct parameters, including the cast-only fast path and nested-aggregate recursion. |
| [Memory-Space Optimization and Restrict](memory-space-opt-and-process-restrict.md) | Inter-procedural callee specialization, the function-local AS lattice, the cast folder, and `__restrict__` propagation. |
| [Printf Lowering and the vprintf ABI](printf-lowering-and-vprintf.md) | Tag-driven rewrite of `printf` into `vprintf`, the per-thread packing buffer, and the constant-AS format-string check. |
| [Dead Sync Elimination and Common Base](dead-sync-elim-and-common-base.md) | Cross-product test for redundant barriers, and SCEV-keyed GEP merging with alloca cloning. |
| [NVVM IR Verifier](nvvm-ir-verifier.md) | Launch-argument address-space check and the parameter-space ceiling per SM family. |
| [Peephole, MIR Cleanup, and Image Handles](peephole-mir-and-image-handles.md) | BASR post-ISel address-arithmetic peephole, the parametric-to-slot rewrite for `tex` / `sust` / `suld` / `suq`, and final MachineIR cleanup. |

For the shared backend relationship with `cicc`, see [cicc comparison](../boundaries/cicc-comparison.md).
