# TraceEntry to XEvent/XStat

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary ships full C++ symbols; `.text`/`.rodata` VMA == file offset. Other versions will differ.*

## Abstract

This page documents the **last hop** of the device profiler: the subscriber stack that turns a decoded proto2 `TraceEntry` — one per 16-byte hardware packet, produced by [TraceEntriesCoder](trace-entries-coder.md) — into an `XEvent` on a device `XLine`, with `XStat` annotations. The [XPlaneBuilder API](xplane-xstat-traceme.md) page owns the *builder primitives* (`GetOrCreateLine`, `GetOrCreate*Metadata`, `AddEvent`, `AddStatValue`); this page owns everything that *drives* those primitives from a decoded entry: the dispatcher fan-out, the per-subscriber `ProcessTraceEntry` visitor, the two XEvent-naming regimes, the payload-scalar → `XStat` mapping, the begin/end pairing of duration events, and the per-band `XLine` assignment.

The shape maps cleanly onto a frame an LLVM/MLIR engineer already owns. Think of the decoded `RepeatedPtrField<TraceEntry>` as an instruction stream, the `CoreDispatcher` as a pass-manager that routes each "instruction" by opcode (`trace_point_id`) to every *pass* (subscriber) that registered interest, and each subscriber's `ProcessTraceEntry` as a one-shot lowering that emits a device-plane `XEvent`. Two facts make this non-trivial and are the reason the step needs its own page. First, the wire `trace_point_id` is **never** stored in the `XEvent` — it is translated to a *name string* (by one of two regimes) and interned per-plane into an `XEventMetadata`, so the `XEvent.metadata_id` is a plane-local intern slot, a different integer entirely. Second, several event kinds are **stateful**: a wait or a DMA is two wire packets (begin + end) that a per-core *Tracker* pairs by a match key into a *single duration `XEvent`* — the `XEvent` does not exist until the closing packet arrives.

The translation has one universal spine and one per-subscriber variable part. The spine is `xprof::TpuXLineBuilder::AddEvent(GtcSpan, XEventMetadata&)` @ `0xf1df1e0`: it always converts the GTC start/duration to picoseconds, stamps `XEvent.offset_ps`/`duration_ps`, **and** appends two redundant `int64` `XStat`s — `device_offset_ps` (StatType 147) and `device_duration_ps` (StatType 148) — whose metadata pointers the line builder caches at construction. The variable part is each subscriber choosing a line (a `TpuComponent` ordinal), a name (raw decimal-string or a human/HLO name), and zero or more domain `XStat`s (a sync wait reason, a DMA byte count, a counter value). This page reconstructs both.

For reimplementation, the contract this page reconstructs is:

- **The dispatch fan-out** — `CoreDispatcher::Dispatch` reads `TraceHeader.trace_point_id` and routes through a `FlatHashMap<id, vector<subscriber*>>` to every registered subscriber's virtual `ProcessTraceEntry`; one wire id can fan out to several subscribers and produce several `XEvent`s.
- **The two naming regimes** — the *raw* path names the `XEvent` by the decimal string of the `trace_point_id`; the *semantic* path computes a human name (`SyncWait:<n>`, an HLO op via the symbolizer, a fixed counter-line label).
- **The universal duration+stat stamp** — `TpuXLineBuilder::AddEvent(GtcSpan)` GTC→ps math and the always-present `device_offset_ps`/`device_duration_ps` `XStat` pair, plus per-subscriber dynamic stats added on the returned `XEventBuilder`.
- **The begin/end pairing** — `SyncTracker` (key = `sync_flag_number`) and `DmaSubscriber` (key = `dma_id`) as per-core state machines that fold two packets into one duration span.
- **The line assignment** — every device `XLine` id is a `TpuComponent` ordinal, named by the 149-entry `TpuComponentName` table; which subscriber owns which line is fixed in code.

| | |
|---|---|
| **Pipeline entry** | `ConvertTpuTraceToXPlaneV2<TraceEntry>` (pxc `0xf1d4360`, +5 families) |
| **Dispatcher** | `CoreDispatcher<TraceEntry>::Dispatch` @ `0xf1ed280` |
| **Registration** | `CoreDispatcher::RegisterSubscriber` @ `0xf1ecee0` |
| **Universal stamp** | `xprof::TpuXLineBuilder::AddEvent(GtcSpan, XEventMetadata&)` @ `0xf1df1e0` |
| **Raw naming** | `TpuXLineBuilder::AddEvent<…ProcessTraceEntry lambda>` (pxc `0xf1f26e0`, +5) |
| **GTC→ps converter** | `GtcSpanConverter::TimespanFromGtcSpan` @ `0xf2cb7e0` |
| **Line key** | `TpuXPlaneBuilder::GetOrCreateLine(TpuComponent)` @ `0xf1df120` (line id == ordinal) |
| **Line-name table** | `TpuComponentName` @ `0x1c8ebb60` (149-arm rel32 jump table @ `0xb43d200`) |
| **Sync pairing** | `SyncTracker::ProcessTraceEntry` @ `0xf1ef3c0`, key = `sync_flag_number` |
| **DMA pairing** | `DmaSubscriber::ProcessTraceEntry` @ `0xf1dfee0`, key = `dma_id` |
| **Universal stats** | `device_offset_ps` (147) @ `+0x38`, `device_duration_ps` (148) @ `+0x40` of `TpuXLineBuilder` |

---

## Pipeline Overview — Decode to XSpace

### Purpose

Frames where the subscriber translation sits relative to the wider device path. The codec ([TraceEntriesCoder](trace-entries-coder.md)) hands up a `RepeatedPtrField<TraceEntry>`, one entry per 16-byte hardware packet. `ConvertTpuTraceToXPlaneV2<TraceEntry>` builds one `/device:TPU:<core>` `XPlane` per `ChipCoreId`, registers a subscriber set against a `CoreDispatcher`, and replays every decoded entry through it. The dispatcher is the only routing point; everything downstream of it is per-subscriber.

### Entry Point

```text
RepeatedPtrField<TraceEntry>                       ── one entry per 16-byte packet (TraceEntriesCoder)
  └─ ConvertTpuTraceToXPlaneV2<TraceEntry>         ── pxc 0xf1d4360 (+vlc/vfc/glc/gfc/jxc)
       │  GetDeviceIdentifiers ; GetTraceCodec     ── per-ChipCoreId setup
       │  build CoreContext: TpuXPlaneBuilder (one XPlane) + subscriber set
       └─ CoreDispatcher<TraceEntry>::Dispatch     ── 0xf1ed280
            │  read TraceHeader.trace_point_id (+0x18)
            │  FlatHashMap<id, vector<subscriber*>> lookup   (built by RegisterSubscriber 0xf1ecee0)
            └─ for each matching subscriber:
                 subscriber::ProcessTraceEntry      ── virtual, call *(vtable+0x10)
                   ├─ GetOrCreateLine(TpuComponent)        ── 0xf1df120, line id == ordinal
                   ├─ name = [raw] decimal | [semantic] SyncWait:/HLO/label
                   ├─ GetOrCreateEventMetadata(name)       ── per-plane intern → metadata_id
                   └─ TpuXLineBuilder::AddEvent(GtcSpan, *meta)   ── 0xf1df1e0
                        → XEvent + {device_offset_ps, device_duration_ps} + per-event XStats
```

### Algorithm

```c
function ConvertTpuTraceToXPlaneV2(decoded_entries):     // pxc 0xf1d4360
    ids   = GetDeviceIdentifiers()                        // transport identity
    codec = GetTraceCodec(ids)                            // 0xf5a2900
    for each ChipCoreId core in ids:
        ctx = CoreContext(core)                           // owns one /device:TPU:<core> XPlane
        ctx.xplane   = TpuXPlaneBuilder(core)
        ctx.dispatcher = CoreDispatcher<TraceEntry>()
        RegisterSubscribers(ctx)                          // bind trace_point_id sets → subscribers
    for each TraceEntry e in decoded_entries:
        ctx.dispatcher.Dispatch(wrap(e))                  // 0xf1ed280
    Flush(all dispatchers)                                // drain stateful trackers' open spans

function CoreDispatcher::Dispatch(entry):                 // 0xf1ed280
    id   = entry.header.trace_point_id                    // TraceHeader+0x18
    subs = subscriber_map.find(id)                        // FlatHashMap<id, vector<subscriber*>>
    if subs == end: return                                // unregistered id → dropped
    for sub in *subs:
        sub->vtable[0x10](sub, entry)                     // virtual ProcessTraceEntry
```

> **QUIRK —** a single `trace_point_id` can be registered by *several* subscribers (a `SET_TRACEMARK`, for example, feeds the step tracker, the HLO subscriber, and the on-device TraceMe subscriber). `Dispatch` fans the one wire entry out to all of them, so one packet can become several `XEvent`s on several lines. A reimplementation that assumes one packet → one event will under-populate the timeline.

> **NOTE —** the per-family `TracePoints<TraceEntry>` registration sets — *which* ids each subscriber registers on each chip family — are read at construction from a runtime descriptor (`subscriber+0x8c`/`+0x10c` for the HLO subscriber's `SET_TRACEMARK`/`TRACE_INSTRUCTION` ids), not baked as literals. They are confirmed for pxc (`84`/`85`) at the comparison sites but were not exhaustively dumped for vfc/vlc/glc/gfc/jxc. Treat the per-family id sets in the tables below as **HIGH confidence for pxc, MEDIUM for other families**. The master id catalog is on [TracePoints Master Registry](tracepoints-master-registry.md); the metadata-id allocation on [XEvent Metadata IDs](xevent-metadata-ids.md).

---

## The Universal Stamp — `AddEvent(GtcSpan)`

### Purpose

Every device `XEvent` that carries timing is created through `xprof::TpuXLineBuilder::AddEvent(GtcSpan, const XEventMetadata&)` @ `0xf1df1e0`. This is the single chokepoint that (a) creates the `XEvent` on the line via the generic builder, (b) converts the GTC span to picoseconds, (c) stamps the line-relative `XEvent.offset_ps`/`duration_ps`, and (d) appends the two redundant absolute `device_offset_ps`/`device_duration_ps` `XStat`s. Subscribers never write timing fields directly — they hand a `GtcSpan` and an interned `XEventMetadata` to this function and let it stamp.

### Algorithm

Byte-confirmed against the decompiled body. The two cached `XStatMetadata*` live at `TpuXLineBuilder+0x38` (`device_offset_ps`, StatType 147) and `+0x40` (`device_duration_ps`, StatType 148); the per-line GTC clock object is at `+0x10`.

```c
function TpuXLineBuilder::AddEvent(this, gtc_start, gtc_dur):    // 0xf1df1e0
    XLineBuilder::AddEvent(this, meta)                  // 0x1cf4dc40 — create XEvent, copy metadata_id

    // (A) the universal device_*_ps XStat pair — only if BOTH metadata ptrs cached
    if *(this+0x38) != null && *(this+0x40) != null:    // line 30: gate
        clk16  = 16 * (**(this+0x10))                    // per-line clock << 4
        // offset: round(gtc_start * 1e9 / clk16)
        off_ps = udivti3(0x3B9ACA00 * (gtc_start & ~0xF) + clk16/2, clk16)   // 128-bit divide
        // duration: round((end - start_masked) * 1e9 / clk16)
        dur_ps = udivti3(0x3B9ACA00 * ((gtc_start+gtc_dur - (gtc_start & 0x1FFFFFFFFFF0)) & 0x1FFFFFFFFFF0)
                         + clk16/2, clk16)
        s1 = RepeatedPtrFieldBase::Add<XStat>(xevent.stats)         // line 46
        s1.metadata_id = (*(this+0x38)) + 0x28          // device_offset_ps XStatMetadata.id
        s1.oneof_case  = 3                              // int64_value (field 4)
        s1.int64_value = off_ps
        s2 = RepeatedPtrFieldBase::Add<XStat>(xevent.stats)         // line 61
        s2.metadata_id = (*(this+0x40)) + 0x28          // device_duration_ps XStatMetadata.id
        s2.oneof_case  = 3
        s2.int64_value = dur_ps

    // (B) the XEvent's own line-relative timing — always
    span = GtcSpanConverter::TimespanFromGtcSpan(*(this+0x10), gtc_start, gtc_dur)   // 0xf2cb7e0
    xevent.offset_ps   = span.offset                    // +0x38, oneof "data" case 2
    xevent.duration_ps = span.duration                  // +0x30, presence orb |= 4
    return XEventBuilder{ line, xevent }                // caller adds per-event XStats
```

> **QUIRK —** every device `XEvent` ends up with timing recorded **twice**: `XEvent.offset_ps`/`duration_ps` (line-relative, ps) *and* the `device_offset_ps`/`device_duration_ps` `XStat`s (absolute, ps). This is deliberate, not redundancy by accident. The `XEvent.offset_ps` is relative to the `XLine` origin and is rewritten if the line is later rebased during merge; the `device_*_ps` `XStat`s carry the absolute device time and survive rebasing — they are how a consumer recovers true device wall time after the converter has normalized line origins.

> **GOTCHA —** the `device_*_ps` stamp is **conditional** on both `XStatMetadata` pointers (`this+0x38`, `this+0x40`) being non-null. A `TpuXLineBuilder` constructed without those two metadata objects interned (e.g. a counter-only line) silently emits `XEvent`s with line-relative timing but *no* absolute `device_*_ps` stats. A reimplementation must intern both StatTypes 147/148 at line-builder construction or it will drop the absolute timebase on every event.

### GTC→ps math

The conversion is `picoseconds = round(gtc_ticks × 1e9 / (clock × 16))`, done in 128-bit integer arithmetic because `gtc_ticks × 1e9` overflows 64 bits. The constants are byte-confirmed: `0x3B9ACA00` is `1e9`; the multiplier `16 × clock` (`clk16`) is the per-gen GTC period scaled; the `+clk16/2` is round-to-nearest; the masks `0x1FFFFFFFFFF0` (41-bit, low nibble cleared) and `0xFFFFFFFFFFFFFFF0` clamp the tick value to the hardware counter's significant range before the multiply. `_udivti3` is the compiler's 128÷64 unsigned divide. This matches the timebase math on the transport layer — see [TraceEntriesCoder](trace-entries-coder.md) for the codec-side GTC handling.

### Function Map

| Function | Address | Role |
|---|---|---|
| `TpuXLineBuilder::AddEvent(GtcSpan, XEventMetadata&)` | `0xf1df1e0` | universal stamp: XEvent + device_*_ps stats |
| `tsl::profiler::XLineBuilder::AddEvent(XEventMetadata&)` | `0x1cf4dc40` | base: append XEvent, copy metadata_id |
| `GtcSpanConverter::TimespanFromGtcSpan` | `0xf2cb7e0` | GTC span → {offset_ps, duration_ps} |
| `_udivti3` | (libgcc) | 128÷64 unsigned divide for the ps multiply |

---

## XEvent Naming — Two Regimes

### Purpose

The wire `trace_point_id` is a banded hardware enum (gappy, family-specific) and is **never** stored in the `XEvent`. Each subscriber computes a *name string*, interns it per-plane via `GetOrCreateEventMetadata`, and the resulting plane-local `metadata_id` keys the `XEvent`. There are exactly two ways a name is produced.

### Regime (a) — Raw / Generic

The generic subscriber path (`TensorCoreHloSubscriber`/`SparseCore…` raw `AddEvent<…lambda>`, pxc @ `0xf1f26e0`, +5 families) names the `XEvent` by the **decimal string of the `trace_point_id`**. This is the codec-level fallback for any banded id with no semantic handler.

```c
function TpuXLineBuilder::AddEvent<lambda>(subscriber, gtc, trace_point_id):   // pxc 0xf1f26e0
    // per-subscriber cache: FlatHashMap<int, XEventMetadata*> at subscriber+0x18
    slot = per_id_cache[trace_point_id]                 // 0xf1e6820 operator[]
    if slot != null:
        meta = slot                                     // hit: reuse interned metadata
    else:
        buf  = FastIntToBuffer(trace_point_id)          // 0x211719e0 — e.g. 81 → "81"
        meta = XPlaneBuilder::GetOrCreateEventMetadata(move(buf))   // 0x1cf4d380, string&&
        per_id_cache[trace_point_id] = meta             // cache the ptr
    AddEvent(gtc, *meta)                                // 0xf1df1e0 universal stamp
```

> **NOTE —** the raw name is the id's *decimal text*, not the human enum name. id `81` becomes the `XEventMetadata.name` `"81"`. The human enum name (`SET_SYNC_FLAG`, …) is applied downstream by the symbolizer in the profile UI when a symbol table is available; in the raw `XSpace` the name is the number. A consumer of the raw `XSpace` must map decimal-string names back through the [TracePoints Master Registry](tracepoints-master-registry.md) to recover the enum.

### Regime (b) — Semantic

The stateful/decorating subscribers compute a human-readable name from decoded fields:

- **Sync** — `GetSyncFlagEventName<TraceEntry>` @ `0xf1ef840` switches on `TraceHeader.trace_point_id` (`+0x18`), picks a fixed prefix string, and appends the `sync_flag_number` (`StrCat` + `FastIntToBuffer`). The prefixes are byte-decoded rodata literals, keyed by `trace_point_id`: `80`→`"SyncWait:"`, `86`→`"SyncWait:"`, `87`→`"SyncNoWait:"`, `81`→`"Set:"`, `82`→`"Add:"`, `88`→`"Read:"` (the `default` arm — ids `83`–`85` — returns an empty string). Within each arm the event-`oneof` discriminant at `entry+0x28` (`0x25`/`0x26`/`0x27`/`0x2b`/`0x2c`/`0x2d` respectively) is compared only to pick the correct typed accessor for the `sync_flag_number`. A blocking wait (id `86` or DMA-done id `80`) → `"SyncWait:<sync_flag_number>"`. (Note: `"Barrier Wait:"`/`"Sync Wait:"` *do* exist as rodata strings but belong to a different code path; `GetSyncFlagEventName` never emits them.)
- **HLO** — `TpuXPlaneBuilder::GetOrCreateXlaEventMetadata(pair<hlo_module, hlo_op>)` @ `0xf1e4e40` plus `Symbolizer::{TensorCore,BarnaCore,SparseCore}Symbolize` (`0xf57ce60`/`da0`/`ccc0`) and `SetNameIfEmpty` @ `0xf1e4f40` → the HLO op name, interned on the `"XLA Ops"` line. Driven by `TRACE_INSTRUCTION`/`SET_TRACEMARK` (pxc `85`/`84`).
- **Fixed counter labels** — a few counter-line events use a constant rodata name, e.g. `"TensorCore Infeed Buffer"` (`@0x85b4e21`) interned via `GetOrCreateEventMetadata(string_view)` @ `0x1cf4d0c0` on the counter line.

### Naming Function Map

| Function | Address | Produces |
|---|---|---|
| `TpuXLineBuilder::AddEvent<lambda>` (pxc) | `0xf1f26e0` | decimal-string raw name |
| `FastIntToBuffer(int)` | `0x211719e0` | the id's base-10 text |
| `GetOrCreateEventMetadata(string&&)` | `0x1cf4d380` | intern by name → metadata_id |
| `GetSyncFlagEventName<TraceEntry>` (pxc) | `0xf1ef840` | `"SyncWait:<n>"` etc. |
| `GetOrCreateXlaEventMetadata(pair)` | `0xf1e4e40` | HLO op name on "XLA Ops" |
| `Symbolizer::TensorCoreSymbolize` | `0xf57ce60` | HLO symbol resolution |

---

## Payload Scalar to XStat

### Purpose

Beyond the universal `device_*_ps` pair, each subscriber surfaces the *one or few* decoded payload scalars it cares about as dynamic `XStat`s on the just-built `XEvent`. The mapping is **per-subscriber**, not table-driven: each `ProcessTraceEntry` reads the `TraceEntryWrapper` accessor for its field (`SyncFlagValue()`, `GetDmaId()`, `MemoryDataEnd()`, …), interns the matching stat name, and calls `AddStatValue<V>` on the returned `XEventBuilder`.

### Algorithm

```c
// after AddEvent(GtcSpan) returns the XEventBuilder:
function add_dynamic_stat(event_builder, name, value):
    md = XPlaneBuilder::GetOrCreateStatMetadata(name)   // intern by name → per-plane stat id
    XStatsBuilder<XEvent>::AddStatValue<V>(event_builder, md, value)   // append XStat, set oneof
```

The `oneof` case written by `AddStatValue<V>` depends on the value type, confirmed at the call sites:

| Value kind | XStat oneof case | Field | Used by |
|---|---|---|---|
| `int64` | 3 (`int64_value`) | 4 | the universal `device_*_ps` pair |
| `uint64` | 2 (`uint64_value`) | 3 | DMA byte count (`AddStatValue<unsigned long>` @ `0xf1df460`) |
| `double` | (per schema) | — | SPI power(W) samples |
| `XStatMetadata&` (ref) | 7 (`ref_value`) | — | sync wait reason (interned string ref, `0xf1e1da0`) |

### Observed dynamic stats

Confirmed `name` → `value` mappings, byte-anchored:

- **Sync wait reason** — `"TensorCore waiting for Host Infeed"` (`@0x871cd36`, len `0x22`) as a `ref_value` (case 7) on the `SyncWait` duration event. For the `id==255` sentinel the stat name is empty (len 0).
- **Counter sample** — `"Available Count"` (`@0x84e8ef6`, len `0xf`) on the TC0 infeed-buffer counter line.
- **DMA byte count** — the transferred byte count (from `MemoryDataEnd()`) as a `uint64_value` (case 2) on the DMA duration event.

> **NOTE —** the *full* per-subscriber field→stat-name map (which decoded scalar becomes `core_id`/`chip_id`/`sync_flag_id`/`overlay_id`/`step_id`/`marker_payload` on each family) is **not fully enumerated** — the mechanism and the high-value names are confirmed, but exhaustively dumping every `ProcessTraceEntry`/`ProcessScTraceEntry` lambda per family was out of scope. The dynamic stat-name catalog is on [XStat Metadata IDs](xstat-metadata-ids.md); treat per-family scalar surfacing as **MEDIUM confidence** beyond the names listed here.

---

## Begin/End Pairing — Duration Events

### Purpose

Some event kinds are *spans*, not points: a sync wait and a DMA each arrive as two wire packets (begin + end). The subscriber holds a per-core *Tracker* that records the begin, matches the end by a key, and only then calls `AddEvent(GtcSpan)` with the matched `{start, end}` span — producing **one** duration `XEvent`. The `XEvent` does not exist until the closing packet arrives; an unmatched begin yields no event (or is flushed at end-of-trace).

### Sync-flag pairing — `SyncTracker`

`SyncTracker` is a per-core, per-sync-flag state machine. `SyncSubscriber::ProcessTraceEntry` (pxc `0xf1eeee0`) dispatches on every sync trace-point in `{80,81,82,86,87,88}` (selected by `(id-80 < 9) & (0x1C7 >> (id-80))`, byte-decoded). Of those, the tracker itself only consumes the two span-forming ids — `86` (block) and `80` (unblock); the rest are rendered as point/counter events. The match key is `sync_flag_number`.

```c
function SyncTracker::ProcessTraceEntry(entry):          // pxc 0xf1ef3c0
    if entry.wrapper_case != 2: return                   // *a2 == 2 gate
    switch entry.header.trace_point_id:                  // +0x18
        case 86: // UNSUCCESSFUL_SYNC_ATTEMPT — the WAIT BLOCKS
            ProcessSyncBlock(sync_flag_value, sync_flag_number)     // 0xf2c46a0
        case 80: // EXTERNAL_SYNC_FLAG_UPDATE_DMA_DONE ── DMA-side unblock
            return ProcessSyncUnblock(value, sync_flag_number)      // 0xf2c4700
        // NOTE: this tracker only handles 86 (block) and 80 (unblock).
        // id 87 (SUCCESSFUL_SYNC_ATTEMPT) is NOT processed here — it names
        // a "SyncNoWait:" instantaneous event, not the end of a wait span.

function ProcessSyncBlock(value, sfn):                   // 0xf2c46a0
    state.value          = value                         // +0x08
    state.sync_flag_num  = sfn                           // +0x18 — THE MATCH KEY (line 18)
    state.waiting        = 1                             // +0x28 (line 22-23)
    // stash first-block start gtc at +0x58/+0x60

function ProcessSyncUnblock(value, sfn) -> optional<SyncWaitInfo>:   // 0xf2c4700
    if state.waiting != 1: return none                   // +0x28 == 1 (line 13)
    if state.sync_flag_num != sfn: return none           // +0x18 == sfn (line 15) — key match
    result = state.SyncWaitInfo{ start_gtc, end_gtc=value }   // copy the 32-byte pending block
    state.waiting = 0
    return result                                        // SyncSubscriber renders it
```

On a match, `SyncSubscriber::AddSyncWaitEvent` @ `0xf1ef520` builds a `GtcSpan{start, end}` from the returned `SyncWaitInfo` and emits one duration `XEvent` on `TpuComponent 17` ("Tensor Core Sync Flag"), named `"SyncWait:<sfn>"`, with the `ref_value` wait-reason stat. Out-of-order and vector-wait completions (`ProcessSyncBlockOOO` @ `0xf2c47e0`, `ProcessSyncUnblockForVwait` @ `0xf2c4740`) extend the match to also compare a second transaction/value at `state+0x20`.

> **GOTCHA —** the begin packet is `UNSUCCESSFUL_SYNC_ATTEMPT` (id 86), not a "begin" opcode — the hardware emits a *failed* sync attempt when the wait blocks. The closing packet in the tracker is the external `EXTERNAL_SYNC_FLAG_UPDATE_DMA_DONE` (id 80), **not** the `SUCCESSFUL_SYNC_ATTEMPT` (id 87): `SyncTracker::ProcessTraceEntry` only routes id 86 → `ProcessSyncBlock` and id 80 → `ProcessSyncUnblock`. A reimplementation reading the names literally will invert begin/end or wrongly close the span on id 87. The pairing semantics are: **block = the wait started (id 86, failed attempt); unblock = the DMA that the wait was blocked on completed (id 80)**. Id 87 (`SUCCESSFUL_SYNC_ATTEMPT`) is a separate, instantaneous `"SyncNoWait:"` event — a sync flag that was already satisfied, so no span. Instantaneous sync-flag ops (`Set`/`Add`/`Read`, ids 81/82/88) are likewise *not* paired — they become point events or counter samples.

### DMA pairing — `DmaSubscriber`

`DmaSubscriber::ProcessTraceEntry` @ `0xf1dfee0` keys pending starts in a `FlatHashMap<uint64 dma_id, vector<pending-start>>` by `GetDmaId()` @ `0xf698180`.

```c
function DmaSubscriber::ProcessTraceEntry(entry):        // jxc 0xf1dfee0
    if entry.CoreId() != this_core: return               // filter
    dma_id = entry.GetDmaId()                             // 0xf698180 — THE MATCH KEY
    if entry.MemoryCommand() && entry.First():           // DMA STARTED (descriptor / first packet)
        pending[dma_id].push(entry)                       // record start gtc
    else if entry.MemoryDataEnd() || entry.Last():        // DMA COMPLETED
        start = pending[dma_id].pop()                      // pop matching start
        span  = GtcSpan{ start.gtc, entry.gtc }
        line  = GetOrCreateLine(component_for(dma_id))     // a mem / Memcpy line
        eb    = TpuXLineBuilder::AddEvent(span, *meta)     // 0xf1df1e0
        add_dynamic_stat(eb, "<byte count>", entry.MemoryDataEnd())   // uint64_value
```

> **NOTE —** the internal match-key offsets for the *other* stateful trackers (`StepTracker` by TraceMark sequence, `TaskTracker` by task id, `OverlayTracker` by overlay id) were located by symbol but **not byte-decoded** here — only `SyncTracker` (`sync_flag_number`) and `DmaSubscriber` (`dma_id`) are confirmed key-exact. Treat the Step/Task/Overlay pairing keys as **LOW confidence**.

### Pairing Function Map

| Function | Address | Match key |
|---|---|---|
| `SyncTracker::ProcessTraceEntry<pxc>` | `0xf1ef3c0` | `sync_flag_number` (`state+0x18`) |
| `SyncTracker::ProcessSyncBlock` | `0xf2c46a0` | stores key `+0x18`, waiting `+0x28` |
| `SyncTracker::ProcessSyncUnblock` | `0xf2c4700` | gates `+0x28==1`, cmp `+0x18==sfn` |
| `SyncSubscriber::AddSyncWaitEvent` | `0xf1ef520` | emits span on TpuComponent 17 |
| `DmaSubscriber::ProcessTraceEntry<jxc>` | `0xf1dfee0` | `dma_id` (FlatHashMap) |
| `StepTracker::ProcessTraceEntry` | `0xf2c4480` | TraceMark sequence (not decoded) |
| `TaskTracker::ProcessTraceEntry` (gfc) | `0xf2394e0` | task id (not decoded) |
| `OverlayTracker::ProcessTraceOperand` | `0xf2c3e40` | overlay id (not decoded) |

---

## XLine Assignment — TpuComponent Lanes

### Purpose

Every device-plane `XLine` is an `xprof::TpuComponent` enum ordinal. `TpuXPlaneBuilder::GetOrCreateLine(TpuComponent c)` @ `0xf1df120` calls the generic `XPlaneBuilder::GetOrCreateLine((long)c)` with the ordinal as the line id and, if the line is new, names it `TpuComponentName(c)`. **The line id is the component ordinal and the name is fixed.** Which subscriber writes to which line(s) is hard-coded in each `ProcessTraceEntry` body.

### Algorithm

```c
function TpuXPlaneBuilder::GetOrCreateLine(this, plane_builder, component):   // 0xf1df120
    line = XPlaneBuilder::GetOrCreateLine(plane_builder, component)   // 0x1cf4d9a0, id == ordinal
    name = TpuComponentName(component)                                // 0x1c8ebb60, 149-arm table
    if line.name is empty:
        XLineBuilder::SetName(line, name)                            // 0xf1ad5a0
    return line
```

### The component → line-name catalog

`TpuComponentName` @ `0x1c8ebb60` is a 149-arm rel32 jump table @ `0xb43d200` (bound check `cmp $0x94`), byte-decoded. Seven entries are relocated `kXxxLineName` string globals (R_X86_64_RELATIVE addend = string VA). Rather than dump all 149 rows, the table is organized by *band*; the rows that name specific subscriber targets are called out.

| Ordinal band | Lines | Owning subsystem |
|---|---|---|
| 0–6 | Host Interface, Steps*, XLA Modules*, **XLA Ops*** (3), Async XLA Ops*, Barna Ch12, XLA TraceMe | top-level HLO / step / TraceMe lanes |
| 7–20 | **TC Overlay** (7), Tensor Core (8), Scalar Unit (9), VPU (10), MXU vpush/vmatmul/vpop (11–13), XLU/XU/RPU (14–16), **Tensor Core Sync Flag** (17), TC IMEM/VMEM/SMEM (18–20) | TensorCore functional units + sync |
| 21–45 | Barna Core + its sync/fabric/concat/channels (28–43)/IMEM/BMEM | Barna Core engine |
| 46–67 | Sparse Core (46), SC TEC (47), SC TAC (48), Node Fabric, Host/ICI router lanes, HBM, Power Throttle (58), SC Modules*/Ops*/Syncs (65–67) | SparseCore + fabric + I/O |
| 68–116 | TAC 0–15 (68–83), TEC 0–15 (84–99), SC TraceMe (100), TEC TraceMe 0–15 (101–116) | per-tile SparseCore lanes |
| 117–148 | SC Steps* (117), power/thermal meters (118–130), GBMC sensors, PCIe util (134–139), cost model, stats lines (140–148) | firmware power/thermal/counter lanes |

`* = relocated kXxxLineName global` (e.g. `kXlaOpLineName` @ `0x21c9e0b8` → `0x8534785` "XLA Ops"; `kStepLineName` → "Steps"; `kSparseCoreStepLineName` → "Sparse Core Steps").

### Subscriber → line ownership

The fixed event→lane routing, byte-confirmed in each subscriber body:

| Subscriber | Trace-points (pxc) | XLine(s) (TpuComponent) |
|---|---|---|
| `TensorCoreHloSubscriber` | 84/85 (SET_TRACEMARK / TRACE_INSTRUCTION) | 3 "XLA Ops" |
| `SyncSubscriber` | {80,81,82,86,87,88} (mask 0x1c7) | 17 "Tensor Core Sync Flag" (+ counter line) |
| `TensorCoreOverlaySubscriber` | overlay trace-points | 7 "TC Overlay" |
| `TensorCoreStepSubscriber` / `StepTracker` | SET_TRACEMARK (step marks) | 1 / 117 "Steps" / "SC Steps" |
| `ScalarFenceSubscriber` | 89/90 (SCALAR_FENCE_START/END) | 9 "Scalar Unit" (fence span) |
| `DmaSubscriber` | DMA req / data-end | mem / Memcpy line by `dma_id` |
| `FirmwareSubscriber` | manager/power FW events | 120–130, 134–139, 141, 143 (`kComponents`, byte-read) |
| `PowerThrottleSubscriber` | 97 / 200.. (THROTTLE_*) | 58 "Power Throttle" |
| `SpiSamplerSubscriber` | 168/169 (SPI_SAMPLER_*) | 118/119 "SPI Sampler Power Meter(W)" |
| `SparseCore{Hlo,Task,Overlay,Syncs,Step}Subscriber` | SC_* (108..135) | 46/47/48, 65/66/67/142 |
| `…OnDeviceTraceMeSubscriber` | TraceMark TraceMe records | 6, 100, 101–116 "TraceMe" lanes |
| `LloOpEventSubscriber` | TRACE_INSTRUCTION (LLO ops) | 8 "Tensor Core" + units 9–16 |

`FirmwareSubscriber::GetTrackedComponents()::kComponents` byte-confirmed (19-entry `int32` array, size `0x4c`, identical for vfc/vlc/glc/gfc) = `{120,121,122,123,124,125,126,127,128,129,130, 134,135,136,137,138,139, 141, 143}` (note the gaps at 131–133, 140, 142).

---

## Event-Type Row Summary

All four deliverables — naming, stats, pairing, line — in one row per representative event:

| Wire trace-point (pxc) | Subscriber | XLine | XEvent name source | Pairing key | Per-event XStats |
|---|---|---|---|---|---|
| 86 UNSUCCESSFUL_SYNC_ATTEMPT (begin) | Sync | 17 Sync Flag | `SyncWait:<sfn>` | `sync_flag_number` (block) | device_*_ps + "…waiting for Host Infeed" (ref) |
| 80 EXT_SYNC_FLAG_UPDATE_DMA_DONE (end) | Sync | 17 | `SyncWait:<sfn>` | `sync_flag_number` (unblock) | (closes the span begun by id 86) |
| 87 SUCCESSFUL_SYNC_ATTEMPT | Sync | 17 | `SyncNoWait:<sfn>` | (instantaneous, no span) | device_*_ps only |
| 81/82/88 SET/ADD/READ_SYNC_FLAG | Sync | 17 / counter | `Set:/Add:/Read:<sfn>` | (instantaneous) | "Available Count" (counter) |
| 84/85 SET_TRACEMARK / TRACE_INSTRUCTION | HLO | 3 XLA Ops | HLO op (symbolizer) | (none) | hlo_op/hlo_module/program_id |
| CMQ/HDE DMA req + data-end | Dma | mem / Memcpy | decimal id / op name | `dma_id` (First→Last) | uint64 byte count |
| 97 / 200.. THROTTLE_* | PowerThrottle | 58 Throttle | decimal id | run-length / point | throttle counters |
| 168/169 SPI_SAMPLER_* | SpiSampler | 118/119 Meter | decimal id | per-frame | power(W) double |
| MGR / power FW | Firmware | 120–130 / 134–139 / 141 / 143 | decimal id / label | run-length | power/util counters |
| (any unhandled banded id) | raw `TraceEventSubscriber` | the owning line | **decimal string of id** | (none) | device_*_ps only |

---

## Relevant Struct Offsets

```text
TpuXLineBuilder        +0x00/+0x08  base XLineBuilder {XPlane*, XLine*}
                       +0x10        per-line GTC clock object (clk for *1e9/(clk<<4))
                       +0x18        TpuXPlaneBuilder*
                       +0x38        device_offset_ps  XStatMetadata*  (StatType 147)
                       +0x40        device_duration_ps XStatMetadata* (StatType 148)
                                    (AddEvent gates on +0x38 && +0x40 non-null)

XEvent (proto2)        +0x18        stats RepeatedPtrField
                       +0x30        duration_ps (presence orb |= 4)
                       +0x38        offset_ps   (oneof "data" case 2)
                       +0x40        oneof "data" case discriminant

XStat (proto2)         +0x18        metadata_id   (= XStatMetadata + 0x28)
                       +0x20        value
                       +0x28        oneof case (2 uint64 / 3 int64 / 7 ref / double)

SyncTracker            +0x08        value
                       +0x18        sync_flag_number  ── THE MATCH KEY
                       +0x20        2nd key (OOO / vwait)
                       +0x28        waiting bit
                       +0x58/+0x60  first-block start-gtc stash
SyncWaitInfo (returned) +0x00 start_gtc  +0x08 end_gtc  +0x20 present-byte

Subscriber base        +0x08/+0x0c  {core_id, chip_id} filter (from CoreId())
                       +0x10        TpuXPlaneBuilder*
                       +0x18        per-id XEventMetadata* cache (FlatHashMap, raw path)

CoreDispatcher          FlatHashMap<trace_point_id, vector<shared_ptr<subscriber>>>
                        (Dispatch keys on TraceHeader+0x18)
```

---

## Related Components

| Component | Relationship |
|---|---|
| `ConvertTpuTraceToXPlaneV2<TraceEntry>` | per-family orchestrator that builds the XPlane and registers the subscriber set |
| `tsl::profiler::XLineBuilder` / `XPlaneBuilder` | the generic builder primitives `TpuXLineBuilder` wraps |
| `Symbolizer` / `TpuXPlaneSymbolizer` | post-hoc HLO/PC symbol enrichment of the semantic-named events |
| `TpuProfilerImpl::CollectData(XSpace*)` @ `0xef34860` | the drain entry that invokes the conversion |

---

## Cross-References

- [Profiling and Telemetry Overview](overview.md) — the five-stage capture pipeline this page's translation closes
- [TraceEntriesCoder](trace-entries-coder.md) — the device codec that decodes the 16-byte packets into the `TraceEntry` protos this page consumes
- [XPlaneBuilder / XStat / TraceMe](xplane-xstat-traceme.md) — the builder API (`AddEvent`, `GetOrCreate*Metadata`, `AddStatValue`) this translation drives
- [XEvent Metadata IDs](xevent-metadata-ids.md) — the per-plane interned event-name dictionary the two naming regimes populate
- [XStat Metadata IDs](xstat-metadata-ids.md) — the stat-id catalog: `device_offset_ps` (147), `device_duration_ps` (148), and the dynamic per-subscriber names
- [TracePoints Master Registry](tracepoints-master-registry.md) — the `trace_point_id` enum the dispatcher routes on and the raw decimal names map back to
- [Riegeli Trace Container](riegeli-trace-container.md) — the on-wire container the decoded `TraceEntry` stream is unpacked from
