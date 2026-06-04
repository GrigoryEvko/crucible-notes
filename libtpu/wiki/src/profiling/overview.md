# Profiling and Telemetry

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

libtpu carries a complete copy of the TensorFlow/XLA profiling stack — the same `XSpace`/`XPlane`/`XLine`/`XEvent`/`XStat` model that JAX, PyTorch-XLA, and TensorBoard already speak — plus a TPU-specific device-trace codec and an orthogonal core-state telemetry schema. A profile is *captured* on-device as a stream of fixed-width hardware trace packets, *encoded* into a compressed `riegeli` blob, *decoded* per chip family into proto2 `TraceEntry` messages, and *shaped* into the device XPlanes of one `XSpace` that leaves the library as a serialized blob through the [PJRT Profiler extension](../pjrt/ext-profiler.md). This section reconstructs every stage of that pipeline from the binary; this page is the map.

The whole subsystem splits into two deliberately disjoint formats. **xprof** (the time-series event trace) answers *"what happened over this interval?"* — its root is `XSpace`, it is produced by a `tsl::profiler::ProfilerCollection` of host and device sub-profilers, and it is pulled through the PJRT C-ABI. **tpu_telemetry** (the point-in-time state snapshot) answers *"what is every core doing right now?"* — its root is `AllCoreStateSummaries`, it is produced by the on-host xdb (TPU debugger) state server, and it is pulled over gRPC by Cloud TPU monitoring and the Megascale hang-detector. The two never cross at the wire: `PLUGIN_Profiler_CollectData` serves only `XSpace` and the two state-summary RPCs never emit `XSpace`. They meet only through the `TpuProfilerControlListener`, which registers the compiler/debug metadata that both the profiler (to stamp XEvent source locations) and the xdb server (to resolve a PC to an HLO location) draw upon.

This page owns the **capture → encode → decode → xplane orientation**, the **per-`DeviceType` profiler dispatch**, and the **trace-point / payload taxonomy** — the shape of the pipeline and the map into its 21 sibling pages. It does not re-derive their internals: the PJRT extension struct and lifecycle live on [PJRT Profiler Extension](../pjrt/ext-profiler.md) (written); the bit-level wire format, the per-family payload field maps, the metadata-id catalogs, and the telemetry schema each have their own deep-dive page, linked below and never duplicated here.

For orientation, the contract this section reconstructs is:

- **Two formats, one boundary:** xprof `XSpace` (event trace, PJRT C-ABI) vs `tpu_telemetry` `AllCoreStateSummaries` (state snapshot, gRPC) — and the `TpuProfilerControlListener` that is their only shared dependency.
- **The five-stage xprof pipeline:** on-device fixed-16-byte trace packets → `riegeli`+zlib transport → per-family `TraceCodecInterface::DecodeEntry` → proto2 `TraceEntry` → `TpuXLineBuilder::AddEvent` into device XPlanes of one `XSpace`.
- **The four-level xprof data model:** `XSpace` ⊃ `XPlane` ⊃ `XLine` ⊃ `XEvent` ⊃ `XStat`, with interned `XEventMetadata`/`XStatMetadata`, identical to upstream TF and extended by TPU event/stat ids.
- **The per-`DeviceType` codec dispatch:** `xprof::tpu::GetTraceCodec(asic_sw::DeviceIdentifiers, int)` selects a chip-family codec (pxc/vfc/vlc/glc/gfc fixed-width; jxc legacy `PerformanceTraceEntry`), each with its own trace-point band layout.

| | |
|---|---|
| **Trace model** | `tensorflow.profiler.XSpace` ⊃ `XPlane` ⊃ `XLine` ⊃ `XEvent` ⊃ `XStat` (4-level event tree, interned metadata) |
| **Capture format** | fixed 16-byte (128-bit) LSB-first hardware trace packet, per chip family |
| **Transport** | `riegeli::ZlibReader<riegeli::StringReader>` (zlib-inflated blob of packets) |
| **Device decode** | `xprof::tpu::DecodeTraceBuffers<TraceEntry>` @ `0xf59ffa0` (pxc) — walks packets → `TraceEntry` |
| **Codec selector** | `xprof::tpu::GetTraceCodec(asic_sw::DeviceIdentifiers, int)` @ `0xf5a2900` |
| **Device sub-profiler** | `xprof::tpu::TpuProfilerImpl::CollectData(XSpace*)` @ `0xef34860` |
| **Backend** | `tsl::profiler::ProfilerCollection` (built by `CreateProfilers` @ `0x1cf50860`) |
| **xprof export** | serialized `XSpace` bytes via [PJRT Profiler extension](../pjrt/ext-profiler.md) (type 1) + legacy `TpuProfiler_*` C-ABI |
| **Telemetry schema** | `platforms/deepsea/jellyfish/xdb/tpu_telemetry/tpu_telemetry.proto` (6 msgs, 2 enums) → `AllCoreStateSummaries`, gRPC-pulled |
| **Run gate** | `xprof::tpu::TpuProfilerControlListener` @ `GetOrCreateTpuProfilerControlListener` `0xf332800` |

---

## The Capture → Encode → Decode → XPlane Pipeline

The xprof half of the subsystem is a five-stage flow from hardware ring buffer to a serialized `XSpace`. Each stage is owned by a different sibling page; this section names the stages, the symbol at each boundary, and the handoff between them.

```text
[1] HW trace ring buffer (per core, per sequencer)
      │  emits fixed 16-byte packets, LSB-first:
      │  [valid:1][started:1][trace_point_id:8][block_id:3|6][timestamp:48|45][payload @ bit 61 …]
      ▼
[2] riegeli ZlibReader<StringReader> inflate          (DecodeTraceBuffers @ 0xf59ffa0)
      │  one compressed blob → decompressed stream of packets
      ▼
[3] TraceCodecInterface::DecodeEntry  (per chip family; pxc DecodeEntry @ 0xf5af3a0)
      │  GetBits(1)valid · GetBits(1)started · GetBits(8)id → 111-entry jump table @ 0xab85bc0
      │  per-event Decode<Name>(): header + [TraceIdHeader 21/3/12] + typed payload; total-bit CHECK
      ▼
[4] proto2 TraceEntry  (oneof variant = the event; oneof tag @ +0x28)
      │
      ▼
[5] xprof::tpu TpuXLineBuilder::AddEvent → device-plane XEvent + XStats
      │  trace_point_id → XEventMetadata.name ; timestamp → XEvent offset/duration_ps ; scalars → XStat
      ▼
   one tensorflow.profiler.XSpace  ── serialized ──▶ PLUGIN_Profiler_CollectData buffer
```

### [1] Capture — on-device trace points

Each TPU core's hardware emits trace events into per-core ring buffers as constant-size 16-byte packets, LSB-first. The packet begins with a 2-bit framing prefix (`valid`, `started`), an 8-bit `trace_point_id`, a 3-or-6-bit `trace_point_block_id`, and a 48-or-45-bit cycle-counter `timestamp` — a fixed 59-bit header inside a 61-bit framing+header envelope, after which a per-event fixed-width payload begins at bit 61. The `valid` bit is the empty-slot sentinel that lets a buffer drain without a count; `started` catches torn hardware writes. The bit-level layout, the framing semantics, and the dual decode/encode dispatch are the subject of [TraceEntriesCoder](trace-entries-coder.md); the per-band payload field maps are split across the four payload pages below.

### [2] Encode / Transport — riegeli + zlib

The packed packets are never stored raw: the device trace blob is wrapped in a `riegeli::StringReader`, inflated through a `riegeli::ZlibReader`, read whole, then walked one 16-byte packet at a time. This is the container layer owned by [riegeli Trace Container](riegeli-trace-container.md). The same `riegeli`/`PutBits` primitives encode packets on the producing side (the byte-exact inverse of decode).

### [3] Decode — per-family codec

`DecodeTraceBuffers<TraceEntry>` iterates the decompressed stream calling the codec's virtual `DecodeEntry` per packet. `DecodeEntry` peeks the 2 framing bits and the 8-bit `trace_point_id`, dispatches through a 111-entry `rel32` jump table (pxc @ `0xab85bc0`) into an anonymous-namespace `Decode<Name>()`, which re-decodes the header, an optional 36-bit `TraceIdHeader`, and a typed payload, then validates the total consumed bit count against a hardcoded `CHECK` constant. The decoder mechanics are owned by [TraceEntriesCoder](trace-entries-coder.md); the resulting proto2 `TraceEntry` (oneof tag at `+0x28`) is the decode product.

### [4] TraceEntry — the decoded proto

A `TraceEntry` is a proto2 message whose active oneof variant identifies the event. The wire `trace_point_id` (banded, gappy) is *not* the proto oneof field number (dense): decode indexes the jump table by the wire id, encode dispatches on the oneof field. This two-id-space pairing and the master id↔field registry are owned by [TracePoints Master Registry](tracepoints-master-registry.md).

### [5] XPlane shaping — TraceEntry to XEvent

`xprof::tpu` turns each `TraceEntry` into a device-plane `XEvent`: `TraceHeader.trace_point_id` becomes the per-plane `XEventMetadata.name` (the enum string), `TraceHeader.timestamp` (a cycle counter) becomes the `XEvent` `offset_ps`/`duration_ps`, and the decoded variant scalars become `XStat`s. The translation — what exactly lands in `XEventMetadata.metadata` and how scalars map to stats — is owned by [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md). Host events arrive on a separate path (see below) and fold into the same `XSpace`.

> **NOTE —** the cycle-counter `timestamp` (48 or 45 bits) is the *raw device clock*, not picoseconds. The cycle→ps conversion factor (per-gen device clock rate) and the per-line `timestamp_ns` origin are applied downstream in `TpuXLineBuilder`, not in the codec. A reimplementation that treats the on-wire timestamp as picoseconds will be off by the clock period and will mis-compute the wrap interval. See [TraceEntriesCoder](trace-entries-coder.md).

---

## The xprof Data Model

The decoded events land in the canonical TensorFlow profiling tree — identical across TF/JAX/PyTorch-XLA, which is why a TPU profile opens in stock TensorBoard. libtpu ships its own copy of the `tsl::profiler` builders (`XPlaneBuilder` @ `0x1cf4d9a0` for `GetOrCreateLine`, `XLineBuilder::AddEvent` @ `0x1cf4dc40`), confirming the model is not merely imported but instantiated in-binary.

| Level | Role | Key fields | Owned by |
|---|---|---|---|
| `XSpace` | root container for one profiling session | list of `XPlane` | this page (opaque blob) |
| `XPlane` | one device core or one host thread-group | `id`, `name` (`/host:0`, `/device:TPU:0`, …), `XLine[]`, interned `XEventMetadata[]` + `XStatMetadata[]` | [XPlane / XStat / TraceMe](xplane-xstat-traceme.md) |
| `XLine` | one timeline within a plane (one core/stream) | `id`, `name`, `timestamp_ns` origin, `duration_ns`, `XEvent[]` | [XPlane / XStat / TraceMe](xplane-xstat-traceme.md) |
| `XEvent` | a point or duration event | `metadata_id` (FK → `XEventMetadata`), `offset_ps`, `duration_ps`, `XStat[]` | [XEvent Metadata IDs](xevent-metadata-ids.md) |
| `XStat` | a key/value annotation on an event | `metadata_id` (FK → `XStatMetadata`), variant value (`int64`/`uint64`/`double`/`bytes`/str-ref) | [XStat Metadata IDs](xstat-metadata-ids.md) |

The two metadata lists per `XPlane` are *interned tables*: an `XEvent` carries only a `metadata_id` index, and the name/category live once in `XEventMetadata`; likewise stats. This is why the metadata-id catalogs are their own pages — the ids are the dictionary every consumer must share with the producer.

### Host events vs device events

Two sources feed one `XSpace`. **Host** events come from `tsl::profiler::TraceMe` scopes on TPU-runtime threads (`TpuCompile`, `TpuExecute`, queue submission, megascale transport) and land on the `/host:0` plane via the `HostTracer`/`ThreadpoolProfilerInterface` sub-profilers. **Device** events come from the per-core hardware ring buffers drained and decoded as above, landing on `/device:TPU:N` planes via `TpuProfilerImpl`. The `TraceMe` macro mechanics and the host-plane shaping are owned by [XPlane / XStat / TraceMe](xplane-xstat-traceme.md).

> **GOTCHA —** the device-trace `trace_point_id` space (banded hardware enum, gappy, max `0x6e` for pxc) is a *different namespace* from the `XEventMetadata.metadata_id` space (a dense per-plane interning index). The codec's id is mapped to an `XEventMetadata` name string during stage 5; the `XEvent.metadata_id` is then allocated fresh per plane. A reimplementation that conflates the two will mis-key every device event. See [XEvent Metadata IDs](xevent-metadata-ids.md) and [TracePoints Master Registry](tracepoints-master-registry.md).

---

## The Backend: ProfilerCollection and Sub-Profilers

The xprof export path is driven by a `tsl::profiler::ProfilerCollection` — a vector of `ProfilerInterface` sub-profilers assembled by `CreateProfilers` @ `0x1cf50860` walking the global factory registry that each sub-profiler joins via `RegisterProfilerFactory` @ `0x1cf50780`. The `ProfilerCollection` is the object the PJRT `PLUGIN_Profiler` handle owns and delegates `Start`/`Stop`/`CollectData` to; that delegation, the C-ABI lifecycle, and the handle layout are fully documented on [PJRT Profiler Extension](../pjrt/ext-profiler.md) — this page does not repeat them.

Each backend op (`ProfilerCollection::Start` @ `0xf6a1640`, `Stop` @ `0xf6a16c0`, `CollectData(XSpace*)` @ `0xf6a1740`) iterates the inner vector and calls the matching slot on every sub-profiler. Confirmed members:

| Sub-profiler | Side | Key symbol | Addr | Confidence |
|---|---|---|---|---|
| `xprof::tpu::TpuProfilerImpl` | device — drains per-chip ring buffers, decodes, builds device planes | `TpuProfilerImpl::CollectData(XSpace*)` | `0xef34860` | CERTAIN |
| `xla::profiler::HostTracer` | host — `TraceMe`/CPU events into `/host:0` (factory in `xprof::cpu`) | `HostTracer::CollectData(XSpace*)` | `0xf32fb40` | CERTAIN |
| `tsl::profiler::ThreadpoolProfilerInterface` | host — threadpool dispatch events | `ThreadpoolProfilerInterface::CollectData(XSpace*)` | `0xf3326c0` | CERTAIN |

`CreateProfilers` wraps each factory output in a `ProfilerController` for crash isolation; the full factory inventory is populated lazily across several static-init blocks and is not exhaustively enumerated (LOW confidence on completeness — same gap noted on the PJRT page).

### The run gate

Device profiling runs *concurrently* with execution, mediated by the `xprof::tpu::TpuProfilerControlListener` singleton (`GetOrCreateTpuProfilerControlListener` @ `0xf332800`). Each chip driver queries `CanStartProfiler(TpuChipLocation, TpuChipProfiler*, int)` @ `0xf3328c0` before opening its trace ring buffer and polls `MustStopProfiler(TpuChipLocation)` @ `0xf332a00` for a mid-run stop. This listener is also the shared PC→HLO metadata source that lets the xdb state server fill `SequencerInfo.hlo_location` — the one point where the trace and telemetry halves touch.

---

## Per-DeviceType Codec Dispatch

There is no single device-trace codec: each TPU chip family has its own `TraceCodecInterface<TraceEntry>` (vtable slots `DecodeEntry`/`EncodeEntry`/`GetMaxEntrySize`/`GetEntryPacketSize`), selected at runtime by `xprof::tpu::GetTraceCodec(asic_sw::DeviceIdentifiers, int)` @ `0xf5a2900` from a `util_registration::StaticMapBase` factory keyed by the chip codename. The packet size (16 bytes) and the 61-bit framing+header envelope are universal; what differs per family is the header split (3-vs-6-bit `block_id`, 48-vs-45-bit timestamp) and the per-trace-point payload field maps. The selector and the producer/reader wiring are owned by [Per-DeviceType Profiler Struct](per-devicetype-struct.md) and [kDeviceTypeInfo Producer / Readers](kdevicetypeinfo-producer-readers.md).

| Family | `CreateTraceCodec` | TraceEntry type | block_id | timestamp | Codec page area | Confidence |
|---|---|---|---|---|---|---|
| pxc | `plc::driver::profiler::CreateTraceCodec` @ `0xf5af2c0` (pxc family) | fixed-width `TraceEntry` | 3 | 48 | [TraceEntriesCoder](trace-entries-coder.md) | CERTAIN |
| vfc | `vfc::driver::profiler::CreateTraceCodec` @ `0xf5f5da0` | fixed-width `TraceEntry` | 6 | 45 | [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) | CERTAIN |
| vlc | `vlc::driver::profiler::CreateTraceCodec` @ `0xf5d5180` | fixed-width `TraceEntry` | 3 | 48 | [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) | CERTAIN |
| glc | `glc::driver::profiler::CreateTraceCodec` @ `0xf6282e0` | fixed-width `TraceEntry` | 6 | 45 | [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) | CERTAIN |
| gfc | `gfc::driver::profiler::CreateTraceCodec` @ `0xf65ed00` | fixed-width `TraceEntry` | 6 | 45 | [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) | CERTAIN |
| jxc | (legacy path) | `jxc::PerformanceTraceEntry` | — | — | [Payload: jxc Legacy](payload-jxc-legacy.md) | HIGH |

> **QUIRK —** jxc uses a *different* TraceEntry type — `asic_sw::driver::deepsea::jxc::PerformanceTraceEntry`, decoded by its own `DecodeTraceBuffers<PerformanceTraceEntry>` instantiation — not the shared fixed-16-byte `TraceEntry` codec the five current families use. A reimplementation that assumes one packet schema across all gens will misparse jxc traces. The jxc DMA/HbmMux/brn_perf specifics are on [jxc DMA / HbmMux / brn_perf](jxc-dma-hbmmux-brnperf.md) and [Payload: jxc Legacy](payload-jxc-legacy.md).

---

## The Trace-Point / Payload Taxonomy

The ~99–144 trace points per family (per-gen cardinality differs) are not a flat enum — they are organized into *subsystem bands* of contiguous `trace_point_id` ranges, with reserved gaps between bands that all dispatch to a common error label. The bands carve the device-trace space by hardware unit; each band's payload field maps live on a dedicated page so this opener stays a map, not a dump.

| Band | `trace_point_id` range (pxc) | Subsystem | Payload page | Confidence |
|---|---|---|---|---|
| UHI | 0–10 | host-DMA / address translation | [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) | HIGH |
| OCI | 20–27 | on-chip interconnect engine | [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) | HIGH |
| ICI | 40–55 | inter-chip-interconnect / collective fabric | [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) | HIGH |
| TCS | 80–97 | TensorCore sequencer sync/control + throttle | [Payload: SparseCore Band](payload-sc-band.md) | HIGH |
| CMQ | 100–110 | command-queue | [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) | MEDIUM |
| (reserved) | 11–19, 28–39, 56–79, 98–99 | unused — all → common error label | n/a (decode rejects) | CERTAIN |

A small set of cross-cutting render/timeline concerns sit above the raw bands: the ICR DMA-timeline derivation ([ICR DMA-Timeline Band](icr-dma-timeline-band.md)), DMA endpoint rendering ([DMA Endpoint Rendering](dma-endpoint-rendering.md)), and the v7x performance-counter lines ([v7x Perf-Counters](v7x-perf-counters.md)). Most variant messages also carry a shared 36-bit `TraceIdHeader` (`transaction_id:21`, `core_id:3`, `chip_id:12`) immediately after the header — the per-transaction identity that lets a multi-packet DMA be stitched back together; some events (OCI read/write) carry three of them.

> **GOTCHA —** the band ranges and reserved gaps are *per family*. The pxc ranges above are the worked example; vfc/vlc/glc/gfc shift band boundaries as trace-point cardinality grows (≈99 → ≈144 events). Drive band detection off the per-family decode jump table, never off a hardcoded pxc range. See [TracePoints Master Registry](tracepoints-master-registry.md).

---

## Telemetry: The Orthogonal State Snapshot

`tpu_telemetry.proto` (package `platforms_deepsea.jellyfish.xdb.tpu_telemetry`, 6 messages, 2 enums, no imports) is **not** part of the xprof pipeline — it is a separate, pull-on-demand core-state snapshot produced by the on-host xdb state server, confirmed in the binary by the `platforms_deepsea::jellyfish::xdb::tpu_telemetry::*` proto symbols (e.g. `AllCoreStateSummaries`, `CurrentCoreStateSummary`, `SequencerInfo`, `QueuedProgramInfo`, `TpuCoreIdentifier`, `TpuCoreOnChipProto`) and the descriptor path `platforms/deepsea/jellyfish/xdb/tpu_telemetry/tpu_telemetry.proto`. Its root, `AllCoreStateSummaries`, is a `map<int32 global_core_id, CurrentCoreStateSummary>`; each `CurrentCoreStateSummary` carries `repeated SequencerInfo` (per hardware sequencer: `pc`, `tag`, `tracemark`, `program_id`, `run_id`, `hlo_location`) and a `repeated QueuedProgramInfo` launch queue. The full field-by-field schema, the producer/consumer graph (xdb debugger, Cloud TPU `RuntimeMetricService.GetTpuRuntimeStatus`, Megascale hang-detector), and the boundary against the companion hardware-telemetry protos are owned by [tpu_telemetry.proto](tpu-telemetry-proto.md).

It carries *core execution state* only — not HBM bytes, temperature, watts, ICI BER, or ECC counters; those live in purpose-built companion protos (`utilization_metrics.proto`, `power_metrics.proto`, `error_report.proto`, …) outside this schema. The one gen-aware surface is the `TpuCoreTypeProto` / `TpuSequencerTypeProto` enum, which encodes the SparseCore evolution: `SPARSE_CORE_V0` (sequencer + address handler) vs the current `SPARSE_CORE` (scalar sequencer + Tile-Access-Core + Tile-Execute-Core sequencers, ids 4/5/6).

> **NOTE —** telemetry and xprof are produced by different runtime subsystems (xdb state server vs `tsl::profiler` `ProfilerCollection`) and consumed over different channels (gRPC pull vs PJRT C-ABI). They share only a small *semantic* overlap (both reference `run_id`/`launch_id` and an HLO location) and one *implementation* dependency (the `TpuProfilerControlListener`'s PC→HLO metadata). They are never serialized into the same blob.

---

## Section Map

The 21 sibling pages divide as follows. This page is the orientation; each link is the deep dive that owns its internals.

**Entry and ABI**
- [PJRT Profiler Extension](pjrt-profiler-extension.md) — the type-1 extension as a profiling-section view
- [TpuProfiler ABI](tpu-profiler-abi.md) — the legacy `TpuProfiler_*` C-ABI quintuplet sharing the same backend
- [Per-DeviceType Profiler Struct](per-devicetype-struct.md) — the codec selector and per-family profiler struct
- [kDeviceTypeInfo Producer / Readers](kdevicetypeinfo-producer-readers.md) — who fills and reads the device-type table

**Capture and transport (the wire)**
- [TraceEntriesCoder](trace-entries-coder.md) — the fixed-16-byte packet, framing, header, decode/encode dispatch
- [riegeli Trace Container](riegeli-trace-container.md) — the zlib/`riegeli` compressed transport
- [TracePoints Master Registry](tracepoints-master-registry.md) — the wire-id ↔ oneof-field two-id-space master table

**Payload field maps (per band/family)**
- [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) — host-DMA, on-chip interconnect, fabric bands
- [Payload: SparseCore Band](payload-sc-band.md) — TensorCore/SparseCore sequencer band
- [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) — the newer-family payload deltas
- [Payload: jxc Legacy](payload-jxc-legacy.md) — the legacy `PerformanceTraceEntry` schema
- [ICR DMA-Timeline Band](icr-dma-timeline-band.md) — DMA timeline derivation
- [DMA Endpoint Rendering](dma-endpoint-rendering.md) — endpoint-pair rendering
- [jxc DMA / HbmMux / brn_perf](jxc-dma-hbmmux-brnperf.md) — jxc DMA / HBM-mux / perf-counter specifics
- [v7x Perf-Counters](v7x-perf-counters.md) — v7x performance-counter lines

**Shaping into XSpace**
- [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md) — the decode-product → XPlane translation
- [XPlane / XStat / TraceMe](xplane-xstat-traceme.md) — the xprof builders and host `TraceMe` emission
- [XEvent Metadata IDs](xevent-metadata-ids.md) — the interned event-id dictionary
- [XStat Metadata IDs](xstat-metadata-ids.md) — the interned stat-id dictionary

**Telemetry (orthogonal)**
- [tpu_telemetry.proto](tpu-telemetry-proto.md) — the core-state snapshot schema and its RPC graph
- [Task Proto](task-proto.md) — the surrounding task/session descriptor

---

## Cross-References

- [PJRT Profiler Extension (type 1)](../pjrt/ext-profiler.md) — the C-ABI entry point: extension node, `PLUGIN_Profiler_Api` vtable, `Create/Start/Stop/CollectData/Destroy` lifecycle, and the `ProfilerCollection` bridge this page builds on
- [TraceEntriesCoder](trace-entries-coder.md) — the bit-level capture/encode/decode wire format summarized in stages 1–4 here
- [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md) — stage 5, the decode-product to XPlane translation
- [XPlane / XStat / TraceMe](xplane-xstat-traceme.md) — the xprof builders and the host-event path that joins the device path in one `XSpace`
- [tpu_telemetry.proto](tpu-telemetry-proto.md) — the orthogonal state-snapshot format and the boundary against xprof
- [TracePoints Master Registry](tracepoints-master-registry.md) — the trace-point id space and the band taxonomy this page maps
