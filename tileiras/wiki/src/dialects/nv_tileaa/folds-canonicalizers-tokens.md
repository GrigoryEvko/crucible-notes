# nv_tileaa Folds, Canonicalizers, Tokens

## Abstract

`nv_tileaa` is where the tile pipeline turns high-level intent into a cleaner,
alias-aware program. Verification proves the IR is structurally legal;
canonicalization makes it useful. The folds that matter strip redundant shape
wrappers, fuse pointer arithmetic, prune dead queue and pragma results,
simplify masked memory operations, reduce atomic identities, and preserve
memory ordering through a small token algebra.

This page lays out those transformations as algorithms. A reimplementation
doesn't need the same pattern classes or registration order, but it does need
the same observable rewrites and the same safety conditions.

## Canonicalization Surface

| Area | Rewrite | Safety condition |
|---|---|---|
| Dot folding | Fold constant dot operands into a constant result or an accumulator identity. | Both multiplicands and the accumulator path are compile-time constants or identity values. |
| Pointer arithmetic | `addptr(addptr(base, a), b)` becomes `addptr(base, a + b)`. | The two offsets use the same element-size interpretation and address space. |
| Assumptions over splats | `assume(splat(x), pred)` becomes `splat(assume(x, pred))`. | The predicate is elementwise and does not depend on lane identity. |
| Select over splats | `splat(select(c, t, f))` becomes `select(splat(c), splat(t), splat(f))`. | All three splats have the same result shape. |
| Masked load | Constant-true mask becomes an unmasked load; constant-false mask becomes the fallback value. | The fallback value has the exact load result type. |
| Masked store | Constant-true mask becomes an unmasked store; constant-false mask erases the store. | The store has no other required side effect besides the memory write. |
| Shape wrappers | Fuse nested `view`; fold `view`, `broadcast`, and `expand_dims` around `splat`. | Element count and result shape remain equal to the original result type. |
| Extract motion | Move `extract` through elementwise operations and through matching `expand_dims`. | The extracted lane maps one-to-one to the source lane. |
| Queue result pruning | Drop unused `queue.get` results and update the matching `queue.yield`. | Result order for the remaining values is preserved. |
| Pragma result pruning | Drop unused pragma-carried results and rewrite the region terminator. | The pragma's semantic payload is independent of the removed result. |
| Atomic identities | Reduce no-op or identity atomics to cheaper reads or preserved tokens. | The selected atomic mode has a true algebraic identity for the operand type. |

## Pattern Driver

Implement the canonicalization pass as an ordinary greedy MLIR-style rewrite
loop. The trick is to register shape and memory folds together, since a shape
fold often exposes a memory fold on the next iteration.

```c
void populate_tileaa_canonicalizers(PatternSet *patterns) {
    add(patterns, fold_constant_dot);
    add(patterns, fuse_addptr_chain);
    add(patterns, push_assume_through_splat);
    add(patterns, push_select_through_splat);
    add(patterns, canonicalize_masked_load);
    add(patterns, canonicalize_masked_store);
    add(patterns, fold_expand_dims_of_splat);
    add(patterns, fold_view_chain);
    add(patterns, fold_broadcast_of_splat);
    add(patterns, hoist_extract_through_elementwise);
    add(patterns, prune_queue_get_results);
    add(patterns, prune_pragma_results);
    add(patterns, fold_atomic_cas);
    add(patterns, fold_atomic_rmw);
}

void canonicalize_tileaa(Module module) {
    PatternSet patterns;
    populate_tileaa_canonicalizers(&patterns);
    run_greedy_rewrite(module, patterns);
}
```

## Constant Dot Folding

`dot` is the only expensive arithmetic fold in the dialect, and it respects
the same element-type and accumulator rules as the verifier. Folding is legal
when all inputs needed for the multiply-accumulate are constant-like, or when
a zero multiplicand proves the result equals the accumulator unchanged.

```c
Optional<Value> fold_dot(DotOp op) {
    if (is_zero_tile(op.a) || is_zero_tile(op.b)) {
        return op.accumulator;
    }

    ConstantTile a = dyn_cast_constant_tile(op.a);
    ConstantTile b = dyn_cast_constant_tile(op.b);
    ConstantTile c = dyn_cast_constant_tile(op.accumulator);

    if (!a.valid || !b.valid || !c.valid) {
        return none();
    }

    ConstantTile result = c;
    for (int m = 0; m < op.m; ++m) {
        for (int n = 0; n < op.n; ++n) {
            for (int k = 0; k < op.k; ++k) {
                result[m, n] += convert(a[m, k]) * convert(b[k, n]);
            }
        }
    }

    return materialize_constant(result, op.result.type);
}
```

For integer MMA, the `convert` step honors the operation's signedness
attributes. For floating MMA, it honors the accumulator type — never the
narrow input format, which would silently drop precision.

## Pointer and Shape Folds

Pointer-arithmetic canonicalization keeps addressing expressions shallow.
The fold is safe only when both offsets are measured in the same logical
element units. If one offset has already been converted to bytes and the
other has not, the rewriter normalizes them before adding.

```c
Optional<AddPtrOp> fuse_addptr_chain(AddPtrOp outer) {
    AddPtrOp inner = dyn_cast_addptr(outer.base);
    if (!inner.valid) {
        return none();
    }

    require(inner.result.address_space == outer.result.address_space);
    IndexValue lhs = normalize_offset(inner.offset, inner.result.element_type);
    IndexValue rhs = normalize_offset(outer.offset, outer.result.element_type);
    IndexValue fused = add_index_values(lhs, rhs);

    return rebuild_addptr(inner.base, fused, outer.result.type);
}
```

Shape folds all follow one rule: remove wrappers that don't change the
logical element stream, but keep the final result type exactly as the
original op requested.

```c
Optional<Value> fold_shape_wrapper(Op op) {
    if (op.kind == VIEW && producer_is_view(op.input)) {
        return rebuild_view(op.input.source, op.result.type);
    }

    if (op.kind == VIEW && producer_is_splat(op.input)) {
        return rebuild_splat(op.input.scalar, op.result.type);
    }

    if (op.kind == BROADCAST && producer_is_splat(op.input)) {
        return rebuild_splat(op.input.scalar, op.result.type);
    }

    if (op.kind == EXPAND_DIMS && producer_is_splat(op.input)) {
        return rebuild_splat(op.input.scalar, op.result.type);
    }

    return none();
}
```

## Masked Memory Folds

Masked load and store folds look simple but are easy to get wrong: memory
effects and token results must stay valid. A constant-false load performs no
read, so it returns the fallback data and the original token. A
constant-false store performs no write, so the op disappears and its token
users get rewired to the incoming token.

```c
RewriteResult canonicalize_masked_load(LoadOp op) {
    if (!op.mask.is_constant()) {
        return no_change();
    }

    if (op.mask.is_true()) {
        return replace_with_unmasked_load(op);
    }

    Value fallback = op.other.has_value ? op.other.value : undef(op.result.type);
    replace_value(op.data_result, fallback);
    replace_value(op.token_result, op.input_token);
    erase(op);
    return changed();
}

RewriteResult canonicalize_masked_store(StoreOp op) {
    if (!op.mask.is_constant()) {
        return no_change();
    }

    if (op.mask.is_true()) {
        return replace_with_unmasked_store(op);
    }

    replace_value(op.token_result, op.input_token);
    erase(op);
    return changed();
}
```

## Atomic Folds

Atomic folds are strength reductions, not permission to erase memory
ordering. Even when the data operation becomes a load or no-op, token users
still need to see the correct ordering edge.

```c
RewriteResult fold_atomic_cas(AtomicCasOp op) {
    Optional<Constant> compare = constant_value(op.compare);
    Optional<Constant> replacement = constant_value(op.replacement);

    if (!compare.has_value || !replacement.has_value) {
        return no_change();
    }

    if (constants_equal(compare.value, replacement.value)) {
        Value loaded = atomic_load(op.address, op.ordering, op.scope);
        replace_value(op.data_result, loaded);
        replace_value(op.token_result, sequence_after(op.input_token, loaded));
        erase(op);
        return changed();
    }

    return rebuild_with_constants(op, compare, replacement);
}

RewriteResult fold_atomic_rmw(AtomicRmwOp op) {
    Optional<Constant> rhs = constant_value(op.value);
    if (!rhs.has_value) {
        return no_change();
    }

    if (is_identity_for_rmw(op.mode, rhs.value)) {
        Value loaded = atomic_load(op.address, op.ordering, op.scope);
        replace_value(op.data_result, loaded);
        replace_value(op.token_result, sequence_after(op.input_token, loaded));
        erase(op);
        return changed();
    }

    return no_change();
}
```

For `add`, `or`, and `xor`, the identity is zero. For `and`, it is all bits
set. For exchange, the fold is legal only when another proof says the stored
value is already present — constant equality with a compare operand is not
enough unless that compare participates in the same atomic contract.

## Queue and Pragma Pruning

Queue and pragma ops often carry multiple results because earlier lowering
doesn't yet know which values get consumed. Once ordinary DCE has marked some
results unused, canonicalization shrinks the result list and updates the
region terminator to yield only the survivors.

```c
RewriteResult prune_region_results(RegionOp op, Terminator terminator) {
    BitSet live = live_result_indices(op);
    if (live.count == op.results.count) {
        return no_change();
    }

    SmallVector<Type> new_types;
    SmallVector<Value> new_yields;

    for (int i = 0; i < op.results.count; ++i) {
        if (!live.contains(i)) {
            continue;
        }

        new_types.push(op.results[i].type);
        new_yields.push(terminator.operands[i]);
    }

    RegionOp replacement = clone_with_result_types(op, new_types);
    replacement.terminator.operands = new_yields;
    replace_live_results(op, replacement, live);
    erase(op);
    return changed();
}
```

This fold preserves relative order. Reordering live queue results changes the
meaning of downstream consumers, even when the types happen to match.

## Memory Token Lowering

At TileAA level, `mem_token` is an abstract SSA value. By TileAS and NVVM
level it has become a compact phase-bearing integer tied to async barrier
state. The exact integer encoding is a backend choice; the required
semantics are that a joined token cannot be considered complete until every
input token is complete, and that every memory effect produces a successor
token.

```c
typedef struct {
    int barrier_id;
    int phase;
} LoweredToken;

LoweredToken lower_create_mem_token(BarrierAllocator *allocator) {
    int barrier = allocator->allocate();
    emit_mbarrier_init(barrier);
    return (LoweredToken){ .barrier_id = barrier, .phase = 0 };
}

LoweredToken lower_join_mem_token(ArrayRef<LoweredToken> inputs) {
    LoweredToken result = inputs[0];

    for (LoweredToken token : inputs.drop_front()) {
        result = later_of(result, token);
    }

    return result;
}

LoweredToken sequence_memory_effect(LoweredToken input, MemoryEffect effect) {
    emit_effect_after_token(effect, input);
    return toggle_phase_when_needed(input, effect);
}
```

## Mem-Token Lifecycle

`nv_tileaa.mem_token` is the linear-type SSA value that threads memory-ordering edges through the IR. Produced by
`create_mem_token`, consumed by `join_mem_token`, ultimately materialised as an `mbarrier` physical handle by the
downstream lowering passes. The mem_token is a pure ordering edge — no user-visible data, only a proof that every
preceding memory effect on the edge has completed before any successor effect observes it. That mechanism is what
lets the scheduler reason about async copy, WGMMA, and TMA completion without baking specific barrier hardware into
the upper-dialect IR.

The TypeID slot for `nv_tileaa.mem_token` is the static-sentinel at `&unk_5B46F78`. Pointer-identity dispatch against
this slot is how walkers, type converters, and verifiers recognise mem_token values without parsing their printed
form. Anchor the type with one stable address per process, not a per-context allocation — cross-pass machinery
compares the slot by pointer.

The mem_token reaches its `mbarrier` physical form in two lowering hops. Both are pattern-driven and leave the
token-graph topology intact, changing only the underlying carrier type.

```text
cuda_tile.make_token                    nv_tileaa.create_mem_token              nvvm.mbarrier.init (i32 handle)
        |                hop 1                       |               hop 2                |
        v          (CudaTile -> TileAA               v        (TileAA -> TileAS           v
cuda_tile.join_tokens   sub_5F8DC0)        nv_tileaa.join_mem_token   sub_110B730)   nvvm.mbarrier.try_wait.parity.shared
```

Hop one runs inside the Part-B populator of ConvertCudaTileToTileAA at `sub_5F8DC0`. The routine rewrites every
`cuda_tile.make_token` into an `nv_tileaa.create_mem_token` and every `cuda_tile.join_tokens` into the matching
`nv_tileaa.join_mem_token`. Op kinds receive new TypeIDs at the rewrite boundary, but operand counts, result
counts, and ordering semantics survive one-for-one. No barrier resource is allocated yet; the token is still an
abstract SSA value.

Hop two runs inside the TileAA-to-TileAS conversion driver at `sub_110B730`. Two conversion patterns dominate:

- `CreateMemTokenOpConversion`, dispatched through vtable `off_59D53C0`, turns each `nv_tileaa.create_mem_token` into
  an `mbarrier.init` LLVM intrinsic call. The op's SSA result becomes a 32-bit handle that mbarrier hardware tracks
  per CTA. The init's phase-count operand is the number of producers the original token expected to merge into the
  barrier, threaded from the op's producer attribute.
- `JoinMemTokenOpConversion`, dispatched through vtable `off_59D5410`, turns each `nv_tileaa.join_mem_token` into a
  chain of `mbarrier.try_wait.parity.shared` calls, one per producer in the join. The chain encodes the spin loop on
  the parity bit so the joined token cannot retire until every input producer has flipped its share of the phase.

When a builder emits `create_mem_token` without an explicit result type, the op's `inferReturnTypes` hook supplies
one. The hook implements a five-step algorithm:

1. Walk the operands to find the alias-set that defines the token's scope.
2. Look up the scope's existing token type via the surrounding function's local TypeConverter.
3. If no existing token type exists for the scope, create a fresh `MemTokenType` carrying the inferred scope.
4. Stash the inferred type in the function's TypeConverter cache so later builders share it.
5. Return the cached type as the op's result type.

That caching keeps mem_token types pointer-equal across a function even when the builder fires from many different
rewrite sites. A reimplementation that re-derives the type per call site fragments the cache and breaks the
pointer-identity dispatch above.

After hop two completes, the mem_token is fully replaced. The successor `i32` value holds the per-CTA mbarrier
index; the mbarrier slot itself comes from D-pass buffer-assignment out of the per-CTA 32-mbarrier pool. Each
`create_mem_token` op claims one slot from that pool — see
[`scheduler/buffer-assignment-and-mbarriers.md`](../../scheduler/buffer-assignment-and-mbarriers.md) for the
allocation details. Pool exhaustion is a hard failure of the lowering pipeline, never a fallback to software
ordering.

```c
Value lowerCreateMemToken(Op op, ConversionPatternRewriter &rw) {
    Value mbarrier = rw.create<nvvm::MbarrierInitOp>(loc, /*phaseCount=*/op.getNumProducers());
    return mbarrier;
}

Value lowerJoinMemToken(Op op, ConversionPatternRewriter &rw, ArrayRef<Value> tokens) {
    Value tryWait = rw.create<nvvm::MbarrierTryWaitParitySharedOp>(loc, tokens.front(), /*phase=*/0);
    /* spin loop on parity bit, repeated for each remaining input token */
    return tryWait;
}
```

The cross-reference pages cover the supporting machinery: [`op-roster.md`](op-roster.md) for the `create_mem_token`
and `join_mem_token` op rosters, [`lowering/cuda-tile-to-tileaa.md`](../../lowering/cuda-tile-to-tileaa.md) for hop
one, [`lowering/tileaa-to-tileas.md`](../../lowering/tileaa-to-tileas.md) for hop two, and
[`scheduler/buffer-assignment-and-mbarriers.md`](../../scheduler/buffer-assignment-and-mbarriers.md) for mbarrier
slot allocation.

## Plugin and Queue Contract

Plugin operations carry resource requirements the scheduler must honor:
register budget, shared-memory scratch, tensor-memory scratch, named
barriers, input layouts, output layouts. Queue lowering consumes those
requirements while turning queue regions into TileAS producer and consumer
pipeline regions.

```c
LogicalResult lower_plugin_execute(ExecuteOp execute, ResourceModel model) {
    require(model.registers_available(execute.max_registers));
    require(model.named_barriers_available(execute.named_barriers));
    require(model.shared_memory_available(execute.shared_memory_bytes));
    require(model.tensor_memory_available(execute.tensor_memory_bytes));

    PipelineRegion region = materialize_agent_region(execute);
    attach_plugin_layouts(region, execute.input_layouts, execute.output_layouts);
    return success();
}
```

The queue conversion fails loudly when a producer or consumer cannot map to
a pipeline slot. Silent fallback to unordered memory traffic loses the main
correctness property the queue was carrying.

## Invariants

- Canonicalization must preserve memory tokens even when it removes data work.
- Shape folds may change producer structure but not the final result type.
- Pointer folds must normalize offsets before adding them.
- Queue and pragma pruning preserve live-result order.
- Atomic folds must preserve memory ordering and volatility semantics.
- Token lowering may choose any compact representation that preserves join and
  successor ordering.

## Reimplementation Checklist

1. Run generic canonicalization before TileAS layout assignment.
2. Keep dot folding centralized so integer signedness and floating accumulation
   rules cannot diverge from verification.
3. Normalize pointer offsets before fusing `addptr`.
4. Treat constant-false memory masks as token rewrites, not as plain erasure.
5. Preserve relative order when pruning queue or pragma results.
6. Lower tokens only after async barrier resources are known.
7. Reject plugin or queue lowering when resource requirements cannot be
   represented by the selected target model.
