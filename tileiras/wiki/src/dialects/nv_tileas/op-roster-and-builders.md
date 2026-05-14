# nv_tileas Operation Roster and Builders

## Abstract

`nv_tileas` is the operational surface for async scheduling, tiled memory movement, layout conversion, TMA descriptor use, and scheduled tile compute. This page lists the operation families, explains which attributes belong to the public contract, and describes the builder helpers used by scheduling and materialization passes.

The useful reference is semantic. The binary holds plenty of generated registration thunks, but a reimplementation only needs the operation names, operand/result contracts, attributes, and builder behavior described here.

## Operation Families

| Family | Operations | Purpose |
|---|---|---|
| async pipeline | `async.pipeline.create_pipeline`, `create_iterator`, `inc_iter`, `produce_one`, `produce_one_async`, `consume_one`, `consume_one_async`, `producer_acquire`, `producer_write`, `producer_commit`, `consumer_wait`, `consumer_read`, `consumer_release`, `agent_switch`, `async.pipeline.yield` | producer/consumer pipeline regions, stage iteration, ownership handshakes, and agent partitioning |
| async tokens | `async.wait`, `async.future_wait`, `async.to_async`, `async.token_to_async`, `create_none` | async completion, token bridging, and placeholder values |
| tiled memops | `tiled_load`, `tiled_store`, `tiled_atomic_rmw`, `async.tiled_load`, `async.copy`, `async.load`, `async.store`, `copy`, `load`, `store`, `gather_load`, `scatter_store` | token-ordered and async memory movement |
| tensor slices | `alloc_tensor`, `extract_slice`, `insert_slice`, `async.extract_slice`, `async.insert_slice` | local tile storage and shape manipulation |
| layout | `convert_layout`, `view`, `expand_dims`, `reinterpret`, `shuffle`, `generate` | layout conversion, value views, and generated tile bodies |
| TMA | `make_tiled_tma_desc`, `async.tiled_tma_load`, `async.tiled_tma_store`, `async.gather_tma_load`, `async.scatter_tma_store` | TMA descriptor construction and async tensor bulk copies |
| compute | `dot`, `async.dot`, `reduce`, `scan` | MMA and region-bearing reduction operations |
| control and metadata | `yield`, `pragma`, `cancel_next_program_id`, `async.cancel_next_program_id` | region termination, optimizer directives, and scheduling control |

## Attribute Roster

| Attribute | Owner concepts | Meaning |
|---|---|---|
| `atom` | copy, dot, tiled memory, TMA, gather/scatter | selects copy, MMA, TMA, or reduce atom |
| `padding_value` | gather/load/store variants | value used when an access is out of bounds |
| `consumer_idx` | consumer wait/read paths | selects a consumer inside a consumer group |
| `ocgEnterDirectives` | `pragma` | optimizer-control directives active on entry |
| `ocgLeaveDirectives` | `pragma` | optimizer-control directives active on exit |
| `operandSegmentSizes` | segmented memops and descriptor ops | separates view, coordinate, offset, token, and metadata operands |
| memory semantic/scope attrs | tiled memory operations | ordering and visibility contract |
| in-bounds attrs | loads and stores | per-dimension bounds information |

Attributes belong to the operation contract. Pattern rewrites may remove stale caches, but they must preserve semantic attributes unless they replace the operation with a semantically equivalent form.

## PipelineOp Enum

The `nv_tileas.async.pipeline.*` op family is a closed 16-entry enumeration. Each entry pairs with a single builder helper and a fixed `OperationState` shape, so a reimplementation can drive the entire family from one indexed dispatch instead of per-op registration code. Entries 0..14 are active; entry 15 is reserved.

| # | Mnemonic | Builder | OperationState |
|---|---|---|---|
| 0 | `nv_tileas.async.pipeline.create_pipeline` | `sub_909290` (3.4 KB) | 6 named operands: `numStages` (i32), `bufferView`, `producerGroupId` (u8), `consumerGroupId` (u8), `sharedMem` (bool), `dynamic` (bool) |
| 1 | `nv_tileas.async.pipeline.produce_one` | `sub_904100` | 1 region op |
| 2 | `nv_tileas.async.pipeline.produce_one_async` | `sub_904260` | 1 region op |
| 3 | `nv_tileas.async.pipeline.consume_one` | `sub_9043B0` | 1 region op + `consumer_idx` i32 attr (via `sub_440A060` + `sub_446DC70`) |
| 4 | `nv_tileas.async.pipeline.consume_one_async` | `sub_9044F0` | 1 region op |
| 5 | `nv_tileas.async.pipeline.consumer_read` | `sub_9073A0` | scalar op + `consumer_idx` i32 attr |
| 6 | `nv_tileas.async.pipeline.producer_write` | `sub_907710` | scalar op |
| 7 | `nv_tileas.async.pipeline.producer_acquire` | `sub_904630` | scalar op |
| 8 | `nv_tileas.async.pipeline.producer_commit` | `sub_904770` | scalar op |
| 9 | `nv_tileas.async.pipeline.consumer_wait` | `sub_9043F0` | scalar op |
| 10 | `nv_tileas.async.pipeline.consumer_release` | `sub_9044B0` | scalar op |
| 11 | `nv_tileas.async.pipeline.yield` | `sub_904910` | variadic terminator |
| 12 | `nv_tileas.async.pipeline.inc_iter` | `sub_904A10` | scalar op |
| 13 | `nv_tileas.async.pipeline.create_iterator` | `sub_904770` | scalar op |
| 14 | `nv_tileas.async.pipeline.agent_switch` | `sub_9130B0` (4.0 KB) | variadic body builder: `num_agents_per_group` i32, `max_regs` per-agent list, `isolated` bool |
| 15 | (reserved / future) | — | — |

Two builders deserve individual notes. `sub_909290` materializes `create_pipeline` — the largest builder at 3.4 KB, because each of its six named operands runs through `sub_149BFF0` (the named-operand helper) before `OperationState::addOperands` populates. The names ride along with the operation, so they reappear in IR-printed form rather than as positional `%0..%5` references. `sub_9130B0` materializes `agent_switch` at 4.0 KB and is variadic in agent-body count: the emitted `OperationState` carries an arbitrary number of regions, one per agent, plus the `num_agents_per_group` count, a `DenseI32ArrayAttr` of per-agent `max_regs` budgets, and an `isolated` boolean that controls whether an agent's region sees the surrounding SSA scope.

Five of the sixteen entries have stable global slot pointers for their `OperationName` lookup; the remaining eleven resolve by mnemonic string each time the builder runs. The five fast-path slots:

| Op | Slot pointer |
|---|---|
| producer_acquire | `&unk_5B44F30` |
| consumer_wait | `&unk_5B44F58` |
| consumer_read | `&unk_5B44F68` |
| agent_switch | `&unk_5B44F80` |
| create_pipeline | `&unk_5B45060` |

A reimplementation needn't mirror this split. Caching the `OperationName` for every entry costs five pointers per dialect instance and erases the per-call string lookup uniformly. The slot table is reported here because it is the only place where the relative hotness of these ops is visible in the binary — the cached ops are what the producer/consumer handshake emits inside steady-state pipeline regions, while the uncached ops fire once per pipeline construction.

The region-op verifiers attached to entries 1, 2, 3, 4, and 11 (the four produce/consume variants and the yield) are documented in [verifiers.md](verifiers.md). The `OperationState` trailing-objects layout that each builder fills in is documented in [mlir-infra/operation-layout.md](../../mlir-infra/operation-layout.md).

## TMA Op Operand/Result Tables

### `nv_tileas.make_tiled_tma_desc`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | global view | `tiled_view` (global memory) | yes | element stride must equal 1 |
| operand 1..R | box dims | `index` | yes (R = atom box rank) | per-axis box size |
| result 0 | descriptor | `nv_tileas.tma_desc` | yes | consumed by `async.tiled_tma_load`/`_store` |
| attr `atom` | atom | TMA load or store atom | yes | drives kind selection |
| attr `swizzle_mode` | enum | `none\|32B\|64B\|128B` | optional | shared-memory swizzle |
| attr `oob_mode` | enum | `zero\|nan\|constant` | optional | out-of-bounds behavior |

### `nv_tileas.async.tiled_tma_load`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | descriptor | `tma_desc` | yes | from `make_tiled_tma_desc` |
| operand 1 | shared destination | `tiled_view` (shared) | yes | TMA-compatible swizzled layout |
| operand 2..R+1 | coords | `index` | yes | per-axis source coordinate |
| operand R+2 | barrier | `mem_token` | yes | mbarrier for completion |
| result 0 | async token | `AsyncTokenType` | yes | observed by `async.wait` |
| attr `atom` | atom | TMA load atom | yes | matches descriptor atom kind |
| attr `padding_value` | typed attr | element-typed scalar | optional | floating-point only |
| attr `operandSegmentSizes` | dense i32 | length 4 | yes | `{desc, dst, coords, barrier}` |

### `nv_tileas.async.tiled_tma_store`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | descriptor | `tma_desc` | yes | TMA store kind |
| operand 1 | shared source | `tiled_view` (shared) | yes | TMA-compatible swizzled layout |
| operand 2..R+1 | coords | `index` | yes | per-axis destination coordinate |
| result 0 | async token | `AsyncTokenType` | yes | |
| attr `atom` | atom | TMA store atom | yes | |
| attr `operandSegmentSizes` | dense i32 | length 3 | yes | `{desc, src, coords}` |

### `nv_tileas.async.gather_tma_load` / `scatter_tma_store`

The discontiguous TMA variants take a per-lane coordinate tile (gather) or
per-lane address tile (scatter) on top of the contiguous operands, and reject
modes the descriptor doesn't support. Their attribute sets mirror the
contiguous variants — `gather_tma_load` accepts `padding_value`,
`scatter_tma_store` rejects it.

```c
LogicalResult verify_make_tiled_tma_desc(MakeTmaDescOp op) {
    require(op.atom().is_tma());
    require(op.box_dims().size() == op.atom().box_rank());
    require(op.global_view().element_stride() == 1);
    require_descriptor_alignment(op.global_view().base());
    require_captures_are_descriptor_abi_compatible(op);
    return success();
}
```

## Pipeline Op Operand/Result Tables

### `nv_tileas.async.pipeline.create_pipeline`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | buffer view | `tiled_view` | yes | stage-local storage view |
| result 0 | producer token | `PipelineProducerTokenType` | yes | feeds `producer_acquire` |
| result 1 | consumer token | `PipelineConsumerTokenType` | yes | feeds `consumer_wait` |
| attr `numStages` | i32 | | yes | stage count |
| attr `producerGroupId` | u8 | | yes | agent group emitting producers |
| attr `consumerGroupId` | u8 | | yes | agent group emitting consumers |
| attr `sharedMem` | bool | | optional | stage storage lives in shared memory |
| attr `dynamic` | bool | | optional | dynamic stage indexing |

### `nv_tileas.async.pipeline.produce_one` / `produce_one_async`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | producer token | `PipelineProducerTokenType` | yes | input ownership |
| operand 1 | iterator | `PipelineIteratorType` | yes | stage indexing |
| region 0 | body | producer | yes | terminated by `async.pipeline.yield` |
| result 0 | producer token | `PipelineProducerTokenType` | yes | returned to caller |
| result 1 | async token | `AsyncTokenType` | async variant only | completion of async producer work |
| attr `producer_types` | typed array | | yes | element-type list yielded by body |

### `nv_tileas.async.pipeline.consume_one` / `consume_one_async`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | consumer token | `PipelineConsumerTokenType` | yes | input ownership |
| operand 1 | iterator | `PipelineIteratorType` | yes | |
| region 0 | body | consumer | yes | terminated by `async.pipeline.yield` |
| result 0 | consumer token | `PipelineConsumerTokenType` | yes | |
| result 1 | async token | `AsyncTokenType` | async variant only | |
| attr `consumer_idx` | i32 | | yes | selects a consumer in consumer group |
| attr `consumer_types` | typed array | | yes | element-type list yielded by body |

### `nv_tileas.async.pipeline.producer_acquire` / `producer_commit` / `consumer_wait` / `consumer_release`

| Op | Operand 0 | Result 0 | Notes |
|---|---|---|---|
| `producer_acquire` | producer token + iterator | producer token | grants stage ownership |
| `producer_commit` | producer token | producer token | publishes stage |
| `consumer_wait` | consumer token + iterator | consumer token | observes commit |
| `consumer_release` | consumer token | consumer token | returns stage to pool |

`consumer_wait` and `consumer_read` additionally carry the `consumer_idx` i32 attribute that maps the wait to a specific consumer inside the consumer group.

### `nv_tileas.async.pipeline.yield`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0.. | yielded values | variadic | yes | operand types match enclosing region's result types |

### `nv_tileas.async.pipeline.create_iterator` / `inc_iter`

| Op | Operand 0 | Result 0 | Notes |
|---|---|---|---|
| `create_iterator` | pipeline value | `PipelineIteratorType` | rotates through `numStages` stages |
| `inc_iter` | iterator | iterator | advances to next stage |

```c
LogicalResult verify_pipeline_handshake(Operation op) {
    require_token_kind(op, op.operand(0));
    require_iterator_type_payload_matches(op.region(0), op.producer_types_attr());
    require_region_terminator_is(op.region(0), "nv_tileas.async.pipeline.yield");
    require_yield_operand_types_match_results(op.region(0), op.result_types());
    return success();
}
```

## Pipeline Builders

Pipeline builders create region operations and token handshakes. A good implementation exposes small helper functions instead of forcing every pass to build raw operation states.

```c
ProduceOneOp build_produce_one(Rewriter *rw,
                               Location loc,
                               ProducerToken token,
                               PipelineIterator iter,
                               TypeRange result_types,
                               RegionBuilder body) {
    ProduceOneOp op = rw->create<ProduceOneOp>(loc, result_types, token, iter);
    body(op.body(), op.region_arguments());
    ensure_pipeline_yield(op.body());
    return op;
}

ConsumeOneOp build_consume_one(Rewriter *rw,
                               Location loc,
                               ConsumerToken token,
                               PipelineIterator iter,
                               uint32_t consumer_idx,
                               TypeRange result_types,
                               RegionBuilder body) {
    ConsumeOneOp op = rw->create<ConsumeOneOp>(loc, result_types, token, iter);
    op.set_consumer_idx(consumer_idx);
    body(op.body(), op.region_arguments());
    ensure_pipeline_yield(op.body());
    return op;
}
```

`agent_switch` is variadic in agent body count and carries per-agent register-budget data. The builder keeps body regions, group counts, and max-register lists together so execution-unit propagation can reason about them.

## Tiled Memop Operand/Result Tables

The tiled memory family shares one segmented operand layout. `operandSegmentSizes` separates view, coordinate, offset, token, and optional padding/mask operands so the verifier walks each slice without re-parsing the op.

### `nv_tileas.tiled_load`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | view | `nv_tileaa.tiled_view` or `nv_tileas.tiled_view` | yes | source tile view |
| operand 1..R | coords | `index` | yes (R = view rank) | per-axis coordinate |
| operand R+1.. | offsets | `index` | optional | per-axis offset; segment may be empty |
| token slot | token | `mem_token` or `async_token` | optional | one or zero |
| result 0 | tile | `tile<S × element>` | yes | shape S = atom box shape |
| result 1 | token | `mem_token` or `async_token` | optional | present when token slot was supplied |
| attr `atom` | atom | `AtomAttr` | yes | selects copy/TMA atom |
| attr `mem_semantic` | enum | `weak\|relaxed\|acquire` | optional | `acquire_release` rejected |
| attr `mem_scope` | enum | `tl_blk\|cluster\|gpu\|sys` | required when semantic > weak | rejected when semantic = weak |
| attr `in_bounds` | dense bool | per-axis | optional | defaults to false |
| attr `padding_value` | typed attr | element-typed scalar | optional | only with `in_bounds=false` |
| attr `operandSegmentSizes` | dense i32 | length 4 or 5 | yes | `{view, coords, offsets, token[, mask]}` |

```c
LogicalResult verify_tiled_load(TiledLoadOp op) {
    require_operand_segments(op, {1, op.view().rank(), -1, /*token*/ -1});
    require_optional_token(op);
    require_coordinate_types_match_index(op);
    require_tile_shape_matches_atom_box(op.atom(), op.result(0));
    require_tile_dimensions_power_of_two(op.result(0).shape());

    if (op.mem_semantic() == ACQUIRE_RELEASE) {
        return op.emit_error("tiled_load rejects acquire_release semantic");
    }
    require_scope_iff_non_weak(op.mem_semantic(), op.mem_scope());
    require_padding_only_when_not_in_bounds(op);
    return success();
}
```

### `nv_tileas.tiled_store`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | view | `tiled_view` | yes | destination tile view |
| operand 1 | value | `tile<S × element>` | yes | element type matches view element type |
| operand 2..R+1 | coords | `index` | yes | per-axis coordinate |
| operand R+2.. | offsets | `index` | optional | per-axis offset |
| token slot | token | `mem_token` or `async_token` | optional | |
| result 0 | token | `mem_token` or `async_token` | optional | mirrors input token slot |
| attr `atom` | atom | `AtomAttr` | yes | TMA store, register-to-global, etc. |
| attr `mem_semantic` | enum | `weak\|relaxed\|release` | optional | `acquire` and `acquire_release` rejected |
| attr `mem_scope` | enum | as above | required when semantic > weak | |
| attr `in_bounds` | dense bool | per-axis | optional | |
| attr `padding_value` | typed attr | element-typed scalar | optional | only with `in_bounds=false` |
| attr `operandSegmentSizes` | dense i32 | length 4 or 5 | yes | |

### `nv_tileas.tiled_atomic_rmw`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | view | `tiled_view` | yes | atomic destination |
| operand 1 | value | `tile<S × element>` | yes | RMW operand |
| operand 2..R+1 | coords | `index` | yes | per-axis coordinate |
| token slot | token | `mem_token` | optional | |
| result 0 | tile | `tile<S × element>` | yes | old value tile |
| result 1 | token | `mem_token` | optional | |
| attr `atom` | atom | `AtomAttr` | yes | |
| attr `rmw_mode` | enum | `add\|and\|or\|xor\|xchg\|min\|max\|umin\|umax\|cmpxchg\|addf` | yes | |
| attr `mem_semantic` | enum | full set | optional | matches CAS semantics |
| attr `mem_scope` | enum | as above | required when semantic > weak | |
| attr `operandSegmentSizes` | dense i32 | length 4 | yes | |

The atomic verifier also rejects 8-bit element types across all modes and rejects 16-bit integer atomics; 16-bit floating atomics restrict the mode set to `add`, `max`, `min`. The shared invariants for memory semantics, scope, and tile-shape validation appear in [Verifiers](verifiers.md#tiled-memop-verification).

## Tiled Load and Store Builders

The most common composite builders emit a view followed by a tiled memory operation. They normalize rank and coordinate widths, attach operand segment sizes, and carry memory-ordering attributes through.

```c
TiledLoadOp build_view_then_tiled_load(Rewriter *rw,
                                      Location loc,
                                      Value source,
                                      TileViewSpec view,
                                      TiledLoadAttrs attrs) {
    Value tile_view = rw->create<ViewOp>(loc, view.type, source, view.indices);
    return rw->create<TiledLoadOp>(
        loc,
        attrs.result_types,
        tile_view,
        attrs.coords,
        attrs.offsets,
        attrs.token,
        attrs.semantic_attrs());
}

TiledStoreOp build_view_then_tiled_store(Rewriter *rw,
                                        Location loc,
                                        Value value,
                                        Value destination,
                                        TileViewSpec view,
                                        TiledStoreAttrs attrs) {
    Value tile_view = rw->create<ViewOp>(loc, view.type, destination, view.indices);
    return rw->create<TiledStoreOp>(
        loc,
        tile_view,
        value,
        attrs.coords,
        attrs.offsets,
        attrs.token,
        attrs.semantic_attrs());
}
```

Scheduling preparation and materialization passes lean on these builders because they repeatedly need the same view-plus-memory-operation shape.

## Dot and Mask Builders

Dot builders cover several recurring patterns:

- allocate a zero accumulator and emit a dot;
- wrap dot emission in `scf.for` and `scf.if` when a predicate or stage guard is needed;
- synthesize a predicate mask, convert layout, and emit dot;
- install dot simplification patterns for select-constant cases.

```c
Value build_zero_accumulator_dot(Rewriter *rw,
                                 Location loc,
                                 DotInputs inputs,
                                 Type acc_type,
                                 AtomAttr atom) {
    Value acc = rw->create<AllocTensorOp>(loc, acc_type);
    Value zero = rw->create<arith::ConstantOp>(loc, zero_attr(acc_type));
    initialize_accumulator(rw, acc, zero);
    return rw->create<DotOp>(loc, inputs.a, inputs.b, acc, atom).result();
}
```

Dot builders preserve the atom and signedness attributes — later NVGPU/NVVM lowering uses them to pick the actual instruction.

## Arithmetic Helper Builders

The builder library also ships thin wrappers for common `arith` operations: constants, add, multiply, subtract, signed division, signed max, and select. These helpers let composite TileAS builders materialize index math without depending on caller-specific boilerplate.

```c
Value build_index_expr(Rewriter *rw, Value base, Value lane, Value stride) {
    Value scaled = rw->create<arith::MulIOp>(lane.get_loc(), lane, stride);
    return rw->create<arith::AddIOp>(base.get_loc(), base, scaled);
}
```

Wrappers must not add overflow or fast-math attributes unless the caller explicitly asks for them. Defaults belong to the `arith` dialect operation itself.

## Schedule Infrastructure Builders

After schedule generation, three helper algorithms convert analysis into concrete IR:

| Helper | Purpose |
|---|---|
| materialize schedule | partitions resident and pending loads/stores/async roots from schedule analysis |
| build stages | turns union constraints into stage-ordered producer/consumer pairs |
| expand single tiled op | clones a tiled operation for each scheduled stage and rewires operands |

```c
ScheduleMaterialization materialize_schedule(ScheduleAnalysis analysis, MaterializeOptions options) {
    ScheduleMaterialization out = {};
    out.resident_loads = compute_resident_loads(analysis, options);
    out.resident_stores = compute_resident_stores(analysis, options);
    out.pending_loads = expand_iteration_arguments(analysis, Side::Read);
    out.pending_stores = expand_iteration_arguments(analysis, Side::Write);
    out.resident_async = filter_async_eligible(out.resident_loads, options);
    out.pending_async = filter_async_eligible(out.pending_loads, options);
    return out;
}
```

Stage expansion needs two maps: one from original operands to their source operation, and one from each source operation to the per-stage replica. Those two maps are what let a single scheduled tiled operation become several stage-specific SSA operations without mixing operands from different stages.

```c
void expand_single_tiled_op(TiledOp op, StageMap stages, Rewriter *rw) {
    OperandSourceMap sources = collect_operand_sources(op);
    ReplicaMap replicas = clone_op_per_stage(op, stages, rw);

    for (Operation *replica : replicas.values()) {
        for (OpOperand &operand : replica->get_op_operands()) {
            if (Value repl = lookup_stage_replacement(operand, sources, replicas)) {
                operand.set(repl);
            }
        }
    }
}
```

