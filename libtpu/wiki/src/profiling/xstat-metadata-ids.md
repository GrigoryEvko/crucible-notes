# XStat Metadata IDs

> *All addresses, offsets, and string VAs on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary ships full C++ symbols; `.text`/`.rodata` VMA == file offset. Other versions will differ.*

## Abstract

An `XStat` is the **annotation** primitive of the profiler's `XSpace`: a typed key/value pair that hangs off an `XEvent` (or off an `XLine`/`XPlane`). The *key* is an `XStatMetadata.id` — an index into the owning `XPlane`'s `stat_metadata` map — and the *value* is one of six protobuf `oneof` arms (`double`/`uint64`/`int64`/`string`/`bytes`/`ref`). This page is the **stat catalog**: the interned `XStatMetadata` *name* strings libtpu attaches to TPU device and host events, grouped by category, each with its value type and the trace-point or payload field that produces it. It is the sibling of [XEvent Metadata IDs](xevent-metadata-ids.md), which catalogs the *event* dictionary — stats annotate events, so the two dictionaries are disjoint and live in separate maps on the same plane.

The shape maps onto a frame an LLVM/MLIR engineer owns. If an `XEvent` is an instruction, an `XStat` is an operand-attribute: `metadata_id` is the attribute *kind* (interned once per plane, like an MLIR attribute name), and the `oneof value` is the attribute *payload*. As with [XEvent Metadata IDs](xevent-metadata-ids.md), there is **no single global stat-id enum** — `XStat.metadata_id` is a plane-local interning key, and the same integer means different things on different planes. What *is* stable is the **name**: the canonical tsl `StatType` enum (`GetStatTypeStrMap()::stat_type_str_map`) provides the well-known names, and `XPlaneBuilder::GetOrCreateStatMetadata(name)` interns each on first sight, assigning the next free plane-local id. So this page catalogs by name + value-type + producer, not by fixed integer — exactly as the [TraceEntry to XEvent](trace-entry-to-xevent.md) page catalogs the per-subscriber stat emission.

Two stats are special and are the only ones with a *de facto* fixed integer in this binary: `device_offset_ps` (StatType **147**) and `device_duration_ps` (StatType **148**), the absolute device-time pair that the universal [`TpuXLineBuilder::AddEvent(GtcSpan)`](trace-entry-to-xevent.md#the-universal-stamp--addeventgtcspan) stamps on every device `XEvent`. Their `XStatMetadata*` are cached at `TpuXLineBuilder+0x38`/`+0x40` at line-builder construction; every other stat is added dynamically through the generic [`XStatsBuilder::AddStatValue<T>`](xplane-xstat-traceme.md) path, with the `oneof` arm selected by the C++ value type `T`.

This page is a **reference catalog**. It covers: the `XStatMetadata` model and the six value arms; how `T` selects the arm at the `AddStatValue` call site; the universal `device_*_ps` pair; the per-subscriber dynamic stats (sync wait reason, DMA byte count, counter sample); the canonical tsl `StatType` name catalog grouped by category; and the protobuf-message-valued stats (`CoreDetails`, `PowerComponentMetrics`) that ride the `bytes` arm.

| | |
|---|---|
| **Owning structure** | `XStatMetadata` (proto2: `id` f1, `name` f2, `description` f3) |
| **Map** | `XPlane.stat_metadata` — `map<int64, XStatMetadata>` (f5), plane-local |
| **Stat record** | `XStat` (proto2): `metadata_id` f1, `oneof value` {2..7} |
| **Intern (string)** | `XPlaneBuilder::GetOrCreateStatMetadata(string_view)` (also `string&&`, `long`) |
| **TPU intern** | `xprof::TpuXPlaneBuilder::GetOrCreateStatMetadata<string_view>` |
| **Add value** | `XStatsBuilder<XEvent>::AddStatValue<T>` — 8 confirmed `T` instantiations |
| **Name table** | `GetStatTypeStrMap()::stat_type_str_map` (canonical tsl `StatType` names) |
| **Fixed-integer pair** | `device_offset_ps` = 147, `device_duration_ps` = 148 |
| **Always-on producer** | `TpuXLineBuilder::AddEvent(GtcSpan)` @ `0xf1df1e0` (stamps 147/148) |

---

## The XStatMetadata Model

### Purpose

`XStatMetadata` is the per-plane dictionary entry that *types* a stat. An `XStat` carries only an integer `metadata_id` and a value; the human name and description live once in `XStatMetadata`, keyed by that id in the plane's `stat_metadata` map. This is the same intern-once / reference-by-id design as `XEventMetadata`, and for the same reason: a 64-byte HLO-op name or a 34-byte wait-reason string is interned once per plane and every event that needs it references the small integer id.

### Schema

Byte-confirmed proto2 layout (from `xplane.proto`, identical structure across host and device planes):

```text
message XStatMetadata { int64 id = 1; string name = 2; string description = 3; }

message XStat {
  int64 metadata_id = 1;            // KEY into XPlane.stat_metadata
  oneof value {
    double  double_value = 2;       // oneof case 2
    uint64  uint64_value = 3;       // oneof case 3   (also used for ref by tsl convention)
    int64   int64_value  = 4;       // oneof case 4
    string  str_value    = 5;       // oneof case 5
    bytes   bytes_value  = 6;       // oneof case 6
    uint64  ref_value    = 7;       // oneof case 7   (an interned StatMetadata id)
  }
}
```

> **GOTCHA —** the `XStat` `oneof case` discriminant numbers (2..7) are **field numbers**, and they do **not** line up with the integer the `AddStatValue` decompilation writes into the discriminant slot. The [TraceEntry to XEvent](trace-entry-to-xevent.md#payload-scalar-to-xstat) page records the runtime discriminant values byte-confirmed at the call sites: `int64` writes case **3**, `uint64` writes case **2**, `ref` writes case **7**. The proto *field* numbering (int64=f4, uint64=f3) and the in-memory *oneof discriminant* (int64→3, uint64→2) are two different encodings of the same arm; a reimplementer must not assume they are equal. The catalog below lists the proto field number; cross-reference the trace-entry page for the in-memory discriminant.

### The `ref_value` indirection

`ref_value` (f7) is the one non-obvious arm: its value is **another `XStatMetadata.id`** on the same plane, used when a stat's value is itself an interned string (so the string is stored once in `stat_metadata` and many events point at it by id rather than each carrying a copy). The sync wait-reason stat uses this — see [the wait-reason row](#sync-and-control-stats). The `AddStatValue<XStatMetadata&>` instantiation (`AddStatValueIRNS3_13XStatMetadataEEE`, confirmed symbol) is exactly this path: pass an interned-string `XStatMetadata&`, get a `ref_value` arm pointing at its id.

---

## Value Type Selection — `AddStatValue<T>`

### Purpose

A stat's value arm is **not** chosen by a per-name table — it is chosen by the C++ type `T` of the argument at the `XStatsBuilder<XEvent>::AddStatValue<T>` call site. The template is instantiated once per value type used in the binary, and each instantiation hard-codes the `oneof` arm it writes. This is the single most important fact for reimplementing the stat layer: to know an arm, look at the call-site type, not the name.

### Confirmed instantiations

Eight `AddStatValue<T>` instantiations are present as mangled symbols on the `XStatsBuilder<tensorflow::profiler::XEvent>` (per-event) and `XStatsBuilder<XEventMetadata>` (per-event-type) builders. The arm each writes:

| `T` (mangled fragment) | C++ type | `oneof` arm (field) | Used for |
|---|---|---|---|
| `Il` | `long` | `int64_value` (f4) | `device_offset_ps`/`device_duration_ps`, step/counter ids |
| `Im` | `unsigned long` | `uint64_value` (f3) | DMA byte count, transaction/correlation ids |
| `IRd` | `double&` | `double_value` (f2) | SPI power(W) samples, bandwidth, occupancy_pct |
| `IRNSt…basic_string…` | `std::string&` | `str_value` (f5) | tensor_shapes, kernel_details, source_stack |
| `IRNSt…basic_string_view…` | `string_view&` | `str_value` (f5) | hlo_op/hlo_module names, fixed labels |
| `IRNS3_13XStatMetadataE` | `XStatMetadata&` | `ref_value` (f7) | sync wait reason (interned-string ref) |
| `IRNS3_11CoreDetailsE` | `CoreDetails&` | `bytes_value` (f6) | per-core hardware identity blob |
| `IRNS3_21PowerComponentMetricsE` | `PowerComponentMetrics&` | `bytes_value` (f6) | power/thermal component metrics blob |

> **NOTE —** the two message-typed instantiations (`CoreDetails`, `PowerComponentMetrics`) serialize the protobuf message into the `bytes` arm. There is **no** dedicated arm for sub-messages; a structured stat is a serialized blob in `bytes_value` (f6), the same arm the device-event `TraceEntry` payload uses in `XEventMetadata.metadata`. A reimplementer must serialize these to bytes, not invent a new arm.

> **QUIRK —** there is an `XStatsBuilder<XEvent>::CopyStatValue(XStat&, XPlane&, XStat*)` symbol — stat copy during plane merge re-interns the source stat's metadata name into the destination plane and rewrites `metadata_id`. This is why stat *ids* are not stable across a merge but stat *names* are: merge keys on the name, not the integer. Build the catalog on names.

---

## The Universal Device-Time Pair

### Purpose

Two stats are stamped on **every** device `XEvent` that carries timing, unconditionally (subject only to both metadata pointers being interned). They are the only stats with a fixed StatType integer in this binary, because the line builder caches their `XStatMetadata*` at construction rather than looking them up by name per event.

### The pair

| Stat name | StatType id | Value arm | Producer |
|---|---|---|---|
| `device_offset_ps` | **147** | `int64_value` (f4) | `TpuXLineBuilder::AddEvent(GtcSpan)` @ `0xf1df1e0`, cached `+0x38` |
| `device_duration_ps` | **148** | `int64_value` (f4) | `TpuXLineBuilder::AddEvent(GtcSpan)` @ `0xf1df1e0`, cached `+0x40` |

Both carry **absolute** device wall time in picoseconds, computed by the GTC→ps math (`round(gtc_ticks × 1e9 / (clock × 16))`, 128-bit). They duplicate the `XEvent.offset_ps`/`duration_ps` fields, which are *line-relative*. The full mechanism — why both exist, the conditional gate, the 128-bit divide — is documented on [TraceEntry to XEvent](trace-entry-to-xevent.md#the-universal-stamp--addeventgtcspan). What this page adds: these two are the catalog's anchor row, the only stat names whose integer id is observable in the binary (as the `+0x28` offset into the cached `XStatMetadata` written into `XStat.metadata_id`).

> **GOTCHA —** a `TpuXLineBuilder` constructed without both StatTypes 147/148 interned (e.g. a counter-only line) emits events with line-relative timing but **no** `device_*_ps` stats. The pair is conditional on `this+0x38 && this+0x40` being non-null. Intern both at line-builder construction or lose the absolute timebase on every event.

---

## Sync and Control Stats

### Purpose

The stateful sync subscriber surfaces *why* a wait happened and *which* sync flag, as dynamic stats on the `SyncWait:<n>` duration event ([begin/end pairing](trace-entry-to-xevent.md#beginend-pairing--duration-events)). These are the highest-value TPU-specific device stats.

| Stat name | Value arm | Producer trace-point / field |
|---|---|---|
| sync wait reason — e.g. `"TensorCore waiting for Host Infeed"` (`@0x871cd36`, len `0x22`) | `ref_value` (f7) | id 86/87/80 sync pair, interned via `AddStatValue<XStatMetadata&>` @ `0xf1e1da0` |
| `sync_flag_id` | `int64_value` (f4) | `SyncFlagValue()` / `sync_flag_number` on TCS sync events (80–88) |
| `overlay_id` | `int64_value` (f4) | overlay subscriber, overlay trace-points → TC Overlay line (7) |
| `step_id` | `int64_value` (f4) | step tracker, `SET_TRACEMARK` (84) step marks |
| `step_num` | `int64_value` (f4) | step tracker, step sequence |
| `group_id` | `int64_value` (f4) | step/event grouping (tsl canonical) |

> **NOTE —** for the `id==255` dummy/sentinel sync entry the wait-reason `XStatMetadata.name` is the **empty string** (len 0) — the stat is still emitted, but its interned name is empty, so a consumer sees a `ref_value` arm pointing at a nameless stat. A reimplementation that asserts non-empty stat names will trip on the sentinel.

---

## Memory / DMA Stats

### Purpose

The DMA subscriber surfaces the transferred byte count of a matched DMA span; the OCI/HDE/ICI bands surface byte counts and chunk counts read straight from the decoded `TraceEntry` variant fields. These ride the `uint64`/`int64` arms.

| Stat name | Value arm | Producer trace-point / field |
|---|---|---|
| DMA byte count (interned `bytes_transferred` or a per-line label) | `uint64_value` (f3) | `MemoryDataEnd()` on DMA completion, `AddStatValue<unsigned long>` @ `0xf1df460` |
| `bytes_transferred` | `uint64_value` (f3) | DMA / memcpy events (tsl canonical, string confirmed) |
| `bytes_accessed` | `uint64_value` (f3) | memory-access events (tsl canonical) |
| `bytes` | `uint64_value` (f3) | generic byte-count (tsl canonical) |
| `memory_bandwidth` | `double_value` (f2) | derived BW on memory events (tsl canonical) |
| `queue_id` | `int64_value` (f4) | DMA/queue events (tsl canonical) |

> **NOTE —** the DMA match key (`dma_id`, from `GetDmaId()` @ `0xf698180`) is the *pairing* key, **not** a stat — it is never interned into `stat_metadata`. The string `"dma_id"` is absent from the binary (confirmed MISS), consistent with it being an in-memory `FlatHashMap` key rather than a serialized stat name. Do not look for a `dma_id` stat; the surfaced DMA scalar is the byte count.

---

## Counter / Power / Throttle Stats

### Purpose

Counter lines and the firmware power/thermal subsystem surface samples as stats. The counter sample uses a fixed rodata label; power components ride the message-valued `bytes` arm.

| Stat name | Value arm | Producer |
|---|---|---|
| `"Available Count"` (`@0x84e8ef6`, len `0xf`) | `int64_value` (f4) | TC0 infeed-buffer counter line sample |
| SPI power sample (W) | `double_value` (f2) | `SpiSamplerSubscriber`, ids 168/169 → SPI Meter lines (118/119) |
| `PowerComponentMetrics` blob | `bytes_value` (f6) | firmware power/thermal subscriber, `AddStatValue<PowerComponentMetrics&>` |
| throttle counters (num_thermal/electrical_throttles) | `int64_value` (f4) | id 97 / 200.. throttle events → Power Throttle line (58) |

> **NOTE —** the counter-sample line uses a constant rodata stat name (`"Available Count"`), interned via `GetOrCreateStatMetadata(string_view)`, in contrast to the canonical-tsl-named stats above. Fixed-label stats and tsl-enum stats share the same `stat_metadata` map — they differ only in where the name string originates (a rodata literal vs the `GetStatTypeStrMap` table).

---

## HLO / XLA-Op Stats

### Purpose

The HLO subscriber and the post-hoc symbolizer attach compiler-side identity to XLA-op events on the `"XLA Ops"` line. These are the canonical tsl names, all string-valued except the numeric ids.

| Stat name | Value arm | Producer |
|---|---|---|
| `hlo_op` | `str_value` (f5, string_view) | `TRACE_INSTRUCTION`/`SET_TRACEMARK` (85/84) via symbolizer |
| `hlo_module` | `str_value` (f5) | HLO module name via `GetOrCreateXlaEventMetadata(pair)` |
| `hlo_category` | `str_value` (f5) | symbolizer (tsl canonical) |
| `program_id` | `int64_value` (f4) | per-program identity |
| `tf_op` | `str_value` (f5) | TF-op name (tsl canonical) |
| `tf_function_call` | `str_value` (f5) | TF function call (tsl canonical) |
| `tensor_shapes` | `str_value` (f5) | op tensor-shape string (tsl canonical) |
| `kernel_details` | `str_value` (f5) | kernel detail string (tsl canonical) |
| `source_stack` | `str_value` (f5) | source-location stack (tsl canonical) |
| `flops` | `int64_value` (f4) | op FLOP count (tsl canonical) |
| `long_name` | `str_value` (f5) | full op name (tsl canonical) |
| `level` | `int64_value` (f4) | nesting level (tsl canonical) |

> **NOTE —** these stat *names* are confirmed present as ASCII in the binary, but the exact subscriber→stat-emission for each on each chip family was not byte-traced — only the `hlo_op`/`hlo_module` path (via `GetOrCreateXlaEventMetadata`) and the symbolizer enrichment are confirmed. Treat the value-arm assignment of the canonical-tsl rows as derived from the tsl `StatType` schema (which fixes the arm per name), HIGH where a producer is named, LOW where only the string is confirmed present.

---

## Identity / Host / Provenance Stats

### Purpose

Plane- and host-level identity stats, plus the build-provenance stat. These appear on `/host:<n>` planes and as plane-level stats; they are interned the same way but are largely host-side (dynamic name-interning through `tsl::profiler::XPlaneBuilder`).

| Stat name | Value arm | Source |
|---|---|---|
| `device_id` | `int64_value` (f4) | plane/event device identity (tsl canonical) |
| `core_id` | `int64_value` (f4) | TPU core ordinal, `CoreId()` on device events |
| `chip_id` | `int64_value` (f4) | TPU chip ordinal (TPU-specific) |
| `device_type` | `str_value` (f5) | device-type label (tsl canonical) |
| `run_id` | `int64_value` (f4) | execution run identity (tsl canonical) |
| `correlation_id` | `uint64_value` (f3) | host↔device correlation (tsl canonical) |
| `context_id` | `int64_value` (f4) | context identity (tsl canonical) |
| `producer_id` | `int64_value` (f4) | flow producer (tsl canonical, flow events) |
| `is_eager` | `int64_value` (f4, bool) | eager-mode flag (tsl canonical) |
| `step_name` | `str_value` (f5) | step label (tsl canonical) |
| `self_duration_ps` | `int64_value` (f4) | self (exclusive) duration (tsl canonical) |
| `min_duration_ps` | `int64_value` (f4) | min instance duration (tsl canonical, event-type stat) |
| `total_profile_duration_ps` | `int64_value` (f4) | profile span (tsl canonical, plane stat) |
| `max_iteration_num` | `int64_value` (f4) | iteration count (tsl canonical) |
| `num_occurrences` | `int64_value` (f4) | aggregated occurrence count (tsl canonical) |
| build provenance — `"<timestamp> cl/<changelist>"` (`absl::StrFormat("%s cl/%s", BuildData::Timestamp(), BuildData::Changelist())`) | `str_value` (f5) | `AddPluginMetadata()` @ `0xf3165c0`, StatType **0xA6 = 166** |

> **QUIRK —** `num_occurrences` is *also* an `XEvent` `oneof data` field (f5), not only a stat. An aggregated `XEvent` (one event-type, many instances folded) stores its count in `XEvent.num_occurrences`; the `num_occurrences` *stat* is the per-event-type variant. A reimplementer must not store the same count in both places — pick the event field for aggregated events and the stat for type-level aggregation.

---

## Stat Categories — At a Glance

The canonical tsl `StatType` enum (seeded by `GetStatTypeStrMap()::stat_type_str_map`) plus the TPU-specific names group into these axes. Rather than dump the full ~150-entry tsl enum, the table gives the category, its dominant value arm, and the producing subsystem:

| Category | Dominant arm | Stat names (representative) | Producer |
|---|---|---|---|
| Device time (fixed-id) | `int64` | `device_offset_ps` (147), `device_duration_ps` (148), `self_duration_ps` | `TpuXLineBuilder::AddEvent(GtcSpan)` |
| Sync / control | `ref` / `int64` | wait-reason (ref), `sync_flag_id`, `overlay_id`, `step_id` | sync/overlay/step subscribers |
| Memory / DMA | `uint64` | byte count, `bytes_transferred`, `bytes_accessed`, `queue_id` | DMA subscriber, OCI/HDE bands |
| Counter / power | `double` / `bytes` | `"Available Count"`, SPI power(W), `PowerComponentMetrics` | counter lines, firmware/SPI subscribers |
| HLO / XLA-op | `string` | `hlo_op`, `hlo_module`, `program_id`, `flops`, `tensor_shapes` | HLO subscriber + symbolizer |
| Identity / host | `int64` / `string` | `device_id`, `core_id`, `chip_id`, `run_id`, `correlation_id` | host TraceMe + device CoreId |
| Provenance | `string` | build-changelist (StatType 166) | `AddPluginMetadata()` |

> **NOTE —** every name above is confirmed present as ASCII in `libtpu.so`. The value arm per name is fixed by the tsl `StatType` schema (a name has one arm by convention), so the arm column is HIGH confidence even where the per-family producer is only MEDIUM/LOW. The *integer ids* of the canonical tsl names are **not** recoverable from strings — they are assigned at build time by the tsl enum and are documented by name + arm + producer, not by fixed integer, exactly as on [XEvent Metadata IDs](xevent-metadata-ids.md). Only `device_offset_ps` (147), `device_duration_ps` (148), and the provenance stat (166) have confirmed integers.

---

## Relevant Struct Offsets

```text
XStatMetadata (proto2)  +0x00  id (field 1)        — the plane-local stat id
                        (name field 2, description field 3 follow)
                        consumers read id at metadata_ptr + 0x28 when stamping XStat

XStat (proto2)          +0x18  metadata_id  (= XStatMetadata + 0x28)
                        +0x20  value (union storage)
                        +0x28  oneof discriminant (2 uint64 / 3 int64 / 7 ref / double)
                               (in-memory discriminant; NOT the proto field number)

TpuXLineBuilder         +0x38  device_offset_ps   XStatMetadata*  (StatType 147)
                        +0x40  device_duration_ps XStatMetadata*  (StatType 148)
                               AddEvent(GtcSpan) gates the pair on both being non-null
```

---

## Related Components

| Component | Relationship |
|---|---|
| `XStatsBuilder<XEvent>::AddStatValue<T>` | the generic add path; `T` selects the value arm |
| `XStatsBuilder<XEvent>::CopyStatValue` | re-interns + rewrites `metadata_id` during plane merge |
| `XPlaneBuilder::GetOrCreateStatMetadata(string_view)` | the per-plane name→id intern entry |
| `xprof::TpuXPlaneBuilder::GetOrCreateStatMetadata<string_view>` | the TPU wrapper that interns on the device plane |
| `GetStatTypeStrMap()::stat_type_str_map` | the canonical tsl `StatType` name table |
| `AddPluginMetadata()` | stamps the StatType-166 build-provenance stat |

---

## Cross-References

- [Profiling and Telemetry Overview](overview.md) — the five-stage capture pipeline these stats annotate
- [XEvent Metadata IDs](xevent-metadata-ids.md) — the *event* dictionary; stats annotate events, the two maps are disjoint
- [TraceEntry to XEvent](trace-entry-to-xevent.md) — the payload-scalar → `XStat` emission, the universal `device_*_ps` stamp, and the in-memory oneof discriminants
- [XPlaneBuilder / XStat / TraceMe](xplane-xstat-traceme.md) — the builder primitives (`AddStatValue`, `GetOrCreateStatMetadata`) this catalog drives
- [TracePoints Master Registry](tracepoints-master-registry.md) — the `trace_point_id` enum whose events these stats hang off
