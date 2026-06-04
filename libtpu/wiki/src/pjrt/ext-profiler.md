# PJRT Profiler Extension (type 1)

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`), PJRT C-API **v0.103**. Other versions will differ.*

## Abstract

The Profiler extension is libtpu's bridge between the PJRT C-API and the TPU/xprof XSpace tracing backend. It is the canonical `PJRT_Profiler_Extension` (extension type id **1**) defined by openxla's `xla/pjrt/c/pjrt_c_api_profiler_extension.h`, hung off the [extension chain](extension-chain.md) and discovered by a framework — JAX, PyTorch-XLA — through a type-id scan of `PJRT_Api.extension_start`. Unlike the other 16 extensions, it carries no methods of its own in its node body; its single payload pointer leads to a separate `PLUGIN_Profiler_Api` vtable of 8 function pointers. That vtable is the real surface: three `absl::Status`-wrapping error helpers plus a five-call lifecycle — `Create → Start → Stop → CollectData → Destroy` — that drives a heap-allocated `PLUGIN_Profiler` handle.

The handle is a thin transport, not a tracer. Its work is delegated to a `tsl::profiler::ProfilerCollection` built by the global factory registry (`tsl::profiler::CreateProfilers`), which fans out to the device-side `xprof::tpu::TpuProfilerImpl` and host-side `HostTracer`/`ThreadpoolProfilerInterface` sub-profilers. Those sub-profilers capture host `tsl::profiler::TraceMe` events and per-chip hardware ring-buffer samples; `CollectData` flattens them into a `tensorflow.profiler.XSpace` protobuf and serializes it into a caller-borrowed byte buffer. The PJRT layer therefore adds zero per-event overhead — it parses `ProfileOptions` in, marshals XSpace bytes out, and owns the lifecycle state machine in between.

This page owns the **`PJRT_Profiler_Extension` node struct**, the **`PLUGIN_Profiler_Api` 8-slot vtable and its per-method args**, the **`PLUGIN_Profiler`/`PLUGIN_Profiler_Error` handle layouts**, the **Create/Start/Stop/CollectData/Destroy state machine**, and the **bridge into `ProfilerCollection`/`TraceMe`/XSpace**. The chain-node mechanics (header, walk, type ids) live on [Extension Chain](extension-chain.md). The deep XSpace/XPlane payload decode and the per-chip-family trace-point enums are the profiling section's territory — referenced here by symbol, not hard-linked.

For reimplementation, the contract is:

- The 40-byte `PJRT_Profiler_Extension` node and its `.data`-resident, relocation-populated nature (no `Create*Extension` call).
- The 80-byte `PLUGIN_Profiler_Api` vtable: the fixed 8-slot ordering and which slots validate `struct_size`.
- The 128-byte `PLUGIN_Profiler` handle layout and the overloaded one-byte state machine.
- The five lifecycle bodies: what each reads/writes, the destructive `CollectData`, the borrowed-buffer contract.
- The delegation into `tsl::profiler::ProfilerCollection` and the `CreateProfilers` factory walk under a global mutex.
- The relationship to the legacy `TpuProfiler_*` C-ABI sharing the same backend.

| | |
|---|---|
| **Extension node** | `pjrt::tpu_plugin::profiler_extension` @ `0x22255B98` (`.data`), 40 bytes (`0x28`), type id 1 |
| **Method vtable** | `pjrt::tpu_plugin::profiler_api` @ `0x22255B48` (`.data`), 80 bytes (`0x50`), 8 fn-ptrs |
| **Handle** | `PLUGIN_Profiler`, 128 bytes (`0x80`), `operator new(0x80)` in Create |
| **Error object** | `PLUGIN_Profiler_Error`, 8 bytes, wraps one `absl::Status` |
| **Backend** | `tsl::profiler::ProfilerCollection` (vtable @ `0x217738A0`), built by `CreateProfilers` @ `0x1CF50860` |
| **Output format** | serialized `tensorflow.profiler.XSpace` proto |
| **Chain role** | the `.data` seed; the type-1 terminator (`next = NULL` until patched) — see [Extension Chain](extension-chain.md) |

---

## The Extension Node

### Layout

The node is a [`PJRT_Extension_Base`](extension-chain.md) header followed by a single payload pointer and a trailing reserved qword. It is the smallest of the 17 extensions and the only one that lives in `.data` with its fields written at link time and patched by the dynamic linker — there is no `pjrt::Create*Extension` initializer for it.

```c
/* PJRT_Profiler_Extension @ 0x22255B98 — 40 bytes (0x28), type id 1.
 * Field order fixed by xla/pjrt/c/pjrt_c_api_profiler_extension.h. */
struct PJRT_Profiler_Extension {
    /* +0x00 */ size_t                struct_size;   /* = 0x28, written at link time      */
    /* +0x08 */ uint32_t              type;          /* = 1, written at link time         */
    /* +0x0C */ uint32_t              _pad0;         /* = 0                                */
    /* +0x10 */ PJRT_Extension_Base*  next;          /* NULL in .data image; patched at
                                                        runtime by GetTpuPjrtApi when the
                                                        .bss chain is appended in front    */
    /* +0x18 */ PLUGIN_Profiler_Api*  profiler_api;  /* -> 0x22255B48 (R_X86_64_RELATIVE)  */
    /* +0x20 */ uint64_t              _unused;       /* = 0                                */
};
```

`struct_size` and `type` occupy the standard chain-header positions, so a `FindExtension(api, 1)` walk lands on this node and a consumer casts the result to `PJRT_Profiler_Extension*`. The only field of interest past the header is `profiler_api` at `+0x18` — the chain header reaches `next` at `+0x10`, then this node adds the API pointer and a zero qword.

> **QUIRK —** Profiler is the chain *seed*, not a chain link the way the other 16 are. `GetTpuPjrtApi` @ `0xE6AA440` never calls a `CreateProfilerExtension`; it merely hands `&profiler_extension` to `CreateRawBufferExtension` as the `next` of the first-built `.bss` node. The image ships `next = NULL`, so until the chain is assembled this node *is* the terminator. After init, RawBuffer's `next` points back here, and this node's `next` stays `NULL` — making Profiler the tail a walker reaches last. See [Extension Chain — How the Chain Is Built](extension-chain.md#how-the-chain-is-built).

### Static Initialization

There is no initializer function. The 10 qwords of the two `.data` structs are filled by the dynamic linker from `R_X86_64_RELATIVE` relocations at load time. The node's own constant fields (`struct_size = 0x28`, `type = 1`) are baked into the `.data` image; the two pointer fields are relocations.

```text
profiler_extension @ 0x22255B98
  +0x18 (0x22255BB0) ── R_X86_64_RELATIVE ──> 0x22255B48   (profiler_api)

profiler_api @ 0x22255B48
  +0x10 (0x22255B58) ── R_X86_64_RELATIVE ──> 0xE6F1540   (Error_Destroy)
  +0x18 (0x22255B60) ── R_X86_64_RELATIVE ──> 0xE6F17C0   (Error_Message)
  +0x20 (0x22255B68) ── R_X86_64_RELATIVE ──> 0xE6F1920   (Error_GetCode)
  +0x28 (0x22255B70) ── R_X86_64_RELATIVE ──> 0xE6F0C60   (Create)
  +0x30 (0x22255B78) ── R_X86_64_RELATIVE ──> 0xE6F0E80   (Destroy)
  +0x38 (0x22255B80) ── R_X86_64_RELATIVE ──> 0xE6F0FA0   (Start)
  +0x40 (0x22255B88) ── R_X86_64_RELATIVE ──> 0xE6F1100   (Stop)
  +0x48 (0x22255B90) ── R_X86_64_RELATIVE ──> 0xE6F1240   (CollectData)
```

> **NOTE —** because the vtable is relocation-populated rather than written by a guarded initializer, the Profiler surface is live the instant the library is mapped — before any `GetTpuPjrtApi` call. The other extensions only materialize on the first `GetPjrtApi()`/`__cxa_guard` pass.

---

## The `PLUGIN_Profiler_Api` Vtable

### Layout

`profiler_api` @ `0x22255B48` is an 80-byte table: a `{struct_size, priv}` preamble followed by 8 function pointers. The slot ordering is fixed by the canonical header — three error helpers first, then the five lifecycle methods.

```c
/* PLUGIN_Profiler_Api @ 0x22255B48 — 80 bytes (0x50), 8 fn-ptrs. */
struct PLUGIN_Profiler_Api {
    /* +0x00 */ size_t  struct_size;                 /* = 0x50                          */
    /* +0x08 */ void*   priv;                         /* = NULL                          */
    /* +0x10 */ PLUGIN_Profiler_Error_Destroy*  Error_Destroy;
    /* +0x18 */ PLUGIN_Profiler_Error_Message*  Error_Message;
    /* +0x20 */ PLUGIN_Profiler_Error_GetCode*  Error_GetCode;
    /* +0x28 */ PLUGIN_Profiler_Create*         Create;
    /* +0x30 */ PLUGIN_Profiler_Destroy*        Destroy;
    /* +0x38 */ PLUGIN_Profiler_Start*          Start;
    /* +0x40 */ PLUGIN_Profiler_Stop*           Stop;
    /* +0x48 */ PLUGIN_Profiler_CollectData*    CollectData;
};
```

Every method takes a single `PLUGIN_Profiler_*_Args*` and returns a `PLUGIN_Profiler_Error*` (`NULL` on success). The lifecycle args drop the `priv` field that the main-table args carry; the three error args keep the canonical `{struct_size, priv, ...}` preamble.

### Method Map

All 8 slots resolve to text-section symbols under `xla::profiler::`, confirmed by mangled name and address in the function table. The "Args size" column is the value each method validates `struct_size` against — when it validates at all.

| Slot | Off | Method | Impl symbol (`xla::profiler::`) | Addr | Size | Args size |
|---|---|---|---|---|---|---|
| 0 | `+0x10` | `Error_Destroy` | `PLUGIN_Profiler_Error_Destroy` | `0xE6F1540` | 235 | `0x18` (24) |
| 1 | `+0x18` | `Error_Message` | `PLUGIN_Profiler_Error_Message` | `0xE6F17C0` | 343 | `0x28` (40) |
| 2 | `+0x20` | `Error_GetCode` | `PLUGIN_Profiler_Error_GetCode` | `0xE6F1920` | 99 | `0x1C` (28) |
| 3 | `+0x28` | `Create` | `PLUGIN_Profiler_Create` | `0xE6F0C60` | 539 | — (no check) |
| 4 | `+0x30` | `Destroy` | `PLUGIN_Profiler_Destroy` | `0xE6F0E80` | 263 | — (no check) |
| 5 | `+0x38` | `Start` | `PLUGIN_Profiler_Start` | `0xE6F0FA0` | 322 | — (no check) |
| 6 | `+0x40` | `Stop` | `PLUGIN_Profiler_Stop` | `0xE6F1100` | 300 | — (no check) |
| 7 | `+0x48` | `CollectData` | `PLUGIN_Profiler_CollectData` | `0xE6F1240` | 728 | — (no check) |

> **GOTCHA —** only the three *error helper* slots validate `struct_size`. The five lifecycle methods perform **no** backward-compat size check at all. The error helpers funnel through `xla::profiler::(anonymous namespace)::CheckMatchingStructSizes(name_view, required, current)` @ `0xE6F1640`, which builds an `absl::Status` `"<Args> size: expected M …"` on mismatch (and proceeds best-effort for Destroy/Message; returns an error for GetCode). The lifecycle methods read fields off fixed offsets unconditionally — a caller built against a different `pjrt_c_api_profiler_extension.h` revision than libtpu ships will silently mis-read `Create_Args`/`CollectData_Args`. There is no forward-compat window on the lifecycle path: link against the matching header.

### Args Structs

The lifecycle args are minimal; `Create` and `CollectData` carry data, the rest carry only the handle.

```c
/* Error helpers keep the {struct_size, priv, ...} preamble. */
struct PLUGIN_Profiler_Error_Destroy_Args {  /* 24 (0x18) */
    size_t                  struct_size;      /* must == 0x18 */
    void*                   priv;
    PLUGIN_Profiler_Error*  error;            /* in; freed                       */
};
struct PLUGIN_Profiler_Error_Message_Args {  /* 40 (0x28) */
    size_t                        struct_size;/* must == 0x28 */
    void*                         priv;
    const PLUGIN_Profiler_Error*  error;      /* in (not consumed)               */
    const char*                   message;    /* out — borrowed view into Status */
    size_t                        message_size;/* out                            */
};
struct PLUGIN_Profiler_Error_GetCode_Args {  /* 28 (0x1C) */
    size_t                        struct_size;/* must == 0x1C */
    void*                         priv;
    const PLUGIN_Profiler_Error*  error;      /* in                              */
    int32_t                       code;       /* out — absl::StatusCode          */
};

/* Lifecycle args drop `priv`. */
struct PLUGIN_Profiler_Create_Args {          /* 32 (0x20) */
    size_t            struct_size;
    const char*       serialized_options;     /* in — tensorflow.ProfileOptions  */
    size_t            serialized_options_size;/* in                              */
    PLUGIN_Profiler*  profiler;               /* out                             */
};
struct PLUGIN_Profiler_Destroy_Args {         /* 16 */
    size_t            struct_size;
    PLUGIN_Profiler*  profiler;               /* in; consumed                    */
};
struct PLUGIN_Profiler_Start_Args { size_t struct_size; PLUGIN_Profiler* profiler; };  /* 16 */
struct PLUGIN_Profiler_Stop_Args  { size_t struct_size; PLUGIN_Profiler* profiler; };  /* 16 */
struct PLUGIN_Profiler_CollectData_Args {     /* 32 (0x20) */
    size_t            struct_size;
    PLUGIN_Profiler*  profiler;               /* in                              */
    size_t            buffer_size_in_bytes;   /* in/out — query vs fetch         */
    uint8_t*          buffer;                 /* in/out — borrowed on return     */
};
```

---

## The Handle and the Error Object

### `PLUGIN_Profiler`

A 128-byte heap object (`operator new(0x80)`), zeroed on construction except the state byte. It inline-aggregates a `tensorflow::profiler::XSpace`, a serialized-bytes vector, the cached size, the backend `ProfilerCollection*`, and one state byte. All offsets below are confirmed directly from the lifecycle method bodies (Create writes `+120`; CollectData writes `+96`/`+104`; Destroy reads `+88`/`+96`/`+112`).

| Field | Off | Type | Meaning |
|---|---|---|---|
| `xspace` | `+0x00` | `tensorflow::profiler::XSpace` (88 B) | proto2 message; only constructed lazily by CollectData |
| `xspace_constructed` | `+0x58` | `uint8_t` | `1` iff `XSpace::XSpace()` ran; gates the `~XSpace` in Destroy |
| `serialized_xspace` | `+0x60` | `std::vector<uint8_t>*` | owned heap vector of proto bytes from CollectData |
| `cached_xspace_size` | `+0x68` | `size_t` | `XSpace::ByteSizeLong()` from first CollectData |
| `collection` | `+0x70` | `tsl::profiler::ProfilerCollection*` | owned backend; freed via vtable in Destroy |
| `ready` | `+0x78` | `uint8_t` | state byte (see below) |

> **GOTCHA —** the field at `+0x60` is the **pointer to** a heap `std::vector<uint8_t>` (a 24-byte `{data,size,capacity}` header `operator new(0x18u)`-allocated by CollectData), not an inline vector. Destroy frees the inner `data` buffer and then the 24-byte header, and CollectData drops the previous header before installing a new one. A reimplementation that treats `+0x60` as an inline 24-byte vector will misalign every field after it.

### `PLUGIN_Profiler_Error`

An 8-byte heap object wrapping exactly one `absl::Status`. It is the only error channel across the C-ABI: a lifecycle method that fails does `new PLUGIN_Profiler_Error{status}`; a successful one returns `NULL`. The caller inspects it with `Error_Message`/`Error_GetCode` and frees it with `Error_Destroy`.

```c
struct PLUGIN_Profiler_Error { absl::Status status; };  /* 8 bytes */
```

`absl::Status` is itself a tagged pointer: bit 0 set marks an inline OK / small-code marker, clear marks a heap `StatusRep`. `Error_GetCode` reads `(rep & 1) ? (rep >> 2) : rep->canonical_code`, maps it via `absl::status_internal::MapToLocalCode`, and returns an `absl::StatusCode` that the public `PJRT_Error_Code` enum mirrors by construction. `Error_Message` returns a borrowed `(message, message_size)` view — into the inline char buffer, the heap `StatusRep` payload, or the static `absl::Status::kMovedFromString` @ `0xA2E8580` — that the caller must not free and must not use past the matching `Error_Destroy`.

---

## The Lifecycle State Machine

The single byte at `PLUGIN_Profiler+0x78` (`ready`) drives a one-shot lifecycle. It takes two values, and the value `0` is **overloaded** across "running" and "stopped".

```text
        Create
          │  ready = 1   ("ready, not yet started")
          ▼
   ┌──────────────┐    Start (ready==1) ──> collection->Start();
   │ ready == 1   │     AddPluginMetadata(); ready = 0
   │  (ready)     │
   └──────┬───────┘
          │  Start when ready==0 ──> log "already started", return NULL (no-op)
          ▼
   ┌──────────────┐    Stop (ready==0) ──> collection->Stop(); ready = 0 (stays 0)
   │ ready == 0   │
   │ (run/stop)   │    Stop when ready==1 ──> log "already stopped", return NULL (no-op)
   └──────┬───────┘
          │  CollectData ──> drains+serializes XSpace (one-shot, destructive)
          ▼
        Destroy
```

> **QUIRK —** `ready == 0` means **both** "running" and "stopped". Start and Stop disambiguate only by precondition: Start requires `ready != 0` to do work, Stop requires `ready == 0`. Stop leaves the byte at `0`, so a second Stop is a no-op against the underlying collection rather than a state error. The ground truth ("did Start actually succeed") lives inside the sub-profiler implementations, not in this byte. A reimplementation that treats the byte as a clean three-state enum will get the overload wrong.

### Create — `0xE6F0C60`

```c
function PLUGIN_Profiler_Create(args):                     // 0xE6F0C60
    VLOG(1) "Creating plugin profiler"                     // plugin_tracer_impl.cc:38
    p = operator new(0x80);                                // 128-byte handle, zeroed
    p->ready = 1;                                          // *(BYTE*)(p+120) = 1
    ProfileOptions opts;                                   // stack
    opts.ParseFromString(args->serialized_options,
                         args->serialized_options_size);   // proto2::MessageLite::ParseFromString
    profs = tsl::profiler::CreateProfilers(opts);          // 0x1CF50860 — factory walk under mu
    p->collection = new ProfilerCollection(move(profs));   // operator new(0x20); ctor 0xF6A15E0
    args->profiler = p;
    return NULL;
```

`CreateProfilers` @ `0x1CF50860` walks the global factory registry, invoking each registered `std::function<unique_ptr<ProfilerInterface>(const ProfileOptions&)>` and wrapping each output in a `ProfilerController` for crash isolation. The `ProfilerCollection` is a 32-byte `{vtable, data, size, capacity}` object that takes the resulting vector inline. No `ProfileOptions` field is consulted at this layer — interpretation happens inside each factory lambda.

### Start — `0xE6F0FA0`

```c
function PLUGIN_Profiler_Start(args):                      // 0xE6F0FA0
    p = args->profiler;
    if (p->ready == 0):                                    // already started/stopped
        VLOG(1) "Profiler is already started"; return NULL // no-op, no error
    p->cached_xspace_size = 0;
    s = p->collection->Start();                            // vtable +0x10 -> 0xF6A1640
    if (s.IsOK()):
        xla::profiler::AddPluginMetadata();                // 0xF3165C0 — stamp build info
        p->ready = 0;                                      // mark running
        return NULL;
    return new PLUGIN_Profiler_Error{s};
```

`AddPluginMetadata` @ `0xF3165C0` stamps two `XStat` entries onto the active session under the StatType keyed by `tsl::profiler::GetStatTypeStr(0xA6)` (166): `BuildData::Changelist()` and `BuildData::Timestamp()`, formatted `"%s @ %s"`. This embeds libtpu build provenance directly into the resulting XSpace, so a consumer can read the producing build without a side channel. `ProfilerCollection::Start` @ `0xF6A1640` calls vtable+0x10 on each sub-profiler, ignoring per-profiler non-OK (last status wins).

### Stop — `0xE6F1100`

```c
function PLUGIN_Profiler_Stop(args):                       // 0xE6F1100
    p = args->profiler;
    if (p->ready == 1):                                    // never started (post-Create)
        VLOG(2) "Profiler is already stopped"; return NULL
    s = p->collection->Stop();                             // vtable +0x18 -> 0xF6A16C0
    if (s.IsOK()): p->ready = 0; return NULL;              // stays 0
    return new PLUGIN_Profiler_Error{s};
```

### CollectData — `0xE6F1240`

The largest body (728 bytes) and the only destructive one. It supports a two-mode contract via `buffer_size_in_bytes`: a query mode (caller pre-sized) and a fetch mode (libtpu allocates).

```c
function PLUGIN_Profiler_CollectData(args):                // 0xE6F1240
    p = args->profiler;
    XSpace xs;  XSpace::XSpace(&xs, 0);                     // stack
    if (p->xspace_constructed == 0):                       // first real collection
        s = p->collection->CollectData(&xs);               // vtable +0x20 -> 0xF6A1740
        if (!s.IsOK()): return new PLUGIN_Profiler_Error{s};
        p->cached_xspace_size = xs.ByteSizeLong();          // stored at p+104 (+0x68)
    size = xs.ByteSizeLong();
    if (args->buffer_size_in_bytes != 0):                  // query mode: caller has buffer
        XSpace::~XSpace(&xs); return NULL;                 //   return size only
    vec = new vector<uint8_t>(size + 1, 0);                // operator new(0x18) hdr + (size+1) data
    free(p->serialized_xspace);                            // drop previous (p+96 / +0x60)
    p->serialized_xspace = vec;
    xs.SerializePartialToArray(vec->data, size);           // proto2::MessageLite
    args->buffer               = vec->data;                // borrowed
    args->buffer_size_in_bytes = size + 1;                 // note the +1 trailing byte
    XSpace::~XSpace(&xs);
    return NULL;
```

> **GOTCHA —** `CollectData` is **one-shot and destructive at the backend**. `ProfilerCollection::CollectData(XSpace*)` @ `0xF6A1740` passes the same XSpace pointer to each sub-profiler (so they *append* planes into one space), then *clears its inner vector* — every `unique_ptr<ProfilerInterface>` is reset, releasing the sub-profilers. A second `CollectData` against the same handle therefore runs against an empty collection: it re-serializes the cached XSpace and returns identical bytes. The returned `buffer` is borrowed from the handle's internal vector — valid only until the next `CollectData` or `Destroy`. Consumers must not free it and must not use it across Destroy.

> **QUIRK —** the serialized length reported back is `size + 1`, not `size`. The buffer is `new vector<uint8_t>(size + 1, 0)` — one extra zero byte past the proto payload. A reimplementation that round-trips `buffer[:buffer_size_in_bytes]` straight into `XSpace::ParseFromString` ingests a trailing `0x00`; proto2 tolerates it as a zero-length field-0 tail, but a strict parser may not.

### Destroy — `0xE6F0E80`

```c
function PLUGIN_Profiler_Destroy(args):                    // 0xE6F0E80
    p = args->profiler;
    if (!p) return NULL;
    if (p->collection):                                    // p+112 (+0x70)
        ~ProfilerCollection(p->collection);                // vtable +0x08 (destroying dtor) -> 0xF6A18E0
        p->collection = NULL;
    if (p->serialized_xspace):                             // p+96 (+0x60)
        free(p->serialized_xspace->data);
        free(p->serialized_xspace);                        // the 24-byte header
        p->serialized_xspace = NULL;
    if (p->xspace_constructed == 1):                       // p+88 (+0x58)
        XSpace::~XSpace(&p->xspace);
    free(p, 0x80);
    return NULL;
```

---

## The Bridge into the Trace Backend

### `tsl::profiler::ProfilerCollection`

The handle's `collection` (`+0x70`) is a `tsl::profiler::ProfilerCollection` — a 32-byte object whose `.data.rel.ro` vtable @ `0x217738A0` carries `{top-offset, RTTI, D2 dtor, D0 dtor, Start, Stop, CollectData}`. The five lifecycle methods call into it at fixed vtable offsets, so the PJRT layer is purely a marshaller.

| Backend op | vtable slot | Symbol (`tsl::profiler::ProfilerCollection::`) | Addr |
|---|---|---|---|
| ctor (takes `vector<unique_ptr<ProfilerInterface>>`) | — | `ProfilerCollection(vector)` | `0xF6A15E0` |
| `Start` | `+0x10` | `Start` | `0xF6A1640` |
| `Stop` | `+0x18` | `Stop` | `0xF6A16C0` |
| `CollectData(XSpace*)` | `+0x20` | `CollectData` | `0xF6A1740` |
| dtor (D2 / D0) | `+0x08` | `~ProfilerCollection` | `0xF6A1840` / `0xF6A18E0` |

Each backend op iterates the inner vector and calls the corresponding `tsl::profiler::ProfilerInterface` vtable slot on every sub-profiler. The sub-profiler set is whatever `CreateProfilers` assembled from the factory registry: confirmed members include `xprof::tpu::TpuProfilerImpl` (device, talks to xdb per-chip), `xprof::cpu::HostTracer` (factory @ `0xEF34760`, host perf/cpu), and `tsl::profiler::ThreadpoolProfilerInterface` (vtable @ `0x2175C150`, host threadpool dispatch).

### XSpace / TraceMe

The actual trace data never touches the PJRT layer per-event. Host events come from `tsl::profiler::TraceMe` macros on TPU-runtime threads (`TpuCompile`, `TpuExecute`, queue submission, megascale transport); device events come from per-chip hardware ring buffers drained by `TpuProfilerImpl::CollectData`. The sub-profilers fold both into a shared `tensorflow.profiler.XSpace` — a host XPlane `/host:0` plus one device XPlane per addressable core (`/device:TPU:0`, …) — whose XEvent/XStat/XEventMetadata hierarchy and per-chip-family `TracePointId` payloads are the profiling section's subject. This page treats XSpace as an opaque serialized blob; the bytes leave through `CollectData`'s buffer unmodified.

> **NOTE —** profiling runs *concurrently* with `PJRT_LoadedExecutable_Execute`. The gate is the `xprof::tpu::TpuProfilerControlListener` singleton (`GetOrCreateTpuProfilerControlListener` @ `0xF332800`): each chip driver queries `CanStartProfiler(chip_loc, profiler*, run_id)` @ `0xF3328C0` before opening its trace ring buffer and polls `MustStopProfiler(chip_loc)` @ `0xF332A00` for mid-run stop. The PJRT side has no direct knowledge of which chips participate. This listener and the per-chip drain are out of scope here — referenced by symbol.

### Threading

- **Per-handle: not thread-safe.** No mutex guards `Create/Start/Stop/CollectData/Destroy` on a single handle. Concurrent calls race the state byte (`+0x78`), the vector pointer (`+0x60`), and the collection's inner lists. `CollectData ⟷ Destroy` is a use-after-free; `Start ⟷ Stop` races the overloaded state byte.
- **Cross-handle: safe.** Distinct handles share no mutable state at this layer. The one shared resource is the factory registry, serialized by `tsl::profiler::(anonymous namespace)::mu` (an `absl::Mutex`) held only for the duration of the `CreateProfilers` factory enumeration inside Create — *released* before factory bodies run, so a factory may re-enter other libtpu APIs but must not register new factories. The mutex is non-recursive: Create must not be called from inside a factory or a vtable callback.

---

## Relationship to the Legacy `TpuProfiler_*` ABI

libtpu exposes the same backend through a second, older C-ABI: four exported symbols `TpuProfiler_Create` @ `0xEF33BC0`, `TpuProfiler_Destroy` @ `0xEF33DE0`, `TpuProfiler_Start` @ `0xEF33EA0`, `TpuProfiler_Stop` @ `0xEF34080`, `TpuProfiler_CollectData` @ `0xEF34240`. These are the `tensorflow/core/tpu` surface, reached through `stream_executor::tpu::ProfilerApiFn()` @ `0x10900EA0` and consumed by TF/TPUEstimator. They have **no** extension wrapper and **no** error helpers — failures are written into a caller-supplied `TF_Status*` instead of returned as a `PLUGIN_Profiler_Error`.

Both ABIs produce the same underlying profilers because TF's profiler service registers itself via `tsl::profiler::RegisterProfilerFactory` @ `0x1CF50780` into the same global registry that `CreateProfilers` walks. The legacy handle is 120 bytes (vs the PJRT handle's 128): it omits the trailing state-byte padding at `+0x78`, placing its collection at `+0x68` and serialized vector at `+0x60`. A byte-by-byte audit of the legacy quadruplet was outside this page's scope — the 8-byte size delta and the shared `ProfilerCollection*` are confirmed; the exact legacy offsets are HIGH-confidence inference from the Create/CollectData paths.

---

## What Is Not Covered Here

- **The XSpace/XPlane payload schema and per-chip-family `TracePointId` enums** — owned by the profiling section; here XSpace is an opaque serialized blob.
- **The full factory inventory registered into the global registry** — confirmed members are `HostTracer`, `TpuProfilerImpl`, `ThreadpoolProfilerInterface`; the complete set is populated lazily across several `_GLOBAL__sub_I_*profiler*.cc` static-init blocks and was not exhaustively enumerated (LOW confidence on completeness).
- **The `TpuProfilerControlListener` vtable ordering and per-run gating semantics** — methods are confirmed at the addresses cited; the abstract-base slot ordering was not extracted.
- **The exact StatType string for `AddPluginMetadata`** — confirmed numeric `0xA6` (166); the interned name requires walking `GetStatTypeStrMap` and was not resolved (LOW).

---

## Cross-References

- [Extension Chain](extension-chain.md) — the `PJRT_Extension_Base` header, the type-id walk, and Profiler's role as the `.data` seed / type-1 terminator
- [PJRT Plugin Overview](overview.md) — how `dlsym("GetPjrtApi")` reaches `GetTpuPjrtApi` and the one-shot init that patches Profiler's `next`
- [API Vtable Reconstruction](api-vtable-reconstruction.md) — the 140 main-table slots that `extension_start` hangs off (a separate structure)
- [Remaining Extensions](ext-remaining.md) — the other extensions that carry their methods inline (contrast with Profiler's pointer-to-vtable indirection)
- [Topology Description Extension](ext-topology-description.md) — type 16, the largest live extension, for comparison of node-body conventions
