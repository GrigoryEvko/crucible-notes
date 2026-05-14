# nv_tileas Folds and Memory Consistency

## Abstract

`nv_tileas` canonicalization is deliberately split. Pure tile-structure rewrites simplify `alloc_tensor`, `insert_slice`, `extract_slice`, `view`, and structured control-flow scaffolding. Memory-ordering operations sit behind `MemoryConsistencyOpInterface` — pure canonicalizations must not reorder, duplicate, or erase them.

This page describes which rewrites are safe, which operations carry memory-consistency behavior, and how a reimplementation keeps folding separate from ordering-sensitive transformations.

## Folding Model

Most useful TileAS simplification lives in rewrite patterns rather than per-operation constant folds. That is the right design for this dialect — the interesting cases are structural, typically an `scf.for` or `scf.if` plus tile slice operations, not a single operation with constant operands.

Pipeline-related lowering may still invoke ordinary MLIR folding during one-to-N conversion. Treat those folds as local simplifications only. Larger layout-chain removal belongs to the layout-conversion removal pass, not to a hidden `convert_layout` fold.

## Canonicalization Patterns

The dialect installs seven canonicalization patterns.

| Pattern | Root | Rewrite |
|---|---|---|
| simplify extract slice | `nv_tileas.extract_slice` | constant offsets and strides become a static-shape view |
| decompose loop iter args | `scf.for` | sinks `alloc_tensor` into the loop body and removes redundant iter args |
| decompose if by insert slice | `scf.if` | duplicates allocation and insertion chains into each branch |
| decompose if by extract slice | `scf.if` | sinks extraction into each branch |
| swap view and extract slice | `nv_tileas.extract_slice` | rewrites `extract_slice(view(x))` into `view(extract_slice(x))` when legal |
| coalesce perfectly nested loops | `scf.for` | flattens compatible nested loops |
| simplify extract from insert | `nv_tileas.extract_slice` | replaces exact extract-after-insert with the inserted source |

The two structural decomposition patterns matter most — they prepare loop-carried tile state for scheduling and materialization.

```c
LogicalResult decompose_for_iter_arg(ScfForOp loop, Rewriter *rw) {
    for (IterArg arg : loop.iter_args()) {
        SliceChain init = trace_insert_slice_chain(arg.init());
        SliceChain yield = trace_insert_slice_chain(arg.yielded_value());

        if (!init.ends_at_alloc_tensor() || !yield.ends_at_same_alloc(init)) {
            return failure();
        }
        if (chain_crosses_memory_consistency_op(init, yield)) {
            return failure();
        }

        sink_alloc_tensor_into_loop(loop, init, rw);
        reemit_insert_slice_chain(loop.body(), init, rw);
        remove_iter_arg(loop, arg, rw);
    }

    return success();
}
```

For `scf.if`, the branch patterns require both arms to chain back to the same source allocation or extraction shape. Each arm receives its own allocation or extraction so the rewrite never creates a shared mutable tile across branches.

## Memory Consistency Interface

`MemoryConsistencyOpInterface` marks operations whose ordering matters. Canonicalization may inspect them, but pure tile rewrites must not move across them or erase them.

| Operation group | Why it participates |
|---|---|
| async load/store/copy/dot | has visible async memory ordering |
| async waits | observes completion of async work |
| async TMA load/store/reduction/gather/scatter | consumes descriptor and memory-ordering semantics |
| synchronous `copy` | may observe or publish data relevant to async regions |
| `make_tiled_tma_desc` | descriptor result is consumed by TMA operations |
| `reduce` and `scan` | region bodies may carry ordering-sensitive operations |

Pure tile-shaping operations are intentionally excluded:

- `alloc_tensor`;
- `insert_slice`;
- `extract_slice`;
- `view`;
- `async.future_wait`;
- async pipeline region plumbing.

The first four are pure SSA tile structure. `future_wait` gets its ordering from the future token itself. Pipeline region operations carry ordering through producer/consumer interfaces and tokens, not through memory semantic attributes.

## Safe Rewrite Rule

A canonicalization pattern is safe when every operation it moves, duplicates, or erases sits outside the memory-consistency set.

```c
bool pure_tile_chain(Operation *op) {
    while (op != NULL) {
        if (implements_memory_consistency(op)) {
            return false;
        }
        if (!is_tile_structure_op(op) && !isa<arith::ConstantOp>(op)) {
            return false;
        }
        op = next_defining_op_in_chain(op);
    }

    return true;
}
```

That rule lets the canonicalizer transform slice scaffolding aggressively while preserving every async memory-ordering attribute and dependency.

## Layout Conversion Folding

The identity `convert_layout(convert_layout(x))` belongs to the layout-conversion removal pass, not to a local `convert_layout` fold. The legality of commuting or deleting a layout conversion depends on whether the value lives in register space, shared memory, tensor memory, or crosses a pipeline boundary — and only the pass has that context.

```c
LogicalResult remove_redundant_layout_chain(ConvertLayoutOp op, Rewriter *rw) {
    ConvertLayoutOp inner = op.source().get_defining_op<ConvertLayoutOp>();
    if (!inner) {
        return failure();
    }

    if (!layouts_compose_to_direct_path(inner.source_layout(), op.dest_layout())) {
        return failure();
    }

    Value replacement = build_direct_layout_conversion(inner.source(), op.dest_layout(), rw);
    rw->replace_op(op, replacement);
    return success();
}
```

Keeping this in a pass rather than a fold lets the compiler consult target atom plans and memory-space rules.

## Ordering Invariants

- Canonicalization roots may be pure tile ops or structured control-flow ops.
- Match chains may include `alloc_tensor`, `insert_slice`, `extract_slice`, `view`, and constants.
- Match chains must reject `copy`, async memory operations, TMA operations, reductions, scans, and descriptor builders.
- Rewrites must not alter memory semantic, memory scope, in-bounds, padding, or RMW attributes.
- Branch decomposition must duplicate allocations per branch rather than share a mutable tile across arms.
- Layout-chain removal belongs to the layout conversion pass, where target layout plans are available.

