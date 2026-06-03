# tpu_telemetry.proto

> *All names, field numbers, and symbols on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Field numbers are stable proto2 wire identifiers; other versions may add fields but will not renumber these.*

## Abstract

`tpu_telemetry.proto` is the **device-state schema** of libtpu: a small proto2 file (package `platforms_deepsea.jellyfish.xdb.tpu_telemetry`, 6 messages, 2 enums, no imports) that captures, per TPU core, the live execution state of every hardware sequencer at one instant — program counter, hardware tag, tracemark, the bound program, the pending launch queue, and an optional resolved HLO source location. It is produced by the on-host **xdb** (TPU debugger) state server and pulled on demand over gRPC. It is **not** part of the xprof trace pipeline: it is a point-in-time *state snapshot* (`AllCoreStateSummaries` — a map of `int32 global_core_id → CurrentCoreStateSummary`), the structural opposite of the `XSpace`/`XPlane`/`XLine`/`XEvent`/`XStat` *event stream* that the trace half emits. The two never share a wire blob; their only meeting point is the `TpuProfilerControlListener`'s PC→HLO metadata, which both the profiler (to stamp `XEvent` source locations) and the xdb server (to fill `SequencerInfo.hlo_location`) read.

The whole schema is generation-agnostic. The single gen-aware surface is the `TpuCoreTypeProto` / `TpuSequencerTypeProto` enum pair, which encodes the SparseCore evolution: an older `SPARSE_CORE_V0` (one sequencer + one address handler) versus the current `SPARSE_CORE` (a scalar sequencer plus the Tile-Access-Core and Tile-Execute-Core sequencers). Everything that a reader might expect to be here but is *not* — HBM bytes, die temperature, watts, ICI link health, ECC counts, clock frequency — lives in purpose-built companion protos outside this file; this page maps that boundary so a reimplementer does not look for those fields here and find a hole.

This page owns the **field-by-field schema** (every message, field number → name → type → label, grouped by message and ordered by field number), the **two enums in full**, the **identity-and-grain model** (per-host → per-chip → per-core → per-sequencer), the **producer/consumer RPC graph**, and the **companion-proto boundary catalog**. The xprof trace model is owned by [XPlane / XStat / TraceMe](xplane-xstat-traceme.md); the surrounding xprof task/session descriptor by [Task Proto](task-proto.md). Neither is re-derived here.

For reimplementation, the contract is:

- **The six message shapes and their nesting** — `AllCoreStateSummaries` ⊃ `CoreStatesEntry` (synthetic map entry) ⊃ `CurrentCoreStateSummary` ⊃ `{TpuCoreIdentifier, repeated SequencerInfo, repeated QueuedProgramInfo}`, with `TpuCoreIdentifier` ⊃ `TpuCoreOnChipProto`.
- **The exact field number → name → type for every field**, since these are the wire identifiers a compatible producer or consumer must reproduce byte-for-byte.
- **The two enums in full**, including the SparseCore-v0 vs current SparseCore split that distinguishes the sequencer set per core type.
- **The grain and identity model** — how `global_core_id`, `chip_id`, and the on-chip `(type, index)` triple address a core, and which message is the unit at each level.

| | |
|---|---|
| **Proto file** | `platforms/deepsea/jellyfish/xdb/tpu_telemetry/tpu_telemetry.proto` |
| **Package** | `platforms_deepsea.jellyfish.xdb.tpu_telemetry` |
| **Syntax** | proto2 — every field `optional` unless marked `repeated`; no field-level defaults in the descriptor |
| **Descriptor VA** | `0xc198170` in the protodesc pool (1,905 bytes), no imports |
| **Shape** | 6 messages, 2 enums, 0 services, 0 imports |
| **Schema root** | `AllCoreStateSummaries` (`map<int32 global_core_id, CurrentCoreStateSummary>`) |
| **Per-core unit** | `CurrentCoreStateSummary` |
| **Finest grain** | `SequencerInfo` (one per hardware sequencer; up to 3 on current SparseCore) |
| **Producer** | xdb per-host state server — `TpuTelemetryHarvester` (`…tpu_telemetry::TpuTelemetryHarvester`) |
| **Consumers** | `TpuDebugService.GetCurrentTpuStateSummary` · `RuntimeMetricService.GetTpuRuntimeStatus` · Megascale hang-detector |
| **Channel** | gRPC pull (request knob `include_hlo_info`) — never PJRT C-ABI |

> **NOTE —** every message name, the synthetic map-entry name `CoreStatesEntry`, all field-name strings, and the enum-value strings on this page are present verbatim in the binary's `.rodata` and descriptor pool; each message additionally has a full generated proto2 runtime class (`SequencerInfo::_table_`, `CurrentCoreStateSummary::Clear`, `AllCoreStateSummaries::MergeImpl`, arena constructors, etc.), so all six are real instantiated messages, not descriptor-only entries. Confidence on the schema is therefore CERTAIN throughout; the few inferences (producer wiring details, defaults) are flagged in place.

---

## At-a-Glance: Per-Message Field Counts

| Message | Fields | Repeated | Nested in | Role |
|---|---|---|---|---|
| `TpuCoreOnChipProto` | 2 | 0 | — | On-chip core locator: `(type, index)` |
| `TpuCoreIdentifier` | 3 | 0 | — | Global core identity: `(global_core_id, chip_id, core_on_chip)` |
| `QueuedProgramInfo` | 3 | 0 | — | One pending-launch-queue entry |
| `SequencerInfo` | 9 | 0 | `CurrentCoreStateSummary` | Live state of one hardware sequencer |
| `CurrentCoreStateSummary` | 7 | 2 | `CoreStatesEntry` | One core's full execution snapshot |
| `AllCoreStateSummaries` | 1 | 1 | — (root) | Host-wide map of all cores' snapshots |
| `AllCoreStateSummaries.CoreStatesEntry` | 2 | 0 | `AllCoreStateSummaries` | Synthetic proto2 map entry |

Two enums accompany the messages: `TpuCoreTypeProto` (4 values) and `TpuSequencerTypeProto` (7 values), detailed in [The Two Enums](#the-two-enums).

> **GOTCHA —** `CoreStatesEntry` is a *synthetic* nested message generated by the proto2 `map<K,V>` desugaring, not a hand-written type. Its `key` (1) and `value` (2) fields are the standard map-entry layout. A reimplementer writing the schema by hand should declare `map<int32, CurrentCoreStateSummary> core_states = 1;` and let the compiler synthesize the entry — but a reimplementer parsing the *wire* bytes must handle it as a `repeated` message field (number 1) of `CoreStatesEntry` records, which is exactly how it appears in the binary's descriptor.

---

## Message: TpuCoreOnChipProto

The on-chip locator. Names *which kind* of core and *which ordinal* of that kind within a single chip — e.g. "TensorCore #1" or "SparseCore #3". It is the leaf of the identity chain, embedded in `TpuCoreIdentifier`.

| # | Name | Type | Label | Meaning |
|---|---|---|---|---|
| 1 | `type` | `TpuCoreTypeProto` (enum) | optional | Core kind: TensorCore / SparseCore-v0 / SparseCore |
| 2 | `index` | `int32` | optional | Ordinal of this core *of that type* on the chip (0-based) |

> **QUIRK —** `index` is a per-type ordinal, not a flat per-chip core index. On a chip with one TensorCore and several SparseCores, the TensorCore is `(TENSOR_CORE, 0)` and the SparseCores are `(SPARSE_CORE, 0)`, `(SPARSE_CORE, 1)`, …. Conflating `index` with a chip-global core number will mis-address every SparseCore on a multi-SC chip. The flat host-wide identity is `TpuCoreIdentifier.global_core_id`, a separate field.

---

## Message: TpuCoreIdentifier

The globally unique identity of one core on the host: a flat id plus the chip and the on-chip locator. This is the message reused outside the snapshot path — `TpuDebugService.GetMachineInfo` returns `repeated TpuCoreIdentifier` as the host's core inventory, so this type doubles as the host topology descriptor.

| # | Name | Type | Label | Meaning |
|---|---|---|---|---|
| 1 | `global_core_id` | `int32` | optional | Flat id across all of the host's cores; the `AllCoreStateSummaries` map key |
| 2 | `chip_id` | `int32` | optional | Which chip on the host this core belongs to |
| 3 | `core_on_chip` | `TpuCoreOnChipProto` (message) | optional | The `(type, index)` locator within that chip |

The binary carries a producer-side helper `…tpu_telemetry::GetUniversalCoreId(tpu::TpuCoreLocation)` (mangled `…13tpu_telemetry18GetUniversalCoreIdERKN3tpu15TpuCoreLocationE`), which derives `global_core_id` from the runtime's `TpuCoreLocation` — i.e. the flat id is *computed* from the hardware location, not stored independently (HIGH confidence — symbol present; the derivation arithmetic was not traced).

---

## Message: QueuedProgramInfo

One entry of a core's pending-launch queue: a program that has been submitted but is not yet the bound/executing program. The `CurrentCoreStateSummary` carries `repeated QueuedProgramInfo` to expose queue depth and contents at the snapshot instant — the signal a hang-detector uses to see "this core has work queued but is not advancing."

| # | Name | Type | Label | Meaning |
|---|---|---|---|---|
| 1 | `run_id` | `int64` | optional | Runtime run id of the queued program |
| 2 | `launch_id` | `int64` | optional | Per-execute launch id |
| 3 | `program_fingerprint` | `bytes` | optional | Executable fingerprint of the queued program |

> **NOTE —** `run_id`/`launch_id` here are the same identifier space carried by `SequencerInfo.run_id` (the *executing* run) and `CurrentCoreStateSummary.launch_id` (the *currently-executing* launch). Diffing the queue's `(run_id, launch_id)` against the executing pair tells a consumer how far behind the queue the core's sequencers are running. These ids are also the only semantic overlap with the xprof trace half, which references the same `run_id`/`launch_id` on its host `TraceMe` events.

---

## Message: SequencerInfo

The finest-grain message and the heart of the schema: the live register snapshot of **one hardware sequencer**. A TensorCore reports one `SequencerInfo`; a SparseCore-v0 reports two (sequencer + address handler); a current SparseCore reports up to three (scalar sequencer + Tile-Access-Core + Tile-Execute-Core) — see [The Two Enums](#the-two-enums). The PC/tag/tracemark triple is what a hang-detector compares across two snapshots: if the PC has not advanced and the tag has not changed, the sequencer is stalled.

| # | Name | Type | Label | Meaning |
|---|---|---|---|---|
| 1 | `sequencer_type` | `TpuSequencerTypeProto` (enum) | optional | Which sequencer this describes (see enum) |
| 2 | `sequencer_index` | `int32` | optional | Ordinal among sequencers of that type on the core |
| 3 | `pc` | `int64` | optional | Current program counter |
| 4 | `tag` | `int64` | optional | Hardware tag register — sync / epoch marker |
| 5 | `tracemark` | `int64` | optional | Current tracemark — compiler-emitted progress marker |
| 6 | `program_id` | `int64` | optional | Id of the currently-bound program |
| 7 | `run_id` | `int64` | optional | Run id the sequencer is executing |
| 8 | `hlo_location` | `string` | optional | Source HLO location resolved at the PC (filled only on request) |
| 9 | `hlo_detailed_info` | `string` | optional | Extended HLO context string (filled only on request) |

> **GOTCHA —** fields 8 and 9 (`hlo_location`, `hlo_detailed_info`) are empty unless the gRPC request set `include_hlo_info = true`. Resolving a PC to an HLO location requires the compiler-side metadata that the `TpuProfilerControlListener` registers (the same PC→HLO map the profiler uses to stamp `XEvent` source locations). A reimplementation that always populates them pays the PC→HLO resolution latency on every poll; one that never does loses the only human-readable link from a stalled PC back to source. The knob is request-time, not schema-time — there is no field for it inside `tpu_telemetry.proto`.

> **QUIRK —** `tracemark` (field 5) is distinct from `tag` (field 4). `tag` is a hardware sync/epoch register the sequencer manages; `tracemark` is a compiler-emitted progress marker baked into the program. Two snapshots with the same `tag` but advancing `tracemark` mean the program is progressing within an epoch; same `tracemark` across snapshots is the stall signal. The `tracemark` string is heavily referenced in the binary (it is also a trace-point concept), so do not assume it is telemetry-only.

---

## Message: CurrentCoreStateSummary

One addressable core's complete execution snapshot — the map *value* in `AllCoreStateSummaries` and the wire type every consumer RPC returns unchanged. It bundles the core's identity, the per-sequencer state, the bound program, the launch queue, and a free-text error string.

| # | Name | Type | Label | Meaning |
|---|---|---|---|---|
| 1 | `core_id` | `TpuCoreIdentifier` (message) | optional | Who this snapshot is for |
| 2 | `sequencer_info` | `SequencerInfo` (message) | **repeated** | One entry per hardware sequencer on the core |
| 3 | `xdb_server_running` | `bool` | optional | Is the xdb state server live for this core |
| 4 | `program_fingerprint` | `bytes` | optional | Fingerprint of the program currently bound |
| 5 | `launch_id` | `int32` | optional | Currently-executing launch id |
| 6 | `queued_program_info` | `QueuedProgramInfo` (message) | **repeated** | Pending launch-queue contents |
| 7 | `error_message` | `string` | optional | Per-core error / fault description, if any |

> **NOTE —** `launch_id` here is `int32`, while `QueuedProgramInfo.launch_id` (and `run_id`) are `int64`. The narrowing is in the descriptor, not a transcription slip: the *executing* launch id is the 32-bit field, the *queued* ids are 64-bit. A reimplementer copying a queued `launch_id` into the executing slot must range-check. `program_fingerprint` is `bytes` in both messages (an opaque executable hash, not a string).

> **GOTCHA —** `error_message` (field 7) is **free text, not an enum**. The schema contains no status-code, link-state, or error-type enum. The typed error taxonomy a reader might expect (ECC, thermal, PCIe, ICI-fatal, …) lives in the companion `error_report.proto`, catalogued in [The Companion-Proto Boundary](#the-companion-proto-boundary). Do not attempt to parse `error_message` as a coded field.

---

## Message: AllCoreStateSummaries

The schema root: the host-wide map of every addressable core's snapshot, keyed by `global_core_id`. There is no multi-host aggregation in this file — a single `AllCoreStateSummaries` is one host's view; cross-host stitching is Megascale's job (see [Producer and Consumers](#producer-and-consumers)).

| # | Name | Type | Label | Meaning |
|---|---|---|---|---|
| 1 | `core_states` | `CoreStatesEntry` (message) | **repeated** | Every addressable core on the host (proto2 map encoding) |

### Synthetic map entry: `AllCoreStateSummaries.CoreStatesEntry`

| # | Name | Type | Label | Meaning |
|---|---|---|---|---|
| 1 | `key` | `int32` | optional | `== TpuCoreIdentifier.global_core_id` |
| 2 | `value` | `CurrentCoreStateSummary` (message) | optional | The per-core snapshot |

This is the standard proto2 `map<int32, CurrentCoreStateSummary>` desugaring; the map declaration is `core_states` at field 1, and the synthesized entry carries the conventional `key=1`, `value=2` layout. The two consumer RPCs wrap the *same* `CurrentCoreStateSummary` value type in their own top-level response (each adding a `host_name` string) rather than returning a bare `AllCoreStateSummaries` — see below.

---

## The Two Enums

The only generation-aware surface in the file. `TpuCoreTypeProto` names the kind of core a snapshot describes; `TpuSequencerTypeProto` names the kind of sequencer a `SequencerInfo` describes. Together they encode the SparseCore evolution: the older v0 design exposed a sequencer plus a separate address handler; the current SparseCore splits work across a scalar sequencer and two tile engines.

### `TpuCoreTypeProto` (4 values)

| Value | Name | Meaning |
|---|---|---|
| 0 | `TPU_CORE_TYPE_INVALID` | Unset / sentinel |
| 1 | `TPU_CORE_TYPE_TENSOR_CORE` | TensorCore (uniform across gens at this granularity) |
| 2 | `TPU_CORE_TYPE_SPARSE_CORE_V0` | Older SparseCore (sequencer + address handler) |
| 3 | `TPU_CORE_TYPE_SPARSE_CORE` | Current SparseCore (scalar sequencer + TAC + TEC) |

### `TpuSequencerTypeProto` (7 values)

| Value | Name | Belongs to core type | Meaning |
|---|---|---|---|
| 0 | `TPU_SEQUENCER_TYPE_INVALID` | — | Unset / sentinel |
| 1 | `TPU_SEQUENCER_TYPE_TENSOR_CORE_SEQUENCER` | TensorCore | The single TensorCore sequencer |
| 2 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_V0_SEQUENCER` | SparseCore-v0 | v0 main sequencer |
| 3 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_V0_ADDRESS_HANDLER` | SparseCore-v0 | v0 address handler |
| 4 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_SEQUENCER` | SparseCore (current) | Scalar sequencer |
| 5 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_TILE_ACCESS_CORE_SEQUENCER` | SparseCore (current) | Tile-Access-Core (TAC) sequencer |
| 6 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_TILE_EXECUTE_CORE_SEQUENCER` | SparseCore (current) | Tile-Execute-Core (TEC) sequencer |

> **QUIRK —** the sequencer *count per core* is implied by the enum, not stored. A TensorCore yields one `SequencerInfo` (type 1); a SparseCore-v0 yields two (types 2, 3); a current SparseCore yields up to three (types 4, 5, 6). A consumer that hard-codes "one sequencer per core" will silently drop the TAC/TEC state on current SparseCores. Drive sequencer enumeration off the `repeated sequencer_info` list length, never off an assumed core layout.

> **NOTE —** the binary carries a producer-side mapper `…tpu_telemetry::ToLocalProto(tpu::TpuSequencerType)` (mangled `…12ToLocalProtoEN3tpu16TpuSequencerTypeE`) that converts the runtime's internal `tpu::TpuSequencerType` to this `TpuSequencerTypeProto`. A symmetric core-type mapper (`tpu::TpuCoreType → TpuCoreTypeProto`) is *expected* but a distinct `ToLocalProto` overload for it was not separately confirmed in the symbol table (LOW confidence on its existence; the runtime may inline the core-type mapping). The SC ids `{4,5,6}` for `{SCS, TAC, TEC}` align with the codec-template sequencer numbering used on the trace side — see [Payload: SparseCore Band](payload-sc-band.md).

---

## Identity and Grain Model

The schema addresses state at four nested grains. A reimplementer must keep these distinct, because the same conceptual "core" is named two ways (a flat `global_core_id` and a structured `(chip_id, type, index)`), and the snapshot map is keyed by the flat one.

```text
host                       AllCoreStateSummaries          (one per host; root)
  └─ core   (global_core_id) → CurrentCoreStateSummary    (map value; the per-core unit)
        ├─ TpuCoreIdentifier  (global_core_id, chip_id,
        │                      core_on_chip:(type,index))  (who this snapshot is for)
        ├─ sequencer_info[]  → SequencerInfo               (finest grain: PC/tag/tracemark
        │                                                    per HARDWARE sequencer)
        └─ queued_program_info[] → QueuedProgramInfo       (pending launch queue)
```

| Grain | Message | Key / selector | Cardinality |
|---|---|---|---|
| Per-host | `AllCoreStateSummaries` | — (one per host) | 1 |
| Per-chip | (implicit) | `TpuCoreIdentifier.chip_id` | N chips |
| Per-core | `CurrentCoreStateSummary` | `TpuCoreIdentifier.global_core_id` (map key) | N×M cores |
| Per-sequencer | `SequencerInfo` | `(sequencer_type, sequencer_index)` | 1–3 per core |

There is no `chip` message — the per-chip grain is purely implicit in `chip_id`. A host with N chips × M cores/chip yields N×M `core_states` entries; grouping by `chip_id` reconstructs the per-chip view. Cross-host aggregation does not exist in this file; it is performed by Megascale, which collects per-host snapshots into a slice-wide picture (next section).

---

## Producer and Consumers

One producer, three consumers, all sharing the single `CurrentCoreStateSummary` wire type. The schema carries **no** sampling-interval, timestamp, or cadence field — it is strictly pull-on-demand; cadence is set entirely by the caller.

### Producer — the xdb state server

The per-host xdb (TPU debugger) state server samples each core's sequencer registers (PC, tag, tracemark, bound program, launch queue) on demand and packs them into `CurrentCoreStateSummary`. The binary names the producer object `…tpu_telemetry::TpuTelemetryHarvester`, owned by `…tpu_debugger::TpuDebugServiceImpl` (the constructor takes a `unique_ptr<TpuTelemetryHarvester>`). The harvester is the symbol that fills `SequencerInfo` from the live registers; `xdb_server_running` reflects whether that server is up per core.

> **CORRECTION (TELEM-1) —** an earlier analysis pass recorded the producer-side packer as *not yet located* ("we have the consumer RPC bindings but not the decompiled writer"). The symbol table resolves it: `…13tpu_telemetry21TpuTelemetryHarvester*` is the producer class, instantiated and held by `TpuDebugServiceImpl`. The internal-type→proto mappers `ToLocalProto(tpu::TpuSequencerType)` and `GetUniversalCoreId(tpu::TpuCoreLocation)` are the conversion helpers it calls. The register-read body inside the harvester was not decompiled (MEDIUM confidence on the read sequence; CERTAIN that the harvester is the producer).

### Consumers

```text
CurrentCoreStateSummary  (one wire type, three callers)
  ├─ TpuDebugService.GetCurrentTpuStateSummary      (xdb debugger; imports tpu_telemetry.proto)
  │     Request : GetCurrentTpuStateSummaryRequest  { bool include_hlo_info = 1; }
  │     Response: GetCurrentTpuStateSummaryResponse { string host_name = 1;
  │                                                   map<int32, CurrentCoreStateSummary> core_states = 2; }
  │     (also: GetMachineInfo → repeated TpuCoreIdentifier, the host core inventory)
  ├─ RuntimeMetricService.GetTpuRuntimeStatus       (PUBLIC Cloud TPU monitoring gRPC)
  │     Request : GetTpuRuntimeStatusRequest        { bool include_hlo_info = 1; }
  │     Response: GetTpuRuntimeStatusResponse       { string host_name = 1;
  │                                                   map<int32, CurrentCoreStateSummary> core_states = 2; }
  └─ Megascale hang-detector                        (cross-host stitching)
        MegaScaleRuntimeError ⊃ RuntimeStateSummary ⊃ RapidEyeInfo
        RapidEye's on-device TpuCoreHLO {chip_id, core_idx, hlo, pc, tag, …} mirrors CurrentCoreStateSummary
```

| Consumer | Surface | What it does | Confidence |
|---|---|---|---|
| `TpuDebugService.GetCurrentTpuStateSummary` | xdb debugger gRPC (live + post-mortem) | Returns the host's per-core state; `GetMachineInfo` returns the core inventory | CERTAIN |
| `RuntimeMetricService.GetTpuRuntimeStatus` | Public Cloud TPU monitoring gRPC | Feeds Google Cloud Monitoring; same service also carries the generic `TPUMetric` streamz envelope (`MetricType ∈ {UNKNOWN, LIBTPU}`) | CERTAIN |
| Megascale hang-detector | `MegaScaleTransport.ReportError` | Embeds per-host state into a slice-wide `RapidEyeInfo` hang report; `RapidEyeInfo` adds `tpu_version`/`chip_config_name` (the gen identity) | HIGH |

> **NOTE —** the local profiler (PJRT Profiler extension → `XSpace`) is **not** a consumer of `tpu_telemetry`. `PLUGIN_Profiler_CollectData` serves only serialized `XSpace` bytes and explicitly does not emit `TpuCoreStateSummary` / `AllCoreStateSummaries`; conversely the two state-summary RPCs never emit `XSpace`. The boundary is enforced at the wire, not by convention. See [TpuProfiler ABI](tpu-profiler-abi.md) and [overview](overview.md).

### Cadence and time-correlation

`tpu_telemetry` is timestamp-free by design — a pure snapshot. The only request-time knob is `include_hlo_info` (a bool on each request, trading PC→HLO resolution latency for richer output). Time-correlation, when needed, comes from the *enclosing* envelope: `RuntimeMetricService`'s generic `Metric` carries a `google.protobuf.Timestamp`; the Megascale/RapidEye path carries `timestamp_ns` and `incarnation_id`. A reimplementer who needs "when was this snapshot taken" must stamp it at the call site — the schema will not.

---

## tpu_telemetry vs xprof (the orthogonal contrast)

The two profiling formats answer opposite questions and never share a blob. This page owns the telemetry side; the xprof side is owned by [XPlane / XStat / TraceMe](xplane-xstat-traceme.md) and mapped by [overview](overview.md).

| Dimension | `tpu_telemetry.proto` (this page) | xprof `XSpace` ([xplane-xstat-traceme](xplane-xstat-traceme.md)) |
|---|---|---|
| Question | "What is every core doing *right now*?" | "What happened over this *interval*?" |
| Shape | State snapshot — `map<core → state>` | Time-series tree — `Space ⊃ Plane ⊃ Line ⊃ Event ⊃ Stat` |
| Root | `AllCoreStateSummaries` | `XSpace` |
| Time model | Point-in-time; no built-in timestamp | Spans (`offset_ps` + `duration_ps`) over a session |
| Producer | xdb per-host state server (`TpuTelemetryHarvester`) | `tsl::profiler::ProfilerCollection` (host + chip tracers) |
| Channel | gRPC pull (`TpuDebugService` / `RuntimeMetricService`) | PJRT Profiler C-ABI (+ legacy `TpuProfiler_*`) |
| Consumers | xdb debugger, Cloud TPU monitoring, Megascale hang-detect | xprof / TensorBoard, JAX / PT-XLA / TF |
| Semantic overlap | `run_id` / `launch_id`, resolved HLO location | same `run_id` / `launch_id` on host `TraceMe` events |
| Implementation overlap | `TpuProfilerControlListener` PC→HLO metadata | same listener stamps `XEvent` source locations |

> **QUIRK —** the *only* implementation coupling between the two formats is the `TpuProfilerControlListener`'s PC→HLO metadata: the profiler reads it to stamp `XEvent` source locations, the xdb harvester reads it to fill `SequencerInfo.hlo_location`. A reimplementer can build the telemetry path with no profiler at all — except for HLO-location resolution, which needs that shared compiler-metadata registry. Without it, `hlo_location`/`hlo_detailed_info` simply stay empty; the rest of the snapshot is unaffected.

---

## The Companion-Proto Boundary

The biggest reimplementation hazard is *expecting fields that are not here*. `tpu_telemetry.proto` carries **core execution state only** — PC, tag, tracemark, program, queue, HLO location. Every hardware-environment metric a reader might expect lives in a separate, purpose-built proto. This catalog draws the boundary so a reimplementer routes each category to the right file.

| Category a reader expects | Where it actually lives | Field(s) / enum | Confidence |
|---|---|---|---|
| Core execution state (PC/tag/tracemark/program/queue) | **`tpu_telemetry.proto`** — `CurrentCoreStateSummary`, `SequencerInfo` | `pc`, `tag`, `tracemark`, `program_id`, `run_id`, `queued_program_info` | CERTAIN |
| Utilization (TensorCore / HBM / ICI / SparseCore) | `utilization_metrics.proto` / `UtilizationMetricData.UtilizationMetric` | `metric_type ∈ UtilizationMetricType`, `utilization`, `usage`, `duty_cycle` (float) | HIGH |
| HBM usage | `utilization_metrics.proto` | `UtilizationMetricType.HBM_UTILIZATION (=2)` + `usage` float | HIGH |
| Temperature (proxy) | `utilization_metrics.proto` / `ChipThrottlingMetric` | `tray_id`, `chip_id`, `throttling_score` (int64) | HIGH |
| ICI link state / health | `utilization_metrics.proto` / `IciLinkMetric` | `port_id` (string), `link_health` (int64) | HIGH |
| ICI link topology | `ici_link_map.proto` / `IciLinkMapEntry`, `IciLinkMap` | `local_chip`, `ici_connector ∈ IciConnector(ICI0..ICI15)`, `orientation`, `polarity`, `remote_chip` | HIGH |
| Power | `power_metrics.proto` (xprof) / `PowerMetrics`, `PowerComponentMetrics` | `component_name`, `max_power`, `avg_power`, `max_moving_avg_power_{100us,1ms,10ms,1s}` | HIGH |
| ECC / thermal / power / PCIe / ICI-fatal errors | `error_report.proto` / `Error` | 12 typed sub-messages: `ecc_correctable`, `ecc_uncorrectable`, `thermal`, `power`, `pcie`, `ici_link_fatal`, `firmware`, `network`, `system_{software,hardware}`, `user`, `internal` | HIGH |
| Error / anomaly aggregation | `anomalies.proto` / `Anomaly`, `EccErrorAnomaly`, `Anomalies` | ECC + general error anomaly carriers | MEDIUM |
| Generic numeric runtime metric envelope | `tpu_metric_service.proto` / `TPUMetric`, `Metric` | gauge / counter / distribution / summary + `Attribute`; `MetricType ∈ {UNKNOWN, LIBTPU}` | HIGH |
| Per-chip throttling rollup | `utilization_metrics.proto` / `TpuHealthMetrics` | `chip_throttling_metric` + `repeated ici_link_metrics` | HIGH |
| Per-event device trace (not telemetry at all) | `xplane.proto` + per-family `trace_entries.proto` | `XEvent` + `TracePointId` enums | CERTAIN |

Companion-proto string anchors confirmed in the binary: `HBM_UTILIZATION`, `TENSORCORE_UTILIZATION`, `SPARSECORE_UTILIZATION`, `throttling_score`, `TpuHealthMetrics`, `ChipThrottlingMetric`, `IciLinkMetric`, `PowerComponentMetrics`. The companion-proto *internals* are out of scope for this page — only the routing boundary is owned here.

Enum highlights of the companion stack (for orientation, not field-level depth):

- `UtilizationMetricType` (5): `UNSPECIFIED_UTILIZATION=0`, `TENSORCORE_UTILIZATION=1`, `HBM_UTILIZATION=2`, `ICI_UTILIZATION=3`, `SPARSECORE_UTILIZATION=4`.
- `IciConnector` (16): `ICI0..ICI15` — current chips expose up to 16 ICI ports.
- `MetricType` (2): `UNKNOWN=0`, `LIBTPU=1` (`tpu_metric_service.proto`).

> **GOTCHA —** unlike `tpu_telemetry`, the hardware-utilization stream (`UtilizationMetricData`) **does** carry its own `google.protobuf.Timestamp` and is designed for periodic *push*, not pull. A reimplementer building a unified monitoring agent must not assume one cadence model across the two: core-state is pull-on-demand and timestamp-free; utilization is push-with-timestamp.

---

## Per-Generation Behavior

The schema is generation-agnostic at the field level; only the enum surface is gen-aware, and even that is *additive* (no field renumbering across gens).

- **`tpu_telemetry.proto` itself:** generation-agnostic. The only gen-sensitive surface is the core/sequencer type enum, which already spans TensorCore + SparseCore-v0 + current SparseCore. TensorCore is uniform across gens at this granularity (always one sequencer, type 1).
- **Sequencer count per core is the gen tell:** TensorCore → 1 `SequencerInfo`; SparseCore-v0 → 2 (sequencer + address handler); current SparseCore → 3 (scalar + TAC + TEC). The number of `SequencerInfo` entries, not any version field, distinguishes the SparseCore generation in a snapshot.
- **Gen identity is recorded outside this file:** the Megascale `RapidEyeInfo` carries `tpu_version` (int32) and `chip_config_name` (string). A standalone `tpu_telemetry` snapshot does not name its TPU generation — a consumer must learn the generation from the surrounding context (the host inventory via `GetMachineInfo`, or the Megascale envelope).

---

## What Was Not Traced

Honest gaps, for a reimplementer deciding what to re-derive:

- **Harvester register-read body.** `TpuTelemetryHarvester` is confirmed as the producer (symbol present, owned by `TpuDebugServiceImpl`), but the decompiled body that reads PC/tag/tracemark out of the hardware sequencer registers into `SequencerInfo` was not traced (MEDIUM confidence on the read sequence).
- **Core-type `ToLocalProto`.** A `ToLocalProto(tpu::TpuSequencerType)` mapper is confirmed; a symmetric core-type mapper was not separately found in the symbol table (LOW confidence — may be inlined).
- **Field defaults.** proto2 supports per-field defaults; the descriptor for this file carries **none**. Whether the harvester unconditionally sets any field (e.g. `xdb_server_running = false` when the server is down) was not confirmed from decompiled writers (LOW confidence).
- **`GetMachineInfo` vs `GetCurrentTpuStateSummary` snapshot sharing.** Whether the two RPCs share one register-sampling pass or take independent snapshots (a possible consistency window between core inventory and per-core state) was not determined.
- **`RuntimeMetricService` numeric-metric name catalog.** The set of `TPUMetric.name` values libtpu publishes under `MetricType.LIBTPU` (the streamz metrics riding alongside the snapshot) was not enumerated.

---

## Cross-References

- [Profiling and Telemetry overview](overview.md) — the two-format split this page sits in; the xprof pipeline and the `TpuProfilerControlListener` boundary
- [XPlane / XStat / TraceMe](xplane-xstat-traceme.md) — the orthogonal xprof event-trace model (`XSpace` ⊃ `XPlane` ⊃ `XLine` ⊃ `XEvent` ⊃ `XStat`) this schema is contrasted against
- [Task Proto](task-proto.md) — the surrounding xprof task/session descriptor
- [TpuProfiler ABI](tpu-profiler-abi.md) — the legacy `TpuProfiler_*` C-ABI; confirms `CollectData` serves only `XSpace`, never `AllCoreStateSummaries`
- [Payload: SparseCore Band](payload-sc-band.md) — the trace-side SparseCore sequencer numbering that the `{SCS=4, TAC=5, TEC=6}` enum ids align with
- [v7x Perf-Counters](v7x-perf-counters.md) — the v7x performance-counter trace lines, a separate utilization-adjacent surface on the trace side
