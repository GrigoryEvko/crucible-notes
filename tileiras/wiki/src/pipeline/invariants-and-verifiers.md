# Pipeline Invariants and Verifiers

## Abstract

Tileiras wraps three concentric verifier layers around its pass pipeline. The innermost layer is the `OperationName` verifier, which fires every time an op is built or modified and checks operand counts, result types, region structure, and trait predicates such as `IsolatedFromAbove`. The middle layer is the pass-manager between-pass verifier, which runs the full `verify()` on the anchor operation after each pass when verify-each is enabled and catches the broader class of failures that involve interactions between newly mutated ops. The outermost layer is the explicit module-level verifier suite that runs at fixed pipeline points and checks semantic rules requiring whole-module or target context, including the NVVM kernel-parameter overflow check. Each layer catches a class of bug the others cannot: per-op invariants surface immediately; cross-op invariants surface after the pass that introduced them; cross-pass invariants surface at named checkpoints.

A fourth layer — the NVVM IR verifier — runs after MLIR-to-LLVM conversion and catches NVPTX-specific errors that upstream LLVM's generic verifier ignores; a fifth, ptxas, closes the loop after PTX serialization. The full five-layer model and the bug-class-to-layer table are documented in [Correctness Layers](../topics/correctness-layers.md); this page covers the three in-pipeline layers in the order the pass manager invokes them.

## Verifier Layers

The three layers run in strict order around each pass invocation. The innermost layer is always active and cannot be disabled because it is part of op construction itself. The middle layer is on by default for non-Release builds and is gated on `verify-each` otherwise. The outermost layer is scheduled as named passes in the pipeline and runs only at the points the pipeline builder places them.

```c
LogicalResult run_pass_with_three_verifier_layers(
        OpPassManager &pm, Pass &pass, Operation *anchor) {

    // Layer 1: OperationName verifiers fired implicitly during op
    // construction inside the pass body. There is no scheduling step
    // for this layer; mutation through OpBuilder triggers the per-op
    // verifier and may fail before pass->run returns.
    if (failed(pass.run(anchor))) {
        return anchor->emitError("pass failed; per-op verifier may have fired");
    }

    // Layer 2: pass-manager between-pass verifier.
    if (pm.getVerifyEach()) {
        if (failed(verify(anchor, /*verifyRecursively=*/true))) {
            return anchor->emitError(
                "between-pass verifier rejected IR after '")
                << pass.getName() << "'";
        }
    }

    // Layer 3 runs only at explicit verifier passes (TileIR ops
    // analysis, agent verifier, NVVM verifier); those passes appear
    // in the pipeline's pass list like any other pass.
    return success();
}
```

The single ordering rule that ties the layers together: layer 1 fires before `pass->run` returns; layer 2 fires immediately after; layer 3 only fires when its named pass is reached. A break at any layer halts the pipeline with the originating pass and operation attached to the diagnostic.

## Layer-1 Example: Per-Op Structural Rejection

A builder that constructs `nv_tileas.async.tiled_tma_load` with the wrong coordinate count is rejected immediately by the per-op verifier. The op's verify body reads the descriptor operand's box rank, walks the coordinate operand slot range, and compares counts. The diagnostic is emitted before `OpBuilder::create` returns:

```text
'nv_tileas.async.tiled_tma_load' op expects 3 coordinates, but got 2
```

The literal partial string in the binary is `" coordinates, but got "`; the expected count is derived from the view's rank plus an optional `+1` when the view carries a TMA descriptor with a leading offset. The diagnostic surfaces inside the pass that built the op, so the failure points at the rewrite pattern that emitted the wrong shape rather than at a later consumer that would have seen the malformed IR.

## Layer-2 Example: Partial-Rewrite Detection

`MaterializeAsync` rewrites every pipeline op into a pair of producer and consumer regions. The pass-level verifier catches a half-rewritten state — a `consume_one` whose paired `produce_one` was rewritten but whose region block-argument types still match the pre-rewrite producer-type list. The region-op verifier walks the block-argument list against the producer-type attribute and emits:

```text
'nv_tileas.async.pipeline.consume_one' op expects region arguement types to match with producer types [...], but got: [...]
```

The typo `arguement` is preserved verbatim — downstream log-scraping infrastructure matches on it. The diagnostic fires at the boundary of `MaterializeAsync`, not at the next pass that would have consumed the inconsistent region. See [nv_tileas Verifiers — Region-Op Verifier Template](../dialects/nv_tileas/verifiers.md#region-op-verifier-template) for the verifier body.

## Layer-3 Example: Missing Kernel Metadata

A late LLVM-tier cleanup pass can strip function attributes after `KernelAttrPass` has stamped `nvvm.kernel` but before the NVVM verifier runs. The module-level verifier walks the function list, sees a kernel-shaped function without the kernel attribute, and rejects the module. The verifier predicate is the canonical `isKernelFunction` four-criteria disjunction documented in [Kernel Identity](../nvptx-passes/kernel-cdp-inline-pretreat.md#kernel-identity); when none of the four criteria fires for a function the rest of the pipeline treats as a kernel, the inconsistency surfaces here rather than as a missing-`.entry` directive in the emitted PTX.

## Explicit Verifier Passes

| Verifier | Stage | Contract |
| --- | --- | --- |
| TileIR operation analysis | Before LLVM conversion in the full pipeline. | Check TileIR region, atom, schedule, and metadata invariants. |
| TileAA agent verifier | Warp-specialized TileAA path. | Check producer/consumer agent graph shape. |
| [NVVM IR verifier](../nvptx-passes/nvvm-ir-verifier.md) | After target conversion and before NVPTX backend lowering. | Check kernel launches and formal parameter-space usage. |

The TileIR verifier runs before high-level operations are erased — once `convert-tileas-to-llvm` removes the Tile schedule attributes, the verifier has nothing to inspect. The NVVM verifier runs after kernel metadata and address-space attributes have been attached because the parameter-space check depends on the resolved data layout.

The NVVM verifier enforces two behaviors that matter to users. A device launch target must be a kernel (a non-kernel call through a launch op is rejected at this layer rather than at the backend; see [Launch-Argument Address-Space Check](../nvptx-passes/nvvm-ir-verifier.md#launch-argument-address-space-check)). A kernel's formal parameter buffer must fit the selected target's parameter-space limit; the verifier walks the argument list, applies the target's data layout, and compares the cumulative size against the limit (the per-SM limits are listed in [ParamSpaceLimit by SM Family](../nvptx-passes/nvvm-ir-verifier.md#paramspacelimit-by-sm-family)). It also emits a warning when a child launch receives a pointer to parent-local or CTA-shared memory: the warning is non-fatal because the IR is well-formed, but the child dereference is undefined behavior and the warning is the only place users see it.

## Ordering Invariants

| Invariant | Required order |
| --- | --- |
| Frontend conversion | `cuda_tile` to TileAA before any TileAA function pass. |
| TileAA lowering | TileAA to TileAS before TileAS-to-LLVM and TileAS-to-NVGPU consumers. |
| TileAS lowering | TileAS-to-LLVM before consumers that expect LLVM-compatible values. |
| TileIR semantic verification | Before LLVM conversion erases TileIR structure. |
| Cleanup bracketing | Canonicalizer and CSE around major dialect conversions. |
| NVVM verification | After kernel metadata and address-space conversion. |
| Target serialization | Only after no high-level TileIR ops remain. |

## NVVM Parameter Verification

The kernel-parameter overflow check is the most user-visible piece of layer-3 verification because it is the first place where a target-specific limit can reject otherwise valid TileIR. The verifier walks each kernel argument, asks the target data layout for size and ABI alignment, accumulates an aligned offset, and compares the total against the target's parameter-space limit.

```c
LogicalResult verify_kernel_parameters(LLVMFuncOp kernel,
                                       NvvmTargetAttr target,
                                       const DataLayout &dl) {
    uint64_t total = 0;
    for (auto [idx, argType] : llvm::enumerate(kernel.getArgumentTypes())) {
        TypeSize size = dl.getTypeSize(argType);
        Align    align = dl.getABITypeAlign(argType);
        if (size.isScalable()) {
            return kernel.emitOpError("argument ") << idx
                << " has scalable type; not supported in NVVM kernels";
        }
        total = llvm::alignTo(total, align.value());
        total += size.getFixedValue();
    }

    if (total > target.getParameterSpaceLimit()) {
        return kernel.emitOpError("formal parameter space overflowed: ")
            << total << " > " << target.getParameterSpaceLimit()
            << " bytes for " << target.getChip();
    }
    return success();
}
```

The limit is target-dependent. Pre-Hopper SM versions allow 4096 bytes; Hopper and later allow 32760 bytes. The verifier reads the limit from the resolved `#nvvm.target` attribute so that a kernel rejected on one architecture can succeed on another without touching the verifier code.

## Cross-References

[Correctness Layers](../topics/correctness-layers.md) is the canonical overview that places the three in-pipeline layers covered here alongside the NVVM IR verifier and ptxas, and gives the bug-class-to-layer table. [Pass Manager Internals — Anchor Hierarchy](pass-manager-internals.md#anchor-hierarchy) documents the anchor and dispatch model the verifier layers run inside. [Pass List by Optimization Level](full-pass-list-by-opt-level.md) is where each explicit verifier pass appears in the pipeline. [Pipeline Options Mapping](options-mapping.md) covers the options that enable or disable verify-each behavior. [NVVM IR Verifier](../nvptx-passes/nvvm-ir-verifier.md) is the LLVM-tier sibling that re-checks parameter-space and address-space constraints after the MLIR-to-LLVM translation.
