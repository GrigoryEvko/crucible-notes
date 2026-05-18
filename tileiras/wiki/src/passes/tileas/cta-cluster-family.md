# TileAS CTA / Cluster Family

## Abstract

The CTA/cluster family is the cluster-aware tier of the `nv_tileas` lowering pipeline. Where schedule and layout passes shape work inside a single CTA, this family shapes how multiple CTAs in a Hopper or Blackwell cluster cooperate and how a single CTA cycles through program-IDs across the grid. It bundles `OptimizeExecutionUnitMapping` (D12), `DynamicPersistent` (D16), `InsertOCGKnobs` (D17), `PlanCTA` (D19), and the D20 aux cluster (`RemoveBufferAlias`, `RemoveLayoutConversions`, `Slicing`, `PrepareForScheduling`, `ResolveAgentBoundary`). The `SinkNegF` sibling rides with `InsertOCGKnobs` — the binary places them adjacent and they share the same `Pass` SSO layout. All of these run after agent materialization and before final schedule and TMA-descriptor generation.

Cluster geometry comes from upstream: the `nv_tileaa.kernel_spec` attribute (read via `sub_152FDF0` / `sub_152FE00` in D19) carries `num_ctas` and an auxiliary scalar. This family propagates the consequences through layouts (`PlanCTA`), register/warp groups (`OptimizeExecutionUnitMapping`), per-CTA work distribution (`DynamicPersistent`), and scheduler-knob pragmas baked into late IR (`InsertOCGKnobs`). The Blackwell 4-CTA MMA path, the DSMEM cluster handshake, and the 2-CTA TMEM copy all consume the IR shape established here — they live in the `ConvertTileASToLLVM` boundary and the `cute_nvgpu` rewriter family, but the conditions driving them are set right here.

## Ordering Context

The family sits between agent materialization (`MaterializeSchedule`, see [Async and Pipeline Family — Materialize Schedule](async-pipeline-family.md#materialize-schedule)) and the scheduling-glue passes ([Scheduling Glue](scheduling-glue.md)). D12 needs `agent_switch` ops on every agent-bearing region. D16 needs a freshly-lowered `nv_tileas.kernel`. D17 needs MMA-family ops and async-pipeline fence/barrier anchors already lowered. D19 needs the `kernel_spec` attribute on the function. The D20 aux cluster expects D08 to have assigned per-op layouts and D11 to have either pipelined or skipped each loop. The Blackwell 4-CTA / DSMEM / 2-CTA paths run later, inside `ConvertTileASToLLVM`, and consume the cluster decisions recorded here.

## OptimizeExecutionUnitMapping (D12)

`OptimizeExecutionUnitMapping` (CLI `optimize-execution-unit-mapping`, description "Optimize the numWarps and warpId alignment for each agent") rewrites warp-specialized IR from `TileASUnspecializedPipeline` so every `AgentLikeOpInterface` op becomes an `nv_tileas.async.pipeline.agent_switch` with consistent `num_warps`, `warp_id`, and `agent_strides` ArrayAttrs across its successor regions. It runs at `ModuleOp` scope through three workhorses: `sub_83AC70` runs the post-order tree walk, `sub_83BA80` dispatches each leaf, and `sub_839240` (6 697 bytes, 239 BBs) is the rewriter that builds the `agent_switch`. A sub-pass `PropagateExecutionUnit` (CLI `propagate-execution-unit`, description "propagate the numWarps for each agent") lives at `sub_836E70` — the non-agent path of the dispatcher calls it to fold `numWarps` upward through `scf.for`, `scf.if`, and `nv_tileaa.func`.

The agent rewriter at `sub_839240` opens with eleven `SmallVector` scratch buffers (inline-12 and inline-6 mixed) for partition state and stable-partitions agents into "normal" vs "interleaved" (`s[i] & 3 == 0`). It then accumulates warp strides, rounding each agent's starting warp to its group size via `v44 = ((v39 != 0) + (v39 - (v39 != 0)) / group) * group`. When the rounded cursor diverges, the rewriter emits a *hole record* — `stride=delta, warpId=prevWarpId, opPtr=0, numWarps=1` — which downstream lowering treats as an empty slot. A final pad rounds total warps up to a multiple of 4. Group size comes from each agent's own `nv_tileas.num_warps`, so Hopper WGMMA (4), Blackwell 1-CTA UMMA (4), and 2-CTA UMMA (8) flow through the same logic with no target-specific branches.

Partitioning done, the rewriter compacts duplicate warp-ids via the `sub_15D4300` / `sub_8369D0` / `sub_836510` triple, builds the new op via `sub_44624C0(&unk_5B44F80, ctx)` (`RegisteredOperationName` lookup for `nv_tileas.async.pipeline.agent_switch`), populates an `OperationState` with `num_warps[]`, `warp_id[]`, and `agent_strides[]` ArrayAttrs, materialises it via `sub_43FFC20`, splices each old child region in through ilist surgery, and erases the original with `sub_446E1E0`. The lone visible diagnostic — "inconsistent numWarps in the agent switch, maybe it is called in different agents with different numWarps" — fires from the propagator when two child regions disagree on `numWarps`.

```c
LogicalResult optimize_execution_unit_mapping(ModuleOp module) {
    module.walk_post_order([&](Operation *op) {
        if (!implements_agent_like(op)) {
            propagate_execution_unit_upward(op);    /* sub_836E70 */
            return;
        }

        SmallVector<AgentDesc> agents = collect_agents(op);
        stable_partition(agents, [](AgentDesc &a) { return (a.kind & 3) == 0; });

        uint32_t cursor = 0;
        SmallVector<int32_t> num_warps, warp_id, strides;
        for (AgentDesc &a : agents) {
            uint32_t group   = read_attr_i32(a.op, "nv_tileas.num_warps");
            uint32_t rounded = ((cursor != 0) + (cursor - (cursor != 0)) / group) * group;
            if (rounded != cursor) {
                push_hole(num_warps, warp_id, strides, rounded - cursor, cursor);
            }
            num_warps.push_back(group);
            warp_id.push_back(rounded);
            strides.push_back(a.stride);
            cursor = rounded + group;
        }
        round_up_to(cursor, 4);                     /* final 4-warp pad */
        compact_duplicate_warp_ids(num_warps, warp_id, strides);

        Operation *fresh = build_op("nv_tileas.async.pipeline.agent_switch",
                                    num_warps, warp_id, strides);
        splice_regions_into(fresh, op);
        erase_op(op);
    });
    return success();
}
```

## DynamicPersistent (D16)

`TileASDynamicPersistent` (CLI `tileas-dynamic-persistent`, description "Make the kernel into dynamic persistent kernels") implements the compiler side of the persistent-grid idiom. The host launches one grid (or a small multiple) per SM; the device-side kernel must keep pulling fresh program-IDs from the runtime tile scheduler until the scheduler signals exhaustion. The pass rewrites a freshly-lowered `nv_tileas.kernel` body into the form

```text
scf.while (%pid) {
  cond: %v = is_valid_program_id %pid
        scf.condition(%v) %pid
} {
  body: <original body with programID remapped>
        %next = cancel_next_program_id
        scf.yield %next
}
```

The pass body at `sub_7C1800` (10 269 bytes, 322 BBs) is a six-step state machine.

Step 1 finds the `KernelOp` (TypeID `&unk_5B46E50`) via the predicate at `sub_7BFC80` plus walker `sub_7BFCB0`. Step 2 runs the idempotence guard `sub_7C0DF0 → sub_7C0C40`, which inspects every nested `scf.while` (TypeID `&unk_5B44FE0`) and, when its condition region already contains `is_valid_program_id`, emits the warning "Kernel is already dynamic persistent" and returns. Step 3 invokes `sub_7C0600` (`walkKernelAndCollectProgramIDs`) to gather every `nv_tileaa.get_program_id` value-number into an inlined `SetVector<uint32>` — the probe-stride-37 open-addressing layout shared across the `nv_tileaa` cluster-A passes. Step 4 builds the `scf.while` head (AbstractOperation `0x5BE3FC8`); the condition region emits `nv_tileaa.is_valid_program_id` and `scf.condition`. Step 5 clones the kernel body into the `scf.while`'s after region using the `SetVector`-driven `IRMapping`; the clone rebuilds five ops from scratch — `nv_tileas.alloc_tensor`, `nv_tileas.convert_layout`, `nv_tileaa.extract`, `arith.constant`, `arith.negf` — because their attributes (layout, element type) need to change for per-iteration re-evaluation. Step 6 emits `nv_tileas.cancel_next_program_id` immediately before the `scf.yield`.

`sub_7BFB90` registers the dependent dialects (`nv_tileaa`, `nv_tileas`, `scf`). The pass is target-agnostic and runs uniformly on sm_80+ — the persistent-grid idiom is a CTA-shape transform whose target-specific scheduler implementation (`StaticPersistent` / `StreamK` / `SM100_scheduler`) lives in CUTLASS host code. No barrier or fence sits between iterations; the scheduler arbitrates persistent-CTA synchronisation through its own `cancel_next_program_id` body.

```c
LogicalResult dynamic_persistent(KernelOp kernel) {
    if (already_dynamic_persistent(kernel)) {         /* sub_7C0DF0 idempotence */
        emit_warning(kernel.loc(), "Kernel is already dynamic persistent");
        return success();
    }

    SetVector<uint32_t> program_ids;
    walk_kernel_and_collect_program_ids(kernel, &program_ids);     /* sub_7C0600 */

    ScfWhileOp loop = build_scf_while(kernel.loc());
    Block      *cond = loop.before_block();
    Block      *body = loop.after_block();

    OpBuilder cb(cond);
    Value valid = cb.create<IsValidProgramIdOp>(/*pid=*/cond->arg(0));
    cb.create<ScfConditionOp>(valid, cond->arg(0));

    IRMapping map = build_program_id_mapping(program_ids, body->arg(0));
    clone_kernel_body_into(kernel, body, map);                      /* rebuilds 5 op kinds */

    OpBuilder bb(body, body->getTerminator());
    Value next = bb.create<CancelNextProgramIdOp>();
    body->getTerminator()->setOperands(next);
    return success();
}
```

## InsertOCGKnobs (D17)

`TileASInsertOCGKnobs` (CLI `tileas-insert-OCG-knobs`, description "This pass emits OCG knobs as specific optimization hints for the backend OCG compiler") bakes `llvm.inline_asm` ops carrying `.pragma "..."` directives into late IR so OCG — the closed-source PTX→SASS scheduler inside ptxas — sees them as scheduler knobs. Two knobs come out of this pass.

The first knob, emitted by `sub_7C6870`, is `.pragma "global knob SchedResBusyXU64=1";\n` (42 bytes). Two conditions gate it: the function contains at least one MMA-family op (TypeID `&unk_5B46EB8`, discovered by walker `sub_7C6150` invoking predicate `sub_7C5FD0`), and the module-level `nv_tileaa.target_spec` value falls in `{100, 101, 102, 103, 110}` — Blackwell sm100..sm103 plus Jetson Thor sm110. The arithmetic at `0x7C6A5D..0x7C6A68` is the literal `target_spec - 100 <= 3u || target_spec == 110`. The `llvm.inline_asm` op lands at function entry with empty operand and constraint strings and `has_side_effects=true`; it tells OCG to treat U64 issue-slots as extra-busy, throttling the scalar 64-bit lane against tcgen05 MMA on TMEM-heavy Blackwell kernels.

The second knob, emitted by `sub_7C6DA0`, is `.pragma "next knob FenceCode";\n` (31 bytes). It lands before every op whose class-info matches `&unk_5B44F28` (async-pipeline fence / arrive, collected by `sub_7C63D0 + sub_7C6220`) or `&unk_5B44F58` (mbarrier / cluster-barrier, collected by `sub_7C6000 + sub_7C6300`). Both walkers post-filter through `sub_13FDD70`, `sub_1496CE0`, and `sub_1497290`. The knob applies to the *next* PTX instruction, so OCG won't reorder the lowered fence/barrier across surrounding memory ops. A second emission path, `emitFenceCodePragmaBefore` at `sub_1162CF0`, fires from three tileas-to-LLVM conversion patterns (`sub_123DC20`, `sub_123E6B0`, `sub_123F090`) so individual op lowerings can emit FenceCode inline during dialect conversion. With `has_side_effects=true` blocking DCE and CSE, the inline-asm op survives every downstream lowering until `NVPTXAsmPrinter` writes it into the PTX text stream. A parallel `ocgEnterDirectives` / `ocgLeaveDirectives` ODS-property family (~12 op-property converters across `nv_tileaa` / `nv_tileas` / `cute_nvgpu`) reaches the same end result through structured attributes rather than inline-asm.

```c
LogicalResult insert_ocg_knobs(FuncOp fn) {
    uint32_t target = read_target_spec(fn->getParentOfType<ModuleOp>());
    bool busy_xu64  = (target - 100u) <= 3u || target == 110u;     /* sm100..103, sm110 */

    if (busy_xu64 && function_has_mma(fn)) {                       /* sub_7C6150 walk */
        OpBuilder b(fn.entry_block(), fn.entry_block().begin());
        emit_inline_asm(b, /*asm=*/".pragma \"global knob SchedResBusyXU64=1\";\n",
                        /*has_side_effects=*/true);                /* sub_7C6870 */
    }

    fn.walk([&](Operation *op) {
        if (op->name() != &unk_5B44F28 &&                          /* async fence/arrive */
            op->name() != &unk_5B44F58) return;                    /* mbarrier/cluster-barrier */
        OpBuilder b(op);
        emit_inline_asm(b, /*asm=*/".pragma \"next knob FenceCode\";\n",
                        /*has_side_effects=*/true);                /* sub_7C6DA0 */
    });
    return success();
}
```

## SinkNegF (D17 sibling)

`TileASSinkNegF` (CLI `sink-negf-through-shapes`, description "Move negf before shape operations") is a single-pattern greedy rewriter at `sub_7C44E0`. Its only pattern is `{anonymous}::ExchangeNegWithBroadcastPattern` (verbatim from the `llvm::getTypeName<T>()` cache at `0x4601750`). The `matchAndRewrite` at `sub_7C5270` accepts `arith.negf` ops whose defining op carries AbstractOperation handle `&unk_5B46F28` (`nv_tileas.broadcast`) or `&unk_5B44FB8` (`nv_tileas.expand_dim`), rebuilds the `arith.negf` on the pre-broadcast operand, then re-broadcasts; on mismatch it emits the note `"no broadcast/expand_dim op"`. Sinking the sign-flip exposes it to downstream MMA selectors that can fold the negation into a `.neg` operand modifier of `mma.sync` / `wgmma` / `tcgen05.mma`. Arch-agnostic.

## PlanCTA (D19 + BF10)

`TileASPlanCTA` (CLI `tileas-plan-cta`, description "propagate CTA related layouts") propagates cluster-aware layouts. `runOnOperation` at `sub_7D4090` binds to `FunctionOpInterface` (intern key `"mlir::FunctionOpInterface]"`, length 25, cached in `qword_5B37670` via `sub_44A8A10 / sub_44A8AC0`), reads `num_ctas` and an auxiliary u32 from the function's `nv_tileaa.kernel_spec` (`sub_152FDF0` / `sub_152FE00`), and short-circuits when `num_ctas == 1`. For multi-CTA clusters — Hopper 2-CTA MMA, Blackwell 2-CTA UMMA, 4-CTA copy-atom — the analysis at `sub_7C9600` constructs a 160-byte state object that interns three StringAttrs (`"plancta.direction"`, `"backward"`, `"forward"`) at `+16/+24/+32`. A 64-byte chunk-list backs a `std::deque<Operation*>` worklist whose iterator state fills slots `+48..+120`.

Seeding happens in two phases. `sub_7CB2C0 → sub_7CB1E0 → sub_7CB300` walks the function post-order and pushes every `nv_tileas.convert_layout` op (classID `&unk_5B44FC0`) into the worklist via the 2 071-byte seeder `sub_7CA9C0`, which inspects the convert's src/dst encodings and tags it forward, backward, or both. When the `direction` byte at analysis+40 is unset, `sub_7CE010` runs a forward-seed walker (`sub_7C94B0` with filter `sub_7C9400`) to gather TMA-load and alloc-tensor anchors, synthesising placeholder `nv_tileas.convert_layout` ops via `sub_7C9C80`. Both flows meet inside the same worklist.

The propagation loop in `sub_7D3F90` pops an op and dispatches to `sub_7D3F50` (`isBackward(op) ? stepBackward : stepForward`). `stepBackward` (`sub_7D3B50`, 1 012 bytes, 62 BBs) walks operands and either retags the producer backward and re-enqueues, or — when the producer is itself a tagged convert_layout — invokes the merge at `sub_7CC3E0`. `stepForward` (`sub_7D1C60`, 850 bytes) does the dual on users. The merge commits the CTA-layout decision: it splices the producer's operands into the consumer's operand list via doubly-linked pointer surgery, then calls `sub_7C9FB0` (deque bulk-pop helper) with flag `1` to erase both convert_layouts.

```c
LogicalResult plan_cta(FuncOp fn) {
    KernelSpec spec = read_kernel_spec(fn);            /* sub_152FDF0 / sub_152FE00 */
    if (spec.num_ctas == 1) return success();          /* trivial cluster: skip */

    PlanCtaState st;                                   /* 160-byte analysis */
    init_direction_attrs(&st);                         /* "plancta.direction" et al */

    seed_from_convert_layouts(fn, &st);                /* sub_7CB2C0 + sub_7CA9C0 */
    if (!st.direction_set) seed_from_anchors(fn, &st); /* sub_7CE010 forward seed */

    while (Operation *op = pop_front(&st.deque)) {     /* std::deque worklist */
        if (is_backward(op, &st)) {
            step_backward(op, &st);                    /* sub_7D3B50 */
        } else {
            step_forward(op, &st);                     /* sub_7D1C60 */
        }
        if (matches_partner(op, &st)) merge_pair(op, &st);   /* sub_7CC3E0 */
    }
    return success();
}
```

A 28-row LOW cluster between `0x7CB8B0` and `0x7D1640` holds the per-classID arm table for these two direction handlers. It splits into 14 backward arms, 9 forward arms, 2 shared broadcast primitives, 2 iter-arg trampolines, and 1 arith-helper-owned deque-consume leaf. The shared primitives at `sub_7CD230` (broadcastEncodingOne — adds one forward-tagged convert_layout per Value use, re-enters the worklist via `sub_7CC540`) and `sub_7CD3A0` (broadcastEncodingAll — fans out across an op's operand vector and its DPS result vector, with the inline/sidecar split at `op - 16*(i+1)` for slots 0..5 and `op - 96 - 24*(i-5)` for slots ≥6) are the fanout workhorses every arm eventually reaches.

Each arm specialises by encoding classID. `sub_7CD0E0` (scatter / extract_slice) reads operand[0]'s encoding, computes a 6-or-`rankOfVector+6` rank tag, looks up the parent op's CTA encoding via `sub_4435F20`, returns 0 on a match, otherwise drives the reduction handler `sub_7CCC20` and clears the direction with `sub_7C9940`. `sub_7CDA50` (iter_arg / scf.while) and `sub_7CDCE0` (scf.for) stack the same skeleton and additionally call `sub_14314D0` to resolve which iter_arg slot in the outer loop maps to the operand being rewritten. The biggest arm — `sub_7D0D80` at 2 234 bytes — is the scf.for region sinker: three-path rank dispatch with a rank-4 branch for 2-CTA MMA atoms (`sub_13D2140`, `sub_14F1150`, `sub_18664A0`). The other heavy LOW, `sub_7CFAE0` (1 397 bytes), is the forward direction's alias-in/alias-out helper, using `sub_1427100` for forward MmaAtomLayout selection and `sub_14265B0` for backward.

### PlanCTA 28-arm table

| Addr     | Dir | Role                                                                                    |
|----------|-----|-----------------------------------------------------------------------------------------|
| 0x7CB8B0 | aux | `dequeBulkConsume(dst, src_begin, src_end, dst_deque)` — std::deque memmove leaf        |
| 0x7CD0E0 | B   | `scatter_operand_encoding` — extract_slice / view path                                  |
| 0x7CD230 | shared | `broadcastEncodingOne(anal, ctx, opOperand, encoding)` — one-Value forward leaf      |
| 0x7CD3A0 | shared | `broadcastEncodingAll(anal, op, ops, n, dests)` — operand + DPS fanout dispatcher    |
| 0x7CD8F0 | B   | `broadcast_to_all_operands` — generic broadcast across operand vector                   |
| 0x7CDA50 | B   | `iter_arg_or_scf_while_propagator` — uses sub_14314D0 for iter_arg slot resolution      |
| 0x7CDCE0 | B   | `scf_for_propagator` — symmetric backward+forward, also called by sub_7CE010 seeder     |
| 0x7CE4F0 | B   | forward-seed helper using `sub_13E9790` transform                                       |
| 0x7CE570 | F   | `operand_prev_block_arg` (13EADF0 path)                                                 |
| 0x7CEDE0 | F   | `operand_prev_block_arg_v2` (13F5210 path)                                              |
| 0x7CEE70 | B   | `extract_slice_propagator` — uses sub_1570430 slice-map reader + sub_13E9920            |
| 0x7CF030 | B   | `broadcast_Y` (13EADF0 path)                                                            |
| 0x7CF0B0 | F   | `prev_block_arg_v3` (13EADF0 path, block-arg)                                           |
| 0x7CF140 | B   | `encoding_helper_A` — backward full-shape recompute via sub_13F6490 + sub_14265B0       |
| 0x7CF470 | F   | `encoding_helper_B` — forward full-shape recompute via sub_13EB2C0                      |
| 0x7CF7A0 | B   | `encoding_helper_C` — 5-arg variant with cached result-encoding                         |
| 0x7CFAE0 | F   | `dual_encoding_helper` — alias-in/alias-out fork, two MmaAtomLayout pickers              |
| 0x7D0060 | iter-trampoline | `inner_scf_for_iter_broadcast` — 4-slot fanout (input/next/DPS-op/DPS-res)   |
| 0x7D01B0 | B   | `yield_slot_router` — scf.yield index dispatch into sub_7D0060                          |
| 0x7D0270 | F   | `yield_index_router` — companion router                                                  |
| 0x7D02F0 | B   | `return_yield_handler` — switch over ModuleOp / &unk_5BE4008 / &unk_5B44F70             |
| 0x7D0510 | iter-trampoline | forward iter-args fanout using sub_4191730 / sub_41918D0                     |
| 0x7D0600 | B   | `yield_slot2` — mirror of 7D01B0 dispatching into 7D0510                                |
| 0x7D06C0 | B   | `while_region_body` — scf.while before-region propagator                                 |
| 0x7D0820 | F   | `DPS_result_propagator` — use-list pointer surgery for &unk_5B44F38 / &unk_5B44F70      |
| 0x7D0B80 | F   | `DPS_operand_propagator` — 4-way classID switch (ModuleOp / 5BE4008 / 5BE3FF8 / 5B44F10)|
| 0x7D0D80 | B   | `scf_region_sinker` — biggest arm; rank-2/rank-2 fast path, reduction, rank-4 (2-CTA)   |
| 0x7D1640 | F   | `scf_while_body_propagator` — forward direction's scf.while companion                    |

Eleven of these arms carry no static caller edge in `tileiras_callgraph.json` — IDA treats indirect calls through the 5-entry dispatch table at `sub_7C8DA0 .. sub_7C8E20` as untracked. The edges were recovered from disassembly of `sub_7D1C60` and `sub_7CD3A0`.

## D20 aux passes

The D20 group bundles the rest of the per-FunctionOp cluster-aware transforms.

`TileASRemoveBufferAliasPass` (`sub_7DACE0`, 11 402 bytes) iterates a worklist of `nv_tileas.alloc_tensor` ops to a fixed point, collapsing aliases introduced by `arith.select` / `scf.while`. Convergence failure emits `"TileASRemoveBufferAliasPass failed to converge"`; unsupported ops yield `"RemoveBufferAlias: not supported operation type"`; `scf.while`-yielded aliases yield `"Yielded alias not implemented yet"`.

`TileASRemoveLayoutConversionsPass` (`sub_7E6210`, 10 124 bytes) delegates to the 11 728-byte worker `sub_7E3440` for buffer-side propagation (`"failed to rewrite in buffer layout propagation"`), register-side propagation (`"failed to rewrite in reg layout propagation"`), and three rounds of greedy cleanup (`"failed to apply patterns greedily"`). The worker switches on op names `nv_tileas.convert_layout`, `nv_tileas.async.pipeline.consume_one`, `nv_tileas.pragma`, and `scf.if`.

`TileASSlicingPass` (`sub_7FE6C0`, 12 298 bytes) materialises the `sliceCount` IntegerAttr on `scf.for` / `scf.while` loops via the 10 289-byte pattern `sub_7F8DC0`. Failure strings are `"unsupported op in Slicing pass"`, `"unsupported op to be a lower bound in slicing pass "`, `" fail to get an initial forOperand in slicing pass "`, and `"is not expected inside sliced part in SlicingPass\n"`. The attribute parser at `sub_7F7480` emits `"The `sliceCount` need to be a `IntegerAttr`"` on malformed input.

`TileASPrepareForScheduling` (`sub_8C4F80`) fetches compute capability via `sub_13FB490`, threads it through an `argv` bundle, and invokes walker `sub_8C4590` with leaf `sub_8C4710`. When the leaf finds `FunctionOpInterface` on both op and parent, it fires the 9 943-byte per-function kernel at `sub_8C1EB0`, which runs six serial sub-passes (names baked at `0x4606C6D` onward): `decomposeTiledLoadStoreView`, `refineVecSizeOfAtoms`, `sliceAndFuse`, `runCanonicalizer`, `compactMemLayout`, `refreshBoxDim`. Step 2 picks between `ld.global.v2/v4/v8` based on compute capability; step 6 is Blackwell-mandatory and recomputes TMA box dimensions for every `tiled_load` / `tiled_store` whose view step 1 modified — `cp.async.bulk.tensor.Nd` traps on SM100+ when the descriptor's boxDim vector mismatches the final view.

`TileASResolveAgentBoundaryPass` (CLI `tileas-resolve-agent-boundary`) is the cluster-family pass that legalises values crossing an `nv_tileas.async.pipeline.agent_switch` boundary. Warp-specialized programs partition work across producer, consumer, and compute agents; values that flow from one agent's region into another's cannot always stay as direct SSA values because the consuming agent runs on a different warp set and reads operands from a different physical register file or shared-memory bank. This pass inserts the IR shape that delivers those values across the boundary — typically a shared-memory handoff combined with a layout conversion sized to the destination agent's expected shape.

The pass body has no strong string anchor in the surveyed range, so the exact rewriter shape is not pinned. The contract, however, is fixed by what the downstream lowering passes assume on input: after this pass runs, every value that crosses an `agent_switch` boundary either stays as a direct SSA value (when the destination agent can consume it in place) or has been materialised through a `nv_tileas.alloc_tensor` / `nv_tileas.copy` / `nv_tileas.convert_layout` chain that delivers it in the destination layout. Named-barrier emission stays deferred — that is a separate pass's responsibility. The pass scope is `FunctionOpInterface`, the gate is post-agent-materialisation (i.e. after `MaterializeSchedule` has emitted the `agent_switch` ops), and the only invariant downstream consumers rely on is the handoff shape itself, not the precise op sequence used to materialise it.

## Blackwell 4-CTA MMA path (BG06)

The `tcgen05.mma` instruction family has no `cta_group::4` opcode — its verifier at `sub_1AD26A0` packs `cta_group` into two bits and accepts only values `1` and `3`. Blackwell's 4-CTA semantics are a *copy-side* notion: four cooperating CTAs hold the A/D TMEM tiles, and the SMEM→TMEM copy atom that stages the A operand fans data out across the cluster before one `tcgen05.mma` runs per peer. The architectural background — copy-side ownership, rank predicates, sibling pairing — is collected in [Blackwell 2-CTA and 4-CTA MMA](../../topics/blackwell-2cta-and-4cta-mma.md).

The relevant pattern is `{anonymous}::AtomCopyMakeS2tCopyOpLowering::matchAndRewrite` at `sub_119B710`. Its shape dispatch reads `Cute_nvgpu_S2tCopy_Shape.+0x20` via `sub_13C5F30`; the `3` arm at `0x119CB98` writes constant `4` into stack slot `var_508` (vs `2` on the `1`/`2` arm at `0x119CA40`). That `4` is the multicast width and propagates into `cute.tiled.copy.partition_S` and the `cta_group` field of the eventual `nvvm.tcgen05.cp`. The 4-CTA path also builds rank-parity predicate `arith.andi (arith.remsi %rank, %mcw), 1` with `%rank` from `nvvm.read.ptx.sreg.cluster.ctarank`, so exactly two of the four CTAs (odd low-bit) issue the actual copy while the others receive the multicast. The predicate is wrapped in `cute_nvgpu.arch.make_warp_uniform` (`sub_1134460 → sub_1165D80`) before `scf.if`. TMEM distribution happens at `cute.tiled.copy.partition_D` (TypeID `&unk_5B48078`, built at `0x119C296`), slicing the destination TMEM MemRef into four quarter-slices keyed off `%rank`.

## DSMEM handshake (S01)

`sub_11420D0` is the cluster-scope DSMEM handshake emitter inside `ConvertTileASToLLVM`. Given a barrier/pipeline op and a destination mnemonic root, it emits one of two shapes. When the op-walk finds no multi-CTA parent, it lays down a bare `nvvm.cluster.arrive{.relaxed}` + `nvvm.cluster.wait` pair. Otherwise it emits the full DSMEM handshake — `nvvm.mapa` → `llvm.addrspacecast` → optional `llvm.inline_asm "fence.release.cluster;"` (gated on the `a4` relaxed flag) → `nvvm.mbarrier.txn` (expect_tx) → `arith.cmpi` → `scf.if { llvm.load / llvm.store / arith.xori; }` → `nvvm.cluster.arrive{.relaxed}` → `nvvm.cluster.wait`. The `fence.release.cluster;` literal lives at `byte_4FA453E`; the relaxed arrive wins when an explicit release fence sits upstream. A fast-path bypass at `sub_1141120` skips the handshake when `sub_152FDF0` returns 1 (trivial single-CTA shape). Ten different rewriters reach this emitter through the `sub_11435E0` thunk, whose `a5` flag switches between this consumer-side body and the producer-side `sub_11420B0`. The handshake protocol is documented end-to-end in [Cluster Sync and DSMEM Handshake — DSMEM Transaction Handshake](../../topics/cluster-sync-and-dsmem-handshake.md#dsmem-transaction-handshake).

## Blackwell 2-CTA TMEM copy (S02)

The Blackwell 2-CTA TMEM copy is the `(v36 - 1) <= 1` arm of the same `sub_119B710` shape dispatch. Field `+0x20` (`sub_13C5F30`) selects shape; field `+0x18` (`sub_13C5F20`) carries the numeric cta-group (1 / 2 / 4). The 2-CTA arm sets multicast width 2 and takes the direct-build predicate fork at `sub_1134400` rather than constructing the `arith.remsi / arith.andi` chain — the downstream `nvvm.tcgen05.cp`'s symmetric 2-CTA handshake covers the peer-CTA half of the TMEM tile through the mma instruction's own `cta_group::2` encoding. Phase 4 resolves the TMEM coord-to-offset map via `sub_116A8D0`, phase 5 constructs the destination TMEM MemRef (memory-space tag 4) via `sub_116AF90`, phase 6 builds `cute.tiled.copy.partition_S`, and phase 7 emits the `scf.if`-wrapped `cute.tiled.copy(atom, coord, mbarrier)`. The 2-CTA mbarrier-init helper `sub_1147B40` is told `isCtaGroup2 = (v25 == 2)`; failure emits `"Failed to init mbarrier"`. See [Blackwell 2-CTA and 4-CTA MMA — CTA Group Control Word](../../topics/blackwell-2cta-and-4cta-mma.md#cta-group-control-word) for the encoded `cta_group::2` bit and [tcgen05 Tensor Memory Model](../../topics/tcgen05-tensor-memory-model.md) for the underlying TMEM model.

## Late rewriter `sub_99A940` (DD02)

`sub_99A940` (10 278 bytes, zero string literals) is the post-Schedule late IR rewriter that fires on every `nv_tileas.async.pipeline.create_pipeline` op. Its sole caller is the `sub_99D170 / sub_99D2E0` walker pair, dispatching on the OperationName sentinel `&unk_5B45060`. The rewriter emits no new MLIR ops — it is a *type rewriter*. It walks the create_pipeline's operand list and inner consumer/producer regions, unwrapping `PipelineIteratorType` (TypeID `&unk_5B45A60`) values via `sub_1496C90` at five sites, gated on the type's kind field (`sub_1496C80`) returning `11` (consumer iterator subclass) or `1` (producer iterator subclass). It also rematerialises new `PipelineIteratorType` values via `sub_1498180(ctx, depth, elem)` and seats them into one of six per-pass DenseMaps. This is the final cluster-aware cleanup once `Schedule::solve` (`sub_8EEE70`) has committed iteration counts: each producer/consumer region's iterator types unwrap to their element types, so subsequent lowering passes see plain SSA values rather than wrapped iterator handles.

## Per-pass roster

| Pass                              | Body (sub_) | Scope                 | Gate                                                  | Output                                                       |
|-----------------------------------|-------------|-----------------------|-------------------------------------------------------|--------------------------------------------------------------|
| `OptimizeExecutionUnitMapping`    | 0x83AC70 / 0x839240 | `ModuleOp`    | `AgentLikeOpInterface` (`&unk_5B44F80`)               | `agent_switch` with `num_warps[]`, `warp_id[]`, `agent_strides[]` |
| `PropagateExecutionUnit` (helper) | 0x836E70    | per-op                | non-agent op classID                                  | folded `numWarps` upward through scf / func                  |
| `TileASDynamicPersistent`         | 0x7C1800    | `nv_tileas.kernel`    | not already wrapped in `scf.while` + `is_valid_program_id` | body wrapped in `scf.while` + `cancel_next_program_id`  |
| `TileASInsertOCGKnobs` (U64)      | 0x7C6870    | `FunctionOpInterface` | target_spec ∈ {100..103, 110} AND MMA present         | `.pragma "global knob SchedResBusyXU64=1";` at entry         |
| `TileASInsertOCGKnobs` (Fence)    | 0x7C6DA0    | `FunctionOpInterface` | classID ∈ {`&unk_5B44F28`, `&unk_5B44F58`}            | `.pragma "next knob FenceCode";` before each anchor          |
| `TileASSinkNegF`                  | 0x7C44E0    | `FunctionOpInterface` | `arith.negf` over broadcast / expand_dim              | `arith.negf` moved before the shape op                       |
| `TileASPlanCTA`                   | 0x7D4090 / 0x7D3F90 | `FunctionOpInterface` | `kernel_spec.num_ctas != 1`                   | folded / moved `convert_layout`s; cleared direction tags     |
| `TileASRemoveBufferAliasPass`     | 0x7DACE0    | `FunctionOpInterface` | aliased `alloc_tensor` users                          | canonical alloc + rebuilt users                              |
| `TileASRemoveLayoutConversionsPass` | 0x7E6210 + 0x7E3440 | `FunctionOpInterface` | redundant convert_layout pairs              | propagated layouts; folded convert chains                    |
| `TileASSlicingPass`               | 0x7FE6C0 + 0x7F8DC0 | `FunctionOpInterface` | `scf.for` with `sliceCount: IntegerAttr`      | N slice regions + optional residual loop                     |
| `TileASPrepareForScheduling`      | 0x8C4F80 + 0x8C1EB0 | `FunctionOpInterface` | valid compute-cap pointer                     | six stages: decomposeTiledLoadStoreView → refineVecSizeOfAtoms → sliceAndFuse → runCanonicalizer → compactMemLayout → refreshBoxDim |
| `TileASResolveAgentBoundaryPass`  | (unpinned)  | `FunctionOpInterface` | post-agent-materialisation                            | renumbered / legalised `agent_switch` edges                  |

## Ordering Invariants

- `OptimizeExecutionUnitMapping` (D12) runs after `MaterializeSchedule` has emitted `AgentLikeOpInterface` ops.
- `DynamicPersistent` (D16) runs once per `KernelOp`, before scheduling; it is idempotent on `scf.while`-wrapped kernels.
- `InsertOCGKnobs` (D17) runs after MMA-family ops and async-pipeline fence/barrier anchors have reached their final form; the `.pragma` inline-asm ops must survive every downstream lowering, so D17 must not run before passes that DCE inline-asm with no side effects.
- `SinkNegF` is order-insensitive relative to D17 but must run before MMA-operand-modifier folding sees the negation.
- `PlanCTA` (D19) requires `nv_tileaa.kernel_spec` on the function; it short-circuits when `num_ctas == 1`.
- The D20 aux cluster expects layouts already assigned by D08 and pipelining decided by D11.
- The 4-CTA / DSMEM / 2-CTA emitters in `ConvertTileASToLLVM` run after every pass in this family has committed its cluster-shape decisions.
