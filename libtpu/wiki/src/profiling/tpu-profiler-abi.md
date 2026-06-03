# TpuProfiler ABI

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

`TpuProfiler_*` is libtpu's **legacy** profiler C-ABI: five exported C symbols — `TpuProfiler_Create`, `TpuProfiler_Start`, `TpuProfiler_Stop`, `TpuProfiler_CollectData`, `TpuProfiler_Destroy` — compiled from `learning/45eac/tfrc/executor/stream_executor/tpu_profiler_c_api.cc` (the source path is baked into every `LogMessage` site). This is the TensorFlow / stream-executor era profiler entry, the surface behind `tensorflow/core/tpu/c_api_decl.h`, reached by TF and TPUEstimator through `stream_executor::tpu::ProfilerApiFn()` @ `0x10900EA0`. It predates and runs in parallel with the modern [PJRT Profiler extension](../pjrt/ext-profiler.md) (`PLUGIN_Profiler_*`, extension type 1) and shares the exact same backend — a `tsl::profiler::ProfilerCollection` of host and device sub-profilers built by the global factory registry — but with a deliberately leaner ABI.

The two surfaces differ in three structural ways, and a reimplementer who has read the PJRT page must internalize all three. First, **error handling**: the legacy ABI has *no* `Error_Destroy`/`Error_Message`/`Error_GetCode` helpers and no `PLUGIN_Profiler_Error` object. Failures are written into a caller-supplied `TF_Status*` via `TSL_SetStatus`, exactly as the rest of the C-API for TF expects. Second, **the handle is 120 bytes** (`operator new(0x78)`), not 128 — it omits the PJRT handle's trailing state-byte padding, placing its one state byte at `+112`. Third, **`CollectData` serializes straight into the caller's buffer** and then calls `XSpace::Clear` on the inline `XSpace` — there is no owned serialization vector and no borrowed-buffer lifetime to track, unlike the PJRT path's internal `std::vector<uint8_t>`.

This page owns the **legacy `TpuProfiler_*` roster** (each entry as a row: signature, the `ProfilerCollection` vtable slot it bounces to, and the byte-level handle access it performs), the **`ProfilerApiFn` dispatch table** that hosts them for stream-executor, and the **`xprof::tpu::TpuProfilerImpl` device sub-profiler** that the shared collection fans out to. The PJRT-side struct/vtable, the `CreateProfilers` factory walk, and the XSpace wire format are owned elsewhere — linked, not duplicated.

For reimplementation, the contract is:

- The five exported `TpuProfiler_*` C functions, their calling conventions, and the `TF_Status*`-out error model.
- The 120-byte legacy handle layout and its single state byte at `+112`.
- The `ProfilerCollection` vtable offsets the entries call (`+16` Start, `+24` Stop, `+32` CollectData, `+8` destroying dtor).
- The destructive, query/fetch two-mode `CollectData` contract and the `XSpace::Clear`-on-success reset.
- The `TpuProfilerImpl` device sub-profiler that produces the device XPlanes inside that collection.

| | |
|---|---|
| **Source unit** | `learning/45eac/tfrc/executor/stream_executor/tpu_profiler_c_api.cc` |
| **Entries** | `TpuProfiler_Create` `0xEF33BC0` · `_Start` `0xEF33EA0` · `_Stop` `0xEF34080` · `_CollectData` `0xEF34240` · `_Destroy` `0xEF33DE0` |
| **Dispatch table** | `stream_executor::tpu::ProfilerApiFn()::profiler_api_fn` (accessor @ `0x10900EA0`) |
| **Handle** | 120 bytes (`operator new(0x78)`); state byte at `+112` |
| **Backend** | `tsl::profiler::ProfilerCollection*` at handle `+104`; vtable @ `0x217738A0` |
| **Device sub-profiler** | `xprof::tpu::TpuProfilerImpl` (`Start` `0xEF347E0`, `Stop` `0xEF34820`, `CollectData` `0xEF34860`) |
| **Error model** | `TF_Status*`-out via `TSL_SetStatus`; no `Error_*` helpers |
| **Output** | serialized `tensorflow.profiler.XSpace` into the caller's buffer |

---

## The Legacy Handle

Every entry past `Create` operates on an opaque 120-byte handle. It is the legacy twin of the PJRT [`PLUGIN_Profiler`](../pjrt/ext-profiler.md#the-handle-and-the-error-object) handle, but eight bytes shorter and with the `XSpace` serialized directly rather than buffered. The offsets below are read directly from the entry bodies: `Create` does `operator new(0x78)`, zero-fills, then writes `+112 = 1` and `+104 = collection`; `CollectData` reads `+88` (the constructed flag) and writes `+96` (cached size); `Destroy` reads `+104`/`+88`.

| Field | Off | Type | Meaning | Confidence |
|---|---|---|---|---|
| `xspace` | `+0x00` | `tensorflow::profiler::XSpace` (88 B) | inline proto2 message; constructed lazily by the first `CollectData` | HIGH |
| `xspace_constructed` | `+0x58` (88) | `uint8_t` | `1` iff `XSpace::XSpace()` has run; gates `~XSpace` in Destroy and the lazy-init in CollectData | CERTAIN |
| `cached_xspace_size` | `+0x60` (96) | `size_t` | `XSpace::ByteSizeLong()` from the collection drain; the size reported to the caller | CERTAIN |
| `collection` | `+0x68` (104) | `tsl::profiler::ProfilerCollection*` | owned backend; vtable-dispatched and freed (vtable `+8`) in Destroy | CERTAIN |
| `state` | `+0x70` (112) | `uint8_t` | `1` = created/stopped (not running); `0` = running. Set `1` by Create, `0` by successful Start, `1` by successful Stop | CERTAIN |

> **QUIRK —** the legacy state byte at `+112` is the *inverse polarity* of the PJRT handle's `ready` byte and one slot further along. PJRT writes `ready = 1` at Create and `ready = 0` on Start (and leaves it `0` on Stop); legacy writes `state = 1` at Create, `0` on Start, and back to `1` on Stop. The legacy byte is therefore a clean two-state "is it running?" flag with no overload — Start guards on `state != 0`, Stop guards on `state != 1`, and Stop *restores* the byte to `1`. A reimplementer porting the PJRT state machine onto the legacy handle must not copy PJRT's overloaded `0`-means-both-running-and-stopped encoding.

> **GOTCHA —** there is **no** owned `std::vector<uint8_t>` in the legacy handle. The PJRT handle holds a heap vector pointer at `+0x60` whose lifetime the caller must respect; the legacy handle has nothing at the corresponding slot (`+96` holds the cached *size*, not a pointer). `TpuProfiler_CollectData` serializes the `XSpace` into the *caller's* buffer and immediately `XSpace::Clear`s the inline message. There is no borrowed-buffer-valid-until-next-call rule here — the caller owns the bytes the instant `CollectData` returns. A reimplementation that ports the PJRT vector field will misalign every offset and leak.

---

## `TpuProfiler_Create` — `0xEF33BC0`

### Purpose

Allocates the 120-byte handle, builds the shared `ProfilerCollection` from a default-constructed `ProfileOptions`, and returns the handle through the first out-parameter. This is the only entry that touches the factory registry.

### Signature

```c
/* learning/.../tpu_profiler_c_api.cc:27 */
void TpuProfiler_Create(TpuProfiler** out_handle, TF_Status* status);
```

> **NOTE —** unlike PJRT's `Create`, the legacy `Create` takes **no** `serialized_options`. It default-constructs `tensorflow::ProfileOptions(0)` on the stack and feeds that to `CreateProfilers`. The TF profiler service configures the actual capture levels by registering its own factory (via `tsl::profiler::RegisterProfilerFactory` @ `0x1CF50780`) rather than passing options through this entry. The factory lambdas read whatever options the *PJRT* path parsed when both surfaces are live; on the pure-TF path the registered factories use their own defaults.

### Algorithm

```c
function TpuProfiler_Create(out_handle, status):                 // 0xEF33BC0
    VLOG(1) "TpuProfiler Create"                                 // tpu_profiler_c_api.cc:27
    ProfileOptions opts(0);                                      // stack, default
    h = operator new(0x78);                                      // 120-byte handle
    memzero(h, 0x78);                                            // vxorps + 4x vmovups
    h->state = 1;                                                // *(BYTE*)(h+112) = 1
    profs = tsl::profiler::CreateProfilers(opts);                // 0x1CF50860 — factory walk under mu
    coll  = operator new(0x20);                                  // 32-byte ProfilerCollection
    ProfilerCollection::ProfilerCollection(coll, &profs);        // 0xF6A15E0 — moves vector in
    drop_each(profs);  free(profs.data);                         // empty out the moved-from vector
    h->collection = coll;                                        // *(QWORD*)(h+104) = coll
    *out_handle = h;
    set_ok(status);                                              // status rep -> inline OK marker
    ~ProfileOptions(opts);
```

The two `drop_each` loops in the decompiled body release any `unique_ptr<ProfilerInterface>` left dangling in the moved-from vector (calling each element's vtable `+8` dtor), then `free` the vector storage — standard `std::vector` move-cleanup, not an error path. `set_ok` is the canonical `absl::Status` "if the rep differs from the inline-OK marker `&dword_0 + 1`, swap it in and `Unref` the old heap rep" sequence shared by every legacy entry.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `TpuProfiler_Create` | `0xEF33BC0` | entry; allocate handle + build collection | CERTAIN |
| `tsl::profiler::CreateProfilers` | `0x1CF50860` | factory walk under global `mu` | CERTAIN |
| `tsl::profiler::ProfilerCollection::ProfilerCollection(vector)` | `0xF6A15E0` | move sub-profiler vector inline | CERTAIN |

---

## `TpuProfiler_Start` — `0xEF33EA0`

### Purpose

Resets any prior `XSpace`, drives `ProfilerCollection::Start` (vtable `+16`), and flips the state byte to "running". Idempotent against a double-start.

### Signature

```c
/* tpu_profiler_c_api.cc:47 */
void TpuProfiler_Start(TpuProfiler* handle, TF_Status* status);
```

### Algorithm

```c
function TpuProfiler_Start(h, status):                           // 0xEF33EA0
    VLOG(1) "TpuProfiler Start"                                  // :47
    if (h->state == 0):                                          // already running
        VLOG(2) "TpuProfiler has already been started."          // :49
        return                                                   // no error, no-op
    if (h->xspace_constructed == 1):                             // tear down stale XSpace
        XSpace::~XSpace(&h->xspace);
        h->xspace_constructed = 0;
    h->cached_xspace_size = 0;                                   // *(QWORD*)(h+96) = 0
    s = (*h->collection->vtable[+16])(h->collection);            // ProfilerCollection::Start 0xF6A1640
    write_status(status, s);                                     // ref/unref swap into TF_Status
    if (s == OK):
        h->state = 0;                                            // mark running
    else:
        LOG "<status>" at :57                                    // log the failure status
```

> **QUIRK —** `Start` proactively destroys a previously-constructed `XSpace` (the `+88` flag) and zeroes the cached size *before* starting. This is the legacy handle's reuse path: because `CollectData` does `XSpace::Clear` (not `~XSpace`) after serializing, the inline message survives in a cleared-but-constructed state, and a subsequent `Start` must fully tear it down so the next `CollectData` reconstructs a fresh one. The PJRT handle never reuses this way — it has no equivalent reset in its `Start`.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `TpuProfiler_Start` | `0xEF33EA0` | entry; reset + Start + state flip | CERTAIN |
| `ProfilerCollection::Start` | `0xF6A1640` | vtable `+16`; fan out Start to sub-profilers | CERTAIN |

---

## `TpuProfiler_Stop` — `0xEF34080`

### Purpose

Drives `ProfilerCollection::Stop` (vtable `+24`) and restores the state byte to "stopped". Idempotent against a stop-before-start.

### Signature

```c
/* tpu_profiler_c_api.cc:65 */
void TpuProfiler_Stop(TpuProfiler* handle, TF_Status* status);
```

### Algorithm

```c
function TpuProfiler_Stop(h, status):                            // 0xEF34080
    VLOG(1) "TpuProfiler Stop"                                   // :65
    if (h->state == 1):                                          // never started / already stopped
        VLOG(2) "TpuProfiler has already been stopped."          // :67
        return                                                   // no error, no-op
    s = (*h->collection->vtable[+24])(h->collection);            // ProfilerCollection::Stop 0xF6A16C0
    write_status(status, s);
    if (s == OK):
        h->state = 1;                                            // mark stopped
    else:
        LOG "<status>" at :73
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `TpuProfiler_Stop` | `0xEF34080` | entry; Stop + state restore | CERTAIN |
| `ProfilerCollection::Stop` | `0xF6A16C0` | vtable `+24`; fan out Stop to sub-profilers | CERTAIN |

---

## `TpuProfiler_CollectData` — `0xEF34240`

### Purpose

The drain + serialize entry, and the most complex of the five. It pulls the device/host planes from the collection into the inline `XSpace`, reports the serialized size, and — when given a buffer — serializes into it and clears the message. It exposes a query/fetch two-mode contract through its size pointer, and a buffer-too-small precondition check.

### Signature

```c
/* tpu_profiler_c_api.cc:85 */
void TpuProfiler_CollectData(TpuProfiler* handle,
                             TF_Status*   status,
                             uint8_t*     buffer,            /* may be NULL = query */
                             size_t*      size_in_bytes);    /* in/out; must be non-NULL */
```

### Algorithm

```c
function TpuProfiler_CollectData(h, status, buffer, size_io):    // 0xEF34240
    if (size_io == NULL):                                        // :85
        TSL_SetStatus(status, 3 /*INVALID_ARGUMENT*/,
                      "size_in_bytes cannot be null.");
        LOG <status>; return

    if (h->xspace_constructed == 0):                             // first call: actually drain
        XSpace::XSpace(&h->xspace, 0);                           // construct inline message
        h->xspace_constructed = 1;
        VLOG(1) "TpuProfiler CollectData"                        // :91
        s = (*h->collection->vtable[+32])(h->collection,
                                          &h->xspace);          // ProfilerCollection::CollectData 0xF6A1740
        write_status(status, s);
        if (s != OK):
            LOG <status> at :95; *size_io = 0; return
        h->cached_xspace_size = XSpace::ByteSizeLong(&h->xspace);// *(QWORD*)(h+96)
        VLOG(2) "Number of XPlanes: " << h->xspace.planes_count  // :101

    requested = *size_io;                                        // caller's buffer capacity
    *size_io  = h->cached_xspace_size;                           // report true size (in/out)
    if (buffer == NULL):                                         // QUERY MODE: size only
        return
    if (requested < cached_xspace_size):                         // FETCH MODE, buffer too small
        TSL_SetStatus(status, 9 /*FAILED_PRECONDITION*/,
            "Buffer provided was smaller than requested profile data. "
            "buffer size=<req> bytes, profile data size=<size> bytes.");  // :123
        LOG <status>; return
    VLOG(2) "Serializing XSpace to buffer."                       // :127
    CHECK(h->xspace_constructed);                                 // ud2 guard
    XSpace::SerializePartialToArray(&h->xspace, buffer, cached_xspace_size);
    CHECK(h->xspace_constructed);                                 // ud2 guard
    XSpace::Clear(&h->xspace);                                    // reset message (NOT ~XSpace)
    set_ok(status)
```

> **GOTCHA —** the size pointer is **in/out and mandatory**. A `NULL` `size_in_bytes` is an immediate `INVALID_ARGUMENT` (`TSL_SetStatus` code 3). On every successful path the entry overwrites `*size_in_bytes` with the true serialized size *before* checking the buffer. The intended call sequence is two-pass: call once with `buffer = NULL` to read the size into `*size_in_bytes`, allocate, then call again with the buffer and the now-known capacity. A single call with an undersized buffer yields `FAILED_PRECONDITION` (code 9) and the message string above — it does *not* truncate.

> **QUIRK —** the drain is **one-shot, but the reporting is repeatable**. The expensive `ProfilerCollection::CollectData` (vtable `+32`) runs only on the first call (gated by `xspace_constructed == 0`); subsequent calls skip straight to reporting `cached_xspace_size` and re-serializing from the inline `XSpace`. But because the success path ends with `XSpace::Clear`, a second *fetch* re-serializes a now-empty message (size still reflects the cached value, content is empty). The PJRT path differs: it keeps the populated XSpace cached in a vector and returns identical *non-empty* bytes on repeat. A reimplementer must not assume the legacy second-fetch returns the same payload — it returns the cached *size* over a *cleared* message.

> **NOTE —** unlike PJRT's `CollectData`, this entry serializes from the handle's **inline** `XSpace` (the message at `+0`), not from a stack-local one. `ProfilerCollection::CollectData` @ `0xF6A1740` is handed `&h->xspace` directly so each sub-profiler appends its planes into the persistent message; the `Clear` at the end is what makes the message reusable across a Start/Stop/CollectData cycle.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `TpuProfiler_CollectData` | `0xEF34240` | entry; drain + size report + serialize | CERTAIN |
| `ProfilerCollection::CollectData(XSpace*)` | `0xF6A1740` | vtable `+32`; fan out drain into shared XSpace | CERTAIN |
| `TSL_SetStatus` | (TF C-API) | write `absl::Status` into `TF_Status*` | CERTAIN |
| `XSpace::SerializePartialToArray` | (proto2) | serialize message into caller buffer | CERTAIN |
| `XSpace::Clear` | (proto2) | reset inline message after fetch | CERTAIN |

---

## `TpuProfiler_Destroy` — `0xEF33DE0`

### Purpose

Tears down the collection (destroying every sub-profiler), tears down the inline `XSpace` if it was ever constructed, and frees the 120-byte handle. The simplest entry; takes no status.

### Signature

```c
/* tpu_profiler_c_api.cc:40 */
void TpuProfiler_Destroy(TpuProfiler* handle);
```

### Algorithm

```c
function TpuProfiler_Destroy(h):                                 // 0xEF33DE0
    VLOG(1) "TpuProfiler Destroy"                                // :40
    if (h == NULL): return
    coll = h->collection;  h->collection = NULL;                 // h+104
    if (coll):
        (*coll->vtable[+8])(coll);                               // destroying dtor 0xF6A18E0 — frees coll
    if (h->xspace_constructed == 1):                             // h+88
        XSpace::~XSpace(&h->xspace);
    free(h);
```

> **NOTE —** `Destroy` takes **no `TF_Status*`** — it cannot fail and returns `void`. It frees the collection via its *destroying* dtor (`vtable +8`, `~ProfilerCollection` D0 @ `0xF6A18E0`), which both destructs and `operator delete`s the 32-byte object. There is no serialized-vector free here (the legacy handle has none), so the only conditional cleanup is the inline `XSpace` gated on `+88`.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `TpuProfiler_Destroy` | `0xEF33DE0` | entry; tear down collection + XSpace + handle | CERTAIN |
| `ProfilerCollection::~ProfilerCollection` (D0) | `0xF6A18E0` | vtable `+8`; destroy + free collection | CERTAIN |

---

## The `ProfilerApiFn` Dispatch Table

`stream_executor::tpu::ProfilerApiFn()` @ `0x10900EA0` is a trivial accessor — its entire body is `return &ProfilerApiFn()::profiler_api_fn;`. The returned object is a function-local static table populated at first use with pointers to the five `TpuProfiler_*` exports. This is the shim stream-executor's TPU backend uses to reach the legacy ABI through a stable struct, the same way the PJRT side reaches `PLUGIN_Profiler_Api` through the [extension node](../pjrt/ext-profiler.md#the-extension-node).

```text
stream_executor::tpu::ProfilerApiFn()         @ 0x10900EA0  (accessor)
  └─ returns &profiler_api_fn  (function-local static)
        profiler_api_fn[0] ── &TpuProfiler_Create       0xEF33BC0
        profiler_api_fn[1] ── &TpuProfiler_Start        0xEF33EA0
        profiler_api_fn[2] ── &TpuProfiler_Stop         0xEF34080
        profiler_api_fn[3] ── &TpuProfiler_CollectData  0xEF34240
        profiler_api_fn[4] ── &TpuProfiler_Destroy      0xEF33DE0
```

| Property | Value | Confidence |
|---|---|---|
| Accessor symbol | `stream_executor::tpu::ProfilerApiFn()` | CERTAIN |
| Accessor addr | `0x10900EA0` | CERTAIN |
| Backing storage | `ProfilerApiFn()::profiler_api_fn` (function-local static) | CERTAIN |
| Slot contents | the five `TpuProfiler_*` function pointers | HIGH |
| Slot ordering / extra metadata | Create/Start/Stop/CollectData/Destroy order; any trailing metadata fields not individually traced | LOW |

> **NOTE —** the accessor returns a *pointer into* the static table; the table itself is filled by the function-local static's guarded initializer on first call. The exact byte layout (whether the table carries trailing version/metadata fields beyond the five pointers) was not individually decoded — the slot ordering matches the export order and the call sites in stream-executor's TPU backend, hence HIGH confidence on the five entries, LOW on any extra fields.

---

## The `TpuProfilerImpl` Device Sub-Profiler

`xprof::tpu::TpuProfilerImpl` is **not** the per-handle collector — that role belongs to `ProfilerCollection`, the same object both ABIs hold. `TpuProfilerImpl` is one `tsl::profiler::ProfilerInterface` *member* of that collection: the device-side profiler that talks to xdb (the TPU debugger) and turns the per-chip trace stream into device XPlanes. Both `TpuProfiler_*` and `PLUGIN_Profiler_*` reach it identically — the collection iterates its inner vector and calls the matching vtable slot on each member, of which `TpuProfilerImpl` is one. Its object layout (read from the three method bodies): `+8` = inner profiler/RPC handle, `+16` = Start timestamp, `+24` = Stop timestamp.

### Algorithm

```c
function TpuProfilerImpl::Start(this):                           // 0xEF347E0
    this[+16] = absl::GetCurrentTimeNanos();                     // record start time
    inner = this[+8];
    if (!inner): return OK
    s = (*inner->vtable[+24])(inner);                            // inner profiler Start
    xla::profiler::AddPluginMetadata(inner);                     // stamp build CL/timestamp
    return s

function TpuProfilerImpl::Stop(this):                            // 0xEF34820
    inner = this[+8];
    s = inner ? (*inner->vtable[+32])(inner) : OK;               // inner profiler Stop
    this[+24] = absl::GetCurrentTimeNanos();                     // record stop time
    return s

function TpuProfilerImpl::CollectData(this, XSpace* xs):         // 0xEF34860
    arena = xs->arena;                                           // proto2 arena (tagged ptr at xs+8)
    resp  = Arena::DefaultConstruct<xprof::XprofResponse>(arena);
    if (this[+16] && this[+24]):                                 // both timestamps present
        resp.start_time_ns = this[+16]; set_bit 0x100000;        // resp+296, presence @ resp+16
        resp.stop_time_ns  = this[+24]; set_bit 0x8100000;       // resp+336
    inner = this[+8];
    if (inner):
        rc = (*inner->vtable[+40])(inner, resp);                 // fill response from chip drain
        this[+8] = NULL;                                         // drop inner — one-shot
        (*inner->vtable[+8])(inner);                             // inner destroying dtor
        if (rc != OK) { ... return rc; }                         // free arena-less resp, propagate
    wrap = XprofResponseWrapper(resp, arena);
    xprof::ConvertResponseToTpuXSpace(wrap, xs, ...);            // append device XPlanes into xs
    xprof::DropExcessBytes(xs);                                  // trim oversized payloads
    ~XprofResponseWrapper(wrap);
    return OK
```

`TpuProfilerImpl::CollectData` is where the device XPlanes are born: it stamps the session start/stop timestamps into an `xprof::XprofResponse`, asks its inner profiler (vtable `+40`) to fill that response from the per-chip trace buffers, then `ConvertResponseToTpuXSpace` translates the response into XEvents/XStats appended onto the shared `XSpace`, and `DropExcessBytes` trims the result. The one-shot semantics are local here too — the inner profiler is nulled and destroyed after the single drain (mirroring `ProfilerCollection`'s vector-clear). The per-chip-family decode that feeds `XprofResponse` and the XEvent shaping are owned by the [trace-entry codec](trace-entries-coder.md) and [XPlane emission](xplane-xstat-traceme.md) pages.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `xprof::tpu::TpuProfilerImpl::Start` | `0xEF347E0` | record start ns; inner Start + `AddPluginMetadata` | CERTAIN |
| `xprof::tpu::TpuProfilerImpl::Stop` | `0xEF34820` | inner Stop; record stop ns | CERTAIN |
| `xprof::tpu::TpuProfilerImpl::CollectData(XSpace*)` | `0xEF34860` | drain → `XprofResponse` → device XPlanes | CERTAIN |
| `xprof::tpu::TpuProfilerImpl::~TpuProfilerImpl` (D2/D0) | `0xEF34DC0` / `0xEF34E00` | base / destroying dtor | CERTAIN |
| `xla::profiler::AddPluginMetadata` | `0xF3165C0` | stamp libtpu build CL/timestamp as an XStat | CERTAIN |
| `xprof::ConvertResponseToTpuXSpace` | (in `CollectData`) | response → device-plane XEvents | HIGH |

> **GOTCHA —** do not conflate `TpuProfilerImpl` with the handle's collector. The 120-byte legacy handle's `+104` field is a `ProfilerCollection*` whose vtable slots `+16/+24/+32` are *its* Start/Stop/CollectData; that collection's inner vector holds `TpuProfilerImpl` (device), `HostTracer` (host), `ThreadpoolProfilerInterface` (host threadpool), and whatever else the factory registry built. `TpuProfilerImpl`'s own `+8` field is yet another level down — its inner per-chip RPC profiler. Three distinct objects, three distinct vtables; the entries on this page only ever touch the top one (`ProfilerCollection`).

---

## Relationship to the PJRT Profiler Extension

Both ABIs are thin marshallers over one `ProfilerCollection`. The table below is the side-by-side a reimplementer needs; the PJRT column is owned by [PJRT Profiler Extension](../pjrt/ext-profiler.md).

| Aspect | Legacy `TpuProfiler_*` (this page) | PJRT `PLUGIN_Profiler_*` |
|---|---|---|
| Discovery | `stream_executor::tpu::ProfilerApiFn()` table | `PJRT_Profiler_Extension` (type 1) → `PLUGIN_Profiler_Api` vtable |
| Consumer | TF, TPUEstimator, stream-executor | JAX, PyTorch-XLA via `PjRtCApiClient::Profile()` |
| Error model | `TF_Status*`-out via `TSL_SetStatus`; no error helpers | `PLUGIN_Profiler_Error*` return + 3 `Error_*` helpers |
| Create options | none (default `ProfileOptions(0)`) | `serialized_options` (`tensorflow.ProfileOptions` proto) |
| Handle size | 120 bytes (`new 0x78`) | 128 bytes (`new 0x80`) |
| State byte | `+112`, two-state (`1`=stopped, `0`=running) | `+0x78`, overloaded (`0`=running *and* stopped) |
| `CollectData` output | serialize into caller buffer; `XSpace::Clear` | serialize into owned `std::vector<uint8_t>`; borrowed buffer |
| Backend | shared `ProfilerCollection` (vtable @ `0x217738A0`) | same `ProfilerCollection` |
| Output format | `tensorflow.profiler.XSpace` | same |

> **QUIRK —** the two surfaces produce the *same* set of sub-profilers because they share the global factory registry. TF registers its profiler via `tsl::profiler::RegisterProfilerFactory` @ `0x1CF50780`; `CreateProfilers` @ `0x1CF50860` (called by both `TpuProfiler_Create` and `PLUGIN_Profiler_Create`) walks that registry. A process that drives both ABIs gets two independent handles over two independent collections, each built from the same factory list — they do not share capture state, only the factory recipe.

---

## What Is Not Covered Here

- **The full `profiler_api_fn` static-table byte layout** — the five `TpuProfiler_*` pointers are confirmed by export order and call sites (HIGH); any trailing version/metadata fields were not individually decoded (LOW).
- **The inner per-chip RPC profiler behind `TpuProfilerImpl+8`** — its vtable (`+24` Start, `+32` Stop, `+40` CollectData, `+8` dtor) is exercised but the implementation class and its xdb wire protocol are out of scope.
- **`ConvertResponseToTpuXSpace` / `DropExcessBytes` internals** — the response-to-XPlane translation is owned by the [XPlane emission](xplane-xstat-traceme.md) and [trace-entry codec](trace-entries-coder.md) pages.
- **The exact StatType for `AddPluginMetadata`** — confirmed numeric `0xA6` (166) on the PJRT page; the interned string name was not resolved (LOW).

---

## Cross-References

- [Profiling and Telemetry — Overview](overview.md) — the capture → encode → decode → XPlane pipeline this ABI exports the tail of
- [PJRT Profiler Extension](../pjrt/ext-profiler.md) — the modern `PLUGIN_Profiler_*` twin: extension struct, 8-slot vtable, 128-byte handle, error helpers
- [PJRT_Profiler Extension](pjrt-profiler-extension.md) — profiling-section view of the `PLUGIN_Profiler_Api` factory/collector machinery
- [TraceEntriesCoder](trace-entries-coder.md) — the fixed-width device-trace codec that feeds `TpuProfilerImpl::CollectData`'s `XprofResponse`
- [XPlane / XStat / TraceMe Emission](xplane-xstat-traceme.md) — how `ConvertResponseToTpuXSpace` shapes the device XPlanes the `XSpace` output carries
