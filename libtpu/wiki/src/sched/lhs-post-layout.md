# LHS post_layout Variant

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Reconstructed by static analysis of the unstripped, full-symbol ELF (`nm -C` resolves every method); other versions will differ.*

## Abstract

The **post_layout** invocation of the XLA `LatencyHidingScheduler` (LHS) is the scheduler pass that v0.0.40 actually runs in production. It lives in `RunHloScheduler` (`0x1096fac0`), the final-scheduling stage of `DeepseaCompilerBase::RunHloPasses`, which executes *after* layout assignment and main fusion. Because layout assignment has already run, the schedule it produces sees real tensor layouts, memory-space placement, and — critically — the result of SparseCore offload assignment. This is the LHS that the wiki's other two variant pages describe by their *delta* from this one: [`lhs-post-layout-pre-fusion`](lhs-post-layout-pre-fusion.md) (the same body wired at a dead "Pre main fusion" slot) and [`lhs-ilp-variant`](lhs-ilp-variant.md) (the flag-gated async-classifier swap inside this same function).

A reimplementer's first surprise is that `RunHloScheduler` builds and runs **two** `xla::HloPassPipeline` objects in sequence, not one nested pipeline. The first, named `final_scheduler`, lays down a *base schedule* with `HloMemorySchedulerWithBrkgaFallback` — a memory-pressure-minimizing order that the LHS requires as a precondition (`module->has_schedule()`). The second, named `async_scheduling`, contains the LHS itself plus the async-overlap rewrites that surround it. The LHS overlaps async start/done op pairs against compute under a resource model, exactly like the upstream XLA `LatencyHidingScheduler`, but driven by a TPU-specific `SchedulerConfig`, a TPU `LatencyEstimator`, and a TPU `AsyncTracker` that knows about ICI links, HBM banks, megacore queues, host-DMA taps, and the SparseCore offload queue.

The single most important structural fact is that the LHS `RunImpl` body is **shared** across all variants — there is exactly one compiled `RunImpl` (`0x136321a0`), the unmodified upstream `latency_hiding_scheduler.cc` body. Variants differ *only* in two inputs handed to `AddPass<LatencyHidingScheduler>`: the `SchedulerConfig` POD and the `SchedulingContext` (estimator + tracker). This page therefore documents the post_layout pass *body* (the shared `RunImpl` and its memory-pressure retry loop) and the post_layout *config delta* (what `GetSchedulerConfig` and `GetTpuAsyncTracker` feed in here that the pre-fusion variant would not).

For reimplementation, the contract is:

- The **two-pipeline structure** of `RunHloScheduler`: `final_scheduler` (base memory schedule) then `async_scheduling` (LHS), run sequentially, with the LHS gated on two `IsPassDisabled` checks.
- The **`SchedulerConfig` POD** built by `GetSchedulerConfig` — the per-kind overlap limits, the `memory_limit` tri-state, and the `v45` post-SC-assignment rerun gate that distinguishes a first run from a rerun.
- The **`SchedulingContext`** wiring: which `LatencyEstimator` is chosen (approximate vs real cost model vs PGLE) and what the `TpuAsyncTracker` resource model adds at this phase (the reserved-SparseCore set).
- The **shared `RunImpl`** body: the `has_schedule()` precondition, the candidate-list scheduler invocation, and the fragmentation-aware memory-pressure retry loop with its relax-by-factor `next_memory_limit()`.

| | |
|---|---|
| **Driver** | `xla::jellyfish::(anonymous)::RunHloScheduler` — `0x1096fac0` (2075-line decompile) |
| **Phase** | Phase 7 — final scheduling, after LayoutAssignment and main fusion |
| **Pipelines** | `final_scheduler` (base sched) → `async_scheduling` (LHS) — two local `HloPassPipeline` objects |
| **AddPass sites** | `0x1096fac0` lines 1137 (common) and 1411 (SparseCore-queue path) |
| **Pass body** | `xla::LatencyHidingScheduler::RunImpl` — `0x136321a0` (shared, 1217-line decompile) |
| **Config builder** | `xla::jellyfish::(anonymous)::GetSchedulerConfig` — `0x10974aa0` (137-line decompile, 137-byte POD) |
| **Estimator builder** | `xla::jellyfish::(anonymous)::GetLatencyEstimator` — `0x10974e00` |
| **Tracker builder** | `xla::jellyfish::(anonymous)::GetTpuAsyncTracker` — `0x10975520` → `TpuAsyncTracker::Create` `0x10ffb3e0` |
| **Scheduler core** | `xla::DefaultSchedulerCore::DefaultSchedulerCore` — `0x10976ce0` |
| **Wired in 0.0.40?** | YES — this is the canonical LHS; the pre-fusion slot is dead |

---

## Pipeline Placement

### Purpose

`RunHloScheduler` is the seventh and final phase of `DeepseaCompilerBase::RunHloPasses` (`0x1093a420`). Phases 1–6 are the HLO→HLO rewrite pipeline (PreOptimization, sharding, SPMD, layout-through-optimize, LayoutAssignment, PostOptimization). By the time control reaches `RunHloScheduler`, every tensor has a concrete layout and a memory space, fusion is final, and — within this function — SparseCore offload ops will have been assigned. The job here is to produce the *instruction schedule* attached to the module (`module->set_schedule`), first as a memory-minimizing base order and then refined by the LHS to overlap async transfers with compute.

### Entry Point

```text
DeepseaCompilerBase::RunHloPasses (0x1093a420)        [Phase 7 / final]
  └─ RunHloScheduler (0x1096fac0)
        Pipeline A — "final_scheduler"  (&v318, inline 16B SSO at line 373)
          MaybeAddInvariantCheckers                       (line 404)
          [opt] LatencyHidingLayerScheduler               (line 504, default OFF)
          HloMemorySchedulerWithBrkgaFallback             (line 567)  ← base schedule
          ConstantDeferring / BarnaCoreFeedScheduler      (lines 611/615)
          Run(Pipeline A)                                 (line 620)
        Pipeline B — "async_scheduling"  (&v341, inline 16B SSO at line 679)
          AsyncOpScheduler / AsyncCollectiveMerger / …    (lines 746-754)
          Run(Pipeline B, first wave)                     (line 765)
          if !IsPassDisabled("latency-hiding-scheduler"):
            GetSchedulerConfig(memory_limit, env, target, is_lem)   (line 802)
            GetLatencyEstimator(...)                       (line 828)
            GetTpuAsyncTracker(..., &reserved_sc)          (line 987 / 1349)
            make_unique<DefaultSchedulerCore>(ctx, cfg)    (line 1046 / 1400)
            AddPass<LatencyHidingScheduler>(ctx, core)     (line 1137 / 1411)  ← THIS PASS
          Run(Pipeline B, LHS wave)
        TpuCopyInsertion / HloSchedule::Update / set_schedule (lines 1722-1971)
```

> **QUIRK —** neither pipeline name survives as a null-terminated `.rodata` C-string usable as a pass argument. Both are built as an inline 16-byte SSO `std::string` on the stack: `final_scheduler` via `strcpy(ptr, "final_scheduler")` with the SSO length byte `ptr[23] = 15` (line 373), the buffer then copied field-ward by `vmovdqu` at lines 379–380; `async_scheduling` via length byte `ptr[23] = 16` (line 679) then `vmovups xmm0, cs:xmmword_865DCAB` (line 682). Verified by byte-anchor: VA `0x865dcab` in `.rodata` holds the 16 raw bytes `async_scheduling` (the xmmword immediate), while `rg -a -c final_scheduler` over the binary returns 0 — `final_scheduler` exists only as the `strcpy` source inlined into the function body. A reimplementer grepping the binary for `final_scheduler` will not find it as a discrete diagnostic string; `async_scheduling` appears exactly once, as that immediate.

### Two pipelines, not one nested pipeline

`final_scheduler` (`&v318`) and `async_scheduling` (`&v341`) are two independent local `HloPassPipeline` objects, each driven by its own `HloPassInterface::Run`; they are not one nested pipeline. `final_scheduler` runs first and completes (`Run` at line 620) before `async_scheduling` (`&v341`) is even constructed, sharing only the module. The base schedule produced by `HloMemorySchedulerWithBrkgaFallback` (line 567) is what satisfies the LHS precondition `module->has_schedule()`. This is precisely why the LHS can run *here* but could not at the Phase-6 "Pre main fusion" slot — there is no prior memory schedule there.

---

## Gating

### Purpose

The post_layout LHS is conditional on two `IsPassDisabled` checks and a set of async-enable predicates. If any gate fails, the schedule is the base memory schedule alone.

### Algorithm

```c
function RunHloScheduler_lhs_gate(module, env, target, config):   // 0x1096fac0
    // Gate 1: is the async_scheduling pipeline built at all? (line 666)
    if IsPassDisabled("async-op-scheduler", config, env):         // 0x12fd8340
        return base_schedule_only
    if !(ShouldUseAsyncAllGather(target, ...)                     // line 674
         || env[184] == 2                                         // line 676
         || (env[230] >= 2 && env[184] == 0)
         || GetTpuCompEnv(env)[3664]):                            // line 677
        return base_schedule_only

    // ... AsyncOpScheduler etc. run as the first wave (line 765) ...

    // Gate 2: is the LHS itself enabled? (line 798)
    if IsPassDisabled("latency-hiding-scheduler", config, env):
        return async_first_wave_only
    // build config + context + core, then AddPass<LatencyHidingScheduler>
```

`IsPassDisabled` (`0x12fd8340`) checks the pass name against (a) the inverted enabled-passes list, (b) the `--xla_disable_hlo_passes` disabled list, and (c) a binary-searched static `kDisabledPasses` table gated by a debug-options version field (env+472).

> **NOTE —** `EnableLatencyHidingLayerScheduler` (`0x1d6b8960`, env+2144) defaults FALSE, so the `LatencyHidingLayerScheduler` branch (lines 434–545) of Pipeline A is skipped. The canonical `LatencyHidingScheduler` in `async_scheduling` is the one that runs.

### Related Knobs

| Knob (effect) | Source | Default |
|---|---|---|
| `async-op-scheduler` pass disable | `IsPassDisabled` name | enabled |
| `latency-hiding-scheduler` pass disable | `IsPassDisabled` name | enabled |
| `EnableLatencyHidingLayerScheduler` | `0x1d6b8960` / env+2144 | **false** |
| `xla_latency_hiding_scheduler_rerun` | rerun count | 1 |

---

## SchedulerConfig POD (the config delta)

### Purpose

`GetSchedulerConfig` (`0x10974aa0`) builds the 137-byte `SchedulerConfig` POD that `DefaultSchedulerCore` copies in and `RunImpl` reads. In the post_layout path it is called at line 802 as `GetSchedulerConfig(out, memory_limit_bytes=a9, env, target, is_lem)`, where `is_lem = ModuleContainsLEMSparseCoreInstruction(module)`. The *builder is identical* to the pre-fusion variant; the delta is in the *values* fed at the callsite — chiefly `memory_limit_bytes` (final budget vs pre-fusion budget) and the `v45` post-SC-assignment rerun byte.

### Algorithm

```c
function GetSchedulerConfig(out, mem_limit_bytes, env, target, is_lem):  // 0x10974aa0
    v45 = env[4097]              // xla_tpu_rerun_latency_hiding_scheduler_post_sc_assignment
    sc_queue = EnableSparseCoreOffloadQueuingInLhs(env, target)          // line 30
    multislice = target.GetMultiSliceTopology()

    out.collective_broadcast_overlap_limit = 1                          // off 0
    out.collective_permute_overlap_limit   = env[3964]                  // off 8
    out.all_to_all_overlap_limit           = 1                          // off 16
    out.all_gather_overlap_limit           = 1                          // off 24

    if sc_queue:                                                        // lines 37-42
        out.all_reduce_overlap_limit      = INT32_MAX                   // off 32
        out.send_recv_overlap_limit       = INT64_MAX                   // off 40
        out.host_send_recv_overlap_limit  = INT64_MAX                   // off 48
    else:
        out.all_reduce_overlap_limit      = env[3960]
        out.send_recv_overlap_limit       = AutoOr<long>(env+1120, dflt=1)
        out.host_send_recv_overlap_limit  = AutoOr<long>(env+1128, dflt=1)

    out.parallel_collective_overlap_limit = env[3644]                   // off 56
    out.max_concurrent_host_send_recv     = MaxConcurrentHostSendRecv(env,...) // off 64
    out.max_async_call_overlap_limit      = env[5000]                   // off 72

    // memory_limit (off 80): -1 (unlimited) UNLESS a rerun or pressure-tracking
    out.memory_limit = -1                                              // lines 66-71
    if v45:                          out.memory_limit = mem_limit_bytes
    if EnableSchedulerMemoryPressureTracking(env, multislice, target):
                                     out.memory_limit = mem_limit_bytes

    out.aggressive_scheduling_policies = env[5384]                     // off 88
    out.force_send_recv_to_use_resource = env[4912]                    // off 96
    out.selective_resource_min_limit   = 1                             // off 104
    out.target_scheduling_rule_priority = 2                            // off 112

    out.schedule_send_recvs = v45 | ScheduleSendRecvs(env,...)         // off 120, line 76
    out.flush_partial_schedule_on_busy_resource = 0                    // off 121
    // off 122-125: packed bool quad via vpinsrb (lines 81-88):
    //   schedule_send_recv_host_on_same_resource, use_real_cost_model,
    //   use_aggressive_scheduling  ← use_real_cost_model lands at off 123
    out.enable_release_start_policy        = env[4583]                 // off 126
    out.enable_send_recv_aggregation       = env[4789] | v45           // off 127, line 90
    out.flush_loop_collective_overlap      = env[4946]                 // off 128
    out.treat_loop_collective_as_non_collective = env[4871]           // off 129
    // off 130-133: packed via vpinsrw/vpshufb (lines 105-117):
    //   release_start_policy_with_resource = AutoOr<bool>(env+1928)
    //   release_one_start_policy_per_iter  = AutoOr<bool>(env+1936)
    //   prioritize_non_collective_over_idle_cycles = AutoOr<bool>(env+2432)
    out.disable_loop_consume_resource = (~AutoOr<bool>(env+2104) & 0x101)==0  // off 134
    out.enable_idle_count_extension   = (~AutoOr<bool>(env+2496) & 0x101)==0  // off 135
    out[136] = 0
    out[144..159] = 0   // vmovdqu xmm0 zero — resource arrays
    out[160] = 0        // scheduler-rerun-count slot
```

### The `v45` post-SC-assignment rerun gate

`v45 = env[4097]` is `xla_tpu_rerun_latency_hiding_scheduler_post_sc_assignment`. It is the *one* field that diverges the post_layout **rerun** from a first post_layout run. When set, it forces three otherwise-conditional fields:

| Field (offset) | First run | Rerun (`v45` set) |
|---|---|---|
| `memory_limit` (80) | `-1` unless pressure-tracking | `mem_limit_bytes` (always real) |
| `schedule_send_recvs` (120) | `ScheduleSendRecvs(env)` | forced **on** (`v45 \| …`) |
| `enable_send_recv_aggregation` (127) | `env[4789]` | forced **on** (`env[4789] \| v45`) |

The rationale: after SparseCore assignment, host/ICI send/recv ops must be ordered tightly around the now-placed SparseCore work, and the schedule is re-derived against a real memory budget rather than the unlimited first pass.

> **GOTCHA —** the SparseCore-offload-queue override (`sc_queue`) and the `v45` rerun gate are independent. The `EnableSparseCoreOffloadQueuingInLhs` branch forces collective/send-recv *overlap limits* to INT32/INT64_MAX so collectives overlap freely with the SparseCore offload stream; `v45` forces *send/recv scheduling and aggregation* on. A reimplementation that conflates them will mis-set the overlap envelope on either a non-rerun SC build or a rerun non-SC build.

### Difference vs the pre-fusion variant

Identical builder, identical 137-byte offset schema. The behavioural difference is entirely in the callsite values: the pre-fusion slot would pass a pre-fusion memory budget and `v45 == 0`; the post_layout call passes the final budget and may carry a non-zero `v45` on the SC-assignment rerun. Because the pre-fusion LHS is **not wired** in 0.0.40, the only `SchedulerConfig` this builder actually produces for a live LHS is the post_layout one (line 802).

---

## SchedulingContext (estimator + tracker)

### Purpose

`AddPass<LatencyHidingScheduler>` takes two ctor args: a `shared_ptr<const SchedulingContext>` and a `unique_ptr<DefaultSchedulerCore>`. The `SchedulingContext` wraps the `LatencyEstimator` (op-latency model) and the `AsyncTracker` (resource model). At this phase the estimator is shared between the LHS and the SparseCoreQueueAssignment pass (one `shared_ptr`, built at line 849), and the tracker carries the reserved-SparseCore set.

### LatencyEstimator selection

`GetLatencyEstimator` (`0x10974e00`) branches on `SchedulerConfig` offset 123 (`use_real_cost_model`), set by `SchedulerUsingRealCostModel(env, multislice, target, is_lem)`:

```c
function GetLatencyEstimator(target, config, env):   // 0x10974e00
    if config.use_real_cost_model != 1:              // off 123, line 55
        // lightweight ApproximateLatencyEstimator wrapping DefaultGetCanonicalAsyncOp
        if PGLE_profile_path is set:
            FATAL "Attempted to use profile-guided latency estimator without "
                  "the real cost model. Either enable "
                  "--xla_tpu_scheduler_using_real_cost_model or unset "
                  "--xla_tpu_impure_latency_hiding_scheduler_profile_path."  // line 70
        return ApproximateLatencyEstimator()
    else:
        est = CostModelLatencyEstimator(target, config, env,           // 0x10ff8a60, line 91
                  cost_model_opts = (env[5070] == 1))   // EmitterLearnedCostModel toggle
        if PGLE_profile_path non-empty:
            proto = tsl::ReadBinaryProto(path)         // line 99; InvalidArgument on failure
            est = ProfileGuidedLatencyEstimator(est, proto)            // line 104
        return est
```

In the post_layout path the estimator is built twice — at line 828 for the LHS context and at line 1195 for SparseCoreQueueAssignment — but both share one instance via the context's refcount block. See [`learned-cost-model-client`](../cost/learned-cost-model-client.md) for the `CostModelLatencyEstimator` internals and [`cost/overview`](../cost/overview.md) for the cost-model family.

### AsyncTracker / resource model

`GetTpuAsyncTracker` (`0x10975520`) → `TpuAsyncTracker::Create` (`0x10ffb3e0`). The base `xla::AsyncTracker` resource classes come from three `.rodata` constants verified present in the binary — `XLA_LATENCY_HIDING_SCHEDULER_RESOURCE_SHARING`, `..._RESOURCE_SERIALIZING`, `..._ENABLE_SELECTIVE_RESOURCES` — driven by the corresponding `xla_latency_hiding_scheduler_resource_{sharing,serializing}` / `..._enable_selective_resources` flags. The TPU subclass adds resource classes for the ICI link, HBM bank, per-megacore queue, host-DMA tap, and (when `EnableSparseCoreOffloadQueuingInLhs`) the SparseCore offload queue. The full resource taxonomy is documented in [`scheduler-resourcetype-model`](scheduler-resourcetype-model.md).

> **NOTE —** the distinctive post_layout behaviour is the **reserved-SparseCore set**. RunHloScheduler lines 869–983 build a `flat_hash_set<int64_t>` from `GetReservedSparseCores` (line 873) and pass it as the final `Create` argument. These are the SparseCore IDs withheld from the overlap pool — information that only exists *after* SparseCore assignment, so the pre-fusion slot (running earlier) would pass an empty set. This is the AsyncTracker delta between the two variants.

> **GOTCHA (LOW) —** the per-physical-resource mapping of the numeric arguments `GetTpuAsyncTracker` (`0x10975520`) reads from the env and forwards into `TpuAsyncTracker::Create` was recovered by offset but **not** named 1:1 to ICI vs HBM vs megacore queue. The observed reads are the two `_QWORD` resource limits at env+4568 / +5320, the byte flags at env+4867 / +5086, a `_DWORD` at env+5412, and the five `AutoOr<long>(env+{2368, 2376, 2384, 2392, 2400})` per-kind limits (plus env+2696). A reimplementer must treat the specific resource each limit gates as inferred.

---

## Pass Body — shared RunImpl

### Purpose

`AddPass<LatencyHidingScheduler>` (the templated add at `0x10975c40`) is called at two mutually-exclusive sites, both feeding the same `RunImpl`:

| Site | Line | When taken | Core built at |
|---|---|---|---|
| 1 | 1137 | common path | `DefaultSchedulerCore` at line 1046 |
| 2 | 1411 | SparseCore-queue path — a fresh `GetTpuAsyncTracker` (1349) re-keys the context with the reserved-SC set, then a fresh `make_unique<DefaultSchedulerCore>` (1400) replaces the core | `DefaultSchedulerCore` at line 1400 |

Both invoke the *same compiled* `RunImpl` at `0x136321a0` — the unmodified upstream `xla::LatencyHidingScheduler::RunImpl`. There is exactly one `RunImpl` in the binary; the variants differ only in the config and context it receives.

### DefaultSchedulerCore wiring

The core ctor (`0x10976ce0`) copies the 137-byte config POD (a `vmovups` block from `[rcx]…[rcx+0x69]` into `[core+0x10]…[core+0x79]`) and installs four `std::function` slots:

| Core offset | Slot | post_layout default |
|---|---|---|
| `+0xB8` | `target_overlap_limit` `fn(Shape const&)→long` | `RunTensorCoreAsyncOpScheduler::$_0` (line 1040) |
| `+0xD8` | `candidate_compare_` `fn(ScheduleCandidate&,…)→optional<CandidateResult>` | **empty** (line 1000) — built-in list-scheduler heuristic |
| `+0xF8` | `post_step_mutator_` `fn(SchedulingState&)→void` | **empty** (line 1002) |
| `+0x118` | `should_schedule_` `fn(HloInstruction const*)→bool` | `$_1` (ILP) or `$_2` (regular) (lines 1086/1097) |

The `should_schedule_` classifier swap is the *ILP variant* (see [`lhs-ilp-variant`](lhs-ilp-variant.md)): `EnableIlpLatencyHidingScheduler(env)` (line 1084) selects the wider `$_1` async set vs the narrower `$_2`. This choice happens *before* `AddPass` and only changes which instructions the core treats as async candidates — the `RunImpl` body is unchanged.

> **QUIRK —** `candidate_compare_` is **empty** in the post_layout (and pre-fusion) variant. An empty compare function means `RunImpl` falls back to the built-in list-scheduler priority: DAG-ready first, then memory-pressure-reducing, then longest-critical-path-remaining, then stable instruction index. A reimplementer who expects a custom comparator here will not find one — the TPU backend relies on the default heuristic plus the resource model, not a bespoke priority function.

### Algorithm — RunImpl body

```c
function LatencyHidingScheduler_RunImpl(module, ctx, core):   // 0x136321a0
    VLOG(5) << module->ToString()                             // source 4106
    CHECK(module->has_schedule(),                             // line 280, source 4110
          "LatencyHidingScheduler expects a base schedule "
          "that minimizes memory pressure.")

    computations = MakeComputationPostOrder(module)           // line 320
        .filter(c -> c.caller_instructions() async-relevant)  // line 332
    VLOG(2) << "Computations to schedule " << computations    // line 578

    for each computation:
        VLOG << "] Statistics before scheduling:"             // line 673
              << LatencyHidingStatistics(...).ToString()      // 0x13630b60 / 0x13631a40
        ScheduleComputation(core, computation)   // candidate-list list scheduler
        // memory-pressure retry loop below
        VLOG << "] Statistics after scheduling:" << ...       // line 1146
    set_schedule(module, new_order)
```

> **GOTCHA —** the `has_schedule()` CHECK is a `LogMessageFatal` (line 280). The LHS will abort the compile, not gracefully no-op, if invoked without a prior memory schedule. This is *the* reason `final_scheduler` must run first and is *the* reason the pre-fusion slot cannot run a live LHS in 0.0.40.

### Memory-pressure retry loop

Recovered from the `RunImpl` decompile (lines 873–919, source 4182). When the produced schedule exceeds the memory limit, the scheduler **tightens** the limit by a multiplicative factor (`0.9`, a 10% shrink — see the byte-anchored [core page](latency-hiding-scheduler-core.md)) and reschedules, driving the comparator's memory-pressure keys to fire harder:

```c
// only if config.estimate_fragmentation_size (a SchedulingContext config byte)
int64_t frag = estimate_fragmentation_size
             ? EstimateFragmentationSize(module, alias_analysis, alias_info)  // TPU-private
             : 0;
int64_t mem_limit   = core.memory_limit();        // vtable +80
int     max_retries = core.memory_limit_retries();// vtable +88
for (int attempt = 0; attempt < max_retries; ++attempt) {
    int64_t used = frag + core.computed_use();     // vtable +64
    if (used <= mem_limit) break;
    LOG(INFO) << "LatencyHidingScheduler current memory usage: " << used
              << " bytes, does not fit in initial limit: " << mem_limit
              << ". Setting the new limit to " << core.next_memory_limit();  // line 881
    if (core.SchedulerInitialize(module) != ok) break;
    mem_limit = core.next_memory_limit();          // vtable +80, relax-by-factor
    core.set_memory_limit(mem_limit);
    reschedule_all_computations();
    if (estimate_fragmentation_size) frag = EstimateFragmentationSize(...);
}
```

`next_memory_limit()` is the AVX shrink-by-factor: `vmulsd xmm0, xmm0, [qword_A2DFD10]` (the scale constant `0.9`) followed by a `vsubsd`/`vcvttsd2si` clamp and a sign-extend — i.e. `new_limit = clamp(floor(old_limit * 0.9))`. In the post_layout variant this loop is *live* (budget is real) whenever `EnableSchedulerMemoryPressureTracking` is true **or** the `v45` rerun byte is set; otherwise `memory_limit == -1` and the loop is inert.

> **GOTCHA (LOW) —** `EstimateFragmentationSize` is TPU-private (same name as OSS, different TU); its peak-live-with-fragmentation model and allocator-alignment assumptions were not traced beyond the `GlobalDecreasingSizeBestFitHeap` HeapSimulator run named on the [core page](latency-hiding-scheduler-core.md). Treat the fragmentation model as inferred.

---

## post_layout vs pre-fusion at a glance

| Aspect | pre-fusion | post_layout (this page) |
|---|---|---|
| Pipeline / phase | "Pre main fusion" (Phase 6) | `async_scheduling` in `RunHloScheduler` (Phase 7) |
| Wired in v0.0.40? | NO (no AddPass site reached) | YES (lines 1137 / 1411) |
| `RunImpl` | `0x136321a0` (shared) | `0x136321a0` (shared, identical body) |
| Base schedule source | (would need a prior memory sched) | `HloMemorySchedulerWithBrkgaFallback` (line 567) |
| `SchedulerConfig` builder | `GetSchedulerConfig` (same) | `GetSchedulerConfig`, final budget + `v45` rerun gate |
| LatencyEstimator | `GetLatencyEstimator` (same helper) | `GetLatencyEstimator` (same helper) |
| AsyncTracker | `GetTpuAsyncTracker`, no reserved-SC | `GetTpuAsyncTracker` + reserved-SparseCore set |
| `candidate_compare_` | empty (heuristic) | empty (heuristic) |
| Async classifier | `$_2` (or `$_1` under ILP gate) | `$_2` (or `$_1` under ILP gate) |
| `memory_limit` (off 80) | `-1` unless pressure-tracking | `-1` unless pressure-tracking **OR** `v45` rerun |

The page therefore documents the *one* live LHS in the build. The pre-fusion slot exists in the code but is dead in 0.0.40; the ILP variant is a flag-gated classifier swap *inside* this same function.

---

## Function Map

| Function | Address | Role |
|---|---|---|
| `RunHloScheduler` | `0x1096fac0` | Phase-7 driver; builds both pipelines, gates and adds the LHS |
| `LatencyHidingScheduler::RunImpl` | `0x136321a0` | Shared pass body; has_schedule CHECK, list scheduler, retry loop |
| `GetSchedulerConfig` | `0x10974aa0` | 137-byte `SchedulerConfig` POD; `v45` rerun gate |
| `GetLatencyEstimator` | `0x10974e00` | Approximate / CostModel / PGLE estimator selection |
| `GetTpuAsyncTracker` | `0x10975520` | Wraps `TpuAsyncTracker::Create` |
| `TpuAsyncTracker::Create` | `0x10ffb3e0` | TPU resource model + reserved-SC set |
| `CostModelLatencyEstimator` ctor | `0x10ff8a60` | Real cost model (object size `0x3D0`) |
| `DefaultSchedulerCore` ctor | `0x10976ce0` | Copies config; installs four `std::function` hooks |
| `AddPass<LatencyHidingScheduler, …>` | `0x10975c40` | Templated pass adder (sites 1137/1411) |
| `IsPassDisabled` | `0x12fd8340` | Pass-enable/disable resolution |
| `EnableLatencyHidingLayerScheduler` | `0x1d6b8960` | Layer-scheduler gate (default false) |
| `EnableSchedulerMemoryPressureTracking` | `0x1d6b66e0` | `memory_limit` tri-state |
| `SchedulerUsingRealCostModel` | `0x1d6b6580` | Sets `use_real_cost_model` (off 123) |
| `EnableIlpLatencyHidingScheduler` | `0x1d6b7e00` | `$_1`/`$_2` classifier gate |
| `HloMemorySchedulerWithBrkgaFallback::RunImpl` | `0x10abe6e0` | Base memory schedule |

---

## Cross-References

- [LatencyHidingScheduler Core](latency-hiding-scheduler-core.md) — the `ScheduleComputation` candidate-list loop and async tracker shared by all variants
- [LHS post_layout_pre_fusion Variant](lhs-post-layout-pre-fusion.md) — same `RunImpl`, wired at the dead Phase-6 "Pre main fusion" slot
- [LHS ILP Variant](lhs-ilp-variant.md) — the `$_1`/`$_2` async-classifier swap (`EnableIlpLatencyHidingScheduler`) inside this same function
- [Scheduler ResourceType Model](scheduler-resourcetype-model.md) — the ICI/HBM/megacore/host-DMA/SparseCore resource taxonomy the `AsyncTracker` enforces
- [Scheduling Overview](overview.md) — where scheduling sits between lowering and encoding
- [Cost Model Overview](../cost/overview.md) — the latency/cost model family feeding `GetLatencyEstimator`
- [Learned Cost Model Client](../cost/learned-cost-model-client.md) — the `CostModelLatencyEstimator` real-cost-model path
- [Part VIII index](../index.md) — Instruction Scheduling & Bundle Packing
