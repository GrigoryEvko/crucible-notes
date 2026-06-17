# neuron_rustime: serde_json Serializer

> *All addresses, offsets, and symbols on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, soname `libnrt.so.1`, version namespace `NRT_2.0.0`). The ELF is **not** stripped; full `.symtab` + DWARF are present, and `.text`/`.rodata` VMA equals file offset, so every `0x5…`/`0xa…` is both an analysis VMA and a file offset. The Rust producer is statically linked from `rustc 1.91.1` (`/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb/`), with vendored `serde_json 1.0.145`, `serde_core 1.0.228`, `crossbeam-queue 0.3.12`, and `hashbrown 0.15.5` (all byte-grepped from cargo registry paths in `.debug_str`). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every function is pinned to a `.symtab` symbol + `.text` address (`nm`-verified); the 11 JSON key strings, the top-level keys, the snake_case display pool, the `ESCAPE`/`HEX_DIGITS` tables, and the `"Couldn't serialize json"` error are read directly from `.rodata`. The per-variant payload-key roster is `[HIGH]` (verbatim from the `EventDataWrapper` switch); the exact data-union byte-offset each custom key reads is owned by [event-taxonomy](event-taxonomy.md) and left as a cross-link. · Part XIII — Profiling, Trace & Telemetry · [back to index](../index.md)*

## Abstract

`api::fetch_events` @`0x5aa3b0` is **producer (2)'s JSON terminal** — the second of two read-out paths that drain the per-NeuronCore capture rings ([rust-capture](rust-capture.md)). Where `event_to_proto` @`0x9aec0` serializes the same drained `nrt_sys_trace_event` records into the NTFF protobuf wire format ([ntff-format](ntff-format.md)), this path serializes them into a **human-readable JSON document** through `serde_json 1.0.145`'s `Serializer<Vec<u8>, CompactFormatter>`, then hands the finished bytes across the C ABI as a NUL-terminated `CString`. The C export `nrt_sys_trace_fetch_events` @`0xb9880` is the consumer's entry; the cross-band shim `nrt_sys_trace_fetch_events_internal` @`0x5099a0` marshals options, calls the Rust driver, validates for interior NUL, and returns `(*out_ptr, *out_len)` to a caller that later frees the buffer via `nrt_sys_trace_buffer_free` @`0xb9910`.

The familiar reference frame is a **`#[derive(Serialize)]` struct tree driven through a `serde_json::to_writer` into a `Vec<u8>`** — the everyday Rust JSON path — but realized by hand-written `impl Serialize` glue rather than derive, because the source records are a C-ABI tagged union, not a Rust enum. The driver builds a two-key top-level object `{"events":[…],"data_version":2}`; the `"events"` array is an `EventsWrapper` whose `serialize` (@`0x5ab5d0`) calls `Serializer::collect_seq` (@`0x5b2f80`) over the drained slice; each element is an `EventWrapper` (@`0x5ab320`) that emits an **11-key object**, whose `"data"` value is an `EventDataWrapper` (@`0x5ab610`) — a 46-arm switch on `trace_type` that emits a nested, phase-gated `custom_data` object. There is **no heap `Formatter` trait object**: serde_json 1.0.145 monomorphizes `CompactFormatter` into each `serialize_entry`, so every structural byte (`{` `}` `[` `]` `:` `,` `"`) is a direct inline store into the `Vec<u8>` `RawVec`, and every integer is formatted by an inlined jeaiii/Lemire itoa LUT.

This page documents the serialize path to reimplementation accuracy: the **11-key JSON schema** (key → JSON type → source struct field → meaning); the **serialize pipeline** from `nrt_sys_trace_event` through the `Serializer<Vec<u8>, CompactFormatter>` to the `CString` sink; the **per-variant payload serialization** the `EventDataWrapper` switch performs, including the phase-gate that emits an empty `{}` on a phase mismatch; and the **`CompactFormatter` behavior** (no-whitespace JSON, `ESCAPE`-table string escaping) plus the `Vec<u8>`→`CString` FFI handoff. The capture ring that fills the records ([rust-capture](rust-capture.md)) and the 46-variant event model + 40-byte `data` union ([event-taxonomy](event-taxonomy.md)) are boundaries — cross-linked, not re-derived.

For reimplementation, the contract is:

- **The 11-key event object** — `event_type`/`phase`/`id`/`nc_idx`/`nc_local_id`/`tracking_id`/`thread_id`/`worker_gid`/`data`/`timestamp_ns`/`nrta_seq_id`, in emission order, each key a `.rodata` string at a pinned address, each value sourced from a known `nrt_sys_trace_event` offset. `nrta_seq_id` is conditional (`has_nrta_seq_id`).
- **The two-key envelope** — the driver wraps the array in `{"events":[…],"data_version":2}`; `data_version` is a compile-time `u32` constant `2`.
- **The `Serialize` glue, not derive** — four hand-written `impl Serialize` types (`EventsWrapper` / `EventWrapper` / `EventDataWrapper` / the `SysTraceEventType`/`Phase` enums) bridge the C-ABI record into serde; each `serialize_entry` is monomorphized per `(key=&str, value-type)` pair.
- **The phase-gated payload** — each of the 46 `EventDataWrapper` arms emits its `custom_data` keys only on its canonical phase (`START`/`STOP`/`INSTANT`); a phase mismatch logs a `Debug` warning and emits `"data":{}`.
- **`CompactFormatter` + `CString` sink** — whitespace-free JSON written byte-by-byte into a growable `Vec<u8>`; strings escaped through the 256-entry `serde_json::ser::ESCAPE` table with `\u00XX` fallback; the finished `Vec<u8>` is NUL-checked and moved into a `CString` whose raw pointer crosses the ABI.

| | |
|---|---|
| **Source TU** | `src/sys_trace/api.rs` (Rust 1.91.1); serde glue inline |
| **C export** | `nrt_sys_trace_fetch_events` @`0xb9880` (`NRT_2.0.0`) → internal @`0x5099a0` |
| **Rust driver** | `api::fetch_events` @`0x5aa3b0` — drain → serialize → return `String{ptr,cap,len}` |
| **Top-level keys** | `"events"` (6) @`0xa30dac` · `"data_version"` (12) @`0xa30db2`; envelope `{…,"data_version":2}` |
| **Array wrapper** | `EventsWrapper::serialize` @`0x5ab5d0` → `Serializer::collect_seq` @`0x5b2f80` |
| **Event object** | `EventWrapper::serialize` @`0x5ab320` — 11 keys @`0xa31392…` (clustered) |
| **Payload switch** | `EventDataWrapper::serialize` @`0x5ab610` — 46-arm `trace_type` switch (largest fn on path) |
| **Enum names** | `SysTraceEventType::serialize` @`0x5aae00` (snake_case pool @`0xa30dbe`) · `…Phase::serialize` @`0x5ab130` |
| **String escaper** | `serde_json::ser::format_escaped_str` @`0x5b4140`; `ESCAPE` table @`0xa33478` (256 B); `HEX_DIGITS` @`0xa33578` (`"0123456789abcdef"`) |
| **Sink** | `Vec<u8>` `RawVec` → `CString::_from_vec_unchecked`; `*out_len = clen-1`; freed by @`0xb9910` |
| **Source record** | `nrt_sys_trace_event` — **112 B** (`0x70`); `data` union @`+48`, `phase` @`+88`, `nc_idx` @`+92` ([event-taxonomy](event-taxonomy.md)) |

---

## 1. The 11-Key JSON Schema

### Purpose

Each drained `nrt_sys_trace_event` becomes one JSON object with up to eleven keys, emitted in the fixed order below by `EventWrapper::serialize` @`0x5ab320`. The keys cluster verbatim in `.rodata` starting at `0xa31392` (byte-verified: a single 100-byte run packs `event_type`/`phase`/`id`/`nc_idx`/`nc_local_id`/`tracking_id`/`thread_id`/`worker_gid`/`timestamp_ns`/`nrta_seq_id`, with `"data"` borrowed from the adjacent pool at `0xa154e1`). Ten keys are unconditional; `nrta_seq_id` is emitted **only** when the record's `has_nrta_seq_id` flag (`evt+104`) is set.

> **NOTE —** this 11-key object is the *JSON view* of the 112-byte binary record. It is **not** a 1:1 field dump: the binary record's `nc_local_id`/`tracking_id`/`thread_id` are direct copies, but `worker_gid` is a *computed* lookup (`gid_map[nc_idx]`), `event_type`/`phase` are *stringified* enum names, and `timestamp_ns` can be *overridden* by a variant's `nc_timestamp_ns` (§3). A reimplementer who emits the raw struct fields will diverge on exactly those three keys.

### Schema table

| Key (`.rodata`) | JSON type | Source field | Meaning | Conf |
|---|---|---|---|---|
| `event_type` @`0xa31392` (10) | string | `evt+0` `trace_type` (u32) | snake_case display name of the 46-variant discriminant — `generic`, `nrt_execute`, `cc_exec_barrier`, … (§4); via `SysTraceEventType::serialize` @`0x5aae00` | HIGH |
| `phase` @`0xa3139c` (5) | string | `evt+88` `phase` (u32) | `start` / `stop` / `instant` / `unknown`; via `SysTraceEventPhase::serialize` @`0x5ab130` | HIGH |
| `id` @`0xa313a1` (2) | number | `evt+16` `id` (u64) | event id | HIGH |
| `nc_idx` @`0xa313a3` (6) | number | `evt+92` `nc_idx` (u32) | NeuronCore index that produced the span | HIGH |
| `nc_local_id` @`0xa313a9` (11) | number | `evt+24` `nc_local_id` (u64) | per-NC local id | HIGH |
| `tracking_id` @`0xa313b4` (11) | number | `evt+32` `tracking_id` (u64) | caller id, else `seq \| (nc_idx<<50)`; shares the 50-bit interned-string id space ([rust-capture §the interner](rust-capture.md)) | HIGH |
| `thread_id` @`0xa313bf` (9) | number | `evt+8` `thread_id` (u64) | cached `gettid` (TLS `THREAD_ID`) | HIGH |
| `worker_gid` @`0xa313c8` (10) | number | `gid_map[evt.nc_idx]` (i32, `-1` if OOB) | worker global id from the `vnc_idx→worker_gid` map built once per fetch (`vnc_idx_to_gid` @`0x5aaca0`); **not** a record field | HIGH |
| `data` @`0xa154e1` (4) | object | `evt+48` `data` union (40 B) | nested per-variant `custom_data` (§3); always `{…}` or `{}` (never absent) | HIGH |
| `timestamp_ns` @`0xa313d2` (12) | number | variant `nc_timestamp_ns` **override**, else `evt+40` (u64) | span timestamp in ns; a variant that supplies `nc_timestamp_ns` lifts it up to replace the host `timestamp_ns` (§3) | HIGH |
| `nrta_seq_id` @`0xa313de` (11) | number | `evt+96` `nrta_seq_id` (u64) | runtime async sequence id — **emitted iff** `evt+104 has_nrta_seq_id != 0` | HIGH |

> **QUIRK —** `worker_gid` is the only top-level key whose value comes from *outside* the record. `fetch_events` builds a `Vec<u32> gid_map = [vnc_idx_to_gid(i) for i in 0..visible_vnc]` once, before the serialize loop, then each event indexes it by `nc_idx`. If `nc_idx` is out of the map's range the key emits `-1` (the `i32` monomorph @`0x5b3b70` is selected precisely because the sentinel is signed). A reimplementation that re-derives the gid per event, or that uses an unsigned type and loses the `-1` sentinel, will not match.

---

## 2. The Serialize Pipeline

### Purpose

`fetch_events` is the driver: it drains the rings, builds the gid map, constructs the `Serializer<Vec<u8>, CompactFormatter>` over a fresh sink, and walks the wrapper tree. The serde tree is hand-written `impl Serialize` (not `#[derive]`) because the inputs are C-ABI records; the structural shape is nonetheless ordinary serde — a top-level map, a sequence, and per-element maps.

### Entry Point

```text
nrt_sys_trace_fetch_events (0xb9880)                 [C export, NRT_2.0.0]
  └─ nrt_sys_trace_fetch_events_internal (0x5099a0)  ── marshal fetch_options; NUL-check; CString
       └─ api::fetch_events (0x5aa3b0)               ── the driver
            ├─ capture::get_all_interned_strings (0x5b19e0)   [BOUNDARY: rust-capture]
            ├─ nrt_get_visible_vnc_count                      [BOUNDARY: C]
            ├─ api::vnc_idx_to_gid (0x5aaca0) × N  → Vec<u32> gid_map
            ├─ capture::ring_size (0x5b2010) → alloc Vec<event>(112·N)
            ├─ capture::drain_events (0x5afea0) per vnc       [BOUNDARY: ArrayQueue::pop]
            ├─ alloc Vec<u8> sink; write '{'
            ├─ serialize_entry("events", EventsWrapper)
            │    └─ EventsWrapper::serialize (0x5ab5d0)
            │         └─ Serializer::collect_seq (0x5b2f80)   ── write '['
            │              └─ (per element) EventWrapper::serialize (0x5ab320)
            │                   └─ 11 × serialize_entry  (§1)  ── write '{' … '}'
            │         └─ write ']'
            ├─ serialize_entry("data_version", &2u32)         ── u32 itoa
            └─ write '}'  →  String{ptr,cap,len}
```

### Algorithm — the driver

```c
// Models api::fetch_events @0x5aa3b0.  Returns a Rust String{ptr,cap,len} by value.
// fetch_opts is the marshaled SysTraceFetchOptions (From @0x5ab1a0):
//   .max_events_per_nc (u64, 0=unlimited) ; .nc_idx (i32, -1=all NCs).
function fetch_events(ret, fetch_opts):
    interned = get_all_interned_strings()              // 0x5b19e0  [BOUNDARY] id→name map
    vnc      = nrt_get_visible_vnc_count()             // [BOUNDARY C]
    gid_map  = [ vnc_idx_to_gid(i) for i in 0..vnc ]   // 0x5aaca0; Vec<u32> worker_gid table

    cap = ring_size()                                  // 0x5b2010
    events = Vec::with_capacity(112 * vnc)             // drained 112-B records
    for i in 0..vnc:
        if fetch_opts.nc_idx >= 0 && i != fetch_opts.nc_idx:  continue  // single-NC filter
        drain_events(&events, i, fetch_opts.max_events_per_nc)          // 0x5afea0 [BOUNDARY]

    sink = Vec::<u8>::with_capacity(128)               // __rust_alloc; the JSON buffer
    ser  = Serializer{ writer: sink, fmt: CompactFormatter }   // no heap Formatter object
    write_byte(sink, '{')                              // 123

    // key 1: "events" -> the array of event objects
    r = serialize_entry(&ser, "events", 6,            // key escaped via format_escaped_str
                        EventsWrapper{ events, gid_map, interned })   // -> §collect_seq
    if r is Err:
        unwrap_failed("Couldn't serialize json"@0xa30d21)             // panic surface (§4)

    // key 2: "data_version" -> compile-time const 2
    serialize_entry(&ser, "data_version", 12, &2u32)  // u32 itoa-inlined

    write_byte(sink, '}')                              // 125
    *ret = String::from_utf8_unchecked(sink)           // {ptr,cap,len}; UTF-8 by construction
```

### Algorithm — the array and the event object

```c
// Models EventsWrapper::serialize @0x5ab5d0.
function EventsWrapper_serialize(self, ser):
    slice = [ self.base + 112*k  for k in 0..self.len ]   // the drained Vec<event> as a slice
    return ser.collect_seq(slice)                          // 0x5b2f80: write '[' … ']'
    //  collect_seq's try_fold calls EventWrapper_serialize per element, inserting ',' between.

// Models EventWrapper::serialize @0x5ab320.  evt = &nrt_sys_trace_event (112 B).
function EventWrapper_serialize(self, ser):                // self = {evt, &gid_map, ctx}
    m = ser.serialize_map()                                // private "first" bool; writes '{'
    serialize_entry(m, "event_type",   SysTraceEventType(evt+0))    // 0x5aae00 -> string
    serialize_entry(m, "phase",        SysTraceEventPhase(evt+88))  // 0x5ab130 -> string
    serialize_entry(m, "id",           u64@(evt+16))               // itoa
    serialize_entry(m, "nc_idx",       u32@(evt+92))               // itoa
    serialize_entry(m, "nc_local_id",  u64@(evt+24))
    serialize_entry(m, "tracking_id",  u64@(evt+32))
    serialize_entry(m, "thread_id",    u64@(evt+8))
    gid = (evt.nc_idx < gid_map.len) ? gid_map[evt.nc_idx] : -1     // i32 (0x5b3b70)
    serialize_entry(m, "worker_gid",   gid)
    data = EventDataWrapper{ evt, interned, trace_type:evt+0, phase:evt+88 }   // §3
    serialize_entry(m, "data",         data)               // 0x5b3d90 -> 0x5ab610; '{' … '}'
    ts = data.ts_override_present ? data.ts_override : u64@(evt+40)            // variant override
    serialize_entry(m, "timestamp_ns", ts)
    if u8@(evt+104) != 0:                                  // has_nrta_seq_id
        serialize_entry(m, "nrta_seq_id", u64@(evt+96))
    m.end()                                                // Vec::extend_from_slice("}"@0xa30d20,1)
```

> **GOTCHA —** the `timestamp_ns` value is decided **inside `EventDataWrapper`**, not in `EventWrapper`. The data wrapper sets a 2-word `(present, value)` stack temp (`a1+0`/`a1+8`) when a variant supplies `nc_timestamp_ns`; `EventWrapper` then re-reads that temp to choose the timestamp *after* the `data` key is serialized. So the per-variant payload serialization is a side-effecting pass that also feeds a top-level key. A reimplementation that serializes `data` and `timestamp_ns` as independent fields will emit the host timestamp where the device timestamp was intended for the variants that override it. `[HIGH]`

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `nrt_sys_trace_fetch_events` | `0xb9880` | C export (`NRT_2.0.0`); thin wrapper → internal | HIGH |
| `nrt_sys_trace_fetch_events_internal` | `0x5099a0` | cross-band shim; marshal opts; NUL-check; `CString::_from_vec_unchecked`; set `out_len/out_ptr` | HIGH |
| `api::fetch_events` | `0x5aa3b0` | driver: drain → build gid map → 2-key envelope → return `String` | HIGH |
| `api::vnc_idx_to_gid` | `0x5aaca0` | `vnc_idx → worker_gid` lookup (per-fetch `gid_map`) | HIGH [BOUNDARY enc/vtpb] |
| `EventsWrapper::serialize` | `0x5ab5d0` | `"events"` array → `collect_seq` over the drained slice | HIGH |
| `EventWrapper::serialize` | `0x5ab320` | the 11-key event object (§1) | HIGH |
| `EventDataWrapper::serialize` | `0x5ab610` | 46-arm `trace_type` switch → nested `custom_data` (§3) | HIGH |
| `SysTraceEventType::serialize` | `0x5aae00` | discriminant → snake_case name → `format_escaped_str` (§4) | HIGH |
| `SysTraceEventPhase::serialize` | `0x5ab130` | phase → `{start,stop,instant,unknown}` | HIGH |
| `SysTraceFetchOptions::from` | `0x5ab1a0` | 16-B C `fetch_options` → Rust struct (`max_events_per_nc`, `nc_idx`) | HIGH |
| `Serializer::collect_seq` | `0x5b2f80` | `'['` … `']'` + per-element `try_fold` (this-path monomorph) | HIGH |
| `format_escaped_str` | `0x5b4140` | string-value/key escaper + sink writer (§4) | HIGH |
| `should_include_default_algo` | `0x508630` | CcExec `algorithm`-key gate | HIGH [helper] |
| `cc_algo_name` | `0x5082c0` | collectives algorithm name (CcExec `algorithm` value) | HIGH [BOUNDARY enc] |
| `dma_mem_usage_name` | `0x508480` | `usage_type` name (DMA alloc/dealloc) | HIGH [BOUNDARY tdrv] |
| `nrt_sys_trace_buffer_free` | `0xb9910` | C free → internal @`0x509650` → `CString::from_raw` + `__rust_dealloc` | HIGH |

> **CORRECTION (W2-SERDE-01) —** an earlier trace cell (P1-F-TRACE §2) attributed the **CamelCase** name pool (`Generic`, `NrtLoad`, `NrtExecute`, …) @`0xa3112f` to the `Serialize` path. It is not. `SysTraceEventType::serialize` @`0x5aae00` reads the **snake_case** pool @`0xa30dbe` (`generic`, `nrt_execute`, `cc_exec_barrier`, …); the CamelCase pool is the `Debug::fmt` output (`SysTraceEventType as Debug` @`0x5ab2c0`), used **only** in the phase-mismatch warning log (§3). Both pools physically abut in `.rodata` — the snake pool runs `0xa30dbe…`, the Camel pool begins at `0xa3112f` (byte-verified: `…instant` `Generic` `NrtLoad` `NrtExe…` at `0xa31136`). A reimplementation that serializes the CamelCase names produces a JSON document no Neuron consumer parses. `[HIGH]`

---

## 3. Per-Variant Payload Serialization

### Purpose

The `"data"` value is an `EventDataWrapper` whose `serialize` @`0x5ab610` is the largest function on this path: a switch over `trace_type` (the 46-variant discriminant) that opens a nested object, emits a variant-specific set of `custom_data` keys, and closes it. The keys are the JSON projection of the 40-byte `data` union member selected by `trace_type` ([event-taxonomy §the data union](event-taxonomy.md) owns the union's binary layout; this page owns the *key naming and phase-gating*).

### The phase gate

Every arm is **phase-gated**: it emits its custom fields only on its canonical phase (`START=0`, `STOP=1`, `INSTANT=2`). On a phase mismatch the arm logs a `Debug`-level record — formatting both `EventType` and `EventPhase` through their `Debug::fmt` impls (@`0x5ab2c0` / @`0x5ab2f0`, the CamelCase pool) via `log::__private_api::log(level=WARN)` — then falls through to the closing-`}` label, emitting `"data":{}`. This is the sole reason the CamelCase name pool exists in the binary.

> **QUIRK —** `"data"` is **always emitted**, even for the empty `generic` variant and for phase-mismatched events. The `{` is written unconditionally at function entry and the `}` at the fall-through label, so the value is `{}` rather than absent or `null`. A consumer can rely on `event.data` being an object for every event; a reimplementation that omits the key for empty variants breaks that invariant. `[HIGH — unconditional '{' entry + '}' fall-through]`

### Algorithm — the payload switch

```c
// Models EventDataWrapper::serialize @0x5ab610.  self = {evt, interned, trace_type, phase}.
// Emits a nested object; each arm is gated on its canonical phase.
function EventDataWrapper_serialize(self, ser):
    m = ser.serialize_map()                       // writes '{'
    switch self.trace_type:                        // 46 arms (0..0x2D); 0x2E = sentinel

      case POST_REQUEST(2):
          entry(m, "request_id", u64)              // 0xa3140c

      case CC_LOAD(5):
          if phase==START: entry("g_device_id"@0xa31416); entry("g_device_count"@0xa31421)
          if phase==STOP : entry("model_id"@0xa152b1)

      case CC_EXEC_BARRIER(6):
          if phase==START: entry("nc_timestamp_ns"@0xa3142f -> ts_override); entry("model_id")
          if phase==STOP : entry("nc_timestamp_ns" -> ts_override)

      case DEVICE_EXEC(7):
          if phase==START: entry("device_core_idx"@0xa3143e); entry("exec_id"@0xa3144d);
                           entry("nc_timestamp_ns" -> ts_override); entry("model_id")
          if phase==STOP : entry("nc_timestamp_ns" -> ts_override)

      case CC_EXEC(8):
          if phase==START: entry("gid"@0xa31454); entry("op_id"@0xa31457);
                           entry("nc_timestamp_ns" -> ts_override); entry("exec_id")
                           if should_include_default_algo():            // 0x508630
                               entry("algorithm"@0xa3145c, cc_algo_name(enc_alg_type)) // 0x5082c0
          if phase==STOP : entry("nc_timestamp_ns" -> ts_override)

      case TENSOR_READ(9), TENSOR_WRITE(0xA), TENSOR_ALLOC(0x1B):
          entry("tensor_id"@0xa31465); entry("size"@0xa154e5)

      case TENSOR_READ_BATCH(0xB), TENSOR_WRITE_BATCH(0xC):
          entry("num_batches"@0xa3146e)

      case NUM_ERR(0xD /*INSTANT*/):
          entry("device_core_idx"); entry("exec_id"); entry("nc_timestamp_ns" -> ts_override);
          entry("model_id")

      case ENC_BARRIER_RECV(0xE), ENC_BARRIER_SEND(0xF), ENC_BARRIER(0x10):
          entry("peer_id"@0xa31479); entry("exec_id"); entry("nec_dev_id"@0xa31480)

      case EXEC_PRE(0x11) .. EXEC_POST(0x14):
          entry("exec_id"); entry("model_id")

      case MODEL_SWITCH(0x15):
          entry("device_core_idx"); entry("is_neuron_core_work"@0xa3148a); entry("model_id")

      case MODEL_SUBMIT(0x16):
          entry("model_id")

      case ASYNC_SEMA_WAIT(0x17):
          entry("available_request_slots"@0xa3149d); entry("has_work"@0xa152a9)

      case HW_NOTIFY(0x18):
          entry("device_core_idx"); entry("hint"@0xa154e9); entry("custom_data"@0xa314b4);
          entry("nc_timestamp_ns" -> ts_override); entry("exec_id")

      case TIMESTAMP_SYNC(0x19):
          entry("exec_id"); entry("nc_timestamp_ns" -> ts_override);
          entry("cpu_timestamp_ns"@0xa30ecb)

      case NC_MODEL_SWITCH(0x1A):
          if phase==START: entry("device_core_idx"); entry("model_id"); entry("exec_id");
                           entry("nc_timestamp_ns" -> ts_override)
          if phase==STOP : entry("nc_timestamp_ns" -> ts_override)

      case TENSOR_FREE(0x1C):
          entry("tensor_id"); entry("size"); entry("ref_count"@0xa314bf)

      case TENSOR_BLOCK(0x1D):
          entry("tensor_id"); entry("pending_reader_count"@0xa314c8);
          entry("pending_writer_count"@0xa314dc)

      case DMEM_BUF_COPYIN(0x1E), DMEM_BUF_COPYOUT(0x1F):
          entry("mem_type"@0xa152b9); entry("mem_handle"@0xa314f0);
          entry("offset"@0xa314fa); entry("size")

      case DMEM_BUF_BATCH_COPYIN(0x20), DMEM_BUF_BATCH_COPYOUT(0x21):
          entry("mem_type"); entry("num_batches");
          entry("total_num_ops"@0xa31500); entry("total_size"@0xa3150d)

      case DMA_MEM_ALLOC(0x22):
          if phase==INSTANT:
              entry("hbm_idx"@0xa31517); entry("is_nc_hbm"@0xa3151e);
              entry("is_aligned"@0xa31527); entry("mla_idx"@0xa31531);
              entry("usage_type"@0xa31538, dma_mem_usage_name(usage_type))   // 0x508480
          if phase==STOP:
              entry("allocated_bytes"@0xa31542); entry("total_bytes"@0xa31551);
              entry("usage_type_total_bytes"@0xa3155c); entry("return_code"@0xa31572);
              entry("physical_address"@0xa30edb, fmt_lower_hex_u64)           // "0x%x"

      case DMA_MEM_DEALLOC(0x23):
          if phase==INSTANT:
              entry("hbm_idx"); entry("is_nc_hbm"); entry("mla_idx");
              entry("usage_type"); entry("physical_address")
          if phase==STOP:
              entry("freed_bytes"@0xa3157f); entry("total_bytes"); entry("usage_type_total_bytes")

      case CONSUME_TPB_STATUS(0x24):
          entry("exec_id")
      case CONSUME_CC_CORE(0x25), NOTIF_ERRORS(0x26), GPSIMD_STDIO(0x27):
          entry("exec_id"); entry("model_id")

      case HOSTCC_PREPARE(0x28):
          entry("ctx_device_id"@0xa3158a); entry("ctx_device_count"@0xa30eeb);
          entry("cc_op"@0xa31597)
      case HOSTCC_SCHEDULE(0x29), HOSTCC_BUILD_CTX(0x2A):
          entry("ctx_device_id"); entry("ctx_device_count")
      case HOSTCC_LOAD(0x2B):
          entry("enc_op"@0xa3159c); entry("dtype"@0xa315a2); entry("num_elem")
      case HOSTCC_EXEC(0x2C):
          entry("stream_id"@0xa315a7)
      case HOSTCC_WAIT(0x2D):
          entry("total_notif_expected"@0xa315b0)

      case GENERIC(0), sentinel(0x2E): /* no fields */ ;

    // model lookups: where a variant carries model_id, the wrapper may also emit
    //   entry("model_name"@0xa315db, interned.find(model_id))   // hashbrown SwissTable
    // when the interned model map has an entry (LABEL_189 path).  [LOW — map semantic]

    PHASE_MISMATCH_FALLTHROUGH:
        // Debug-log "…for type {:?} phase {:?}" via SysTraceEventType/Phase::Debug::fmt
        //   (CamelCase pool @0xa3112f) + log::__private_api::log(WARN)
        ;                                              // no fields emitted
    m.end()                                            // writes '}'  (or '{}' on mismatch)
```

> **NOTE —** several keys reuse one `.rodata` string across arms (`model_id`@`0xa152b1`, `size`@`0xa154e5`, `exec_id`@`0xa3144d`, `nc_timestamp_ns`@`0xa3142f`, `device_core_idx`@`0xa3143e`). The `nc_timestamp_ns` key is special: emitting it *also* sets the `EventWrapper` timestamp override (§2). Two values feed a non-int monomorph: `usage_type` and `model_name`/`mem_type` are *strings* (entries @`0x5b30a0`/`0x5b3290`), and `physical_address`/`algorithm` are strings produced by a `LowerHex`/name formatter rather than the int itoa path — `physical_address` is rendered as `"0x…"`, not a JSON number.

> **GOTCHA —** the `custom_data` keys here are the **same names** the C++ `event_to_proto` @`0x9aec0` path uses for its `Map<string,string> AttributesEntry` in the NTFF protobuf ([ntff-format](ntff-format.md)). Two *independent* serializers — this serde_json JSON path and the protobuf path — serialize the *same* drained records under the *same* key names. A reimplementer maintaining both must keep the two key rosters in lockstep; they are not generated from a shared schema, so they can silently drift. `[HIGH — names byte-identical across both emitters]`

---

## 4. CompactFormatter and the CString Sink

### Purpose

serde_json offers two `Formatter`s: `CompactFormatter` (no whitespace) and `PrettyFormatter` (indentation + newlines). This path uses `CompactFormatter`, and — crucially for 1.0.145 — uses it as a **zero-sized monomorphization target**, not a heap trait object. The formatter's job reduces to writing structural bytes and escaping strings; everything else (integers, the sink growth) is inlined into each `serialize_entry`.

### CompactFormatter vs PrettyFormatter

```text
CompactFormatter (this path)          PrettyFormatter (NOT used)
  {"a":1,"b":2}                         {
                                          "a": 1,
                                          "b": 2
                                        }
  begin_object   -> write '{'           write '{' + push indent
  begin_object_key (not first) -> ','   write ",\n" + indent
  begin_object_value -> ':'             write ": "
  end_object     -> '}'                 "\n" + pop indent + '}'
```

`CompactFormatter`'s structural methods are pure single-byte writes — `{`=123, `}`=125, `[`=91, `]`=93, `:`=58, `,`=44, `"`=34 — emitted **inline** by `collect_seq` / `serialize_entry` / the wrapper `serialize` bodies. There is no separate formatter object on the heap; the `Serializer` carries only the `Vec<u8>` writer (the `CompactFormatter` is a unit struct). The `,` separators are governed by a private `first` bool in each `SerializeMap`/`SerializeSeq` state (the `state` byte the entry receiver reads: write `,` before the key only when not the first entry).

### The growable byte sink

The writer is a `Vec<u8>`, accessed as a `RawVec` triple `[cap, ptr, len]` through a double pointer. Every byte write is the same three steps:

```c
// Inlined at every CompactFormatter byte write and itoa digit store.
function push_byte(vec, b):                 // vec -> RawVec{cap@+0, ptr@+8, len@+16}
    if vec.len == vec.cap:                  // full
        do_reserve_and_handle(vec, 1)       // raw_vec::reserve — amortized doubling
    *(vec.ptr + vec.len) = b
    vec.len += 1
```

Integers are formatted by an **inlined jeaiii/Lemire itoa**: a 200-byte LUT of `"00".."99"` ASCII pairs (`anon_…_9`) emits two decimal digits per table lookup, monomorphized into each int `serialize_entry` (so `id`/`timestamp_ns`/`tracking_id`/… each carry their own copy of the digit loop). There is no `write!`/`fmt::Display` call.

### String escaping

```c
// Models serde_json::ser::format_escaped_str @0x5b4140.  Writes a quoted JSON string.
function format_escaped_str(sink, s):
    push_byte(sink, '"')                              // 34
    run_start = 0
    for i in 0..s.len:
        c = s[i]
        e = ESCAPE[c]                                 // 0xa33478: 256-entry table
        if e == 0:  continue                          // 0 = no escape; extend the memcpy run
        if i > run_start:
            extend_from_slice(sink, s[run_start..i])  // flush the literal run (memcpy)
        if e == 'u' (0x75):                           // control char with no short form
            // emit "\u00" then two hex digits from HEX_DIGITS
            extend_from_slice(sink, "\\u00")          // const
            push_byte(sink, HEX_DIGITS[c >> 4])       // 0xa33578: "0123456789abcdef"
            push_byte(sink, HEX_DIGITS[c & 0xF])
        else:
            push_byte(sink, '\\'); push_byte(sink, e) // short escape: \" \\ \b \t \n \f \r
        run_start = i + 1
    if run_start < s.len:
        extend_from_slice(sink, s[run_start..])       // trailing literal run
    push_byte(sink, '"')                              // 34
```

The `ESCAPE` table @`0xa33478` is the standard serde_json 256-entry map (byte-verified): `0x08='b'`, `0x09='t'`, `0x0a='n'`, `0x0c='f'`, `0x0d='r'`, every other byte in `0x00..0x1f` = `'u'` (→ `\u00XX`), `0x22='"'`, `0x5c='\'`, all others `0`. `HEX_DIGITS` @`0xa33578` is the literal ASCII `"0123456789abcdef"`. Since all key names and enum display names are pure-ASCII identifiers, the common case is a single `extend_from_slice` of the whole run bracketed by two `"` bytes — no per-char escaping.

### The Vec<u8> → CString handoff

```c
// Models nrt_sys_trace_fetch_events_internal @0x5099a0 (the FFI return).
function fetch_events_internal(out_ptr, out_len, opts):
    if *out_ptr != NULL:  log_warn("…already populated…"); return ERR   // double-call guard
    s = fetch_events(SysTraceFetchOptions::from(opts))   // 0x5aa3b0; returns String{ptr,cap,len}
    bytes = s.into_bytes()                                // Vec<u8>, no trailing NUL yet
    if memchr(0x00, bytes) is Some:                       // interior NUL scan
        // CString::new would fail; the unchecked path requires NUL-free input
        panic / error                                     // unwrap_failed surface
    cstr = CString::_from_vec_unchecked(bytes)            // appends one NUL; takes ownership
    *out_len = cstr.len() - 1                             // length WITHOUT the NUL terminator
    *out_ptr = cstr.into_raw()                            // ptr crosses the C ABI; C now owns it
    return OK
```

> **GOTCHA —** `*out_len` is the byte length **minus one** — i.e. the JSON length *excluding* the NUL terminator that `CString` appends. The buffer the C caller receives is NUL-terminated (safe for `strlen`/`printf`), but `out_len` counts only the JSON bytes. A reimplementation that returns the `CString`'s full length (including the NUL) will report one byte too many and may double-count the terminator. The buffer must later be freed through `nrt_sys_trace_buffer_free` @`0xb9910` → internal @`0x509650`, which reconstructs the `CString` via `CString::from_raw` and `__rust_dealloc`s it — **not** `free()`, because the allocation came from Rust's global allocator. `[HIGH]`

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `format_escaped_str` | `0x5b4140` | quoted-string writer; `ESCAPE`-driven escaping (this-path monomorph) | HIGH |
| `serde_json::ser::ESCAPE` | `0xa33478` (256 B) | escape-class table; `'b'/'t'/'n'/'f'/'r'`, else `'u'` for `<0x20` | HIGH |
| `Formatter::write_char_escape::HEX_DIGITS` | `0xa33578` (16 B) | `"0123456789abcdef"` for `\u00XX` | HIGH |
| `Serializer::collect_seq` | `0x5b2f80` | `'['`…`']'` + `,` separation | HIGH |
| `serialize_entry` (u64) | `0x5b3f30` | `id`/`nc_local_id`/`tracking_id`/`thread_id`/`timestamp_ns`/`nrta_seq_id` | HIGH |
| `serialize_entry` (u32) | `0x5b3640` | `nc_idx`/`data_version` + many u32 custom keys | HIGH |
| `serialize_entry` (i32) | `0x5b3b70` | `worker_gid` (signed, `-1` sentinel) | HIGH |
| `serialize_entry` (EventDataWrapper) | `0x5b3d90` | the `"data"` key → `EventDataWrapper::serialize` | HIGH |
| `serialize_entry` (SysTraceEventType) | `0x5b3e60` | the `"event_type"` key → enum `serialize` | HIGH |
| `serialize_entry` (SysTraceEventPhase) | `0x5b3420` | the `"phase"` key → enum `serialize` | HIGH |
| `serialize_entry` (str/Cow) | `0x5b30a0` / `0x5b3290` | `model_name`/`usage_type` · `physical_address`/`algorithm` | MEDIUM (value-type inferred from operand widths) |
| `nrt_sys_trace_buffer_free` | `0xb9910` → `0x509650` | `CString::from_raw` + `__rust_dealloc` | HIGH |

> **NOTE —** the per-key int `serialize_entry` monomorphs are distinguished only by value width and signedness (the key is always an escaped `&str`). The u64 path has two monomorphs (`0x5b3f30` for the 11-key object, `0x5b3830`/`0x5b3a00` for rarer custom fields); the value-type labels for the custom-field monomorphs are inferred from operand widths, not from a DWARF type, hence MEDIUM. `[MED — monomorph value-type labels]`

---

## Related Components

| Name | Relationship |
|---|---|
| `capture::drain_events` @`0x5afea0` | fills the `Vec<event>` this path serializes (the ring drain) — [rust-capture](rust-capture.md) |
| `event_to_proto` @`0x9aec0` | the *other* serializer of the same records (protobuf, not JSON) — shares the custom_data key names — [ntff-format](ntff-format.md) |
| `vnc_idx_to_gid` @`0x5aaca0` | builds the `worker_gid` map keyed by `nc_idx` (BOUNDARY enc/vtpb) |
| `get_all_interned_strings` @`0x5b19e0` | supplies the `model_id → model_name` map for the `model_name` key — [interned-strings](../runtime/interned-strings.md) |
| `cc_algo_name` @`0x5082c0` · `dma_mem_usage_name` @`0x508480` | name helpers feeding `algorithm` / `usage_type` (BOUNDARY enc/tdrv) |

## Cross-References

- [neuron_rustime: sys_trace Capture Engine](rust-capture.md) — producer (2)'s ring engine; `fetch_events` drains the rings this serializer reads; the boundary that owns `drain_events`, the 112-byte record, and the gid/interner sources
- [SysTraceEventType Taxonomy (46 Variants)](event-taxonomy.md) — the 46-variant discriminant whose names this path stringifies, and the 40-byte `data` union whose members the per-variant `custom_data` keys project
- [Interned String Database](../runtime/interned-strings.md) — the `string_db` SipHash interner that resolves `model_id → model_name`; ids share the 50-bit space with `tracking_id`
- [NTFF Trace File Format (ntff.proto)](ntff-format.md) — the parallel protobuf serializer (`event_to_proto`) of the same records; the `custom_data` key names map 1:1 to its `Map<string,string>` attributes
- [back to index](../index.md)
