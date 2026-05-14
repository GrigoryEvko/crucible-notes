# Pipeline Options Mapping

## Abstract

`TileIRPipelineOptions` is the configuration object that parameterizes the MLIR-tier pipeline. It is
filled from the driver and from `--pass-pipeline="tileir{...}"` syntax, then read while building the
pass manager. This page maps each public option to the behavior it controls.

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

## Propagation Model

Options are read in two ways. Driver-level options decide which passes are added. Pass-local options
are forwarded into pass constructors or registered again by the pass itself.

```c
void build_pipeline(PassManager *pm, PipelineOptions opts) {
    add_frontend_passes(pm, opts.num_warps, opts.num_ctas, opts.compute_capability);

    if (opts.opt_level >= 2) {
        add_tileas_lowering(pm, opts);
    }

    if (opts.pipeline_strategy == PIPELINE_WARP_SPECIALIZE) {
        add_warp_specialization(pm, opts.rrt_size_threshold, opts.max_constraint_iterations);
    }

    if (opts.opt_level >= 3) {
        add_full_llvm_nvvm_conversion(pm, opts);
    }
}
```

## Cross-Tier Notes

The command-line driver and the MLIR pass-pipeline parser both expose names such as `opt-level` and
`compute-capability`. A normal `tileiras` invocation should resolve these through the driver first and
then populate pipeline options consistently. Direct `--pass-pipeline` use can bypass driver defaults,
so tests should set target and opt-level explicitly when they construct pipelines by text.

The recovered defaults differ by layer: the driver-level optimization default is `3`, while the
pipeline-options default is `2`; the driver-level compute capability defaults to a Blackwell target,
while the pipeline parser's standalone default is older. Public tests should avoid relying on either
fallback and should pass both values explicitly.

## Reimplementation Checklist

1. Keep one typed options object for pipeline construction.
2. Validate enum values before building the pass manager.
3. Make `compute-capability` explicit in tests that bypass the driver.
4. Treat `opt-level` and `v2-opt-level` independently.
5. Forward `index-bitwidth` to all LLVM conversion passes that need it.
6. Gate scheduler tracing on a non-empty output path.
7. Keep host-wrapper options separate from device lowering options.
8. Document every option's default and owner pass.
