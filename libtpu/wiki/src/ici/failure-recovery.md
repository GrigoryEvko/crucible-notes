# ICI Failure Detection, Degraded-Axis Fallback, and Recovery

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). Other versions will differ. `.text` VMA equals file offset; all addresses are VMA.*

## Abstract

When an Inter-Chip Interconnect (ICI) link on a TPU slice misbehaves, libtpu does **not** dynamically reroute traffic. The on-chip routing tables are installed once at bring-up and never rewritten in response to a live drop. What libtpu *does* have is a four-layer detection stack — firmware-state read, an interrupt-driven status-change handler, an on-demand health predicate with a retry-rate budget, and a cross-host error-report broadcast RPC — feeding a binary soft/hard verdict. A soft fault is recovered in-band by re-running the bring-up sequence on the affected link (`LinksDownReset` → re-enable → wait-for-data-link-up → GTC re-sync). A hard fault tears down the device through a `FailDevice` cascade and escalates out-of-band to the slice master and the Megascale aggregator; the recovery unit is the whole slice's restart.

Two slice-builder mechanisms named in the binary anchor the cross-host story and are documented here for the first time at decompile depth. `Master::ControlIciErrorReport` (`0x1fbc0d00`) is the master's fan-out broadcast: it stamps a deadline (`absl::Now() + timeout`), attaches the `IsLimitedIciRouting` flag, and calls every worker's `SliceBuilderWorkerService` stub; `Worker::ControlIciErrorReport` (`0x1fc40d80`) walks its per-peer stub list under a shared mutex, builds one closure per peer, and runs them in parallel on a `ThreadPool` via `RunInParallel`. The companion `Master::DetectRoutingTableDeadlock` (`0x1fbbed60`) is a *static* deadlock check run at slice-init time: it regenerates each chip's `superpod::routing::RoutingTableSet`, hands them to a `RoutingTableAnalyzer`, and on a detected channel-dependency cycle returns `FAILED_PRECONDITION` with the bug-filing string — it validates the chosen (possibly resilient) route table is deadlock-free *before* the slice runs, rather than detecting a runtime hang.

A reader who knows MPI fault-tolerant collectives and credit/channel-dependency deadlock theory owns the frame. The degraded-axis *proto ingest* (how a faulty-link orientation becomes three booleans that fold one torus axis out of the collective ring) is on [Degraded-Axis Ingest](../collectives/degraded-axis.md); the *resilient route cache* on [Resilient Route-Cache Dedup](../routing/route-cache-dedup.md); link bring-up on [Link Bring-Up Sequence](link-bringup.md). This page owns the **detection stack, the soft/hard classifier, the degraded fallback selection, the routing-table deadlock detector, and the recovery/escalation flow**.

For reimplementation, the contract is:

- **The detection stack** — `IsLinkUp` (firmware-state read, port `< 5` bound), `HandleIciLinkStatusChange` (interrupt-driven, `AllLinksUp` compare → `Link error.`), `IsHealthy` (link-up AND retry-rate-within-budget), and `UpdateAndGetRetriesPerMinute` (60-second sliding-window deque).
- **The soft/hard split** — `FatalErrorCheck` reading two firmware fatal bits (vtable +0x20, +0x28) → `Fatal error occurred. Data links will go down.`; the soft path re-runs bring-up; the hard path signals a deferred failure into the `FailDevice` cascade.
- **The degraded fallback** — routing is static; resilience is fault-aware *table selection* gated by `UseResilientAlgorithmTwistedTorus` under super-pod fault symmetry, with the faulty-link axis carried as three degraded bytes.
- **The deadlock detector** — `DetectRoutingTableDeadlock` → `RoutingTableAnalyzer::DetectPotentialDeadlock` over the per-chip `RoutingTableSet` channel-dependency graph, run at slice init.
- **The escalation cascade** — per-chip deferred failure → `FailDevice` → `SliceFailureType::CHIP_DRIVER_ERROR` → `ControlIciErrorReport` broadcast / `FailSlice` → tpunetd session-failing → Megascale `NETWORKING_ISSUE`.

| | |
|---|---|
| **Firmware link-state read** | `jxc::IciControl::IsLinkUp(int)` @ `0xe7afe80` (per-port bit `(state>>link)&1`, port `< 5`) |
| **Interrupt status handler** | `jxc::IciControl::HandleIciLinkStatusChange(Span<int>,bool)` @ `0x21381e40` |
| **All-links-up compare** | `jxc::IciControl::AllLinksUp(Span<int>,bool)` @ `0xe7b0200` |
| **Health predicate** | `jxc::IciControl::IsHealthy(int)` @ `0xe7af720` (4-port loop) |
| **Retry-rate window** | `jxc::IciControl::UpdateAndGetRetriesPerMinute(RetryHistory*,long)` @ `0xe7af540` (60 s deque) |
| **Fatal gate** | `ici::SliceConfiguration::FatalErrorCheck()` @ `0x1fdb6720` (fw bits +0x20/+0x28) |
| **Chip-health rollup** | `ici::SliceConfiguration::GetChipHealth()` @ `0x1fdb6320` |
| **Link-down recovery** | `ici::SliceConfiguration::LinksDownReset()` @ `0x1fdb5c00` (legacy `0x1fe82f20`) |
| **Error masking** | `ici::SliceConfiguration::MaskIciErrorsInternal(IciErrorType,bool)` @ `0x1fdb6ec0` |
| **Cross-host error report** | `Master::ControlIciErrorReport` @ `0x1fbc0d00` / `Worker::ControlIciErrorReport` @ `0x1fc40d80` |
| **Routing-table deadlock** | `Master::DetectRoutingTableDeadlock()` @ `0x1fbbed60`; `RoutingTableAnalyzer::DetectPotentialDeadlock` @ `0x1fbcd520` |
| **Degraded fallback gate** | `xla::jellyfish::UseResilientAlgorithmTwistedTorus` @ `0x1c894fc0`; `GetDegradedAxis` @ `0x1c894c20` |
| **Confidence** | HIGH (decompile-verified bodies for detection, fatal gate, error-report broadcast, deadlock detector, link-down reset) unless a row/callout says otherwise |

---

## Where This Sits

ICI failure handling is the fault-tolerance layer beneath the all-reduce / DMA primitives. It is layered from fastest/lowest to slowest/highest; each layer observes a different granularity of failure and either absorbs it, recovers it in-band, or escalates it.

```text
Layer 0  HARDWARE/FIRMWARE  — SerDes PHY, link-stack fw, NIU credit FSM
         absorb transient bit errors (CRC/lane retry); host sees only outcomes
              │  port_ready_state code, link-up bit, fatal bit, stall counters
              ▼
Layer 1  DRIVER INTERRUPT   — Ici::HandleIciLinkInterrupt → HandleIciLinkStatusChange
         per-chip, per-event; decides recoverable (retrain) vs deferred-fatal
              ▼
Layer 2  DRIVER HEALTH POLL — IciControl::IsHealthy(link)
         link-up bit AND retry rate (60 s window) → 0–10 ici_link_health
              ▼
Layer 3  SLICE-WIDE CHECK   — LinkChecker / MeshVerifier; GetChipHealth rollup
         the whole chip×link set vs the expected toroidal mesh
              ▼
Layer 4  CROSS-HOST REPORT  — Master::ControlIciErrorReport broadcast
         IciSessionMonitor CHECK_SESSION_HEALTH; missed_health_check watchdog
              ▼
Layer 5  MEGASCALE AGGREGATION — ReportError → MegascaleErrorAggregator
         whole-pod digest → Cause::NETWORKING_ISSUE (error-aggregator.md)
```

The static-deadlock pre-check (`DetectRoutingTableDeadlock`) sits off to the side of this stack: it runs at slice **init**, not at runtime, validating that the route table chosen by discovery — including a resilient table picked to route around a known-bad link — does not contain a channel-dependency cycle. Upstream is link discovery and bring-up ([Link Bring-Up Sequence](link-bringup.md), [Topology Discovery](topology-discovery.md)); downstream consumers are the collective picker ([Degraded-Axis Ingest](../collectives/degraded-axis.md)) and the cross-pod aggregator ([Megascale Error Aggregator](../megascale/error-aggregator.md)). The overview of the whole ICI fabric is on [ICI Overview](overview.md).

> **NOTE —** cross-host escalation does not run through the Megascale RPC alone: the slice-builder layer has its own dedicated ICI error-report broadcast. `Master::ControlIciErrorReport` and `Master::DetectRoutingTableDeadlock` are present in the binary as full slice-builder symbols (`accel_ssw::deepsea::slice_builder`), are referenced from `Master::InitSlice` (`0x1fbbaac0`), and their bodies are decompiled below.

---

## Link-Down / Link-Error Detection

### Purpose

Establish, from the host side, whether a given ICI link is alive — using the firmware mailbox, the interrupt-driven status change, and an active liveness probe — and convert per-link error activity into a single retry-rate signal that feeds the health verdict.

### Entry Point

```text
Ici::HandleIciLinkInterrupt (jfc 0xe7adc80 / dfc 0xe76fe80)   ── driver IRQ, under mutex
  └─ IciControl::HandleIciLinkStatusChange (0x21381e40)        ── interrupt status handler
       ├─ IciControl::AllLinksUp (0xe7b0200)                   ── expected-up compare
       └─ MakeErrorImpl<13> "Link error."                      ── on not-all-up
IciControl::IsLinkUp (0xe7afe80)                               ── firmware-state read (on demand)
IciControl::IsHealthy (0xe7af720)                             ── 4-port health poll
  └─ IciControl::UpdateAndGetRetriesPerMinute (0xe7af540)      ── 60 s retry-rate window
```

### Algorithm

`IsLinkUp` reads the firmware port-ready state through the link-stack interface and bounds-checks the port index against 5:

```c
function IsLinkUp(link):                 // IciControl::IsLinkUp @ 0xe7afe80
    if link >= 5:                        // cmp $0x5 — ports 0..4 valid; ≥5 fails
        return MakeErrorImpl<3>("Invalid link number %d", link)  // kInvalidArgument, line 153
    state = fw->ReadPortReadyState()     // vtable+0x40 on the firmware-comm object, line 150
    if link == 4:                        // default switch arm inside the <5 branch
        LOG(FATAL) "port_ready index is invalid. "
    return (state >> link) & 1           // per-port bit, ports 0..3; up iff bit set
```

> **QUIRK —** the bound is `< 5`, not `< 4`. The host models up to **5** ICI ports per chip (4 physical SerDes + 1 reserved/loopback slot). The `< 5` index check returns `kInvalidArgument` (`"Invalid link number %d"`, line 153) only for `link >= 5`; index `4` passes the bound but hits a `LOG(FATAL)` default switch arm (`"port_ready index is invalid. "`), so only ports `0..3` actually resolve. `IsHealthy` iterates only those 4 physical ports. A reimplementation that sizes the port array at 4 will overflow on the reserved index. (The fuller `"port_ready_state index is invalid."` string belongs to the sibling `GetLinkStackReadyState` @ `0xe7afd00`, which extracts a 4-bit nibble per port rather than a single bit.)

`HandleIciLinkStatusChange` is the interrupt-driven reaction, verified at `ici_control.cc:193/199/201/202`:

```c
function HandleIciLinkStatusChange(links, links_are_enabled):   // 0x21381e40
    LOG(INFO) << "Got link status change from device, "
                 "links_are_enabled_: " << links_are_enabled    // ici_control.cc:193
    if not links_are_enabled:
        return OK                                               // disabled → nothing to check
    st = AllLinksUp(links, /*expect_up=*/true)                  // 0xe7b0200, ici_control.cc:199
    if st.ok():
        if all_up:                                              // AllLinksUp true → no action
            return OK
        LOG(INFO) << "Link status changed to down."             // ici_control.cc:201
        return MakeErrorImpl<13>("Link error.")                 // INTERNAL, ici_control.cc:202
    return StatusBuilder(st) << "while HandleIciLinkStatusChange"
```

`MakeErrorImpl<13>` is the `absl::StatusCode::kInternal` constructor; the trailing `StatusBuilder(...) << "while …"` wraps a non-OK `AllLinksUp` result with context. The non-OK return is what `Ici::HandleIciLinkInterrupt` (the caller, under the driver mutex) inspects to decide whether to schedule a deferred failure.

The retry-rate signal is a true sliding window — `UpdateAndGetRetriesPerMinute` maintains a deque and a running sum:

```c
function UpdateAndGetRetriesPerMinute(history, delta):   // 0xe7af540
    history.deque.push_back({absl::Now(), (int)delta, delta})
    history.sum += delta                                 // running sum at object+0x30
    cutoff = absl::Now() - Seconds(60)                   // mov $0x3c (=60) → Duration
    while history.deque.front().time < cutoff:           // evict stale entries
        history.sum -= history.deque.front().weight
        history.deque.pop_front()                        // frees a head block past 0x154 entries
    return (double) history.sum                          // retries in the trailing 60 s
```

This is the per-link enforcement point for `max_ici_retries_per_minute`. The deque is bounded: it grows via `__add_back_capacity` (`0xe7b58e0`) and frees the head block when it crosses 0x154 entries, so the window cannot leak memory under a sustained retry storm.

### Function Map

| Function | Address | Role |
|---|---|---|
| `IciControl::IsLinkUp(int)` | `0xe7afe80` | Firmware port-ready read, port `< 5` bound |
| `IciControl::HandleIciLinkStatusChange` | `0x21381e40` | Interrupt status handler → `Link error.` |
| `IciControl::AllLinksUp(Span<int>,bool)` | `0xe7b0200` | Expected-up set compare |
| `IciControl::IsHealthy(int)` | `0xe7af720` | Per-link health = up AND retry-in-budget |
| `IciControl::UpdateAndGetRetriesPerMinute` | `0xe7af540` | 60 s sliding-window deque sum |
| `Ici::HandleIciLinkInterrupt` | `0xe7adc80` (jfc) / `0xe76fe80` (dfc) | Driver IRQ entry, under mutex |
| `LinkChecker::CheckLinks` | `0x1fc38580` | Post-bring-up active liveness probe |

### Considerations

There is **no host-visible lane-degradation / width-fallback path** for ICI. An exhaustive search finds the only half-width / L0p / lane-retry prose in the binary describes Intel QPI/UPI host-uncore counters (`UNC_IO_LINK_NUM_RETRIES`), not ICI. SerDes width negotiation and per-lane deskew are firmware-owned: if a lane degrades, firmware either renegotiates transparently (visible only as added training latency before `port_ready_state` reaches "up") or declares the link unusable (the link-down / fatal path). The host's only continuous quality signal for a degraded-but-up link is the 0–10 `ici_link_health` score — a degraded link shows as 1–9 rather than 0 or 10, driven by an elevated retry rate. (MEDIUM: the exact rate→score curve is not recovered.)

---

## Soft vs Hard Failure — the Classifier

### Purpose

Reduce an ICI link event to one of two outcomes: a *soft* fault recovered in-band by re-running bring-up, or a *hard* fault that tears the device down and escalates. The discriminator is the firmware fatal bit plus the retry-rate budget.

### Algorithm

`FatalErrorCheck` is the gate, verified at `slice_configuration.cc:739/741/743/746`:

```c
function FatalErrorCheck():                       // ici::SliceConfiguration @ 0x1fdb6720
    hw_fatal_st  = fw->ReadHardwareFatal()        // vtable+0x20 on fw object (**this+49)
    if not hw_fatal_st.ok():
        return AddSourceLocation(hw_fatal_st, line 739)
    hardware_fatal = hw_fatal_st.value
    net_fatal_st = fw->ReadNetworkFatal()         // vtable+0x28
    if not net_fatal_st.ok():
        return AddSourceLocation(net_fatal_st, line 741)
    network_fatal = net_fatal_st.value
    if hardware_fatal | network_fatal:            // line 743
        LOG(ERROR) << "!!!! FATAL ERROR !!!! for "
                   << " hardware_fatal: " << hardware_fatal
                   << " network_fatal: "  << network_fatal
        return MakeErrorImpl<13>(                  // INTERNAL, line 746
            "Fatal error occurred. Data links will go down.")
    return OK
```

The log line appends `hardware_fatal` (read at vtable+0x20, line 739) *before* `network_fatal` (vtable+0x28, line 741), under the literal prefix `"!!!! FATAL ERROR !!!! for "`. The verdict, as the driver follows it on an ICI link event:

```text
ICI link IRQ → HandleIciLinkInterrupt → HandleIciLinkStatusChange
  AllLinksUp == true ?
    YES → transient blip, no action (counted via retry rate)
    NO  → IsHealthy(link) ?  (link-up AND retry < max_ici_retries_per_minute,
                              AND no firmware fatal bit)
            YES → SOFT: schedule LinksDownReset + re-run bring-up;
                  chip stays in slice; health score 1–9
            NO  → FatalErrorCheck(): network_fatal or hardware_fatal set,
                  OR retry budget persistently exceeded
                → HARD: SignalDeferredFailure → FailDevice cascade;
                  health score 10; core dump; CHIP_DRIVER_ERROR;
                  Megascale NETWORKING_ISSUE; host flagged for fleet removal
```

| Soft (recoverable) markers | Hard (fatal) markers |
|---|---|
| link-up bit clears transiently | `!!!! FATAL ERROR !!!! for` logged |
| retry rate `< max_ici_retries_per_minute` | `network_fatal` / `hardware_fatal` bit set |
| `ici_link_health` score 1–9 | `ici_link_health` score 10 |
| `LinksDownReset` + re-enable + wait-for-DL-up succeeds | `FailDevice` cascade fires |
| GTC re-sync succeeds | `SliceFailureType::CHIP_DRIVER_ERROR` (value 5) |
| no process restart | core dump + Megascale `NETWORKING_ISSUE` + fleet removal |

### Considerations

Soft-failure recovery is in-band (retrain, no process restart). Hard-failure "recovery" is out-of-band: the process exits (or the coordinator log-fatals under `megascale_error_reporter_abort_on_*`), the orchestrator restarts the job, and the *next* bring-up either selects a resilient route table excluding the now-known-bad link (next section, if symmetry permits) or fails discovery because the link is required and unreplaceable. There is no in-place "isolate one chip and continue"; the recovery unit is the whole slice. (LOW: the exact CM-register bit positions behind vtable+0x20 / +0x28, and the default value of `max_ici_retries_per_minute`, are populated from Options at runtime and not in user-facing rodata.)

---

## Recovery — Link-Down Reset and Retrain

### Purpose

Recover a soft fault by re-running the bring-up sequence on the affected link(s). There is no dedicated `RetrainLink()`; "retrain" is `LinksDownReset` followed by the normal enable / wait / GTC-resync path.

### Algorithm

`LinksDownReset` brings every non-down link down and confirms the data-link layer reached a down/disabled state, verified at `slice_configuration.cc:570`:

```c
function LinksDownReset():                       // ici::SliceConfiguration @ 0x1fdb5c00
    lock(this+200)                               // absl::Mutex
    st = CollectDataLinkState()                  // snapshot per-port DL state
    if not st.ok(): return StatusBuilder(st, line 570)
    for port in 0 .. enabled_port_count-1:       // *((this+9)) = port count
        state = data_link_state[port]            // *((this+30))[port]
        if (state - 3) >= 2:                     // states 3,4 = kDown/kDisabled → skip
            continue
        IciPortUser::SetDataLinkLayerState(port, false)   // firmware "turn link down"
        // on failure: "Failed to turn down ICI link %d during slice reset, state=%d"
    CollectDataLinkState()                       // re-confirm all reached kDown/kDisabled
    clear enabled-port list (this+0x110 = 0)
    if ports were enabled: clear bring-up status fields (this+0x120 / +0x128)
```

The full retrain then re-runs bring-up:

```text
1. LinksDownReset           — SetDataLinkLayerState(false) per non-down port
                              umbrella: "Bringing ICI links down." /
                              "Failed to take down links and reset ICI"
2. EnableIci(span)          — re-issue enable_ici_serdes_training → fw re-trains PHY
   WaitForDataLinkUp(dur)   — poll port_ready_state (fixed 1 ms quantum, clamped to the
                              remaining budget when under 1 ms; no second tier — see link-bringup.md §2)
   MaskIciErrors during the window; UnmaskIciErrors once DL-up succeeds
3. ClearGtc → WaitForGtcReset → StartGtc   — tear and rebuild global-time-counter sync
                              failure: "Failed to restart GTC on link "
```

During the retrain window, errors are suppressed through the mask map so the in-progress reset does not itself trip the escalation cascade.

### Function Map

| Function | Address | Role |
|---|---|---|
| `ici::SliceConfiguration::LinksDownReset` | `0x1fdb5c00` | Bring links down, confirm DL down/disabled |
| `SliceConfiguration::LinksDownReset` (legacy) | `0x1fe82f20` | Pre-`ici`-namespace variant |
| `ici::SliceConfiguration::CollectDataLinkState` | (callee of above) | Snapshot per-port DL state |
| `ici::SliceConfiguration::MaskIciErrorsInternal` | `0x1fdb6ec0` | `flat_hash_map<IciErrorType, vector<MaskedErrors>>` |
| `ici::SliceConfiguration::PerformReset` | `0x1fdb71a0` | Slice-wide reset (chip reset path) |
| `ici::SliceConfiguration::GenerateAndSerializeCoreDump` | `0x1fdb5fa0` | Forensic `CORE_DUMP_ICI_DUMP` artifact |
| `KernelPrivilegedInterface::PerformReset(ResetType)` | (per-gen) | Chip-level hard reset when LinksDownReset is insufficient |

### Considerations

The retry-rate budget (previous section) bounds how many times a link may be retrained per minute before it is declared hard-failed. There is **no descriptor-level DMA retry**: a stuck remote DMA is never retried at the descriptor layer — the timeout escalates and the enclosing collective fails. The only "retry" granularity is the link-level retrain. (MEDIUM: `PerformReset` / `KernelPrivilegedInterface::PerformReset` bodies and the `ResetType` enum — warm vs cold vs link-only — were not individually traced.)

---

## Degraded-Axis Fallback — Static, Fault-Aware Routing

### Purpose

Route around a *known-at-bring-up* faulty link by selecting a pre-computed resilient route table — not by recomputing routes at runtime. A live link drop is **never** rerouted; it escalates as a fault.

### Algorithm

Routing tables are generated once and installed once at bring-up; there is no `Reroute`, `RegenerateRoutingTable`, or `RecomputeRoutes` symbol in the binary. Resilience is achieved by *table selection*:

```c
function select_route_table(target, env):       // at bring-up, not at runtime
    axis = GetDegradedAxis(target, faulty_links) // 0x1c894c20 → -1, or 0/1/2 (X/Y/Z)
    if axis >= 0 and UseResilientAlgorithmTwistedTorus(target, env):  // 0x1c894fc0
        // precondition: non-empty faulty set with super-pod fault symmetry
        // "ICI resiliency only supports … super-pod fault symmetry …"
        // "The topology size must be a multiple of the fault symmetry"
        install pre-baked resilient cache:
            cache_ici_resiliency_<codename>_config.binarypb
            cache_ici_resiliency_<codename>_fault_dim_{x,y,z}.binarypb
            cache_ici_resiliency_<codename>_{x,y,z}_data.binarypb
        // routes computed by RandomizedToroidalWildFirstPaths (deadlock-free)
    else:
        install the normal generated table (or fail discovery if the link is required)
```

The faulty-link axis is reduced to three degraded bytes by `tpu::OrientationsToTpuDegradedAxes` (`0x1fc57d00`, orientation `1`→X, `2`→Y, `3`→Z) and consumed by the collective-ring picker. The full proto/POD ingest and the `Target[+0x3f8..+0x3fa]` byte layout are documented on [Degraded-Axis Ingest](../collectives/degraded-axis.md); the resilient cache codec and dedup are on [Resilient Route-Cache Dedup](../routing/route-cache-dedup.md). This page links them rather than duplicating.

> **GOTCHA —** "fault-aware" is **not** "fault-tolerant at runtime". A link that drops during execution is *not* routed around — packets routed onto a dead link back-pressure and stall until the sflag/DMA watchdog fires, then the fault escalates. The resilient table only helps a fault that was already known and conformed to super-pod symmetry when the slice was built. An arbitrary single-link fault that breaks symmetry is **not** reroutable and falls to discovery failure.

### Function Map

| Function | Address | Role |
|---|---|---|
| `xla::jellyfish::GetDegradedAxis` | `0x1c894c20` | Reduce faulty bitset to one axis index or `-1` |
| `xla::jellyfish::UseResilientAlgorithmTwistedTorus` | `0x1c894fc0` | Gate the resilient path |
| `tpu::OrientationsToTpuDegradedAxes` | `0x1fc57d00` | Faulty `Orientation` enum → X/Y/Z degraded bytes |
| Resilient route caches | (rodata blobs) | `cache_ici_resiliency_{pufferfish,viperfish,6acc60406}_*.binarypb` |

---

## Routing-Table Deadlock Detection

### Purpose

Validate, at slice init, that the chosen on-chip route tables (including a resilient table selected to route around a fault) contain no channel-dependency cycle. This is a *static* pre-flight check, not a runtime hang detector.

### Entry Point

```text
Master::InitSlice (0x1fbbaac0)
  └─ Master::DetectRoutingTableDeadlock (0x1fbbed60)        ── per-chip, at init
       ├─ superpod::routing::RoutingTableSet (per chip)     ── regenerated routing tables
       │     err: "Failed to generate routing table set for chip … at <Coordinates>"
       ├─ RoutingTableAnalyzer (chips, 0x7FFFFFFF, map)     ── builds dependency graph
       └─ RoutingTableAnalyzer::DetectPotentialDeadlock (0x1fbcd520)
             → MakeErrorImpl<9> (FAILED_PRECONDITION) on a detected cycle
```

### Algorithm

`DetectRoutingTableDeadlock` (master.cc:705–726) walks every chip, regenerates its `RoutingTableSet`, and feeds the set into a `RoutingTableAnalyzer` that runs a channel-dependency-graph cycle search:

```c
function DetectRoutingTableDeadlock():           // Master @ 0x1fbbed60
    chip_tables = {}                              // flat_hash_map<int, unique_ptr<RoutingTableSet>>
    chip_views  = {}                              // flat_hash_map<int, RoutingTableSet const*>
    for chip in slice.chips:
        set_or = GenerateRoutingTableSet(chip)    // RoutingTableSet ctor per chip
        if not set_or.ok():
            return StatusBuilder(set_or)          // master.cc:55:
                << "Failed to generate routing table set for chip "
                << chip.id << " at " << chip.coords.ToString()
        chip_tables[chip.id] = move(set_or.value)
        chip_views[chip.id]  = chip_tables[chip.id].get()
    t0 = absl::Now()
    analyzer = RoutingTableAnalyzer(slice, /*budget=*/0x7FFFFFFF, chip_views, ...)
    st = analyzer.DetectPotentialDeadlock()       // 0x1fbcd520
    VLOG(1) << "Deadlock detection took " << (absl::Now() - t0)   // master.cc:724
    if st.ok() and st.value == /*deadlock found*/:
        return MakeErrorImpl<9>(                   // FAILED_PRECONDITION, master.cc:726
            "RoutingTableAnalyzer detects a potential deadlock!  "
            "File a bug against SliceBuilder (…).  "
            "Please attach core dumps retrieved from Coroner.")
    return st
```

`RoutingTableAnalyzer::DetectPotentialDeadlock` (`0x1fbcd520`) takes a `BufferId` and a `map<pair<int,int>, int>` and computes an edge set (`Edges`) over the buffer/channel pairs — the classic turn/channel-dependency-graph test for routing deadlock-freedom. The routing entries it analyzes are the `RandomizedUnicastEgressNextHopRoutingEntry` and `CumulativeWeightsEgressRoutingEntryTI<6,12>` variants — the same egress-table shapes that the resilient `RandomizedToroidalWildFirstPaths` generator emits, which is why the deadlock check is run after a resilient table may have been selected.

### Considerations

The `0x7FFFFFFF` budget passed to the analyzer is an effectively unbounded search depth — the check is exhaustive over the per-chip dependency graph rather than time-boxed. Because it runs at init under `InitSlice`, a deadlock-prone route table fails the slice *before* any collective runs, surfacing as `FAILED_PRECONDITION` rather than a mid-run hang. (HIGH for the outer driver and the error path; MEDIUM for the internal cycle-search algorithm inside `DetectPotentialDeadlock`, which was identified by signature — `BufferId` + edge map + `Edges` accumulation — but not traced line-by-line.)

> **NOTE —** this is distinct from the XLA-level collective deadlock verifier (`CheckPendingSendRecvDeadlocks` / `VerifyNoCollectiveDeadlocksRecursive` in the `xla` namespace), which checks HLO send/recv pairing at compile time. `DetectRoutingTableDeadlock` operates on the physical `superpod::routing` tables at slice init, one layer below the HLO graph.

---

## Cross-Host Error Report and Escalation

### Purpose

Propagate a chip-level ICI fault from the failing worker outward: through the device-teardown cascade, the slice-builder error-report broadcast, the tpunetd session monitor, and finally the Megascale aggregator.

### The `ControlIciErrorReport` Broadcast

`Master::ControlIciErrorReport` (`0x1fbc0d00`) is the master's fan-out, verified at `master.cc:1231`:

```c
function Master::ControlIciErrorReport(worker_name, stub):   // 0x1fbc0d00
    req = ControlIciErrorReportRequest()
    req.field6 = 1                                   // request flag
    req.is_limited_ici_routing =
        RoutingTableGeneratorFactory::IsLimitedIciRouting(this+8)
    ctx = ClientContext()
    deadline = absl::Now() + this.rpc_timeout        // Now() += Duration(this+20,this+28)
    ctx.set_deadline(deadline)                       // gpr_inf_future/past clamps handled
    reply = ControlIciErrorReportReply()
    grpc_status = stub->ControlIciErrorReport(ctx, req, &reply)   // vtable+72
    st = GrpcStatusToAbslStatus(grpc_status)
    if not st.ok():
        return StatusBuilder(st, "master.cc", line 1231)
    return OK
```

`Worker::ControlIciErrorReport` (`0x1fc40d80`, worker.cc:378) is the receiving side that fans the report out to its own peers in parallel:

```c
function Worker::ControlIciErrorReport(req):         // 0x1fc40d80
    lock_shared(this+8)                              // absl::Mutex, shared
    closures = []                                    // vector<function<absl::Status()>>
    for peer in this.peer_stub_list:                 // *((this+9)) entries, stride 64
        // capture peer endpoint strings + req.field6 + req.byte28 into a $_0 closure
        closures.push_back(make_report_closure(peer, req))
    pool = ThreadPool(closures.size())
    status = RunInParallel(closures)                 // worker.cc $_0 → ThreadPool
    pool.JoinAll()
    unlock_shared(this+8)
    if status != OK:
        return StatusBuilder(status, "worker.cc", line 378)
    return OK
```

> **QUIRK —** the error report is delivered as a slice-builder gRPC unary call (`SliceBuilderWorkerService::ControlIciErrorReport`, server stub at `WorkerService::ControlIciErrorReport` `0x1fc3cda0`), and it carries the `IsLimitedIciRouting` flag, not just a fault descriptor. The cross-host story is therefore two-stage: the slice-builder broadcast (`ControlIciErrorReport`) propagates the ICI fault and the routing-limitation state across the slice, and *separately* the Megascale `ReportError` path classifies the whole-pod digest. A reimplementation that wires only the Megascale RPC will miss the slice-builder broadcast that informs every worker the slice is now in limited-routing mode.

### The Escalation Cascade

```text
TIER 1  per-chip driver — Ici::HandleIciLinkInterrupt → HandleIciLinkStatusChange
          → Ici::SignalDeferredFailure(Status) → Ici::FailDevice(Status)
          FatalErrorCheck() gates recoverable vs fatal
TIER 2  FailDevice cascade — Ici::FailDevice
          → Driver::FailDevice / FailDeviceLocked
            → TensorNode::FailDevice → Queue::FailDevice → BarnaCore::FailDevice
          aborts all in-flight queues/transfers on the chip
TIER 3  slice-builder — SliceFailureType::CHIP_DRIVER_ERROR (value 5) in
          UpdateSessionInfoRequest; Master::FailSlice (0x1fbc1760);
          Master::ControlIciErrorReport broadcast (limited-routing propagation)
TIER 4  tpunetd — SessionMaster::CheckSessionHeartbeat /
          HandleFailingSession(SessionState); IncrementMissedHealthCheck
          watchdog; GenerateAndSerializeCoreDump → CORE_DUMP_ICI_DUMP
TIER 5  Megascale — ReportError → MegascaleErrorAggregator::ProcessAndShutdown
          → Cause::NETWORKING_ISSUE + FaultyNetworkLink proto
          "Megascale detects a hang that is likely caused by a networking issue."
          (see ../megascale/error-aggregator.md)
```

### Function Map

| Function | Address | Role |
|---|---|---|
| `Master::ControlIciErrorReport` | `0x1fbc0d00` | Master fan-out broadcast (deadline + IsLimitedIciRouting) |
| `Worker::ControlIciErrorReport` | `0x1fc40d80` | Parallel peer re-broadcast on ThreadPool |
| `WorkerService::ControlIciErrorReport` | `0x1fc3cda0` | gRPC server entry |
| `Ici::SignalDeferredFailure(Status)` | `0xe7aeb20` (jfc) / `0xe770ec0` (dfc) | Post deferred-failure closure |
| `Ici::FailDevice(Status)` | `0xe7ae320` (jfc) / `0xe7706a0` (dfc) | Whole-chip teardown entry |
| `AsyncDriver::HandleFatalError(b,b,b,b)` | `0x1fe993c0` | 4-category fatal classifier |
| `Master::FailSlice(SliceFailureType)` | `0x1fbc1760` | Per-slice failure transition |
| `ErrorHandler::InterruptFired` | `0x21382040` | Generic error-IRQ handler |

### Considerations

A `RoutingTableGeneratorFactory::IsLimitedIciRouting` true value flowing through the report tells every worker the slice is running on a degraded/limited route table — this is the runtime correlate of the static resilient-table selection. A core dump can be collected for forensics via `GenerateAndSerializeCoreDump` (`0x1fdb5fa0`) producing the `CORE_DUMP_ICI_DUMP` artifact that the `master.cc` deadlock and `worker.cc` report strings both ask the operator to attach. (LOW: the `SessionState` enum values driving `HandleFailingSession` and the per-state recovery action table were not traced.)

---

## Health-Check and Diagnostic Counters

### Purpose

Expose continuous, host-readable health telemetry that feeds both the soft/hard verdict and the cross-host monitor.

### Algorithm

`GetChipHealth` (`0x1fdb6320`) is the per-chip rollup: it first calls `FatalErrorCheck` and returns the fatal Status immediately if set; otherwise, under a shared lock, it inspects the status fields at `+0x120` (counter, `cmp $0x2`) and `+0x128` (17-bit mask, `cmp $0x20000`), and on a link-down condition builds the diagnostic `"The following ICI link(s) have unexpectedly gone down: "` appended with the per-link index list, returned as `MakeErrorImpl<15>` (INTERNAL). The chain exists in six layers (Synchronous/Async driver, `ici::IciDriver`, and `ici`/`jxc`/base `SliceConfiguration`).

### Streamz Metrics

| Metric | Producer | Meaning |
|---|---|---|
| `ici_link_health` | telemetry harvest | 0–10 per-link scale: 0 healthy, 1–5 transient, 6–9 persistent minor, 10 unusable; `.int`/`.ext` suffix = intra-/inter-host cable |
| `missed_health_check` | `IciSessionMonitorImpl::IncrementMissedHealthCheck` (`0x1ff94a60`) | Cross-host heartbeat watchdog, keyed by `TpuType`; cleared by `ClearMissedHealthCheck` (`0x1ff94c80`) |
| `session_health` | `RecordSessionHealth` | Session state-transition health |
| `broadcast_latency` / `notification_latency` | `RecordBroadcastLatency` / `RecordNotificationLatency` | Cross-host barrier/notify timing |

### Considerations

The `CHECK_SESSION_HEALTH` tpunetd debug RPC (`check_session_health_request/response`) is the cross-host probe whose misses increment `missed_health_check`; past threshold the session is failed via `HandleFailingSession`. Per-gen telemetry harvest (`CollectIciTelemetryCounterSet` on gxc/gfc, vxc/vfc, vxc/vlc) confirms newer generations carry the same per-link MGT stall counter set. (LOW: the exact rate→`ici_link_health` mapping curve was not recovered.)

---

## Per-Generation Differences

Recovery machinery splits by chip family along two axes — the ICI interrupt model and the resilient-route-cache availability.

| Family (driver ns) | ICI interrupt model | Resilient route cache | DMA-timeout error factory |
|---|---|---|---|
| Jellyfish (`jxc::jfc`) | direct MSIX `IciInterrupt` | none observed | — |
| Dragonfish (`jxc::dfc`) | direct MSIX `IciInterrupt` | none observed | — |
| Pufferfish (`pxc/plc`) | `IciInterrupt` (`pxc::plc` factory) | `cache_ici_resiliency_pufferfish_*` | — |
| Viperfish (`vxc/vfc`) | `VfIciFirmwareInterrupt` (fw-mediated) | `cache_ici_resiliency_viperfish_*` | `vxc::vfc::SparsecoreDmaTimeoutErrorFactory` |
| Viperlite (`vxc/vlc`) | `VfIciFirmwareInterrupt` (fw-mediated) | shares viperfish | `vxc::vlc::DmaRoutingMismatchError` |
| Ghostlite (`gxc/gfc`) | `VfIciFirmwareInterrupt` (fw-mediated) | `cache_ici_resiliency_6acc60406_*` | — |
| Ghostlite (`gxc/glc`) | `VfIciFirmwareInterrupt` (fw-mediated) | shares `6acc60406` | `gxc::glc::SparsecoreDmaTimeoutErrorFactory` |

Key deltas:

- **Interrupt delivery.** Older `jxc`/`pxc` deliver the ICI link interrupt as a direct MSIX vector; newer families route it through a firmware-mediated `VfIciFirmwareInterrupt` (the `Vf` = via-firmware), adding a firmware-arbitration step per IRQ but decoupling the host from raw MSIX vector layout.
- **Resilient routing.** Pre-baked fault-route caches exist only for `pufferfish`, `viperfish`, and `6acc60406`. Jellyfish/Dragonfish have no resilient cache in this build → a faulty link cannot be routed around; discovery fails or the slice is reshaped.
- **DMA-timeout class.** The discrete `SparsecoreDmaTimeoutError` has factories only on `gxc/glc` and `vxc/vfc`; `vxc/vlc` additionally has `DmaRoutingMismatchError` (raised when a DMA descriptor's routing field disagrees with the installed table). Older gens surface a stuck DMA only through the sflag-wait watchdog, not a dedicated hardware error type.
- **Chip reset granularity.** `KernelPrivilegedInterface::PerformReset(ResetType)` and `FirmwareInterface::Reset(ResetType)` are present on every family's `KernelFirmware` — the chip-level hard reset used when `LinksDownReset` is insufficient (the fatal path).

---

## Cross-References

- [ICI Overview](overview.md) — the ICI fabric and where failure handling sits beneath the collective/DMA primitives
- [Link Bring-Up Sequence](link-bringup.md) — the bring-up state machine that `LinksDownReset` re-runs to retrain a link
- [Topology Discovery](topology-discovery.md) — discovery produces the faulty-link set the resilient fallback selects against
- [Degraded-Axis Ingest](../collectives/degraded-axis.md) — the proto/POD ingest of the faulty-link orientation into the collective ring (the producer side of the degraded fallback)
- [Resilient Route-Cache Dedup](../routing/route-cache-dedup.md) — the `cache_ici_resiliency_*` resilient route-table codec selected by the degraded fallback
- [Megascale Error Aggregator](../megascale/error-aggregator.md) — the cross-pod escalation target that classifies the digest as `NETWORKING_ISSUE`
