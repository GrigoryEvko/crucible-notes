# MODS Async Dispatch

## Abstract

The MODS async-dispatch path is the diagnostic and scheduling bridge for CUTLASS-shaped persistent kernels. It records mainloop telemetry, SM-id reporting, throttling, async opcode counts, and pipeline role information into a schedule descriptor that later materialisation consumes. The telemetry operations are side-effecting by design — optimisers must not erase or merge them, since they drive runtime characterisation and schedule debugging.

## Telemetry Operations

| Operation | Contract |
|---|---|
| `cutlass.tile_scheduler.mods_report_mainloop_start` | Mark or sample the beginning of the persistent mainloop. |
| `cutlass.tile_scheduler.mods_report_mainloop_end` | Mark or sample the end of the persistent mainloop after pipeline drain. |
| `cutlass.tile_scheduler.mods_report_smid` | Record the current SM id. |
| `cutlass.tile_scheduler.mods_throttle` | Insert a runtime throttle or backoff point. |

The two mainloop probes may carry `is_2cta_mma`. If one probe says the mainloop uses two-CTA MMA and the other does not, the verifier rejects the pair.

```c
LogicalResult verify_mods_probe_pair(ModsStartOp start, ModsEndOp end) {
    require(start.is_2cta_mma == end.is_2cta_mma);
    require(start.encloses_same_mainloop_as(end));
    return success();
}
```

## Async Dispatch Opcodes

The dispatch layer classifies async units into a fixed opcode enum. The semantic groups worth knowing are:

| Group | Examples |
|---|---|
| TMA movement | TMA load, TMA store, TMA reduce, two-CTA TMA load. |
| MMA | WMMA, GMMA, UMMA, two-CTA UMMA, dense-persistent MMA. |
| Matrix copies | LDSM, STSM, LDTM, STTM. |
| Global/shared copies | LDS, STS, LDG, STG, LDGSTS, block copy. |
| Work scheduling | WorkID query, pipeline control, throttle. |
| Utility copies | UTC copy and two-CTA UTC copy. |

```c
AsyncDispatchOpcode classify_async_op(Operation op) {
    if (is_tma_load(op)) {
        return op.is_2cta ? TMALDG_2CTA : TMALDG;
    }

    if (is_umma(op)) {
        return op.is_2cta ? UMMA_2CTA : UMMA;
    }

    if (is_workid_query(op)) {
        return WORKID_QUERY;
    }

    if (is_mods_throttle(op)) {
        return THROTTLE;
    }

    return classify_copy_or_mma(op);
}
```

## Schedule Descriptor

Model the schedule descriptor as structured data:

```c
typedef struct {
    Operation owner;
    DenseMap<Operation, int> stage;
    DenseMap<Operation, int> order;
    bool is_2cta_mma;
    SmallVector<ModsProbe> probes;
    SmallVector<uint64_t> opcode_counts;
    SmallVector<PipeRecord> pipes;
    Digest digest;
} ModsScheduleDescriptor;
```

The descriptor carries:

- the owning async execution region;
- stage and order maps for scheduled operations;
- the cumulative two-CTA MMA gate;
- telemetry probe records;
- async-dispatch opcode counts;
- producer and consumer pipe records;
- a digest or version marker so stale descriptors can be detected.

Do not treat the descriptor as an anonymous byte blob. Named fields make verifier and materialiser behaviour testable.

## Builder Algorithm

```c
ModsScheduleDescriptor build_mods_descriptor(AsyncExecOp async_exec) {
    ModsScheduleDescriptor desc;
    desc.owner = async_exec;

    for (Operation op : walk_pipeline_ops(async_exec)) {
        AsyncDispatchOpcode opcode = classify_async_op(op);
        desc.opcode_counts[opcode] += 1;

        if (is_mods_probe(op)) {
            desc.probes.push(make_probe_record(op));
            desc.is_2cta_mma |= probe_is_2cta(op);
        }

        if (is_pipeline_role(op)) {
            desc.pipes.push(make_pipe_record(op));
        }
    }

    desc.stage = compute_stage_map(async_exec);
    desc.order = compute_order_map(async_exec);
    desc.digest = digest_descriptor(desc);
    return desc;
}
```

The builder walks the pipeline in deterministic program order. If two equivalent modules produce different descriptors, schedule caching and replay become brittle.

## Integration With Scheduling

The MODS descriptor sits next to the schedule data both serial and cost-based schedule generation use. The flow is:

1. Generate or refine a schedule for an async execution region.
2. Solve stage/order constraints.
3. Emit pipe records for producer and consumer roles.
4. Build the MODS descriptor from the solved schedule.
5. Materialize pipeline roles, mutexes, async execution, and telemetry.

```c
void materialize_scheduled_async_exec(AsyncExecOp op) {
    Schedule schedule = solve_schedule(op);
    ModsScheduleDescriptor mods = build_mods_descriptor(op);

    for (PipeRecord pipe : mods.pipes) {
        materialize_pipe(schedule, pipe);
    }

    for (ModsProbe probe : mods.probes) {
        materialize_probe(probe, mods.is_2cta_mma);
    }
}
```

Telemetry probes stay side-effecting throughout this flow. Removing them changes the descriptor and may change runtime characterisation.

## Two-CTA Gate

The `is_2cta_mma` flag changes participant masks, cluster-CTA-rank reads, and which async opcode variants get counted. Treat it as a schedule-level property, not a cosmetic telemetry bit.

```c
TargetParticipantModel participant_model(bool is_2cta_mma) {
    if (is_2cta_mma) {
        return (TargetParticipantModel){
            .uses_cluster_cta_rank = true,
            .cta_count = 2,
        };
    }

    return (TargetParticipantModel){
        .uses_cluster_cta_rank = false,
        .cta_count = 1,
    };
}
```

## Lowering Shape

Typical materialization:

- `mods_report_smid` lowers to an SM-id special-register read;
- mainloop start/end probes lower to clock or timestamp reads;
- `mods_throttle` lowers to a side-effecting throttle or barrier-like hook;
- opcode counts and pipe records remain in schedule metadata;
- two-CTA gate selects cluster-aware participant behavior.

## Invariants

- MODS telemetry ops are side-effecting.
- Mainloop start and end probes agree on `is_2cta_mma`.
- Async op classification is deterministic.
- Descriptor fields are named and versioned or digested.
- Stage and order maps match the solved schedule.
- Two-CTA state changes participant selection and opcode classification.
- Materialization consumes the same schedule that produced the descriptor.

