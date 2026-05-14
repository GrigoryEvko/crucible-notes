# Pass Manager Internals

## Abstract

Tileiras uses MLIR's nested pass-manager model. A top-level pass manager runs on a module, nested pass
managers run on operations such as `gpu.module` and `nv_tileaa.func`, and adaptors walk the IR to find
matching operations. The important contract is anchor correctness: a pass declared for one operation
type must be nested under a pass manager for that operation type.

## Nested Pass Managers

```c
typedef struct OpPassManager {
    OperationName anchor;
    Vector<Pass *> passes;
    bool verify_each;
} OpPassManager;

typedef struct OpToOpPassAdaptor {
    OpPassManager nested;
    bool run_parallel;
} OpToOpPassAdaptor;
```

The adaptor is itself a pass in the outer manager. When it runs, it walks the current operation and
applies the nested manager to every operation with the requested anchor.

## Anchors

Tileiras primarily nests under these anchors:

| Anchor | Role |
| --- | --- |
| `builtin.module` | Whole-module pipeline root. |
| `gpu.module` | Device module lowering and serialization preparation. |
| `nv_tileaa.func` | Per-function TileAA and TileAS scheduling/lowering. |

```c
void add_pass(OpPassManager *pm, Pass *pass) {
    if (!pass_anchor_is_empty(pass) && pass_anchor(pass) != pm->anchor) {
        fatal("pass must be nested under its anchor operation");
    }

    vector_push(&pm->passes, pass);
}
```

This check catches pipeline construction mistakes before any IR is mutated.

## Runtime Dispatch

```c
LogicalResult run_adaptor(OpToOpPassAdaptor *adaptor, Operation *root) {
    for (Operation *op : walk_operations(root)) {
        if (operation_name(op) != adaptor->nested.anchor) {
            continue;
        }

        if (!operation_is_isolated_from_above(op)) {
            return failure("nested pass anchor must be isolated from above");
        }

        if (failed(run_op_pass_manager(&adaptor->nested, op))) {
            return failure();
        }
    }

    return success();
}
```

`IsolatedFromAbove` matters because nested passes can otherwise observe or mutate values outside their
own scheduling boundary.

## Analyses and Instrumentation

Nested managers also own analysis caches and instrumentation records. A pass that preserves an
analysis should say so explicitly; otherwise downstream passes must recompute it.

```c
void invalidate_after_pass(AnalysisCache *cache, PreservedAnalyses preserved) {
    for (AnalysisId id : cache->entries) {
        if (!preserved_contains(preserved, id)) {
            analysis_cache_erase(cache, id);
        }
    }
}
```

Instrumentation hooks should surround each pass run and each nested pipeline run so timing reports
match the nested structure users see in textual pipelines.

## Reimplementation Checklist

1. Represent every nested pass manager with an explicit operation anchor.
2. Reject anchor mismatches during pipeline construction.
3. Verify that nested anchors are registered and isolated from above.
4. Walk the IR deterministically when applying nested managers.
5. Maintain per-anchor analysis caches.
6. Invalidate analyses unless the pass preserves them.
7. Emit instrumentation around nested pipeline entry and each pass.
8. Keep `gpu.module` and `nv_tileaa.func` nesting visible in textual pipeline dumps.
