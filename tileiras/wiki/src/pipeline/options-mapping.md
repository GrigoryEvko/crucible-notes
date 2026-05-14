# Pipeline Options Mapping

## Abstract

`TileIRPipelineOptions` is the configuration object that parameterizes the MLIR-tier pipeline. It is filled from the driver and from `--pass-pipeline="tileir{...}"` syntax, then read while building the pass manager. Every public option has a single consuming pass plus a well-defined access pattern: the pipeline builder either passes the option into the pass's constructor (compile-time binding) or attaches it as a module attribute the pass reads from inside its `runOnOperation` body (run-time binding). This page maps each option to its consumer and its access pattern, then describes the layered defaulting strategy that decides what each option holds when the user does not set it explicitly.

## Core Options

| Option | Type | Default | Used for |
| --- | --- | --- | --- |
| `num-warps` | integer | `4` | Warp count used by TileAA/TileAS scheduling and launch metadata. |
| `num-ctas` | integer | `1` | CTA count per cluster. |
| `compute-capability` | string | driver target | SM target such as `sm_100`, `sm_103`, `sm_110`, `sm_120`, or `sm_121`. |
| `opt-level` | integer | `2` | MLIR-tier optimization tier. |
| `v2-opt-level` | integer | `0` | Secondary TileAS scheduling/lowering axis. |
| `pipeline-strategy` | enum | `none` | Selects none, unspecialized, or warp-specialized pipeline behavior. |
| `index-bitwidth` | integer | `32` | Index type width used by LLVM conversion passes. |
| `unspecialized-pipeline-num-stages` | integer | `4` | Stage count for the unspecialized software pipeline path. |

## Math and Target Options

| Option | Type | Default | Used for |
| --- | --- | --- | --- |
| `approx` | boolean | `false` | Approximate math behavior in target conversion and NVVM reflection. |
| `ftz` | boolean | `false` | Flush-to-zero behavior for floating-point lowering. |
| `use-nvgpucomp-libnvvm` | boolean | `false` | Route target conversion through NVGpuComp/libNVVM integration. |
| `emit-line-info` | enum | `none` | Select the IR stage used for line-info snapshots. |

## Scheduler Options

| Option | Type | Default | Used for |
| --- | --- | --- | --- |
| `dynamic-persistent` | boolean | `false` | Enable dynamic persistent-kernel transformation. |
| `schedule-trace-file` | string | empty | Write a Chrome-style scheduler trace to the given path. |
| `enable-random-delay` | boolean | `false` | Stress-test scheduler ordering with random delays. |
| `rrt-size-threshold` | unsigned | `4096` | Resource-reservation-table compression threshold. |
| `max-constraint-iterations` | unsigned | `10` | Iteration cap for resource constraint generation. |

## Host Wrapper Options

| Option | Type | Default | Used for |
| --- | --- | --- | --- |
| `enable-debug-logging` | boolean | `false` | Enable extra host-wrapper logging. |
| `host-triple` | string | `native` | Target triple for generated host callback code. |
| `dump-host` | string | empty | Write generated host code to a file. |

## Option-to-Pass Map

Each option resolves to one or more consuming passes and a specific access pattern. "Constructor" means the pipeline builder reads the option and passes the value as a `PassOption` to the pass's factory; the pass then reads it through its own option field. "Module attribute" means the pipeline builder attaches the value to the `gpu.module` and the pass reads it through `op->getAttrOfType<...>` inside `runOnOperation`. "Both" means the pipeline builder writes the attribute and also wires the option through the pass constructor; this is used for options consumed both inside MLIR passes and across the MLIR-to-LLVM serialization boundary.

| Option | Consuming pass | Access pattern |
|---|---|---|
| `num-warps` | `convert-cudatile-to-tileaa`, [`tileas-generate-schedule-constraints`](../scheduler/schedule-constraint-attributes.md) | Both |
| `num-ctas` | `convert-cudatile-to-tileaa`, `tileir-gpu-module-prepare` | Module attribute |
| `compute-capability` | `convert-target-to-nvvm`, `tileir-post-nvvm-finalize` | Module attribute (via resolved `#nvvm.target`) |
| `opt-level` | Pipeline builder | Decides which passes are added |
| `v2-opt-level` | [`tileas-generate-schedule-constraints`](../scheduler/schedule-constraint-attributes.md), [`tileas-materialize-schedule`](../passes/tileas/async-pipeline-family.md#materialize-schedule) | Constructor |
| `pipeline-strategy` | Pipeline builder (gates warp-specialization adders) | Decides which passes are added |
| `index-bitwidth` | `convert-tileas-to-llvm`, `convert-to-llvm`, `convert-memref-to-llvm` | Constructor |
| `unspecialized-pipeline-num-stages` | [`unspecialized-pipeline`](../passes/tileas/async-pipeline-family.md) | Constructor |
| `approx` | `convert-target-to-nvvm` (NVVM reflect map) | Module attribute |
| `ftz` | `convert-target-to-nvvm` (NVVM reflect map) | Module attribute |
| `use-nvgpucomp-libnvvm` | Serialization driver | Read at serialize time |
| `emit-line-info` | Snapshot printers in O1 and O2 | Constructor (printer enable + tag) |
| `dynamic-persistent` | `tileir-gpu-module-prepare` | Module attribute |
| `schedule-trace-file` | [`DumpTraceImpl`](instrumentation-and-action-handler.md#pass-instrumentation-scopes) instrumentation | Read at instrumentation install |
| `enable-random-delay` | [`tileas-generate-schedule-constraints`](../scheduler/schedule-constraint-attributes.md) | Constructor |
| `rrt-size-threshold` | Pipeline builder + [`ResourceConstraintBuilder`](../scheduler/resource-constraint-builder-and-rrt.md) | Both |
| `max-constraint-iterations` | [`tileas-generate-schedule-constraints`](../scheduler/schedule-constraint-attributes.md) | Constructor |
| `enable-debug-logging` | `tileir-emit-host-wrapper` | Constructor |
| `host-triple` | `tileir-emit-host-wrapper` | Constructor |
| `dump-host` | `tileir-emit-host-wrapper` | Constructor |

## Pipeline Builder

The pipeline builder reads `opt-level`, `pipeline-strategy`, and `v2-opt-level` to decide which pass-list segments to append, then forwards the remaining options into the passes themselves. Two segments are conditional on `opt-level` (TileAS lowering for `>= 2`, full LLVM/NVVM conversion for `>= 3`); one is conditional on `pipeline-strategy` (warp-specialization adders); two are conditional on `emit-line-info` (snapshot printers).

```c
void populate_pipeline(PassManager &pm, const PipelineOptions &opts) {
    OpPassManager &gpu_pm = pm.nest<GpuModuleOp>();

    attach_target_attributes(pm, opts);

    add_frontend_segment(gpu_pm, opts);
    if (opts.emit_line_info == EmitLineInfo::Frontend) {
        add_snapshot_printer(gpu_pm, "after-frontend");
    }

    if (opts.opt_level >= 2) {
        add_tileas_lowering_segment(gpu_pm, opts);
    }
    if (opts.pipeline_strategy != PipelineStrategy::None) {
        add_warp_specialization_segment(gpu_pm, opts);
    }
    if (opts.emit_line_info == EmitLineInfo::TileasBoundary) {
        add_snapshot_printer(gpu_pm, "tileas-llvm-boundary");
    }
    if (opts.opt_level >= 3) {
        add_full_conversion_segment(gpu_pm, opts);
    }
}
```

The `attach_target_attributes` step is what turns the module-attribute access pattern into a real binding: it writes `compute-capability`, `num-ctas`, `approx`, `ftz`, and `dynamic-persistent` onto every `gpu.module` so that downstream passes pick them up uniformly.

## Defaulting Strategy

Defaults are layered. The driver applies command-line defaults first (its `opt-level` default is `3`, its `compute-capability` default points at the newest supported Blackwell SM). The pipeline-options parser applies its own defaults if the driver did not (its `opt-level` default is `2`, its `compute-capability` default is older). The TileGen front end applies a final tier of defaults for options the user never touches.

| Layer | Sets | Wins when |
|---|---|---|
| Driver CLI | `opt-level=3`, `compute-capability=<latest Blackwell>` | User invokes the `tileiras` binary directly. |
| `--pass-pipeline` parser | `opt-level=2`, `compute-capability=<older default>` | Pipeline is built from a textual `--pass-pipeline=tileir{...}` string with no driver wrapping. |
| TileGen front end | Scheduler-trace path, debug-logging flag | Driver did not set them and parser does not see them. |

Tests should set every option they care about explicitly because the two driver-vs-parser defaults disagree on `opt-level` and on target.

## Unconsumed Options

When an option is set but its consuming pass is not in the active pipeline (for example `unspecialized-pipeline-num-stages=8` is set but `pipeline-strategy=none` so the unspecialized pass is never added), the option is silently ignored. The pipeline builder does not emit a warning because the textual parser cannot distinguish a redundant option from a user-supplied override that will become relevant on a later pipeline rebuild. Driver invocations that combine incompatible flags should be rejected at the driver layer, not at the pipeline builder.

## Cross-References

[Driver Entry and Optimization Levels — Optimization Tiers](driver-and-opt-levels.md#optimization-tiers) explains how `opt-level` and `pipeline-strategy` decompose into the pass-list segments above. [Pass List by Optimization Level](full-pass-list-by-opt-level.md) names the passes each segment contains. [LLVM PassBuilder Registry](passbuilder-mega-registry.md) covers options consumed past the MLIR-to-LLVM boundary. [Driver Entry and Optimization Levels — Schedule Analysis Ordering](driver-and-opt-levels.md#schedule-analysis-ordering) covers the `ScheduleAnalysis` preservation contract that `rrt-size-threshold` and the scheduler options feed.
