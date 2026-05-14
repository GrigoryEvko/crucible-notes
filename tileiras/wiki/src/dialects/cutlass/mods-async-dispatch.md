# MODS Async Dispatch

## Abstract

`cutlass.tile_scheduler.mods_*` is a four-op sidecar family for the MODS (Multi-Op Dispatch) async-dispatch path used by CUTLASS-style persistent GEMM kernels. The four ops attach to the tile-scheduler boundary of a persistent mainloop: two mark the start and end of the steady-state pipeline, one reads the current SM id, and one inserts a runtime throttle point. None of the four moves data, computes a tile, or participates in producer/consumer synchronisation. They exist to give the persistent-kernel runtime an explicit handshake with the SM hardware that ordinary `cutlass.pipeline.*` and `cutlass.tile_scheduler.*` ops do not provide.

The family is small but the placement is exact. MODS ops appear inside `nv_tileas.async.exec` regions alongside the rest of the persistent mainloop's pipeline plumbing. They lower to single-instruction NVVM intrinsics plus one ABI side effect: the mainloop-start and mainloop-end probes also drop arrive/wait pairs against the cluster-wide barrier that coordinates the alternate async-call ABI MODS uses for cross-CTA progress reporting.

## Position in the cutlass Dialect

The cutlass dialect groups its thirty-eight ops into five families. Pipeline (twelve ops), tile_scheduler (eight ops), seq_bar (six ops), and block_striped (eight ops) cover the four large-scale orchestration concerns. The MODS family is the fifth, smaller cluster: four ops that target the same persistent-kernel structure the other tile_scheduler ops build, but for runtime reporting rather than work assignment.

| Op | Role | Side effect |
| --- | --- | --- |
| `cutlass.tile_scheduler.mods_report_mainloop_start` | Mark the entry into the persistent mainloop. | Cluster barrier arrive; optional timestamp read. |
| `cutlass.tile_scheduler.mods_report_mainloop_end` | Mark the exit from the persistent mainloop after pipeline drain. | Cluster barrier wait; optional timestamp read. |
| `cutlass.tile_scheduler.mods_report_smid` | Read the SM id assigned to the current CTA. | Special-register read. |
| `cutlass.tile_scheduler.mods_throttle` | Insert a backoff point to relieve hardware queue pressure. | Side-effecting throttle hook. |

Calling all four "telemetry" overstates what the start/end probes do. The probes carry the cluster handshake the MODS dispatch ABI relies on — the actual mainloop-completion signal — and removing them breaks the persistent kernel's progress contract, not just its diagnostic output.

## The Persistent-Kernel Setting

The MODS ops only make sense in the context of a persistent CUTLASS kernel. A persistent kernel launches one CTA per SM (or one cluster per SM tier) and walks an internal tile iterator until no work remains. The structure is:

1. Kernel entry → arrive at the persistent setup barrier.
2. Pipeline init (`cutlass.pipeline.init`, `cutlass.seq_bar.init`).
3. Tile-scheduler init (`cutlass.tile_scheduler.{streamk,static_persistent,data_parallel}`).
4. **`mods_report_mainloop_start`** → mainloop entry barrier.
5. Steady-state mainloop: per-tile producer/consumer handshake plus pipeline advance.
6. **`mods_report_mainloop_end`** → mainloop exit barrier after the pipeline tail drains.
7. Epilogue and kernel exit.

Steps 4 and 6 are the MODS-specific additions to a standard CUTLASS persistent kernel. They open and close the cluster-coordinated MODS execution window in between which the alternate dispatch ABI is active. Outside the window, the kernel behaves as an ordinary CUTLASS persistent kernel; inside the window, certain cross-CTA progress queries are valid that are not valid elsewhere.

`mods_report_smid` is independent — it can appear anywhere inside the window and lowers to a special-register read. `mods_throttle` is also window-internal but is conditionally emitted: the scheduler-state computation decides per-tile whether the pipeline depth is large enough that a throttle is worth inserting.

## Op Signatures

The four ops carry minimal operand bundles. Mainloop probes take a participant mask used to size the cluster-barrier arrive count, an `is_2cta_mma` flag that determines whether participants are CTAs or cluster halves, and an optional timestamp output. `mods_report_smid` takes no operands and returns one `i32`. `mods_throttle` takes one constant operand: the throttle profile to apply.

```mlir
%t_start = cutlass.tile_scheduler.mods_report_mainloop_start %sched, %participants
              {is_2cta_mma = true} : !cutlass.tile_scheduler<streamk>, i64 -> i64
%smid = cutlass.tile_scheduler.mods_report_smid : i32
cutlass.tile_scheduler.mods_throttle {profile = 1 : i32}
cutlass.tile_scheduler.mods_report_mainloop_end %sched, %participants, %t_start
              {is_2cta_mma = true} : !cutlass.tile_scheduler<streamk>, i64, i64 -> ()
```

The start probe optionally returns the timestamp value the end probe consumes. The two probes form a matched pair: the end probe verifies that the start probe it pairs with has the same `is_2cta_mma` flag and the same participant mask. Mismatch produces a verifier diagnostic rather than a runtime hang.

## Dispatch Model and Producer/Consumer Integration

MODS does not replace the producer/consumer protocol the rest of the cutlass dialect implements. It runs in parallel with it: ordinary `cutlass.pipeline.producer.acquire`, `cutlass.pipeline.producer.commit`, `cutlass.pipeline.consumer.wait`, and `cutlass.pipeline.consumer.release` continue to drive the per-stage mbarrier handshake inside the mainloop. MODS adds one outer layer on top, scoped to the entire mainloop:

```text
mods_report_mainloop_start                 ─┐
                                            │   cluster-scoped MODS dispatch window
  pipeline.init                             │
  for each tile:                            │
    producer.acquire / commit               │
    consumer.wait / release                 │
  pipeline.producer.tail                    │
                                            │
mods_report_mainloop_end                   ─┘
```

Inside the window, the alternate async-call ABI is active: a producer CTA can issue a progress query that another CTA in the same cluster will pick up at its next `mods_throttle` point. Outside the window, the same query is rejected. The verifier checks that no MODS-specific op (such as a `mods_throttle` with a profile that depends on cluster-wide state) appears outside a matched start/end pair.

The participant model passed to the start probe interacts with the cluster shape declared on the enclosing kernel. For single-CTA MMA, the participant mask is one bit per CTA in the cluster. For two-CTA MMA, the participant mask is one bit per cluster half, and the start probe's `is_2cta_mma` flag forces the lowering to read `cluster.ctarank` rather than `tid.x` when computing each participant's arrive contribution. The two paths produce different NVVM emit sets, not just different attribute values.

## NamedBarrier and mbarrier Integration

The mainloop-start probe and mainloop-end probe both touch the cluster barrier, not the per-stage mbarrier slots. The cluster barrier is allocated from the NamedBarrier pool by the same `cutlass.pipeline.init` / `cutlass.seq_bar.init` barrier-id allocator the rest of the dialect uses; MODS does not claim its own pool slots. The mainloop-start probe arrives on the cluster barrier with a count derived from the participant mask; the mainloop-end probe waits for the matching arrive count from every participant before continuing.

This is why removing the probes breaks the kernel rather than just its reporting. The cluster barrier participates in the persistent-kernel progress contract — without the matched arrive/wait pair, late CTAs can re-enter the mainloop while early CTAs have already finished, and the `mods_throttle` hooks downstream observe inconsistent cluster-wide state.

The per-stage mbarrier slots used by `cutlass.pipeline.*` are untouched by MODS. The two synchronization domains are separate by design: one coordinates producer/consumer agents within a CTA, the other coordinates progress across CTAs in a cluster. MODS sits at the cluster-scoped level alongside `cute_nvgpu.arch.cluster.barrier`, not at the per-stage level.

## Verifier Rules

The dialect contract for the MODS family covers four distinct checks:

1. **Matched probe pair.** Every `mods_report_mainloop_start` op must be paired with exactly one `mods_report_mainloop_end` op in the same enclosing `nv_tileas.async.exec` region. The pair must agree on the `is_2cta_mma` flag and on the participant mask.

2. **Window enclosure.** Any `mods_throttle` or `mods_report_smid` op appearing inside a region must lie between a matched start/end pair if its operand bundle depends on cluster-wide state. Smid reads (no cluster dependency) can appear anywhere; throttle ops with cluster-aware profiles cannot.

3. **Cluster shape coherence.** The participant mask passed to the start probe must agree with the cluster shape declared on the enclosing kernel's `gpu.module`. A 2x2 cluster with `is_2cta_mma = true` declares two participants; a 4x1 cluster with `is_2cta_mma = false` declares four. The verifier rejects participant masks that have more bits set than the cluster shape supports.

4. **Side-effect preservation.** MODS ops carry the `MemoryEffects::Write` side-effect on the cluster barrier. Optimizers that drop ops based on read/written-value analysis must observe this and leave the probes in place even when their return values appear unused.

The first three checks fire at op verify time. The fourth is a property of the op's memory-effect declaration and is enforced by the standard MLIR optimizer machinery rather than by a dedicated verifier.

```c
LogicalResult verify_mods_probe_pair(MlirOperation start, MlirOperation end) {
    require(start->is_2cta_mma == end->is_2cta_mma);
    require(start->participants == end->participants);
    require(same_async_exec_region(start, end));
    require(no_other_probe_op_between(start, end));
    return success();
}
```

## Lowering

The MODS lowering runs as part of `ConvertPipelineToNVVM` — the same pass that lowers `cutlass.pipeline.*` to mbarrier intrinsics. It is not a separate pass because MODS shares its barrier-allocation state with the rest of the cutlass dialect; running it later would require re-reading the NamedBarrier pool state the pipeline-init lowering already consumed.

| Op | Lowered emit set |
| --- | --- |
| `mods_report_mainloop_start` | `nvvm.cluster.arrive` + optional `nvvm.read.ptx.sreg.globaltimer` |
| `mods_report_mainloop_end` | `nvvm.cluster.wait` after `cp.async.bulk.wait_group { count = 0 }` drain + optional second `nvvm.read.ptx.sreg.globaltimer` and subtraction |
| `mods_report_smid` | `nvvm.read.ptx.sreg.smid` |
| `mods_throttle` | `nvvm.nanosleep` with profile-derived constant, or `nvvm.bar.sync` for the cooperative-throttle profile |

The `mods_report_mainloop_end` lowering is the most intricate. Before it can issue the cluster-barrier wait, it must drain any outstanding TMA stores from the pipeline tail; the lowering inserts a `cp.async.bulk.wait_group { count = 0 }` between the pipeline's `producer.tail` op and the cluster wait. This is one of the few places the cutlass-to-NVVM lowering inserts a wait that does not correspond directly to a `cutlass.pipeline.consumer.wait` op — it exists because the cluster barrier observes the full mainloop, not a single stage, and the tail drain is what makes that observation safe.

The `mods_throttle` lowering picks one of three profiles. Profile 0 is a no-op — the op is dropped during lowering when the surrounding pipeline depth analysis decides the kernel does not need a throttle. Profile 1 emits a fixed-duration `nvvm.nanosleep`. Profile 2 emits a cooperative-throttle path: the throttle becomes a participation point in a per-cluster barrier whose count adapts to current hardware queue pressure measured at runtime.

## Relationship to the cutlass C++ Library

The OSS CUTLASS library exposes a `cutlass::mods::*` namespace with `mainloop_begin`, `mainloop_end`, `report_smid`, and `throttle` helpers. The four MLIR ops correspond one-to-one with these helpers, but the IR shape collapses what the C++ surface presents as templated function calls into named ops with explicit operand bundles. The participant mask the C++ helpers compute at compile time from the `ClusterShape` template parameter becomes an explicit operand on the IR ops; the `is_2cta_mma` flag the C++ helpers derive from the `MMA::Tile` policy becomes an explicit attribute.

The same simplification the cutlass dialect applies to other CUTLASS templates applies here: template specialisation chains in `cutlass/mods/*.hpp` turn into op attributes the verifier cross-checks, and the runtime behavior is preserved through the dialect contract rather than through the C++ template instantiation surface.

## Invariants

- The four MODS ops appear only inside `nv_tileas.async.exec` regions, never at module or kernel scope.
- The mainloop-start and mainloop-end probes form matched pairs scoped to one async region.
- The pair agrees on `is_2cta_mma` and on the participant mask.
- The participant mask agrees with the cluster shape declared on the enclosing `gpu.module`.
- Cluster-aware throttle profiles appear only between a matched start/end pair.
- The probes' side effects on the cluster barrier are not removable by ordinary optimization passes.
- `mods_report_smid` and the profile-0 `mods_throttle` are the only MODS ops that can be safely dropped if their results are unused; the other three carry barrier-side-effect semantics that pin them in place.

## Cross-References

- [cutlass Dialect Overview — Operation Roster](overview.md#operation-roster) — the family roster and verifier inventory.
- [cutlass Pipeline and Tile Scheduler — Pipeline Model](pipeline-and-tile-scheduler.md#pipeline-model) — the producer/consumer protocol MODS runs alongside.
- [Cluster Sync and DSMEM Handshake — Plain Cluster Barrier](../../topics/cluster-sync-and-dsmem-handshake.md#plain-cluster-barrier) — the cluster-barrier mechanism the mainloop probes use.
- [mbarrier State Machine — State Machine](../../topics/mbarrier-state-machine.md#state-machine) — the per-stage barrier protocol MODS does not touch.
