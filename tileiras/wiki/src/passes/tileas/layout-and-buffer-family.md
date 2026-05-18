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
| `TileASResolveAgentBoundary` | legalises values crossing `agent_switch` boundaries (documented under [CTA Cluster Family — D20 aux passes](cta-cluster-family.md#d20-aux-passes)) |
| `TileASSlicingPass` | splits loops carrying a `sliceCount` attribute into per-slice loop regions |

## Assign Load/Store Layouts

D14 picks concrete memory layouts — shared, blocked, dot-operand, or linear — for every loadable or storable value flowing through a pipelined kernel. It runs at function scope through three cooperating layers. The outer driver walks the function and partitions ops into pipeline alias groups by following producer/consumer edges between `produce_one`/`consume_one` pairs and `convert_layout` seeds. The per-group candidate collector enumerates every layout each op in the group could legally accept, keyed by `(memKind, sub_layout_axis, alignment)`. The pipeline-layout assigner scores the surviving candidates against a three-term hardware-cost model and writes the winning `nv_tileas.layout` attribute back onto each op.

The four sub-layout axes (A, B, C, D) of a dot-product pipeline have specialised emitters because a candidate for operand A of a WGMMA carries different alignment and stride constraints than the accumulator. A-axis and B-axis emitters handle the operand-broadcast paths, the C-axis emitter handles the accumulator, and the D-axis emitter handles the result; D never participates in operand-broadcast paths and is inlined directly into the candidate collector.

When the candidate collector returns an empty set for a group, the assigner emits the verbatim diagnostic `" can not find common memKind among pipeline alias group\n"` (the leading space and trailing newline are part of the constant). The terseness is intentional — the upstream candidate collector has already attached per-op diagnostics for every other failure shape, and by the time control reaches the group-level emitter only the cross-op memKind disagreement remains to report.

> ⚡ **QUIRK — diagnostic constant carries a leading space and a trailing newline**
> The string `" can not find common memKind among pipeline alias group\n"` includes both a leading space and an embedded `\n` — both bytes are part of the string-pool constant, not formatter side-effects. A grep that anchors with `^can` misses the message; a frontend that wraps diagnostics with its own newline produces a double blank line. The composition is intentional (the upstream emitter assumes a trailing punctuation slot was already consumed), but reproducing it byte-for-byte matters for log scrapers.

The driver dispatches each op in a group on its op kind:

| Op kind | Candidate-collector behaviour |
|---|---|
| erased sentinel | skip without dispatch |
| `produce_one` | emit producer-side memory candidates |
| `consume_one` | emit consumer-side memory candidates |
| `view` | thread existing layout through without new candidates |
| `convert_layout` | seed register-side candidates from the target encoding |

Each candidate also carries a layout family — one of shared, blocked, dot-operand, or linear — and the cost scorer dispatches its family-specific cost function on this tag. Candidates whose family disagrees with the rest of the group are pruned before scoring rather than penalised, keeping the scoring loop's branch profile flat. The op-kind and layout-family dispatch both use the pointer-identity convention described in [TypeID Sentinels and Anchors — Idiom 1 — Static Pointer-Identity Sentinel](../../mlir-infra/typeid-sentinels-and-anchors.md#idiom-1-static-pointer-identity-sentinel).

```c
LogicalResult assignLayouts(FunctionOpInterface fn) {
    SmallVector<PipelineGroup> groups = collectPipelineGroups(fn);   // Layer 1
    for (PipelineGroup &g : groups) {
        SmallVector<LayoutCandidate> cands = collectCandidates(g);   // Layer 2
        if (cands.empty()) {
            return emitDiag(" can not find common memKind among pipeline alias group\n");
        }
        Layout best = pickByCost(cands, hwModel());                  // Layer 3
        applyLayout(g, best);
    }
    return success();
}
```

The per-operation rewrite dispatcher covers ordinary loads and stores, tiled loads and stores, tiled atomics, gather/scatter ops, register-layout index math, and TMA-preferred paths. An environment switch biases eligible load/store ops toward TMA form, but verifier checks remain authoritative.

### Three-Layer Cost Model

Layer 1 enumerates candidate atoms per op. Layer 2 filters by structural legality: operand shape must match the atom's accepted shape, the memory space must match the atom's source/destination domains, and the alignment of each operand must satisfy the atom's minimum. Layer 3 scores the remaining candidates against three additive cost terms:

- **SMEM bank-conflict cost** — the number of 32-byte transactions required to service the chosen swizzle without two threads of a warp hitting the same shared-memory bank in the same cycle. The cost is the count of conflict-free transactions; an atom that needs four transactions to deliver a tile row costs more than one that needs one.
- **TMEM bandwidth cost** — for SM100 and SM103 paths only, the number of tensor-memory cycles per tile row consumed by the chosen `tcgen05` atom. The cost is denominated in cycles directly.
- **Register pressure cost** — the count of live registers across the atom's window. Atoms that materialise fragments in registers (`ldmatrix.sync` family) add the fragment size to the cost; atoms that keep the value memory-resident (`cp.async.bulk.tensor` family) contribute zero register cost but pay in SMEM and TMEM terms.

The scorer sums the three terms with a fixed weight vector, breaks ties on register pressure first then SMEM bank-conflict count, and returns the candidate with the lowest score that the structural filter has not already pruned.

### Worked Example: Load of `tensor<128x64xf16>` from SMEM

Consider a single `tiled_load` of a `tensor<128x64xf16>` value out of shared memory, with three structurally legal candidate atoms reaching layer 3:

| Atom | SMEM transactions | TMEM cycles/row | Live registers |
|---|---|---|---|
| `LDSM_M88` (ldmatrix.sync.aligned.m8n8.x4) | 4 | n/a | 32 |
| `LDSM_M816` (ldmatrix.sync.aligned.m8n16.x4) | 2 | n/a | 64 |
| `CP_ASYNC_BULK_TENSOR` (cp.async.bulk.tensor.2d.shared) | 1 | 0 | 0 |

With the default weight vector `w = (1, 4, 0.25)` on `(SMEM, TMEM, registers)`:

- `LDSM_M88`: `1·4 + 4·0 + 0.25·32 = 12`
- `LDSM_M816`: `1·2 + 4·0 + 0.25·64 = 18`
- `CP_ASYNC_BULK_TENSOR`: `1·1 + 4·0 + 0.25·0 = 1`

`CP_ASYNC_BULK_TENSOR` wins because it keeps the value memory-resident, avoiding the register-fragment cost the two `LDSM` atoms pay. If the surrounding context already binds the consumer to a register-fragment layout (a downstream WGMMA, for example), structural filtering eliminates the bulk-tensor candidate at layer 2 and the scorer chooses between the two `LDSM` atoms; `LDSM_M88` wins on the tie-break because its register-pressure cost is half that of `LDSM_M816`.

### Input / Output Shape

Input — a function with `tiled_load`/`tiled_store` ops carrying no layout attribute, grouped by upstream pipelining:

```mlir
%a = nv_tileas.tiled_load %src : memref<128x64xf16, #smem> -> tensor<128x64xf16>
%b = nv_tileas.convert_layout %a : tensor<128x64xf16> -> tensor<128x64xf16, #dot_a>
%r = nv_tileas.wgmma %b, %w, %acc : ...
```

Output — every load and store now carries a chosen layout, and conversions that the layout pass made redundant fold away in the next pass:

```mlir
%a = nv_tileas.tiled_load %src
       {nv_tileas.layout = <(128, 64), (64, 1), swizzle<3, 4, 3>>}
       : memref<128x64xf16, #smem> -> tensor<128x64xf16, #dot_a>
%r = nv_tileas.wgmma %a, %w, %acc : ...
```

See [Pipe / Mutex Value Layout](../../scheduler/pipe-mutex-value-layout.md) for the downstream consumer of the assigned `nv_tileas.layout` attribute and [Buffer Assignment and mbarriers](../../scheduler/buffer-assignment-and-mbarriers.md) for how the chosen memKind feeds buffer materialisation.

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

### Two-Way Propagation

The buffer-side propagator walks backwards from each `convert_layout` whose source is a buffer-typed value (SMEM or TMEM). For each such conversion, it asks the producer whether its result type can be rebuilt at the conversion's target layout; if yes, it re-types the producer, redirects every other use of the original result through a fresh `view`, and deletes the conversion. The buffer side is the natural direction for this rewrite because SMEM and TMEM allocations carry their layout in their result type, so retyping a producer's result is a local edit rather than a transitive rewrite.

The register-side propagator walks forwards from each `convert_layout` whose source is a register-typed value. For each such conversion, it visits every elementwise or layout-transparent consumer and asks whether the consumer can adopt the conversion's target layout instead of the source layout; if yes, it absorbs the layout into the consumer's result type and re-points downstream uses. Forward propagation continues until it meets either a layout-fixing consumer (a `wgmma`, `tcgen05`, or a tiled load/store with an assigned memKind) or an unfusable boundary, at which point the propagator leaves the conversion in place. Each propagator can fail without aborting the pass; the recorded failure flag only blocks the final `success()` return.

### Propagate-Rewrite-Cleanup Cycle

The pass runs the propagators once, then runs three greedy cleanup sweeps separated by a single rewrite-layout-sensitive-ops sweep. The structure is `propagate → cleanup → rewrite → cleanup → cleanup`. The first cleanup folds the conversions the propagators have already identified as redundant. The rewrite sweep visits the layout-sensitive ops (`scf.if`, paired `produce_one`/`consume_one`, paired pragma ops) and commutes adjacent conversions or unifies layouts across arms. The two trailing cleanups converge the rewrite sweep's output: the second cleanup folds the conversions the rewrite sweep made identity, and the third catches the new commute opportunities the `scf.if` unification has exposed by sinking a conversion past the merge point.

Convergence is bounded because every cycle either folds at least one conversion (strictly reducing the conversion population) or makes no rewrite at all (terminating). The 3-cleanup count is an empirical upper bound — the cleanup pattern set is closed under one cycle of `scf.if` unification, and the third cleanup is the safety margin that absorbs interactions between elementwise propagation and `scf.if` unification on the same value.

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

### Failure Modes

Semantic layout changes survive every cycle. A `convert_layout` whose source and destination disagree on memKind (register ↔ SMEM, or SMEM ↔ TMEM) never folds: the buffer- and register-side propagators both refuse to retype across memKinds. A conversion between two encodings within the same memKind survives when a memory-consistency op (a `nv_tileas.fence`, an `async_wait`, or a paired `produce_one`/`consume_one` whose pipeline depth is non-trivial) lies between the conversion and the producer or consumer the propagator would otherwise retype — the consistency op pins the value's layout at the boundary and the propagator backs off.

The pattern set produces no diagnostics on these failures; surviving conversions are valid IR, just not optimal. The pass returns `failure()` only when one of the directional propagators trips an internal invariant (a re-type produces an op the verifier rejects, for example), which surfaces through the standard MLIR pass-failure diagnostic rather than a custom emitter.

### Input / Output Shape

Input — an SMEM producer followed by a layout conversion before a WGMMA operand:

```mlir
%t = nv_tileas.tiled_load %src : memref<...> -> tensor<128x64xf16, #smem_blocked>
%c = nv_tileas.convert_layout %t : tensor<128x64xf16, #smem_blocked>
                                -> tensor<128x64xf16, #smem_swizzled>
%r = nv_tileas.wgmma %c, %w, %acc : ...
```

Output — the producer has been retyped to the conversion's target layout, and the conversion folds away:

```mlir
%t = nv_tileas.tiled_load %src : memref<...> -> tensor<128x64xf16, #smem_swizzled>
%r = nv_tileas.wgmma %t, %w, %acc : ...
```

## Remove Buffer Aliases

`TileASRemoveBufferAliasPass` collapses alias chains over SMEM and TMEM allocations into a canonical allocation plus, when the alias was renaming the layout, an explicit `nv_tileas.copy` or `nv_tileas.view`. Two alias shapes appear in practice.

### Select-on-Condition Aliases

The first shape is `tile.select %c, %a, %b` (the dialect's variant of `arith.select`) on a 1-bit condition where both operands are SMEM- or TMEM-typed buffers. Both branches refer to the same underlying allocation through different SSA values, typically because a double-buffered pipeline names its two slots and a control-flow path selects between them. When both operands trace back to the same `alloc_tensor`, the select collapses.

```mlir
// Before
%a = nv_tileas.alloc_tensor : tensor<128x64xf16, #smem>
%b = nv_tileas.view %a {offset = 8192} : tensor<128x64xf16, #smem>
%buf = tile.select %flag, %a, %b : tensor<128x64xf16, #smem>

// After — both arms share the canonical allocation %a; the select is gone.
%a   = nv_tileas.alloc_tensor : tensor<128x64xf16, #smem>
%buf = nv_tileas.view %a {offset = tile.select(%flag, 0, 8192)} : tensor<128x64xf16, #smem>
```

### Loop-Carried Aliases

The second shape is `scf.for` whose iter-arg is initialised from a buffer SSA value and whose yield in the loop body produces the same underlying allocation; the buffer is threaded through the loop body for legibility but adds no temporal storage. When the iter-arg and yield trace back to the same allocation, the iter-arg drops out and consumers inside the body refer directly to the canonical allocation.

```mlir
// Before — %buf is loop-carried but every iteration yields the same allocation.
%a = nv_tileas.alloc_tensor : tensor<128x64xf16, #smem>
%r = scf.for %i = %c0 to %n step %c1 iter_args(%buf = %a) -> tensor<128x64xf16, #smem> {
    %x = use %buf
    scf.yield %a : tensor<128x64xf16, #smem>
}

// After — %a is referenced directly inside the body; the iter-arg is gone.
%a = nv_tileas.alloc_tensor : tensor<128x64xf16, #smem>
scf.for %i = %c0 to %n step %c1 {
    %x = use %a
}
```

### Canonical-Allocation Tracer

The driver walks the function looking for these shapes. For each, it traces back through `view`, `select`, and the loop-carried path to the `nv_tileas.alloc_tensor` that produced storage; this is the canonical allocation. If the alias preserved the layout, the pass replaces the alias with a `view` of the canonical allocation; if the alias changed layout (the rare case where a `select` chose between buffers laid out differently), the pass inserts a `copy` first so the consumer's view sees the expected layout.

```c
AllocTensorOp find_last_written_alloc(Value v) {
    while (Operation *def = v.getDefiningOp()) {
        if (auto alloc = dyn_cast<AllocTensorOp>(def)) return alloc;
        if (auto view  = dyn_cast<ViewOp>(def))       { v = view.source();      continue; }
        if (auto copy  = dyn_cast<CopyOp>(def))       { v = copy.destination(); continue; }
        if (auto sel   = dyn_cast<SelectOp>(def)) {
            AllocTensorOp lhs = find_last_written_alloc(sel.true_value());
            AllocTensorOp rhs = find_last_written_alloc(sel.false_value());
            if (lhs == rhs && lhs) return lhs;
            return nullptr;                  // arms disagree; not an alias
        }
        emitDiag("Cannot find last written SSA.");
        return nullptr;
    }
    // v is a block argument — walk back through region predecessors
    return trace_through_region_predecessors(v);
}

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

### Convergence Bound

The pass iterates the rewrite until the function reaches a fixed point. Convergence is bounded by `N`, the depth of the deepest alias chain in the function — each iteration strictly reduces that depth, since every rewrite eliminates one alias hop on the path from a use to the canonical allocation. A program whose deepest alias chain is `select(view(view(alloc, ...), ...), ...)` converges in three iterations.

### Failure Modes

The tracer fails when its walk reaches a defining op that is neither a pure tile-structure op (`view`, `copy`, `select`) nor an `alloc_tensor`. Typical culprits are an `affine.apply` that synthesises a buffer pointer, a `call` returning an SMEM buffer from another function, or a `select` whose two arms trace to different allocations. The failure emits the verbatim diagnostic `"Cannot find last written SSA."` and the alias stays in the IR. Downstream passes that identify each tensor allocation by SSA value (notably the buffer-assignment pass in the scheduler family) will then see the alias and refuse to compute a barrier layout for it.

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

### Input / Output Shape

Input — a `scf.for` whose `%pre_acc` iter-arg has been left unreferenced because a downstream pass folded its single use into an in-place update on `%acc`:

```mlir
%r:2 = scf.for %i = %c0 to %n step %c1
       iter_args(%acc = %init_acc, %pre_acc = %init_pre)
       -> (tensor<128x128xf32>, tensor<128x128xf32>) {
    %x   = nv_tileas.wgmma %a, %b, %acc : tensor<128x128xf32>
    scf.yield %x, %pre_acc : tensor<128x128xf32>, tensor<128x128xf32>
}
%out = use %r#0
```

Output — `%pre_acc` and its `%init_pre` incoming operand are gone, the loop's result arity drops to one, and the yield carries only the live value:

```mlir
%r = scf.for %i = %c0 to %n step %c1
     iter_args(%acc = %init_acc) -> tensor<128x128xf32> {
    %x = nv_tileas.wgmma %a, %b, %acc : tensor<128x128xf32>
    scf.yield %x : tensor<128x128xf32>
}
%out = use %r
```

Iterating in reverse index order matters: erasing argument index `i` shifts every higher index down by one, and recording the indices ascending then erasing descending keeps the indices valid throughout the inner loop. The `RegionBranchOpInterface` query for the matching incoming-operand index is asked before the erase, while the indexing is still consistent.

## Resolve Agent Boundaries

`TileASResolveAgentBoundary` runs in this family's ordering window — after layout assignment and buffer canonicalization, before slicing — but its contract and rewriter belong to the CTA/cluster family and are documented under [CTA Cluster Family — D20 aux passes](cta-cluster-family.md#d20-aux-passes). The only invariant the rest of the layout-and-buffer family relies on is the handoff shape: every value crossing an `nv_tileas.async.pipeline.agent_switch` either remains a direct SSA value (when the destination agent can consume it in place) or has been materialised through a shared-memory `alloc_tensor` / `copy` / `convert_layout` chain that delivers it in the destination agent's expected layout. Named-barrier emission stays deferred to [Buffer Assignment and mbarriers — Phase 2 — Assign Named Barriers](../../scheduler/buffer-assignment-and-mbarriers.md#phase-2-assign-named-barriers).

## Slicing

`TileASSlicingPass` splits loops carrying a `sliceCount` attribute into independent per-slice loop regions, exposing parallelism the scheduler can later interleave across warps or async pipeline stages. The pass walks the function looking for `scf.for` (and, on warp-specialized programs, the matching pipeline region ops) that carry a positive `sliceCount` IntegerAttr. For each match, it builds a slice plan: divide the iteration space by the slice count, propagate the divided extent through every tiled operand inside the body, and materialize one cloned region per slice with a fresh induction range and rewritten `insert_slice` ops.

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

### Diagnostics

The slicing transform attaches six verbatim diagnostics to the loop op it is rewriting. Each fires from a different stage of the pass.

``"The `sliceCount` need to be a `IntegerAttr`"`` — fires when the `sliceCount` attribute on the candidate loop is present but is not an `IntegerAttr`. Valid input is `scf.for ... attributes {sliceCount = 4 : i32}`; the diagnostic triggers if `sliceCount` is, for example, a `StringAttr` carrying a stringified count, an `ArrayAttr` of per-stage counts (an old-style encoding the parser still accepts), or an `IntegerAttr` whose underlying value does not fit the loop's iteration space.

`"unsupported op in Slicing pass"` — fires while the plan-builder walks the loop body and meets an op it cannot clone per-slice. Valid input contains only loads, stores, copies, math, control flow, and the pipeline `produce_one`/`consume_one` pair. The diagnostic triggers on ops the rewriter has no clone strategy for — typically a custom dialect op the pipeline was never extended to handle, or a `func.call` to an unknown callee.

`"unsupported op to be a lower bound in slicing pass "` — fires while the plan-builder traces the loop's lower bound. Valid input is a lower bound of the form `affine.apply` over the induction variable of an enclosing loop, or a constant. The diagnostic triggers when the lower bound resolves to an arbitrary SSA value (an `arith.muli` whose operand history the pass cannot decode, a `call` result, or a block argument the pass cannot trace through). The trailing space is part of the constant.

`"fail to get an initial forOperand in slicing pass"` — fires when the plan-builder needs the initial `iter_arg` value to clone into each slice's prologue and the value's defining op either escapes the function (a `func.return` reaches the value first) or is itself loop-carried from an outer region the pass does not traverse.

`"is not expected inside sliced part in SlicingPass"` — fires from the rewrite phase, not the plan-builder. The plan-builder records the set of ops the rewriter expects to clone; if the rewriter walks a cloned slice and finds an op outside that set, the IR has been mutated unexpectedly between plan and rewrite (usually because an earlier match-and-rewrite that the pass tolerated has reshaped the body). The pass refuses to continue.

`"unsupported atom of copyOp in slicing pass"` — fires when the rewriter visits a `copy` op whose `CopyAtomAttrInterface` does not resolve to a concrete CopyAtom. This is almost always a sequencing fault: layout assignment did not finish on the op (no `nv_tileas.layout` attribute was written), so the copy's atom is still abstract and slicing cannot pick the right per-slice atom variant.

### Input / Output Shape

Input — a single loop with `sliceCount = 2`, carrying one `tiled_load` and one `wgmma` over the full iteration space:

```mlir
scf.for %i = %c0 to %c64 step %c1 iter_args(%acc = %init) -> tensor<...>
       attributes {sliceCount = 2 : i32} {
    %a = nv_tileas.tiled_load %src[%i] : ... -> tensor<...>
    %x = nv_tileas.wgmma %a, %b, %acc : tensor<...>
    scf.yield %x : tensor<...>
}
```

Output — two cloned loops, each over half the iteration space, with the operand-side `tiled_load` repointed at the corresponding half of the source:

```mlir
scf.for %i = %c0 to %c32 step %c1 iter_args(%acc0 = %init) -> tensor<...> {
    %a0 = nv_tileas.tiled_load %src[%i] : ... -> tensor<...>
    %x0 = nv_tileas.wgmma %a0, %b, %acc0 : tensor<...>
    scf.yield %x0 : tensor<...>
}
scf.for %j = %c32 to %c64 step %c1 iter_args(%acc1 = %init) -> tensor<...> {
    %a1 = nv_tileas.tiled_load %src[%j] : ... -> tensor<...>
    %x1 = nv_tileas.wgmma %a1, %b, %acc1 : tensor<...>
    scf.yield %x1 : tensor<...>
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

The swizzle triple's three integers are the descriptor packer's `(B, M, S)` parameters — base-2 log of the swizzle period, the mode width, and the swizzle shift respectively — and the closed accepted set of triples matches the swizzle predicate documented under [Mode Pattern Verifiers — UMMA Canonical Layout Verifier](../../dialects/cute_nvgpu/mode-pattern-verifiers.md#umma-canonical-layout-verifier). When the `elem` clause is absent the layout inherits its element type from the value carrying it; when present the named-element-type is looked up against the dialect's element-type registry, with unknown names rejected by the parser before any other validation runs.

### Examples

| Descriptor | Reading |
|---|---|
| `<(1,1),(0,0)>` | identity 1×1 tile; both strides zero, the degenerate base case |
| `<(16,16),(1,16)>` | 16×16 column-major tile, inner stride 1, outer stride 16 |
| `<(16,16),(1,16),swizzle<2,5,2>>` | 16×16 column-major tile with 128-byte swizzle `(B=2, M=5, S=2)` |
| `<(16,(8,2)),(1,(16,8))>` | hierarchical 2-D layout: 16 outer, inner split as 8 sub-tiles of 2 |
| `<(128,64),(64,1),swizzle<3,4,3>,elem=nvfp4>` | 128×64 row-major tile, 128-byte swizzle, descriptor reads NVFP4 elements |
| `<((4,32),64),((1,512),16),elem=mxf4>` | hierarchical layout with named-element override for MXF4 microscaled path |

The first three forms cover the bulk of WGMMA and tcgen05 operand paths. Hierarchical forms appear when a tile is partitioned across warps or warp groups before reaching the descriptor packer — the outer group is the warp partition, the inner group is the per-warp slab. The `elem=` clause appears only on paths where the tensor element type and the descriptor's internal element type differ; NVFP4 and MXF4 are the production callers because the value-carrying tensor is `f8e4m3` or `bf16` but the descriptor's packed payload is sub-byte.

