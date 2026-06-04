# LatencyHidingScheduler Core

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. All `.text` and `.rodata` addresses are virtual; for this binary `.text` VMA == file offset `0xe63c000` and `.rodata` VMA == file offset `0x84a0000`.*

## Abstract

`LatencyHidingScheduler` (LHS) is the list scheduler that re-orders an already-memory-minimized HLO instruction sequence so that long-latency *async* work — collective starts, host/ICI DMA, async copies — is issued early and its latency is hidden under independent compute. It does **not** build a schedule from scratch: a base schedule already exists (produced by `HloMemorySchedulerWithBrkgaFallback`, the LHS `has_schedule()` precondition), and LHS replaces it with one that overlaps async edges. The work is delegated to a `DefaultSchedulerCore` per computation; the TPU build wires in a `TpuAsyncTracker` that models which physical resource each async op consumes.

The reader who knows LLVM should hold one analogy and one divergence. The analogy: `DefaultSchedulerCore::ScheduleComputation` is a classic critical-path list scheduler — a ready-set drain where, at each step, every DAG-ready node is a candidate and a multi-key comparator picks one. The divergence: this scheduler builds the sequence **in reverse** (bottom-up by a `current_time` clock that *decreases* as it walks back from the roots), the priority is dominated by *async critical-path depth and height* rather than plain dependency height, and the comparator interleaves a per-candidate **memory-peak budget gate** with the latency-hiding keys, so the same comparator that hides latency can be driven to trade overlap for lower HBM peak when the budget is tight.

This page is the Part VIII anchor for the core algorithm: the `ScheduleComputation` drain loop and its `SchedulingStep`, the 22-key `ReadySetLt` candidate comparator (with the async-depth / async-height priority and the `max(0, current_time − ready_time)` latency-hiding term at its center), the `InitializeGraphAnalysis` pass that computes the per-node depth/height the priority reads, and how the `TpuAsyncTracker` resource model decides which async ops can overlap. The pipeline-placement variants (`post_layout`, `post_layout_pre_fusion`, the ILP swap) get their own pages and are linked below; the per-node cost the comparator consumes comes from the [bundle-aware cost model](../cost/bundle-aware-cost.md).

For reimplementation, the contract is:

- **The drain loop is `while (ready_set || pending) { SchedulingStep; }`** — `SchedulingStep` extracts the single best ready node via `FindAndExtractBestNodeAvailable`, appends it, and advances `current_time` by the node's bundle cost. The sequence is reversed at the end.
- **The priority is a 22-key ordered comparator (`ReadySetLt`)** — force-flags first, then the memory-peak budget gate, then the async-overlap keys (schedule-async-start/done, resource-conflict, async-**depth**, async-**height**, unlock-start/done), then ready-node fan-out, then a stable original-order tie-break. The first key that distinguishes two candidates wins.
- **Critical-path depth/height are precomputed by `InitializeGraphAnalysis`** — each node's `[+0x38]` async depth is a `max` over predecessors, its `[+0x40]` async height a `max`/`+` over successors. The comparator never recomputes them; it reads the fields.
- **The latency-hiding term is `remaining = max(0.0, current_time − node.ready_time)`** — computed inline (`vsubsd` of `current_time` at state `+0x138` minus node `+0x28`, clamped at 0 by `vmaxsd`). A node whose operands have not yet "retired" (its `ready_time` is still ahead of the clock) is penalized; this is the dependency stall surfacing at scheduling time.
- **Async overlap is gated per physical resource by `TpuAsyncTracker`** — a 0..46 `TpuResourceType` enum (base OSS collective classes + TPU ICI directions + host-DMA + SparseCore + VMEM + DCN) with per-resource hazard classes; two async ops overlap only if they do not contend on the same hazardous resource.

| | |
|---|---|
| **Per-computation entry (thin)** | `DefaultSchedulerCore::ScheduleComputation(comp)` @ `0x1362e920` — builds state via vtable `+24`, force-delay RE2 gate, dispatches vtable `+40` |
| **Drain-loop worker** | `DefaultSchedulerCore::ScheduleComputation(comp, state)` @ `0x1362eb60` (LHS src 3633–3783) |
| **One step** | `DefaultSchedulerCore::SchedulingStep(state)` @ `0x1362d480` (LHS src 3466–3471) |
| **Candidate select / comparator** | `DefaultSchedulerCore::FindAndExtractBestNodeAvailable` @ `0x13618880` (2458-line decompile; 22 `ReadySetLt` keys) |
| **Depth/height precompute** | `HloScheduleGraph::InitializeGraphAnalysis` @ `0x1362a860` (1264 lines) |
| **Async resource model** | `TpuAsyncTracker::GetResourceName` @ `0x10fff420`; `GetResourceHazardType` @ `0x110015e0` |
| **Per-node cost source** | `CostModelLatencyEstimator::GetLatencyBetween` @ `0x10ff8f00` → bundle `MaxResourceCycles` (see [Bundle-Aware Cost](../cost/bundle-aware-cost.md)) |
| **Mem-budget retry driver** | `LatencyHidingScheduler::RunImpl` @ `0x136321a0` (LHS src 4182); relax factor `qword_A2DFD10` = `0.9` |
| **State offsets** | `ready_set` @ `state+208` (qw 26); `pending` @ `state+504` (qw 63); `current_time` (double) @ `state+0x138` |
| **Source file** | `third_party/tensorflow/compiler/xla/service/latency_hiding_scheduler.cc` (line refs in `.rodata`) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Drain Loop — `ScheduleComputation`

### Purpose

`ScheduleComputation` schedules a single `HloComputation`. The public per-computation entry (`0x1362e920`) is a thin wrapper that builds the per-computation `SchedulingState`, applies the `force_delay` / scheduling-group frontend-attribute gate, and forwards to the worker (`0x1362eb60`) that runs the actual drain loop. The worker is the heart of the scheduler: it drains a ready-set until every instruction is placed, then flips the reversed sequence and (optionally) captures a `ScheduleProto`.

### Entry Point

```text
DefaultSchedulerCore::ScheduleComputation(comp)        0x1362e920   ── thin wrapper
  ├─ vtable+24  MakeSchedulingState(comp)              0x1362e620   ── per-comp state
  │     └─ RE2::RE2 / RE2::PartialMatchN               (src 76-79)  ── force_delay / scheduling-group attr match
  └─ vtable+40  ScheduleComputation(comp, state)       0x1362eb60   ── worker, the drain loop
        ├─ HloScheduleGraph::InitializeGraphAnalysis    0x1362a860   ── precompute depth/height
        ├─ MemoryPressureTracker::Reset                              ── seed pressure tracker
        ├─ FindTopRoots / FindBottomRoots                            ── seed ready_set (roots first)
        ├─ [loop] SchedulingStep(state)                 0x1362d480
        └─ ComputationScheduleToProto                   0x13630780   ── optional capture (src 3783)
```

The thin wrapper dispatches through the `DefaultSchedulerCore` vtable so the SparseCore variants (`SparseCoreSchedulerCore`, `SparseCoreCostModelSchedulerCore`) can substitute their own `MakeSchedulingState` / worker while reusing the same `SchedulingStep` machinery. The vtable slots are read literally at `0x1362e920`: `(*(...)(*(_QWORD*)core + 24))(...)` builds the state and `(*(...)(*(_QWORD*)core + 40))(...)` runs the worker.

### Algorithm

```c
// DefaultSchedulerCore::ScheduleComputation(comp, state)  @ 0x1362eb60  (LHS src 3633-3783)
StatusOr<HloInstructionSequence>
ScheduleComputation(const HloComputation *comp, shared_ptr<SchedulingState> st) {
    CHECK(st != nullptr);                                    // src 3649 "sched_state != nullptr"
    HloScheduleGraph *g = &st->sched_graph;                  // state+8
    g->InitializeGraphAnalysis();                            // 0x1362a860 — fills depth/height (+0x38/+0x40)
    g->ResetScheduling();
    MemoryPressureTracker::Reset(st->pressure_tracker /*qw51*/, comp,
                                 ModulePressureState::GetPressureStateForComputation(...));

    // seed the ready set with the DAG roots; bottom-up by default, top-down if
    // st->schedule_top_down (byte +440) == 1.
    if (st->schedule_top_down /*byte+440*/) g->FindTopRoots(&roots);     // src ~3700
    else                                    g->FindBottomRoots(&roots);
    for (HloGraphNode *r : roots) {
        r->ready_time = 0;                                   // node[+0x28] = 0   (root)
        st->ready_set.push_back(r);                          // unless force-delayed → nodes_pending
    }
    VLOG(5) << "Initial memory pressure for " << comp << ": " << peak;   // src 3717

    // ---- THE DRAIN LOOP ----  (src 3719)
    while (st->ready_set_size /*qw26 @ +208*/ || st->pending_size /*qw63 @ +504*/) {
        VLOG(10) << "Current ready time: " << st->current_time;          // src 3721 (state+0x138)
        VLOG(2)  << "Current ready queue:";                             // src 3722
        // first try the non-annotated ops; if none can be placed, retry an
        // annotation group with minimum resources (TryScheduleOneAnnotationGroup).
        Status s = TryScheduleOneAnnotationGroup(comp, st, /*use_max=*/1);// src ~3732
        if (!s.ok()) {
            VLOG(3) << "Failed to schedule any non-annotated ops, trying again "
                       "with minimum resources for annotation groups";   // src 3745
            s = TryScheduleOneAnnotationGroup(comp, st, /*use_max=*/0);   // src ~3755
            TF_RETURN_IF_ERROR(s);
        }
    }

    if (st->schedule_top_down /*byte+440*/ == 0)
        reverse(st->new_sequence_reversed);                  // built reversed → flip to program order
    CHECK(st->new_sequence_reversed.size() ==
          g->GetOriginalInstrList().size());                 // src 3779 "Not all instructions ... scheduled"

    if (st->capture_schedule_proto /*byte+376*/)
        *captured += ComputationScheduleToProto(comp, *st, *latency_estimator);  // src 3783
    return st->new_sequence;
}
```

`TryScheduleOneAnnotationGroup` is the per-iteration body that ultimately calls `SchedulingStep`; its two-attempt structure (max-resources then min-resources for annotation groups) is the loop's only retry. The drain condition is read byte-exactly at `0x1362eb60` line 539: `while ( *((_QWORD *)state + 26) || *((_QWORD *)state + 63) )` — qword index 26 is `state+208` (the ready-set size) and index 63 is `state+504` (the annotation-pending count). The terminal `CHECK` at src 3779 compares `new_sequence_reversed.size()` against the original instruction count and FATALs `"Not all instructions have been scheduled <n> vs <m>"` if they differ.

> **QUIRK — the schedule is built in reverse.** By default the ready-set is seeded from `FindBottomRoots` (the computation's *outputs*) and walked backward; `current_time` starts at 0 at the roots and *increases* as the scheduler moves toward the inputs. The emitted `new_sequence_reversed` is then flipped to program order at the end. A reimplementation that seeds from the inputs and walks forward will produce a mirror-image priority and hide latency on the wrong side of every async edge. The direction is the `byte+440` flag (`schedule_top_down`); when set (annotation-group mode), `FindTopRoots` is used instead and the reverse is skipped.

### Function Map

| Function | Address | Role |
|---|---|---|
| `ScheduleComputation(comp)` | `0x1362e920` | thin wrapper: state build + force-delay RE2 gate + vtable dispatch |
| `ScheduleComputation(comp, state)` | `0x1362eb60` | worker: the drain loop (src 3633–3783) |
| `MakeSchedulingState` | `0x1362e620` | per-computation `SchedulingState` ctor (vtable `+24`) |
| `InitializeScheduler` | `0x1362c180` | one-time scheduler init |
| `TryScheduleOneAnnotationGroup` | `0x1362e360` | per-iteration body; max- then min-resource retry |
| `ComputationScheduleToProto` | `0x13630780` | optional schedule-dump populator (src 3783) |

---

## One Step — `SchedulingStep`

### Purpose

`SchedulingStep` performs exactly one placement: pick the best ready node, append it, advance the clock. It is the indirection point at which the comparator (`FindAndExtractBestNodeAvailable`) and the node-placement logic (`ScheduleNode`) are invoked through the core vtable, so a subclass can override either half.

### Algorithm

```c
// DefaultSchedulerCore::SchedulingStep(state)  @ 0x1362d480  (LHS src 3466-3471)
Status SchedulingStep(SchedulingState *st) {
    HloGraphNode *node = nullptr;
    // vtable+128 : FindAndExtractBestNodeAvailable(state, pred = empty)
    //   picks the single best ready node by the ReadySetLt comparator and POPS
    //   it out of ready_set (swap-with-last + shrink).
    Status s = core->vtable[+128](&result, core, st, /*pred=*/{});       // 0x13618880
    node = result.node;
    TF_RETURN_IF_ERROR_WITH_SOURCE(s, src 3466);
    CHECK(node != nullptr);                                              // src 3467 "node != nullptr"

    // vtable+112 : ScheduleNode(node, state)
    //   appends node to new_sequence_reversed, advances current_time by the
    //   node's bundle NodeCost, updates the MemoryPressureTracker, releases the
    //   node's resources, and pushes newly-ready successors into ready_set.
    //   The new clock value comes back in xmm0 (var_38).
    double new_time = core->vtable[+112](&done, core, node, st);         // ScheduleNode
    TF_RETURN_IF_ERROR_WITH_SOURCE(done, src 3469);
    st->current_time = new_time;                                        // state+0x138  (src 3470)

    VLOG(2) << "Scheduled: " << node->name();                           // src 3470
    VLOG(5) << node->ToString();                                        // src 3471
    return OkStatus();
}
```

The two vtable indirections are read literally at `0x1362d480`: `(*(...)(*(_QWORD*)core + 128))(...)` is the candidate selector and `(*(...)(*(_QWORD*)core + 112))(...)` is `ScheduleNode`. The clock write-back is the `vmovsd qword ptr [r14+138h], xmm0` at line 60 — `current_time` lives at `state+0x138` and is the only mutable scalar the comparator reads on the next step.

> **NOTE — `ScheduleNode` (vtable `+112`) is not decompiled line-by-line here.** Its effects are inferred from the comparator's reads and the `current_time` write-back: it advances the clock by the node's bundle cost ([Bundle-Aware Cost](../cost/bundle-aware-cost.md)), mutates the `MemoryPressureTracker`, and refreshes `ready_set` with successors whose last predecessor just retired. The resource-occupier bookkeeping it drives lives in `AddOccupierToResource` (`0x1361dc00`) / `DeleteOccupierFromResource` (`0x1361d9c0`) and is the dynamic state that the comparator's resource-conflict and release keys read on subsequent steps. Treat the clock-advance and ready-set refresh as CONFIRMED (the write-back is byte-anchored); the per-resource occupier push/pop is HIGH (inferred from the comparator's reads).

---

## Critical-Path Depth and Height — `InitializeGraphAnalysis`

### Purpose

The comparator's strongest non-force, non-memory keys are **async depth** and **async height** — the critical-path distance of a node from the async leaves and to the async roots, measured in cost units. These are *not* recomputed per comparison; they are written once into each `HloGraphNode` by `InitializeGraphAnalysis` (`0x1362a860`) before the drain loop begins, and the comparator only reads the fields.

### Algorithm

```c
// HloScheduleGraph::InitializeGraphAnalysis()  @ 0x1362a860  (1264-line decompile)
//   Two reverse-topological sweeps over the schedule graph.
void InitializeGraphAnalysis() {
    // (1) ASYNC DEPTH  — node[+0x38]
    //   for each node in forward topo order:
    //       depth = max over predecessors p of  p.depth  (raised by async edge cost)
    //   implemented as the vmaxsd chain over [pred+0x38] writing [node+0x38]
    for (node in topo_order) {
        double d = 0.0;
        for (pred in node.predecessors)
            d = max(d, pred.depth);                          // vmaxsd [r12+38h], [rsi+38h] (lines 539-597)
        node.depth = d;                                      // vmovsd [r12+38h], xmm0
    }
    // (2) ASYNC HEIGHT — node[+0x40]
    //   for each node in reverse topo order:
    //       height = (max / sum) over successors s of  s.height + edge_latency
    //   the async edge contributes its GetLatencyBetween cost; the chain at
    //   lines 946-1000 mixes vmaxsd (critical path) with vaddsd (async accumulation).
    for (node in reverse_topo_order) {
        double h = 0.0;
        for (succ in node.successors)
            h = max(h, succ.height /* + edge cost */);       // vmaxsd / vaddsd [rax+40h] (lines 946-1000)
        node.height = h;                                     // vmovsd [rax+40h], xmm0
    }
}
```

The depth sweep is the run of `vmaxsd xmm0, xmm0, [pred+38h]` / `vmovsd [node+38h], xmm0` at lines 481–597; the height sweep is the `vmaxsd`/`vaddsd` cluster over `[node+40h]` at lines 946–1000. Both are pure max-propagation (height additionally accumulates async-edge latency), so `[+0x38]` is the longest-cost path *into* the node from the leaves and `[+0x40]` is the longest async-cost path *out* of the node to the roots. The edge cost fed into the height propagation is the bundle/latency cost from `GetLatencyBetween` ([Bundle-Aware Cost](../cost/bundle-aware-cost.md)).

> **QUIRK — "depth" and "height" here are async critical-path costs, not graph levels.** `[+0x38]`/`[+0x40]` are `double` cycle costs accumulated through async edges, not integer DAG depths. The comparator prefers DEEPER async depth (key 15) and HIGHER async height (key 16) precisely because a node sitting on a long async critical path should be issued early so its dependent async op's latency can be hidden under everything below it. A reimplementation that uses integer topological levels will rank candidates correctly only when all edges cost the same — which they never do.

---

## The Candidate Comparator — `ReadySetLt` (22 keys)

### Purpose

`FindAndExtractBestNodeAvailable` (`0x13618880`, 2458-line decompile) iterates the ready-set, keeping a running "best" candidate, and for each contender calls the `ReadySetLt` comparator inlined from the OSS `latency_hiding_scheduler.cc`. The comparator is a fixed *ordered* list of tie-break keys: each key is evaluated top to bottom, and the first key that distinguishes the two candidates decides the winner and records a `CandidateResult` reason string (the `kXxx` enum). When no candidate can be picked, the function emits one of two diagnostics and fails.

There is **no separate user comparator**: both the `candidate_compare_` and `post_step_mutator_` `SchedulerConfig` hooks are empty in this build, so the built-in `ReadySetLt` chain documented here is what runs. The ILP variant ([ILP-LHS](lhs-ilp-variant.md)) swaps only the async *classifier*, never this chain.

### Algorithm

The keys, in evaluation order, with the compared `HloGraphNode` field, the `CandidateResult` reason string (all 22 verified present in the decompile), and the winning direction:

| # | Reason string | Compares (`HloGraphNode` field) | Prefer |
|--:|---|---|---|
| 1 | `kForceEarly` | `[+16]` force-early flag | flag-set first |
| 2 | `kForceDelay` | `[+18]` force-delay flag | flag-NOT-set first (delay later) |
| 3 | `kForceDelayAfterTarget` | `[+18]` force-delay flag (post-target variant) | flag-NOT-set first (delay later) |
| 4 | `kForceDelayPriority` | `[+12]` force-delay priority (int) | LOWER priority first |
| 5 | `kPreference` | `[+0x14]` preference (float) | HIGHER first |
| 6 | `kMemoryPeakOverLimit` | `(live_peak + ΔMemoryPressure)` vs `memory_limit` | the one that stays UNDER limit |
| 7 | `kOnlyDecreaseMemoryOverLimit` | only-this-reduces-pressure while over limit | the decreasing one |
| 8 | `kDecreaseMemoryOverLimit` | both `ΔMemoryPressure < 0`, magnitude | larger reduction |
| 9 | `kScheduleAsyncStart` | `[+24] & 9 == 9` (async-start kind) | async-start first (when "schedule ASAP") |
| 10 | `kScheduleDone` | `ReadySetLt::ShouldScheduleAsyncDone(node)` | the async-done that should fire |
| 11 | `kScheduleStart` | `[+17] == 1` and operand-shape match | the matching start |
| 12 | `kReleaseNonextendable` | `[+0x28]` ready_time vs current_time, occupier count ≥ 2 | the one releasing a non-extendable resource |
| 13 | `kLessSerialResourceConflict` | `GetNumConflictingSerialResources(node)` | FEWER conflicts first |
| 14 | `kNotValuableForSelectiveOverlap` | `[+24] >> 7` selective bit and ready-min ≤ cfg`[+88]` | the valuable one (gated by config `+130`) |
| 15 | `kFreeBackedupResource` | `[+40]` frees-backed-up + `UpdateCandidateResourceConstrained` | the freeing one |
| 16 | `kAsyncDepth` | `[+0x38]` async depth | DEEPER first (when schedule-ASAP) |
| 17 | `kAsyncHeight` | `[+0x40]` async height (critical path to async) | HIGHER first |
| 18 | `kUnlockStart` | `[+24] & 0x20` (unlock-start bit) | unlocking-start first |
| 19 | `kUnlockDone` | `[+24] & 0x10` (unlock-done bit) | unlocking-done first |
| 20 | `kCreatesMoreReadyNodes` | `[+100]` #successors-made-ready (uint) | MORE first |
| 21 | `kDecreaseMemory` | `ΔMemoryPressure` sign (one `<0`, one `≥0`) | the pressure-reducing one |
| 22 | `kOriginalOrder` | `[+28]` original sequence index (int) | LOWER index first (stable; flips in schedule-ASAP) |

Keys 6–8 are gated by config byte `+129` (prioritize-pressure) combined with the over-limit state; key 14 by config byte `+130`. The schedule-ASAP flag (the decompile's `v36`) flips the direction of several keys (async-start, async-depth, original-order) so that, in the ASAP regime, async work and long-critical-path nodes are pulled as early as possible.

### The latency-hiding term

The core of the latency-hiding heuristic is a single clamped subtraction that appears in keys 12, 16, and 17:

```c
// remaining latency of a candidate node  (lines 988-991, 1025-1028, 1463-1493)
double remaining = max(0.0, st->current_time - node->ready_time);
//   vmovsd  xmm1, [r14+138h]        ; current_time      (state+0x138)
//   vsubsd  xmm1, xmm1, [r12+28h]   ; - node.ready_time (node+0x28)
//   vmaxsd  xmm1, xmm2, xmm1        ; clamp at 0
```

`ready_time` (`node[+0x28]`) is the earliest cycle a node *can* be scheduled given its operands; `current_time` (`state+0x138`) is the clock at the current step. When `current_time` has already passed the node's `ready_time`, `remaining` is 0 — the node's inputs are retired and it can issue with no stall. When `ready_time` is still ahead of the clock, the node would stall, and the comparator uses this term (together with the async height key 17) to prefer issuing a *different* node now and the stalling one later. This is exactly the dependency stall from `GetLatencyBetween` ([Bundle-Aware Cost](../cost/bundle-aware-cost.md)) re-surfacing at scheduling time. When neither the async-height (`[+0x40]`) nor async-depth (`[+0x38]`) keys separate the pair, the comparator falls to a clock-distance tie-break (lines 1207–1218): it forms `first_root.ready_time − current_time − candidate[+0x30]` for each candidate, takes the absolute value via `vandpd` against `qword_A2DF238` (`0x7FFFFFFFFFFFFFFF`, the IEEE abs-value mask, byte-confirmed) at line 1212, and prefers the smaller magnitude — the candidate whose `[+0x30]` estimate lands closest to the current clock.

> **GOTCHA — the memory-peak gate (key 6) can override every later key.** `kMemoryPeakOverLimit` adds the candidate's `MemoryPressureTracker::MemoryPressureDifference` to the live peak and compares the result against `memory_limit` (the `SchedulerConfig` copy at `state→config + 672`). If only one candidate keeps the peak `≤ limit`, it wins *regardless of async depth, height, or any latency-hiding key below it.* A reimplementation that places the memory keys after the async keys will hide latency at the cost of OOM. The gate sits at position 6, above all overlap heuristics, on purpose.

### Failure diagnostics

When the ready-set is non-empty but no node passes the per-instruction `should_schedule_` predicate or the annotation-group overlap limits, candidates are collected into an `InlinedVector<pair<HloGraphNode*, SkipNodeReason>, 2>` and the function fails with one of two messages, both anchored in the decompile:

```text
"There is a scheduling group which exceeds the overlap limits. Annotation id: <n>.   (line 1742, LHS src 2092)
 It needs <x> resources, but the limit is <y>."
"FindAndExtractBestNodeAvailable failed to find a node to schedule, skipped nodes: <list>"   (line 1939)
```

VLOG diagnostics inside the comparator narrate the choice: `"Choosing from ready (<name>) Reason: First Candidate"` (line 534) and `"... mem pressure chosen <m> mem pressure other <n>"` (line 840).

### Function Map

| Function | Address | Role |
|---|---|---|
| `FindAndExtractBestNodeAvailable` | `0x13618880` | ready-set iteration + 22-key `ReadySetLt` chain + pop |
| `FindAndExtractBestAnnotatedNode` | `0x1361e2c0` | annotation-group-restricted variant |
| `ReadySetLt::ShouldScheduleAsyncDone` | (inlined) | key 10 predicate; called twice on the pair (lines 969–977) |
| `ReadySetLt::UpdateCandidateResourceConstrained` | (inlined) | folds resource occupancy into keys 13/15 (lines 581–583); body not dumped |
| `GetNumConflictingSerialResources` | (inlined) | key 13 conflict count; recovered as call target |

---

## The Async Resource Model — `TpuAsyncTracker`

### Purpose

Whether two async ops may overlap is decided by which physical resource each consumes and that resource's *hazard class*. `TpuAsyncTracker` extends the OSS `AsyncTracker` with TPU-specific resources: the six directional ICI links, host-DMA taps, SparseCore queues, VMEM, and DCN bandwidth. The comparator's resource-conflict keys (12 and 14) and the `ScheduleNode` occupier bookkeeping read this model to gate overlap. The full 0..46 `TpuResourceType` enum and the hazard taxonomy are catalogued on the [ResourceType Taxonomy](scheduler-resourcetype-model.md) page; this section pins the dispatch that names and classifies a resource.

### Algorithm

```c
// TpuAsyncTracker::GetResourceName(resource)  @ 0x10fff420  (src 1152)
char *GetResourceName(long r) {
    CHECK(r <= 46);                                          // FATAL "resource_type < ...kTpuResourceTypeEnd"
    if (r < 13)  return AsyncTracker::GetResourceName(r);    // 0..12 base OSS collective/copy/sendrecv
    if ((0x17FFF >> (r - 13)) & ((r - 13) < 0x11))           // 13..29 with the bitmask gap at idx 28
        return off_2181E148[r - 13];                         // TPU names (ICI / host-DMA / SparseCore / VMEM)
    if ((r - 30) < 0x10) return "kCustomCollective";         // 30..45
    return &nptr;                                            // 46 sentinel
}

// TpuAsyncTracker::GetResourceHazardType(resource)  @ 0x110015e0  (src 1848)
long GetResourceHazardType(long r) {
    CHECK(r <= 46);
    if (r >= 13) {
        if ((0x109FF >> (r - 13)) & ((r - 13) < 0x11))
            return dword_AC0B2C0[r - 13];                    // [0,1,1,1,1,1,1,0,0,0,0,2,0,0,0,0,2]
        return 3 * (unsigned)((r - 30) >= 0x10) + 1;         // kCustomCollective → 1
    }
    // base resources 0..12:
    if (this->track_sync_op_resource /*byte+202*/ != 1)
        return AsyncTracker::GetResourceHazardType(r);       // default: 4*(r != 5)
    // TPU collective-serialization override:
    if (r == 3 /*kAllReduce*/ || r == 6 /*kReduceScatter*/ ||
        (r == 2 /*kAllGather*/ && this->byte_314))
        return 3;                                            // kSerial — collective engine single-occupancy
    return AsyncTracker::GetResourceHazardType(r);
}
```

The split is read literally: `r > 46` FATALs at source line 1152 (name) / 1848 (hazard); `r < 13` defers to the base `AsyncTracker`; 13..29 index `off_2181E148` (names) and `dword_AC0B2C0` (hazards); 30..45 are all `kCustomCollective`. The base hazard is `4 * (r != 5)` — every base class is **shareable** (hazard 4, overlap up to a per-kind limit) *except* `kCopy` (`r == 5`, hazard 0 = unsharable, async copies serialize on the copy engine).

The hazard codes are: `0` = unsharable (single-issue), `1` = serial (one in flight, FIFO), `2` = nonextendable (cannot be deferred past its window — `kVmem`), `3` = serial-TPU-collective-override, `4` = shareable. The TPU resource hazard table is `dword_AC0B2C0 = [0,1,1,1,1,1,1,0,0,0,0,2,0,0,0,0,2]` for indices 13..29: DCN bw unsharable, the six ICI directions and the two host-DMA taps and `kSparseCore` serial, the SparseCore sub-queues unsharable, and `kVmem` nonextendable.

> **QUIRK — collectives overlap by ICI *direction*, not by a single "collective" counter.** `MayAddIciLinks` (`0x10fffb20`) decodes a collective's opcode, prices the transfer with `CostModel::GetCycles` (line 187), and deposits the cost into *one of the six directional ICI counters* (resources 14..19), selecting the direction from the per-collective host-to-device flag at tracker byte `+208` (line 130: `kHostToDevice ? r+1 : 2-r`). Two collectives on *different* ICI axes overlap freely; two on the *same* axis serialize (hazard 1). A reimplementation that models "one collective resource" will incorrectly serialize an all-reduce on X+ against an all-gather on Y+.

> **NOTE — the TPU collective-serialization override is config-gated.** The `byte+202` "track-sync-op-resource" flag and the `byte+314` all-gather flag decide whether `kAllReduce`/`kReduceScatter`/`kAllGather` are promoted from shareable (4) to serial (3). With the flags clear, the base OSS hazard (shareable) applies and collectives of the same kind may overlap up to their per-kind limit. The per-kind limits themselves come from the `TpuAsyncTracker` ctor args (`a13..a20`, defaulting to `INT64_MAX` via `AutoOr<long>`); only the DCN (`a5 = env+4568`) and host/ICI pool (`a6 = env+5320`) limits are anchored to specific env offsets. Treat the override gating as CONFIRMED and the exact per-kind default limits as PARTIAL.

### Function Map

| Function | Address | Role |
|---|---|---|
| `TpuAsyncTracker::GetResourceName` | `0x10fff420` | resource id → name; 0..46 dispatch |
| `TpuAsyncTracker::GetResourceHazardType` | `0x110015e0` | resource id → hazard class; collective override |
| `TpuAsyncTracker::GetNumAvailableResources` | `0x10fff600` | resource id → per-kind overlap limit |
| `TpuAsyncTracker::MayAddIciLinks` | `0x10fffb20` | deposit collective cost into directional ICI counter |
| `TpuAsyncTracker::MayAddHostTransfers` | `0x11000280` | host send/recv → host-DMA counters |
| `TpuAsyncTracker::ctor` | `0x10ffba60` | assigns the per-kind limits (`a5/a6` env-anchored) |

---

## Memory-Budget Driver — `RunImpl`

The drain loop above schedules *one* attempt. `LatencyHidingScheduler::RunImpl` (`0x136321a0`, LHS src 4182) wraps it in a memory-pressure retry loop. After each scheduling attempt it checks whether the resulting peak HBM footprint fits the budget; if not, it tightens the budget and re-runs, forcing the comparator's memory keys (6–8) to fire more aggressively on the next pass.

```c
// LatencyHidingScheduler::RunImpl memory retry  @ 0x136321a0  (lines 882-926)
//   peak footprint = EstimateFragmentationSize() + computed_use()
//   while ( frag + computed_use() > memory_limit  &&  retries-- > 0 ):
//       memory_limit = next_memory_limit();    // TIGHTENS by 10%
//       re-run ScheduleComputation
double next_memory_limit(uint64 current) {
    double d = (double)current;                  // exact u64->f64 (2^52/2^84 biases)
    d *= 0.9;                                     // qword_A2DFD10 = 0x3FECCCCCCCCCCCCD   <<< RELAX FACTOR
    return (int64)floor(d);                       // vcvttsd2si, with 2^63 (qword_A2DF388) signed-cast fixup
}
```

> **NOTE — `next_memory_limit` shrinks the budget; it does not relax it.** The OSS name reads as if it relaxes the budget upward, but the byte-exact factor is `qword_A2DFD10 = 0.9` (read at RunImpl lines 892/918), so each retry **shrinks** the budget by 10%. A failed attempt asks the list scheduler to fit a 10%-smaller budget, which pushes it to favor the memory-pressure-reducing candidate keys (6–8) over the latency-hiding keys — trading overlap for a lower HBM peak. The loop is inert (`memory_limit == -1`) unless `EnableSchedulerMemoryPressureTracking` or the post-SC-assignment rerun byte makes the limit finite.

The peak-footprint estimate's fragmentation term comes from `EstimateFragmentationSize` (`0x13633c00`, LHS src 334/336), which is *not* a closed-form alignment formula but a full heap simulation: it builds a `GlobalDecreasingSizeBestFitHeap<HloValue>` (largest-buffer-first best-fit, alignment 1) and runs `HeapSimulator::Run`, returning `max(0, heap.fragmentation_size)` — the bytes lost to allocator gaps. The `vmaxsd`/`vmulsd` constants (`qword_A2DFD10`, `qword_A2DF388`) and the heap construction are byte-confirmed in the decompile.

---

## Worked Example — Overlap vs Stall

A 4-op fragment on `6acc60406` (TensorCore clock `f` MHz), base schedule already laid by the memory scheduler:

```text
%ar   = all-reduce-start(...)      ; collective on ICI X+  (resource 16, hazard 1)
%ar.d = all-reduce-done(%ar)
%mm   = dot(%a, %b)                ; bf16 matmul, 212-cycle bundle
%add  = add(%ar.d, %mm)
```

The matmul's `NodeCost` is `212 / (f·1e6)` s; the all-reduce's transfer cost is `CostModel::GetCycles` deposited into the ICI X+ counter (resource 16). Walking the comparator after `%ar` is issued at `t0`:

- `ready_set = {%mm}` (`%ar.d` is not ready until the ICI latency elapses; `%add` waits on both). `%mm` is the only candidate, so it is scheduled at `t0` and `current_time` advances by 212 cycles. During that window the ICI transfer for `%ar` is in flight on a *different* resource (ICI X+, not the MXU slot) → **OVERLAP**: the matmul hides the all-reduce latency. The async-height key (17) had already biased the scheduler to issue `%ar` early (its `[+0x40]` reaches `%add`), and the matmul filled the bubble.
- If ICI latency `< 212` cycles, `%ar.d` becomes ready before `%mm` finishes; `kScheduleDone` (key 10) fires it next, then `%add`. Total ≈ 212 cycles — the collective is fully hidden, zero stall.
- If ICI latency `> 212` cycles (cross-host DCN), after `%mm` there is no other ready non-async node, so `current_time` jumps to `%ar.d`'s `ready_time`. **STALL** = `(latency − 212)` cycles, accounted as wasted all-reduce cycles. A second independent matmul would let key 17/20 (`kAsyncHeight`/`kCreatesMoreReadyNodes`) slot into the bubble and cut the stall.
- If `memory_limit` is tight, `kMemoryPeakOverLimit` (key 6) can force `%ar.d` (which frees the all-reduce buffer) ahead of `%mm` even though that loses the overlap — trading latency-hiding for a lower HBM peak. The 0.9 retry shrink is what drives the scheduler into this regime across reruns.

---

## Confidence Summary

| Claim | Evidence |
|---|---|
| Drain loop is `while (ready_set @ +208 \|\| pending @ +504)` | `ScheduleComputation(comp,state)` @ `0x1362eb60` line 539 |
| `SchedulingStep` = vtable+128 select, vtable+112 `ScheduleNode`, write `current_time` @ `+0x138` | `0x1362d480` lines 32/53/60 |
| Schedule built reversed, flipped at end; `byte+440` chooses direction | `FindTopRoots`/`FindBottomRoots` + reverse @ `0x1362eb60` |
| Comparator is the 22-key `ReadySetLt` chain (all reason strings present) | `0x13618880`, lines 534–1553 (22 `kXxx` strings) |
| Latency-hiding term `max(0, current_time − ready_time)` | `vsubsd [r14+138h]−[r12+28h]` + `vmaxsd` @ lines 988-991 |
| Async depth/height read at `[+0x38]`/`[+0x40]`, precomputed by max-propagation | `InitializeGraphAnalysis` @ `0x1362a860` lines 481-1000 |
| Memory-peak gate (key 6) precedes all overlap keys | `kMemoryPeakOverLimit` branch @ line 771, before async keys |
| `TpuAsyncTracker` 0..46 split; base hazard `4*(r!=5)`; TPU table `[0,1,1,…,2]` | `GetResourceName`/`GetResourceHazardType` @ `0x10fff420`/`0x110015e0` |
| Collectives overlap by ICI direction (byte `+208`), priced via `GetCycles` | `MayAddIciLinks` @ `0x10fffb20` lines 130/187 |
| `next_memory_limit` shrinks budget by 0.9 each retry | `qword_A2DFD10` @ RunImpl `0x136321a0` line 918 |
| Fragmentation = `GlobalDecreasingSizeBestFitHeap` HeapSimulator run | `EstimateFragmentationSize` @ `0x13633c00` lines 39/52/73 |
| `ScheduleNode` interior (clock-advance/resource-release) inferred, not dumped | comparator reads + `current_time` write-back |
| Per-kind async overlap limits (`a13..a20`) default `INT64_MAX` | `TpuAsyncTracker` ctor @ `0x10ffba60`; only `a5/a6` env-anchored |

---

## Cross-References

- [Scheduler Overview](overview.md) — where LHS sits in the TPU scheduling pipeline and the two `AddPass` callsites.
- [LHS post_layout](lhs-post-layout.md) — the post-layout LHS pipeline placement that drives this core.
- [LHS post_layout_pre_fusion](lhs-post-layout-pre-fusion.md) — the pre-fusion variant's `SchedulerConfig` and hook table.
- [LHS ILP variant](lhs-ilp-variant.md) — the `EnableIlpLatencyHidingScheduler` gate that swaps the async classifier ahead of this comparator (the comparator chain is unchanged).
- [ResourceType Taxonomy](scheduler-resourcetype-model.md) — the full 0..46 `TpuResourceType` enum, hazard classes, and per-resource availability counts this page's `TpuAsyncTracker` dispatch consumes.
- [Cost Model Overview](../cost/overview.md) — the three cost-class families and the `Target` clock wiring behind every cycle number.
- [Bundle-Aware Cost](../cost/bundle-aware-cost.md) — the `MaxResourceCycles` bundle cost and `LatencyBetween` dependency latency that feed this comparator's `NodeCost` and async-height keys.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VIII — Instruction Scheduling & Bundle Packing — [back to index](../index.md)
