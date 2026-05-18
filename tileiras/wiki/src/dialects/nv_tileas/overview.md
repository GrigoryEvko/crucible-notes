# nv_tileas Dialect Overview

`nv_tileas` is the operational async-scheduling dialect in the TileIR lowering stack, sitting below `nv_tileaa` — where queues and agents still describe intent — and above the LLVM/NVVM dialects, where the same work is spelled out as barriers, bulk tensor-memory transfers, register-budget changes, and target intrinsics.

The dialect's job is to make a warp-specialized kernel explicit enough to schedule. Queue edges become producer and consumer regions. Stage movement becomes an SSA iterator. Asynchronous copies become token-producing memory operations. Layout choices become concrete tiled loads, stores, descriptor construction, and conversions. Once a kernel is in `nv_tileas`, later passes can ask precise questions: which agent owns this region, which pipeline stage this operation occupies, which async value orders this consumer, and which memory operation must not be reordered past another.

Users of the compiler pipeline treat `nv_tileas` as an internal form — write `cuda_tile` or `nv_tileaa` and let the pipeline materialize it. Reimplementers treat it as the main contract between high-level tile semantics and target-specific code generation.

## Position in the Cascade

```text
cuda_tile
    |
    | lift public tile operations into agent-aware TileIR
    v
nv_tileaa
    |
    | materialize queues, agents, async regions, and pipeline iterators
    v
nv_tileas
    |
    | schedule stages, assign layouts, lower async memory and barriers
    v
llvm + nvvm
```

`nv_tileaa` is declarative: it says a producer puts a value into a queue and a consumer later gets it. `nv_tileas` is operational: it says how that edge is represented — producer acquire/write/commit, consumer wait/read/release, stage iteration, region yields. That distinction is why the scheduler and final lowering passes operate on `nv_tileas`.

## Programming Model

The central abstraction is a bounded asynchronous pipeline shared by one or more producer agents and one or more consumer agents. Each pipeline has a fixed stage count, stage-local storage, producer and consumer token types, a rotating iterator identifying the active stage, and optional async result tokens for operations whose completion is observed separately from the producer/consumer handshake.

The normal pipeline lifecycle:

```c
Pipeline pipe = create_pipeline(stages, storage, producer_group, consumer_group);
PipelineIter iter = create_iterator(pipe);

for (int logical_stage = 0; logical_stage < work_items; ++logical_stage) {
    if (current_agent_is_producer()) {
        ProducerToken ready = producer_acquire(pipe, iter);

        ProducerToken written = producer_write(ready, iter) {
            // Fill the stage-local buffer: TMA load, tiled load, layout conversion,
            // MMA preparation, or another producer-side action.
            yield(next_producer_values);
        };

        producer_commit(written);
    }

    if (current_agent_is_consumer()) {
        ConsumerToken ready = consumer_wait(pipe, iter, consumer_idx);

        ConsumerToken consumed = consumer_read(ready, iter) {
            // Read values produced for this stage and run the consumer work:
            // MMA, reductions, stores, or downstream async launches.
            yield(next_consumer_values);
        };

        consumer_release(consumed);
    }

    iter = inc_iter(iter);
}
```

The real IR uses MLIR regions rather than C blocks. The pseudocode is the semantic contract — acquire grants ownership of a producer stage, write fills it, commit publishes it, wait observes a committed stage, read consumes it, release returns the stage to the pipeline.

## Operation Families

Three related operation families divide the dialect.

| Family | Representative operations | Purpose |
| --- | --- | --- |
| Async pipeline | `async.pipeline.create_pipeline`, `create_iterator`, `inc_iter`, `produce_one`, `consume_one`, `producer_acquire`, `producer_write`, `producer_commit`, `consumer_wait`, `consumer_read`, `consumer_release`, `agent_switch`, `yield` | Represents warp-specialized producer/consumer execution as explicit regions and tokens. |
| Memory and movement | `tiled_load`, `tiled_store`, `tiled_atomic_rmw`, `async.tiled_load`, `async.tiled_tma_load`, `async.tiled_tma_store`, `async.gather_tma_load`, `async.scatter_tma_store`, `make_tiled_tma_desc`, `copy`, `dot`, `load`, `store` | Represents layout-aware data movement, TMA descriptors, asynchronous copies, and tiled compute edges. |
| Tile structure | `alloc_tensor`, `convert_layout`, `view`, `reinterpret`, `extract_slice`, `insert_slice`, `expand_dims`, `shuffle`, `reduce`, `scan`, `generate`, `pragma` | Represents local tile storage, shape/view manipulation, layout conversion, reductions, scans, generation, and optimizer directives. |

The `atom` attribute connects tiled memory operations to the later `cute_nvgpu` atom selection. `padding_value` controls out-of-bounds behavior for gather-like operations. `consumer_idx` distinguishes consumers inside a consumer group. `ocgEnterDirectives` and `ocgLeaveDirectives` carry optimizer directive payloads through structured regions.

## Queue-to-Pipeline Lowering

The primary producer of `nv_tileas` is the lowering from `nv_tileaa`. It turns an abstract queue program into a pipeline program the scheduler can reason about.

```c
void lower_tileaa_to_tileas(Module module) {
    for (ExecuteOp exec : module.execute_ops()) {
        replace exec with agent_switch {
            clone_each_agent_body(exec);
            preserve_agent_group_ids_and_register_budgets(exec);
        };
    }

    for (QueueOp queue : module.queues()) {
        Pipeline pipe = create_pipeline(
            queue.stage_count,
            queue.stage_storage,
            queue.producer_group,
            queue.consumer_group);

        replace queue.create with pipe;

        for (QueuePutOp put : queue.puts()) {
            replace put with produce_one(pipe.producer_token, pipe.iterator) {
                ProducerToken t0 = producer_acquire(pipe, iterator);
                ProducerToken t1 = producer_write(t0, iterator) {
                    clone_producer_body(put);
                    yield(produced_values);
                };
                producer_commit(t1);
                yield(updated_pipeline_values);
            };
        }

        for (QueueGetOp get : queue.gets()) {
            replace get with consume_one(pipe.consumer_token, pipe.iterator) {
                ConsumerToken t0 = consumer_wait(pipe, iterator, get.consumer_idx);
                ConsumerToken t1 = consumer_read(t0, iterator) {
                    clone_consumer_body(get);
                    yield(consumed_values);
                };
                consumer_release(t1);
                yield(updated_pipeline_values);
            };
        }
    }

    propagate_pipeline_iterator_types_through_scf(module);
    erase_dead_queue_scaffolding(module);
}
```

The lowering is deliberately one-way. After this point the compiler doesn't need the original queue identity — only the pipeline stages, tokens, agent regions, and iterator values, since those are the handles scheduling, layout assignment, and final lowering work with.

## Iterator Propagation

`PipelineIteratorType` is the type-level wrapper that lets an iterator travel through structured control flow without losing the element type it indexes. The propagation pass rewrites loop and branch signatures until every path agrees on the same iterator type.

```c
Type propagate_iterator_type(Value value, Type expected) {
    if (!is_pipeline_iterator(value.type)) {
        value.type = PipelineIteratorType(expected);
    }

    for (Use use : value.uses) {
        Operation owner = use.owner;

        if (owner is scf.for) {
            rewrite_loop_iter_arg(owner, use.index, value.type);
            rewrite_loop_yield(owner, use.index, value.type);
        } else if (owner is scf.if) {
            Type then_type = propagate_branch_yield(owner.then_region, use.index, value.type);
            Type else_type = propagate_branch_yield(owner.else_region, use.index, value.type);
            assert(then_type == else_type);
            owner.result(use.index).type = then_type;
        } else {
            rewrite_operand(owner, use.index, value.type);
        }
    }

    return value.type;
}
```

Branch agreement is the key invariant: when a pipeline iterator yields from both sides of an `scf.if`, the two yielded values must have the same iterator type. Without that rule, later schedule and lowering passes cannot assign a single stage meaning to the merged SSA value.

## Memory and Layout Contract

Tiled memory operations carry two independent contracts. The layout contract demands that value shape, chosen atom, and any `convert_layout` operations agree on how the tile is partitioned across registers, shared memory, tensor memory, or global memory. The ordering contract demands that async memory operations and descriptor construction carry memory-consistency information so canonicalization cannot reorder them across a visible synchronization edge.

A correct lowering treats TMA descriptor construction as a separate operation, not a side effect of a TMA load or store. Later passes then have a real SSA value for the descriptor, verifier logic can check descriptor alignment and capture restrictions, and the scheduler can account for descriptor-producing work independently from the transfer that consumes it.

```c
TmaDesc make_tiled_tma_desc(TensorView global, TileShape box, Atom atom) {
    require(atom.kind == TMA_LOAD || atom.kind == TMA_STORE);
    require(box.rank == atom.box_rank);
    require(global.element_stride == 1);
    require(descriptor_pointer_is_aligned());
    return encode_descriptor(global.base, global.shape, global.strides, box, atom);
}

AsyncToken async_tiled_tma_load(TmaDesc desc, SmemTile dst, PipelineStage stage) {
    require(dst.layout.is_tma_compatible());
    require(stage.is_owned_by_current_producer());
    return launch_bulk_tensor_copy(desc, dst, stage.barrier);
}
```

## Verifier Invariants

A correct `nv_tileas` implementation enforces these invariants before the scheduler runs:

- `produce_one` and `consume_one` regions end with `nv_tileas.async.pipeline.yield`.
- Producer region argument types match the element types carried by the producer token.
- Consumer region argument types match the element types carried by the consumer token.
- Region result types match the operation result types.
- `consumer_idx` identifies a valid consumer in the consumer group.
- Pipeline iterator values yielded from both arms of a branch have the same type.
- TMA operations use the correct atom kind for load or store.
- TMA box dimensions and atom box dimensions agree.
- TMA element stride is one.
- Shared-memory layouts consumed by TMA operations are TMA-compatible.
- Special padding values are only used for floating-point element types.
- Memory operations that carry acquire, release, or stronger semantics are not reordered across their synchronization boundary.

None of these checks is cosmetic. The scheduler assumes region types, iterator types, consumer indices, and memory ordering are already valid. Invalid `nv_tileas` that reaches scheduling can produce a `(stage, order)` assignment that looks well-formed while representing an impossible pipeline.

## AbstractOperation Record

Every registered op in `nv_tileas` carries a single 0x70-byte `AbstractOperation` record — same layout as
`nv_tileaa`, eight bytes wider than the `cuda_tile` record. The dialect ctor allocates each slab with
`sub_44A8C20(0x70)` and uses the extra slot at `+0x68` for the alias-token concept pointer the dialect
inherits from its alias-aware sibling. Otherwise the shape matches the descriptor an `Operation*` resolves
through its `OperationName` slot to reach the dialect's interface tables and fold callback.

```c
typedef struct AbstractOperation {
    /*+0x00*/ void           **vtable;                       // dispatch for the op
    /*+0x08*/ StringRef        mnemonic;                     // e.g. "nv_tileas.async.tiled_tma_load"
    /*+0x18*/ ConceptModel    *interface_inliner;
    /*+0x20*/ ConceptModel    *interface_opasm;
    /*+0x28*/ ConceptModel    *interface_fold;
    /*+0x30*/ ConceptModel    *interface_typeinfer;
    /*+0x38*/ ConceptModel    *interface_bytecode;
    /*+0x40*/ ConceptModel    *interface_memeffects;
    /*+0x48*/ ConceptModel    *interface_destinationstyle;
    /*+0x50*/ ConceptModel    *interface_alias;              // alias-aware concept inherited from nv_tileaa
    /*+0x58*/ ConceptModel    *interface_extra1;
    /*+0x60*/ FoldCallback     fold_canon;                   // op-fold and canonicalize hook
    /*+0x68*/ ConceptModel    *interface_alias_token;        // extra slot for the alias-token concept
} AbstractOperation;
```

The slab is zero-initialized by the allocator, so unused interface slots stay null and the dispatcher can
probe them without a presence flag. The `mnemonic` field is an embedded `StringRef` whose pointer references
a `.rodata` literal owned by the binary, not a heap-interned copy. The interface-concept pointers at
`+0x18..+0x58` are the MLIR concept-model singletons that implement inlining, asm printing, folding, type
inference, bytecode, memory effects, destination-style, and the alias-aware concept at `+0x50`. The fold
callback at `+0x60` is the op's per-op rewriter, and the extra concept pointer at `+0x68` is the alias-token
model. The per-op class vtables are dense in the `unk_59DC...` neighbourhood: for example,
`nv_tileas.alloc_tensor` is registered with vtable `&unk_59DC860`, `nv_tileas.async.pipeline.create_pipeline`
with `&unk_59DC9F0`, and `nv_tileas.async.tiled_tma_load` with the corresponding `&unk_59DD...` slot, each
populated by its inline reg stanza in `sub_147CAC0`.

The records sit consecutively in a statically-allocated array in `.data.rel.ro` that mirrors the layout of
the other tile dialects: one 0x70 slab per op, walked from the dialect base by mnemonic hash. The end-of-
registered-ops boundary is marked by the same null sentinel as `cuda_tile` and `nv_tileaa`, `0x5BE6138`;
lookup helpers stop walking the bank when they hit it.

This is the static-sentinel idiom described in
[TypeID Sentinels and Anchors](../../mlir-infra/typeid-sentinels-and-anchors.md#idiom-1-static-pointer-identity-sentinel): the bank is
allocated once, lives for the entire process, and is indexed by mnemonic hash from the dialect base. Live
`Operation*` instances reach this record through their `OperationName` slot, which is the resolution path
documented in [Operation Layout — Pointer-Identity Dispatch](../../mlir-infra/operation-layout.md#pointer-identity-dispatch). The per-op `vtable`
and fold-callback pairs for the rest of the roster are catalogued in
[Operation Roster and Builders](op-roster-and-builders.md#operation-families).

## Cross-links

- [Op Roster and Builders](op-roster-and-builders.md#operation-families) — complete operation list and low-level builder notes.
- [Types](types.md#pipeline-types) — pipeline tokens, iterator types, agent-like interfaces, and yield interface behavior.
- [Verifiers](verifiers.md#async-pipeline-verification) — detailed verifier behavior for pipeline regions, TMA operations, tiled memory operations, and layout constraints.
- [Folds and Memory Consistency](folds-and-mem-consistency.md#canonicalization-patterns) — canonical rewrites, memory consistency interface behavior, and ordering-sensitive rewrite rules.

## Appendix: Pass-Object PIMPL Layout

In the binary, most TileAS passes appear as a thin `mlir::Pass` shim wrapping a single heap-allocated PIMPL
object. The shim's run method dispatches through the vtable at offset `+0x00` of the PIMPL; the secondary
interface pointer at `+0x08` is the analysis-and-statistics object the pass framework calls into when the
run method asks for an option store, a dominator analysis, or a symbol-table cache. Every pass shares this
skeleton — find one pass, find all of them by following the same offsets.

The objects range from 0x150 to 0x3C0 bytes. Beyond the two leading pointers, one field matters for
verification: the pass-failure bit at `*(pass+40) & 4`, which the framework reads after the run method
returns to decide whether to mark the pipeline failed. Options the pass forwards from the command line sit
at predictable offsets — typically `+0x1D0`, `+0x2A0`, `+464`, `+672`, or `+880`, depending on how many
string and integer options the pass carries.

Three passes from the TileAS pipeline are visible in the binary with clean vtable cross-references; their sizes and the
location of their option slabs are:

| Pass | Size | Vtable | Notes |
|---|---:|---|---|
| D08 `MaterializeConvertLayout` | 752 B (`0x2F0`) | `off_59B4688` | String options at `+0x1D0` and `+0x2A0`; failure bit at `+40` |
| D09 `MaterializeSchedule` | 960 B (`0x3C0`) | `&unk_59B4768` | Three option slabs at `+464`, `+672`, `+880`; failure bit at `+40` |
| D11 `UnspecializedPipeline` | — | — | `numStages` option lives at `+464`; the rest of the object is shared with D09 |

The pattern is uniform enough to reimplement as a single C++ base class with offsets parameterised by a
template argument. The vtable's first slot is the standard `runOnOperation` entry; the second is
`getPassName`; later slots carry the `mlir::Pass` clone, dependent-dialect, and options-printing hooks. The
secondary interface at `+0x08` is the analysis-manager facet — touching it after `runOnOperation` returns
is undefined behavior, because the framework tears it down before invoking the next pass.
