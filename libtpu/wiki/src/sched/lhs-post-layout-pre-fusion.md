# LHS post_layout_pre_fusion Variant

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Reconstructed by static analysis of the unstripped, full-symbol ELF (`nm -C` resolves every method); other versions will differ.*

## Abstract

The **post_layout_pre_fusion** variant is the `LatencyHidingScheduler` (LHS) as it *would* sit at the `"Pre main fusion"` slot of `HloOptimizeAfterLayoutAssignment` (`0x10962660`) — the scheduler that runs after layout assignment but **before** the dedicated `"Main fusion"` pipeline, so that it schedules still-unfused ops and the resulting overlap profile can inform the fusion pass. The defining finding of this page is negative and decisive for any reimplementer: **in v0.0.40 this slot is not wired.** The `"Pre main fusion"` `HloPassPipeline` (built at line 1169 of `HloOptimizeAfterLayoutAssignment`) contains no `AddPass<LatencyHidingScheduler>`, and `HloOptimizeAfterLayoutAssignment` never calls `RunHloScheduler`. Every reachable `AddPass<LatencyHidingScheduler>` lives in `RunHloScheduler` (`0x1096fac0`), which is Phase 7 — *after* main fusion — and is the canonical [`post_layout`](lhs-post-layout.md) pass.

The reason the slot is dead is structural, not accidental. The shared `RunImpl` (`0x136321a0`) opens with a `LogMessageFatal` on `!module->has_schedule()` — *"LatencyHidingScheduler expects a base schedule that minimizes memory pressure."* That base schedule is produced by `HloMemorySchedulerWithBrkgaFallback`, which is added only inside `RunHloScheduler` (line 567). At the pre-fusion slot no prior memory schedule exists, so a live LHS there would abort the compile. The `"Pre main fusion"` pipeline therefore *cannot* host the canonical OSS LHS, and the only collective-/schedule-adjacent work it does run is `CollectivePipeliner` and its cleanups — none of which add the LHS.

This page documents the variant the way the wiki documents the other two: by its **delta** from the live [`post_layout`](lhs-post-layout.md) pass and from the shared [scheduler core](latency-hiding-scheduler-core.md). Because the pass body (`RunImpl`), the config builder (`GetSchedulerConfig`), the estimator selector (`GetLatencyEstimator`), and the tracker builder (`GetTpuAsyncTracker`) are all *single compiled instances* shared with `post_layout`, the difference is entirely in the **inputs** a pre-fusion callsite would feed — which, in this build, no callsite supplies. A reimplementer porting a future libtpu that *does* wire this slot needs exactly three deltas: the pre-fusion `memory_limit_bytes`, the cleared `v45` post-SC-assignment rerun byte, and the empty reserved-SparseCore set.

For reimplementation, the contract is:

- **The slot is dead in v0.0.40.** The `"Pre main fusion"` pipeline exists (`0x10962660` line 1169) but adds no LHS; the only two `AddPass<LatencyHidingScheduler>` sites (`RunHloScheduler` lines 1137 / 1411) are both Phase 7. A grep of the whole decompile for the canonical `AddPass<LatencyHidingScheduler>` template finds references only inside `RunHloScheduler`.
- **The `has_schedule()` FATAL is why.** `RunImpl` line 280 is a `LogMessageFatal`; without a prior `HloMemorySchedulerWithBrkgaFallback` base schedule (added at `RunHloScheduler` line 567, Phase 7), the LHS aborts rather than no-ops. The pre-fusion slot has no such base schedule.
- **The pass body is the shared `RunImpl`** (`0x136321a0`) — identical to `post_layout`: the `has_schedule()` precondition, the computation-list filter, the pre/post `LatencyHidingStatistics` VLOG(1) passes, and the fragmentation-aware memory-pressure retry loop with a `0.9` relax factor.
- **The config delta vs `post_layout`** is three callsite values, not a different builder: `v45 == 0` (no SC-assignment rerun), a pre-fusion `memory_limit_bytes`, and an empty reserved-SparseCore set. The 137-byte `SchedulerConfig` schema, the estimator branch, and the resource model are otherwise byte-for-byte the same as `post_layout`.

| | |
|---|---|
| **Intended phase** | Phase 6 — `"Pre main fusion"` sub-pipeline of `HloOptimizeAfterLayoutAssignment` (`0x10962660`, string at line 1169) |
| **Wired in v0.0.40?** | **NO** — no `AddPass<LatencyHidingScheduler>` in the pre-fusion pipeline; the slot is dead |
| **Why dead** | `RunImpl` `has_schedule()` `LogMessageFatal` (`0x136321a0` line 280); no base schedule exists pre-fusion |
| **Live LHS instead** | [`post_layout`](lhs-post-layout.md) in `RunHloScheduler` (`0x1096fac0`), Phase 7, after main fusion |
| **Shared pass body** | `xla::LatencyHidingScheduler::RunImpl` — `0x136321a0` (1217-line decompile; identical to `post_layout`) |
| **Shared config builder** | `xla::jellyfish::(anon)::GetSchedulerConfig` — `0x10974aa0` (137-line decompile, 137-byte POD) |
| **Config delta vs post_layout** | `v45 == 0`; pre-fusion `memory_limit_bytes`; empty reserved-SparseCore set |
| **Canonical AddPass template** | `0x10975c40` — referenced only from `RunHloScheduler` |
| **Confidence** | HIGH (the negative result is byte-anchored across two functions) |

---

## Where the slot lives

### Purpose

`HloOptimizeAfterLayoutAssignment` (`0x10962660`, 2651-line decompile) is the Phase-6 driver. It builds a sequence of named `HloPassPipeline` objects between `LayoutAssignment` (Phase 5) and final scheduling (Phase 7). The pre-fusion LHS *would* be added to its `"Pre main fusion"` pipeline — the one whose name string is stored at line 1169.

### Phase layout (recovered)

```text
DeepseaCompilerBase::RunHloPasses  (0x1093a420)
  ...
  RunHloPassesAfterLayoutAssignment  (0x21395d20)
    └─ HloOptimizeAfterLayoutAssignment  (0x10962660)        [Phase 6]
         "Post layout assignment"  HloPassPipeline           (SSO-built; tail "signment" at line 707)
         "Pre main fusion"         HloPassPipeline   (line 1169)  ← THIS SLOT (no LHS added)
             CreateNestedHloPipeline "layout fixup"           (line 1176)
             CreateNestedHloPipeline "host offloading"        (line 1396)
             CollectivePipeliner + PipeliningCleanups         (collective-adjacent; no AddPass<LHS>)
             (60+ inline AddPass<...> sites — none is LatencyHidingScheduler)
         "Main fusion"             HloPassPipeline   (line 2279)
         PostMainFusionHloOptimize (0x10966560)               [Phase 6 tail; called at line 2614]
  PostOptimizationPipeline  (0x1093fd40)                      [Phase 7 driver; called from RunHloPasses line 1987]
    └─ RunHloScheduler  (0x1096fac0)                          [Phase 7] ← the live LHS
```

> **NOTE —** the `"Post layout assignment"` pipeline name is not a single `.rodata` C-string; it is assembled by an inline 16-byte SSO store whose tail `"signment"` is the literal at `0x10962660` line 707 (`strcpy(&v342[14], "signment")`). The same stack-built-string idiom hides the pipeline names in `RunHloScheduler` (`final_scheduler` / `async_scheduling`); see [`post_layout`](lhs-post-layout.md). A reimplementer grepping the binary for `"Post layout assignment"` will not find it.

### The negative result, byte-anchored

```c
// HloOptimizeAfterLayoutAssignment  @ 0x10962660
//   "Pre main fusion" pipeline built at line 1169.
//   rg over this function for LatencyHidingScheduler / AddPass<...Latency...>
//   / RunHloScheduler  →  NO MATCHES.
//   It builds the pipeline names and 60+ pass adds but never the LHS,
//   and never invokes RunHloScheduler (Phase 7 is a sibling call, not nested here).
```

`RunHloScheduler` is *not* reached from inside `HloOptimizeAfterLayoutAssignment`; it is a separate Phase-7 call, driven by `PostOptimizationPipeline` (`0x1093fd40`), which `RunHloPasses` invokes at line 1987 — after the Phase-6 `HloOptimizeAfterLayoutAssignment` call returns. The canonical `AddPass<LatencyHidingScheduler>` template body (`0x10975c40`) is referenced from exactly one function in the entire decompile — `RunHloScheduler` — at two mutually-exclusive sites (lines 1137 and 1411), both *after* `"Main fusion"`. There is no pre-fusion instantiation to configure.

---

## Why the slot is dead — the `has_schedule()` FATAL

### Purpose

The structural reason a live LHS cannot sit pre-fusion is the very first thing `RunImpl` does after logging: it asserts a base schedule exists. This is a hard `LogMessageFatal`, not a recoverable status.

### Algorithm

```c
// xla::LatencyHidingScheduler::RunImpl(module, exec_threads)   // 0x136321a0
//   line 280 / source latency_hiding_scheduler.cc:4110
if (!module->has_schedule()) {
    LogMessageFatal(".../latency_hiding_scheduler.cc", 4110, "module->has_schedule()")
        << "LatencyHidingScheduler expects a base schedule that minimizes memory pressure.";
    // ^ aborts the compile
}
```

The base schedule the precondition demands is built by `HloMemorySchedulerWithBrkgaFallback`, constructed at `RunHloScheduler` line 567 — inside Phase 7's `final_scheduler` pipeline. No equivalent runs in the `"Pre main fusion"` pipeline. Adding the LHS pre-fusion would therefore FATAL on the first computation. This is the same precondition documented for the live [`post_layout`](lhs-post-layout.md) pass; here it is the reason the variant is *only* a configuration, never a live pass.

> **GOTCHA —** the FATAL is exactly why "schedule unfused ops to inform fusion" is not achievable by simply re-adding the LHS earlier in the pipeline. A future libtpu that wants a pre-fusion LHS must *also* add a memory scheduler ahead of it (the BRKGA-fallback base scheduler or equivalent) in the same pre-fusion pipeline; otherwise the `has_schedule()` CHECK aborts. A reimplementer cannot satisfy the intent by moving the `AddPass` alone.

---

## Pass body — shared `RunImpl`

### Purpose

When (in a future build) a pre-fusion callsite *does* add the LHS, it invokes the same compiled `RunImpl` (`0x136321a0`) that `post_layout` uses. There is exactly one `RunImpl` in the binary — the unmodified upstream `latency_hiding_scheduler.cc` body. The variant changes the config and context handed to `AddPass`, never the body. The full algorithm (drain loop, 22-key comparator, async resource model) is documented on the [scheduler core](latency-hiding-scheduler-core.md) page; this section pins the `RunImpl` envelope that surrounds it.

### Algorithm — RunImpl envelope

```c
absl::StatusOr<bool> RunImpl(HloModule *module,
                             const flat_hash_set<string_view>& exec_threads) {  // 0x136321a0
    VLOG(5) << "Original module:" << module->ToString();          // line ~270

    // (1) precondition — FATAL if no base schedule (the dead-slot reason above)
    CHECK(module->has_schedule());                                // line 280, src 4110

    // (2) computations to schedule: post-order, keep top-level OR force_delay group
    to_schedule = MakeComputationPostOrder(exec_threads)
        .filter(c -> c->caller_instructions().empty()
                  || c->frontend_attribute("scheduling_group") == "force_delay");
    VLOG(2) << "Computations to schedule " << to_schedule.size()  // line 578
            << " size HloGraphNode: " << ... << " size HloEdge " << ...;
    if (to_schedule.empty()) return false;

    // (3) core init (+ real-cost-model init if config.use_real_cost_model)
    scheduler_core_->SchedulerInitialize(module);

    // (4) pre-schedule statistics (VLOG(1) only)
    if (VLOG_IS_ON(1))
        for (c : to_schedule)
            LOG(INFO) << "] Statistics before scheduling:"        // line 673
                      << LatencyHidingStatistics(c, ...).ToString();

    // (5) memory-pressure retry loop  (see below)

    // (6) final-limit log + (7) post-schedule statistics (VLOG(1))
    LOG(INFO) << " LatencyHidingScheduler current memory usage: " // line 1088
              << (frag + computed_use()) << " bytes. Current limit: " << memory_limit();
    if (VLOG_IS_ON(1))
        for (c : to_schedule)
            LOG(INFO) << "] Statistics after scheduling:"         // line 1146
                      << LatencyHidingStatistics(c, ...).ToString();

    // (8) optional ScheduleProto dump
    return true;  // always reports "changed"
}
```

The pre/post `LatencyHidingStatistics` passes (lines 673 / 1146) are what make the LHS observable: emitted *before* the schedule they report the input base-schedule wasted cycles, and *after* the delta is the latency hidden. Both build a fresh `BufferInfoTracker` + `ModulePressureState`. They are VLOG(1)-gated, so they cost nothing in production unless `--v=1`.

> **NOTE —** the `force_delay` filter (step 2) keeps a non-top-level computation in the schedule set only if its caller carries the `scheduling_group` frontend attribute `"force_delay"`. This is the same string the core's per-computation entry RE2-matches; it is the single mechanism by which a callee computation is scheduled independently rather than inline with its caller.

### Memory-pressure retry loop

```c
// RunImpl memory retry  @ 0x136321a0  (lines ~840-1054)
int64_t frag = config.estimate_fragmentation_size
             ? EstimateFragmentationSize(module, alias_analysis, alias_info)   // line 840
             : 0;
int64_t mem_limit = scheduler_core_->memory_limit();
for (int attempt = 0; attempt < scheduler_core_->memory_limit_retries(); ++attempt) {
    if (frag + scheduler_core_->computed_use() <= mem_limit) break;
    LOG(INFO) << "LatencyHidingScheduler current memory usage: "  // line 874
              << (frag + computed_use())
              << " bytes, does not fit in initial limit: " << mem_limit  // line 878
              << ". Setting the new limit to " << next_memory_limit();   // line 881
    scheduler_core_->SchedulerInitialize(module);
    mem_limit = (int64_t)scheduler_core_->next_memory_limit();    // relax-by-factor
    scheduler_core_->set_memory_limit(mem_limit);
    reschedule_all_computations();
    if (config.estimate_fragmentation_size)
        frag = EstimateFragmentationSize(...);                    // line 1054
}
```

`next_memory_limit()` multiplies the current limit by the AVX constant `qword_A2DFD10` (read at lines 892 / 918: `vmovsd xmm1, cs:qword_A2DFD10` / `vmulsd`), which the core page resolves to **`0.9`** — each retry *shrinks* the budget by 10%, pushing the comparator's memory-pressure keys (5-7) over its overlap keys. In the **pre-fusion configuration this loop would be inert** for the same reason it is inert on a first `post_layout` run: `memory_limit == -1` unless `EnableSchedulerMemoryPressureTracking` is true. The pre-fusion config can never set the `v45` rerun byte (it is, by construction, not a post-SC-assignment rerun), so the *only* way the loop fires pre-fusion is the pressure-tracking flag.

> **GOTCHA (LOW) —** the exact `qword_A2DFD10 = 0.9` decimal is resolved on the [scheduler core](latency-hiding-scheduler-core.md) page from the IEEE bit pattern; `EstimateFragmentationSize` (TPU-private; a `GlobalDecreasingSizeBestFitHeap` heap simulation, not a closed-form alignment formula) is documented there too. Treat both as inherited facts, not re-derived on this page.

---

## Config delta vs post_layout

### Purpose

`GetSchedulerConfig` (`0x10974aa0`) is a single compiled function. It builds the same 137-byte `SchedulerConfig` POD regardless of phase; what differs between a pre-fusion and a post_layout invocation is the **arguments at the callsite**. Three of the POD fields are driven by inputs a pre-fusion site supplies differently.

### The three deltas

```c
// GetSchedulerConfig(out, mem_limit_bytes=a2, env=a3, target=a4, is_lem=a5)  // 0x10974aa0
char v45 = *(BYTE*)(env + 4097);   // xla_tpu_rerun_lhs_post_sc_assignment   (line 29)

// (A) memory_limit  @ POD off 80  (lines 66-71)
out.memory_limit = -1;
if (v45)                                       out.memory_limit = mem_limit_bytes;
if (EnableSchedulerMemoryPressureTracking(...)) out.memory_limit = mem_limit_bytes;

// (B) schedule_send_recvs  @ off 120  (line 76)
out.schedule_send_recvs = v45 | ScheduleSendRecvs(...);

// (C) enable_send_recv_aggregation  @ off 127  (line 90)
out.enable_send_recv_aggregation = env[4789] | v45;
```

| POD field (offset) | post_layout (live) | post_layout_pre_fusion (this variant) |
|---|---|---|
| `v45` source (`env+4097`) | may be **1** on the SC-assignment rerun | **always 0** (not a rerun) |
| `memory_limit` (80) | `-1`, or `mem_limit_bytes` on pressure-track **or** rerun | `-1` unless pressure-track (rerun path unreachable) |
| `schedule_send_recvs` (120) | `v45 \| ScheduleSendRecvs(...)` — forced on a rerun | `ScheduleSendRecvs(...)` only |
| `enable_send_recv_aggregation` (127) | `env[4789] \| v45` — forced on a rerun | `env[4789]` only |
| `mem_limit_bytes` argument | the **final** post-fusion budget | a **pre-fusion** budget (fewer fused buffers) |
| reserved-SparseCore set | populated from `GetReservedSparseCores` | **empty** (SC assignment has not run pre-fusion) |

Everything else in the POD is identical: the per-kind overlap limits (offsets 0-72, e.g. `collective_permute_overlap_limit = env[3964]`, `all_reduce_overlap_limit = env[3960]` or `INT32_MAX` under `EnableSparseCoreOffloadQueuingInLhs`), the `use_real_cost_model` byte at offset 123, the packed bool quad at offsets 122-125, and the `disable_loop_consume_resource` / `enable_idle_count_extension` bytes at 134-135. The complete 137-byte schema is tabulated on the [`post_layout`](lhs-post-layout.md) page; this variant does not change a single offset.

> **GOTCHA —** the `v45` byte and the reserved-SparseCore set are the *only* things that structurally distinguish a pre-fusion config from a post_layout config, and both are forced to their "first run / nothing reserved" state pre-fusion. A reimplementer who copies the post_layout config verbatim into a pre-fusion slot would wrongly carry a populated reserved-SC set (information that does not exist before SC assignment) and a possibly-set `v45` (a rerun semantic that makes no sense pre-fusion). Clear both.

---

## Context delta — estimator and tracker

### Purpose

`AddPass<LatencyHidingScheduler>` takes a `SchedulingContext` (estimator + tracker) and a `DefaultSchedulerCore`. Both are built by shared helpers; the pre-fusion delta is again in inputs, chiefly the empty reserved-SparseCore set passed to the tracker.

### LatencyEstimator — identical selection

`GetLatencyEstimator` (`0x10974e00`) branches on `SchedulerConfig` offset 123 (`use_real_cost_model`):

```c
function GetLatencyEstimator(target, config, env):   // 0x10974e00
    if config.use_real_cost_model != 1:              // off 123, line 55
        if PGLE_profile_path set:
            FATAL "Attempted to use profile-guided latency estimator without the real "
                  "cost model. Either enable --xla_tpu_scheduler_using_real_cost_model "
                  "or unset --xla_tpu_impure_latency_hiding_scheduler_profile_path."   // line 70
        return ApproximateLatencyEstimator()
    est = CostModelLatencyEstimator(target, config, env)         // 0x10ff8a60, line 91
    if PGLE_profile_path non-empty:
        proto = tsl::ReadBinaryProto(path)                       // line 99
        est = ProfileGuidedLatencyEstimator(est, proto)          // line 104
    return est
```

This is the same `GetLatencyEstimator` the live `post_layout` pass uses; the pre-fusion variant chooses the estimator identically (the choice is a function of `use_real_cost_model`, a config byte, and the PGLE flag — none phase-dependent). See [`cost/overview`](../cost/overview.md) for the cost-model family and [`learned-cost-model-client`](../cost/learned-cost-model-client.md) for the `CostModelLatencyEstimator` path.

### AsyncTracker — empty reserved-SC set

`GetTpuAsyncTracker` (`0x10975520`) wraps `TpuAsyncTracker::Create` (`0x10ffb3e0`, called at line 179) and takes an `absl::flat_hash_set<int64_t>` reserved-SparseCore set as its final argument. In the live `post_layout` path that set is filled from `GetReservedSparseCores` (`RunHloScheduler` line 873) — the SparseCore IDs withheld from the overlap pool, which exist *only after* SparseCore assignment. A pre-fusion invocation runs before SC assignment, so this set is **empty**. The base resource taxonomy (ICI links, HBM banks, megacore queues, host-DMA taps, the SparseCore offload queue under `EnableSparseCoreOffloadQueuingInLhs`) is unchanged; the [`scheduler-resourcetype-model`](scheduler-resourcetype-model.md) page documents it. The empty reserved-SC set is the single AsyncTracker delta of this variant.

### DefaultSchedulerCore — identical wiring

The core ctor (`0x10976ce0`) copies the 137-byte config POD with `vmovups` blocks from `[rcx]…[rcx+0x69]` into `[rdi+0x10]…[rdi+0x79]` (lines 23-32) and installs four `std::function` slots. In the live build `candidate_compare_` and `post_step_mutator_` are **empty** (`RunHloScheduler` lines 1000 / 1002), so the core falls back to the built-in `ReadySetLt` heuristic; `should_schedule_` is the `$_1` (ILP) or `$_2` (regular) classifier selected by `EnableIlpLatencyHidingScheduler` (lines 1084-1107), and `target_overlap_limit` is `RunTensorCoreAsyncOpScheduler::$_0` (line 1040). A pre-fusion build would wire these identically — the classifier swap is the [ILP variant](lhs-ilp-variant.md), orthogonal to phase.

---

## post_layout vs post_layout_pre_fusion at a glance

| Aspect | post_layout (live) | post_layout_pre_fusion (this page) |
|---|---|---|
| Intended phase | Phase 7, `async_scheduling` in `RunHloScheduler` | Phase 6, `"Pre main fusion"` pipeline |
| Wired in v0.0.40? | **YES** (`RunHloScheduler` lines 1137 / 1411) | **NO** — no `AddPass<LHS>` in pre-fusion pipeline |
| Sees fusion result? | yes (runs after `"Main fusion"`) | no (would run before fusion — its intent) |
| Base schedule available? | yes (`HloMemorySchedulerWithBrkgaFallback`, line 567) | **no** → `has_schedule()` FATAL if added |
| `RunImpl` | `0x136321a0` (shared) | `0x136321a0` (shared, identical body) |
| `SchedulerConfig` builder | `GetSchedulerConfig` (`0x10974aa0`) | `GetSchedulerConfig` (same) |
| `v45` rerun byte (`env+4097`) | may be 1 (SC-assignment rerun) | always 0 |
| `memory_limit` (off 80) | `-1` unless pressure-track **or** `v45` | `-1` unless pressure-track |
| `mem_limit_bytes` arg | final post-fusion budget | pre-fusion budget |
| reserved-SparseCore set | from `GetReservedSparseCores` | empty (SC assignment not yet run) |
| LatencyEstimator | `GetLatencyEstimator` (same) | `GetLatencyEstimator` (same) |
| `candidate_compare_` / mutator | empty (heuristic) | empty (heuristic) |
| Async classifier | `$_2`, or `$_1` under ILP gate | `$_2`, or `$_1` under ILP gate |

The single live LHS in v0.0.40 is `post_layout`. This variant is the same `RunImpl` configured for the dead pre-fusion slot; documenting it is documenting (a) why the slot is empty and (b) the exact three-input delta a future build would need.

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `HloOptimizeAfterLayoutAssignment` | `0x10962660` | Phase-6 driver; builds `"Pre main fusion"` pipeline (line 1169) — no LHS added | HIGH |
| `PostOptimizationPipeline` | `0x1093fd40` | Phase-7 driver; calls `RunHloScheduler` (reached from `RunHloPasses` line 1987) | HIGH |
| `RunHloScheduler` | `0x1096fac0` | The only LHS `AddPass` sites (1137 / 1411); built by `PostOptimizationPipeline` | HIGH |
| `LatencyHidingScheduler::RunImpl` | `0x136321a0` | Shared pass body; `has_schedule()` FATAL at line 280, retry loop | HIGH |
| `GetSchedulerConfig` | `0x10974aa0` | 137-byte `SchedulerConfig`; `v45 = env[4097]` at line 29 | HIGH |
| `GetLatencyEstimator` | `0x10974e00` | Approximate / CostModel / PGLE selection (byte 123) | HIGH |
| `GetTpuAsyncTracker` | `0x10975520` | Wraps `TpuAsyncTracker::Create`; takes reserved-SC `flat_hash_set` | HIGH |
| `TpuAsyncTracker::Create` | `0x10ffb3e0` | TPU resource model + reserved-SC set (line 179 callsite) | MEDIUM |
| `DefaultSchedulerCore` ctor | `0x10976ce0` | Copies config POD (`vmovups`); installs four `std::function` hooks | HIGH |
| `AddPass<LatencyHidingScheduler, …>` | `0x10975c40` | Templated pass adder — referenced only from `RunHloScheduler` | HIGH |
| `HloMemorySchedulerWithBrkgaFallback` ctor | (line 567 of `0x1096fac0`) | Phase-7 base memory schedule (the `has_schedule()` precondition) | MEDIUM |
| `IsPassDisabled` | `0x12fd8340` | `"latency-hiding-scheduler"` / `"async-op-scheduler"` gates | HIGH |
| `EnableSchedulerMemoryPressureTracking` | `0x1d6b66e0` | `memory_limit` tri-state | HIGH |
| `EnableIlpLatencyHidingScheduler` | `0x1d6b7e00` | `$_1`/`$_2` classifier gate (ILP variant) | MEDIUM |

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| Pre-fusion slot has no `AddPass<LatencyHidingScheduler>` | `0x10962660` — only `"Pre main fusion"` (1169) / `"Main fusion"` (2279); no LHS / no `RunHloScheduler` ref | HIGH |
| Canonical `AddPass<LHS>` template referenced only from `RunHloScheduler` | `rg --no-ignore` for `AddPassINS_22LatencyHidingScheduler` → 1 file (its own body) | HIGH |
| Both live LHS sites are Phase 7 (post-fusion) | `RunHloScheduler` lines 1137 / 1411 | HIGH |
| Slot is dead because `RunImpl` FATALs on `!has_schedule()` | `0x136321a0` line 280 `LogMessageFatal` + precondition string | HIGH |
| Base schedule only exists in Phase 7 | `HloMemorySchedulerWithBrkgaFallback` at `RunHloScheduler` line 567 | MEDIUM |
| `v45 = env[4097]`; drives `memory_limit`/`schedule_send_recvs`/aggregation | `GetSchedulerConfig` lines 29 / 66-71 / 76 / 90 | HIGH |
| Pre-fusion config delta = `v45==0`, pre-fusion budget, empty reserved-SC | derived from `GetSchedulerConfig` + `GetReservedSparseCores` (line 873) phase-ordering | HIGH |
| `next_memory_limit` relax factor `qword_A2DFD10` | `RunImpl` lines 892 / 918 `vmulsd` (decimal `0.9` resolved on core page) | HIGH |
| Empty reserved-SC set is the AsyncTracker delta | `GetTpuAsyncTracker` final `flat_hash_set<long>` arg; SC assignment post-dates pre-fusion | HIGH |
| `GetLatencyEstimator` branch + PGLE FATAL identical | `0x10974e00` lines 55 / 70 / 91 / 104 | HIGH |

---

## Cross-References

- [LatencyHidingScheduler Core](latency-hiding-scheduler-core.md) — the shared `ScheduleComputation` drain loop, 22-key comparator, and `TpuAsyncTracker` resource model that `RunImpl` drives.
- [LHS post_layout](lhs-post-layout.md) — the live Phase-7 LHS; this page is its delta. The full 137-byte `SchedulerConfig` schema, the two-pipeline `RunHloScheduler` structure, and the reserved-SparseCore set are documented there.
- [LHS ILP Variant](lhs-ilp-variant.md) — the `EnableIlpLatencyHidingScheduler` `$_1`/`$_2` classifier swap, orthogonal to scheduling phase.
- [Scheduler ResourceType Model](scheduler-resourcetype-model.md) — the ICI/HBM/megacore/host-DMA/SparseCore resource taxonomy the `AsyncTracker` enforces.
- [Scheduling Overview](overview.md) — where scheduling sits between lowering and encoding.
- [Cost Model Overview](../cost/overview.md) — the latency/cost model family feeding `GetLatencyEstimator`.
- [Learned Cost Model Client](../cost/learned-cost-model-client.md) — the `CostModelLatencyEstimator` real-cost-model path.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VIII — Instruction Scheduling & Bundle Packing — [back to index](../index.md)
