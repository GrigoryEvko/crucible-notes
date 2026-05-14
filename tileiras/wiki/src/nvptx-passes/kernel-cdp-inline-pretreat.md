# Kernel, CDP, Force-Inline, and Pretreat Passes

## Abstract

Four cooperating NVPTX-side passes share a single notion of kernel identity and run before the heavier NVPTX middle end. The kernel-attribute pass tags entry points with `nvvm.kernel`; the CDP expander rewrites device-side `cudaLaunchDevice` calls into runtime stubs; the force-inline pass collapses helpers the PTX ABI can't carry across a call boundary; and the pretreat pass normalizes frontend IR so address-space inference and argument lowering see a uniform form. They register together because they all consult the same `isKernelFunction` predicate and the same kernel-name registration table, and because their ordering is coupled: pretreat runs first, kernel attributes get stamped before CDP expansion goes looking for launchable targets, and force-inline runs last so it sees the final set of kernel and helper annotations.

## Pass Registration Table

A single shared registration sub at `sub_1CCB7D0` (lines 1121-1170) wires ten short names into the NVPTX pass registry. Each entry is a four-row block calling `RegisterPass<T>(short_name, long_name)` with the static class metadata, the short string consumed by `--passes=` and `opt -passes=`, and the long human-readable description. Other passes look these names up when scheduling a dependency or querying whether a pass already ran.

| Short name | C++ class | Purpose |
|---|---|---|
| `KernelAttrPass` | `mlir::nvvm::KernelAttrPass` | annotate kernels with `nvvm.kernel` |
| `KernelInfoPrinter` | `mlir::nvvm::KernelInfoPrinter` | emit `"kernel-info: …"` remarks |
| `InlineMustPass` | `mlir::nvvm::InlineMustPass` | force AlwaysInline on hot kernels |
| `Pretreat` | `mlir::nvvm::PretreatPass` | early IR cleanup before NVPTX |
| `CDPLaunchExpander` | `mlir::nvvm::CDPLaunchExpander` | expand `cudaLaunchDevice` to `__cudaCDP*LaunchDeviceV2` |
| `CDPParameterBuffer` | `mlir::nvvm::CDPParameterBuffer` | wire up `__cudaCDP*GetParameterBufferV2` |
| `KernelArgEliminator` | `mlir::nvvm::KernelArgEliminator` | drop unused kernel args |
| `KernelAttrTransplanter` | `mlir::nvvm::KernelAttrTransplanter` | move kernel attrs to nvvm.* form |
| `RemoveDeadFunctions` | `mlir::nvvm::RemoveDeadFunctions` | dead-fn DCE |
| `LegalizeFunctions` | `mlir::nvvm::LegalizeFunctions` | post-link function-level cleanup |

Treat the short names as stable public surface. They appear in remark output, in command-line pass pipelines, and in the names emitted by `-debug-pass-manager`.

## Kernel Identity

Kernel detection is the primary cross-cutting decision in this cluster. `KernelAttrPass`, `InlineMustPass`, `CDPLaunchExpander`, `KernelArgEliminator`, and several later NVPTX passes all consult one shared `isKernelFunction` predicate. The predicate is a four-criteria disjunction: a function is a kernel iff at least one of the following holds.

| # | Criterion | Source |
|---|---|---|
| 1 | `Function::getCallingConv() == 0x47` | numeric value of `CallingConv::PTX_Kernel` |
| 2 | function has attribute `nvvm.kernel` | new-style NVVM attribute set by `KernelAttrPass` |
| 3 | function has attribute `nvvm.annotations_transplanted` | set by `KernelAttrTransplanter` when it migrates old `!nvvm.annotations` metadata |
| 4 | function has the legacy string attribute `"kernel"` | CUDA 11 and earlier frontend output |

The third criterion is the subtle one. `KernelAttrTransplanter` walks the legacy `!nvvm.annotations` metadata list, copies each kernel mark to the modern attribute form, then stamps the source function with `nvvm.annotations_transplanted` so subsequent passes can distinguish a transplanted-and-already-modernized kernel from one that still owns its legacy metadata. The four-criteria predicate is the canonical "is this a kernel?" check across the NVPTX backend; every other pass reaches it through the shared callee in `sub_1CCB7D0`.

```c
bool isKernelFunction(Function fn) {
    if (fn.calling_convention == 0x47) {
        return true;
    }
    if (has_attribute(fn, "nvvm.kernel")) {
        return true;
    }
    if (has_attribute(fn, "nvvm.annotations_transplanted")) {
        return true;
    }
    if (has_string_attribute(fn, "kernel")) {
        return true;
    }
    return false;
}
```

Keep this predicate centralized in a single header. Forking the check across passes is how older NVPTX backends produced inconsistent "is this a kernel?" answers between `KernelArgEliminator` and `InlineMustPass`, with the predictable result that argument elimination dropped parameters of a function the inliner then refused to inline.

## CDP Launch Expansion

CUDA Dynamic Parallelism lets device code launch another kernel. `CDPLaunchExpander` rewrites each `cudaLaunchDevice(...)` call site into a call to one of two runtime launch stubs; `CDPParameterBuffer` rewrites each `cudaGetParameterBuffer(...)` call into a call to one of two runtime buffer-allocation stubs. The four stubs partition by CDP variant: CDP-1 is the single-grid form, CDP-2 is the two-grid form the runtime introduced for grid-of-grids workloads.

| Stub | Variant |
|---|---|
| `__cudaCDP1LaunchDeviceV2` | CDP-1 (single grid) |
| `__cudaCDP2LaunchDeviceV2` | CDP-2 (two grids) |
| `__cudaCDP1GetParameterBufferV2` | CDP-1 parameter buffer alloc |
| `__cudaCDP2GetParameterBufferV2` | CDP-2 parameter buffer alloc |

Stub names are not hardcoded in the rewriter body. `ctor_370` runs at backend-initialization time and installs two NULL-terminated arrays of `const char*` at `unk_5B6A4A0` (launch stubs) and `unk_5B6A4C0` (parameter-buffer stubs). The expander indexes into those arrays by CDP variant, so a future CDP-3 variant slots in without touching the rewriter logic. Keep that indirection in a reimplementation — it turns the CDP runtime ABI into a data table rather than a control-flow tree.

The expander must also re-resolve every launched target through `isKernelFunction`. A `cudaLaunchDevice` whose target resolves to an ordinary device function is a hard error: there is no PTX kernel entry to call, and the V2 launch stubs assume the callee is a real kernel.

The rewrite shape for the launch path is:

```text
input  : %r = call i32 @cudaLaunchDevice(ptr @child_kernel, ptr %params,
                                          %dim grid, %dim block, i32 %smem,
                                          ptr %stream)
output : %r = call i32 @__cudaCDP1LaunchDeviceV2(ptr @child_kernel, ptr %params,
                                                  %dim grid, %dim block, i32 %smem,
                                                  ptr %stream)
```

The parameter-buffer path follows the same pattern, rewriting
`cudaGetParameterBuffer` into `__cudaCDP{1,2}GetParameterBufferV2`. CDP variant
selection (`CDP1` vs `CDP2`) comes from the call site's variant flag, not from
the kernel signature.

## Force-Inline Policy

`InlineMustPass` at `sub_3A550F0` walks every call site and force-inlines callees marked `nvvm.always_inline`. The pass exists because parts of the NVPTX ABI can't lower certain helper signatures faithfully: image and sampler arguments must arrive at the kernel boundary as opaque handles, large aggregate arguments can't survive a call boundary, and some helpers exist solely so the frontend has somewhere to attach attributes that must be visible at the use site.

When the inliner hits a callee it cannot inline — a recursive cycle, an exception handler frame, an interposable definition, or a callee whose body is unavailable — it emits a Remark of the form `"not AlwaysInline into "` followed by the caller's function name. The pass never silently downgrades the requirement: either the callee is inlined or the user receives the diagnostic and can fix the offending annotation.

```c
void inline_must_pass(Module module) {
    for (Function caller : module.functions) {
        for (CallInst call : calls_in(caller)) {
            Function callee = call.resolved_callee;
            if (!has_attribute(callee, "nvvm.always_inline")) {
                continue;
            }
            if (!try_inline_at_call_site(call)) {
                emit_remark(caller, "not AlwaysInline into ", caller.name);
            }
        }
    }
}
```

## Kernel Info Printer

`KernelInfoPrinter` is a read-only diagnostic pass. It walks every function that satisfies `isKernelFunction` and emits one Remark per metric in a fixed `"kernel-info: <Metric> in function '<fn>' = <value>"` format. The metric set is exactly nineteen entries, in order: `regs`, `smem`, `cmem`, `tex`, `params`, `local`, `stack`, `barriers`, `loads`, `stores`, `branches`, `fp_ops`, `int_ops`, `divergence`, `predicated`, `vector_ops`, `mma_ops`, `tcgen05_ops`, `tma_ops`.

The last three are Blackwell-era additions. `mma_ops` counts WGMMA-family tensor-core instructions, `tcgen05_ops` counts the tensor-memory ops introduced for sm_100 and later, and `tma_ops` counts asynchronous bulk-copy instructions. Keep the metric list ordered in any reimplementation — downstream tooling parses the remark stream positionally and breaks the moment the order shifts.

## Pretreat

`PretreatPass` is the first cleanup stage after libNVVM accepts frontend IR. Its job is to strip or normalize frontend-specific forms before verification, address-space inference, and argument lowering start relying on them. Keep the pass deliberately narrow: canonicalize pointer casts, normalize lifetime and memory intrinsics, strip metadata that earlier frontend stages already consumed, and rewrite placeholder intrinsics into the forms later NVVM passes expect. It must not perform any optimization that depends on the analysis results it precedes.

```c
void pretreat_module(Module module) {
    for (Function fn : module.functions) {
        canonicalize_pointer_casts(fn);
        normalize_lifetime_intrinsics(fn);
        normalize_small_memory_intrinsics(fn);
        rewrite_frontend_nvvm_placeholders(fn);
    }

    remove_consumed_frontend_metadata(module);
}
```

## Cross-References

[NVPTX Pass Pipeline Overview](pass-pipeline-overview.md) shows where this cluster sits in the full NVPTX schedule. [Kernel Argument Elimination](kernel-arg-eliminator.md) covers the downstream consumer of `nvvm.kernel` attributes. [CDP Runtime ABI](../runtime/cdp-runtime-abi.md) documents the stub signatures the expander targets.
