# TileAS Async and Pipeline Family

## Abstract

The async pipeline family turns token-ordered tile work into explicit producer/consumer scaffolding. Starting from queue-like TileAA/TileAS ops, it materializes `nv_tileas.async.pipeline.*` regions, threads async handshakes through every producer and consumer site, picks between unspecialized and warp-specialized execution, and finally trims each pipeline region to the minimal backward slice of its yielded values.

Downstream LLVM/NVVM lowering converts that scaffold into mbarrier waits, async bulk-copy waits, WGMMA group waits, named barriers, and ordinary LLVM control flow. Phase, stage, iterator, and producer/consumer ownership must survive every transformation in this family bit-for-bit — downstream synchronization assumes them.

## Pass Roster

| Pass or family | Purpose |
|---|---|
| queue-to-pipeline rewrite | rewrites `nv_tileaa.queue.*` and `execute` into async pipeline operations |
| `TileASMaterializeAsync` | injects async tokens, future waits, producer and consumer handshakes |
| `TileASMaterializeConvertLayout` | decomposes layout conversions that cross pipeline boundaries |
| `TileASMaterializeSchedule` | consumes `ScheduleAnalysis` and selects AUS or AWS materialization |
| `TileASUnspecializedPipeline` | software-pipelines single-agent loops with prologue/body/epilogue |
| `TileASOptimizePipelineRegion` | shrinks `produce_one` and `consume_one` regions to minimal scopes |
| block-scaled MMA verifier | checks Blackwell microscale MMA invariants before lowering |
| pipeline-to-NVVM lowerings | convert async pipeline ops to NVVM and LLVM operations |

## Pipeline Operation Surface

The `nv_tileas.async.pipeline` dialect exposes operations for creating a pipeline, switching agents, producing and consuming one stage, reading and writing through the pipeline slot, acquiring and releasing producer/consumer ownership, advancing iterators, and yielding region results.

| Operation concept | Role |
|---|---|
| create pipeline | builds stage count, producer group, consumer group, and memory-mode state |
| create iterator / increment iterator | tracks stage and phase progression |
| producer acquire / commit | claims and publishes a producer stage |
| consumer wait / release | waits for and releases a consumer stage |
| producer write / consumer read | transfers values through the logical pipeline slot |
| produce one / consume one | region operations that scope producer or consumer work |
| agent switch | partitions the function into producer, consumer, and compute agents |
| pipeline yield | returns region values and iterator state |

Region-op verifiers force block argument types and yielded result types to match the pipeline iterator and operation result contract. When an `if` or loop yields a pipeline iterator value, both arms must agree on the iterator type — there is no implicit merge.

## Queue to Pipeline Rewrite

The queue-to-pipeline rewrite bridges TileAA queue ops onto the pipeline surface.

```c
LogicalResult rewrite_queue_program(ModuleOp module) {
    RewritePatternSet patterns(module.context());

    patterns.add(rewrite_execute_to_agent_switch);
    patterns.add(rewrite_create_queue_to_pipeline);
    patterns.add(rewrite_queue_put_to_produce_one);
    patterns.add(rewrite_queue_get_to_consume_one);
    patterns.add(rewrite_mark_for_reuse_passthrough);

    if (failed(apply_patterns_greedily(module, std::move(patterns)))) {
        return failure();
    }

    return propagate_pipeline_iterator_types(module);
}
```

Iterator propagation is not cleanup — it is part of the contract. Every downstream pass assumes producer and consumer regions carry consistent iterator values across structured control flow.

## Materialize Async

`TileASMaterializeAsync` (CLI: `tileas-materialize-async`) takes synchronous tile loops still carrying `nv_tileaa.queue.*` and `execute` ops and rewrites them into the full `nv_tileas.async.pipeline.*` producer/consumer scaffold. It runs at function scope (`OperationPass<FunctionOpInterface>`) and depends on the `SymbolTable` trait. Every async-bearing loop receives a token iter-arg, every async-defining value gets wrapped through `to_async`, and producer and consumer handshakes ring the original tile work.

The pass body lives at `sub_8174C0` (10 137 B, 463 BB). It walks `LoopLikeOpInterface` operations via `sub_8172F0` with callback `sub_819C60` and delegates per-loop rewriting to an assembler. Once the walk finishes, a reconciler verifies that pipeline types stay coherent across every producer-like user.

| Sub | Size | Role |
|---|---|---|
| `sub_813DC0` | 5 518 B | per-loop rewriter; emits `create_none`, `to_async` wrappers, the reissued `scf.for` with token iter-arg, and tail `future_wait` + `async.wait` |
| `sub_81A290` | 6 314 B | consumer emitter; emits `consume_one -> consumer_read -> consume_one_async -> consumer_release -> async.wait` |
| `sub_81BB40` | 5 051 B | producer emitter; emits `produce_one_async -> producer_commit -> token_to_async -> async.wait` |
| `sub_815AD0` | 6 176 B | post-walk reconciler; verifies one `produce_one`-like writer per pipeline across all `AllocationOpInterface` ops |

Consumer sequence:

```text
consume_one -> consumer_read -> consume_one_async -> consumer_release -> async.wait
```

Producer sequence:

```text
produce_one_async -> producer_commit -> token_to_async -> async.wait
```

Exactly one `produce_one`-like op may write data into a given pipeline. On conflict the reconciler emits the verbatim diagnostic ``there are two `produce-one-like` operations using different instructions to generate data into the same pipeline. It's a bug of MaterializeAsync Pass.`` (full sentence, trailing period included) through `sub_446CE00` at severity `259` (`0x103`).

Errors never call `signalPassFailure()` directly. They set `*(self + 40) |= 4`, the cross-pass failure handshake documented in [Pass-Failure Handshake — Convention](../../mlir-infra/pass-failure-handshake.md#convention). The driver inspects it once the walk completes and lifts it to a top-level failure.

### Per-Loop Rewrite Body

The per-loop body at `sub_813DC0` builds the rewritten loop in a single pass over the original region. It seeds the initial token, walks the body to classify each async-bearing op, clones the loop with one extra iter-arg, dispatches to the producer or consumer emitter for each classified op, and tails the new loop with a `future_wait` plus `async.wait` so the function-level user observes a fully synchronized value.

```c
LogicalResult materialize_async_loop(ScfForOp loop, Rewriter *rw) {
    Value initial_token = rw->create("nv_tileas.create_none").result(0);

    SmallVector<Value> async_defs = collect_async_defining_values(loop);
    for (Value v : async_defs) {
        Value storage = rw->create("nv_tileas.async.to_async", v, AS_STORAGE).result(0);
        Value tok     = rw->create("nv_tileas.async.to_async", v, AS_TOKEN).result(0);
        rw->replace_uses_inside_loop(v, storage, tok);
    }

    ScfForOp rewritten = clone_loop_with_extra_iter_arg(loop, initial_token, rw);

    for (Operation *op : rewritten.get_body_ops()) {
        if (is_pipeline_consumer(op)) {
            emit_consumer_handshake(op, rw);   /* sub_81A290 */
        } else if (is_pipeline_producer(op)) {
            emit_producer_handshake(op, rw);   /* sub_81BB40 */
        }
    }

    Value final_token = rewritten.get_loop_result(TOKEN_RESULT_IDX);
    rw->create("nv_tileas.async.future_wait", final_token);
    rw->create("nv_tileas.async.wait", final_token);

    return success();
}
```

Each async-defining value is wrapped twice through `to_async` — once for the storage side, once for the token side. Both wrappers stay live until the tail `future_wait` collapses them back into a synchronized result.

### Input and Output IR Shapes

The input is a synchronous tile loop in which loads, MMAs, and stores are sequenced through ordinary SSA values and `nv_tileaa.queue.*` ops. The shapes the per-loop rewriter expects are precise: a `scf.for` body containing one or more TMA-eligible load chains, with `execute` ops marking the producer side and ordinary tile-compute ops on the consumer side.

```mlir
// Input: synchronous tile loop, TMA-eligible loads on producer side.
%out = scf.for %i = %c0 to %n step %c1 iter_args(%acc = %init) -> tensor<...> {
  %a = nv_tileaa.queue.get %qa[%i] : tensor<...,#smem>
  %b = nv_tileaa.queue.get %qb[%i] : tensor<...,#smem>
  %c = nv_tileas.dot %a, %b, %acc : tensor<...>
  scf.yield %c : tensor<...>
}
```

After the rewrite, the loop carries a token iter-arg, async-defining values are wrapped through `to_async`, the body splits into producer and consumer regions with a `pipeline_stage` attribute on each, and a tail `future_wait` + `async.wait` synchronises the loop result for the function-level user.

```mlir
// Output: async pipeline scaffold. Producer region issues async copies;
// consumer region runs compute under an mbarrier try-wait.
%tok0 = nv_tileas.create_none : !nv_tileas.token
%out, %tok = scf.for %i = %c0 to %n step %c1
    iter_args(%acc = %init, %t = %tok0)
    -> (tensor<...>, !nv_tileas.token) {

  // Producer region: emits the async TMA issue and a stage-tagged commit.
  %ap, %ta = nv_tileas.async.pipeline.produce_one_async %i,
              { pipeline_stage = 0 : i32 } : !nv_tileas.token
  %bp, %tb = nv_tileas.async.pipeline.produce_one_async %i,
              { pipeline_stage = 0 : i32 } : !nv_tileas.token
  nv_tileas.async.pipeline.producer_commit %ta, %tb : !nv_tileas.token

  // Consumer region: waits for the mbarrier parity flip, reads, computes.
  %a, %tac = nv_tileas.async.pipeline.consume_one %ta { pipeline_stage = 1 : i32 }
  %b, %tbc = nv_tileas.async.pipeline.consume_one %tb { pipeline_stage = 1 : i32 }
  %c = nv_tileas.dot %a, %b, %acc : tensor<...>
  nv_tileas.async.pipeline.consumer_release %tac, %tbc : !nv_tileas.token

  scf.yield %c, %t : tensor<...>, !nv_tileas.token
}
nv_tileas.async.future_wait %tok : !nv_tileas.token
nv_tileas.async.wait %tok : !nv_tileas.token
```

### Attribute Hand-Off

D07 produces three attributes that downstream passes in the family consume. The contract is one-way: D07 writes them once during rewrite, and no later pass touches the schema, only the values.

| Attribute | Producer | Consumer | Meaning |
|---|---|---|---|
| `pipeline_stage` | D07 | D09 MaterializeSchedule, D11 UnspecializedPipeline | integer index naming which pipeline stage this producer or consumer region belongs to; drives modulo-schedule placement |
| `token_iter_idx` | D07 | D09, D11 | position of the token iter-arg in the rewritten loop; lets stage materialisation thread token state through prologue and epilogue |
| `producer_kind` | D07 | D08 MaterializeConvertLayout, D14 AssignLoadStoreLayouts | tag distinguishing TMA bulk, generic async-copy, and synchronous-fallback producers; drives layout selection on the consumer side |

The `pipeline_stage` attribute is what binds D07's region split to the scheduler. D09 reads it during stage materialisation: each stage's `produce_one` and `consume_one` ops must agree on stage index, otherwise the prologue and epilogue peel-piece builders cannot match producer to consumer across iterations. A mismatch trips the alias-check diagnostic ``"Alias is not expected here."`` in D09's helper pipeline.

### Failure Modes

D07 has three structural failure paths, all of which set ``*(self + 40) |= 4`` and let the driver lift the bit after the walk completes:

1. **Two producers for one pipeline.** The post-walk reconciler verifies one `produce_one`-like writer per pipeline across all `AllocationOpInterface` ops. On conflict it emits the verbatim diagnostic ``there are two `produce-one-like` operations using different instructions to generate data into the same pipeline. It's a bug of MaterializeAsync Pass.`` at severity `259`. The conflicting writers usually come from an earlier pass that duplicated a queue producer without updating the alias map.
2. **No TMA-eligible chain.** When the per-loop classifier cannot find a load chain that terminates in a tile compute op, the rewriter leaves the loop synchronous and flags the loop as not-pipelinable. D09 reads the flag and routes the loop through the synchronous fallback. This is recoverable and emits no diagnostic.
3. **Iterator-type disagreement.** If two arms of an `if` or `scf.for` yield iterator values of different types, the post-walk iterator-type propagation fails. The verifier on `nv_tileas.async.pipeline.create_iterator` rejects the merge, and the pass bubbles the failure up through the standard handshake.

### Anonymous Rewrite Patterns

Two anonymous `RewritePattern` instances are allocated through `sub_44A8C20` + `sub_4481530` and registered into the local pattern set. Both are 0x60 B and use the 5-slot vtable shape A documented in [Pattern Vtables and Shapes — Five-Slot RewritePattern Vtable](../../mlir-infra/pattern-vtables-and-shapes.md#five-slot-rewritepattern-vtable): `{matchAndRewrite, anchor/match, getDebugName, nullsub_11937 (slot 3), dtor/clone}`. The debug-name string pair sits at offsets `+0x40` and `+0x48` of each pattern object.

| Pattern | Anchor op | Vtable | Debug-name string |
|---|---|---|---|
| `AsyncWaitOpRemoval` | `nv_tileas.async.wait` | `off_59B4500` | `mlir::nv_tile_ir::as::{anonymous}::AsyncWaitOpRemoval]` |
| `ExtractSliceOpToAsync` | `nv_tileas.extract_slice` | `off_59B4538` | `mlir::nv_tile_ir::as::{anonymous}::ExtractSliceOpToAsync]` |

`AsyncWaitOpRemoval` drops redundant `async.wait` ops that follow another wait on the same token with no intervening async consumer. `ExtractSliceOpToAsync` rewrites synchronous `extract_slice` into its async form whenever the slice source already carries an async token.

### Interface TypeID Caching

Interface lookups intern TypeIDs through `sub_44A6CA0` and cache the resulting `TypeID` pointer in three globals so later loop walks and trait checks skip the interning hash. All three must populate before the pass can claim its `OperationPass<FunctionOpInterface>` anchor and verify the `SymbolTable` trait.

| Interface | Cache slot |
|---|---|
| `FunctionOpInterface` | `qword_5B37670` |
| `SymbolTable` | `qword_5B37798` |
| `LoopLikeOpInterface` | `qword_5B38E18` |

Multiple producer-like ops writing into the same pipeline must agree on the instruction family that generates the data. Mixing incompatible producers is a hard error: the downstream wait and barrier sequence would be ambiguous.

## Materialize Convert Layout

Pipeline boundaries demand layout conversion between register, shared-memory, and tensor-memory views. `TileASMaterializeConvertLayout` (CLI: `tileas-materialize-convert-layout`) decomposes every surviving `nv_tileas.convert_layout` into a sequence of alloc, view, copy, and shuffle ops, picking register-to-register staging or shared-memory staging based on what the target specification reports as feasible for the source and destination layouts.

The pass object is a 752-B (`0x2F0`) PIMPL allocated through `sub_44A8C20(0x2F0)`. Two CLI-visible options sit at fixed offsets inside the body; two callback slots in the pass-object header back them through the standard pass-option registration helper.

| Field | Offset | Type | Default | Meaning |
|---|---|---|---|---|
| `reg2reg-vec-size` | `+0x1D0` | `u32` | 16 | Cap on register-to-register copy atom width |
| `reinterpret-to-i8` | `+0x2A0` | `bool` | 0 | Reinterpret source and destination tensors as `tensor<...xi8>` for sub-byte fp formats so staging happens at byte granularity |

Option-callback slots live at header offsets `+65` and `+91`; the vtable pair is `(off_59B4688, unk_5B38E50)`. Both constructors `sub_8206C0` and `sub_820940` register the two options through `sub_6D3140` (the pass-option registration helper) and run `sub_5FED40` (the pass-init helper) to wire the pass into the global registry.

### Pass Body

The pass body at `sub_820D30` (10 359 B) reads the two options, walks the function bottom-up via `sub_81EA30` to collect every `nv_tileas.convert_layout` op (TypeID `&unk_5B44FD8`), and asks the target-specification driver `sub_91A9B0` for a decomposition plan per op. The plan is a `SmallVector<AtomPlan>` of 32-byte entries:

```c
struct AtomPlan {
    uint32_t tag;             /* 0 = reg-to-reg, 1 = via-SMEM       */
    uint32_t smem_layout;     /* descriptor index when tag == 1     */
    uint32_t atom_layout;     /* per-atom layout descriptor         */
    uint128_t atom_descriptor;/* CuTe-style atom encoding           */
};
```

The option-read sequence for one op is:

```c
LogicalResult materialize_one(ConvertLayoutOp op, PassState *self, Rewriter *rw) {
    uint32_t vec_cap     = *(uint32_t *)((uint8_t *)self + 0x1D0);
    bool     reinterpret = *(bool     *)((uint8_t *)self + 0x2A0);

    SmallVector<AtomPlan> plans;
    if (failed(sub_91A9B0(op, vec_cap, &plans))) {
        sub_446CE00(op.loc(), "failed to query target spec for convert_layout");
        *(uint32_t *)((uint8_t *)self + 40) |= 4;
        return failure();
    }

    sub_8200D0(plans.data(), plans.size());          /* stable sort by efficiency */

    if (reinterpret && is_sub_byte_fp(op.source().type())) {
        op = sub_81F8C0(op, rw);                     /* src  -> tensor<...xi8> */
        op = sub_81F9F0(op, rw);                     /* dst  -> tensor<...xi8> */
    }

    return apply_first_feasible_plan(op, plans, rw);
}
```

The pass-failure handshake matches the rest of the TileAS family: errors set `*(self + 40) |= 4` instead of calling `signalPassFailure()` directly, and the driver inspects the bit after the walk completes. Option-misuse and target-spec lookup failures share the verbatim diagnostic ``failed to query target spec for convert_layout`` via `sub_446CE00`.

### Plan Sort and Apply

`sub_8200D0` sorts candidate plans by descending efficiency — cost-per-byte transferred through the chosen staging shape — so the first plan that clears the per-op constraint set also carries the highest expected throughput. The merge is the libc++ `std::stable_sort` over 32-B entries, recognisable by the same `__buffered_inplace_merge` shape that drives the FUSE arm in the modulo scheduler.

Once sorted, the dispatcher walks the plan vector and accepts the first plan whose tag is feasible for the op's source layout, destination layout, and current vector cap. Tag 0 expands into a sequence of register-to-register `nv_tileas.copy` ops bounded by `reg2reg-vec-size`, with an optional `nv_tileas.shuffle` when the atom needs a cross-lane permutation. Tag 1 stages through shared memory: allocate a `tensor<...,#smem>`, view the source through the plan's source view, copy into SMEM, then read the SMEM tile back at the destination layout.

```c
Value apply_plan(ConvertLayoutOp op, AtomPlan plan, Rewriter *rw) {
    if (plan.tag == 1) {
        Value smem = rw->create("nv_tileas.alloc_tensor", plan.smem_type()).result(0);
        Value view = rw->create("nv_tileas.view", op.source(), plan.source_view()).result(0);
        rw->create("nv_tileas.copy", view, smem, plan.atom_descriptor);
        return rw->create("nv_tileas.convert_layout", smem, op.dst_layout()).result(0);
    }

    if (plan.can_convert_directly()) {
        return rw->create("nv_tileas.convert_layout", op.source(), plan.dst_layout()).result(0);
    }

    return rw->create("nv_tileas.shuffle", op.source(), plan.atom_descriptor).result(0);
}
```

### Reinterpret Builders

With `reinterpret-to-i8` set, the pass rewrites source and destination layouts into byte-granular form before consulting the target-spec driver. The two builders look textually similar but each operates on a different end of the op; keeping them separate enables asymmetric reinterpretation — a bytewise source view paired with a native destination, for instance.

| Builder | Operand | Role |
|---|---|---|
| `sub_81F8C0` | source | rewrites the source tensor type into `tensor<...xi8>` and inserts a matching view |
| `sub_81F9F0` | destination | rewrites the destination tensor type into `tensor<...xi8>` and inserts a matching view |

Byte reinterpretation kicks in for NVFP4, FP6, and FP8 source or destination tensors. The SMEM staging plan then runs over a normal byte-granular tile, sidestepping the otherwise mandatory sub-byte SMEM atoms and letting one SMEM staging path serve every sub-byte fp format.

### Input and Output IR Shapes

The input is IR carrying one or more `nv_tileas.convert_layout` ops between producer and consumer with incompatible layouts. The producer's output layout (typically a `#smem` or `#tmem` layout coming out of a TMA load) and the consumer's input layout (typically a WGMMA fragment layout or a register layout for a downstream `nv_tileas.dot`) do not match the target's native copy-atom catalogue.

```mlir
// Input: a convert_layout op crossing pipeline boundaries.
%a_smem = nv_tileas.async.pipeline.consume_one %ta : tensor<128x64xf16, #smem>
%a_frag = nv_tileas.convert_layout %a_smem : tensor<128x64xf16, #smem>
                                          -> tensor<128x64xf16, #wgmma_a>
%c = nv_tileas.dot %a_frag, %b_frag, %acc : tensor<128x128xf32>
```

After materialisation, the chosen plan expands into either a register-to-register sequence (tag 0) or an SMEM-staging sequence (tag 1). The SMEM staging case allocates a private SMEM tile, copies through it using the plan's atom descriptor, and reads back at the destination layout:

```mlir
// Output (tag 1, SMEM staging): the convert_layout is replaced by an
// alloc + view + copy + read sequence; the atom_plan attribute survives
// onto the final reader op for D14 AssignLoadStoreLayouts.
%tmp   = nv_tileas.alloc_tensor : tensor<128x64xf16, #smem_swizzled>
%vsrc  = nv_tileas.view %a_smem  : tensor<128x64xf16, #smem>
                                -> tensor<128x64xf16, #smem_byte_view>
nv_tileas.copy %vsrc, %tmp { atom_descriptor = #cute<atom "TiledCopy<...>"> }
%a_frag = nv_tileas.convert_layout %tmp
            { atom_plan = #nv_tileas.atom_plan<tag = 1, smem = #smem_swizzled, ...> }
            : tensor<128x64xf16, #smem_swizzled>
           -> tensor<128x64xf16, #wgmma_a>
```

Tag 0 collapses to a chain of `nv_tileas.copy` ops bounded by `reg2reg-vec-size`, optionally fronted by a `nv_tileas.shuffle` when the atom requires a cross-lane permutation.

### Attribute Hand-Off

The `atom_plan` attribute survives onto the final reader op as a fully-resolved AtomPlan record. D14 AssignLoadStoreLayouts is the primary downstream consumer: it reads the plan to bind concrete copy-atom shapes onto the load and store ops the plan expanded into, completing the lowering toward the LLVM/NVVM backend.

| Attribute | Producer | Consumer | Meaning |
|---|---|---|---|
| `atom_plan` | D08 | D14 AssignLoadStoreLayouts | 32-byte AtomPlan record (tag, smem layout, atom layout, atom descriptor); names the concrete copy atom that lowering must instantiate |
| `reinterpret_byte` | D08 | D14, downstream lowering | flag set when the pass rewrote source or destination as `tensor<...xi8>` so that downstream passes do not re-fold the byte view back to the sub-byte layout |

D08 does not invent atom choices; it consults the target-specification driver, sorts plans by descending efficiency, and records the chosen plan onto the op so D14 has a stable handle. D14, in turn, may refine the plan when target-specific constraints emerge (cluster-shared atoms, multi-CTA descriptors) but it must not silently change the plan tag — the verifier rejects tag changes after D08 has run.

### Failure Modes

The two structural failures both emit ``"failed to query target spec for convert_layout"`` and set the failure bit:

1. **Target-spec lookup miss.** When the source and destination layouts do not appear in the target's atom catalogue, `sub_91A9B0` returns an empty plan vector. The dispatcher emits the diagnostic and tears the op down without rewriting it; the verifier on the unchanged op trips at the next pass.
2. **All plans infeasible at current vector cap.** When every returned plan exceeds the `reg2reg-vec-size` cap (typical for sub-byte FP formats without `reinterpret-to-i8` set), no plan passes the feasibility gate. The same diagnostic fires; the recommended fix is to raise the cap or enable `reinterpret-to-i8`.

### Failure Handling and Cross-References

Both option-misuse cases and target-spec lookup failures share the verbatim diagnostic above. Pass-level failure sets `*(self + 40) |= 4`. Successful expansion replaces the original `nv_tileas.convert_layout` with the plan's final result value and erases the op.

The SM-specific atom catalogues that `sub_91A9B0` reads to build plans are documented in [MMA Atoms SM70..SM120 — Per-Arch MMA Shape Lattice](../../dialects/cute_nvgpu/mma-atoms-sm70-120.md#per-arch-mma-shape-lattice). The 8-slot pattern vtable convention that `off_59B4688` uses is documented in [Pattern Vtables and Shapes — Eight-Slot Vtable](../../mlir-infra/pattern-vtables-and-shapes.md#eight-slot-vtable). The `nv_tileas.convert_layout` op definition itself, including its layout-attribute schema and verifier, is documented in [nv_tileas Op Roster and Builders](../../dialects/nv_tileas/op-roster-and-builders.md).

## Materialize Schedule

`TileASMaterializeSchedule` (CLI: `tileas-materialize-schedule`) consumes a `ScheduleAnalysis` and dispatches to one of two driver flavours: AUS (Agent-Unspecialized — one SIMT agent owns producer and compute work) or AWS (Agent-Warp-Specialized — distinct producer and consumer agents partitioned by `nv_tileas.async.pipeline.agent_switch`). CLI options and a heuristic over the schedule's work-vs-stage shape gate the choice; the pass invents no schedule, it materialises an existing one onto the function.

| Mode | Meaning |
|---|---|
| AUS | single-agent materialization; all stages share the same warp group |
| AWS | warp-specialized materialization with one or two compute agents and one producer agent |

The pass identity triple is `sub_8235B0` / `sub_8235C0` / `sub_8235D0`. The name slot returns the literal `"MaterializeSchedule"`; the description slot returns ``"Meterialize the pipeline schedule to generate warp-specialized or unspecialized IR"`` verbatim — the leading typo `Meterialize` lives in the binary and must survive bit-for-bit in tool output. The factory `sub_825050` takes a 3-byte packed option mask whose bits feed the offsets listed below. Dependent dialect registration runs through `sub_8235E0`, which inserts `nv_tileaa`, `nv_tileas`, and `scf` into the dependency set.

### Pass Object and CLI Options

The pass body is a 960-B (`0x3C0`) PIMPL allocated through `sub_44A8C20(0x3C0)`. Three boolean CLI-visible options sit at fixed offsets inside the body, mirroring the option layout in `TileASMaterializeConvertLayout`.

| Field | Offset | Default | Meaning |
|---|---|---|---|
| `use-AUS` | `+464` | `false` | forces the AUS driver; bypasses the dual-SIMT heuristic entirely |
| `use-dual-simt` | `+672` | `true` | AWS-only: splits compute into two SIMT agents of 4 warps each when feasible |
| `enable-schedule-rewrite` | `+880` | `true` | gates `sub_8D6700`'s re-folding of expanded stages back onto the original `scf.for` |

Each option threads through the standard pass-option apply thunk at offset `+728`, the same indirect-call shape used elsewhere in the family. The thunk receives the address of the option storage (`a1 + 704` for the dual-SIMT triple), so heuristic updates flow back into the option store without bypassing CLI parsing.

### Dispatcher Body

The dispatcher at `sub_824000` (4 175 B, 133 BB) opens by resolving the surrounding `FunctionOpInterface` through the `mlir::FunctionOpInterface]` interned TypeID cached in `qword_5B37670`, falling back to a sorted binary search over the operation-info trait table when the host op stores its interfaces in the secondary array form — the same dual lookup every other TileAS pass uses. Errors set ``*(self + 40) |= 4`` and the driver inspects the bit after the walk completes.

With the function handle resolved, the dispatcher loads the cached `ScheduleAnalysis` from the AnalysisManager DenseMap. Its key is `"mlir::nv_tile_ir::as::schedule_utils::ScheduleAnalysis]"` (54 chars, trailing `]` preserved), interned through `sub_44A6CA0` and cached at `qword_5B38E78`. The probe is the canonical Tileiras `(h>>9) ^ (h>>4) & (cap-1)` pattern with linear-step rehashing; tombstone `-4096` aborts the search. Two loader shims sit behind the probe: `sub_8FDE40` is the entry point and forwards to `sub_8FCC10` when an analysis was found in the map, or `sub_8FD850` when it must be created from defaults. On failure the dispatcher sets the failure bit and falls through to the cleanup tail without allocating a driver.

```c
LogicalResult materialize_schedule(FuncOp func, PassState *self) {
    Operation       *op       = func.getOperation();
    FunctionOpInterface fi    = lookup_interface(op, qword_5B37670);
    ScheduleAnalysis *sched   = analysis_manager_lookup(
        self->parent, qword_5B38E78,
        /* h>>9 ^ h>>4 probe */ &sub_8FCC10, &sub_8FD850);

    if (!sub_8FDE40(/* slot */, sched, /* present */)) {
        *(uint64_t *)((uint8_t *)self + 40) |= 4;
        return failure();
    }
    ...
}
```

### Driver Allocation

The dispatcher picks one of two driver flavours based on `use-AUS` and the dual-SIMT heuristic, then allocates the driver, invokes its `prepare()` slot, and finally runs the shared materialisation pipeline.

| Driver | Size | Vtable | Extra state |
|---|---|---|---|
| AUS | `0x68` B | `&unk_59DBBE8` | three `SmallVector<Op*>` slots for stages, allocations, and tokens |
| AWS | `0xC8` B | `&unk_59DBBA8` | agent-partition map at `+96`, `numWarps` at `+112`, `warpId` at `+128`, `useDualSimt` byte at `+192`, shape sentinel `0x600000000` at `+200` |

The dispatcher picks AUS whenever `use-AUS` is true, or when the dual-SIMT heuristic doesn't pay off. The heuristic is one floating-point compare: ``useDualSimt = (double)N > (totalWork / iters) * 0.6``, with `N`, `totalWork`, and `iters` read from three schedule-header fields at `*(v23 + 5)`, `*(v23 + 4)`, and `*(v23 + 3)`. The integer division pre-clamps `iters` to 1 if non-positive — `idiv` would otherwise fault on the header's signed-zero case. The computed bit lands at `*(self + 672)` and re-applies through the option thunk, so the option store reflects the final decision, not just the parsed CLI default.

```c
if (*((uint8_t *)self + 464)) {                       /* use-AUS */
    driver = alloc(0x68);
    driver->vtable = &unk_59DBBE8;                    /* AUS */
} else {
    if (analysis_present) {
        ScheduleHeader *h = (ScheduleHeader *)(slot + 8);
        int iters       = h->iters > 0 ? h->iters : 1;
        int total_work  = h->total_work;
        double n_double = (double)h->stage_count;
        bool useDual    = n_double > (double)(total_work / iters) * 0.6;

        *((uint8_t *)self + 672) = useDual;
        (*(self->option_apply))(self + 704, &useDual, /* arg */);
    }

    driver = alloc(0xC8);
    driver->vtable                = &unk_59DBBA8;     /* AWS */
    *((uint8_t *)driver + 192)    = *(uint8_t *)((uint8_t *)self + 672);
    *((uint64_t *)driver + 13)    = 0x600000000ULL;   /* shape sentinel */
}
```

The shape sentinel `0x600000000` (stamped into the dispatcher's local frame at slot `v147` and again at `v150`) encodes a default `(numStages=6, stageWidth=0)` pair that the AWS prepare slot overwrites with the real schedule header. With no schedule analysis present, the dispatcher skips the heuristic block entirely, allocates the AWS object with `useDualSimt = 0`, and leaves the fail/succeed decision to `prepare()`.

### Prepare and Materialisation Pipeline

The driver then receives its `prepare()` call through ``(*driver->vtable[0])(driver)``. AUS and AWS share the prepare slot offset; the vtable dispatch picks the right body. On failure the dispatcher sets the failure bit and invokes the destructor through ``(*driver->vtable[5])(driver)`` (offset `+40`, the standard 8-slot Tileiras driver dtor slot).

Once `prepare()` succeeds, control passes into the shared materialisation pipeline. The entry helper `sub_8F1AA0` (248 B) sequences six fixed-order passes plus the alias-materialisation pass:

| Stage | Helper | Notes |
|---|---|---|
| 1 | `sub_8E4510` | producer-region setup |
| 2 | `sub_8E2790` | alias check; emits ``"Alias is not expected here."`` on contract violation |
| 3 | `sub_8E2F00` | consumer-region setup |
| 4 | `sub_8F19D0` | iterator threading |
| 5 | `sub_8EC560` | release-op insertion |
| 6 | `sub_8E1900` | barrier-token wiring |
| 7 | `sub_8E4F10` | 10 430-B alias-materialisation pass; the heaviest body in the sequence |

The alias-check diagnostic is severity-`259` (`0x103`) like the rest of the family. It fires when an earlier pass leaves a pipeline-aliased value reaching schedule materialisation, which would corrupt the producer/consumer ownership graph. The error is fatal: it sets the failure bit and tears the driver down.

On the AWS path only, the dispatcher next calls the agent-switch materialiser `sub_9130B0` (4 047 B, 114 BB). For each agent boundary detected in the schedule, it emits one `nv_tileas.async.pipeline.agent_switch` op whose payload encodes the agent id, the warp count partition, and the resource window. The same body carries the `"Building op `"` ... ``"` but it isn't known in this MLIRContext: the dialect may no"`` diagnostic pair used by every generic op builder in the dialect.

### Stage Materialisation

The expanded stage IR then re-folds onto the loop. `sub_90C600` (85 B, single basic block) is the entry point: it prepares the per-stage SmallVector frames and forwards into the heavy `sub_8D6700` (10 399 B, 506 BB). That body walks the schedule's stage list, builds an `scf.if` guard per pipeline-stage prelude through `sub_8CE1B0`, and constructs the big-tensor MLIR ops through the 13 858-B `sub_8D30D0` nest. Each per-stage construction allocates 64-B-strided records into the driver's stage SmallVector; tombstone slots tagged `-4096` or `-8192` let the cleanup loop skip them without dereferencing freed payloads.

When `enable-schedule-rewrite` is `false`, the stage builder still expands stages but skips the final re-fold over the original `scf.for`, leaving the expanded form for downstream passes to consume directly. Debugging dumps take this path, as does the AUS driver when the heuristic prefers a non-pipelined fallback.

Epilogue handling at `sub_8F1F40` (918 B, 62 BB) picks off consumer-side release ops that survived the stage rewrite. It walks the post-loop region, finds `consumer_release` ops whose pipeline argument escapes the rewritten loop, and re-anchors them onto the AWS agent boundary or the AUS post-loop sequence depending on the active driver. The same helper carries the `LABEL_86` cleanup tail in the dispatcher: epilogue failure falls through into the SmallVector teardown the success path uses.

### Schedule Cleanup

`sub_823B60` (1 183 B, 59 BB) is the schedule-state destructor. It frees eight 24-strided per-stage SmallVectors (producer, consumer, and intermediate slot arrays) plus two 48-strided SmallVectors at `+216` and `+240` (the alias-materialisation work-lists). DenseMap rows whose first qword equals `-4096` (empty) or `-8192` (tombstone) are skipped; live rows release their inner SmallVector payloads (`*(row + 40)`, `*(row + 16)`) through the standard 16-stride deallocator `sub_4560420`. The dispatcher calls `sub_823B60` once on success and once on failure, sharing one cleanup tail to keep the failure handshake symmetric with success.

### Strategy Routing

The dispatcher reads its strategy enum (NONE / UNSPECIALIZED / WARP_SPECIALIZED) through `sub_6D3460`. The enum drives the top-level pass-manager: UNSPECIALIZED routes to the [TileASUnspecializedPipeline](#unspecialized-pipeline) pass below, while WARP_SPECIALIZED stays inside `TileASMaterializeSchedule` with `use-AUS=false`. NONE short-circuits both — the dispatcher tears the driver down immediately and returns success without emitting any pipeline IR, leaving the loop synchronous for downstream NVVM lowering.

### Scheduler Hand-Off

The schedule analysis itself does not live in this pass. The modulo scheduler computes II, places ops modulo II, and emits a `ScheduleAnalysis` record into the AnalysisManager; D09 is the consumer side of that split. The boundary between analysis and materialisation is documented in [Schedule Solve and Cost Evaluators — Pass Boundary](../../scheduler/schedule-solve-and-cost-evaluators.md#pass-boundary): the scheduler is forbidden from touching IR directly, and D09 is forbidden from inventing schedules. Every field D09 reads — stage count, total work, iteration count, per-op stage tag, per-op iteration offset — was written by the scheduler. The dispatcher's role is to translate that record into producer and consumer regions, agent boundaries, and peel-piece copies of the loop body.

The strategy enum returned by `sub_6D3460` is what binds D09 to the scheduler's chosen strategy:

| Strategy | Meaning | D09 path |
|---|---|---|
| `NONE` | scheduler found no profitable pipeline | tear down driver, return success, leave loop synchronous |
| `SERIAL` | one-stage serial schedule (`II == latency`) | AUS driver with single-stage degenerate path; no peeling |
| `COST_BASED` | cost-evaluated multi-stage schedule | AUS or AWS driver per CLI options and dual-SIMT heuristic |
| `FAST` | first-feasible-II schedule | same driver path as `COST_BASED`; only the schedule values differ |
| `DEFAULT` | platform default for current SM | resolves to `COST_BASED` on Blackwell, `SERIAL` on pre-Hopper |

The strategy enum does not change D09's algorithm — it only selects which `ScheduleAnalysis` record was published into the AnalysisManager. The dispatcher reads whichever record is present; the strategy tag travels with the record for diagnostic purposes.

### Dual-SIMT FP Heuristic

The dual-SIMT heuristic ``(double)N > (totalWork / iters) * 0.6`` is one floating-point compare, but its three inputs encode a specific shape question: does the schedule have more pipeline stages than work-per-iteration, scaled by a 0.6 efficiency floor? When the answer is yes, splitting the compute warp group into two 4-warp SIMT agents keeps both agents busy; the producer agent issues TMAs for both consumers in parallel, and the second consumer hides instruction-issue latency on the first.

The heuristic fires only under a specific shape combination: an FP-heavy MMA body (where the per-iteration work is dominated by tensor-core throughput, not memory bandwidth) and an SM with dual-issue capability (Hopper, Blackwell, Blackwell Ultra). For integer-dominated or memory-dominated loops the heuristic typically fails the 0.6 threshold and falls back to single-SIMT. The floating-point compare is intentional: integer division would round the work-per-iteration ratio at every iteration count, hiding the difference between balanced and imbalanced shapes.

### Peel-Piece Emission

After `prepare()` succeeds, the stage materialisation pipeline emits the modulo schedule's overlapping iterations as explicit IR. The schedule's stage list becomes a sequence of peel-piece copies: a prologue that fills the pipeline before the steady-state body, a steady-state body that runs one iteration per pipeline stage, and an epilogue that drains the pipeline after the loop exits.

```c
void emit_peel_pieces(Schedule *sched, ScfForOp loop, Rewriter *rw) {
    uint32_t num_stages = sched->stage_count;

    /* Prologue: stages 0 .. num_stages-2 of iteration 0,
       stages 0 .. num_stages-3 of iteration 1, and so on.
       At the end, the pipeline has one in-flight iteration per stage. */
    for (uint32_t k = 0; k < num_stages - 1; ++k) {
        emit_stage_peel(sched, k, /*iter=*/0, rw);
    }

    /* Steady state: rebuild the scf.for body so each iteration
       carries one stage-k op for k in 0..num_stages-1. */
    ScfForOp rebuilt = rebuild_with_overlapped_stages(loop, sched, rw);

    /* Epilogue: drain the pipeline. After loop exit there are num_stages-1
       in-flight iterations; emit consume-only copies that finish them. */
    for (uint32_t k = 0; k < num_stages - 1; ++k) {
        emit_stage_drain(sched, k, /*iter=*/N - k, rw);
    }
}
```

Each peel-piece copy reuses the stage-mapped clone helper `sub_8307E0`, which reissues each op with operands rewritten to the corresponding stage's value mapping. The rebuilt loop's trip count is `N - (num_stages - 1)`, matching the prologue's pre-execution of the first `num_stages - 1` iterations. When `enable-schedule-rewrite` is false, the pass emits prologue and epilogue but skips the steady-state re-fold; the unrolled stage IR is left in place for debugging or for downstream passes that expect the expanded form.

### Failure Modes

D09 has four structural failure paths, all of which set ``*(self + 40) |= 4``:

1. **No `ScheduleAnalysis` in the AnalysisManager.** The analysis loader returns `false`; the dispatcher tears down the driver and bubbles failure up. This indicates the scheduler pass never ran or failed silently — usually a pass-pipeline ordering bug.
2. **`prepare()` rejection.** When the driver's `prepare()` slot fails (mismatch between schedule header and function shape, or AWS agent partition impossible), the dispatcher calls the driver's destructor and bubbles failure up.
3. **Alias-check trip.** The helper pipeline's stage-2 alias check emits ``"Alias is not expected here."`` at severity `259` when an earlier pass left a pipeline-aliased value reaching schedule materialisation. The error is fatal; the driver tears down.
4. **`agent_switch` emission failure (AWS path only).** When the agent-switch materialiser cannot find a valid warp-count partition for the SM, the AWS path fails and the dispatcher falls back to the synchronous path. The fallback is silent — no diagnostic — because the schedule analysis is intact and the loop can still execute correctly without warp specialisation.

## Unspecialized Pipeline

`TileASUnspecializedPipeline` (CLI: `tileas-unspecialized-pipeline`) software-pipelines loops in the single-agent AUS flow. It peels a prologue, builds the steady-state body, and emits an epilogue drain. A two-stage pipeline takes a simpler shape; three or more stages introduce a repeating middle stage. The pass runs after D09 has chosen AUS over AWS and never fires on warp-specialized functions — AWS materialization owns its own pipelining and partitions the function into producer/consumer agents long before this pass would see it.

```c
LogicalResult pipeline_unspecialized_loop(ScfForOp loop, uint32_t num_stages) {
    if (num_stages <= 1) {
        return success();
    }

    ScheduleMap map = extract_schedule_map(loop);
    if (!has_valid_pipeline_schedule(map)) {
        return failure();
    }

    SmallVector<Operation *> prologue = build_prologue(loop, map, num_stages);
    ScfForOp body = rebuild_body_loop(loop, map, num_stages);
    SmallVector<Operation *> epilogue = build_epilogue(loop, map, num_stages);

    splice_pipeline_pieces(loop, prologue, body, epilogue);
    return success();
}
```

The pass identity triple is `sub_826530` / `sub_826540` / `sub_826550`. The option `num-stages` sits at `pass + 464` (`u32`, default 2); the driver-level switch `unspecialized-pipeline-num-stages` from `sub_6D3460` overrides it. The pass early-exits when `numStages <= 1` — the schedule expander has nothing to peel.

### Pass Body

The pass body at `sub_8337F0` (9 774 B, 290 BB) walks the top-level region with `sub_827610` plus the callback `sub_827000`, collecting candidate `scf.for` and `scf.while` loops. Each candidate runs through a two-stage legality vtable `v239 = {sub_8274D0, sub_826C50}` (hasPipelinableOps + hasValidSchedule). Loops failing either gate pass through unchanged, honoring the schedule-map contract earlier scheduling passes published.

```c
LogicalResult run_unspecialized_pipeline(FuncOp func, PassState *self) {
    uint32_t num_stages = *(uint32_t *)((uint8_t *)self + 464);
    if (num_stages <= 1) {
        return success();
    }

    SmallVector<LoopLikeOp> candidates;
    sub_827610(func, &candidates, sub_827000);                /* region walk */

    for (LoopLikeOp loop : candidates) {
        if (!sub_8274D0(loop) || !sub_826C50(loop)) {         /* legality vtable */
            continue;                                          /* leave loop bit-for-bit unchanged */
        }
        if (failed(expand_loop_schedule(loop, num_stages))) {
            sub_446CE00(loop.loc(), "Failed to pipeline loop", /*severity=Remark=*/3);
            *(uint32_t *)((uint8_t *)self + 40) |= 4;
        }
    }

    return success();
}
```

On the failure remark, the loop stays bit-for-bit unchanged and `*(self+40) |= 4` flags the recoverable miss so downstream passes can react. The verbatim diagnostic `"Failed to pipeline loop"` (23 chars) fires at `0x834F26` with `LODWORD(severity) = 3` (Remark). D13 OptimizePipelineRegion is the primary consumer of that bit — it checks bit 2 of the same word to skip un-pipelined loops rather than chase regions that were never materialized.

### LoopScheduleExpander

The schedule expander at `sub_82CC30` is `LoopScheduleExpander::expand` (10 341 B, 505 BB). It extracts a `ScheduleMap` consisting of a 0x28-B header followed by 16-B `StageEntry {Operation*, i32 stage, i32 iterOffset}` records. The map itself is an open-addressed DenseMap with 72-B slots — the same shape as `DenseMap<Operation*, SmallVector<Value, 4>>` used elsewhere in the schedule layer — with sentinels `-4096` and `-8192` and identity-pointer hashing. Stage and iter-offset values come off operations through inherent-attribute classIDs: stage-attr classID at `&unk_5B44F90`, iteration-offset classID at `&unk_5B44ED0`.

```c
struct StageEntry {                  /* 16 B */
    Operation *op;
    int32_t    stage;
    int32_t    iter_offset;
};

struct ScheduleMap {                 /* header 0x28 B + 72-B slots */
    uint8_t  header[0x28];
    Slot    *slots;                  /* tombstone keys: -4096, -8192 */
};
```

### Peel-Piece Builders

`LoopScheduleExpander::expand` invokes three peel-piece builders in a fixed order. The interior-stage selector is `v227 = 2 * (numStages != 2)`: at `numStages == 2` it collapses to a single-copy prologue and single-copy drain with no interior stage; for three or more stages it expands stage 1 into the repeating middle.

| Builder | Stage | Role |
|---|---|---|
| `sub_82F650` | prologue | emits the lead-in iterations that fill the pipeline before the steady-state body |
| `sub_829440(stage=0)` | stage-0 body | emits the first repeating slice of the steady-state body |
| `sub_829440(stage=1)` | stage-1 body | emits the second slice; for `numStages >= 3` this slice becomes the repeating middle |
| `sub_827E10` | rewrite | rebuilds the original `scf.for` with adjusted trip count `N - (numStages - 1)` |

After `sub_827E10` produces the rebuilt loop, `sub_82CC30` runs a second time on the new body to rebuild the `ScheduleMap` against the fresh operations. The 9-argument rewrite driver `sub_82BB80` `(induction var, new loop op, prologue ops + count, stage map, stage0 body, stage context, epilogue ops + count)` splices prologue, body, and epilogue into place. Per-op SSA wiring goes through `sub_8307E0`, the stage-mapped clone helper that reissues each op with operands rewritten to the corresponding stage's value mapping. The schedule remapper `sub_82A860` updates per-op stage tags so the rebuilt body still matches the published schedule.

### Failure Handling

Failure leaves the original loop bit-for-bit unchanged and tags the pass result so later pipeline-region optimization skips it. The flag bit at `*(self + 40) |= 4` is the only signal the downstream pipeline reads; the pass itself returns `success()` because a missed pipelining opportunity is recoverable, not a hard verifier error. D13 OptimizePipelineRegion is the primary downstream consumer and checks bit 2 to skip un-pipelined loops.

### Why "Unspecialized"

The name distinguishes this pass from the warp-specialized pipeliner inside D09 MaterializeSchedule. Both produce software-pipelined loops, but they target different execution models:

| Pass | Execution model | When it runs |
|---|---|---|
| D09 AWS driver | warp-specialized: one or two compute agents plus a separate producer agent partitioned by `agent_switch` | scheduler chose `WARP_SPECIALIZED` strategy and `use-AUS` is false |
| D11 UnspecializedPipeline | single-agent: producer and consumer share one warp group; pipelining happens through token iter-args and stage tags | scheduler chose `UNSPECIALIZED` strategy, or D09 ran with `use-AUS=true` |

D11 never fires on warp-specialized functions. Its filter callback checks the function for the presence of `nv_tileas.async.pipeline.agent_switch` ops and skips any function that carries them — AWS materialisation owns its own pipelining and would corrupt the agent boundaries if D11 re-pipelined on top.

### IR Before and After Expansion

The input is a function carrying `nv_tileas.async.pipeline.*` ops whose stage and iteration-offset attributes were placed by the scheduler but whose loop body has not yet been peeled. The output is the same function with prologue, body, and epilogue copies of the body fused into the surrounding region.

```mlir
// Input: a stage-tagged but un-peeled scf.for. Each pipeline op carries
// stage = 0..num_stages-1; iter_offset names how far back this op should run.
%out = scf.for %i = %c0 to %n step %c1 iter_args(%t = %tok0) -> !nv_tileas.token {
  %ap, %ta = nv_tileas.async.pipeline.produce_one_async %i
              { stage = 0 : i32, iter_offset = 0 : i32 } : !nv_tileas.token
  %a, %tac = nv_tileas.async.pipeline.consume_one %ta
              { stage = 2 : i32, iter_offset = -2 : i32 } : !nv_tileas.token
  ...
  scf.yield %t : !nv_tileas.token
}
```

After expansion the producer side leads the consumer side by `num_stages - 1` iterations in the prologue, the steady-state body runs one stage of each pipeline phase per iteration, and the epilogue drains the producer-side state:

```mlir
// Output (num_stages = 3, simplified). Prologue fires two producer
// iterations before the steady-state body opens.
%ap0, %ta0 = nv_tileas.async.pipeline.produce_one_async %c0
              { stage = 0 } : !nv_tileas.token
%ap1, %ta1 = nv_tileas.async.pipeline.produce_one_async %c1
              { stage = 0 } : !nv_tileas.token

%out = scf.for %i = %c0 to %n_minus_2 step %c1
    iter_args(%t = %ta1) -> !nv_tileas.token {
  // Steady state: one producer and one consumer per iteration, staggered.
  %a, %tac = nv_tileas.async.pipeline.consume_one %t
              { stage = 2 } : !nv_tileas.token
  %ap, %ta = nv_tileas.async.pipeline.produce_one_async (%i + %c2)
              { stage = 0 } : !nv_tileas.token
  scf.yield %ta : !nv_tileas.token
}

// Epilogue: drain remaining in-flight iterations.
%a_drain0, %td0 = nv_tileas.async.pipeline.consume_one %out
                    { stage = 2 } : !nv_tileas.token
%a_drain1, %td1 = nv_tileas.async.pipeline.consume_one %td0
                    { stage = 2 } : !nv_tileas.token
```

### Peel-Piece Builder Sequence

The three peel-piece builders run in the fixed order prologue → body → epilogue, and the interior-stage selector `v227 = 2 * (numStages != 2)` controls how stage 1 is treated. For `numStages == 2` the selector evaluates to zero: the prologue emits one producer-only iteration, the body alternates producer and consumer once per iteration, and the epilogue emits one consumer-only iteration. For `numStages >= 3` the selector evaluates to two: stage 1 becomes the repeating middle, and the steady-state body fires one op from each of stages 0, 1, and 2 per iteration.

```c
LogicalResult expand_pipeline(ScfForOp loop, uint32_t num_stages) {
    ScheduleMap map = extract_schedule_map(loop);
    if (!has_valid_pipeline_schedule(map)) {
        return failure();          /* sister legality vtable rejected */
    }

    /* Prologue: emit num_stages - 1 lead-in iterations. */
    SmallVector<Operation *> prologue;
    for (uint32_t k = 0; k < num_stages - 1; ++k) {
        emit_stage_peel(&prologue, map, /*iter=*/k, /*stages_through=*/k);
    }

    /* Body: rebuild with overlapped stages. The interior-stage selector
       collapses stage 1 to a no-op when num_stages == 2. */
    uint32_t interior = 2 * (num_stages != 2);
    ScfForOp body = rebuild_body_loop(loop, map, num_stages, interior);

    /* Epilogue: drain in-flight iterations in stage order. */
    SmallVector<Operation *> epilogue;
    for (uint32_t k = 0; k < num_stages - 1; ++k) {
        emit_stage_drain(&epilogue, map, /*iter=*/N - num_stages + 1 + k,
                         /*stages_from=*/k + 1);
    }

    splice_pipeline_pieces(loop, prologue, body, epilogue);
    return success();
}
```

The body builder rebuilds the loop with the adjusted trip count `N - (num_stages - 1)`. The schedule remapper `sub_82A860` then walks the rebuilt body and updates per-op stage tags to point at the freshly cloned ops, so the post-rebuild `ScheduleMap` matches the published schedule.

## Optimize Pipeline Region

`TileASOptimizePipelineRegion` (CLI: `tileas-optimize-pipeline-region`) shrinks every `nv_tileas.async.pipeline.produce_one` and `consume_one` region to the minimal backward slice of the ops actually feeding the region's yielded values. It runs immediately after `TileASUnspecializedPipeline` (D11) and reads D11's pass-result bit so it skips loops D11 left synchronous.

The pass identity triple is `sub_83BAE0` / `sub_83BAF0` / `sub_83BB00`. The description string is the verbatim ``"Optimize the region scope of tileas.async.pipeline.produce_one/consume_one ops"`` (no leading typo, no trailing punctuation). The pass exposes no CLI options; behaviour is deterministic given the input IR and the D11 bit.

### Pass Body

The pass body at `sub_840EF0` (2 657 B, 104 BB) is a thin region-walk driver. It collects every `produce_one` and `consume_one` op in the function into a `SmallVector<Operation*, 48>` and iterates that vector back-to-front, calling the region shrinker on each candidate. The walk dispatches through `sub_83C190` (the standard Tileiras region-walk driver) with `sub_83C100` as the per-op filter callback; the filter classifies each visited op by reading its `OperationName*` slot at `op+48` and matching it against two interned pointers.

```c
bool filter_pipeline_region_ops(Operation *op, void *bucket) {
    const void *opname = *(const void **)((uint8_t *)op + 48);

    if (opname == &unk_5BE6138) {
        return false;                                  /* unregistered sentinel: skip */
    }
    if (opname == &unk_5B44F70 ||                      /* consume_one */
        opname == &unk_5B44F38) {                      /* produce_one */
        smallvector_push_back((SmallVector *)bucket, op);
    }
    return true;
}
```

The sentinel `&unk_5BE6138` guards against unregistered op shells that share storage with registered dialect ops but must not be visited. After the walk, the driver inspects D11's failure-remark bit and walks the bucket back-to-front:

```c
void run_optimize_pipeline_region(FuncOp func, PassState *self, PassState *d11) {
    if ((*(uint32_t *)((uint8_t *)d11 + 40)) & 4) {    /* D11 "Failed to pipeline loop" remark */
        /* schedule expander never materialised; nothing to shrink */
    }

    SmallVector<Operation *, 48> ops;
    sub_83C190(func, &ops, &filter_pipeline_region_ops);

    Operation **end = ops.end();
    for (Operation **cur = end; cur != ops.begin();) {
        cur -= 1;                                      /* v2 -= 8 in the binary */
        sub_83E1B0(*cur, self);                        /* region shrinker */
    }
}
```

Back-to-front iteration is contract, not preference. The region walk pushes ops in source order, but the shrinker rewrites the region in place by creating a fresh op next to the original and moving slice ops into the new region; processing siblings last-first keeps every earlier op's defining-op chain stable until the shrinker reaches it. Front-to-back walking would invalidate later vector entries the first time a slice contains an unvisited sibling.

### D11 Bit-2 Gate

D11's pass state at `*(d11_state + 40)` is the failure-handshake word every TileAS pass shares; bit 2 (`0x4`) is the recoverable ``"Failed to pipeline loop"`` remark emitted at `0x834F26`. D13 reads it through the standard pass-result lookup and skips the shrinker on functions whose loops D11 refused to pipeline. The reasoning is direct: when D11 leaves a loop synchronous, its `produce_one` / `consume_one` regions were never materialised and have no surplus ops to remove. Touching them would still be safe, but the region walker would find no candidates and the shrinker would never fire.

### Three Structural Boundary Checks

The shrinker's correctness rests on three predicates evaluated for every defining op the backward slice walker reaches. Each predicate names a different reason an op must not be pulled into the new region body, and the walker terminates on the first predicate that fires.

| Predicate | Boundary it enforces | Why it terminates the walk |
|---|---|---|
| `getDefiningOp() == nullptr` | the value is a block argument (region-external) | the producer lives outside the region; merging it would change SSA scoping |
| `def->parentRegion() != parent` | the value is region-external — typically a loop iter-arg or surrounding `scf.for` live-in | pulling a region-external op into the new region would extend its lifetime past its original scope |
| `def->name() == &unk_5B44F38` | the defining op is a sibling `produce_one` | pipeline ownership crosses producer/consumer pairs through tokens, not SSA edges; merging a producer into a consumer slice would erase the ownership boundary |

The order of the checks matters: nullptr-first avoids dereferencing a null `Operation*` in the second predicate's region lookup, and the region check ahead of the op-name check skips the `OperationName*` interning probe for the common region-external case. Broadening the set (for instance, stopping at any `nv_tileas.async.pipeline.*` op) over-shrinks consumer regions that legitimately read through pipeline view ops; narrowing it (for instance, dropping the region-parent check) pulls live-in values into the new region and loses them at the next verifier pass.

### Region Shrinker

The region shrinker at `sub_83E1B0` (11 571 B, 512 BB) is the heavy body. For each candidate op it computes the minimal backward slice of the op's yielded values, allocates a fresh op with an empty region, builds a `nv_tileas.async.pipeline.yield` terminator, moves every slice op into the new region in source order, and erases the original. The slice walker tracks visited ops in a DenseSet keyed by `Operation*` using the verbatim LLVM `DenseMapInfo<const void*>::getHashValue` constants — CityHash multiplier `0x9DDFEA08EB382D69` and seed `0xAE502812AA7333` — sized from 64 buckets and grown at the standard load factor `4 * (size + 1) >= 3 * num_buckets`. The CityHash constants serve double duty: they identify the DenseSet as a region-identity cache so repeat invocations on the same region hash to the same bucket, and they keep the dedup probe collision-free across the typical pipeline region size of 50-200 ops.

```c
LogicalResult shrink_consume_one(Operation *op, Rewriter *rw) {
    if ((op->flags & 0x7FFFFF) != 0) {                 /* malformed op */
        llvm::report_fatal_error(/* trap */);
    }

    Region *parent = op->parentRegion();
    DenseSet<Operation *> slice;                       /* CityHash 0x9DDFEA08EB382D69 / seed 0xAE502812AA7333 */
    Worklist work(op->getResults());                   /* seeded with yielded values */

    while (!work.empty()) {
        Value v   = work.pop();
        Operation *def = v.getDefiningOp();

        if (def == nullptr) {
            continue;                                  /* block argument: external boundary */
        }
        if (def->parentRegion() != parent) {
            continue;                                  /* outside the current region */
        }
        if (def->name() == &unk_5B44F38) {
            continue;                                  /* produce_one: cross-pipeline boundary */
        }
        if (slice.insert(def)) {
            work.push(def->getOperands());
        }
    }

    Operation *fresh = rw->create(                     /* see line 1927 fatal-error branch */
        "nv_tileas.async.pipeline.consume_one", op->getResultTypes(), op->getOperands());
    Region *body = sub_43FCA60(fresh);                 /* allocate region */
    rw->create_in(body, "nv_tileas.async.pipeline.yield", yielded_values);  /* line 2177, length 30 */

    sub_448E010(slice, body, &sub_83BC00);             /* moveInto with per-op callback */
    sub_446E1E0(op);                                   /* eraseOp */
    return success();
}
```

The three boundary checks in the pseudocode mirror the table in [Three Structural Boundary Checks](#three-structural-boundary-checks). The verbatim op name ``"nv_tileas.async.pipeline.consume_one"`` is the string passed to the `OperationName` lookup when the fresh op is built; the terminator name ``"nv_tileas.async.pipeline.yield"`` (length 30) is the second registered name the shrinker emits.

The op-flag sanity check `(op->flags & 0x7FFFFF) == 0` traps on malformed ops whose 23-bit op-properties word is non-zero. Tileiras's pipeline region ops carry their properties on the region body, not on the wrapper op, so a non-zero properties word means an earlier pass broke the contract. The trap is intentional: continuing would silently lose the properties.

### Transitive Operand Closure

The slice walker reaches every region-internal defining op by chasing operands transitively. The closure helper at `sub_83CB40` (2 306 B) builds an op's transitive operand set into a fresh DenseSet, stopping at the same three boundaries (producer-op name, region change, block argument). It runs from `sub_83DAB0`, the per-operand closure walker the shrinker uses to expand the worklist one yielded value at a time without materialising the full operand DAG in memory.

```c
void transitive_operand_closure(Operation *root, Region *region, DenseSet<Operation *> *out) {
    Worklist work(root->getOperands());

    while (!work.empty()) {
        Value v = work.pop();
        Operation *def = v.getDefiningOp();

        if (def == nullptr || def->parentRegion() != region || def->name() == &unk_5B44F38) {
            continue;
        }
        if (out->insert(def)) {
            work.push(def->getOperands());
        }
    }
}
```

Keeping the closure walker separate from the shrinker lets the `produce_one` path reuse the same boundary logic without dragging in the yield-rebuild and op-replace machinery.

### produce_one Shrinker

The `produce_one` path runs an inlined slice walker that uses the same three boundaries as the `consume_one` path but skips the yield rebuild — `produce_one` regions have no result values to thread through a terminator, so the original yield op suffices once unused defining ops are gone. Instead of `sub_448E010`'s `moveInto` callback, the inlined walker dispatches through three `moveBefore` variants — `sub_446E270`, `sub_446E300`, `sub_446E390` — depending on whether each slice op lands before the original op, before a specific anchor, or before the region's first non-terminator op. The three-variant fan-out matches the structural cases `produce_one` regions present after D11 expands stages: bare producer ops, ops anchored to a `producer_acquire` lifetime, and ops live across an `scf.if` guard prelude.

### ClassID Dispatch Table

The shrinker's per-op behaviour branches on the op's `OperationName*` pointer, not on a dynamic type query. The dispatch table sits implicit in the filter callback and the boundary checks, but it can be read straight out of the binary:

| `OperationName*` slot | Op | Role in shrinker |
|---|---|---|
| `&unk_5B44F38` | `nv_tileas.async.pipeline.produce_one` | candidate for `produce_one` shrinker; backward-slice boundary in `consume_one` walks |
| `&unk_5B44F70` | `nv_tileas.async.pipeline.consume_one` | candidate for `consume_one` shrinker |
| `&unk_5BE6138` | unregistered sentinel | skipped by filter callback; guards against unregistered op shells |

No other op-name pointer reaches the shrinker. Every defining op encountered during the backward walk joins the slice unconditionally as long as it lives in the same region and isn't a `produce_one`. The shrinker therefore needs no knowledge of the rest of the pipeline op surface — `producer_acquire`, `consumer_release`, `producer_commit`, `consumer_wait`, and the various read/write ops all move into the new region as ordinary slice members.

### Diagnostics and Failure Handling

The shrinker emits no diagnostics on the success path. The op-flag sanity check is a fatal trap, not a recoverable error: a malformed op indicates an earlier-pass bug, not user IR Tileiras can reject gracefully. The pass itself never sets `*(self + 40) |= 4` and never returns `failure()` — every candidate either shrinks successfully or is structurally unshrinkable (slice equals the original region) and stays bit-for-bit unchanged.

### Input and Output IR Shapes

The input is a `consume_one` or `produce_one` region whose body contains every op the upstream pass placed into it, including ops whose results no longer reach the region's yielded values. After D11 expansion the region's body typically contains the union of all stages' compute work; the shrinker reduces it to just the slice that contributes to the yielded values.

```mlir
// Input: a consume_one region with surplus ops left over from D11
// expansion. %x_dead and %y_dead are produced but never reach the yield.
%r = nv_tileas.async.pipeline.consume_one %t {
  %a       = nv_tileas.async.pipeline.consumer_read %t : tensor<128x64xf16>
  %b       = nv_tileas.async.pipeline.consumer_read %t : tensor<64x128xf16>
  %x_dead  = nv_tileas.dot %a, %b, %acc_dead : tensor<128x128xf32>  // unused
  %y_dead  = nv_tileas.shuffle %a, %perm     : tensor<128x64xf16>   // unused
  %c       = nv_tileas.dot %a, %b, %acc      : tensor<128x128xf32>
  nv_tileas.async.pipeline.consumer_release %t
  nv_tileas.async.pipeline.yield %c : tensor<128x128xf32>
} : tensor<128x128xf32>
```

After shrinking, the new `consume_one` region contains only the backward slice of `%c` and the original `consumer_release`. The CityHash-keyed DenseSet ensures each defining op enters the slice exactly once even when multiple yielded values reach it through different operand chains.

```mlir
// Output: surplus ops dropped; only the slice reaching %c remains.
%r = nv_tileas.async.pipeline.consume_one %t {
  %a = nv_tileas.async.pipeline.consumer_read %t : tensor<128x64xf16>
  %b = nv_tileas.async.pipeline.consumer_read %t : tensor<64x128xf16>
  %c = nv_tileas.dot %a, %b, %acc : tensor<128x128xf32>
  nv_tileas.async.pipeline.consumer_release %t
  nv_tileas.async.pipeline.yield %c : tensor<128x128xf32>
} : tensor<128x128xf32>
```

When the slice equals the original region body, the shrinker is a no-op: it builds the fresh op, finds the slice covers every original member, and erases the fresh op instead of the original. This is the "structurally unshrinkable" case the failure handling section refers to.

### Why Back-to-Front Matters

The region walker pushes candidates in source order; the shrinker iterates `v2 -= 8` (a pointer decrement over an 8-byte-stride `SmallVector<Operation*, 48>`). When a function contains several sibling `consume_one` regions in the same parent — typical of an AUS-pipelined loop body with multiple compute stages — shrinking the last sibling first keeps earlier siblings' slice walks looking at a consistent operand DAG. Front-to-back shrinking would erase a defining op a later sibling's slice still references, and the later walk would either skip a live op (silently dropping work) or trap on a dangling `Operation*` — the DenseSet probe touches the pointer's hash, not its body, but the subsequent operand expansion dereferences the erased op's storage.

### Reimplementation Notes

A reimplementation must key its DenseSet by `Operation*`, not by SSA value or op index: the slice walker inserts the same defining op multiple times — once per yielded value reaching it through different operand chains — and only pointer-keyed dedup keeps the closure linear. The boundary set must be exactly three checks. Broadening it (e.g. stopping at any `nv_tileas.async.pipeline.*` op) over-shrinks consumer regions that legitimately read through pipeline view ops; narrowing it (e.g. dropping the region-parent check) pulls live-in values into the new region and loses them at the next verifier pass.

## Block-Scaled MMA Verification

Blackwell block-scaled MMA must satisfy a small catalog of shape and type invariants before lowering:

- FP4 MMA requires scale factors.
- Scale-factor element types for A and B must agree with the MMA kind.
- The accumulator must be Float32.
- Scale-factor vector size must match the K extent.
- Only supported `(atom_k, vector_size)` combinations are accepted.
- One-CTA and two-CTA variants must use compatible shapes.

The verifier returns the selected atom shape for lowering. Zero or failure means the op is invalid and must not proceed to NVVM.

## Pipeline to NVVM

Pipeline lowerings consume the logical pipeline surface and emit fixed NVVM/LLVM sequences.

| Pipeline concept | NVVM/LLVM lowering |
|---|---|
| producer acquire | participant masks, cluster arrive, mbarrier arrive, and state update |
| producer commit or tail | async bulk wait or named-barrier synchronization |
| async wait on TMA | `nvvm.cp.async.bulk.commit.group` and `nvvm.cp.async.bulk.wait_group` |
| async wait on GMMA | `nvvm.wgmma.commit.group.sync.aligned` and wait-group sync |
| async wait on mbarrier | `nvvm.mbarrier.try_wait.parity.shared` loop |
| create none | LLVM poison value |
| token/async casts | temporary unrealized conversion casts |
| named barrier | `nvvm.barrier.cta.sync` or warp/cluster barrier sequence |

The TMA bulk-copy templates are documented in [TMA, Tensormap, and cp.async.bulk Emission — cp.async.bulk Template Catalog](../../codegen/tma-tensormap-and-cp-async-bulk.md#cpasyncbulk-template-catalog); the WGMMA emission protocol that produces the commit-group / wait-group sequence is in [WGMMA Emission Protocol — The Four-Op Sequence](../../topics/wgmma-emission-protocol.md#the-four-op-sequence); the mbarrier state machine that anchors the arrive/try-wait loop is in [mbarrier State Machine](../../topics/mbarrier-state-machine.md); cluster-arrive / cluster-wait pairs and DSMEM transactions are documented in [Cluster Sync and DSMEM Handshake](../../topics/cluster-sync-and-dsmem-handshake.md). The shared codegen surface for the tcgen05 / WGMMA / mbarrier / cluster families lives in [tcgen05, WGMMA, mbarrier, and Cluster Sync](../../codegen/tcgen05-wgmma-mbarrier-cluster.md).

## Ordering Invariants

- Queue-to-pipeline rewrite must run before async materialization.
- Async materialization must run before schedule materialization.
- Convert-layout materialization must run before schedule consumers rely on final stage counts.
- AWS materialization emits `agent_switch`; unspecialized pipeline must skip AWS-partitioned functions.
- Pipeline-region optimization must run after producer/consumer regions are in their final form.
- Block-scaled MMA verification runs whenever the op is built or transformed.

## Cross-References

The pipeline-op surface this family consumes and produces is catalogued in [nv_tileas Op Roster and Builders — Pipeline Op Operand/Result Tables](../../dialects/nv_tileas/op-roster-and-builders.md#pipeline-op-operandresult-tables); the worked producer/consumer region example aligns with the pre-shrink IR shape D13 sees. The boundary between scheduler analysis and D09 materialisation, including the `ScheduleAnalysis` record and the strategy enum, is documented in [Schedule Solve and Cost Evaluators — Pass Boundary](../../scheduler/schedule-solve-and-cost-evaluators.md#pass-boundary); the modulo-scheduling algorithm that fills the record is in [Modulo Scheduler and Rau-Style Placement](../../scheduler/modulo-scheduler-and-rau.md). The mbarrier `try_wait.parity` loop the consumer side eventually lowers into is described in [mbarrier State Machine — Phase Parity](../../topics/mbarrier-state-machine.md#phase-parity); the WGMMA commit-group / wait-group pair the dot ops lower into is in [WGMMA Emission Protocol — The Four-Op Sequence](../../topics/wgmma-emission-protocol.md#the-four-op-sequence). The cross-pass failure-bit convention every TileAS pass uses for recoverable errors is in [Pass-Failure Handshake — Convention](../../mlir-infra/pass-failure-handshake.md#convention).

