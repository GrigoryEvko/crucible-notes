# Link Bring-Up Sequence

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel. Other versions differ.*
> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`; `.text` VMA == file offset). Symbols below are demangled from the full-symbol binary and cross-checked against the IDA decompile.

## Abstract

ICI link bring-up is the act of taking four cold SerDes ports on every chip in a pod-slice and driving them to a state where the data-link layer is up, routing is installed, and the global time counter is synchronized — the precondition for any collective to move a byte. This page owns the **slice-wide bring-up sequence** that the controller `accel_ssw::deepsea::slice_builder::Master` runs: the 16-step `Master::InitSlice` @`0x1fbbaac0` choreography, the per-chip data-link poll loop `IciControl::WaitForLinksUp` @`0xe7b1060`, the 7-value `LinkStackReadyState` enum that the poll inspects, and where the SerDes ports are actually enabled (`EnableIciDataLink` fan-out → driver `EnableIciPorts`).

The shape will be familiar to anyone who has brought up a multi-node fabric (InfiniBand subnet manager, NVLink/NVSwitch fabric manager): a single global orderer fans work out to per-node agents over RPC, then polls each node to a ready state before advancing. The divergence from those fabrics is the **firmware/host seam**. The analog PHY — SerDes calibration, adaptive equalization, lane lock, 64b/66b alignment — is entirely firmware-owned on the chip's embedded core; the host has no software hook into it. The host only flips `enable_ici_serdes_training`, then observes progress through a single per-port 3-bit `port_ready_state` register, remapped to the software `LinkStackReadyState` enum. Everything from the data-link layer up is host/driver-owned, and that is what `Master` orders and `IciControl` polls.

This page documents three things a reimplementer must reproduce: (1) the **16-step `InitSlice` sequence** — which steps are `ExecuteOnAllWorkers` gRPC fan-outs, which are local-and-locked, which are sequential, and the gates on two of them; (2) the **`WaitForLinksUp` poll loop** — the dual-quantum (1 ms / 500 ms) `AbslInternalSleepFor` cadence, the `IsLinkUp` ∧ `GetLinkStackReadyState` per-link exit condition, and the deadline arithmetic; and (3) the **link-state model** — the firmware-to-software state remap at `0xe7b6400`, the 7-value `LinkStackReadyState` enum, and the per-port `DataLinkLayerState`. Topology discovery (step 2's payload) lives on [Topology Discovery](topology-discovery.md); the fault/reset paths live on [Failure Recovery](failure-recovery.md); the section map is on [overview](overview.md). This page does not duplicate those.

For reimplementation, the contract is:

- The **16-step `InitSlice` ordering** and the dispatch class of each step (fanout / local-locked / sequential / gated). Order is load-bearing: routing is installed before data-link is enabled, data-link-up is waited on before coordinates are pushed.
- The **PHY/host seam**: the host writes `enable_ici_serdes_training` + a `disabled_serdes_index` mask through `EnableIciPorts`, then polls a per-port `port_ready_state`. It never touches the analog PHY.
- The **`WaitForLinksUp` poll**: per-link `IsLinkUp(l) == up ∧ GetLinkStackReadyState(l) == ready`, slept on a 1 ms quantum (clamped up to 500 ms for long deadlines), bounded by `Now() + configure_ici_timeout + wait_for_data_link_up_timeout`.
- The **state model**: firmware `port_ready_state ∈ [0,7]` → `LinkStackReadyState` (identity remap at `0xe7b6400`, ≥8 is an error), and the per-port `DataLinkLayerState` the driver flips on enable and clears on reset.

| | |
|---|---|
| **Slice controller entry** | `Master::InitSlice` @`0x1fbbaac0` (578 decompiled lines; 11 `ExecuteOnAllWorkers` sites) |
| **Step count** | 16 ordered steps; ~11 fanout, the rest local-locked / sequential |
| **DL-up poll loop** | `IciControl::WaitForLinksUp` @`0xe7b1060` (`set<int>, absl::Duration, bool`) |
| **Poll quantum** | 1 ms (`0x3D0900` q-ns); comparand `0x3D0901`; fallback 500 ms (`0x77359400`), comparand `0x77359401` |
| **Per-link exit** | `IsLinkUp` @`0xe7afe80` ∧ `GetLinkStackReadyState` @`0xe7afd00` |
| **Link-state enum** | `LinkStackReadyState` — 7 values, descriptor `0xe7b6540`; FW→SW remap `0xe7b6400` |
| **SerDes enable (driver)** | `jfc::Ici::EnableIciPorts` @`0xe7accc0` / `dfc::Ici::EnableIciPorts` @`0xe76e980` |
| **Enable-once gate** | `SliceConfiguration` offset `0xe8` (`ports_enabled`) — `"ICI ports should only be enabled once."` |

---

## 1. `Master::InitSlice` — the 16-step sequence

### Purpose

`Master::InitSlice` @`0x1fbbaac0` is the single global orderer for slice bring-up. One `Master` exists per pod-slice; it owns the cross-chip ordering and drives every step either as an `ExecuteOnAllWorkers` gRPC fan-out (the same per-worker callable posted to every peer's `SliceBuilderWorkerService` in parallel, then joined), as a local-and-locked computation under `Master::mu_`, or as a sequential per-worker walk. The fan-out/local split matters: the local steps (discovery, routing-table generation, GTC-tree generation) must complete on the controller before the fan-out that installs their result can run.

### Entry Point

```text
Master::InitSlice (0x1fbbaac0)                      ── slice-wide orderer, under Master::mu_
  ├─ ExecuteOnAllWorkers(GetLocalTopology)          ── step 1, fanout  → per-worker link sets
  ├─ DiscoverTopology (0x1fbbe4e0)                  ── step 2, local   → topology-discovery.md
  ├─ ExecuteOnAllWorkers(SetGlobalChipId)           ── step 3, fanout
  ├─ DetectRoutingTableDeadlock (0x1fbbed60)        ── step 5, gated on this+0x90
  ├─ ExecuteOnAllWorkers(SetRoutingTable)           ── step 6, fanout  → ../routing
  ├─ ExecuteOnAllWorkers(SetGtcConfiguration)       ── step 8, fanout
  ├─ ExecuteOnAllWorkers(ControlIciErrorReport)     ── step 9, gated fanout — masks bring-up errors
  ├─ ExecuteOnAllWorkers(EnableIciDataLink)         ── step 10, fanout → PHY + DL kick-off (§3)
  └─ ExecuteOnAllWorkers × 4 (steps 11,14 + GTC)    ── WaitForDataLinkUp / GTC reset / SetChipCoordinates
```

> **NOTE —** the decompile of `InitSlice` contains exactly **11** `ExecuteOnAllWorkers` call sites (verified). The first six bind a named `Master::` method as the callable (`GetLocalTopology`, `SetGlobalChipId`, `SetRoutingTable`, `SetGtcConfiguration`, `ControlIciErrorReport`, `EnableIciDataLink`); the last four (lines 424/458/475/493/512 of the decompile) bind member-function pointers through a vtable-relative offset that this decompile pass does not resolve to a symbol. Their identity (WaitForDataLinkUp, ClearGlobalGtc/WaitForGtcReset, SetChipCoordinates, BroadcastSliceInformation/DisableIciInterrupts) is reconstructed from the surrounding `Worker::` RPC handlers (§4) and is marked HIGH, not CERTAIN.

### Algorithm

```c
function Master_InitSlice(this):                       // 0x1fbbaac0
    lock(this->mu_)                                    // held across phase boundaries
    drain_stale_local_topology()                       // step 0 — LocalTopology dtor on cached entries

    // ---- discovery ----
    ExecuteOnAllWorkers(&Master::GetLocalTopology)      // 1  fanout: each worker returns its links
    if !ok: goto fail
    DiscoverTopology(this)                              // 2  local+locked → ResilientToroidalTopology
    ExecuteOnAllWorkers(&Master::SetGlobalChipId)       // 3  fanout: push Cartesian-ordered chip-id map

    // ---- routing ----
    GenerateRoutingTables()                             // 4  local: RoutingTableGeneratorFactory
    if this->flag_0x90:                                 // 5  gated — only if deadlock-check enabled
        DetectRoutingTableDeadlock(this)                //    walk channel-dependency graph for cycles
    ExecuteOnAllWorkers(&Master::SetRoutingTable)       // 6  fanout: install per-link ICR tables

    // ---- GTC tree ----
    GenerateGtcTree()                                   // 7  local: root/leaf assignment
    ExecuteOnAllWorkers(&Master::SetGtcConfiguration)   // 8  fanout

    // ---- error masking + link enable ----
    if error_report_gate:                               // 9  gated fanout
        ExecuteOnAllWorkers(&Master::ControlIciErrorReport)   // mask bring-up errors
    ExecuteOnAllWorkers(&Master::EnableIciDataLink)     // 10 fanout: PHY + DL training kick-off (§3)

    // ---- per-chip DL-up wait, then GTC resync, then coordinates ----
    ExecuteOnAllWorkers(/* WaitForDataLinkUp */)        // 11 per-chip DL-up poll → IciControl::WaitForLinksUp
    ExecuteOnAllWorkers(/* ClearGlobalGtc */)           // 12
    ExecuteOnAllWorkers(/* WaitForGtcReset */)          // 13
    ExecuteOnAllWorkers(/* SetChipCoordinates */)       // 14 push (X,Y,Z) per chip
    BroadcastSliceInformation(); DisableIciInterrupts() // 15,16 sequential — quiesce bring-up IRQ
    unlock(this->mu_)
    return OK
fail:
    FailSlice(SLICE_FAILURE_INIT_ERROR)                 // → failure-recovery.md
```

The ordering invariants are the part worth internalizing. Routing is installed (step 6) **before** the data-link is enabled (step 10) because the routing tables must be resident when the first DL traffic flows; coordinates are pushed (step 14) **after** DL-up is confirmed (step 11) because the chip-id-to-coordinate map is only valid once discovery has folded every worker's links. The two gated steps — deadlock detection (step 5, gated on `this+0x90`) and `ControlIciErrorReport` (step 9) — are skippable without breaking correctness; deadlock detection is a diagnostic, and error reporting is masked only to keep transient PHY noise off the failure path during training.

### Step Table

`(fanout)` = `ExecuteOnAllWorkers` gRPC broadcast-and-join; `(local)` = on the controller under `Master::mu_`; `(seq)` = per-worker sequential RPC; `(gated)` = conditional.

| # | Step | Dispatch | Implementation | Confidence |
|---|---|---|---|---|
| 0 | Drain stale local topology | local | `LocalTopology` dtor on cached entries | HIGH |
| 1 | Discover local topology | fanout | `Master::GetLocalTopology` → per-worker link set | CERTAIN |
| 2 | Aggregate → global topology | local | `Master::DiscoverTopology` @`0x1fbbe4e0` | CERTAIN |
| 3 | Set global chip IDs | fanout | `Master::SetGlobalChipId` @`0x1fbbe7e0` | CERTAIN |
| 4 | Generate routing tables | local | `RoutingTableGeneratorFactory::Generate` | HIGH |
| 5 | Detect routing-table deadlock | gated local | `Master::DetectRoutingTableDeadlock` @`0x1fbbed60` (if `this+0x90`) | CERTAIN |
| 6 | Install routing tables | fanout | `Master::SetRoutingTable` @`0x1fbbf6e0` | CERTAIN |
| 7 | Generate GTC tree | local | global-time-counter root/leaf | HIGH |
| 8 | Install GTC configuration | fanout | `Master::SetGtcConfiguration` @`0x1fbc0580` | CERTAIN |
| 9 | Control ICI error reporting | gated fanout | `Master::ControlIciErrorReport` @`0x1fbc0d00` | CERTAIN |
| 10 | Enable ICI data link | fanout | `Master::EnableIciDataLink` @`0x1fbc0ee0` (§3) | CERTAIN |
| 11 | Wait for data-link-up | seq | `Master::WaitForDataLinkUp` @`0x1fbc3b20` → `IciControl::WaitForLinksUp` (§2) | HIGH |
| 12 | Clear / reset global GTC | seq | `Master::ClearGlobalGtc` @`0x1fbc3d80` | HIGH |
| 13 | Wait for GTC reset | seq | `Master::WaitForGtcReset` @`0x1fbc3fe0` | HIGH |
| 14 | Set chip coordinates | fanout | `Master::SetChipCoordinates` @`0x1fbc4640` | HIGH |
| 15 | Broadcast slice information | seq | `Master::BroadcastSliceInformation` @`0x1fbc4240` | HIGH |
| 16 | Disable ICI interrupts | seq | `Master::DisableIciInterrupts` @`0x1fbc4a80` | HIGH |

> **GOTCHA —** step 11 (`WaitForDataLinkUp`) is **not** an `ExecuteOnAllWorkers` fan-out in the original design intent — the raw classification marked it sequential, "wait per chip, not fanned out." The decompile shows a fan-out call site in the trailing block, but the per-chip poll inside each worker (`Worker::WaitForDataLinkUp` @`0x1fc417e0` → driver `WaitForDataLinkUp` → `IciControl::WaitForLinksUp`) blocks until that chip's links are up or its deadline expires. Whether the controller waits on all chips concurrently (fanout-join) or serially, the per-chip blocking semantics are identical and the deadline is per-chip-overridable via `WaitForDataLinkUpRequest_ChipDataLinkUpTimeout`. Treat step 11 as "every chip must reach DL-up before any chip advances to step 12."

### Phase-to-RPC mapping

The worker side of each fan-out is a method on `SliceBuilderWorkerService` (RPC prefix `/accel_ssw.deepsea.slice_builder.SliceBuilderWorkerService/`). The recovered worker entry points:

| Step | RPC | Worker entry (VA) | Request → Reply |
|---|---|---|---|
| 3 | SetGlobalChipId | `Worker::SetGlobalChipId` | `SetGlobalChipIdRequest → …Reply` |
| 6 | SetRoutingTable | `Worker::SetRoutingTable` @`0x1fc40140` | `SetRoutingTableRequest → …Reply` |
| 8 | SetGtcConfiguration | `Worker::SetGtcConfiguration` @`0x1fc40760` | `SetGtcConfigurationRequest → …Reply` |
| 10 | EnableIciDataLink | `Worker::EnableIciDataLink` @`0x1fc411c0` | `EnableIciDataLinkRequest → …Reply` |
| 11 | WaitForDataLinkUp | `Worker::WaitForDataLinkUp` @`0x1fc417e0` | `WaitForDataLinkUpRequest → …Reply` |
| 12 | ClearGlobalGtc | `Worker::ClearGlobalGtc` @`0x1fc41c80` | `ClearGlobalGtcRequest → …Reply` |
| 13 | WaitForGtcReset | `Worker::WaitForGtcReset` @`0x1fc42120` | `WaitForGtcResetRequest → …Reply` |
| teardown | LinksDownReset | `Worker::LinksDownReset` @`0x1fc430a0` | `LinksDownResetRequest → …Reply` ([failure-recovery](failure-recovery.md)) |

> **NOTE —** the Cloud deployment inserts a `tpunetd` daemon between `Master` and the driver (`SuperpodController → tpunetd → driver`). The protobuf message shapes for the ICI subset are identical; the Cloud envelope is `superpod.tpunetd.ConfigureIciRequest` with three oneof arms (`EnableIciDataLinkRequest`, `WaitForDataLinkUpRequest`, `ResetIciNetworkRequest`). Only the transport differs, so the 16-step ordering above holds on both paths.

---

## 2. `IciControl::WaitForLinksUp` — the data-link poll loop

### Purpose

`IciControl::WaitForLinksUp` @`0xe7b1060` is the chip-local poll that step 11 ultimately blocks on. Given a `std::set<int>` of link indices, an `absl::Duration` deadline budget, and a boolean (include-loopback / verbose), it spins until every requested link reports both hardware-up and a ready firmware state, or the deadline expires — at which point it returns `DEADLINE_EXCEEDED` carrying the per-link state of the offenders. This is the host's only synchronization point against firmware-owned PHY training: the analog bring-up is a black box, and this loop is how the host learns it finished.

### Entry Point

```text
Master::WaitForDataLinkUp (0x1fbc3b20)              ── slice step 11, reads timeout offsets
  └─ Worker::WaitForDataLinkUp (0x1fc417e0)         ── per-worker RPC handler
       └─ ici::SliceConfiguration::WaitForDataLinkUp (0x1fdb46e0)
            └─ IciControl::WaitForLinksReadyAndUp (0xe7b0780)   ── umbrella: refresh + waitUp
                 ├─ IciControl::WaitForLinkStateRefresh (0xe7b0ec0)  ── one-shot, no loop
                 └─ IciControl::WaitForLinksUp (0xe7b1060)           ── the poll loop (this section)
                      ├─ IciControl::IsLinkUp (0xe7afe80)             ── HW link-up bit per port
                      ├─ IciControl::GetLinkStackReadyState (0xe7afd00) ── FW state per port (§3)
                      └─ AbslInternalSleepFor(quantum)
```

### Algorithm

```c
function WaitForLinksUp(this, links, budget, include_loopback):   // 0xe7b1060
    deadline = absl::Now() + budget                  // budget = configure_ici_timeout
                                                      //        + wait_for_data_link_up_timeout (§ Deadlines)
    loop:
        all_up = true
        for link in links:                            // set<int>, ascending
            up    = IsLinkUp(link)                     // 0xe7afe80 — HW link-up bit
            state = GetLinkStackReadyState(link)       // 0xe7afd00 — FW state → LinkStackReadyState (§3)
            if not (up == 1 and state == kReady):
                all_up = false
                if link is unrecognized: log "Unrecognized data link layer state: <v>"
        if all_up:
            return OK
        now = absl::Now()
        if now >= deadline:                            // deadline reached
            return DEADLINE_EXCEEDED(per-link state)   // names rendered via NameOfDenseEnum<...,0,7>
        remaining = deadline - now
        // quantum selection (verified constants):
        //   if remaining >= 0x3D0901 q-ns (≈1 ms)  → base poll quantum is 1 ms
        //   if remaining >= 0x77359401 q-ns (≈500 ms) → clamp the sleep up to the 500 ms cadence
        quantum = clamp(remaining, 1ms, 500ms)
        AbslInternalSleepFor(quantum)                  // yields; firmware advances PHY in the gap
```

The two comparands recovered from the disassembly (`v184 >= 0x3D0901` at decompile line 299) are the off-by-one upper guards on the encoded quarter-nanosecond durations: `0x3D0900` = 4,000,000 q-ns = **1 ms** (the base quantum) and `0x77359400` = 2,000,000,000 q-ns = **500 ms** (the long-deadline cadence). The loop sleeps 1 ms when the remaining budget is short and stretches toward a 500 ms cadence when a long deadline makes tight polling wasteful — the classic "poll fast near the wire, poll slow when you have minutes to wait" trade-off. `absl::InfiniteFuture` (int64 max, low word `0xFFFFFFFF`) collapses the deadline to `gpr_inf_future` and the loop blocks indefinitely.

> **QUIRK —** the per-link failure message is rendered by `proto2::internal::NameOfDenseEnum<&LinkStackReadyState_descriptor, 0, 7>` (verified at decompile lines 947–950) — the second template argument `7` is the enum arity, confirming the **7-value** `LinkStackReadyState` enum. The names are pulled from the proto descriptor at runtime, not from `.rodata` string literals, so a static dump of the binary will not show the seven enum-value strings even though the arity is provable.

### Deadlines and retries

`Master::WaitForDataLinkUp` @`0x1fbc3b20` reads two `absl::Duration` fields off `Master` and sums them to form the budget passed down:

| Master offset | Field | Feeds |
|---|---|---|
| `0x14:0x1c` | `configure_ici_timeout` | step 10 (`EnableIciDataLink`) PHY-training budget |
| `0x30:0x38` | `wait_for_data_link_up_timeout` | step 11 (`WaitForDataLinkUp`) DL-up budget |

The gRPC deadline on each call is `absl::Now() + configure_ici_timeout + wait_for_data_link_up_timeout`, converted to `timespec` via `absl::ToTimespec` and stored on the `ClientContext`. If either is `absl::InfiniteFuture`, the deadline collapses to `gpr_inf_future`. The compiled-in defaults are populated from `slice_builder::Options` (constructed via `MasterFactory::Create` @`0x1fbb6a20`) and are **not recovered** (LOW) — they are user/env-overridable. The user-facing knobs are `wait_for_data_link_up_timeout` (per-call deadline override; a per-chip override exists as `WaitForDataLinkUpRequest_ChipDataLinkUpTimeout` for heterogeneous pods) and `max_ici_retries_per_minute` (a per-link retry budget enforced by `IciControl::UpdateAndGetRetriesPerMinute` @`0xe7af540` over a `RetryHistory` ring buffer).

### Sibling state-check functions

`WaitForLinksUp` is one of a family of state inspectors on `IciControl`; a reimplementer should know which one to call:

| Function | VA | Behavior |
|---|---|---|
| `IciControl::AllLinksUp(span<int>, bool)` | `0xe7b0200` | Snapshot check, **no wait** |
| `IciControl::IsLinkUp(int)` | `0xe7afe80` | Single-port HW link-up bit |
| `IciControl::GetLinkStackReadyState(int)` | `0xe7afd00` | Single-port FW state (§3) |
| `IciControl::GetValidLinks(bool)` | `0xe7b0980` | Enumerate enabled non-loopback ports |
| `IciControl::WaitForLinkStateRefresh(Duration)` | `0xe7b0ec0` | One-shot refresh poll, **no loop** |
| `IciControl::WaitForLinksReadyAndUp(Duration, bool)` | `0xe7b0780` | Umbrella: refresh then `WaitForLinksUp` |
| `IciControl::WaitForLinksUp(set<int>, Duration, bool)` | `0xe7b1060` | The poll loop (this section) |

> **GOTCHA —** `GetValidLinks(bool include_loopback)` excludes any port firmware left in loopback mode (`"<port> is incorrectly left in loopback mode. Ignoring this link for ICI links discovery."`). A reimplementer who passes the raw 0..3 port set to `WaitForLinksUp` instead of the `GetValidLinks` result will hang waiting on a loopback port that never reaches a peer.

---

## 3. SerDes port enablement and the firmware/host seam

### Purpose

Step 10 (`EnableIciDataLink`) is where the SerDes ports are actually turned on. The slice-side `Master::EnableIciDataLink` @`0x1fbc0ee0` builds a per-link configuration and fans it out; each worker hands it to the chip-local driver, which writes the firmware-facing enable through `EnableIciPorts`. This is the precise seam between host-owned data-link control and firmware-owned PHY training.

### The PHY is firmware-owned

The host has **no software hook into the analog PHY**. SerDes calibration, adaptive equalization, eye-opening, baud/lane lock, and 64b/66b alignment all run on the chip's embedded DeepSea CM/MGT firmware. The host's entire contribution to PHY bring-up is three flags written through `EnableIciPorts`:

- `enable_ici_serdes_training` — gates PHY-level training (the kick-off).
- `ignore_external_ici_ports` — omits unconnected (tray-external) ports.
- `disabled_serdes_index` — a per-link disable mask.

After writing these, the host's only window into PHY progress is the per-port 3-bit `cm_scratch_user_firmware::link_stack_ready_state::port_ready_state` register and the `IciSerdesInterrupt` IRQ. This is why the host-side timeline (the §2 poll) treats PHY training as a black-box latency budgeted by `configure_ici_timeout`.

### Algorithm

```c
function Master_EnableIciDataLink(this, target, stub):     // 0x1fbc0ee0
    req = EnableIciDataLinkRequest()
    for each owned chip/link:
        cfg = req.add_ici_data_link_configuration()        // RepeatedPtrFieldBase::Add<...
                                                            //   IciDataLinkConfiguration> @0x1fbc6ba0
        cfg.set_*(...)                                      // per-link: serdes-training, disable mask, ...
    stub->EnableIciDataLink(req)  → worker → driver

function Driver_EnableIciPorts(span<int> links):           // jfc 0xe7accc0 / dfc 0xe76e980
    // host-side: write enable_ici_serdes_training + disabled_serdes_index to firmware mailbox
    // firmware then runs the analog PHY bring-up asynchronously
    for link in links:
        IciPortUser::SetDataLinkLayerState(link, /*on=*/true)   // 0x1fe8a2e0
    // host now leaves; progress observed only via port_ready_state (§ State model)
```

The per-link config is a repeated `EnableIciDataLinkRequest_IciDataLinkConfiguration` sub-message (the `RepeatedPtrFieldBase::Add<…IciDataLinkConfiguration>` site is verified at decompile line 133 of `EnableIciDataLink`). Its field-level layout is recovered only at runtime via the protobuf descriptor and is **not enumerated** here (LOW).

### The `EnableIci` one-shot gate

The chip-local `SliceConfiguration::EnableIci` @`0x1fdb1dc0` is a one-shot. It sets `ports_enabled` (offset `0xe8`) and refuses a second call:

| Guard | Trigger | Message |
|---|---|---|
| Enable-once | `EnableIci` called while `0xe8 == 1` | `"ICI ports should only be enabled once."` |
| Enable-before-wait | `WaitForDataLinkUp` while `0xe8 == 0` | `"EnableIci() must be called before WaitForDataLinkUp()"` |

`LinksDownReset` ([failure-recovery](failure-recovery.md)) clears `0xe8` so the slice can be re-enabled after a reset.

### Driver-side bring-up state

The chip-local `SliceConfiguration` holds the per-port DL state and enable bookkeeping. The fields a reimplementer must replicate (offsets in the modern `asic_sw::driver::deepsea::ici::SliceConfiguration`):

| Offset | Type | Field | Set by | Cleared by |
|---|---|---|---|---|
| `0xe0` | bool | `links_open` (`Ici::Open` returned OK) | ctor end | `LinksDownReset` |
| `0xe8` | bool | `ports_enabled` (`EnableIci` called once) | `EnableIci` | `LinksDownReset` |
| `0xf0` | int32* | per-port `DataLinkLayerState` array | `EnableIci` | overwritten by `CollectDataLinkState` |
| `0xf8` | uint64 | DL-state array length | ctor | (immutable) |
| `0x108` | int* | enabled-port indices | `EnableIci` | `LinksDownReset` |
| `0x110` | uint64 | enabled-port count | `EnableIci` | `LinksDownReset` (→ 0) |

---

## 4. The link-state model

### `LinkStackReadyState` — the 7-value enum and the FW remap

Firmware reports a per-port `port_ready_state ∈ [0,7]` (a 3-bit field). The host translates it into the software `LinkStackReadyState` enum through `IciLinkInfo::FirmwareStateToLinkStackReadyState` @`0xe7b6400`, returning a `StatusOr<LinkStackReadyState>`.

> **CORRECTION (BRINGUP-01) —** the firmware-to-software mapping at `0xe7b6400` was originally reconstructed as a *permutation* (1→4, 2→2, 3→3, 4→7, 5→1, 6→5, 7→6) implemented by an 8-entry jump table. Decompilation of the function body overturns this: it is a plain 8-arm `switch` that writes an **identity** map — `case k:` stores `k` into the `StatusOr` value slot (offset +8) and sets the OK status (`*(_QWORD*)a1 = 1`) for every `k ∈ [0,7]`. The `default` arm (`port_ready_state ≥ 8`, structurally unreachable for a 3-bit field) builds an error: `"Unknown ready_state %d"` via `MakeErrorImpl<3>` at `platforms/asic_sw/lib/deepsea/jxc/common/ici_link_info.cc:54`. There is no reordering. A reimplementer should map firmware code → enum value 1:1 and reject codes ≥8, not apply a permutation.

The enum descriptor is `LinkStackReadyState_descriptor` @`0xe7b6540`; its arity (7) is confirmed by the `NameOfDenseEnum<&…, 0, 7>` calls in `WaitForLinksUp` (§2). The seven value-name strings are emitted at runtime from the proto descriptor and are **not present as `.rodata` literals** (LOW — names require the `link_stack.proto` FileDescriptorProto). The numeric model:

| FW `port_ready_state` | `LinkStackReadyState` value | Source | Confidence |
|---|---|---|---|
| 0 | 0 | `0xe7b6400` case 0 | CERTAIN |
| 1 | 1 | case 1 | CERTAIN |
| 2 | 2 | case 2 | CERTAIN |
| 3 | 3 | case 3 | CERTAIN |
| 4 | 4 | case 4 | CERTAIN |
| 5 | 5 | case 5 | CERTAIN |
| 6 | 6 | case 6 | CERTAIN |
| 7 | 7 → error if proto arity is 7 | case 7; descriptor arity 7 | HIGH |
| ≥8 | `"Unknown ready_state %d"` error | default arm | CERTAIN |

> **QUIRK —** the firmware register is 8-valued (0..7) but the proto enum is described as 7-valued by `NameOfDenseEnum<…,0,7>`. The identity remap passes all eight firmware codes through unchanged; if the descriptor truly carries only seven dense names (indices 0..6), firmware code 7 would have no name and `NameOfDenseEnum` falls to its `NameOfDenseEnumSlow` path (verified present at decompile line 950) rather than indexing the cached name table. The discrepancy is benign for the poll — `WaitForLinksUp` compares against the numeric ready value, not the name — but a reimplementer rendering diagnostics must handle the un-named eighth code.

### Per-port `DataLinkLayerState`

Distinct from the firmware ready-state, the host maintains a per-port `DataLinkLayerState` that it sets on enable and reads back to confirm DL-up. It is owned by `IciPortUser`:

| Symbol | VA | Purpose |
|---|---|---|
| `IciPortUser::SetDataLinkLayerState(bool)` | `0x1fe8a2e0` | Host turns DL on/off for a port |
| `IciPortUser::GetDataLinkLayerState() const` | `0x1fe8a3c0` | Reads the per-port DL state |

Both raise `"… Must call Initialize() first"` if invoked before driver init. `CollectDataLinkState` ([failure-recovery](failure-recovery.md)) re-reads every port into the `0xf0` array; the `kDown` value (slot 4 in the enum) is the reset target. Diagnostics seen on this path: `"Bringing DL up on ICI link <n>"`, `"Failed to get data link layer state on link <n>"`, `"Unrecognized data link layer state: <v>"`, `"Failed to bring up data link on chip <loc>"`.

---

## 5. Considerations

- **Order is correctness, not optimization.** A reimplementation that enables the data link (step 10) before installing routing (step 6) will move flits with no routing table resident; one that pushes coordinates (step 14) before DL-up (step 11) will publish a coordinate map built from incomplete discovery. The four ordering edges — discovery → chip-ids, routing → DL-enable, DL-up → coordinates, GTC-config → GTC-reset — are the load points.
- **The poll budget is a sum, not a single timeout.** `WaitForLinksUp` is handed `configure_ici_timeout + wait_for_data_link_up_timeout`, so a heterogeneous pod with one slow chip needs the per-chip `ChipDataLinkUpTimeout` override; bumping only the global `wait_for_data_link_up_timeout` extends every chip's deadline uniformly.
- **PHY is opaque.** There is no host-visible knob for SerDes equalization taps, baud, or lane width — those are firmware-internal. The host's leverage is exactly three flags (`enable_ici_serdes_training`, `ignore_external_ici_ports`, `disabled_serdes_index`) plus the poll deadline. A reimplementer must own the firmware side separately to reproduce PHY training; this page covers only the host/driver half.
- **Single-chip slices skip the GTC tree.** When a slice owns one chip, steps 7–8/12–13 degenerate: `SliceConfiguration::EnableSingleChipGtc` @`0x1fdb38e0` / `IciControl::SetupSingleGtc` @`0xe7b49c0` install a self-leader GTC instead of a peer tree, and `"Failed to find any enabled ICI port as the single-chip GTC leader"` fires if no enabled port exists.

---

## Verification notes

> **[CONFIRMED]** Cross-checked against the IDA decompile of `libtpu.so` v0.0.40:
> - `Master::InitSlice` @`0x1fbbaac0` (578 lines): exactly **11** `ExecuteOnAllWorkers` sites; named callables in order `GetLocalTopology, SetGlobalChipId, SetRoutingTable, SetGtcConfiguration, ControlIciErrorReport, EnableIciDataLink`; `DiscoverTopology` (local) between sites 1 and 2; `DetectRoutingTableDeadlock` gated before `SetRoutingTable`. The 16-step sequence is consistent.
> - `IciControl::WaitForLinksUp` @`0xe7b1060`: `absl::Now()` deadline base, comparand `0x3D0901` (line 299), per-link `IsLinkUp` (line 316) ∧ `GetLinkStackReadyState` (line 325), `AbslInternalSleepFor` (line 896), and `NameOfDenseEnum<&LinkStackReadyState_descriptor, 0, 7>` (lines 947–950, with `NameOfDenseEnumSlow` fallback) — exact; the **7-value** arity is proven by the template argument.
> - `EnableIciDataLink` @`0x1fbc0ee0`: builds a repeated `EnableIciDataLinkRequest_IciDataLinkConfiguration` (`RepeatedPtrFieldBase::Add<…>` at line 133) — confirms the per-link config fan-out.
> - `FirmwareStateToLinkStackReadyState` @`0xe7b6400`: an 8-arm identity `switch` returning `StatusOr<LinkStackReadyState>`; `default` → `"Unknown ready_state %d"` at `ici_link_info.cc:54` — overturns the earlier permutation claim (CORRECTION BRINGUP-01).
>
> **[LOW]** Compiled-in defaults of `configure_ici_timeout` / `wait_for_data_link_up_timeout` (from `slice_builder::Options`); the seven `LinkStackReadyState` value-name strings (rendered at runtime via the proto descriptor, not in `.rodata`); the field layout of `EnableIciDataLinkRequest_IciDataLinkConfiguration`. The identity of the four trailing `ExecuteOnAllWorkers` callables (steps 11–16) is reconstructed from the `Worker::` RPC handlers (HIGH), since the decompile binds them through unresolved vtable-relative member pointers.

---

## Related Components

| Component | Relationship |
|---|---|
| [Topology Discovery](topology-discovery.md) | Step 2's payload — folds the step-1 `LocalTopology` sets into the global toroidal topology |
| [Failure Recovery](failure-recovery.md) | The fail path out of every step (`FailSlice`, `LinksDownReset`) and the `0xe8`/`0x110` reset |
| [Routing](../routing/overview.md) | Owns steps 4 (generate) and 6 (install) — the per-link ICR tables resident before DL-enable |
| [DMA Descriptor](dma-descriptor.md) | The transfer unit that rides the links this page brings up |

## Cross-References

- [ICI Overview](overview.md) — the section map: two-level control plane, the bring-up → discovery → transfer spine, link/resource model
- [Topology Discovery](topology-discovery.md) — square-seed polarity, BFS coordinates, `LocalTopology` wire format
- [Failure Recovery](failure-recovery.md) — `SliceFailureType`, `FailDevice` cascade, `LinksDownReset`, error masking during bring-up
- [DMA Descriptor](dma-descriptor.md) — per-family descriptor word layout, remote sync-flag encoding
- [Routing](../routing/overview.md) — `(src,dst) → link path`, route-table generation and install
- [Megascale](../megascale/overview.md) — cross-slice topology stitching that consumes the per-slice bring-up result
- [back to index](../index.md)
