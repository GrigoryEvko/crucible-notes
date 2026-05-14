# Driver Entry and Optimization Levels

## Abstract

The Tileiras driver chooses a single MLIR pass pipeline for each compilation. The choice is a pure function of four inputs: the resolved compute target, the requested `opt-level`, the `v2-opt-level` axis that gates the newer TileAS lowering, and the `pipeline-strategy` flag that selects the warp-specialization variant. The output is a fully-constructed `PassManager` whose pass list and analysis-preservation contract are fixed before any IR mutates. Decoupling pipeline construction from pipeline execution is what lets the driver report the exact pipeline it is about to run, lets the textual `--pass-pipeline` parser produce the same pass graph, and lets diagnostics name each pass that contributed to a failure.

## Entry Chain

The driver entry point is a small, linear state machine. It registers dialects, parses bytecode, builds the pipeline, runs it, and serializes. Each phase has a defined failure mode that cannot leak state into a later phase.

```c
int compile_tileir(ByteSpan input, TileirasConfig config, ByteBuffer *out) {
    MLIRContext ctx;
    register_tileiras_dialects(&ctx);

    OwningOpRef<ModuleOp> module = parse_tileir_bytecode(&ctx, input);
    if (!module) {
        return TILEIR_ERROR_BAD_BYTECODE;
    }

    PipelineOptions opts = resolve_pipeline_options(config);
    PassManager pm(&ctx, ModuleOp::getOperationName());
    populate_pipeline(&pm, opts);

    if (failed(pm.run(*module))) {
        return TILEIR_ERROR_COMPILE_FAILED;
    }

    return serialize_gpu_module(*module, config, out);
}
```

`populate_pipeline` is the only place that consults `opts.opt_level`, `opts.v2_opt_level`, and `opts.pipeline_strategy`. Once it returns, the pass manager is immutable; no later phase decides which passes run.

## Optimization Tiers

| Tier | Role | Typical use |
| --- | --- | --- |
| `O0` | Verifier-only skeleton. | Debugging bytecode ingestion and early IR validity. |
| `O1` | Frontend conversion and light cleanup. | Fast checks of `cuda_tile` to TileAA lowering. |
| `O2` | Default TileIR lowering through TileAS and first LLVM/NVGPU conversions. | Normal compilation. |
| `O3` | Full conversion stack, extra canonicalization, target finalization, and debug-scope synthesis. | Highest quality output and late-stage validation. |

`v2-opt-level` is a second axis. The primary `opt-level` selects the tier; `v2-opt-level` enables or suppresses the newer TileAS scheduling and specialization stages independently of that tier. The driver propagates both values into the pass manager as separate attributes so that the textual `--pass-pipeline` parser sees the same configuration the driver sees.

The recovered dispatcher uses the following effective structure:

| Requested tier | Base adders | Extra behavior |
| --- | --- | --- |
| `O0` | none | Only automatic verifier slots run. |
| `O1` | frontend adder | Convert `cuda_tile` to TileAA, insert debug scopes, canonicalize. |
| `O2` | frontend + TileAS adder | Add TileAA-to-TileAS, host wrapper, TileAS-to-LLVM, CSE, TileAS-to-NVGPU. |
| `O3` | `O2` + full conversion adder | Add TileIR verification, LLVM conversion, NVGPU/NVVM conversion, finalization. |

Two snapshot printers are conditional on `emit-line-info`. The first runs after frontend conversion; the second runs at the TileAS/LLVM boundary. Both are pure diagnostics — they print textual IR for line-info correlation and never mutate the module.

## Pipeline Strategy

`pipeline-strategy` selects how aggressively the compiler specializes producer/consumer execution. The TileAS-side rewrites these strategies select between are documented in the [Async Pipeline Family](../passes/tileas/async-pipeline-family.md).

| Strategy | Meaning |
| --- | --- |
| `none` | Do not add TileAS pipeline-specialization passes. |
| `unspecialize` | Use the unspecialized pipeline path with configurable stage count. |
| `warp-specialize` | Split work across producer and consumer agents and schedule resource use. |

For warp specialization, `rrt-size-threshold` chooses between lighter and heavier scheduling behavior.
A zero threshold selects the lighter path; a nonzero threshold enables resource-reservation-table
compression and the heavier scheduler preparation passes.

The heavy path is the one that prepares scheduling, specializes agents, checks register pressure, and
rewrites layouts around the schedule. The light path still inserts boundaries and barriers, but avoids
the full resource-reservation machinery.

## Schedule Analysis Ordering

TileAS scheduling does not happen in one pass. The work splits across a constraint-generation pass that builds a `ScheduleAnalysis` and stores it in the analysis manager, a configurable run of cleanup passes that promise to preserve `ScheduleAnalysis`, and a materialization pass that retrieves the analysis, runs the modulo scheduler, and rewrites IR to express the solved schedule. The separation matters because cleanup passes that do not declare `ScheduleAnalysis` as preserved cause the analysis to be invalidated and recomputed, which both breaks compile times and produces a different schedule than the one any earlier diagnostic referred to.

The contract reduces to a dependency map. Each pass declares what it requires, what it produces, and what it preserves; the pass manager enforces ordering and invalidation from those declarations.

| Pass | Requires | Produces / Modifies | Preserves |
|---|---|---|---|
| `tileas-generate-schedule-constraints` | TileAS IR with stable function shape | `ScheduleAnalysis` | TileAA, DominanceInfo |
| `canonicalize` (between generate and materialize) | — | — | `ScheduleAnalysis`, TileAA |
| `cse` (between generate and materialize) | — | — | `ScheduleAnalysis`, TileAA |
| `tileas-materialize-schedule` | `ScheduleAnalysis` | TileAS schedule attributes, pipe IR | — |

```c
LogicalResult run_schedule_pipeline(FuncOp fn, AnalysisManager am) {
    ScheduleAnalysis &constraints =
        am.getAnalysis<ScheduleAnalysis>(fn);

    for (Pass *cleanup : cleanup_between_schedule_and_materialize) {
        PreservedAnalyses preserved = cleanup->run(fn);
        if (!preserved.isPreserved<ScheduleAnalysis>()) {
            return fn.emitError(
                "cleanup pass invalidated ScheduleAnalysis; "
                "rerun constraint generation or remove the pass");
        }
    }

    Schedule solved = solve_modulo_schedule(constraints);
    if (!solved.feasible) {
        return fn.emitError("modulo scheduler returned no feasible II");
    }

    return materialize_schedule(fn, solved);
}
```

The hard-failure rule on invalidation is deliberate. A silent recompute would hide the underlying mistake that some cleanup pass was added to the pipeline without declaring `ScheduleAnalysis` as preserved, and the symptom would surface much later as a mismatched schedule.

## Serialization Scopes

Two outer instrumentation scopes give profilers and callback integrations stable handles.

| Scope | Covers |
| --- | --- |
| `CompileNVVM` | Running the MLIR-to-NVVM/NVPTX compilation pipeline. |
| `SerializeGPUModule` | Translating the GPU module to PTX/cubin and invoking downstream tools. |

These scope names are part of the public ABI for embedders. Fine-grained pass scopes underneath them can change between releases, but external profilers rely on the outer names being durable.

## Cross-References

[Pipeline Options Mapping — Option-to-Pass Map](options-mapping.md#option-to-pass-map) is the lookup table that resolves each option to its consuming pass. [Pass List by Optimization Level](full-pass-list-by-opt-level.md) names the exact pass sequence per tier. [Pass Manager Internals — Anchor Hierarchy](pass-manager-internals.md#anchor-hierarchy) explains the nesting model the driver populates. [Modulo Scheduler and Rau-Style Placement — Placement Arms](../scheduler/modulo-scheduler-and-rau.md#placement-arms) is the scheduler that consumes the preserved `ScheduleAnalysis`.
