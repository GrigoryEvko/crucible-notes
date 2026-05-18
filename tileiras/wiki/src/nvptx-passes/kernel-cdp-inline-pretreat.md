# Kernel, CDP, Force-Inline, and Pretreat Passes

## Abstract

Four cooperating NVPTX-side passes share a single notion of kernel identity and run before the heavier NVPTX middle end. The kernel-attribute pass tags entry points with `nvvm.kernel`; the CDP expander rewrites device-side `cudaLaunchDevice` calls into runtime stubs; the force-inline pass collapses helpers the PTX ABI can't carry across a call boundary; and the pretreat pass normalizes frontend IR so address-space inference and argument lowering see a uniform form. They register together because they all consult the same `isKernelFunction` predicate and the same kernel-name registration table, and because their ordering is coupled: pretreat runs first, kernel attributes get stamped before CDP expansion goes looking for launchable targets, and force-inline runs last so it sees the final set of kernel and helper annotations.

## Pass Registration Table

A single shared registration entry wires ten short names into the NVPTX pass registry. Each entry calls `RegisterPass<T>(short_name, long_name)` with the static class metadata, the short string consumed by `--passes=` and `opt -passes=`, and the long human-readable description. Other passes look these names up when scheduling a dependency or querying whether a pass already ran.

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
| 1 | `Function::getCallingConv() == CallingConv::PTX_Kernel` | the LLVM calling convention enumerator (value `0x47`) emitted by the front-end on every kernel entry point |
| 2 | function carries the `nvvm.kernel` LLVM attribute | new-style NVVM attribute set by `KernelAttrPass` after CUDA 12 |
| 3 | function carries the `nvvm.annotations_transplanted` attribute | set by `KernelAttrTransplanter` when it migrates old `!nvvm.annotations` metadata |
| 4 | function carries the legacy string attribute `"kernel"` | CUDA 11 and earlier frontend output |

Criterion 1 is what every modern CUDA front-end emits directly. Criterion 2 is the canonical form `KernelAttrPass` produces and the form every later analysis prefers to read. Criterion 3 is the bookkeeping marker that lets the rest of the pipeline distinguish a kernel whose modern attribute was synthesized from old metadata from one that originally carried only the calling convention or the modern attribute. Criterion 4 is the long-tail fallback for IR consumed from older toolchains.

The third criterion is the subtle one. `KernelAttrTransplanter` walks the legacy `!nvvm.annotations` metadata list, copies each kernel mark to the modern attribute form, then stamps the source function with `nvvm.annotations_transplanted` so subsequent passes can distinguish a transplanted-and-already-modernized kernel from one that still owns its legacy metadata. The four-criteria predicate is the canonical "is this a kernel?" check across the NVPTX backend; every other pass reaches it through a single shared callee.

```c
bool isKernelFunction(Function *fn) {
    if (fn->getCallingConv() == CallingConv::PTX_Kernel)     return true;
    if (fn->hasFnAttribute("nvvm.kernel"))                   return true;
    if (fn->hasFnAttribute("nvvm.annotations_transplanted")) return true;
    if (fn->hasFnAttribute("kernel"))                        return true;
    return false;
}
```

Keep this predicate centralized in a single header. Forking the check across passes is how older NVPTX backends produced inconsistent "is this a kernel?" answers between `KernelArgEliminator` and `InlineMustPass`, with the predictable result that argument elimination dropped parameters of a function the inliner then refused to inline.

## CDP Launch Expansion

### Input and Output IR Shape

CUDA Dynamic Parallelism lets device code launch another kernel. `CDPLaunchExpander` rewrites each high-level `cudaLaunchDevice` call site into a CDP-specific intrinsic-call sequence that targets one of two runtime launch stubs; `CDPParameterBuffer` rewrites each `cudaGetParameterBuffer` call into a call to one of two runtime buffer-allocation stubs. The four stubs partition by CDP variant: CDP-1 is the single-grid form, CDP-2 is the two-grid form the runtime introduced for grid-of-grids workloads.

```llvm
; before: high-level CUDA-runtime call
%pbuf = call ptr @cudaGetParameterBuffer(i64 64, i64 16)
store ptr %arg0, ptr %pbuf, align 8
%pbuf.1 = getelementptr i8, ptr %pbuf, i64 8
store i32 %arg1, ptr %pbuf.1, align 4
%r = call i32 @cudaLaunchDevice(ptr @child_kernel, ptr %pbuf,
                                %struct.dim3 %grid, %struct.dim3 %block,
                                i32 %smem, ptr %stream)

; after: CDP-1 intrinsic sequence
%pbuf = call ptr @__cudaCDP1GetParameterBufferV2(ptr @child_kernel,
                                                  %struct.dim3 %grid,
                                                  %struct.dim3 %block,
                                                  i32 %smem)
store ptr %arg0, ptr %pbuf, align 8
%pbuf.1 = getelementptr i8, ptr %pbuf, i64 8
store i32 %arg1, ptr %pbuf.1, align 4
%r = call i32 @__cudaCDP1LaunchDeviceV2(ptr @child_kernel, ptr %pbuf,
                                         %struct.dim3 %grid, %struct.dim3 %block,
                                         i32 %smem, ptr %stream)
```

The parameter-buffer rewrite is not merely a name swap. The V2 buffer-allocation stub takes the child-kernel pointer and launch geometry as arguments so the runtime can allocate a buffer sized exactly for the child's parameter layout; the high-level call only carried the size and alignment. The expander reconstructs the geometry by walking the matching `cudaLaunchDevice` and threading its dim3 arguments back to the buffer allocation, which is why both passes register together and the launch expander has to run after the parameter-buffer rewrite (or visit them as a pair).

### CDP Variant Selection

| Stub | Variant |
|---|---|
| `__cudaCDP1LaunchDeviceV2` | CDP-1 (single grid) |
| `__cudaCDP2LaunchDeviceV2` | CDP-2 (two grids) |
| `__cudaCDP1GetParameterBufferV2` | CDP-1 parameter buffer alloc |
| `__cudaCDP2GetParameterBufferV2` | CDP-2 parameter buffer alloc |

CDP variant selection (`CDP1` vs `CDP2`) comes from the call site's variant flag, not from the kernel signature. The stub names are held in two `const char*` lookup arrays — one for launch stubs, one for parameter-buffer stubs — indexed by the variant. A future CDP-3 variant slots in by adding the new entries to those arrays without touching the rewriter logic. Keep that indirection in a reimplementation: it turns the CDP runtime ABI into a data table rather than a control-flow tree.

### Matching Predicate

A call site is rewritable iff:

1. the callee resolves to one of the four high-level entry points (`cudaLaunchDevice`, `cudaLaunchDeviceV2`, `cudaGetParameterBuffer`, `cudaGetParameterBufferV2`);
2. the launched child resolves through `isKernelFunction` to a real PTX kernel;
3. the call site carries a CDP-variant flag (1 or 2);
4. the call site's parent function is itself a device function that the backend will lower to PTX.

A `cudaLaunchDevice` whose target resolves to an ordinary device function is a hard error: there is no PTX kernel entry to call, and the V2 launch stubs assume the callee is a real kernel. The expander emits a diagnostic and leaves the IR unchanged.

### Algorithm

```c
void expand_cdp_launches(Function *F) {
    for (Instruction &inst : instructions(F)) {
        if (auto *call = dyn_cast<CallInst>(&inst)) {
            Function *callee = call->getCalledFunction();
            if (!callee) continue;

            CdpKind k = classify_cdp_entry(callee);
            if (k == CDP_NONE) continue;

            Function *child = resolve_child_kernel(call);
            if (child && !isKernelFunction(child)) {
                emit_error(call, "CDP target is not a kernel");
                continue;
            }

            int variant = read_variant_flag(call);          // 1 or 2
            const char *stub = (k == CDP_LAUNCH)
                ? launch_stub_table[variant]
                : pbuf_stub_table[variant];

            rewrite_call_to_stub(call, stub);
        }
    }
}
```

### Failure Modes

- **Non-kernel target.** The diagnostic fires before the launch stub is wired up; the IR retains the original `cudaLaunchDevice` call and a later verifier flags it.
- **Variant flag missing.** A call site with no readable variant tag is rewritten to CDP-1 by default; this is correct on every existing CUDA toolchain but a reimplementation that omits the default produces an unrewritten call.
- **Parameter-buffer / launch mismatch.** When the rewriter sees a buffer alloc whose corresponding launch is unreachable in the same function, it falls back to the legacy `cudaGetParameterBuffer` ABI and emits a diagnostic; mixing legacy and V2 ABIs is supported but the user loses the V2 size-checking guarantees.

## Force-Inline Policy

### Input and Output IR Shape

`InlineMustPass` walks every call site and force-inlines callees marked with the `always_inline` attribute. The pass exists because parts of the NVPTX ABI can't lower certain helper signatures faithfully: image and sampler arguments must arrive at the kernel boundary as opaque handles, large aggregate arguments can't survive a call boundary, and some helpers exist solely so the frontend has somewhere to attach attributes that must be visible at the use site.

```llvm
; before
define internal float @sqrt_approx(float %x) "nvvm.always_inline" {
  %r = call float @llvm.nvvm.rsqrt.approx.f(float %x)
  %s = fmul float %r, %x
  ret float %s
}

define void @kernel(ptr addrspace(1) %p, float %x) {
  %v = call float @sqrt_approx(float %x)
  store float %v, ptr addrspace(1) %p, align 4
  ret void
}

; after: callee body inlined, internal callee dead-stripped
define void @kernel(ptr addrspace(1) %p, float %x) {
  %r = call float @llvm.nvvm.rsqrt.approx.f(float %x)
  %s = fmul float %r, %x
  store float %s, ptr addrspace(1) %p, align 4
  ret void
}
```

### Force-Inline Marker Propagation

Certain callees are unconditionally inlined regardless of whether the front-end marked them: math-library wrappers (the `__nv_*` family that wraps NVPTX intrinsics), the intrinsic-wrappers the frontend emits to attach `convergent` or `noreturn` to a callsite, and any helper whose body contains an NVPTX intrinsic that cannot survive an ABI boundary. The pass detects these by walking the callee's body for a small set of forced-inline-triggering opcodes; on a match it stamps the callee with `always_inline` itself before the inlining walk.

The propagation step is intentionally idempotent: a second run of the pass over already-marked IR is a no-op for the marker pass and either a no-op or a redundant inline for the inliner. This matters because some pass pipelines run `InlineMustPass` twice — once before CDP expansion and once after — and the marker must survive the first run untouched.

### Matching Predicate

A call site is forced-inline iff:

1. its callee carries `always_inline` (either from the front-end or from the marker-propagation step);
2. the callee has a body in this module (not an external declaration);
3. the call is not part of a recursive cycle the inliner cannot break;
4. the callee is not interposable.

The marker propagation step itself stamps `always_inline` on any internal callee whose body contains a forced-inline trigger and whose signature obeys the ABI constraints.

### Algorithm

```c
void inline_must_pass(Module *M) {
    // Phase 1: propagate the always-inline marker.
    for (Function &F : *M) {
        if (!F.isDeclaration() && contains_forced_inline_trigger(&F)) {
            F.addFnAttr("nvvm.always_inline");
        }
    }

    // Phase 2: actually inline.
    for (Function &caller : *M) {
        for (CallInst *call : calls_in(&caller)) {
            Function *callee = call->getCalledFunction();
            if (!callee || !callee->hasFnAttribute("nvvm.always_inline")) continue;

            if (!try_inline_at_call_site(call)) {
                emit_remark(&caller, "not AlwaysInline into ", caller.getName());
            }
        }
    }
}
```

When the inliner hits a callee it cannot inline — a recursive cycle, an exception handler frame, an interposable definition, or a callee whose body is unavailable — it emits a Remark of the form `"not AlwaysInline into "` followed by the caller's function name. The pass never silently downgrades the requirement: either the callee is inlined or the user receives the diagnostic and can fix the offending annotation.

### Failure Modes

- **Recursive always-inline.** Two functions both marked `always_inline` that call each other produce an infinite inline chain; the inliner breaks the cycle, emits the Remark, and leaves the cycle in place for later DCE.
- **Marker on a declaration.** An always-inline declaration without a body is unreachable: there is nothing to inline. The inliner emits the Remark and leaves the call.
- **Marker propagation false positive.** A reimplementation that lists too many opcodes as forced-inline triggers will stamp ordinary library helpers and inflate code size; the trigger set should be exactly the opcodes whose ABI requires inlining, not a heuristic.

## Kernel Info Printer

`KernelInfoPrinter` is a read-only diagnostic pass. It walks every function that satisfies `isKernelFunction` and emits one Remark per metric in a fixed `"kernel-info: <Metric> in function '<fn>' = <value>"` format. The metric set is exactly nineteen entries, in order: `regs`, `smem`, `cmem`, `tex`, `params`, `local`, `stack`, `barriers`, `loads`, `stores`, `branches`, `fp_ops`, `int_ops`, `divergence`, `predicated`, `vector_ops`, `mma_ops`, `tcgen05_ops`, `tma_ops`.

The last three are Blackwell-era additions. `mma_ops` counts WGMMA-family tensor-core instructions, `tcgen05_ops` counts the tensor-memory ops introduced for sm_100 and later, and `tma_ops` counts asynchronous bulk-copy instructions. Keep the metric list ordered in any reimplementation — downstream tooling parses the remark stream positionally and breaks the moment the order shifts.

## Pretreat

### Input and Output IR Shape

`PretreatPass` is the first cleanup stage after libNVVM accepts frontend IR. Its job is to strip or normalize frontend-specific forms before verification, address-space inference, and argument lowering start relying on them. The pass is deliberately narrow: it canonicalizes pointer casts, normalizes lifetime and memory intrinsics, strips metadata that earlier frontend stages already consumed, and rewrites placeholder intrinsics into the forms later NVVM passes expect. It performs no optimization that depends on the analysis results it precedes — the contract is "make the IR uniform without changing observable behavior".

```llvm
; before: typical frontend output
define void @k(ptr %p, i32 %n) {
entry:
  %cast1 = bitcast ptr %p to ptr addrspace(1)
  %cast2 = bitcast ptr addrspace(1) %cast1 to ptr
  call void @llvm.lifetime.start.p0(i64 -1, ptr %p)        ; -1 means "whole alloca"
  call void @llvm.memcpy.p0.p0.i32(ptr %p, ptr %p, i32 0, i1 false)  ; zero-length
  call void @llvm.nvvm.kernel.placeholder()                ; consumed by libNVVM
  call void @llvm.lifetime.end.p0(i64 -1, ptr %p)
  ret void
}

; after: canonical form
define void @k(ptr %p, i32 %n) {
entry:
  call void @llvm.lifetime.start.p0(i64 -1, ptr %p)
  ; zero-length memcpy deleted
  ; placeholder intrinsic deleted
  call void @llvm.lifetime.end.p0(i64 -1, ptr %p)
  ret void
}
```

### Matching Predicate

The pass is a sequence of independent rewrite rules. Each rule matches a fixed IR shape — an intrinsic call, a pointer cast, a metadata kind — and either deletes it, rewrites it into canonical form, or stamps a marker that later passes consume. No rule depends on the result of another rule running in the same pass invocation; the ordering is fixed for determinism but not for correctness.

### The 19 Metric-Ordered Cleanups

The cleanups run in a fixed order. Each is independent and idempotent, and the order is the one downstream passes assume. Reordering produces correct IR but breaks the verification-friendly invariants that the NVVM verifier checks for.

| # | Cleanup | Effect |
|---|---|---|
| 1 | Strip already-consumed `!nvvm.annotations` entries | Remove metadata entries whose attribute was already migrated. |
| 2 | Canonicalize trivial `bitcast` chains | Collapse `bitcast(bitcast(x))` to a single cast. |
| 3 | Drop no-op `addrspacecast` pairs | Remove `cvta`-to-self casts produced by the front-end. |
| 4 | Normalize `llvm.lifetime.{start,end}` sizes | Replace explicit alloca sizes with `-1` (whole-alloca) where the size matches. |
| 5 | Delete zero-length `llvm.memcpy` / `llvm.memmove` / `llvm.memset` | Remove explicit no-op moves. |
| 6 | Replace constant-fold-eligible casts | Fold `bitcast` of a constant into the constant itself. |
| 7 | Collapse `getelementptr` chains with zero indices | Drop GEPs that produce the same pointer they consume. |
| 8 | Canonicalize integer extensions | Choose `zext` over `sext` for known-non-negative sources where the front-end emitted the wrong one. |
| 9 | Strip `convergent` from non-convergent intrinsics | Remove a front-end-conservative `convergent` from intrinsics whose semantics do not require it. |
| 10 | Rewrite `llvm.nvvm.read.ptx.sreg.*` placeholder calls | Replace placeholder special-register reads with the canonical form. |
| 11 | Normalize `llvm.dbg.declare` to `llvm.dbg.value` | Convert variable-address debug info to value debug info where applicable. |
| 12 | Canonicalize `select` of constants | Reorder operands so the constant-true branch comes first. |
| 13 | Strip dead `llvm.assume` calls | Delete `assume(true)` and `assume(constant)` calls. |
| 14 | Replace `undef` operands in `memcpy` byte-count | Rewrite `undef` lengths to zero so cleanup 5 can delete them. |
| 15 | Canonicalize NaN/Inf floating-point literals | Convert non-IEEE-canonical NaN bit patterns to the canonical quiet NaN. |
| 16 | Strip discarded loop metadata | Remove `!llvm.loop` entries the front-end attached but the back-end ignores. |
| 17 | Lift `nvvm.kernel` metadata to function attribute | When `KernelAttrTransplanter` has not yet run, do the equivalent stamping. |
| 18 | Remove unreachable basic blocks | Delete BBs with no predecessor and no entry-block status. |
| 19 | Drop empty `llvm.global_ctors` / `llvm.global_dtors` entries | Clean up the trailing nulls some front-ends emit. |

The numbering is the canonical order. Cleanups 1, 16, and 19 strip metadata or globals; 2–8 simplify pointer and integer arithmetic; 9–14 normalize intrinsics and debug info; 15 fixes floating-point bit patterns; 17 is the legacy-attribute migration; 18 is the unreachable-block sweep that gives later passes a non-pessimistic dominator tree.

### Algorithm

```c
void pretreat_module(Module *M) {
    for (Function &F : *M) {
        if (F.isDeclaration()) continue;

        strip_consumed_annotations(&F);                     // 1
        canonicalize_bitcast_chains(&F);                    // 2
        drop_noop_addrspace_casts(&F);                      // 3
        normalize_lifetime_sizes(&F);                       // 4
        delete_zero_length_mem_intrinsics(&F);              // 5
        constant_fold_casts(&F);                            // 6
        collapse_zero_index_geps(&F);                       // 7
        canonicalize_int_extensions(&F);                    // 8
        strip_spurious_convergent(&F);                      // 9
        rewrite_sreg_placeholders(&F);                      // 10
        normalize_dbg_declare(&F);                          // 11
        canonicalize_select_constants(&F);                  // 12
        strip_dead_assume(&F);                              // 13
        normalize_undef_memcpy_lengths(&F);                 // 14
        canonicalize_fp_specials(&F);                       // 15
        strip_discarded_loop_metadata(&F);                  // 16
        lift_kernel_metadata_to_attr(&F);                   // 17
        remove_unreachable_blocks(&F);                      // 18
    }
    drop_empty_ctor_dtor_entries(M);                        // 19
}
```

### Failure Modes

- **Out-of-order cleanups.** Running cleanup 5 before cleanup 14 leaves `memcpy(_, _, undef)` in the IR; the verifier accepts it but the back-end emits a runtime call to a memcpy stub.
- **Skipping cleanup 17.** A kernel that retained only `!nvvm.annotations` and was never visited by `KernelAttrTransplanter` will not be recognized as a kernel by `isKernelFunction`'s criterion 2; criteria 1 and 3 still catch it, but cleanup 17 is what gives criterion 2 a chance.
- **Aggressive optimization in pretreat.** A reimplementation that adds an arithmetic-simplification rule to pretreat will change observable IR before the verifier runs, breaking the contract that pretreat is purely canonicalization. The rule belongs in the optimization passes downstream.

## Cross-References

[NVPTX Backend Passes Overview](overview.md#pipeline-position) shows where this cluster sits in the full NVPTX schedule. [NVVM IR Verifier](nvvm-ir-verifier.md#launch-argument-address-space-check) is the downstream consumer that re-checks `nvvm.kernel` on every CDP launch target. [cicc comparison](../boundaries/cicc-comparison.md) documents the shared NVPTX backend lineage these passes inherited.
