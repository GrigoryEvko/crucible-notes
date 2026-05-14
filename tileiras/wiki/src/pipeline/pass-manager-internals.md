# Pass Manager Internals

## Abstract

Tileiras's pass manager is upstream MLIR's `PassManager` plus a small set of local conventions that make the nested structure predictable enough to reason about by inspection. This page documents those conventions: the anchor hierarchy that fixes which op type each nested pipeline targets, the `OperationName`-identity dispatch that adaptors use instead of string compare, the analysis-preservation discipline that the scheduling pipeline relies on, and the threading model that determines when the pass manager fans out across operations.

## Anchor Hierarchy

The pipeline nests three deep, with each level targeting one op type. The outermost level is the driver's `PassManager` itself, anchored on `builtin.module`. The next level is an `OpPassManager` reached through `pm.nest<GpuModuleOp>()`, anchored on `gpu.module`. The innermost level is reached through `gpu_pm.nest<...>()` for each function-shaped op the inner stages operate on; in practice that resolves to one of `nv_tileaa.func`, `nv_tileas.func`, or `llvm.func` depending on the stage of the cascade.

| Anchor | Role | Adaptor enters via |
|---|---|---|
| `builtin.module` | Driver root; dialect normalization, host-wrapper, gpu.module walk. | `PassManager::run` |
| `gpu.module` | Device-module lowering, scheduling, codegen preparation. | `OpToOpPassAdaptor` walking `builtin.module` |
| `nv_tileaa.func` | Per-function TileAA cleanup. | `OpToOpPassAdaptor` walking `gpu.module` |
| `nv_tileas.func` | Per-function TileAS scheduling and lowering. | `OpToOpPassAdaptor` walking `gpu.module` |
| `llvm.func` | Function-scoped MLIR-LLVM cleanup before translation. | `OpToOpPassAdaptor` walking `gpu.module` |

Adding a pass with a mismatched anchor is rejected at pass-manager construction time rather than at run time. The check uses the anchor `OperationName` already stored on the pass:

```c
void OpPassManager::addPass(std::unique_ptr<Pass> pass) {
    Optional<OperationName> required = pass->getOpName(getContext());
    if (required && *required != getOpAnchor()) {
        llvm::report_fatal_error(
            Twine("pass '") + pass->getName() +
            "' anchored on '" + required->getStringRef() +
            "' added to pipeline anchored on '" +
            getOpAnchor().getStringRef() + "'");
    }
    passes.push_back(std::move(pass));
}
```

## OperationName Dispatch

Adaptors do not compare op-name strings at run time. Each `OperationName` carries a `TypeID` that uniquely identifies its registered op class within the `MLIRContext`. The adaptor caches the anchor's `TypeID` once at construction and compares pointers during the walk. This makes the inner-loop check a single integer compare per op visited, which matters because the outer adaptor walks the entire `builtin.module` and the inner adaptor walks every nested operation under each `gpu.module`.

```c
LogicalResult OpToOpPassAdaptor::run(Operation *root) {
    TypeID anchorId = nestedAnchor.getTypeID();

    for (Region &region : root->getRegions()) {
        for (Block &block : region) {
            for (Operation &op : block) {
                if (op.getName().getTypeID() != anchorId) {
                    continue;
                }
                if (!op.hasTrait<OpTrait::IsIsolatedFromAbove>()) {
                    return op.emitOpError(
                        "nested pipeline anchor must be IsolatedFromAbove");
                }
                if (failed(runOnOperation(&op))) {
                    return failure();
                }
            }
        }
    }
    return success();
}
```

`IsIsolatedFromAbove` is what makes the dispatch sound. Without it, a nested pass could read or mutate SSA values defined above the anchor, which would let the threading model below race those values.

## Analysis Preservation Discipline

Each anchor level owns its own `AnalysisManager`. Analyses computed at the `gpu.module` level (target queries, kernel symbol tables, NVVM target attribute caches) outlive the function-scoped passes that consume them; analyses computed at the `nv_tileas.func` level (`ScheduleAnalysis`, register-pressure estimates) live only as long as their function passes do not invalidate them.

The pass manager invalidates everything not explicitly listed in the `PreservedAnalyses` set the pass returns. Tileiras follows a strict rule for the scheduling pipeline: any pass placed between `tileas-generate-schedule-constraints` and `tileas-materialize-schedule` must declare `ScheduleAnalysis` as preserved or the build is rejected. The check is enforced at pipeline construction:

```c
void verify_schedule_preservation(OpPassManager &pm) {
    bool inScheduleRegion = false;
    for (Pass &pass : pm.getPasses()) {
        if (pass.getArgument() == "tileas-generate-schedule-constraints") {
            inScheduleRegion = true;
            continue;
        }
        if (pass.getArgument() == "tileas-materialize-schedule") {
            inScheduleRegion = false;
            continue;
        }
        if (inScheduleRegion &&
            !pass.preserves<ScheduleAnalysis>()) {
            llvm::report_fatal_error(
                Twine("pass '") + pass.getName() +
                "' between schedule generation and materialization "
                "does not preserve ScheduleAnalysis");
        }
    }
}
```

This check moves a class of scheduling bugs from rare runtime symptoms (mismatched schedule, wrong II) to a deterministic pipeline-construction failure.

## Threading Model

When the outer adaptor is constructed with parallelism enabled and the anchor type is `IsolatedFromAbove`, the pass manager runs the nested pipeline on different `gpu.module` ops concurrently using its thread pool. Each thread takes a clone of the pass list and a fresh `AnalysisManager`; the only shared state is the `MLIRContext` (which is thread-safe by construction) and the `PassInstrumentation` chain (which serializes its own callbacks).

Tileiras enables parallelism for the outer `builtin.module` → `gpu.module` adaptor only. The inner `gpu.module` → function adaptors run sequentially because the per-function scheduling pipeline already saturates the thread pool through its own parallel solvers and because pass instrumentation is easier to read when function-level events from one device module do not interleave with another's.

```c
LogicalResult run_with_threading(OpToOpPassAdaptor &adaptor,
                                 Operation *root) {
    SmallVector<Operation *> targets;
    collect_anchor_operations(root, adaptor.anchor, targets);

    if (!adaptor.runInParallel) {
        for (Operation *op : targets) {
            if (failed(adaptor.runOnOperation(op))) {
                return failure();
            }
        }
        return success();
    }

    return parallelForEach(root->getContext(), targets,
        [&](Operation *op) { return adaptor.runOnOperation(op); });
}
```

The isolation guarantee that holds across both modes: a pass run on one anchor operation observes only that operation and its regions. Cross-anchor effects must travel through the shared context's symbol tables or through attributes attached to operations the outer pipeline visits.

## Cross-References

[Pipeline Invariants and Verifiers](invariants-and-verifiers.md) describes the verifier layers that run between the pass manager's pass invocations. [Driver Entry and Optimization Levels](driver-and-opt-levels.md) is where the pass manager is populated. [LLVM PassBuilder Registry](passbuilder-mega-registry.md) covers the textual resolution that produces the same pass graph at the LLVM tier.
