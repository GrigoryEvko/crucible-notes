# cutlass Seq-Bar and Block-Striped Operations

## Abstract

`cutlass.seq_bar.*` models CUTLASS ordered sequence barriers — a ring of mbarrier slots plus a phase/index state cursor. `cutlass.block_striped.*` models per-thread striped memory movement and partial reduction across a CTA. Together they form the synchronisation and cooperative-movement substrate for warp-specialised GEMM mainloops and StreamK/split-K epilogues. The seq-bar family contributes six of the thirty-eight ops the dialect ctor registers at `sub_1761D90`; the block-striped family contributes eight.

## Sequential Barrier Model

A sequence barrier is a circular array of barrier slots. Each participant carries a state cursor:

```c
typedef struct {
    int phase;
    int index;
    int count;
} SeqBarState;
```

The slot is `index % depth`. Arrival advances the cursor; waiting uses the current phase.

```c
BarrierSlot seq_bar_slot(SeqBar bar, SeqBarState state) {
    int slot_index = state.index % bar.depth;
    return bar.base[slot_index];
}

SeqBarState seq_bar_advance(SeqBar bar, SeqBarState state) {
    state.index += 1;
    state.count += 1;

    if ((state.index % bar.depth) == 0) {
        state.phase ^= 1;
    }

    return state;
}
```

## Seq-Bar Operations

The six seq-bar ops are `cutlass.seq_bar.init`, `cutlass.seq_bar.arrive`, `cutlass.seq_bar.wait`, `cutlass.seq_bar.arrive_relaxed`, `cutlass.seq_bar.try_wait`, and `cutlass.seq_bar.finish`. The init op claims a slot range from the per-CTA NamedBarrier pool via the same barrier-id allocator at `sub_1771850` that `PipelineInitOp::verify` uses. The thirty-two-slot pool serves both `cutlass.pipeline` and `cutlass.seq_bar`, so pipeline-init and seq-bar-init compete for the same physical resource and must agree on allocation order.

| Operation | Contract |
|---|---|
| `cutlass.seq_bar.init` | Initialize every barrier slot in the ring; allocates slot IDs from the per-CTA NamedBarrier pool. |
| `cutlass.seq_bar.arrive` | Arrive on the current slot and advance state. |
| `cutlass.seq_bar.wait` | Wait for the current slot and phase. |
| `cutlass.seq_bar.arrive_relaxed` | Arrive without ordering against prior memory accesses. |
| `cutlass.seq_bar.try_wait` | Probe the current phase without blocking. |
| `cutlass.seq_bar.finish` | Drain the ring and release the slot range back to the pool. |

```c
void lower_seq_bar_wait(SeqBar bar, SeqBarState state) {
    BarrierSlot slot = seq_bar_slot(bar, state);
    emit_mbarrier_try_wait_parity(slot, state.phase);
}

SeqBarState lower_seq_bar_arrive(SeqBar bar, SeqBarState state) {
    BarrierSlot slot = seq_bar_slot(bar, state);
    emit_mbarrier_arrive(slot, bar.participant_count);
    return seq_bar_advance(bar, state);
}
```

The verifier checks that `wait` and `arrive` use a state whose depth matches the sequence-barrier type. Mismatched depths usually mean producer and consumer cursors were constructed for different rings.

## Relationship to `cutlass.pipeline`

`seq_bar` is the simple ordered-ring path. `cutlass.pipeline` carries richer producer/consumer participant masks and transaction-byte accounting; its init op is verified by the 3 406-byte `PipelineInitOp::verify` at `sub_1771F40`, which checks `numStages > 0`, that the participants list length (read via `sub_172E930`) matches `numProducers`, that the consumer list length (read via `sub_172E940`) matches `numConsumers`, that `barrier_id_base` falls within `[0, 32)`, and that `producer_group_id` and `consumer_group_id` are distinct. Seq-bar init skips all of these checks because its participant model is a single flat ring. Reach for `seq_bar` when ordered arrival and wait are enough; reach for `pipeline` when TMA, WGMMA, executor masks, or participant roles must be explicit.

Once `PipelineInitOp::verify` passes, the post-verify builder at `sub_1772C90` stamps a derived arrive-count attribute on the pipeline-init op. Seq-bar init has no equivalent post-verify step — its arrive-count is the static participant count from the op attribute and needs no recomputation.

## Block-Striped Model

Block-striped movement partitions a contiguous tile across `T` threads. Thread `i` touches indices:

```text
i, T + i, 2T + i, ...
```

The result: coalesced per-thread stripes with a dead-simple mapping from thread id to element index.

```c
SmallVector<int> block_striped_indices(int thread_id, int threads, int elements) {
    SmallVector<int> result;

    for (int index = thread_id; index < elements; index += threads) {
        result.push(index);
    }

    return result;
}
```

## Block-Striped Operations

The eight block-striped ops cover three core movement variants plus five type-specialised forms. The three core variants are `cutlass.block_striped.load`, `cutlass.block_striped.store`, and `cutlass.block_striped.reduce`. The five specialised forms — half, bfloat, packed, integer, float — exist because each needs distinct atom selection at lowering time.

| Operation | Contract |
|---|---|
| `cutlass.block_striped.load` | Load a per-thread stripe from global memory into register memory. |
| `cutlass.block_striped.store` | Store a per-thread stripe from register memory to global memory. |
| `cutlass.block_striped.reduce` | Atomically add or max each register stripe into a global workspace. |

Every variant requires one register-memory memref, one global-memory pointer or memref, an element width of at least sixteen bits, and a static shape. Static shape lets the lowering pick a vector width and atom shape.

## Four Operand-Layout Checkers

The block-striped family uses four operand-layout checkers — one per variant: `sub_176E670` for load, `sub_176EE10` for store, `sub_176F5B0` for reduce-add, `sub_176FD50` for reduce-max. Each one checks register-memory operand width, global-memory pointer or memref shape, element-type width (at least sixteen bits), and static stripe shape. They reject malformed operand combinations before lowering picks a vector width and copy atom.

```c
LogicalResult verify_block_striped(BlockStripedOp op) {
    require(has_rmem_operand(op));
    require(has_gmem_operand(op));
    require(bit_width(op.element_type) >= 16);
    require(shape_is_static(op.stripe_shape));
    require(op.n_threads_in_block > 0);
    return success();
}
```

## Reduce Lowering

`block_striped.reduce` is the side-effecting atomic path. It emits a global atomic add or max appropriate for the element type and packed width.

```c
void lower_block_striped_reduce(BlockStripedReduceOp op) {
    for (int lane_index : block_striped_indices(thread_id(), op.threads, op.elements)) {
        Pointer dst = op.workspace + lane_index * sizeof(op.element_type);
        Value value = load_register_fragment(op.rmem, lane_index);
        emit_global_atomic_add(dst, value, GPU_SCOPE);
    }
}
```

Half and bfloat packed reductions use the target's packed no-flush path when available. Integer and floating scalar reductions select the matching global-add op. The five type-specialised forms exist so the lowering can route straight to the correct atom without rederiving the element type from a generic op.

## Cutlass-Bar Warp-Cooperative Diagnostic

`BarOpLowering` at `sub_15FC250` is a ~5.5 KB routine handling `cutlass.bar` lowering plus the warp-cooperative diagnostic. The diagnostic fires when a `cutlass.bar` op carries an arrive-count that is not a multiple of warp size, or when the op sits outside warp-cooperative scope. Block-striped and seq-bar lowering both depend on the warp-cooperative scope guarantee the diagnostic enforces — partial-warp arrivals on a `cutlass.bar` would otherwise corrupt the per-stage arrive count the pipeline-init post-verify builder stamped.

## Load/Add/Store Lowering

The non-atomic variants lower through `cute` copy atoms and vector load/store helpers.

```c
void lower_block_striped_load_add(BlockStripedLoadAddOp op) {
    CopyAtom atom = make_block_striped_copy_atom(op.element_type, op.stripe_shape);

    RegisterFragment old_value = cute_load_vec(op.gmem, atom);
    RegisterFragment partial = cute_load_vec(op.rmem, atom);
    RegisterFragment merged = add_fragments(old_value, partial, op.element_type);

    cute_store_vec(op.rmem, merged, atom);
}
```

Use floating addition for floating element types and integer addition for integer element types. The verifier nails that choice down by requiring a concrete element type.

## StreamK and Split-K Use

StreamK and split-K reach for block-striped ops in their epilogues. Partial CTAs write accumulator fragments into a workspace. The final reducer CTA loads the partials, accumulates them, and stores or atomically reduces the result. The block-striped mapping pins every thread to a deterministic stripe of the accumulator tile.

## Invariants

- Sequence-barrier state depth matches the sequence-barrier value.
- Sequence-barrier arrive advances index and flips phase on wraparound.
- Block-striped operands include one register-memory and one global-memory object.
- Block-striped element widths are at least sixteen bits.
- Block-striped shapes are static.
- Reduce is side-effecting and must not be commoned or removed.
- Load/add/store variants choose integer or floating addition from element type.
- `cutlass.bar` arrive-count is a multiple of warp size and the op is in warp-cooperative scope.

## Reimplementation Checklist

1. Implement sequence-barrier state as phase, index, and count.
2. Lower wait and arrive through mbarrier parity wait and arrive operations.
3. Verify state/barrier depth compatibility.
4. Allocate seq-bar slot IDs from the same thirty-two-slot per-CTA NamedBarrier pool that pipeline-init uses, through the shared allocator at `sub_1771850`.
5. Implement block-striped index mapping once and reuse it for all variants.
6. Implement four operand-layout checkers, one per block-striped variant.
7. Keep reduce side-effecting and GPU-scope.
8. Route load/store/load_add through `cute` copy/vector helpers where possible.
9. Emit the warp-cooperative diagnostic from `cutlass.bar` lowering on arrive-count or scope mismatch.
