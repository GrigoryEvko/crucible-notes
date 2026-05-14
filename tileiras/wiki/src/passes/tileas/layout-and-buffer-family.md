# TileAS Layout and Buffer Family

## Abstract

The layout and buffer passes decide where tile values live, remove redundant layout conversions, canonicalize buffer aliases, prune dead region arguments, materialize shared-memory handoffs between agents, and split sliced loops. They run after async and schedule materialization has exposed producer/consumer structure, but before final scheduling and lowering demand stable memory layouts.

The family is internal to the TileAS pipeline, but its public contract is concrete: load/store-class operations come out with assigned layouts, buffer aliases are explicit, agent boundaries cross through shared memory when needed, and sliced loops expose independent per-slice regions.

## Pass Roster

| Pass | Purpose |
|---|---|
| `TileASAssignLoadStoreLayouts` | assigns register, shared-memory, tensor-memory, and tiled layouts for load/store groups |
| `TileASRemoveLayoutConversions` | commutes and deletes redundant `convert_layout` operations |
| `TileASRemoveBufferAliasPass` | rewrites aliased SMEM/TMEM allocs through selects and loops into canonical buffers |
| `TileASRemoveDeadArgs` | removes unused block arguments from region-branch operations |
| `TileASResolveAgentBoundary` | inserts shared-memory handoffs across `agent_switch` boundaries |
| `TileASSlicingPass` | splits loops carrying a `sliceCount` attribute into per-slice loop regions |

## Assign Load/Store Layouts

D14 picks concrete memory layouts — shared, blocked, dot-operand, or linear — for every loadable or storable value flowing through a pipelined kernel. It runs at function scope through three cooperating layers: an outer driver that walks the function and partitions ops into pipeline alias groups, a per-group candidate collector that enumerates the layouts every op in the group could agree on, and a pipeline-layout assigner that scores those candidates against a hardware-cost model and writes the winning attribute back onto each op.

The outer driver `sub_79CAD0` is the body of `runOnOperation`. It walks the function once, treats each `nv_tileas.convert_layout`-bracketed region as a seed, and follows producer/consumer edges that share a layout to grow the seed into a maximal pipeline alias group. Each completed group passes to the second layer; the driver itself holds no scoring state — only the partitioning.

The per-group candidate collector `sub_79A080` is the largest function in the family — roughly 10.8 KB across 511 basic blocks with 78 callees. It produces a `SmallVector<LayoutCandidate>` keyed by the triple `(memKind, sub_layout_axis, alignment)`. The four sub-layout axes (A, B, C, D) of a dot-product pipeline each get a specialised emitter, because a candidate for operand A of a WGMMA carries different alignment and stride constraints than one for the accumulator. The A-axis emitter is `sub_783A50`, the B-axis `sub_7842B0`, and the C-axis (accumulator) `sub_784B10`. The D-axis emitter inlines directly into `sub_79A080` — the D operand never participates in operand-broadcast paths and needs no out-of-line specialisation.

The pipeline-layout assigner `sub_797F70` is roughly 8.5 KB across 388 basic blocks. It receives the candidate set from layer 2, scores each candidate against a hardware-cost model weighting TMEM bandwidth, SMEM bank-conflict count, and register pressure, picks the lowest-cost feasible candidate, and rewrites each op's `nv_tileas.layout` attribute in place. When no candidate is feasible for the whole group, it routes through the diagnostic emitter `sub_446CE00` with the verbatim message `" can not find common memKind among pipeline alias group\n"` (leading space and trailing newline are part of the constant). The terseness is intentional — the upstream candidate collector already filtered every per-op reason, and only the group-level memKind disagreement remains by the time control lands here.

The driver classifies each op in a group by comparing its operation classID against five sentinel pointers, then dispatches to the matching candidate emitter inside layer 2:

| classID | Op role |
|---|---|
| `&unk_5BE6138` | null or erased sentinel; skip without dispatch |
| `&unk_5B44F38` | `produce_one` op; emits producer-side memory candidates |
| `&unk_5B44F70` | `consume_one` op; emits consumer-side memory candidates |
| `&unk_5B44EB8` | `view` op; threads existing layout through without new candidates |
| `&unk_5B44FB8` | `convert_layout` op; seeds register-side candidates from the target encoding |

Pointer-identity comparison against these five constants is the canonical way to distinguish op roles inside the family — no virtual call, no string compare. Reordering the comparisons doesn't affect correctness, but checking the `&unk_5BE6138` sentinel first short-circuits erased ops cheaply.

The layout family on each candidate is distinguished by a second set of pointer-identity comparisons against four TypeID anchors:

| TypeID | Layout family |
|---|---|
| `&unk_5B46FA8` | shared layout (SMEM-backed tile) |
| `&unk_5BAADB8` | blocked layout (register-backed warp tiling) |
| `&unk_5B46FA0` | dot-operand layout (WGMMA operand-A or operand-B) |
| `&unk_5BE6038` | linear layout (CuTe-style basis descriptor) |

The cost scorer in layer 3 reads the TypeID first, then dispatches to a family-specific cost function. Candidates whose family disagrees with the rest of the group are pruned before scoring, not penalised — keeping the scoring loop's branch profile flat.

```c
LogicalResult assignLayouts(FunctionOpInterface fn) {
    SmallVector<PipelineGroup> groups = collectPipelineGroups(fn);              // Layer 1: sub_79CAD0
    for (PipelineGroup &g : groups) {
        SmallVector<LayoutCandidate> cands = collectCandidates(g);              // Layer 2: sub_79A080
        if (cands.empty()) {
            return emitDiag(" can not find common memKind among pipeline alias group\n");
        }
        Layout best = pickByCost(cands, hwModel());                              // Layer 3: sub_797F70
        applyLayout(g, best);
    }
    return success();
}
```

The per-operation rewrite dispatcher covers ordinary loads and stores, tiled loads and stores, tiled atomics, gather/scatter ops, register-layout index math, and TMA-preferred paths. An environment switch biases eligible load/store ops toward TMA form, but verifier checks remain authoritative.

See [pipe-mutex-value-layout.md](pipe-mutex-value-layout.md) for the downstream consumer of the assigned `nv_tileas.layout` attribute, [buffer-assignment-and-mbarriers.md](buffer-assignment-and-mbarriers.md) for how the chosen memKind feeds buffer materialisation, and [mlir-infra/typeid-sentinels-and-anchors.md](../../mlir-infra/typeid-sentinels-and-anchors.md) for the pointer-identity dispatch convention used by both the classID and TypeID tables above.

## Candidate Records

Each operation contributes candidates in four conceptual buckets:

| Bucket | Meaning |
|---|---|
| A register | source or destination is register-backed for operand A |
| A memory | source or destination is memory-backed for operand A |
| B register | source or destination is register-backed for operand B |
| B memory | source or destination is memory-backed for operand B |

The assignment pass picks one compatible memory kind across the alias group. With no common kind available, it fails rather than guessing a conversion.

## Remove Layout Conversions

`TileASRemoveLayoutConversions` cuts the `nv_tileas.convert_layout` count through alternating propagation and greedy cleanup. A single greedy pass is not enough — conversions move in opposite directions depending on whether values live in buffer space or register space.

```c
LogicalResult remove_layout_conversions(FuncOp func) {
    if (failed(propagate_buffer_layouts(func))) {
        mark_pass_failure_but_continue();
    }
    if (failed(propagate_register_layouts(func))) {
        mark_pass_failure_but_continue();
    }

    apply_greedy_cleanup(func);
    rewrite_layout_sensitive_ops(func);
    apply_greedy_cleanup(func);
    apply_greedy_cleanup(func);

    return success_if_no_required_failure();
}
```

The rewrite dispatcher handles `convert_layout`, pipeline consumer regions, pragma-like paired forms, `scf.if`, and elementwise or same-encoding ops. Semantic layout changes survive; identity and commute-cancellable conversions disappear.

```c
LogicalResult rewrite_layout_sensitive_op(Operation *op, Rewriter *rw) {
    switch (op_name_tag(op)) {
        case OP_CONVERT_LAYOUT:
            return fold_identity_or_commute(op, rw);
        case OP_PIPELINE_CONSUME_ONE:
            return propagate_through_consumer_region(op, rw);
        case OP_PRAGMA:
            return rewrite_paired_pragma(op, rw);
        case OP_SCF_IF:
            return unify_layouts_across_arms(op, rw);
        default:
            if (is_elementwise(op) || preserves_encoding(op)) {
                return propagate_operand_layout_to_result(op, rw);
            }
            return failure();        /* leave unfamiliar op untouched */
    }
}
```

## Remove Buffer Aliases

SMEM and TMEM allocations alias through `arith.select` or loop-carried values. `TileASRemoveBufferAliasPass` rewrites those aliases into a canonical allocation plus explicit copy or view ops.

```c
LogicalResult rewrite_buffer_select(SelectOp select, Rewriter *rw) {
    if (!is_smem_or_tmem(select.result().get_type())) {
        return failure();
    }
    if (!select.condition().get_type().is_i1()) {
        return failure();
    }

    AllocTensorOp true_alloc = find_last_written_alloc(select.true_value());
    AllocTensorOp false_alloc = find_last_written_alloc(select.false_value());
    if (!true_alloc || !false_alloc) {
        return failure();
    }

    AllocTensorOp canonical = choose_canonical_alloc(true_alloc, false_alloc);
    if (layouts_differ(canonical, select.result())) {
        rw->create("nv_tileas.copy", select.result(), canonical);
    } else {
        rw->create("nv_tileas.view", canonical, select.result().get_type());
    }

    rw->replace_op(select, canonical);
    return success();
}
```

The pass is iterative but bounded. Failure to converge aborts compilation with a clear diagnostic — leaving unstable aliases for scheduling is not an option.

## Remove Dead Region Arguments

`TileASRemoveDeadArgs` is a hygiene pass for any op implementing region-branch behavior, including structured control flow and async pipeline regions. It drops region init operands and block arguments that go unused inside the target region.

Both sides must move together: deleting a block argument without deleting the corresponding incoming operand breaks region-branch invariants.

```c
void remove_dead_region_args(RegionBranchOp op) {
    for (Region &region : op.regions()) {
        for (BlockArgument arg : region.entry_block().arguments()) {
            if (!arg.use_empty()) {
                continue;
            }

            Operand incoming = op.incoming_operand_for(arg);
            region.entry_block().erase_argument(arg.index());
            op.erase_incoming_operand(incoming.index());
        }
    }
}
```

## Resolve Agent Boundaries

Warp-specialized programs partition work across producer, consumer, and compute agents. Values crossing an `agent_switch` boundary can't always stay as direct SSA values — they often need a shared-memory handoff.

The canonical handoff is:

```text
nv_tileaa.splat
nv_tileaa.extract
nv_tileas.alloc_tensor
nv_tileas.copy
nv_tileas.convert_layout
```

The splat/extract pair encodes per-agent tile coordinates. The allocation and copy materialize the shared-memory transfer. The final layout conversion delivers the value in the destination agent's expected layout.

```c
Value materialize_agent_boundary(Value value, AgentBoundary boundary, Rewriter *rw) {
    Value tiled = rw->create("nv_tileaa.splat", value, boundary.tile_shape()).result(0);
    Value slice = rw->create("nv_tileaa.extract", tiled, boundary.agent_coord()).result(0);
    Value smem = rw->create("nv_tileas.alloc_tensor", boundary.shared_type()).result(0);

    rw->create("nv_tileas.copy", slice, smem, boundary.copy_atom());
    return rw->create("nv_tileas.convert_layout", smem, boundary.dest_layout()).result(0);
}
```

Named-barrier emission stays deferred. This pass establishes the data handoff; synchronization is a later pass's job.

## Slicing

`TileASSlicingPass` splits loops carrying a `sliceCount` attribute into parallel slice regions. The parser checks that the layout is blocked, the requested slice count is an integer, and the divided tile fits under the warp budget. When the count is too high, the pass falls back to the largest supported power-of-two slice factor.

```c
LogicalResult slice_loop(ScfForOp loop, uint32_t slice_count, Rewriter *rw) {
    if (!has_supported_blocked_layout(loop)) {
        return failure();
    }

    SlicePlan plan = build_slice_plan(loop, slice_count);
    if (!plan.valid()) {
        return failure();
    }

    for (uint32_t slice = 0; slice < plan.count; ++slice) {
        ScfForOp slice_loop = clone_loop_for_slice(loop, slice, plan, rw);
        rewrite_slice_operands(slice_loop, slice, plan, rw);
    }

    rw->erase_op(loop);
    return success();
}
```

The pass rejects unsupported ops inside the sliced region and copy ops whose atom cannot be interpreted as a valid CopyAtom.

## Layout Descriptor Grammar

Candidate records use CuTe-style layout descriptors. A descriptor token like `(1@0,1@1)` describes basis vectors: each `<count>@<dim>` term maps `count` lanes onto output dimension `dim`. The grammar supports nested groups, comma-separated lists, dimensions, and fractional bases.

```text
layout      := group
group       := "(" item ("," item)* ")"
item        := basis | group
basis       := integer fraction? "@" integer ("@" integer)*
fraction    := "/" integer
```

The identity descriptor `(1@0,1@1)` is the trivial two-dimensional projection. Gather and scatter paths build their memory-layout candidates against this grammar.

