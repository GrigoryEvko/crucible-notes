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
| `TileASResolveAgentBoundary` | legalises values crossing `agent_switch` boundaries (documented under [CTA Cluster Family](cta-cluster-family.md)) |
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

`TileASRemoveLayoutConversions` shrinks the `nv_tileas.convert_layout` population by alternating two directional propagators with a greedy cleanup driver. The two propagators read in opposite directions because layout demand flows one way through buffer-backed values and the other way through register-backed values, and neither single direction reaches a fixed point on its own.

The pass body walks the function once. The first pass — buffer propagation — pushes layout requests from `convert_layout` ops sitting in front of SMEM/TMEM-backed producers backwards to the producer's result type, then deletes the conversion when the producer can be rebuilt at the requested layout. The second pass — register propagation — pushes layout requests forwards through register-backed elementwise chains until they meet either a consumer that fixes the layout (a `wgmma`, `tcgen05`, or a tiled load/store with an assigned memKind) or an unfusable boundary. Each propagator can fail without aborting the pass; the recorded failure flag only blocks the final `success()` return.

The greedy cleanup is the same `applyPatternsAndFoldGreedily` driver that other tile passes use, parameterized with a small pattern set that handles `convert_layout`, the paired `produce_one`/`consume_one` pipeline ops, the pragma-paired forms emitted by warp specialization, `scf.if` arms whose two branches converge to different layouts, and elementwise / same-encoding ops that can transparently inherit their operand's layout. Each pattern either folds an identity, swaps two adjacent conversions across a commutable op (so the next greedy pass can fold the resulting pair), or unifies layouts across `scf.if` arms by sinking a single conversion past the merge.

The pass runs cleanup three times around a single rewrite-layout-sensitive-ops sweep — propagate-rewrite-cleanup-cleanup-cleanup. The third cleanup catches the conversions left over after `scf.if` unification has introduced new commute opportunities. Semantic layout changes survive every pass: a `convert_layout` whose source and destination disagree on memKind or encoding never folds, only conversions that commute to an identity disappear.

```c
LogicalResult remove_layout_conversions(FuncOp func) {
    bool propagation_failed = false;
    propagation_failed |= failed(propagate_buffer_layouts(func));
    propagation_failed |= failed(propagate_register_layouts(func));

    apply_greedy_cleanup(func);
    rewrite_layout_sensitive_ops(func);
    apply_greedy_cleanup(func);
    apply_greedy_cleanup(func);
    return propagation_failed ? failure() : success();
}

LogicalResult rewrite_layout_sensitive_op(Operation *op, Rewriter *rw) {
    switch (op_kind(op)) {
        case OP_CONVERT_LAYOUT:      return fold_identity_or_commute(op, rw);
        case OP_PIPELINE_CONSUME_ONE:return propagate_through_consumer_region(op, rw);
        case OP_PRAGMA:              return rewrite_paired_pragma(op, rw);
        case OP_SCF_IF:              return unify_layouts_across_arms(op, rw);
        default:
            if (is_elementwise(op) || preserves_encoding(op)) {
                return propagate_operand_layout_to_result(op, rw);
            }
            return failure();
    }
}
```

## Remove Buffer Aliases

`TileASRemoveBufferAliasPass` collapses alias chains over SMEM and TMEM allocations into a canonical allocation plus, when the alias was renaming the layout, an explicit `nv_tileas.copy` or `nv_tileas.view`. Two alias shapes occur in practice. The first is `arith.select` on a 1-bit condition with two SMEM- or TMEM-typed operands — both branches refer to the same logical buffer through different SSA values, often from double-buffered pipelines. The second is loop-carried buffers: an `scf.for` whose iter-arg and yield-operand both alias the same underlying allocation, threaded through the loop body for legibility but adding no temporal storage.

The driver walks the function looking for these shapes. For each, it traces back through `view`, `select`, and the loop-carried path to the last `nv_tileas.alloc_tensor` that produced storage; this is the canonical allocation. If the alias preserved the layout, the pass replaces the alias with a `view` of the canonical allocation; if the alias also changed layout (the rare case where a `select` chose between buffers laid out differently), the pass inserts a `copy` first so the consumer's view sees the expected layout.

```c
LogicalResult rewrite_buffer_select(SelectOp select, Rewriter *rw) {
    if (!is_smem_or_tmem(select.result().get_type())) return failure();
    if (!select.condition().get_type().is_i1())       return failure();

    AllocTensorOp true_alloc  = find_last_written_alloc(select.true_value());
    AllocTensorOp false_alloc = find_last_written_alloc(select.false_value());
    if (!true_alloc || !false_alloc) return failure();

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

The pass iterates the rewrite until the function reaches a fixed point. Convergence is bounded by the depth of the longest alias chain — each pass strictly reduces that depth. Failure to converge — for instance, two aliases mutually referring to each other across a loop boundary the canonical-allocation lookup cannot resolve — aborts the pass via the diagnostic emitted by `find_last_written_alloc` when its walk leaves the function without finding an allocation: `"Cannot find last written SSA."`. Leaving unstable aliases visible to scheduling is not an option because the buffer-assignment pass downstream identifies each tensor allocation by SSA value.

## Remove Dead Region Arguments

`TileASRemoveDeadArgs` is the hygiene pass that follows layout assignment. Once the layout passes have rebuilt op signatures around the chosen memKinds, some block arguments and the matching region init operands fall out of use — most often because a `convert_layout` that was producing one of the loop-carried values has been folded into an equivalent in-place use. The pass walks every op that implements `RegionBranchOpInterface` — `scf.for`, `scf.while`, `scf.if`, and the `nv_tileas.async.pipeline.*` region ops — and drops each block-argument-plus-incoming-operand pair where the block argument has no use inside the region.

The two sides must move together: deleting a block argument without deleting the corresponding incoming operand leaves the region-branch interface in an inconsistent state and trips the next verifier the IR meets. The pass therefore reads the incoming operand index from the interface before the erase, then erases both in one transactional step. Block arguments that still have uses, even uses that only feed the region terminator, are preserved — this pass eliminates only the strictly dead ones.

```c
void remove_dead_region_args(RegionBranchOpInterface op) {
    for (Region &region : op.regions()) {
        SmallVector<unsigned> dead_indices;
        for (BlockArgument arg : region.entry_block().arguments()) {
            if (arg.use_empty()) dead_indices.push_back(arg.index());
        }
        for (unsigned idx : llvm::reverse(dead_indices)) {
            unsigned incoming = op.incoming_operand_index(region, idx);
            region.entry_block().erase_argument(idx);
            op.erase_incoming_operand(incoming);
        }
    }
}
```

## Resolve Agent Boundaries

`TileASResolveAgentBoundary` runs in this family's ordering window — after layout assignment and buffer canonicalization, before slicing — but its contract and rewriter belong to the CTA/cluster family and are documented under [CTA Cluster Family — D20 aux passes](cta-cluster-family.md#d20-aux-passes). The only invariant the rest of the layout-and-buffer family relies on is the handoff shape: every value crossing an `nv_tileas.async.pipeline.agent_switch` either remains a direct SSA value (when the destination agent can consume it in place) or has been materialised through a shared-memory `alloc_tensor` / `copy` / `convert_layout` chain that delivers it in the destination agent's expected layout. Named-barrier emission stays deferred to a later pass.

## Slicing

`TileASSlicingPass` splits loops carrying a `sliceCount` attribute into independent per-slice loop regions, exposing parallelism the scheduler can later interleave across warps or async pipeline stages. The pass walks the function looking for `scf.for` (and, on warp-specialized programs, the matching pipeline region ops) that carry a positive `sliceCount` IntegerAttr. For each match, it builds a slice plan: divide the iteration space by the slice count, propagate the divided extent through every tiled operand inside the body, and materialize one cloned region per slice with a fresh induction range and rewritten `insert_slice` ops.

The plan-building stage refuses several shapes. The `sliceCount` attribute must be an `IntegerAttr`; a different kind of attribute, or a value that does not fit the underlying loop's iteration space, fails the pass with ``"The `sliceCount` need to be a `IntegerAttr`"``. Inside the candidate region every op must either be a known op the rewriter can clone (loads, stores, copies, math, control flow, the pipeline produce/consume pair) or contribute zero IR after slicing. An op outside that set fires `"unsupported op in Slicing pass"`. The slicing transform also examines the lower bound of the loop being sliced — `affine.apply` patterns over the induction variable are supported, arbitrary SSA-defined lower bounds are not. An unsupported lower-bound shape fires `"unsupported op to be a lower bound in slicing pass "`. Pulling the initial `iter_arg` value out of the cloned loop's prologue can fail when the original value escapes the function or is loop-carried from an outer region the pass cannot reach; that failure emits `" fail to get an initial forOperand in slicing pass "`.

Two additional verifications run during the rewrite itself. Each cloned slice must contain only ops the rewriter expected to see — if a child op shows up that was not in the original region (typically a side effect of an earlier failed match-and-rewrite), the pass refuses with `"is not expected inside sliced part in SlicingPass"`. Copies whose `CopyAtomAttrInterface` cannot be resolved to a concrete CopyAtom — usually because layout assignment did not finish for that op — fire `"unsupported atom of copyOp in slicing pass"`.

```c
LogicalResult slice_loop(ScfForOp loop, IntegerAttr count_attr, Rewriter *rw) {
    if (!count_attr) return loop.emitOpError() << "The `sliceCount` need to be a `IntegerAttr`";
    if (!has_supported_blocked_layout(loop))  return failure();

    SlicePlan plan = build_slice_plan(loop, count_attr.getInt());
    if (!plan.valid()) return failure();          // diagnostics already attached

    for (uint32_t s = 0; s < plan.count; ++s) {
        ScfForOp slice = clone_loop_for_slice(loop, s, plan, rw);
        rewrite_slice_operands(slice, s, plan, rw);
    }
    rw->erase_op(loop);
    return success();
}
```

## Layout Descriptor Grammar

`nv_tileas.layout` is serialised as a literal whose parser accepts a shape tuple, a parallel stride tuple, an optional swizzle clause, and an optional named-element-type clause. The shape and stride tuples can nest — nested groups give the parser everything it needs to reconstruct a CuTe-style hierarchical layout — and the swizzle clause is the bit-mask triple `<B, M, S>` that the descriptor packer later threads into shared-memory descriptors. The named-element-type clause overrides the element type inferred from the operand for paths where the descriptor's internal element type differs from the tensor's element type (the NVFP4 and microscaled paths are the visible callers).

```text
layout-desc   := "<" shape "," stride swizzle-opt elem-opt ">"

shape         := tuple
stride        := tuple
tuple         := integer | "(" tuple-item ("," tuple-item)* ")"
tuple-item    := tuple | integer

swizzle-opt   := ("," "swizzle" "<" integer "," integer "," integer ">")?
elem-opt      := ("," "elem" "=" elem-name)?

elem-name     := ident                            -- e.g. "nvfp4", "mxf4", "bf16"
integer       := decimal-uint
```

The identity layout `<(1,1),(0,0)>` describes a degenerate 1x1 tile with both strides zero. A typical 16x16 column-major tile carrying a 128-byte swizzle reads as `<(16,16),(1,16),swizzle<2,5,2>>`. Hierarchical layouts read with one extra group level per nesting: a tile that splits 16 along its inner dimension into 8 sub-tiles of 2 reads as `<(16,(8,2)),(1,(16,8))>`. The swizzle triple's three integers are the descriptor packer's `(B, M, S)` parameters — base-2 log of the swizzle period, the mode width, and the swizzle shift respectively — and the closed accepted set of triples matches the swizzle predicate documented under [Mode Pattern Verifiers — UMMA Canonical Layout Verifier](../../dialects/cute_nvgpu/mode-pattern-verifiers.md#umma-canonical-layout-verifier). When the `elem` clause is absent the layout inherits its element type from the value carrying it; when present the named-element-type is looked up against the dialect's element-type registry, with unknown names rejected by the parser before any other validation runs.

