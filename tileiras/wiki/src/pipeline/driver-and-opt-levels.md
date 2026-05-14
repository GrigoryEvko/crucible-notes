# Driver Entry and Optimization Levels

## Abstract

The Tileiras driver builds one MLIR pass pipeline for each compilation. That pipeline is selected from
the user configuration, target, optimization level, and pipeline strategy. The key idea is staged
lowering: the driver first loads TileIR bytecode into MLIR, then lowers `cuda_tile` through TileAA and
TileAS, then lowers to LLVM/NVVM, and finally hands the module to the NVPTX backend.

## Entry Chain

```c
int compile_tileir(ByteSpan input, TileirasConfig config, ByteBuffer *output) {
    Context ctx = create_mlir_context();
    register_tileiras_dialects(&ctx);

    Module module = parse_tileir_bytecode(&ctx, input);
    if (!module.valid) {
        return TILEIR_ERROR_BAD_BYTECODE;
    }

    PipelineOptions opts = make_pipeline_options(config);
    PassManager pm = build_tileir_pipeline(&ctx, opts);

    if (failed(run_pass_manager(pm, module))) {
        return TILEIR_ERROR_COMPILE_FAILED;
    }

    return serialize_gpu_module(module, config, output);
}
```

Pipeline construction finishes before the pass manager runs. This keeps option decoding, pass
selection, and compile execution separate.

## Optimization Tiers

| Tier | Role | Typical use |
| --- | --- | --- |
| `O0` | Verifier-only skeleton. | Debugging bytecode ingestion and early IR validity. |
| `O1` | Frontend conversion and light cleanup. | Fast checks of `cuda_tile` to TileAA lowering. |
| `O2` | Default TileIR lowering through TileAS and first LLVM/NVGPU conversions. | Normal compilation. |
| `O3` | Full conversion stack, extra canonicalization, target finalization, and debug-scope synthesis. | Highest quality output and late-stage validation. |

`v2-opt-level` is a second axis. The primary `opt-level` selects the intended tier, while
`v2-opt-level` can suppress or enable the newer TileAS scheduling and specialization stages. A driver
implementation should make this explicit instead of treating it as a hidden copy of `opt-level`.

The recovered dispatcher uses the following effective structure:

| Requested tier | Base adders | Extra behavior |
| --- | --- | --- |
| `O0` | none | Only automatic verifier slots run. |
| `O1` | frontend adder | Convert `cuda_tile` to TileAA, insert debug scopes, canonicalize. |
| `O2` | frontend + TileAS adder | Add TileAA-to-TileAS, host wrapper, TileAS-to-LLVM, CSE, TileAS-to-NVGPU. |
| `O3` | `O2` + full conversion adder | Add TileIR verification, LLVM conversion, NVGPU/NVVM conversion, finalization. |

Two snapshot printers are conditional. The first is tied to the early frontend stage; the second is
tied to the TileAS/LLVM boundary. They are diagnostics, not semantic lowering passes.

## Pipeline Strategy

`pipeline-strategy` selects how aggressively the compiler specializes producer/consumer execution.

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

TileAS scheduling is split into two phases:

1. Generate schedule constraints.
2. Materialize a solved schedule into IR.

The phases must remain separate because intermediate passes may preserve and refine the analysis. A
pass that mutates schedule constraints must run before materialization.

```c
void run_schedule_pipeline(Function fn) {
    ScheduleAnalysis analysis = generate_schedule_constraints(fn);
    preserve_analysis(fn, analysis);

    run_allowed_cleanup_passes(fn);

    ScheduleAnalysis recovered = require_preserved_schedule(fn);
    Schedule solved = solve_schedule(recovered);
    materialize_schedule(fn, solved);
}
```

If the analysis is invalidated, materialization should fail clearly or skip with an explicit
diagnostic. Silent no-op materialization makes schedule bugs hard to trace.

## Serialization Scopes

Two outer instrumentation scopes are useful for profilers and callback integrations:

| Scope | Covers |
| --- | --- |
| `CompileNVVM` | Running the MLIR-to-NVVM/NVPTX compilation pipeline. |
| `SerializeGPUModule` | Translating the GPU module to PTX/cubin and invoking downstream tools. |

Keep these scopes coarse and stable. Fine-grained pass scopes can change, but external profilers need
durable outer names.

