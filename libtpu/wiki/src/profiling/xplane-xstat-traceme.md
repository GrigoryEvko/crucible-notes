# XPlane / XStat / TraceMe Emission

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

libtpu ships a full, in-binary copy of the TensorFlow/TSL profiler emission stack — not merely the `XSpace`/`XPlane`/`XLine`/`XEvent`/`XStat` proto2 message classes, but the *builder* layer (`tsl::profiler::XPlaneBuilder`, `XLineBuilder`, `XEventBuilder`, `XStatBuilder`) that every producer in the library uses to populate them. A device trace-entry decoded from a hardware ring buffer and a host `TraceMe` scope opened on a runtime thread both terminate in the *same* builder calls writing into the *same* `XSpace`. This page owns that object model and that builder API: the four-level event tree, the two per-plane interned metadata dictionaries, the `GetOrCreateLine`/`AddEvent`/`AddStat` construction surface, and the host-side `TraceMe`/`TraceMeEncode` instrumentation that produces the `/host:*` lines.

The model maps onto a frame any LLVM/XLA engineer already owns. Think of an `XSpace` as a profiling *module*, each `XPlane` a *function* (one device core or one host thread-group) with its own private symbol tables, each `XLine` a *basic-block timeline*, each `XEvent` an *instruction* with a duration, and each `XStat` an *operand annotation*. The two symbol tables — `XEventMetadata` and `XStatMetadata` — are **interned**: an event stores only a 64-bit `metadata_id` foreign key, and the human name, category, and source location live exactly once per plane. This is the single most important structural fact: producers and consumers must agree on the id↔name dictionary, which is why the id enumerations are split onto their own pages ([XEvent Metadata IDs](xevent-metadata-ids.md), [XStat Metadata IDs](xstat-metadata-ids.md)) and never duplicated here.

Two builder dialects coexist. The generic `tsl::profiler` builders (`XPlaneBuilder::GetOrCreateLine` @ `0x1cf4d9a0`, `XLineBuilder::AddEvent` @ `0x1cf4dc40`) are the upstream-identical core. libtpu wraps them in a TPU dialect (`xprof::TpuXPlaneBuilder`, `xprof::TpuXLineBuilder::AddEvent` @ `0xf1df1e0`) that adds device-semantic line keys (`TpuComponent`, `IciPort`, `GtcSpan`) and HLO-aware metadata. The device path (trace-entry → `TpuXLineBuilder::AddEvent`) is owned by [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md); this page documents the generic builders that dialect sits on, and the wholly separate host `TraceMe` path that folds into the same `XSpace`.

For reimplementation, the contract this page reconstructs is:

- **The four-level object model** — `XSpace` ⊃ `XPlane` ⊃ `XLine` ⊃ `XEvent` ⊃ `XStat`, with two interned metadata maps per `XPlane`, and the proto2 field layout each level requires.
- **The builder API** — `GetOrCreateLine`, `GetOrCreate{Event,Stat}Metadata` (id-keyed and name-keyed), `XLineBuilder::AddEvent`, and the `XEventBuilder`/`XStatBuilder` mutators — including the exact proto offsets they write.
- **The metadata interning protocol** — `proto2::Map<int64, XEventMetadata>` / `Map<int64, XStatMetadata>`, the SOO-cached `int64→XLine*` line map, and why ids are allocated per-plane.
- **The host `TraceMe` path** — thread-local lock-free event capture (`TraceMeRecorder`), the `name#k1=v1,k2=v2#` `TraceMeEncode` wire format, the `(thread<<32)|counter` activity-id scheme, and the `ConvertCompleteEventsToXPlane` host-line bridge.

| | |
|---|---|
| **Object model** | `tensorflow.profiler.XSpace` ⊃ `XPlane` ⊃ `XLine` ⊃ `XEvent` ⊃ `XStat` (proto2) |
| **Generic builders** | `tsl::profiler::{XPlaneBuilder, XLineBuilder, XEventBuilder, XStatBuilder}` |
| **`GetOrCreateLine(int64)`** | `XPlaneBuilder::GetOrCreateLine` @ `0x1cf4d9a0` |
| **`AddEvent(XEventMetadata&)`** | `XLineBuilder::AddEvent` @ `0x1cf4dc40` |
| **Event-metadata intern (by id)** | `XPlaneBuilder::GetOrCreateEventMetadata(int64)` @ `0x1cf4cfe0` |
| **Stat-metadata intern (by id)** | `XPlaneBuilder::GetOrCreateStatMetadata(int64)` @ `0x1cf4d500` |
| **Event-metadata intern (by name)** | `GetOrCreateEventMetadata(string_view)` @ `0x1cf4d0c0` |
| **TPU device dialect** | `xprof::TpuXLineBuilder::AddEvent(GtcSpan, XEventMetadata&)` @ `0xf1df1e0` |
| **Host capture** | `tsl::profiler::TraceMeRecorder::Record` @ `0x207ff580` |
| **Host encode** | `tensorflow::profiler::TraceMeEncode` @ `0x10885f20` |
| **Host → XPlane bridge** | `ConvertCompleteEventsToXPlane` @ `0xf32ff00` |

---

## The XSpace Object Model

### Purpose

`XSpace` is the root container for one profiling session and the *only* object that leaves libtpu through the [PJRT Profiler extension](../pjrt/ext-profiler.md) — a serialized `XSpace` blob. Everything below it is structure the producer builds up before serialization. The model is upstream-identical proto2; libtpu's contribution is the TPU-specific event/stat *ids* that populate it, not the schema.

### Level layout

```text
XSpace
 ├─ repeated XPlane planes          // one per device core + one per host thread-group
 ├─ repeated string errors
 ├─ repeated string warnings
 └─ repeated string hostnames

XPlane
 ├─ int64  id                       // plane id (e.g. device:TPU:0, host:0)
 ├─ string name                     // "/device:TPU:0", "/host:0", …
 ├─ repeated XLine                 lines
 ├─ map<int64, XEventMetadata>     event_metadata    // interned event dictionary
 ├─ map<int64, XStatMetadata>      stat_metadata     // interned stat dictionary
 └─ map<int64, string>            (string interning, optional)

XLine
 ├─ int64  id                       // timeline id within the plane
 ├─ int64  display_id
 ├─ string name / display_name
 ├─ int64  timestamp_ns             // line origin (absolute wall clock)
 ├─ int64  duration_ps              // line extent
 └─ repeated XEvent events

XEvent
 ├─ int64  metadata_id              // FK → XPlane.event_metadata
 ├─ int64  offset_ps                // start, relative to XLine.timestamp_ns
 ├─ int64  duration_ps
 ├─ int64  num_occurrences
 └─ repeated XStat stats

XStat
 ├─ int64  metadata_id              // FK → XPlane.stat_metadata
 └─ oneof  value { int64 | uint64 | double | bytes str_value | int64 ref_value }
```

> **NOTE —** times live in two scales. `XLine.timestamp_ns` is an absolute wall-clock origin in **nanoseconds**; `XEvent.offset_ps`/`duration_ps` are **picoseconds** *relative to that origin*. A device cycle counter is neither — it is converted to a ps offset by `TpuXLineBuilder` using the per-gen clock period before it ever reaches an `XEvent`. Conflating these scales is the classic mis-timing bug; see the timestamp note on [profiling overview](overview.md).

### Why interning

The two `map<int64, *Metadata>` fields on each `XPlane` are the dictionaries an `XEvent`/`XStat` keys into. A `TpuExecute` event repeated ten thousand times stores ten thousand 8-byte `metadata_id` values and *one* `XEventMetadata{name:"TpuExecute", …}`. The interning is per-plane, not global: the same logical event on `/device:TPU:0` and `/device:TPU:1` gets a freshly-allocated id in each plane's map. This is why a consumer must read `XPlane.event_metadata` to resolve any `XEvent.metadata_id` — there is no cross-plane id namespace. The id catalogs that producers and consumers share are on [XEvent Metadata IDs](xevent-metadata-ids.md) and [XStat Metadata IDs](xstat-metadata-ids.md).

---

## The XPlaneBuilder API

### Purpose

`XPlaneBuilder` is the mutation handle on a single `XPlane`. It owns three responsibilities: hand out (or create) an `XLine` by id, intern an `XEventMetadata`/`XStatMetadata` by id or by name, and cache the lookups so repeated calls in a hot decode loop do not re-scan the proto maps. Its in-binary instances are confirmed by the `tsl::profiler::XPlaneBuilder` symbol at the cited addresses — the model is *instantiated* in libtpu, not merely declared.

### Object layout

From the decompiled builders, an `XPlaneBuilder` holds the backing `XPlane*` and three side caches:

| Field | Offset (qword index) | Type | Meaning | Confidence |
|---|---|---|---|---|
| plane backing | `+0x10` (`*((q*)this+2)`) | `XPlane*` | the proto being built; all `RepeatedPtrFieldBase::Add` targets resolve from here | CERTAIN |
| line map | `+0x68` (`this+104`) | `absl flat_hash_map<int64, XLine*>` (SOO) | id → line cache; small-object-optimized for ≤1 entry | CERTAIN |
| SOO single-slot key | `+0x70`/`+0x78` (`this+14/15`) | `int64` | inline last-line id/ptr before the map grows past SOO | HIGH |
| event-metadata cache | `+0x80` (`this+16`) | hash-set storage | backing for the metadata lookups | MEDIUM |

> **QUIRK —** `GetOrCreateLine` (@ `0x1cf4d9a0`) does *not* go straight to a hash map. For the first line it stores the id inline in two builder qwords (`this+14`, `this+15`) guarded by a thread-local fast-path counter (`__tls_get_addr(&qword_22048D78)`), and only calls `GrowSooTableToNextCapacityAndPrepareInsert` once a second distinct line id appears. A reimplementation that allocates a full `flat_hash_map` per plane up front matches the *semantics* but not the layout, and will read the wrong offsets if it tries to cross-check against the binary.

### `GetOrCreateLine(int64 id)` — @ `0x1cf4d9a0`

```c
function GetOrCreateLine(builder, id):                  // 0x1cf4d9a0
    // Fast path: SOO single-slot or grown flat_hash_map keyed by id
    if line_map_size > 1:
        slot = find_or_prepare_insert_large(line_map, id)   // absl raw_hash_set
    else:
        // inline single-entry cache in builder+14/+15, gated by a
        // thread-local fast-path counter to avoid hashing the common case
        slot = soo_single_slot(builder, id)
    if slot.value != null:                              // already created
        return slot.value
    // Miss: append a fresh XLine to XPlane.lines and back-fill id
    plane = builder->plane                              // *(builder+2)
    xline = RepeatedPtrFieldBase::Add<XLine>(plane+24)  // proto repeated field
    plane->_has_bits |= 1                               // +0x10 |= 1
    slot.value      = xline
    *(xline + 56)   = id                                // XLine.id  @ +0x38
    xline->_has_bits |= 8                               // mark id present
    return xline
```

The contract: `GetOrCreateLine` is idempotent on `id` within a plane, returns a stable `XLine*`, and lazily appends to `XPlane.lines` only on first request. The TPU dialect's `TpuXPlaneBuilder::GetOrCreateLine(TpuComponent)` @ `0xf1df120` and `GetOrCreateLine(IciPort)` @ `0xf25af20` are thin wrappers that map a device-semantic key to an `int64` line id, then call this.

### Metadata interning — by id

`GetOrCreateEventMetadata(int64)` @ `0x1cf4cfe0` and `GetOrCreateStatMetadata(int64)` @ `0x1cf4d500` are the interning primitives. Both follow the identical shape — only the target map and the back-filled field offset differ:

```c
function GetOrCreateEventMetadata(builder, id):         // 0x1cf4cfe0
    plane = builder->plane                              // *(builder+2)
    plane->_has_bits |= 0x10                             // mark event_metadata present
    SyncMapWithRepeatedField(plane->event_metadata)      // proto2 map<->repeated sync
    md = Map<int64, XEventMetadata>::TryEmplace(         // insert-or-find by id
             plane->event_metadata_map, id)
    md->id = id                                          // XEventMetadata.id @ +0x68
    md->_has_bits |= 0x20
    return md                                            // caller fills name/category

function GetOrCreateStatMetadata(builder, id):          // 0x1cf4d500
    plane->_has_bits |= 0x20                             // mark stat_metadata present
    SyncMapWithRepeatedField(plane->stat_metadata)
    md = Map<int64, XStatMetadata>::TryEmplace(plane->stat_metadata_map, id)
    md->id = id                                          // XStatMetadata.id @ +0x38
    md->_has_bits |= 4
    return md
```

> **NOTE —** the back-filled offsets differ because the two metadata messages have different layouts: `XEventMetadata.id` lands at proto offset `+0x68` (the message also carries `name`, `display_name`, `metadata` bytes, and child stats), while `XStatMetadata.id` lands at `+0x38` (it carries only `id`, `name`, `description`). Both functions return a *pointer into the proto map node*, so the caller mutates the interned message in place — there is no copy-back step.

### Metadata interning — by name

The name-keyed overloads (`GetOrCreateEventMetadata(string_view)` @ `0x1cf4d0c0`, plus `string`/`&&` variants at `0x1cf4d380`/`0x1cf4d0c0`, and the stat equivalents at `0x1cf4d5e0`/`0x1cf4d8a0`) maintain a *second* index: a name→id hash so two producers naming the same event get the same interned entry. They allocate a fresh id when the name is new, create the metadata via the id path above, and set its `name` field. The read-only companions `GetEventMetadata(string_view)` @ `0x1cf4d480` and `GetStatMetadata(*)` @ `0x1cf4d4c0`/`0x1cf4d560` look up without creating — used by consumers that must find an existing id.

| Builder method | Key | Address | Creates? | Confidence |
|---|---|---|---|---|
| `GetOrCreateEventMetadata(int64)` | id | `0x1cf4cfe0` | yes | CERTAIN |
| `GetOrCreateEventMetadata(string_view)` | name | `0x1cf4d0c0` | yes | CERTAIN |
| `GetOrCreateEventMetadata(string&&)` | name | `0x1cf4d380` | yes | HIGH |
| `GetEventMetadata(string_view)` | name | `0x1cf4d480` | no (lookup) | CERTAIN |
| `GetOrCreateStatMetadata(int64)` | id | `0x1cf4d500` | yes | CERTAIN |
| `GetOrCreateStatMetadata(string_view)` | name | `0x1cf4d5e0` | yes | CERTAIN |
| `GetOrCreateStatMetadata(string&&)` | name | `0x1cf4d8a0` | yes | HIGH |
| `GetStatMetadata(int64)` | id | `0x1cf4d560` | no (lookup) | CERTAIN |
| `GetStatMetadata(string_view)` | name | `0x1cf4d4c0` | no (lookup) | CERTAIN |
| `CreateEventMetadata()` | — | `0x1cf4d040` | yes (no key) | HIGH |
| `GetOrCreateCounterLine()` | — | `0x1cf4db20` | yes | HIGH |

---

## The XLineBuilder / XEventBuilder API

### Purpose

`XLineBuilder` is the mutation handle on one `XLine`; its single hot method `AddEvent` appends an `XEvent` keyed by an already-interned `XEventMetadata` and returns an `XEventBuilder` for filling the event's timing and stats. `XStatBuilder` analogously fills one `XStat`'s value.

### `XLineBuilder::AddEvent(const XEventMetadata&)` — @ `0x1cf4dc40`

```c
function XLineBuilder::AddEvent(line_builder, md):      // 0x1cf4dc40
    xline = line_builder->line                          // *(md) → line backing via builder
    arena = xline->arena                                // *(line+8), low-bit tagged
    xevent = RepeatedPtrFieldBase::Add<XEvent>(xline+24) // append to XLine.events
    xline->_has_bits |= 1
    xevent->metadata_id = md->id                        // *(xevent+40) = *(md+88)  → XEvent.metadata_id @ +0x28
    xevent->_has_bits |= 2
    // returns an XEventBuilder { line_builder copy, xevent* } in `this`
    return XEventBuilder{ line=line_builder.line, event=xevent }
```

The function is dense but unambiguous: it allocates an `XEvent` on the line's repeated field, copies the metadata id from the interned `XEventMetadata` (`*(md+88)` → `XEvent.metadata_id` at `+0x28`), and returns an `XEventBuilder` value (the `vpermilps`/`vmovups` tail packs the line backing and the new event pointer into the returned builder). The caller then sets timing and stats on that returned builder.

> **GOTCHA —** `AddEvent` takes the `XEventMetadata&`, **not** an id integer. The metadata *must* already be interned in this plane (via a `GetOrCreate*Metadata` call on the parent `XPlaneBuilder`) before `AddEvent` — the function reads `*(md+88)` to copy the id and does no interning itself. Calling `AddEvent` with a metadata object belonging to a different plane copies a foreign id and silently mis-keys the event.

### XEventBuilder mutators

After `AddEvent`, the `XEventBuilder` carries `{line*, event*}` and exposes inline setters that write directly into the `XEvent` proto:

| Setter (logical) | Writes | XEvent offset | Confidence |
|---|---|---|---|
| `SetOffsetPs` / `SetTimestampNs(line_ns)` | `offset_ps` (relative to line origin) | `+0x18` | HIGH |
| `SetDurationPs` / `SetEndTimestampPs` | `duration_ps` | `+0x20` | HIGH |
| `SetNumOccurrences` | `num_occurrences` | `+0x30` | MEDIUM |
| `AddStatValue(XStatMetadata&, value)` | appends `XStat` to `XEvent.stats` | repeated `+0x38` | HIGH |

`AddStatValue` is the `XEvent`-level analogue of `AddEvent`: it appends an `XStat` to the event's repeated `stats` field, sets `XStat.metadata_id` from the passed (already-interned) `XStatMetadata`, and writes the value into the `oneof`. The value variants observed in the proto schema are `int64`/`uint64`/`double`/`bytes (str_value)`/`int64 (ref_value)` — `ref_value` references an interned string in the plane's string table, used for large/repeated string stats.

### The TPU device dialect

The device path does not call the generic `AddEvent` directly; it calls `xprof::TpuXLineBuilder::AddEvent(GtcSpan, const XEventMetadata&)` @ `0xf1df1e0`, which carries the device timing in a `GtcSpan` (a global-time-counter start/end pair) and delegates to the generic builder. The templated instantiations `TpuXLineBuilder::AddEvent<…TraceEntry…>` (one per chip family: pxc @ `0xf1f26e0`, vfc, vlc, glc, gfc, jxc) are the exact join points where a decoded `TraceEntry` becomes an `XEvent` — each is the device counterpart of the host bridge below. Those translations (cycle→ps conversion, which scalars become which `XStat`s) are owned by [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md); the codec feeding them is on [TraceEntriesCoder](trace-entries-coder.md).

---

## How a Device Trace-Entry Becomes an XEvent

The five-stage capture pipeline (overview) ends in this object model. Stitching the two together, a single decoded `TraceEntry` lands on a device `XLine` as follows:

```text
TraceEntry (decoded proto, oneof variant = the event)
   │
   ├─ trace_point_id  ──▶ map to enum name string
   │        │
   │        └─▶ XPlaneBuilder::GetOrCreateEventMetadata(name)   // intern → metadata_id
   │
   ├─ TpuXPlaneBuilder::GetOrCreateLine(TpuComponent/IciPort)   // 0xf1df120 / 0xf25af20
   │        └─▶ XPlaneBuilder::GetOrCreateLine(int64)           // 0x1cf4d9a0
   │
   ├─ TpuXLineBuilder::AddEvent(GtcSpan, XEventMetadata&)       // 0xf1df1e0
   │        └─▶ XLineBuilder::AddEvent(XEventMetadata&)         // 0x1cf4dc40 → XEvent
   │
   ├─ timestamp (device cycle counter) ──▶ ÷ clock period ──▶ XEvent.offset_ps / duration_ps
   │
   └─ variant scalars (transaction_id, core_id, byte counts, …)
            └─▶ GetOrCreateStatMetadata(name) + AddStatValue(...) ──▶ XStat[]
```

The key observation a reimplementer must internalize: the device `trace_point_id` (a banded hardware enum, gappy, family-specific) is **never** stored in the `XEvent`. It is translated to a *name string* in stage 5, that name is interned via `GetOrCreateEventMetadata`, and the `XEvent.metadata_id` is the *plane-local* interning index — a different number entirely. The full translation table lives on [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md).

---

## The Host TraceMe Path

### Purpose

Host events — `TpuCompile`, `TpuExecute`, queue submission, megascale transport, allocator activity — do not come from hardware ring buffers. They come from `tsl::profiler::TraceMe` RAII scopes opened on TPU-runtime threads. The `TraceMe` subsystem captures these lock-free per thread, then a converter folds the captured scopes into `/host:*` `XLine`s of the same `XSpace` the device path writes. This is the second of the "two sources, one `XSpace`" halves named on the [profiling overview](overview.md).

### Capture — `TraceMeRecorder`

A `TraceMe` scope, on construction, records a start event; on destruction, an end event — each via `TraceMeRecorder::Record` @ `0x207ff580`. `Record` is deliberately lock-free: it writes into a **thread-local** chunked event queue (`PerThread<ThreadLocalRecorder>`), so producing a host event never contends a global lock.

```c
function TraceMeRecorder::Record(event):                // 0x207ff580
    tls = PerThread<ThreadLocalRecorder>::Get()         // lazily inited per thread
    queue = tls->active_chunk                            // *(tls+48)
    n     = tls->count                                   // *(tls+56)
    slot  = queue + 16 + 40 * (n - queue->base)          // 40-byte Event record
    *slot = move(event)                                  // {activity_id, name, ts, …}
    if (n + 1) - queue->base == 1638:                    // chunk full (1638 events)
        next = new chunk(0x10000)                        // 64 KiB, zeroed
        next->base = n + 1
        queue->next = next
        tls->active_chunk = next                         // append-only linked chunks
    tls->count = n + 1
```

> **QUIRK —** the chunk holds exactly **1638** events before it links a fresh 64 KiB chunk. That is `(0x10000 − 16) / 40 ≈ 1638` — the chunk is a 65536-byte block, 16 bytes of header (`base` index + `next` pointer), the rest a flat array of 40-byte `Event` records. A reimplementation that sizes the chunk by event count rather than by a fixed 64 KiB block will diverge on the boundary and mis-stitch the start/end pairing that `Consume` relies on.

### Activity ids — `NewActivityId`

Start and end events are paired by a 64-bit *activity id* from `TraceMeRecorder::NewActivityId` @ `0x207ff7a0`:

```c
function NewActivityId():                               // 0x207ff7a0
    if !tls.thread_id_set:                               // first call this thread
        tls.thread_id = atomic_fetch_add(&thread_counter, 1)   // global thread index
        tls.thread_id_set = true
    id = (uint64(tls.thread_id) << 32) | tls.local_counter      // pack
    tls.local_counter += 1
    return id
```

The id packs a globally-unique 32-bit thread index in the high half and a per-thread monotonic counter in the low half: `(thread_index << 32) | per_thread_seq`. This guarantees global uniqueness without any cross-thread synchronization on the hot path — the only atomic is the *once-per-thread* `thread_counter` increment. The consumer uses this id to match a `TraceMeProducer` scope to its `TraceMeConsumer` (the `ContextType`/flow-event linkage seen in the many `TraceMe::AppendMetadata` instantiations).

### Encode — `TraceMeEncode`

A `TraceMe` whose name carries key/value metadata uses `tensorflow::profiler::TraceMeEncode` @ `0x10885f20` to serialize the args into the *event name string itself*, in the canonical TraceMe metadata wire format:

```text
name#key1=value1,key2=value2,…#
```

`TraceMeEncode(name, {TraceMeArg…})` builds a single string: the base `name`, then `#`, then comma-separated `key=value` pairs, then a closing `#`. The decompile shows it `strlen`s the name, sizes the output buffer to `name_len + Σ(key_len + value_len) + (2·argc + 1)` (the `#`, `,`, `=`, `#` punctuation), heap-allocates if it overruns the 22-byte SSO buffer, and appends each `TraceMeArg`. Numeric args are stringified inline. The consumer side parses the `#…#` suffix back into `XStat`s — so a TraceMe arg becomes an `XStat` on the host `XEvent`, exactly mirroring how a device variant scalar becomes an `XStat`.

> **NOTE —** `TraceMeEncode` is the *only* metadata channel for host events: there is no separate stat-id field at capture time. The key/value pairs ride inside the name string until conversion, where the parser interns each key as an `XStatMetadata` and each value as the `XStat` value. This is why host stat names are dynamic strings (interned by name) while device stat ids are a fixed enum (interned by id) — the two halves intern through different overloads of the same `GetOrCreateStatMetadata`.

### Host → XPlane bridge — `ConvertCompleteEventsToXPlane`

At collection time, the host sub-profiler (`HostTracer`) calls `TraceMeRecorder::Consume` @ `0x207fe700` to drain every thread's chunk queue into a vector of `ThreadEvents`, then `tsl::profiler::ConvertCompleteEventsToXPlane` @ `0xf32ff00` folds them into one `XPlane` (`/host:0`). This converter is the host analogue of the device `TpuXLineBuilder::AddEvent` path: the decompile shows it walking the captured events, calling `GetOrCreateEventMetadata` for each event name, `RepeatedPtrFieldBase::Add<XEvent>` to append, and `GetOrCreateStatMetadata` + an `XStat` append for each decoded metadata pair — the same builder primitives documented above, driven from host capture instead of a device codec.

```text
TraceMeRecorder (per-thread lock-free chunks)
   │  Stop() → Consume() @ 0x207fe700  → vector<ThreadEvents>
   ▼
ConvertCompleteEventsToXPlane @ 0xf32ff00
   │  per event:
   │    name        ──▶ GetOrCreateEventMetadata(name)  → XEvent.metadata_id
   │    start/end   ──▶ XEvent.offset_ps / duration_ps  (line origin = capture start)
   │    "#k=v#" args──▶ GetOrCreateStatMetadata(k) + AddStatValue(v) → XStat[]
   │    one XLine per producing thread (line id = thread id)
   ▼
   /host:0 XPlane  ── merged into the same XSpace as the device planes
```

The result: host `TraceMe` scopes and device hardware trace-entries arrive on different planes (`/host:N` vs `/device:TPU:N`) of one `XSpace`, built by the identical `XPlaneBuilder`/`XLineBuilder` API, distinguished only by their metadata dictionaries and their line-key conventions.

---

## Builder Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `XPlaneBuilder::GetOrCreateLine(int64)` | `0x1cf4d9a0` | id → `XLine`, SOO-cached, lazy append | CERTAIN |
| `XPlaneBuilder::GetOrCreateEventMetadata(int64)` | `0x1cf4cfe0` | intern event metadata by id | CERTAIN |
| `XPlaneBuilder::GetOrCreateEventMetadata(string_view)` | `0x1cf4d0c0` | intern event metadata by name | CERTAIN |
| `XPlaneBuilder::GetOrCreateStatMetadata(int64)` | `0x1cf4d500` | intern stat metadata by id | CERTAIN |
| `XPlaneBuilder::GetOrCreateStatMetadata(string_view)` | `0x1cf4d5e0` | intern stat metadata by name | CERTAIN |
| `XPlaneBuilder::CreateEventMetadata()` | `0x1cf4d040` | allocate metadata without a key | HIGH |
| `XPlaneBuilder::GetOrCreateCounterLine()` | `0x1cf4db20` | dedicated counter timeline | HIGH |
| `XLineBuilder::AddEvent(const XEventMetadata&)` | `0x1cf4dc40` | append `XEvent`, copy metadata id | CERTAIN |
| `TpuXLineBuilder::AddEvent(GtcSpan, XEventMetadata&)` | `0xf1df1e0` | device dialect wrapper | CERTAIN |
| `TpuXPlaneBuilder::GetOrCreateLine(TpuComponent)` | `0xf1df120` | device-semantic line key | CERTAIN |
| `TpuXPlaneBuilder::GetOrCreateLine(IciPort)` | `0xf25af20` | ICI-port line key | CERTAIN |
| `TraceMeRecorder::Record(Event&&)` | `0x207ff580` | lock-free per-thread capture | CERTAIN |
| `TraceMeRecorder::NewActivityId()` | `0x207ff7a0` | `(thread<<32)\|counter` id | CERTAIN |
| `TraceMeRecorder::Consume()` | `0x207fe700` | drain per-thread chunks | CERTAIN |
| `TraceMeEncode(name, args)` | `0x10885f20` | `name#k=v,…#` wire format | CERTAIN |
| `ConvertCompleteEventsToXPlane(...)` | `0xf32ff00` | host events → `/host` XPlane | CERTAIN |

---

## Related Components

| Component | Relationship |
|---|---|
| `tsl::profiler::ProfilerCollection` | owns the host (`HostTracer`) and device (`TpuProfilerImpl`) sub-profilers that drive these builders |
| `xprof::TpuXLineBuilder` (templated) | device-side `AddEvent<TraceEntry>` join point; the device counterpart of `ConvertCompleteEventsToXPlane` |
| PJRT Profiler extension | serializes the finished `XSpace` out of the library |

---

## Cross-References

- [Profiling and Telemetry Overview](overview.md) — the five-stage pipeline and the two-source (`/host` + `/device`) `XSpace` this page's builders construct
- [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md) — the device-side translation that calls `TpuXLineBuilder::AddEvent` and the cycle→ps conversion
- [TraceEntriesCoder](trace-entries-coder.md) — the device codec that produces the `TraceEntry` protos this model consumes
- [XEvent Metadata IDs](xevent-metadata-ids.md) — the interned event-id dictionary that `GetOrCreateEventMetadata` keys into
- [XStat Metadata IDs](xstat-metadata-ids.md) — the interned stat-id dictionary that `GetOrCreateStatMetadata` keys into
- [Task Proto](task-proto.md) — the surrounding task/session descriptor framing a profiling run
- [PJRT Profiler Extension](../pjrt/ext-profiler.md) — the C-ABI that serializes the assembled `XSpace` to the client
