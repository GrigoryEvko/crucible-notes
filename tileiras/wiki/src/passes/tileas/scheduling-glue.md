# TileAS Scheduling Glue Passes

## Abstract

The scheduling glue passes prepare IR for schedule generation, publish schedule analysis, and tag loops that LLVM must unroll later. They sit between layout/buffer preparation and async-pipeline materialization. Their job: make the IR schedulable, run the scheduler, preserve the result for materialization, and keep register-tensor loops from surviving as runtime-indexed loops in PTX.

This chapter also records the LLVM/NVVM cleanup passes that run after TileIR has been lowered. They schedule no TileAS IR, but they protect the backend handoff — selecting kernels, printing kernel metrics, propagating address spaces, and demoting non-kernel functions before PTX emission.

## Pass Roster

| Pass | Layer | Purpose |
|---|---|---|
| `TileASPrepareForScheduling` | TileAS MLIR | canonicalizes tiled views, atom widths, loop hints, memory layout, and TMA box dimensions |
| `TileASGenerateSchedule` | TileAS MLIR | runs serial or cost-based scheduling and publishes `ScheduleAnalysis` |
| `TileASUnrollRegisterLoops` | TileAS/LLVM bridge | tags register-tensor loops for full LLVM unrolling |
| `PretreatPass` | LLVM/NVVM | canonicalizes NVVM LLVM IR after inlining and before verification |
| `CheckGepIndexPass` | LLVM/NVVM | validates constant GEP indices after canonicalization |
| `SelectKernelsPass` | LLVM/NVVM | filters the module to selected kernels |
| `KernelInfoPrinter` | LLVM/NVVM | prints kernel metrics for diagnostics and tuning |
| `IPMSPPass` | LLVM/NVVM | propagates concrete NVPTX address-space facts interprocedurally |
| `NVVMAA` | LLVM analysis | provides alias facts from NVPTX address spaces |
| `NVPTXSetFunctionLinkagesPass` | LLVM/NVVM | demotes non-kernel functions and marks kernel visibility |

## Prepare for Scheduling

`TileASPrepareForScheduling` is the last TileAS cleanup before schedule generation. Six stages run in fixed order.

| Stage | Purpose |
|---|---|
| tiled load/store view decomposition | rewrites reshaped tiled views into direct views of the underlying allocation |
| atom vector-size refinement | chooses target-aware vector widths for copy, TMA, and MMA atoms |
| slice/fuse marking | annotates loops with slicing and fusion hints already discovered upstream |
| scheduling canonicalizer | unrolls tiny loops and decomposes single-op wrappers around atom operations |
| memory layout compaction | assigns shared-memory offsets to non-overlapping live ranges |
| TMA box-dimension refresh | recomputes runtime box dimensions used by Blackwell tensor-copy operations |

The stage order is part of the contract. TMA box dimensions, for instance, must refresh after view decomposition and memory layout compaction — the descriptor dimensions have to reflect the final view and allocation shape.

```c
LogicalResult prepare_for_scheduling(FuncOp func, TargetInfo target) {
    if (failed(decompose_tiled_load_store_views(func))) {
        return failure();
    }
    if (failed(refine_atom_vector_sizes(func, target))) {
        return failure();
    }
    mark_slice_and_fuse_candidates(func);

    if (failed(run_scheduling_canonicalizer(func))) {
        return failure();
    }
    if (failed(compact_memory_layout(func, target))) {
        return failure();
    }

    return refresh_tma_box_dimensions(func, target);
}
```

## Generate Schedule

`TileASGenerateSchedule` reads compute capability, builds the machine model, picks a scheduler strategy, runs it on each schedulable region, legalizes the result for materialization, and publishes `ScheduleAnalysis`.

The scheduler option accepts the serial baseline and cost-based variants. Unknown values fall back to the cost scheduler. The cost path also reads the maximum constraint-iteration count and the RRT-size threshold — it can refine constraints repeatedly before convergence.

```c
LogicalResult generate_schedule(FuncOp func, SchedulerOptions options) {
    ComputeCapability cc = read_compute_capability(func);
    if (!cc.valid()) {
        return func.emit_error("failed to get compute capability");
    }

    MachineModel model = build_machine_model(cc);
    Scheduler scheduler = make_scheduler(options.scheduler, model, options);

    for (Region *region : find_schedulable_regions(func)) {
        ScheduleAnalysis analysis;
        if (failed(scheduler.schedule(region, &analysis))) {
            return region->emit_error("failed to find a schedule");
        }
        if (failed(legalize_loop_schedule_for_materialization(region, analysis))) {
            return region->emit_error("failed to legalize schedule");
        }
        preserve_schedule_analysis(region, analysis);
    }

    return success();
}
```

With trace output enabled, the pass emits a Chrome tracing JSON stream. The useful events: issue timing, atom attributes, dependency edges, minimal latency, actual latency, and thread order. Tracing is diagnostic only — it must not affect scheduling.

## Register-Loop Unroll Tagging

`TileASUnrollRegisterLoops` (D10) is the smallest pass in this family. It attaches a single `#llvm.loop_annotation<unroll = <enable>>` attribute to ops D08 already marked as the root of a reg-to-reg copy sequence. The pass unrolls nothing itself — it writes metadata so LLVM's `LoopUnrollPass` fires on the resulting loop later in the NVPTX backend, after `convert-scf-to-cf` and `LowerLoopAnnotation` turn the `scf.for` body into an `llvm.br`/`llvm.phi` cycle carrying the `!llvm.loop` MD node.

The pass triple lives at `0x825590` (`getName` returning `"TileASUnrollRegisterLoops"`), `0x8255A0` (`getArgument` returning `"tileas-unroll-register-loops"`), and `0x8255B0` (`getDescription` returning `"Unroll loops that access slices of register tensors"`). `getDependentDialects` at `0x8256B0` loads `nv_tileaa` and `nv_tileas`. The pass exposes no options: no `Option<...>` registrations appear in the range, and no command-line wrappers reference it beyond the argument string.

The core rewriter is `sub_825BF0` (1 272 B). It walks the function with `runOnOperation` at `sub_8261E0`, and for each op carrying D08's `"nv_tileas.root_reg_to_reg_copy_op"` UnitAttr marker it interns the `"loop_annotation"` key via `sub_440A060(ctx, "loop_annotation", 15)` and attaches a `LoopAnnotationAttr` whose only populated field is `unroll = <enable>`. The cached global Identifier slot for the `llvm.loop_annotation` attribute name is `qword_591E3B8`; the rewriter loads it once and reuses the interned key across every tagged op in the function.

```c
LogicalResult unrollRegisterLoops(FunctionOpInterface fn) {
    fn.walk([&](Operation *op) {
        if (!op->hasAttr("nv_tileas.root_reg_to_reg_copy_op")) {
            return;
        }
        StringAttr key = StringAttr::get(ctx, "loop_annotation");        // sub_440A060
        op->setAttr(key, LoopAnnotationAttr::get(ctx, /*unroll=*/UnrollAttr::enable()));
    });
    return success();
}
```

The resulting attribute is the single-element `LoopAnnotationAttr` form `#llvm.loop_annotation<unroll = <enable>>`. LLVM's `LoopUnrollPass` reads this through the `!llvm.loop` metadata convention and picks the unroll factor from its own cost model (`computeUnrollCount`, `PartialThreshold`, register pressure). D10 deliberately writes neither `llvm.loop.unroll.count` nor `llvm.loop.unroll.full`: the policy is "enable, and let LLVM decide", keeping the factor decision in one place rather than splitting it between the TileAS scheduler and the NVPTX backend.

### Interaction with D08

D10 pairs with D08 (`MaterializeConvertLayout`). With `reg2reg-vec-size` at or below 15, D08 cannot vectorise the layout conversion and falls back to a per-element copy sequence. The root op of that sequence carries the `"nv_tileas.root_reg_to_reg_copy_op"` UnitAttr; downstream lowerings turn that root into a `scf.for` whose body shuffles register slices one element at a time. D10 recognises the marker and adds the unroll hint so the loop fully unrolls in PTX, where register variables have no runtime addressing — a rolled loop would either spill to local memory or be miscompiled by the tile-to-vector lowering.

The two passes stay split rather than fused because their context requirements pull in opposite directions. D08 needs layout-decomposition state — the cute atom catalogue, the per-target vector-size table, the layout-conversion analysis — to decide which ops are reg-to-reg copy roots. D10 needs the final IR shape: every layout-affecting pass between D08 and PTX emission must already have run so the marker still sits on the op that becomes the actual loop. Fold D10 into D08 and the marker decision has to win against every subsequent rewrite; fold D08 into D10 and D10 has to redo the layout analysis. Splitting them lets each pass run at its natural point in the pipeline and reduces the marker to a one-bit handoff.

## LLVM/NVVM Backend Glue

The LLVM/NVVM passes run after TileAS has lowered to LLVM and NVVM dialects.

`PretreatPass` performs NVIDIA-specific LLVM IR cleanup after inlining: normalising pointer casts, byval aggregates, lifetime intrinsics, intrinsic placeholders, and NVVM metadata before verification and lowering.

`CheckGepIndexPass` validates that constant GEP indices are in bounds once canonicalization has exposed the final aggregate shapes.

`SelectKernelsPass` filters the module by a configured list or ordinal range. Non-selected kernels disappear before code generation.

`KernelInfoPrinter` emits tuning metrics — stack allocation count, direct and indirect calls, inline assembly calls, invokes, and flat-address-space accesses. Flat address-space use matters: generic pointers block tensor-memory and bulk-copy lowering.

`IPMSPPass` propagates concrete pointer address spaces through calls. When a helper receives a generic pointer but every caller supplies the same concrete space, the pass rewrites or clones the helper so later NVPTX code skips the generic address-space cast.

`NVVMAA` is the companion alias analysis: generic address space may alias, distinct non-generic spaces do not alias, and equal non-generic spaces may alias.

`NVPTXSetFunctionLinkagesPass` identifies kernels by calling convention, `nvvm.kernel`, or kernel metadata, then demotes non-kernel definitions to internal linkage.

## Schedule Support Bodies

Three support algorithms serve every schedule consumer (`MaterializeSchedule`, `UnspecializedPipeline`, and the downstream stage expander). They live as free functions in the schedule-utils namespace rather than methods on `ScheduleAnalysis` — the same code then serves both AUS and AWS drivers without dragging analysis state into per-driver vtables.

| Helper | Role |
|---|---|
| materialize schedule | partitions loads, stores, pending async operations, and iteration data into materialization maps keyed by the producing op |
| build stages | converts union constraints into stage-ordered producer/consumer pairs that drive prologue/body/epilogue construction |
| expand single tiled op | replicates a tiled operation per stage and rewires operands to stage-local values |

```c
StageMap materialize_schedule(Region *region, ScheduleAnalysis &an) {
    StageMap m;
    for (Operation *op : region_ops(region)) {
        Stage s = an.stage_of(op);
        if      (is_load(op))  m.loads[s].push_back(op);
        else if (is_store(op)) m.stores[s].push_back(op);
        else if (is_async(op)) m.pending_async[s].push_back(op);
        else                    m.iter_data[s].push_back(op);
    }
    return m;
}

void build_stages(ScheduleAnalysis &an, SmallVector<Stage> *out) {
    for (Constraint &c : an.union_constraints()) {
        Stage producer = c.producer_stage();
        Stage consumer = c.consumer_stage();
        emit_producer_consumer_pair(*out, producer, consumer);
    }
    stable_sort_by_stage(*out);
}

void expand_single_tiled_op(TiledOp op, ScheduleAnalysis &an, Rewriter *rw) {
    Map<Value, Value> operand_source       = collect_operand_sources(op);
    Map<Stage, Operation *> stage_anchor   = collect_stage_anchors(an);

    for (Stage stage : an.stages_for(op)) {
        Operation *replica = clone_for_stage(op, stage, rw);
        for (Operand operand : replica->operands()) {
            if (Value replacement = lookup_stage_value(operand, stage, operand_source, stage_anchor)) {
                operand.replace_with(replacement);
            }
        }
    }
}
```

## Ordering Invariants

- `PrepareForScheduling` must run before `GenerateSchedule`.
- `GenerateSchedule` must preserve `ScheduleAnalysis` for materialization.
- Register-tensor unroll tagging must run before LLVM loop-unroll passes.
- LLVM/NVVM pretreatment and verification must run before PTX emission.
- Kernel selection and linkage demotion must not run before kernel attributes are finalized.
- Address-space propagation should run before alias-sensitive optimization and final NVPTX lowering.

## Cross-References

[Layout and Buffer Family — Assign Load/Store Layouts](layout-and-buffer-family.md#assign-loadstore-layouts) documents D08 (`MaterializeConvertLayout`), which emits the `"nv_tileas.root_reg_to_reg_copy_op"` marker that D10 consumes. [nv_tileas Op Roster and Builders — Attribute Roster](../../dialects/nv_tileas/op-roster-and-builders.md#attribute-roster) lists the attribute as part of the dialect's UnitAttr inventory. The upstream LLVM dialect `loop_annotation` attribute documents the metadata shape that D10 attaches and that the NVPTX backend's `LoopUnrollPass` later reads. The schedule analysis this family publishes is consumed by [Materialize Schedule](async-pipeline-family.md#materialize-schedule); the resource model behind it is documented in [Modulo Scheduler and Rau-Style Placement](../../scheduler/modulo-scheduler-and-rau.md) and [Resource Constraint Builder and RRT](../../scheduler/resource-constraint-builder-and-rrt.md). Backend address-space alias facts that `NVVMAA` exposes are described in [NVPTX Bring-up and Target Init](../../codegen/nvptx-bring-up-and-target-init.md).
