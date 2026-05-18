# cutlass Dialect Overview

## Provenance vs Upstream MLIR

`cutlass` is NVIDIA-introduced and has no upstream MLIR counterpart. Upstream MLIR has no dialect that models CUTLASS-style asynchronous producer/consumer pipelines, persistent tile schedulers, sequence barriers, or block-striped shared-memory movement — the open-source CUTLASS library expresses all of this in C++ templates that the compiler instantiates per kernel. Tileiras lifts those template-time constructs into IR so the scheduler and the architecture-atom dialects can see them as ordinary ops with verifier-checked operands. Without this dialect, pipeline shape, scheduler kind, and barrier identity would have to be inferred from instantiation patterns rather than stated by the producer.

## Abstract

The `cutlass` dialect packs thirty-eight ops across four operation families plus a small MODS sidecar nested under tile_scheduler. It models the structure CUTLASS C++ templates normally generate: asynchronous producer/consumer pipelines, persistent tile schedulers, ordered sequence barriers, and block-striped shared-memory movement. The dialect constructor at `sub_1761D90` registers all thirty-eight ops in a single thunk-chain, then installs two op-level verifiers and the post-verify arrive-count builder.

`cutlass` sits above `cute_nvgpu` and `nv_tileas`. `cute_nvgpu` provides hardware atoms — MMA, TMA. `nv_tileas` provides operational async scheduling. `cutlass` connects the two at a larger granularity: it names which agents participate in the pipeline, how tiles are assigned to CTAs, how producers and consumers synchronise, and how persistent kernels advance through their work.

## Position in the Cascade

```text
cutlass
    |
    | lower pipeline, scheduler, barrier, and block-striped abstractions
    v
nv_tileas + cute + cute_nvgpu
    |
    | schedule, assign layouts, emit architecture atoms
    v
nvgpu + nvvm
    |
    | emit LLVM IR and PTX
    v
PTX
```

For users, `cutlass` is a frontend-oriented dialect — useful when the source program already has CUTLASS pipeline structure. For reimplementers, it is a bridge: preserve the CUTLASS semantics long enough to lower them into the scheduler and atom dialects without losing synchronisation or tile-scheduler intent.

## Operation Roster

The thirty-eight ops split into five families. Pipeline is the largest, covering the full producer/consumer state machine plus the per-CTA executor switch. Tile-scheduler carries one op per scheduler kind plus the per-variant accessors. Seq-bar and block-striped are smaller and more regular. The MODS async-dispatch family is a four-op sidecar wired to an alternate async-call ABI.

| Family | Count | Examples |
|---|---:|---|
| pipeline | 12 | `cutlass.pipeline.create`, `cutlass.pipeline.init`, `cutlass.pipeline.producer_acquire`, `cutlass.pipeline.producer_commit`, `cutlass.pipeline.producer_tail`, `cutlass.pipeline.consumer_wait`, `cutlass.pipeline.consumer_release`, `cutlass.pipeline.state.create`, `cutlass.pipeline.state.increment`, `cutlass.pipeline.consume`, `cutlass.pipeline.produce`, `cutlass.pipeline.switch_by_executor` |
| tile_scheduler | 8 | `cutlass.tile_scheduler.create_streamk_params`, `cutlass.tile_scheduler.create_static_persistent_params`, `cutlass.tile_scheduler.create_dp_params`, plus per-variant accessors (`work_tile_info_*`, `fetch_next_work`, `advance_to_next_work`, and the SM100 persistent-fixup hooks) |
| seq_bar | 5 | `cutlass.seq_bar.create`, `cutlass.seq_bar.init`, `cutlass.seq_bar.arrive`, `cutlass.seq_bar.wait`, `cutlass.seq_bar.state.create` |
| block_striped | 8 | `cutlass.block_striped.load`, `cutlass.block_striped.store`, `cutlass.block_striped.reduce`, plus five type-specialized forms covering the half, bfloat, packed, integer, and float variants |
| MODS (nested under tile_scheduler) | 4 | `cutlass.tile_scheduler.mods_report_mainloop_start`, `cutlass.tile_scheduler.mods_report_mainloop_end`, `cutlass.tile_scheduler.mods_report_smid`, `cutlass.tile_scheduler.mods_throttle` (four ops covering the alternate async-call ABI used by the MODS telemetry path) |

## Two Verifiers Carry Pipeline Correctness

Of the thirty-eight ops, only two carry non-trivial verifier code. The rest lean on type-system structural checks plus the operand-layout helpers below. Both non-trivial verifiers target the pipeline family and both gate the rest of the lowering pipeline.

`PipelineInitOp::verify` at `sub_1771F40` is a 3 406-byte routine that verifies the `cutlass.pipeline.init` op's operands match the declared pipeline shape. It reads `numStages` from the op attribute, checks that `numStages > 0`, then reads the participants list via `sub_172E930` and checks that its length matches `numProducers`. It reads the consumer list via `sub_172E940` and checks that its length matches `numConsumers`. It then checks that `barrier_id_base` falls within the per-CTA NamedBarrier pool `[0, 32)`, and that `producer_group_id` and `consumer_group_id` are distinct so producer and consumer groups do not overlap. The diagnostics it emits include `"cutlass.pipeline.init: invalid numStages"` and `"cutlass.pipeline.init: participants length mismatch"` among the per-field messages.

`PipelineSwitchByExecutorOp::verify` at `sub_1775780` is a 4 848-byte routine that verifies the `cutlass.pipeline.switch_by_executor` op's branch dispatch. It walks the executor-mode arms and checks that each arm has matching `numProducers` and `numConsumers` counts so the participant accounting is consistent across modes. It then checks that the per-arm participant lists are disjoint so no participant is double-counted across executor arms. It reads num_producers via `sub_172E930`, num_consumers via `sub_172E940`, the participant list via `sub_172E950`, and the executor mode via `sub_172E960`.

Both verifiers run before any pipeline lowering pass and gate the rest of the lowering pipeline. A malformed `cutlass.pipeline.init` or `cutlass.pipeline.switch_by_executor` never reaches the TileAS scheduler.

## Post-Verify Arrive-Count Builder

Once `PipelineInitOp::verify` passes, the post-verify builder at `sub_1772C90` computes the per-stage arrive-count and stamps it as a derived attribute on the op. The arrive-count is a function of the participants list length, the consumer count, and the executor-mode mask, and downstream lowering needs it on every per-stage emit path. The `ConvertPipelineToNVVM` pass reads the attribute on every per-stage emit and uses it to size the per-stage NamedBarrier arrive count. Without it, the lowering would have to recompute the count at every emit site by walking the same participant tables the verifier already read.

## Block-Striped Operand Checkers

Four operand-layout checkers serve the block-striped family, one per variant: `sub_176E670` for load, `sub_176EE10` for store, `sub_176F5B0` for reduce-add, `sub_176FD50` for reduce-max. Each one checks operand-layout compatibility with the per-CTA tile shape — register-memory operand width, global-memory pointer or memref shape, element-type width (at least sixteen bits), and static stripe shape. The checkers fire from the relevant op's `verify` thunk and reject malformed operand combinations before lowering picks a vector width and copy atom.

## Cutlass-Bar Warp-Cooperative Diagnostic

`BarOpLowering` at `sub_15FC250` is a ~5.5 KB routine that handles `cutlass.named_barrier.*` and `cutlass.generic_barrier.*` lowering and emits the warp-cooperative diagnostic. It fires when an arrive-count is not a multiple of warp size, or when the op sits outside warp-cooperative scope. The diagnostic catches the misuse pattern where a thread-level barrier lands in a kernel region the rest of the dialect expects to coordinate warps as a unit.

## Barrier-Id Helper

The barrier-id helper at `sub_1771850` allocates per-CTA NamedBarrier slots from the thirty-two-slot pool. Both `PipelineInitOp::verify` and `cutlass.seq_bar.init` call it to claim barrier IDs — the pool is the same physical resource on Hopper and Blackwell, so the helper is shared. Allocation order is deterministic and follows declaration order in the parent module, so two builds of the same IR produce the same barrier-id assignment.

## Pipeline Lowering

The central lowering takes CUTLASS pipeline objects to TileAS pipeline regions. The CUTLASS dialect models the state machine in the same terms as the C++ library; TileAS needs explicit producer and consumer regions, stage iterators, and token flow.

```c
void lower_cutlass_pipeline(CutlassPipeline pipeline) {
    NvTileAsPipeline as_pipeline = create_tileas_pipeline(
        pipeline.stage_count,
        pipeline.shared_storage,
        pipeline.producer_group,
        pipeline.consumer_group);

    for (CutlassPipelineOp op : pipeline.ops) {
        switch (op.role) {
        case PIPELINE_PRODUCER_ACQUIRE:
            replace_with_producer_acquire(as_pipeline, op.stage);
            break;

        case PIPELINE_PRODUCER_COMMIT:
            replace_with_producer_commit(as_pipeline, op.stage);
            break;

        case PIPELINE_CONSUMER_WAIT:
            replace_with_consumer_wait(as_pipeline, op.stage, op.consumer_idx);
            break;

        case PIPELINE_CONSUMER_RELEASE:
            replace_with_consumer_release(as_pipeline, op.stage);
            break;

        case PIPELINE_SWITCH_BY_EXECUTOR:
            replace_with_agent_switch(as_pipeline, op.executor);
            break;
        }
    }
}
```

The lowering preserves stage identity and executor identity. Lower a producer acquire/commit pair independently of its consumer wait/release pair without a shared pipeline object, and the scheduler can no longer prove they coordinate the same stage.

## Tile Scheduler Semantics

CUTLASS tile schedulers decide which CTA owns which tile of work. The dialect preserves both the scheduling policy and the current scheduler state. Data-parallel scheduling maps CTAs straight to tiles. StreamK and split-K scheduling bring partial work, fixup paths, and a reduction workspace. Static persistent scheduling keeps CTAs resident and hands them new tiles in sequence. SM100 scheduler forms layer target-specific persistent-scheduling details on top for Blackwell kernels.

```c
WorkTileInfo next_tile(TileScheduler *scheduler, CtaId cta) {
    switch (scheduler->kind) {
    case SCHEDULER_DATA_PARALLEL:
        return data_parallel_tile_for_cta(scheduler, cta);

    case SCHEDULER_STREAM_K:
        return stream_k_next_tile(scheduler, cta);

    case SCHEDULER_STATIC_PERSISTENT:
        return persistent_next_tile(scheduler, cta);

    case SCHEDULER_SM100:
        return sm100_next_tile_with_fixup(scheduler, cta);
    }
}
```

The work-tile-info value is not a convenience wrapper. Downstream code derives problem coordinates, mainloop bounds, reduction participation, and epilogue fixup behaviour from it.

## If You Know CUTLASS (open source) — cross-walk

The `cutlass` dialect is the IR shape of the orchestration classes living in `cutlass/pipeline/*.hpp`, `cutlass/gemm/kernel/tile_scheduler/*.hpp`, `cutlass/arch/barrier.h`, and the related epilogue plumbing.

| CUTLASS C++ class / template | tileiras IR (`cutlass.*`) |
|---|---|
| `PipelineTmaAsync<Stages>`, `PipelineAsync<Stages>` | `cutlass.pipeline.create` + `cutlass.pipeline.init` with `numStages`/`numProducers`/`numConsumers` attrs |
| `PipelineState<Stages>` member tuple | `!cutlass.pipeline_state` typed value (phase, index, count) |
| `producer_acquire / commit / tail` | `cutlass.pipeline.producer_{acquire,commit,tail}` ops |
| `consumer_wait / release` | `cutlass.pipeline.consumer_{wait,release}` ops |
| Warp-specialized executor partition | `cutlass.pipeline.switch_by_executor` |
| `OrderedSequenceBarrier<Stages, ...>` | `cutlass.seq_bar.{create,init,arrive,wait,state.create}` (five-op family) |
| `arch::NamedBarrier::sync(id, threads)` | `cutlass.named_barrier.arrive`, `cutlass.named_barrier.arrive_and_wait`, `cutlass.generic_barrier.{arrive_increment,wait_eq,wait_less_than}`, `cutlass.generic_barrier_sync` (warp-cooperative-only; gated by the `BarOpLowering` diagnostic) |
| `PersistentTileScheduler` | `cutlass.tile_scheduler.create_static_persistent_params` (with companion `create_static_persistent_work_tile_info`) |
| `StreamKScheduler` | `cutlass.tile_scheduler.create_streamk_params` (with companion `create_streamk_work_tile_info`; SM100 variant body `sub_R01`) |
| `DataParallelScheduler` | `cutlass.tile_scheduler.create_dp_params` (with companion `create_dp_work_tile_info`) |
| `BlockStriped<T>::load/store/reduce` | `cutlass.block_striped.{load,store,reduce}` (eight-op family) |
| `MODS` telemetry hooks (`cutlass::mods::*`) | `cutlass.tile_scheduler.mods_*` ops (side-effecting) |

Two structural points. First, most of CUTLASS's class-template instantiations turn into op attributes on a small set of ops, so a kernel using three pipelines and two schedulers is described by a few dozen ops rather than by template specialisations in a thousand-line header. Second, the participant model — producers, consumers, warp-specialized executors — lives in explicit lists on the init op, cross-checked by `PipelineInitOp::verify` at `sub_1771F40` before the lowering pass ever runs.

## Cross-links

- [Pipeline and Tile Scheduler — Pipeline Model](pipeline-and-tile-scheduler.md#pipeline-model) covers pipeline roles and [Tile Scheduler Kinds](pipeline-and-tile-scheduler.md#tile-scheduler-kinds) persistent tile scheduling.
- [Seq Bar and Block Striped — Sequential Barrier Model](seq-bar-and-block-striped.md#sequential-barrier-model) covers sequence barriers and [Block-Striped Model](seq-bar-and-block-striped.md#block-striped-model) movement.
- [MODS Async Dispatch — Dispatch Model and Producer/Consumer Integration](mods-async-dispatch.md#dispatch-model-and-producerconsumer-integration) covers telemetry and async-dispatch operations.
