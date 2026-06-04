# v7x Perf-Counters

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The `.text`, `.rodata`, and `.lrodata` sections map VMA == file offset; `kDeviceTypeInfo` lives at VMA `0x1c60480`. Other builds will differ.*

## Abstract

The TPU profiler exposes two distinct on-device telemetry layers that share one backing structure: the `kDeviceTypeInfo` array (17 entries of stride `0x448` = 1096 bytes, one per `DeviceType`). The first layer is a **named hardware performance-counter resolver** — `GetPerformanceCounterNames<N>` (`0xf240980` / `0xf240c00` / `0xf240ac0`) — that turns user-selected counter ordinals into the on-die register names the profiler stamps onto its counter timelines. The second is a **firmware power/thermal/DVFS event model** — `ConvertFirmwareTraceEntriesToXPlane` feeding `FirmwareEventBuilder` — that converts a firmware trace into power, temperature, throttle, and P-state XStats. Both read their per-generation constants from the same `kDeviceTypeInfo` row.

The single most important fact about the perf-counter layer is that it is **inert on every silicon generation except one**. All three resolver instantiations open with `if (DeviceType == 12)` and return immediately otherwise. `DeviceType` 12 is **v7x** — a distinct silicon generation from v6e (`DeviceType` 13), confirmed by both the enum name strings (`v7x`, `v6e`, `Viperfish` all present in `.rodata`) and by the descriptor bytes: only the `0x1c637e0` row (index 12) carries nonzero perf-counter enum bases; every other row is all-zero at the six descriptor fields. So "v7x" is not a counter-naming axis layered over older silicon — it is the only generation in this build that exposes *named* on-device counters at all. The counter-name dispatcher itself lives in the `asic_sw::driver::deepsea::gxc::gfc::profiler` namespace; `gfc` is one of the `gxc`-namespace profiler codec families (sibling to `glc`), and v7x resolves its counter names through it (this page establishes that relationship in [§ Codec Family](#codec-family-gfc-vs-the-v7x-gate)).

The six perf-counter sets map 1:1 onto the six `STATS_COUNTER_SAMPLE_ISSUED_FROM_{TCS,SCS,SCTC,SCTD,CMNUR,ICR}` tracepoint sources — TensorCore Sequencer, SparseCore Scalar, SparseCore Tile Compute, SparseCore Tile DMA, the memory-network/HBM controller, and the ICI router. The tracepoints carry the sampled counter *values*; this resolver supplies their *names*. The page covers: the resolver and its `base + index*8` enum encoding; the six `kDeviceTypeInfo` descriptor fields and the recovered counter-name strings; the request-driven counter-index selection (`GetTpuCounterIndicesFromRequest`); and the firmware `FirmwareEventBuilder` event model with its per-generation calibration bundle.

For reimplementation, the contract is:

- The `DeviceType == 12` gate and the `PerformanceCounterName = base + ordinal*8` enum-encoding that every resolver uses.
- The six `kDeviceTypeInfo` descriptor fields (`+0x2c8/+0x348/+0x350/+0x358/+0x438/+0x440`) as packed enum **bases** (not bit-masks), one per counter set, with their resolver inline-caps (28/28/28/28/3/12).
- The request→set wiring in `GetTpuCounterIndicesFromRequest`: which `XprofRequest` flag bit selects which `TpuCounterIndices` member, fed to which resolver.
- The firmware event model: the five `TraceEntry` oneof variants, the `RunLengthTracker` aggregation, the power/temperature/throttle/P-state formulas, and the per-generation `+0x360..+0x398` calibration bundle.

| | |
|---|---|
| **Resolver `<28>`** | `xprof::tpu::GetPerformanceCounterNames<28>` @ `0xf240980` |
| **Resolver `<12>`** | `...<12>` @ `0xf240c00` (ICR set) |
| **Resolver `<3>`** | `...<3>` @ `0xf240ac0` (CMNUR/HBM set) |
| **Name dispatcher** | `asic_sw::driver::deepsea::gxc::gfc::profiler::PerformanceCounterNameToString` @ `0x1fc701e0` (20 band sub-decoders `0x1fc703e0`..) |
| **Request decoder** | `xprof::tpu::GetTpuCounterIndicesFromRequest` @ `0xf2c5000` |
| **Caller** | `ConvertTpuTraceToXPlaneV2<...jxc::PerformanceTraceEntry>` (six call sites) |
| **Firmware convert** | `ConvertFirmwareTraceEntriesToXPlane<...gfc::TraceEntry>` @ `0xf2b1ee0` (vlc `0xf27f140`, glc `0xf29d5c0`, vfc `0xf28ae00`) |
| **Firmware builder ctor** | `FirmwareEventBuilder<...gfc::TraceEntry>::FirmwareEventBuilder` @ `0xf2b8780` (vlc `0xf284d20`) |
| **`kDeviceTypeInfo`** | `0x1c60480`, 17 × `0x448`; v7x row (DT12) @ `0x1c637e0` |
| **Active generation** | `DeviceType == 12` = **v7x** only; all other rows zero |

---

## The v7x Gate and the Counter-Name Resolver

### Purpose

`GetPerformanceCounterNames<N>` is the function that names on-device HW performance counters. Given a list of user-selected counter ordinals and a per-set enum base, it produces one `string_view` name per ordinal. The names become the column labels on the v7x counter timelines. There are three instantiations differing only by the `InlinedVector` inline capacity `N`, which is the maximum number of counters that set can carry: `<28>` for the four TensorCore/SparseCore sets, `<12>` for the ICI router set, `<3>` for the HBM/memory-network set.

### Entry Point

```text
ConvertTpuTraceToXPlaneV2<jxc::PerformanceTraceEntry>      ── six call sites, one per counter set
  └─ GetPerformanceCounterNames<28|12|3> (0xf240980 / 0xf240c00 / 0xf240ac0)
       └─ gxc::gfc::profiler::PerformanceCounterNameToString (0x1fc701e0)
            └─ ToString0..19 (0x1fc703e0 ..)   ── 20 banded binary-search sub-decoders
```

### Algorithm

All three resolvers share one body. The `<28>` form is reproduced; `<12>` and `<3>` differ only in the inline-capacity constant.

```c
function GetPerformanceCounterNames_28(DeviceType d,        // 0xf240980
                                       const int* idxs, size_t n,
                                       uint64_t base,        // the kDeviceTypeInfo field
                                       InlinedVector<string_view,28>* out):
    if d != 12:                       // 0xf240980 — gate: ONLY v7x; every other gen returns empty
        return
    reserve(out, n)                   // grow inline storage if n > current cap
    for i in 0 .. n-1:                // loop @ 0xf240a20
        ordinal = idxs[i]             // movsxd from the request-selected index vector
        name = PerformanceCounterNameToString(base + 8 * ordinal)   // 0xf240a28
        out.push_back(name)           // EmplaceBackSlow when storage is full
```

The arithmetic `base + 8*ordinal` is the whole encoding (`lea rdi,[r14 + rax*8]` at the call site `0xf240a24`, with `r14` holding the per-set base). `PerformanceCounterName` is a packed 32-bit enum; the per-set base selects the counter *family* (chiplet / unit / domain) and the ordinal indexes the counter within it, scaled by 8.

> **QUIRK —** the `<N>` template parameter is the `InlinedVector` inline capacity, i.e. the per-set maximum counter count — not a counter ID and not a device generation. `<28>` carries up to 28 names, `<12>` up to 12, `<3>` up to 3. A reimplementation that treats `N` as anything else will mis-size the four SparseCore/TensorCore sets.

> **GOTCHA —** the `DeviceType == 12` gate is the entire generation guard. There is no fallback path: on v6e, v5p, v5e, v4, v3, v2 the resolver returns with an empty vector and the counter timelines carry no names. A reimplementation that omits the gate, or that drives the resolver on a non-v7x `kDeviceTypeInfo` row (whose bases are zero), will dereference `PerformanceCounterNameToString(0 + 8*ordinal)` and produce garbage / empty strings rather than the intended no-op.

### The Six Descriptor Fields

The six `kDeviceTypeInfo` trailing fields are packed 32-bit `PerformanceCounterName` enum **bases**, one per counter set. They are stored in the low 32 bits with the high 32 zero, and on v7x (DT12, row `0x1c637e0`) they read byte-exact:

| kDTI field | Resolver | Inline cap | Subsystem (`STATS_COUNTER` source) | v7x base | Confidence |
|---|---|---|---|---|---|
| `+0x2c8` | `<28>` | 28 | TCS — TensorCore Sequencer | `0xa668a008` | High |
| `+0x348` | `<28>` | 28 | SCS — SparseCore Scalar | `0xa7f61008` | High |
| `+0x350` | `<28>` | 28 | SCTC — SparseCore Tile Compute | `0xa7724008` | High |
| `+0x358` | `<28>` | 28 | SCTD — SparseCore Tile DMA / TEC | `0xa6726008` | High |
| `+0x440` | `<3>` | 3 | CMNUR — memory-network / HBM controller | `0xa5463408` | High |
| `+0x438` | `<12>` | 12 | ICR — ICI router (data) | `0xd6438c08` | High |

> **NOTE —** these six fields are not bit-masks. Each is a packed `PerformanceCounterName` enum **base** that is *added* to `ordinal*8`. The `base + ordinal*8` arithmetic in the resolver body (`lea rdi,[r14+rax*8]`) is direct evidence the field is an additive base, not a mask. There are six counter sets, including `+0x440` → `<3>` CMNUR and `+0x438` → `<12>` ICR.

Every other `DeviceType` row is `0x00000000` at all six fields — verified by sweeping all 17 rows: only index 12 (`0x1c637e0`) is nonzero. The whole named-counter machinery is therefore v7x-exclusive in this build.

### Recovered Counter Names

The actual counter strings live in the `gfc` name tables (`SamplingData::kNames` / `kOffsets`) reached through the 20 band sub-decoders. Resolving `base + ordinal*8` for the first few ordinals of each set recovers names byte-exact (all confirmed present in `.rodata`); the full register prefix is the `VF_` (Viperfish-register) namespace that v7x reuses:

| Set | base | ordinal 0..3 (suffix after `..._UNPRIVILEGED_`) | Confidence |
|---|---|---|---|
| SCS (`+0x348`) | `0xa7f61008` | `COUNT_CYCLES` / `COUNT_SCALAR_ISSUE` / `COUNT_BRANCH_TAKEN` / `COUNT_S0_INSTRUCTION` | High |
| SCTC (`+0x350`) | `0xa7724008` | `COUNT_CYCLES` / `COUNT_VECTOR_ISSUE` / `COUNT_V0_INSTRUCTION` / `COUNT_V1_INSTRUCTION` | High |
| SCTD (`+0x358`) | `0xa6726008` | `COUNT_CYCLES` / `TEC_SCALAR_ISSUE` / `TEC_BRANCH_TAKEN` / `TEC_S0_INSTRUCTION` | Medium |
| CMNUR (`+0x440`) | `0xa5463408` | `CYCLE_COUNTER_WINDOW` / `RD_RSP_BEAT_FROM_HBM` / `WR_REQ_BEAT_TO_HBM` | High |
| ICR (`+0x438`) | `0xd6438c08` | `LINK0_EGRESS_CONTROL_PACKET_SENT` / `LINK0_EGRESS_DATA_PACKET_SENT` / `LINK0_INGRESS_CONTROL_PACKET_RECEIVED` / `LINK0_INGRESS_DATA_PACKET_RECEIVED` | High |
| TCS (`+0x2c8`) | `0xa668a008` | *(not named — see GOTCHA)* | Low |

Sample full names (verified verbatim in the binary):

```text
VF_CHIP_DIE0_SC_0_SCS_SC_STATS_COUNTERS_UNPRIVILEGED_COUNT_CYCLES
VF_CHIP_DIE0_CMN_CMNUR_0_CMN_STATS_DEBUG_FIXED_STATS_COUNTERS_UNPRIVILEGED_RD_RSP_BEAT_FROM_HBM
VF_CHIP_CHIPLET_ICR_ICR_DATA_0_DEBUG_DOMAIN_ICR_DATA_STATS_PACKET_COUNTERS_UNPRIVILEGED_LINK0_EGRESS_DATA_PACKET_SENT
```

> **GOTCHA —** the TCS (TensorCore) set at `+0x2c8` resolves through `PerformanceCounterNameToString` into an enum band the `gfc` dispatcher does not map — ordinals 0..N return empty there. The set *is* the TCS source (1:1 with `STATS_COUNTER_SAMPLE_ISSUED_FROM_TCS` and the `0x10000000` request flag), and it uses the `<28>` resolver, but the TensorCore counter strings live in a separate TC-specific name table not reached by the `gfc` band chain decoded here. A reimplementer must locate that TC table to name the v7x TensorCore counters; the other five sets are complete.

### Codec Family (`gfc`) vs the v7x Gate

The name dispatcher is `asic_sw::driver::deepsea::gxc::gfc::profiler::PerformanceCounterNameToString`. `gfc` is a profiler codec family in the `gxc` namespace (sibling to `glc`, both under `gxc`; `vlc`/`vfc` live under `vxc`). The resolver gates on `DeviceType == 12` = v7x but resolves names through the `gfc` tables. This is consistent, not contradictory: v7x is a newer silicon generation than v6e (`DeviceType` 13), but it shares the `gfc` profiler codec and the Viperfish (`VF_`) register namespace. The firmware path makes the same choice — `FirmwareEventBuilder` and `ConvertFirmwareTraceEntriesToXPlane` are instantiated on the `gfc::profiler::TraceEntry` codec (alongside `vlc`, `glc`, `vfc` siblings). So the v7x/gfc relationship is: **v7x is a distinct `DeviceType` that the `gfc` codec serves**, with the enum bases and calibration constants varying per row of `kDeviceTypeInfo`.

---

## Request-Driven Counter Selection

### Purpose

The counter-index vectors handed to the resolvers are not baked into the binary — they are user-selected through the profiling request. `GetTpuCounterIndicesFromRequest` (`0xf2c5000`) copies the `XprofRequest.PeriodicCounterSamplingOptions` repeated-int proto fields into a `TpuCounterIndices` struct of six `InlinedVector<int,N>` members, gated by five flag bits plus one byte flag.

### Algorithm

```c
function GetTpuCounterIndicesFromRequest(XprofRequest* req,        // 0xf2c5000
                                         TpuCounterIndices* out):
    flags = req->u32[+0x10]
    if flags & 0x10000000:  Assign<28>(out + 0x000, req->sampling_opts[+0xb8])   // TCS
    if flags & 0x20000000:  Assign<28>(out + 0x078, req->sampling_opts[+0xc0])   // SCS
    if flags & 0x40000000:  Assign<28>(out + 0x0f0, req->sampling_opts[+0xc8])   // SCTC
    if flags & 0x00000020:  Assign<28>(out + 0x168, req->sampling_opts[+0x48])   // SCTD
    if (int)flags < 0:      Assign<3> (out + 0x1e0, req->sampling_opts[+0xd0])    // CMNUR (sign bit = 0x80000000)
    if req->u8[+0x14] & 1:  Assign<12>(out + 0x1f8, req->sampling_opts[+0xd8])    // ICR
```

`Assign<N>` is `absl::inlined_vector_internal::Storage<int,N>::Assign<...>` — it copies the repeated-int proto field (a `RepeatedIterator<int const>` range) into the member's inline storage. The `<N>` per member matches the resolver caps exactly: 28/28/28/28 for the four SparseCore/TensorCore members, 3 for CMNUR, 12 for ICR.

### Request → Set Wiring

| Flag bit | `TpuCounterIndices` member | `Assign<N>` | Resolver fed | kDTI base | Set | Confidence |
|---|---|---|---|---|---|---|
| `+0x10 & 0x10000000` | `+0x000` | `<28>` | `<28>` | `+0x2c8` | TCS | High |
| `+0x10 & 0x20000000` | `+0x078` | `<28>` | `<28>` | `+0x348` | SCS | High |
| `+0x10 & 0x40000000` | `+0x0f0` | `<28>` | `<28>` | `+0x350` | SCTC | High |
| `+0x10 & 0x00000020` | `+0x168` | `<28>` | `<28>` | `+0x358` | SCTD | High |
| `+0x10 & 0x80000000` (sign) | `+0x1e0` | `<3>` | `<3>` | `+0x440` | CMNUR | High |
| `+0x14 & 0x1` | `+0x1f8` | `<12>` | `<12>` | `+0x438` | ICR | High |

> **NOTE —** the first member is at struct offset `+0x000`, not `+0x8`. The decompiled `Assign` target is the bare struct pointer, so the six members sit at `0x000 / 0x078 / 0x0f0 / 0x168 / 0x1e0 / 0x1f8`. The downstream caller (`ConvertTpuTraceToXPlaneV2`) reads `.data()` of each `InlinedVector` from those offsets and pairs it with the matching kDTI base before calling the resolver.

The six populated vectors are then handed, one per call site, to the six `GetPerformanceCounterNames` invocations inside `ConvertTpuTraceToXPlaneV2`. The request flags therefore decide *which* counters to collect; the resolver decides *what they are named*.

---

## The Firmware Telemetry Event Model

### Purpose

`ConvertFirmwareTraceEntriesToXPlane<fam>` converts a firmware trace into power / temperature / throttle / P-state events. It loads the per-generation calibration bundle from the `kDeviceTypeInfo` row, constructs a `FirmwareEventBuilder<fam>`, run-length-aggregates the trace, and writes back a DVFS P-state timeline. It is instantiated per codec family: `gfc` (`0xf2b1ee0`), `vlc` (`0xf27f140`), `glc` (`0xf29d5c0`), `vfc` (`0xf28ae00`) — identical in shape.

### Entry Point

```text
ConvertFirmwareTraceEntriesToXPlane<gfc::TraceEntry> (0xf2b1ee0)
  ├─ load [kDTI + DeviceType*0x448 + 0x360..+0x378]  ── 4 doubles  (power/thermal calibration)
  ├─ load [kDTI + DeviceType*0x448 + 0x380..+0x398]  ── 4 ulongs   (meter / sensor / window counts)
  ├─ FirmwareEventBuilder<gfc>::ctor (0xf2b8780)      ── caches 19 XLines + 5 XStatMetadata
  ├─ RunLengthTracker<double,...>::ProcessTraceEntry  ── per entry: Get() value, close runs on change
  ├─ FirmwareEventBuilder::Flush                       ── close the trailing run
  └─ assign vector<pair<u64, PState>>& (out arg)       ── the DVFS timeline
```

### Algorithm

```c
function ConvertFirmwareTraceEntriesToXPlane_gfc(entries, gtc_span,    // 0xf2b1ee0
                                                 DeviceType d,
                                                 vector<pair<u64,PState>>& dvfs_out,
                                                 TpuXPlaneBuilder* xb):
    if d >= 17: trap                          // 0x448-stride bound check
    row = kDeviceTypeInfo + 1096 * d          // 1096 == 0x448
    doubles = { row[+0x360], row[+0x368], row[+0x370], row[+0x378] }   // bias/scale pair per rail
    ulongs  = { row[+0x380], row[+0x388], row[+0x390], row[+0x398] }   // meter/sensor/window counts
    fb = FirmwareEventBuilder_gfc(xb, doubles..., ulongs...)           // 0xf2b8780
    for e in entries:
        RunLengthTracker.ProcessTraceEntry(fb, e)   // Get() -> value; OnEvent() closes prev run
    fb.Flush()
    dvfs_out = fb.dvfs_timeline                       // __assign_with_size of pair<u64,PState> vector
```

### Builder State

The `FirmwareEventBuilder` ctor (`0xf2b8780`) lays out:

| obj offset | Source | Meaning | Confidence |
|---|---|---|---|
| `+0x00` | `xb` | `TpuXPlaneBuilder*` | High |
| `+0x08 / +0x0c` | `xb[+0x8c] / xb[+0x90]` | core / chip id | High |
| `+0x10 / +0x18 / +0x20 / +0x28` | kDTI `+0x360..+0x378` | 4 calibration doubles (bias/scale) | High |
| `+0x30 / +0x38 / +0x40 / +0x48` | kDTI `+0x380..+0x398` | 4 calibration ulongs (counts) | High |
| `+0x60 / +0x58` | `19` | 19-entry `TpuComponent` vector size | High |
| `+0x80` | `"power"` (id 5) | XStatMetadata | High |
| `+0x88` | `"temperature"` (id 11) | XStatMetadata | High |
| `+0x90` | `"throttle %"` (id 10) | XStatMetadata | High |
| `+0x98` | `"P State"` (id 7) | XStatMetadata | High |
| `+0xa0` | `"PCIe BW (GB/s)"` (id 14) | XStatMetadata | High |

The 19 firmware XLine components are read byte-exact from `ymmword_AB59ACC` (an `int32[19]` at `0xab59acc`): `{120,121,122,123,124,125,126,127,128,129,130,134,135,136,137,138,139,141,143}`. By `TpuComponentName`, these are the VDD-core power meters PL1..PL4, VDD-core throttle, HBM power meters PL1..PL4, HBM throttle, HBM max temperature, PCIe read utilization 1..4, PCIe write utilization 1..2, ICR Stats, and Compute-Die max temperature — all confirmed as `.rodata` strings (e.g. `"VDD Core FW Power Meter PL1(W)"`, `"Compute Die FW Max Temperature(C)"`).

### The Five Event Families

The firmware `TraceEntry` is a oneof; `FirmwareEventBuilder::Get` (`0xf2b8b00`) dispatches on the variant and computes the per-event value using the obj-stored calibration constants:

| Variant (proto message) | Get() value formula | XLine(s) / XStat | Confidence |
|---|---|---|---|
| `PowerLevelEvent` | `power(W) = (raw_energy/Δt − bias) / (meter_count·1000.0)`, scaled by obj `+0x10/+0x18` and biased by `+0x20/+0x28` | 120..128 / `"power"`; PCIe 134..139 / `"PCIe BW (GB/s)"` | High |
| `ThermalEvent` | `temperature(C) = cvtsi2sd(thermal_sensor_int)` | 130/143 / `"temperature"` | High |
| `ThrottleEvent` | `throttle% = (throttle_cycles·100.0) / cycle_window` | 124/129 / `"throttle %"` | High |
| `DvfsEvent` | `P-state = cvttsd2si(dvfs_p_state)`; push `pair<u64 ts, PState>` | DVFS timeline / `"P State"` | High |
| `MgrFwEvent` (tag 94) | manager-firmware status passthrough | mgr line | Medium |

The constants are pinned in `.rodata`: `100.0` at `qword_A2DF5C0`, `1000.0` at `qword_A2E0430`, `1.0` at `qword_A2DF230`, and the `u64→double` `2^52` reconstruction magic at `xmmword_A2C1520` / `xmmword_A2C5F90`. The PowerLevel path uses obj `+0x10`/`+0x18` as the per-rail scale and `+0x20`/`+0x28` as the bias (two rails — the `v35 == 1` and `v35 == 2` branches select which double pair).

> **NOTE —** the exact `power_level_index → power-meter-line` routing (which of PL1..PL4 maps to which `kDeviceTypeInfo` power-meter instance, VDD-core rail vs HBM rail) was decoded structurally from the `OnEvent` component-index selection, not enumerated index-by-index. The 19-line set, the 5 stats, and the power formula are pinned; the per-`power_level_index` line assignment within OnEvent is the one Medium-confidence gap in the firmware path.

### RunLengthTracker and OnEvent

`RunLengthTracker<double, FirmwareEventBuilder, ...>::ProcessTraceEntry` is the aggregation engine. For each trace entry it calls `Get()` to extract the current scalar; when the value changes (a run-length boundary), it calls `OnEvent(span=[prev_gtc, cur_gtc], prevEntry, curEntry, value)` to close the previous run as a single duration XEvent, then records the new run. `Flush` closes the trailing run.

`OnEvent` (`0xf2b8d20`, gfc) for each event: picks a component index from the variant, `GetOrCreateLine(TpuComponent)`, `AddEvent(GtcSpan, XEventMetadata)` — which stamps the universal `device_offset_ps` / `device_duration_ps` stats — then `AddStatValue<double&>` with the matching interned stat (`"power"` / `"temperature"` / `"throttle %"`, at obj `+0x80..+0x90`) and the value. The `DvfsEvent` path `push_back`s a `pair<u64 timestamp, PState>` into the timeline vector at obj `+0xf0`.

### The DVFS Timeline

The `DvfsEvent` stream builds a `vector<pair<u64 gtc, PState>>` — the convert function's output reference argument, assigned via `__assign_with_size`. `PState` is the `xprof` enum `{P_STATE_ACTIVE, P_STATE_INACTIVE, PSTATE_REQUEST_RECEIVED, PSTATE_REQUEST_COMPLETED, PSTATE_REQUEST_DROPPED}` (all five strings confirmed in `.rodata`). This timeline is then passed as a `Span<pair<u64,PState>>` into the compute-trace converter, annotating the per-instruction compute trace with the chip's observed P-state at each point — the DVFS overlay. It closes the loop with the static DVFS frequency ladders elsewhere in `kDeviceTypeInfo`: the ladders are the operating points, the `DvfsEvent` stream is the observed per-run P-state transitions.

### The Per-Generation Calibration Bundle

The `+0x360..+0x398` bundle is per-`DeviceType`, read byte-exact from `kDeviceTypeInfo`:

| Gen (row) | `+0x360` | `+0x368` | `+0x370` | `+0x378` | `+0x380` | `+0x388` | `+0x390` | `+0x398` | Confidence |
|---|---|---|---|---|---|---|---|---|---|
| v7x (idx 12, `0x1c637e0`) | `0.2` | `0.0` | `0.2` | `0.0` | `0` | `30` | `26` | `50` | High |
| v6e (idx 13, `0x1c63c28`) | `0.785` | `9.04` | `0.887` | `0.225` | `0` | `31` | `11` | `50` | High |
| (idx 10, 11) | `0` | `0` | `0` | `0` | `0` | `31` | `0` | `0` | High |
| (other rows) | `0` | `0` | `0` | `0` | `0` | `0` | `0` | `0` | High |

The four doubles are power/thermal calibration coefficients (per-rail energy→W bias/scale): v7x uses a flat `0.2` pair, v6e a richer `0.785 / 9.04 / 0.887 / 0.225` curve. The four ulongs are per-generation counts used as divisors / ranges in `Get()`: `+0x388` = number of power meters (30 on v7x, 31 on v6e), `+0x390` = number of thermal/throttle items (26 on v7x, 11 on v6e), `+0x398` = a sample/window count (50).

> **NOTE —** unlike the perf-counter descriptors (v7x-only), the firmware calibration bundle is populated on both v7x and v6e, with two further rows carrying only `+0x388 = 31`. So the firmware power/thermal/DVFS layer is *not* v7x-exclusive — only the named HW perf-counter layer is. Whether the v6e four-coefficient curve is a polynomial (`a0 + a1·x + …`) or independent per-rail coefficients was not fully separated; the `Get()` PowerLevel math uses `+0x10`/`+0x18` as bias/scale, leaving the role of the higher-order v6e terms (`9.04`, `0.887`) Low-confidence.

---

## Not Yet Traced

- **TCS / TensorCore counter names.** The `+0x2c8` base `0xa668a008 + ordinal*8` lands in a `PerformanceCounterName` band the `gfc` `PerformanceCounterNameToString` does not map (returns empty for all sampled ordinals). The set is identified (TCS, `<28>`, `0x10000000` flag) but its name table is elsewhere.
- **Full enum bit-layout.** `base + ordinal*8` proves the counter ordinal is the low bitfield (×8); the partition of the upper bits (which bits = chiplet / die / unit instance, the meaning of the `0xa5/0xa6/0xa7/0xd6` high nibbles as subsystem domain) was not bit-sliced. Recoverable by sweeping the 20 `ToString` bands.
- **Exhaustive per-set enumeration.** Ordinals 0..3 were sampled per set; the complete `≤28` SCS/SCTC/SCTD, 3 CMNUR, 12 ICR name lists are mechanically dumpable by resolving `base + ordinal*8` for the full ordinal range.
- **`power_level_index` → exact line routing** and the precise role of the v6e four-coefficient power curve (see the two NOTEs above).

---

## Related Components

| Name | Relationship |
|---|---|
| `ConvertTpuTraceToXPlaneV2<jxc::PerformanceTraceEntry>` | Caller — six sites, one per counter set; pairs each `TpuCounterIndices` member with its kDTI base |
| `gxc::gfc::profiler::PerformanceCounterNameToString` | The 20-band name dispatcher the resolver calls |
| `STATS_COUNTER_SAMPLE_ISSUED_FROM_{TCS,SCS,SCTC,SCTD,CMNUR,ICR}` | The six tracepoints carrying the sampled counter *values*; this page names them |
| `RunLengthTracker<double, FirmwareEventBuilder, ...>` | Run-length aggregation engine for the firmware trace |
| `FirmwareEventBuilder<vlc/glc/vfc/gfc>` | Per-codec instantiations of the firmware event model (identical shape) |

## Cross-References

- [kDeviceTypeInfo Producer / Readers](kdevicetypeinfo-producer-readers.md) — the `0x1c60480` array (17 × `0x448`) that backs both the descriptor fields and the calibration bundle
- [Per-DeviceType Profiler Struct](per-devicetype-struct.md) — the `0x448`-byte per-generation row layout the trailing fields belong to
- [tpu_telemetry.proto](tpu-telemetry-proto.md) — the `TpuTelemetry` proto + `TpuTelemetryHarvester` wire schema for the telemetry path
- [XStat Metadata IDs](xstat-metadata-ids.md) — the interned stat metadata (`"power"`, `"temperature"`, `"throttle %"`, `"P State"`, `"PCIe BW (GB/s)"`) the builder stamps
- [XEvent Metadata IDs](xevent-metadata-ids.md) — the per-component XEvent metadata for the 19 firmware XLines
- [TracePoints Master Registry](tracepoints-master-registry.md) — the `STATS_COUNTER` tracepoint sources and the firmware power/thermal line catalog
- [Task Proto](task-proto.md) — `XprofRequest` and the `PeriodicCounterSamplingOptions` repeated-int request fields
- [Profiling and Telemetry](overview.md) — the profiling pipeline overview
