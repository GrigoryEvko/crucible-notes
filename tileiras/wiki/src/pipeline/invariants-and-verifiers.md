# Pipeline Invariants and Verifiers

## Abstract

Tileiras keeps the pipeline correct through three verifier layers: pass-manager anchor checks, verifier
runs between passes, and explicit target-aware verifier passes. These layers catch different classes of
bugs. Anchor checks prevent a pass from being scheduled on the wrong operation type. Between-pass
verification catches malformed IR immediately after the pass that created it. Explicit verifier passes
check semantic rules that require whole-module or target context.

## Verifier Layers

| Layer | When it runs | What it catches |
| --- | --- | --- |
| Anchor checks | While building or scheduling the pass manager. | A pass nested under the wrong operation type. |
| Verify-each | Between transformation passes. | Broken operation, type, region, and trait invariants. |
| Explicit verifiers | At selected pipeline points. | Schedule, launch, ABI, and target-specific rules. |

```c
LogicalResult run_pipeline_with_verification(PassManager *pm, Operation *root) {
    for (Pass *pass : pm->passes) {
        if (!pass_can_run_on(pass, root)) {
            return failure("pass anchor does not match operation");
        }

        if (failed(pass->run(root))) {
            return failure("pass failed");
        }

        if (pm->verify_each && failed(verify(root))) {
            return failure("IR failed verification after pass");
        }
    }

    return verify(root);
}
```

## Explicit Verifiers

| Verifier | Stage | Contract |
| --- | --- | --- |
| TileIR operation analysis | Before LLVM conversion in the full pipeline. | Check TileIR region, atom, schedule, and metadata invariants. |
| TileAA agent verifier | Warp-specialized TileAA path. | Check producer/consumer agent graph shape. |
| NVVM IR verifier | After target conversion and before NVPTX backend lowering. | Check kernel launches and formal parameter-space usage. |

The TileIR verifier must run before high-level operations are erased. The NVVM verifier must run after
kernel metadata and address-space attributes have been attached.

The NVVM verifier enforces two recovered behaviors that matter to users:

- a device launch target must be a kernel,
- a kernel's formal parameter buffer must fit the selected target's parameter-space limit.

It also warns when a child launch receives a pointer to parent-local or CTA-shared memory. That warning
is accepted IR, but the child dereference is undefined.

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

The NVVM verifier accounts for each kernel parameter using the target data layout and rejects
signatures that cannot fit the target's parameter-space buffer.

```c
void verify_kernel_parameters(Function kernel, TargetInfo target, DataLayout layout) {
    uint64_t total = 0;

    for (Argument arg : kernel.arguments) {
        SizeAlign sa = size_and_abi_alignment(arg.type, layout);

        if (sa.scalable) {
            error(arg, "scalable parameter type is not supported");
        }

        total = align_up(total, sa.alignment);
        total += sa.size;
    }

    if (total > target.parameter_space_limit) {
        error(kernel, "formal parameter space overflowed");
    }
}
```

