# TpuExecutor Roster

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, ELF x86-64 DYN, not stripped; `.text` VMA equals file offset). Other versions differ.*

## Abstract

`TpuExecutor_*` is the per-device-runtime cluster of the TfTpu C-ABI shim: the 25 `extern "C"` free functions that back `stream_executor::tpu::TpuExecutor`, the StreamExecutor object that owns one TPU core's allocations, streams, events, and host/device transfers. Where `StreamExecutor` in the open-source tree is a fat C++ virtual interface, the closed plugin exposes it as a flat function table. Every method an XLA `StreamExecutor` call needs — allocate HBM, mint an event, record it on a stream, wait on it, copy a buffer to or from host, block until a stream drains — is one slot of the `TfTpu_ExecutorApiFn` struct, and that slot points at one of the functions rostered here.

The functions share a single shape, and it is the only thing a reimplementer must reproduce. Each takes an opaque `SE_StreamExecutor*` handle as its first argument; the handle's first qword is a pointer to the real device-executor C++ object (IDA recovers it as `deepsea::executor::DeepseaExecutor`), and that object's first qword is its vtable. The C-ABI function reads `*handle` to get the driver object, then calls through a **fixed byte offset into the driver vtable** — `Allocate` bounces to `vtable+136`, `Deallocate` to `vtable+144`, `BlockHostUntilDone` to `vtable+120`, and so on. The offset is the entire dispatch contract: there is no name lookup, no dynamic resolution, just `(*(vtable + N))(driver, args...)`. A handful of functions skip the vtable and call a named `DeepseaExecutor::*` method directly (`GetCoreLocation`, `EnqueueInfeed`, `DequeueOutfeed`), and one (`Free`) is a bare `free()`.

This page owns the *roster and the per-function impl-symbol→vtable-slot map* only. The `*ApiFn()` accessor pattern that fronts the table, the opaque-handle / `ApiConverter` marshalling convention, and the roster-map across all clusters are on the [shim overview](overview.md) — link, not re-explained. The host-side `stream_executor::tpu::TpuExecutor` C++ shim that *calls through* this table is on [StreamExecutor Platform & Executor Model](../pjrt/stream-executor-host-interpreter.md). The HBM allocator the memory ops bottom out in is on [Allocator Integration](../runtime/allocator-integration.md).

For reimplementation, the contract is:

- **The handle indirection** — `arg0` is an `SE_StreamExecutor*`; `*arg0` is the `DeepseaExecutor*` driver object; `**arg0` is its vtable. Two dereferences before the dispatch.
- **The per-function vtable offset** — the byte offset each function adds to the driver vtable, tabulated below. This is the dispatch key; reproduce the offsets and the layout exactly.
- **The status-out idiom** — most functions take a trailing `absl::status_internal::StatusRep**` out-slot they overwrite (with ref-count fix-ups), returning the raw `StatusRep*`; a sentinel of `(&dword_0 + 1)` (an odd-tagged inline-OK pointer) means success.
- **The `ApiConverter::FromC` boundary** — functions that take a `DeviceMemoryBase` receive the flat C twin and call `ApiConverter::FromC` to rebuild the C++ `DeviceAddressBase` on a stack scratch buffer before the vtable call.

| | |
|---|---|
| **Cluster** | `TpuExecutor_*` — per-device runtime, 25 `extern "C"` functions |
| **Address span** | `0xeab90c0` (`Init`) – `0xeab9cc0` (`CreateDeviceDescription`) |
| **Table** | `TfTpu_ExecutorApiFn` (reached via `ExecutorApiFn() @ 0x20819360`) — see [overview](overview.md#2-the-apifn-accessor-pattern) |
| **Handle (arg0)** | `SE_StreamExecutor*`; `*handle` = `deepsea::executor::DeepseaExecutor*`; `**handle` = driver vtable |
| **Dispatch** | `(*(driver_vtable + offset))(driver, marshalled_args...)` |
| **Status convention** | trailing `StatusRep**` out-slot; OK sentinel `(&dword_0 + 1)`; `Unref` on the displaced rep |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

> **Scope —** the singleton `ExecutorApiFn()` accessor, the `IsStreamExecutorEnabled` probe, and the `SE_*`/`XLA_*` opaque-handle + `ApiConverter::ToC`/`FromC`/`Destroy` convention are on the [shim overview](overview.md). The `DeviceMemoryBase`/`XLA_Literal` flat-C twins are described there; this page references them but does not re-define them. Infeed/outfeed *semantics* (queue model, transfer-manager interplay) are on [Infeed/Outfeed](../runtime/infeed-outfeed.md); the `TpuExecutor_*` functions here are only the SE-level enqueue/dequeue entry stubs.

---

## At a Glance — Functions per Area

| Area | Count | Functions |
|---|---|---|
| Lifecycle | 4 | `Init`, `GetStatus`, `CreateDeviceDescription`, `Free` |
| Memory | 4 | `Allocate`, `Deallocate`, `GetAllocatorStats`, `DeviceMemoryUsage` |
| Stream | 2 | `CreateStreamDependency`, `DeallocateStream` |
| Event | 3 | `AllocateEvent`, `RecordEvent`, `WaitForEvent` |
| Transfer (memcpy) | 4 | `SynchronousMemcpyToHost`, `SynchronousMemcpyFromHost`, `MemcpyToHost`, `MemcpyFromHost` |
| Transfer (infeed/outfeed) | 2 | `EnqueueInfeed`, `DequeueOutfeed` |
| Sync | 3 | `BlockHostUntilDone`, `SynchronizeAllActivity`, `WaitForEvent` (also Event) |
| HBM / host-side | 3 | `EnqueueCompactionOnStreamForHbm`, `HostCallback`, `UnloadAllPrograms` |
| Topology | 1 | `GetCoreLocation` |

> **QUIRK —** there is **no** `TpuExecutor_CreateStream` in the roster. Stream *creation* is not delegated to a C-ABI slot: the host-side `stream_executor::tpu::TpuExecutor::CreateStream` (`0xe996ca0`) constructs the SE stream object itself and calls `deepsea::executor::DeepseaExecutor::CreateStream` (`0x1d0db000`) directly. The C-ABI surface only exposes the *post-creation* stream operations — `CreateStreamDependency` (wire a wait edge) and `DeallocateStream` (tear down). A reimplementer who expects a `CreateStream` slot in `TfTpu_ExecutorApiFn` will not find one; the slot count for executor stream ops is two, not three.

---

## Lifecycle

### Purpose

Bring a device executor online (`Init`), report its health (`GetStatus`), describe its hardware to the SE layer (`CreateDeviceDescription`), and release a heap allocation the plugin returned to the host (`Free`). `Init` and `GetStatus` are the same vtable slot (`+32`) — both ask the driver "are you OK?", once at bring-up and once on demand.

### Function Map

| Function | Address | Size | Driver dispatch | Confidence |
|---|---|---|---|---|
| `TpuExecutor_Init` | `0xeab90c0` | 60 | `driver_vtable+32` (status query) | CERTAIN |
| `TpuExecutor_GetStatus` | `0xeab92e0` | 60 | `driver_vtable+32` (same slot as `Init`) | CERTAIN |
| `TpuExecutor_CreateDeviceDescription` | `0xeab9cc0` | 991 | `driver_vtable+272`, then builds `stream_executor::DeviceDescription` | HIGH |
| `TpuExecutor_Free` | `0xeab9100` | 16 | none — `if (p) free(p)` | CERTAIN |

### Algorithm

```c
// TpuExecutor_Init / TpuExecutor_GetStatus            0xeab90c0 / 0xeab92e0
int64 Init(SE_StreamExecutor* handle, StatusRep** out):
    rep = (*(*handle->driver + 32))(handle->driver)   // driver vtable slot +32
    return store_status(out, rep)                      // status-out idiom (see below)

// TpuExecutor_Free                                    0xeab9100
void Free(void* p):
    if (p) free(p)        // releases a plugin-allocated heap buffer handed to the host

// TpuExecutor_CreateDeviceDescription                 0xeab9cc0  (991 bytes, largest in cluster)
void CreateDeviceDescription(SE_StreamExecutor* handle, ..., StatusRep** out):
    (*(*handle->driver + 272))(&scratch)               // driver fills a device-info struct
    // ... marshals name/vendor/HBM-size/clock fields into stream_executor::DeviceDescription
```

`CreateDeviceDescription` is the cluster's outlier at 991 bytes: it pulls a populated device-info blob through `driver_vtable+272`, then assembles a `stream_executor::DeviceDescription` by copying string and numeric fields one at a time (the long `v17`…`v29` chain in the decompile is string-field marshalling). The field-by-field copy is why it dwarfs every other roster entry; the dispatch itself is still one vtable call.

> **NOTE —** the status-out idiom recurs in ~16 of the 25 functions and is described once here. The function calls the driver, which returns a raw `absl::status_internal::StatusRep*`. The C-ABI wrapper writes it into the caller's `StatusRep** out`, ref-counts correctly (it `Unref`s whatever rep the slot previously held, and for inline-OK reps — those with the low bit set, the `(&dword_0 + 1)` sentinel — skips the unref). The returned value is the rep itself; the host's `TpuStatus`-style wrapper queries it for ok/code/message. Functions below show only the *dispatch* line and elide this boilerplate.

---

## Memory

### Purpose

Allocate and free device memory (`Allocate`/`Deallocate`), and report capacity (`DeviceMemoryUsage`) and detailed allocator statistics (`GetAllocatorStats`). These are the SE-level face of the HBM allocator documented on [Allocator Integration](../runtime/allocator-integration.md); the vtable slots here forward into that allocator inside the driver.

### Function Map

| Function | Address | Size | Driver dispatch | Marshalling | Confidence |
|---|---|---|---|---|---|
| `TpuExecutor_Allocate` | `0xeab9120` | 55 | `driver_vtable+136` | returns 16-byte `DeviceMemoryBase` by value | CERTAIN |
| `TpuExecutor_Deallocate` | `0xeab9160` | 50 | `driver_vtable+144` | `ApiConverter::FromC` on the `DeviceMemoryBase` arg | CERTAIN |
| `TpuExecutor_GetAllocatorStats` | `0xeab91a0` | 120 | `driver_vtable+320` | copies optional `AllocatorStats` fields out | CERTAIN |
| `TpuExecutor_DeviceMemoryUsage` | `0xeab9220` | 64 | `driver_vtable+256` | two out-params: `free`, `total` | CERTAIN |

### Algorithm

```c
// TpuExecutor_Allocate                                0xeab9120
DeviceMemoryBase* Allocate(DeviceMemoryBase* out, SE_StreamExecutor* handle /*+args*/):
    (*(*handle->driver + 136))(&tmp)        // driver allocates; returns {opaque, ptr, size}
    out->lo = tmp.lo; out->hi = tmp.hi      // 16-byte struct copied via xmm move
    return out

// TpuExecutor_Deallocate                              0xeab9160
int64 Deallocate(SE_StreamExecutor* handle, SE_DeviceMemoryBase* cmem):
    DeviceAddressBase mem = ApiConverter::FromC(cmem)   // flat C twin -> C++ object on stack
    return (*(*handle->driver + 144))(handle->driver, &mem)

// TpuExecutor_DeviceMemoryUsage                       0xeab9220
bool DeviceMemoryUsage(SE_StreamExecutor* handle, int64* free_out, int64* total_out):
    ok = (*(*handle->driver + 256))(handle->driver, &free, &total)
    if (ok): *free_out = free; *total_out = total
    return ok

// TpuExecutor_GetAllocatorStats                       0xeab91a0
char GetAllocatorStats(SE_StreamExecutor* handle, SE_AllocatorStats* out):
    (*(*handle->driver + 320))(&stats)      // driver fills a stats struct on the stack
    if (stats.valid == 1):                  // last byte is the "present" flag
        copy num_allocs / bytes_in_use / peak_bytes ... into *out
        // each optional field (bytes_limit, largest_alloc) guarded by its own bool
    return stats.valid
```

> **GOTCHA —** `Allocate` returns its 16-byte `DeviceMemoryBase` *by value* through an out-pointer (`arg0` is the result slot, the executor handle is `arg1`), and the decompile copies it with an inline `vmovups xmm0` pair, not a field assignment. A reimplementation that treats `DeviceMemoryBase` as a single pointer will lose the second qword (the size / element-count). The struct is 16 bytes: `{opaque_or_base, payload}`; copy both qwords.

> **QUIRK —** `GetAllocatorStats` reads the *last byte* of the driver-filled stats struct as a presence flag and only copies fields out when it is `1`. Sub-fields (`bytes_limit`, `largest_alloc_size`) each carry their own `bool` guard before the value, mirroring `std::optional<int64>`. Skipping a guard copies an uninitialised qword into the host's stats — the guards are not optional.

---

## Stream

### Purpose

Operate on streams the host already created. `CreateStreamDependency` records a happens-before edge (one stream waits on another); `DeallocateStream` tears a stream down and drops the plugin's reference. Stream *creation* itself is host-side (see the QUIRK in [At a Glance](#at-a-glance--functions-per-area)).

### Function Map

| Function | Address | Size | Driver dispatch | Confidence |
|---|---|---|---|---|
| `TpuExecutor_CreateStreamDependency` | `0xeab92a0` | 48 | `*arg1->vtable+56` (on the *stream* object) | CERTAIN |
| `TpuExecutor_DeallocateStream` | `0xeab9260` | 58 | `driver_vtable+224`, then stream dtor `vtable+8` | CERTAIN |

### Algorithm

```c
// TpuExecutor_CreateStreamDependency                  0xeab92a0
bool CreateStreamDependency(_, Stream** dependent, Stream** dependency):
    rep = (*(*(*dependent) + 56))(*dependent, *dependency)   // dispatch is on the STREAM vtable
    if (!is_ok_inline(rep)): Unref(rep)
    return rep == OK_SENTINEL                                 // (&dword_0 + 1)

// TpuExecutor_DeallocateStream                        0xeab9260
int64 DeallocateStream(SE_StreamExecutor* handle, Stream** stream):
    rep = (*(*handle->driver + 224))(handle->driver, *stream) // detach from driver
    s = *stream; *stream = 0
    if (s): (*(*s + 8))(s)                                    // virtual destructor at vtable+8
    return rep
```

> **QUIRK —** `CreateStreamDependency` dispatches on the **stream object's** vtable (`*arg1` is the dependent stream, and `+56` indexes *its* vtable), not on the executor's driver vtable. It is the only stream-area function that bypasses the executor handle entirely — the executor is passed as `arg0` but unused. Reproduce the wait edge as a method on the dependent stream, not on the executor.

---

## Event

### Purpose

Mint a device event (`AllocateEvent`), mark it reached on a stream (`RecordEvent`), and make a stream wait for it (`WaitForEvent`). Events are SE's cross-stream synchronisation primitive; these three plus `BlockHostUntilDone` are the whole device-side event surface.

### Function Map

| Function | Address | Size | Driver dispatch | Confidence |
|---|---|---|---|---|
| `TpuExecutor_AllocateEvent` | `0xeab9340` | 179 | `driver_vtable+72` (creates event), ref-count plumbing | CERTAIN |
| `TpuExecutor_RecordEvent` | `0xeab9400` | 63 | `*arg1->vtable+72` (on the *stream*: record event) | CERTAIN |
| `TpuExecutor_WaitForEvent` | `0xeab9440` | 63 | `*arg1->vtable+64` (on the *stream*: wait on event) | CERTAIN |

### Algorithm

```c
// TpuExecutor_AllocateEvent                           0xeab9340
int64 AllocateEvent(SE_StreamExecutor* handle, Event** out_event, StatusRep** out_status):
    (*(*handle->driver + 72))(&tmp)        // driver constructs the event (refcounted handle)
    if (tmp.status == OK_SENTINEL):
        move tmp.event into *out_event     // transfer ownership; null the temp
    store_status(out_status, tmp.status)   // with the usual Unref bookkeeping

// TpuExecutor_RecordEvent / TpuExecutor_WaitForEvent  0xeab9400 / 0xeab9440
int64 RecordEvent(_, Stream** stream, Event** event, StatusRep** out):
    rep = (*(*(*stream) + 72))(*stream, *event)   // RecordEvent: stream vtable +72
    // WaitForEvent is identical with offset +64
    return store_status(out, rep)
```

> **NOTE —** `RecordEvent` and `WaitForEvent` both dispatch on the **stream** vtable (offsets `+72` and `+64` respectively), not the executor. The executor handle is `arg0` and ignored; `arg1` is the stream, `arg2` the event. Only `AllocateEvent` touches the driver vtable (`+72`), because only event *construction* belongs to the executor. `AllocateEvent` is the cluster's second-largest function (179 bytes) entirely because of the dual ref-count fix-up — it manages both the event handle's refcount and the status rep's.

---

## Transfer — Memcpy

### Purpose

Copy buffers between host and device. Four variants split on two axes: **direction** (`ToHost` = D→H, `FromHost` = H→D) and **synchrony** (`Synchronous*` blocks the calling thread; the plain `Memcpy*` enqueue onto a stream and return). All four marshal a `DeviceMemoryBase` through `ApiConverter::FromC` before dispatch.

### Function Map

| Function | Address | Size | Direction | Sync | Driver dispatch | Confidence |
|---|---|---|---|---|---|---|
| `TpuExecutor_SynchronousMemcpyToHost` | `0xeab9480` | 112 | D→H | blocking | `driver_vtable+216` | CERTAIN |
| `TpuExecutor_SynchronousMemcpyFromHost` | `0xeab9500` | 109 | H→D | blocking | `driver_vtable+208` | CERTAIN |
| `TpuExecutor_MemcpyToHost` | `0xeab9580` | 109 | D→H | stream | `driver_vtable+80` | CERTAIN |
| `TpuExecutor_MemcpyFromHost` | `0xeab9600` | 109 | H→D | stream | `driver_vtable+88` | CERTAIN |

### Algorithm

```c
// All four share this body; only the vtable offset and arg order differ.
// TpuExecutor_SynchronousMemcpyToHost                 0xeab9480 (offset +216)
int64 SyncMemcpyToHost(SE_StreamExecutor* handle, void* host_dst,
                       SE_DeviceMemoryBase* src, int64 size, StatusRep** out):
    DeviceAddressBase dev = ApiConverter::FromC(src)   // flat twin -> C++ object on stack
    rep = (*(*handle->driver + 216))(handle->driver, host_dst, &dev, size)
    return store_status(out, rep)

// Offset map:
//   SynchronousMemcpyToHost   +216   (D->H, blocking)
//   SynchronousMemcpyFromHost +208   (H->D, blocking)  -- FromC on the H->D dest buffer
//   MemcpyToHost              +80    (D->H, enqueue on arg2 stream)
//   MemcpyFromHost           +88    (H->D, enqueue on arg2 stream)
```

> **QUIRK —** the synchronous and asynchronous variants use *far apart* driver-vtable slots: the blocking pair sits at `+208`/`+216` while the stream-enqueue pair sits at `+80`/`+88`. They are not adjacent and not interleaved by direction — the driver groups by synchrony first. A reimplementer laying out the driver vtable cannot assume the four memcpy methods are contiguous. The async `Memcpy*` take an extra stream argument (the buffer to enqueue against); the `Synchronous*` do not.

> **GOTCHA —** every memcpy rebuilds the `DeviceMemoryBase` with `ApiConverter::FromC` into a 64-byte stack scratch (`v10`/`v11`/`v12`) *before* the vtable call, and passes `&scratch`. The flat C `SE_DeviceMemoryBase` is never handed to the driver directly — the driver expects the C++ `DeviceAddressBase`. Skip the `FromC` and the driver reads a wrongly-shaped struct. See the [overview](overview.md#3-the-opaque-handle-convention) for the `ToC`/`FromC` contract.

---

## Transfer — Infeed / Outfeed

### Purpose

Push a literal into the device's infeed queue (`EnqueueInfeed`) and pull one from the outfeed queue (`DequeueOutfeed`). These are the SE-level entry stubs; the queue model and transfer-manager interplay live on [Infeed/Outfeed](../runtime/infeed-outfeed.md).

### Function Map

| Function | Address | Size | Driver dispatch | Confidence |
|---|---|---|---|---|
| `TpuExecutor_EnqueueInfeed` | `0xeab9680` | 59 | direct call `DeepseaExecutor::EnqueueInfeed` | HIGH |
| `TpuExecutor_DequeueOutfeed` | `0xeab96c0` | 68 | direct call `DeepseaExecutor::DequeueOutfeed`, with an `AnyInvocable` completion | HIGH |

### Algorithm

```c
// TpuExecutor_EnqueueInfeed                           0xeab9680
int64 EnqueueInfeed(SE_StreamExecutor* handle, ..., StatusRep** out):
    rep = deepsea::executor::DeepseaExecutor::EnqueueInfeed(*handle)  // NOT a vtable slot
    return store_status(out, rep)

// TpuExecutor_DequeueOutfeed                          0xeab96c0
int64 DequeueOutfeed(SE_StreamExecutor* handle, ..., callback):
    // wraps the host callback in an absl::AnyInvocable (LocalInvoker/LocalManagerTrivial)
    DeepseaExecutor::DequeueOutfeed(*handle, ..., &any_invocable)
    return any_invocable.invoke()   // fires the trivially-managed completion
```

> **QUIRK —** `EnqueueInfeed` and `DequeueOutfeed` call the named `deepsea::executor::DeepseaExecutor::EnqueueInfeed` / `DequeueOutfeed` symbols *directly*, not through a vtable offset like every memory/sync function. `DequeueOutfeed` additionally builds an `absl::internal_any_invocable` (a `LocalManagerTrivial` / `LocalInvoker` pair) to carry the host's completion callback into the driver and invokes it on return. These two are the only transfer functions that are not pure vtable bounces.

---

## Sync

### Purpose

Block the host thread until work completes: until one stream drains (`BlockHostUntilDone`), or until *all* of the executor's streams quiesce (`SynchronizeAllActivity`). `WaitForEvent` (under [Event](#event)) is the stream-to-stream counterpart.

### Function Map

| Function | Address | Size | Driver dispatch | Confidence |
|---|---|---|---|---|
| `TpuExecutor_BlockHostUntilDone` | `0xeab9720` | 60 | `*arg1->vtable+120` (on the *stream*) | CERTAIN |
| `TpuExecutor_SynchronizeAllActivity` | `0xeab9760` | 12 | `driver_vtable+168` | CERTAIN |

### Algorithm

```c
// TpuExecutor_BlockHostUntilDone                      0xeab9720
int64 BlockHostUntilDone(_, Stream** stream, StatusRep** out):
    rep = (*(*(*stream) + 120))(*stream)   // dispatch on the STREAM vtable +120
    return store_status(out, rep)

// TpuExecutor_SynchronizeAllActivity                  0xeab9760  (12 bytes — bare bounce)
int64 SynchronizeAllActivity(SE_StreamExecutor* handle):
    return (*(*handle->driver + 168))(handle->driver)
```

`SynchronizeAllActivity` at 12 bytes is the smallest non-trivial function in the cluster: a single tail-call through `driver_vtable+168` with no status-out plumbing (it returns the driver's value verbatim). It is the purest example of the vtable-bounce pattern — load `*handle`, load `*(driver)`, call `+168`, return.

> **NOTE —** `BlockHostUntilDone` dispatches on the *stream* vtable (`+120`), consistent with `RecordEvent`/`WaitForEvent`/`CreateStreamDependency` — anything that operates on a specific stream addresses that stream's vtable, while executor-wide operations (`SynchronizeAllActivity`, `Allocate`, the memcpys) address the driver vtable. This split — stream-scoped vs executor-scoped dispatch — is the cleanest organising rule for the whole cluster.

---

## HBM / Host-Side

### Purpose

Three operations that do not fit the stream/event/memcpy regulars: compact the HBM allocator on a stream (`EnqueueCompactionOnStreamForHbm`), run a host-side callback as a device-stream node (`HostCallback`), and drop every loaded program from the executor (`UnloadAllPrograms`).

### Function Map

| Function | Address | Size | Driver dispatch | Confidence |
|---|---|---|---|---|
| `TpuExecutor_EnqueueCompactionOnStreamForHbm` | `0xeab97c0` | 66 | `driver_vtable+432` | HIGH |
| `TpuExecutor_HostCallback` | `0xeab9c20` | 149 | `*arg1->vtable+128` (on the *stream*), wraps a `std::function` | HIGH |
| `TpuExecutor_UnloadAllPrograms` | `0xeab9780` | 63 | `driver_vtable+424` | CERTAIN |

### Algorithm

```c
// TpuExecutor_UnloadAllPrograms                       0xeab9780
int64 UnloadAllPrograms(SE_StreamExecutor* handle, StatusRep** out):
    rep = (*(*handle->driver + 424))(handle->driver)
    return store_status(out, rep)

// TpuExecutor_EnqueueCompactionOnStreamForHbm         0xeab97c0
int64 EnqueueCompaction(SE_StreamExecutor* handle, Stream** stream, StatusRep** out):
    rep = (*(*handle->driver + 432))(handle->driver, *stream)
    return store_status(out, rep)

// TpuExecutor_HostCallback                            0xeab9c20
bool HostCallback(_, Stream** stream, fn_ptr, fn_ctx):
    closure = operator new(0x20)                  // {fn_ptr, fn_ctx, policy thunks}
    wrap closure in absl::AnyInvocable (RemoteManagerNontrivial/RemoteInvoker)
    rep = (*(*(*stream) + 128))(*stream, &any_invocable)   // stream vtable +128
    destroy any_invocable
    return rep == OK_SENTINEL
```

> **QUIRK —** `HostCallback` heap-allocates a 32-byte closure with `operator new(0x20)` holding `{fn_ptr, fn_ctx, __call_func thunk, __policy ptr}`, wraps it in a `std::function`→`absl::AnyInvocable` (the *non-trivial* `RemoteManager` path, unlike `DequeueOutfeed`'s trivial path), and hands it to the stream's `+128` slot. It is the only one of the five stream-vtable dispatchers that *also* heap-allocates — a reimplementer must free the closure (the decompile destroys the any-invocable after the call regardless of the status) or it leaks one allocation per host callback. `EnqueueCompactionOnStreamForHbm` (`+432`) and `UnloadAllPrograms` (`+424`) are the highest driver-vtable offsets in the cluster, sitting just past the memcpy/sync block.

---

## Topology

### Purpose

Return the executor's `TpuCoreLocation` — its chip/core coordinate in the mesh. The single odd member of the cluster: it has no status, no handle indirection through `*arg0`, and the smallest body (8 bytes).

### Function Map

| Function | Address | Size | Driver dispatch | Confidence |
|---|---|---|---|---|
| `TpuExecutor_GetCoreLocation` | `0xeab9320` | 8 | direct tail-call `DeepseaExecutor::GetCoreLocation(*handle)` | CERTAIN |

### Algorithm

```c
// TpuExecutor_GetCoreLocation                         0xeab9320  (8 bytes)
int64 GetCoreLocation(DeepseaExecutor** handle):
    return deepsea::executor::DeepseaExecutor::GetCoreLocation(*handle)  // pure tail call
```

> **NOTE —** at 8 bytes this is a `jmp` to the C++ method — no vtable indirection, no status wrapping. The returned `TpuCoreLocation` is the same opaque coordinate handle the `TpuCoreLocation_*` cluster operates on (see [TpuTopology & TpuCoreLocation](tpu-topology.md)). The executor exposes its own location; everything else about the mesh comes from `TpuTopology_*`.

---

## The Dispatch Pattern, Distilled

Every function in the cluster reduces to one of four dispatch shapes. A reimplementer who internalises these and the offset table needs nothing else.

| Shape | Members | Form |
|---|---|---|
| Driver-vtable bounce | `Allocate`, `Deallocate`, `GetAllocatorStats`, `DeviceMemoryUsage`, all 4 memcpys, `SynchronizeAllActivity`, `UnloadAllPrograms`, `EnqueueCompactionOnStreamForHbm`, `AllocateEvent`, `Init`/`GetStatus`, `CreateDeviceDescription` | `(*(*handle->driver + N))(driver, args)` |
| Stream-vtable bounce | `CreateStreamDependency`, `RecordEvent`, `WaitForEvent`, `BlockHostUntilDone`, `HostCallback` | `(*(*(*stream) + N))(stream, args)` — executor handle unused |
| Direct named call | `GetCoreLocation`, `EnqueueInfeed`, `DequeueOutfeed` | `DeepseaExecutor::Method(*handle, ...)` |
| Pure C | `Free` | `if (p) free(p)` |

```text
SE_StreamExecutor* handle  (arg0)
   │  *handle
   ▼
DeepseaExecutor* driver    ── executor-scoped ops dispatch here:  *(driver_vtable + N)
   │  *driver
   ▼
driver vtable  [+32 status][+72 alloc-event][+80/+88 async-memcpy]
               [+136 alloc][+144 dealloc][+168 sync-all]
               [+208/+216 sync-memcpy][+224 dealloc-stream][+256 mem-usage]
               [+272 device-desc][+320 alloc-stats][+424 unload][+432 compact]

Stream* (arg1/arg2)        ── stream-scoped ops dispatch here:  *(stream_vtable + N)
   └─ stream vtable [+56 add-dependency][+64 wait-event][+72 record-event]
                    [+120 block-host][+128 host-callback]
```

> **GOTCHA —** the offsets in the two diagrams above belong to **two different vtables**. `+72` on the *driver* is "allocate event"; `+72` on the *stream* is "record event". An implementation that flattens both into one offset space will mis-dispatch every stream operation. The discriminator is `arg0` (executor → driver vtable) versus `arg1`/`arg2` (stream → stream vtable).

---

## Related Components

| Name | Relationship |
|---|---|
| `TfTpu_ExecutorApiFn` (struct) | the function-pointer table these 25 functions populate, reached via `ExecutorApiFn() @ 0x20819360` |
| `stream_executor::tpu::TpuExecutor` (`0xe996ca0`+) | the host-side C++ shim that calls *into* these slots; owns `CreateStream` itself |
| `deepsea::executor::DeepseaExecutor` | the real driver object behind the `SE_StreamExecutor` handle; owns the vtable these functions dispatch through |
| `ApiConverter::FromC` | rebuilds `DeviceAddressBase` from the flat `SE_DeviceMemoryBase` for every memcpy/deallocate |
| `TpuTransferManager_*` | the literal-level host↔device transfer surface that layers above the executor's raw memcpy slots |

## Cross-References

- [The TfTpu C-API Shim](overview.md) — the `*ApiFn()` accessor pattern, opaque-handle convention, and `ApiConverter` marshalling these functions rely on
- [StreamExecutor Platform & Executor Model](../pjrt/stream-executor-host-interpreter.md) — the host-side `TpuExecutor` C++ shim that calls through this table
- [TpuTransferManager Roster](tpu-transfer-manager.md) — the higher-level transfer C ABI that uses the executor's memcpy/infeed/outfeed primitives
- [TpuProgram Roster](tpu-program-roster.md) — the program objects that `UnloadAllPrograms` releases from the executor
- [TpuPlatform & TpuNodeContext](tpu-platform-and-topology.md) — the platform that mints `TpuExecutor`s and the `TpuCoreLocation` returned by `GetCoreLocation`
- [Allocator Integration](../runtime/allocator-integration.md) — the HBM allocator behind `Allocate`/`Deallocate`/`GetAllocatorStats`/`EnqueueCompactionOnStreamForHbm`
- [Infeed/Outfeed](../runtime/infeed-outfeed.md) — the queue model behind `EnqueueInfeed`/`DequeueOutfeed`
