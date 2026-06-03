# Stream Semantics & Dependencies

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`). Other versions will differ.*

## Abstract

A *stream* in libtpu is a `stream_executor::Stream` — an ordered command queue against one device. It is the same abstraction CUDA programmers know as a `cudaStream_t`: every operation enqueued on a stream executes **in program order, FIFO, with no overlap between two items on the same stream**, and two *different* streams run concurrently unless an explicit dependency links them. libtpu carries three concrete `Stream` subclasses — `tensorflow::tpu::TpuStream` (the real device command stream, a thin C-ABI shim over `TfTpu_ExecutorApiFn`), `deepsea::executor::DeepseaStream` (the in-binary TPU driver stream that the shim ultimately drives, built on a `DeepseaRequestQueue`), and `stream_executor::host::HostStream` (the synchronous CPU stream used for host staging and trivial host ops). All three descend from `StreamCommon`, which holds the parent executor and enforces the invariant the whole model rests on: `parent != nullptr` (`stream_common.cc:44`).

The ordering contract has two halves. *Intra-stream* order is implicit and total: the `DeepseaRequestQueue` behind a `TpuStream` is a FIFO, so the Nth enqueued request cannot start before the (N−1)th retires — no fences are needed between successive ops on one stream. *Cross-stream* order is explicit and event-or-stream-based: `Stream::WaitFor(Stream*)` makes this stream block on all work currently enqueued on another stream, and `Stream::WaitFor(Event*)` makes it block on a single recorded `Event`. On the TPU these are not host-side blocks — `WaitFor` inserts a *waiter* into the device command queue (`DeepseaRequestQueue::EnqueueWaitFor`) so the dependency is resolved on-device without stalling the host. `RecordEvent` posts the matching completion token. The host backend implements the same surface with `absl::Notification`: `RecordEvent` notifies, `WaitFor(Event*)` blocks the calling thread, and `WaitFor(Stream*)` is a no-op because a synchronous host stream has already run everything.

The third axis is the *stream-type split*. XLA does not pour compute, host→device copies, and device→host copies onto one stream; it allocates a small fixed set of role-specific streams per executor — a compute stream plus separate H2D and D2H transfer streams — and serializes the producer/consumer hand-offs between them with the cross-stream `WaitFor`/`RecordEvent` machinery above. This page owns the **ordering model, the cross-stream dependency/wait semantics, and the stream-type split**. The execution entry that enqueues a program onto the compute stream is [ExecuteAsyncOnStream](execute-async-on-stream.md) / [LoadProgramAndEnqueueToStream](load-program-enqueue.md); the completion-event mechanism that fires when a stream drains is [Completion Loop & AsyncTrackingEvent](completion-loop.md); the host↔device copy transport itself is [Host↔Device DMA](../dma/host-device-dma.md).

For reimplementation, the contract is:

- **The `Stream` class layout and intra-stream FIFO invariant** — `StreamCommon`'s parent pointer at `+0x48`, the `parent != nullptr` CHECK, and why no per-op fence is required within a stream.
- **The two `WaitFor` overloads** — `WaitFor(Stream*)` (depend on another stream's enqueued work) vs `WaitFor(Event*)` (depend on one recorded event), and the `RecordEvent` that pairs with the latter — at byte level for both the TPU shim and the host backend.
- **The TPU realisation** — how `TpuStream::WaitFor`/`RecordEvent` route through `TfTpu_ExecutorApiFn` slots `+152/+184/+192`, and how the underlying `DeepseaStream` turns a stream dependency into a `DeepseaRequestQueue::CreateWaiter` + `EnqueueWaitFor` device-queue insert.
- **The compute / H2D / D2H stream split** — that transfers and compute occupy distinct streams and that producer→consumer ordering across them is carried only by `WaitFor`/`RecordEvent`, never by implicit cross-stream order.

| | |
|---|---|
| **Abstract base** | `stream_executor::Stream` (vtable-only) → `StreamCommon` (ctor `0x1d100280`) |
| **`StreamCommon` parent ptr** | `+0x48` (`this+9`); `parent != nullptr` CHECK @ `stream_common.cc:44` |
| **TPU stream** | `tensorflow::tpu::TpuStream` : `TpuStreamInterface` |
| **TPU driver stream** | `deepsea::executor::DeepseaStream` (request queue at `this+0x88` / `this+17`) |
| **Host stream** | `stream_executor::host::HostStream` : `StreamCommon` (ctor `0xfe6ec80`, 0x80 B) |
| **`WaitFor(Stream*)`** | TPU `0xe9989e0` · Host `0xfe6eec0` (no-op `true`) · Deepsea `0x1d0e9480` |
| **`WaitFor(Event*)`** | TPU `0xe998aa0` · Host `0xfe6eee0` (`Notification::WaitForNotification`) |
| **`RecordEvent`** | TPU `0xe998b80` · Host `0xfe6ef40` (`Notification::Notify`, CHECK @ `host_stream.cc:92`) |
| **TPU ExecutorApiFn slots** | `+152` create-dependency · `+184` record-event · `+192` wait-event · `+360` device |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

---

## 1. What a Stream Is

### Purpose

A `stream_executor::Stream` is a handle to one ordered command queue against one device. Work — a program launch, a memcpy, a memset, a host callback, an event record — is *enqueued* on a stream and the stream guarantees the items execute in enqueue order. The model is deliberately the CUDA-stream model: ordering is per-stream and implicit; concurrency is per-pair-of-streams and the default; cross-stream ordering is the caller's responsibility, expressed through events and `WaitFor`. There is no global program counter across streams.

### Class Layout

Every concrete stream descends from `StreamCommon`, the device-agnostic intermediate that holds the back-pointer to its owning executor. The ctor `stream_executor::StreamCommon::StreamCommon(StreamExecutor*)` @ `0x1d100280` is small and reveals the whole layout:

```c
// StreamCommon::StreamCommon(StreamExecutor* parent)        sub_1D100280
function StreamCommon_ctor(this, parent):
    this[0]            = &Stream_vtable             // off_21CA99F8
    this[+0x08]        = 0                          // status rep (OkStatus)
    this[+0x10]        = 1                          // (default count / live flag)
    this[+0x18]        = 0
    zero this[+0x30 .. +0x40]                       // name string head, priority slot
    this[+0x40]        = 0
    this[+0x48]        = parent                     // <-- parent StreamExecutor* (this+9)
    this[+0x50]        = 0
    this[+0x58]        = 1                          // (sub-stream / refcount default)
    zero this[+0x60 .. +0x7D]                       // sub-stream pool head
    if (parent == nullptr):                         // stream_common.cc:44
        LOG(FATAL) "parent != nullptr"
```

The single hard invariant is `parent != nullptr`: a stream with no executor is a programming error and aborts immediately. The parent pointer at `+0x48` is how a stream reaches its executor's `TfTpu_ExecutorApiFn` table (TPU) or thread pool (host) to enqueue work — every `Memcpy`, `WaitFor`, and `RecordEvent` below dereferences it. `HostStream` is a 0x80-byte object (ctor `0xfe6ec80`), consistent with this layout plus the host-specific notification slot; `TpuStream` adds the opaque `SE_Stream*` handle and the parent `TpuPlatform*` (read at `this+16/17/18` in the WaitFor/RecordEvent decompile).

> **NOTE —** `StreamCommon` reserves a *sub-stream pool* head (`+0x60..`). Upstream StreamExecutor lets a caller borrow a temporary child stream via `GetOrCreateSubStream`/`ReturnSubStream` for fork/join inside one logical stream; those slots are present but the borrow path was not exercised in any traced libtpu call site. The cross-stream dependency primitive libtpu actually uses is `WaitFor` (§3), not sub-streams. Confidence: HIGH (layout slots present; borrow callers not traced — LOW that they are reachable in this build).

### The Intra-Stream FIFO Invariant

Within one stream, order is total and implicit: item N+1 does not begin until item N has retired, and no two items on the same stream overlap. On the TPU this is not enforced by the StreamExecutor layer at all — it is a property of the `DeepseaRequestQueue` that backs every `DeepseaStream`. A `TpuStream` forwards each operation to its driver stream, which appends a `DeepseaRequest` to that queue; the device drains the queue in FIFO order. Consequently a reimplementation needs **no fence between successive ops on a single stream** — back-to-back `Memcpy` then program-launch on the same stream are already ordered. Fences (events) are needed *only* to cross between streams (§3). This is the single most important consequence of the model and the reason the stream-type split (§4) works.

---

## 2. The Stream Backends

libtpu links three `Stream` implementations. They share the `StreamCommon` base and the `WaitFor`/`RecordEvent`/`Memcpy` virtual surface, but differ entirely in *where* the ordered work lands.

| Backend | Class | Backing queue | Concurrency | Role |
|---|---|---|---|---|
| TPU shim | `tensorflow::tpu::TpuStream` | `SE_Stream*` → `TfTpu_ExecutorApiFn` | async, device-ordered | the real TPU command stream callers enqueue programs on |
| TPU driver | `deepsea::executor::DeepseaStream` | `DeepseaRequestQueue` (`this+0x88`) | async worker | in-binary stream the shim drives; owns the FIFO |
| Host CPU | `stream_executor::host::HostStream` | none (inline) | **synchronous** | host staging, trivial host ops, host callbacks |

### TpuStream — the C-ABI shim

`TpuStream` is a thin forwarder. It holds three fields the WaitFor/RecordEvent decompile reads directly: an opaque stream handle (`this+16`, `SE_Stream*`), the parent `SE_StreamExecutor*` (`this+17`), and the parent `TpuPlatform*` (`this+18`). Every operation resolves the singleton `TfTpu_ExecutorApiFn` table via `stream_executor::tpu::ExecutorApiFn()` and calls the appropriate slot. Slot `+360` returns the *device* object used for status harvesting; status is checked via `+408` (ok?), and on failure the code/message are pulled from `+400`/`+392` and packed into an `absl::Status` through the `status_helper.h` shim (`absl::Status::MakeRep(..., status_helper.h)`). This is the shared "C-shim → `absl::Status`" pattern across the entire TPU backend.

### DeepseaStream — the queue underneath the shim

Below the C-ABI, the real stream is `deepsea::executor::DeepseaStream`, whose dependency primitive is the byte-clearest evidence on this page (`0x1d0e9480`):

```c
// DeepseaStream::WaitFor(Stream* other)                     sub_1D0E9480
function DeepseaStream_WaitFor_Stream(this, other):
    waiter = DeepseaRequestQueue::CreateWaiter(other->queue)   // other[17] = other's request queue
    return DeepseaRequestQueue::EnqueueWaitFor(this->queue,    // this[17] = this stream's request queue
                                               waiter)
```

A cross-stream wait is a *waiter object* manufactured from the other stream's request queue and **enqueued onto this stream's queue** as just another FIFO item. The device, draining this stream in order, hits the waiter and blocks the stream (not the host) until the other queue has reached the recorded point. This is why TPU `WaitFor` is async: nothing on the host blocks. `DeepseaStream` also carries an `H2DTransferState::LaunchAndWait` helper (`0x1d0ea5e0` invoker) — direct evidence that the driver stream distinguishes a host→device transfer sub-state from compute work (§4).

### HostStream — synchronous, inline

The linked `HostStream` is the *synchronous* variant: there is no worker thread or task queue. Because every operation has already run inline on the calling thread by the time the next one is enqueued, the stream is trivially drained at all times. This shapes its `WaitFor`/`RecordEvent` directly (§3) and means `BlockHostUntilDone` returns `true` unconditionally. It exists for host-memory staging and trivial host ops, not as a full async CPU device.

---

## 3. Cross-Stream Dependencies

Two streams run concurrently by default. To order work on stream B after work on stream A, the caller inserts an explicit dependency. libtpu exposes the two `Stream` overloads every StreamExecutor backend implements, plus the `RecordEvent` that seeds the event-based one.

### `WaitFor(Stream*)` — depend on another stream

`B.WaitFor(&A)` makes B block until all work *currently enqueued* on A has completed. The TPU realisation (`0xe9989e0`) maps the foreign `Stream*` to its driver handle and asks the executor to create a dependency:

```c
// TpuStream::WaitFor(Stream* other)                         sub_E9989E0
function TpuStream_WaitFor_Stream(this, other):
    create_dep = ExecutorApiFn()[+152]                    // TfTpu create-stream-dependency
    self_handle = this[16]                                 // SE_Stream*
    self_se     = this[17]                                 // SE_StreamExecutor*
    se          = this[18]                                 // TpuPlatform* owning the map
    lock(se->mutex @ +144)                                 // absl::Mutex
    se_other = FlatHashMap<Stream*, SE_Stream*>(se+80).at(other)   // map foreign Stream* -> SE_Stream*
        // if absent: ThrowStdOutOfRange("raw_hash_map<>::at")
    unlock(se->mutex)
    ok = create_dep(self_se, self_handle, se_other)        // ExecutorApiFn()+152
    if (!ok):
        return Status(INTERNAL, "Failed to create stream dependency")   // tpu_stream.h:108
    return OkStatus
```

Two reimplementation-critical details: (1) the executor keeps a `FlatHashMap<Stream*, SE_Stream*>` at `executor+0x80`, guarded by an `absl::Mutex` at `executor+0x144`, that translates a *language-level* `Stream*` into the *driver-level* `SE_Stream*` the C-ABI understands; a `WaitFor` on an unregistered stream throws `out_of_range`. (2) The dependency is created via `TfTpu_ExecutorApiFn` slot `+152`; failure is reported as `INTERNAL` "Failed to create stream dependency" anchored to `tpu_stream.h:108`. Underneath, this becomes the `DeepseaRequestQueue::EnqueueWaitFor` waiter of §2 — the dependency is resolved on-device, the host is not stalled.

> **QUIRK —** `WaitFor(Stream*)` waits on the work *enqueued so far*, not on the stream forever. It snapshots: a subsequent op pushed onto A after the `WaitFor` is **not** covered. A reimplementation that treats the call as "B always trails A" is wrong; it is "B waits for A's current tail". This matches CUDA `cudaStreamWaitEvent` semantics applied to a stream's implicit tail rather than a named event.

### `WaitFor(Event*)` — depend on one recorded event

The finer-grained primitive waits on a single `Event`. The TPU side (`0xe998aa0`) looks the event up in the platform's event registry and calls the wait-event slot:

```c
// TpuStream::WaitFor(Event* e)                              sub_E998AA0
function TpuStream_WaitFor_Event(this, e):
    dev      = ExecutorApiFn()[+360](this)                 // device object for status
    platform = this[18]                                    // TpuPlatform*
    se_event = TpuPlatform::LookupEvent(platform, e)       // Event* -> SE_Event*
    wait_ev  = ExecutorApiFn(platform)[+192]               // TfTpu wait-for-event
    wait_ev(this[17], this[16], se_event, dev)             // (SE_SE*, SE_Stream*, SE_Event*, dev)
    status = harvest_status(dev)                            // +408/+400/+392 -> status_helper.h:38
    ExecutorApiFn(dev)[+384](dev)                           // free the device-status object
    return status
```

`RecordEvent` (`0xe998b80`) is the mirror image — identical structure, but it calls slot `+184` (record) instead of `+192` (wait), against the same `LookupEvent`-resolved `SE_Event*`. The pairing is the standard producer/consumer token: stream A enqueues `RecordEvent(e)` after its producing op; stream B enqueues `WaitFor(e)` before its consuming op; the device resolves the token ordering.

| Primitive | TPU `ExecutorApiFn` slot | Resolves via | On failure |
|---|---:|---|---|
| `WaitFor(Stream*)` | `+152` (create dependency) | `FlatHashMap<Stream*,SE_Stream*>` @ exec`+0x80` | `INTERNAL` "Failed to create stream dependency" (`tpu_stream.h:108`) |
| `WaitFor(Event*)` | `+192` (wait event) | `TpuPlatform::LookupEvent` | status via `status_helper.h:38` |
| `RecordEvent(Event*)` | `+184` (record event) | `TpuPlatform::LookupEvent` | status via `status_helper.h:38` |

### The host backend — `absl::Notification`

The synchronous `HostStream` implements the identical surface with notifications, and the contrast is instructive.

```c
// HostStream::WaitFor(Stream*)                              sub_FE6EEC0
function HostStream_WaitFor_Stream(this, other):
    return true                                              // nothing to wait for: host stream is inline

// HostStream::WaitFor(Event*)                               sub_FE6EEE0
function HostStream_WaitFor_Event(this, e):
    note = e[1]                                              // absl::Notification*
    rc   = e[2]                                              // shared_weak refcount (nullable)
    if (rc):
        atomic_inc(rc[1])
        Notification::WaitForNotification(note)              // block calling thread
        if (atomic_dec(rc[1]) == 0):
            vtable_destroy(rc); __release_weak(rc)
    else:
        Notification::WaitForNotification(note)
    return true

// HostStream::RecordEvent(Event*)                           sub_FE6EF40
function HostStream_RecordEvent(this, e):
    note = e[1]; rc = e[2]
    CHECK(!note->HasBeenNotified())                          // host_stream.cc:92 — record-twice is FATAL
    Notification::Notify(note)
    // (refcount release mirrors WaitFor)
    return true
```

Three things to carry across: (1) `WaitFor(Stream*)` is a **no-op returning `true`** — a synchronous host stream has already run all prior work on the caller thread, so there is nothing left to await. (2) `WaitFor(Event*)` genuinely blocks the *calling thread* on `Notification::WaitForNotification`, with a `shared_weak_count` refcount so the event can be released exactly once. (3) `RecordEvent` `Notify`s the notification and **CHECK-fails if the event was already notified** (`host_stream.cc:92`, `!notification->HasBeenNotified()`) — a host `Event` is single-shot; recording it twice aborts.

> **GOTCHA —** the host and TPU backends differ in *where* a `WaitFor` blocks. On the TPU, `WaitFor` enqueues a device waiter and returns to the host immediately (async). On the host, `WaitFor(Event*)` blocks the calling thread synchronously. A reimplementation that assumes one model for both will either busy-wait the host on TPU work or fail to actually serialize on the host. The unifying contract is the *ordering guarantee*, not the blocking behaviour.

> **QUIRK —** a host `Event` is single-use (the `host_stream.cc:92` CHECK). TPU events are looked up by handle and re-recorded through the driver each time, so they are reusable. Do not assume event objects are interchangeable across backends.

---

## 4. The Compute / Transfer Stream Split

### Why three streams, not one

XLA does not serialize compute and data movement onto a single stream. An executor allocates a small fixed set of role-specific streams — a **compute** stream for program launches, an **H2D** stream for host→device input copies, and a **D2H** stream for device→host output copies. The reason is concurrency: while the compute stream runs program K, the H2D stream can already be staging the inputs of program K+1 and the D2H stream can be draining the outputs of program K−1. Putting all three on one FIFO would force every copy to block every launch, since intra-stream order is total (§1.3).

The byte-level fingerprint of the split is `DeepseaStream`'s dedicated `H2DTransferState::LaunchAndWait` sub-machine (`H2DTransferState` invoker `0x1d0ea5e0`) — a host→device transfer carries its own launch-and-wait state distinct from the compute path, exactly the structure expected if transfers run on their own stream. The `TpuStream::EnqueueTransferHostToDevice` / `EnqueueTransferDeviceToHost` pair (`0xeab9960` / `0xeab99e0`) are the enqueue entries onto the transfer streams; a program launch is enqueued by [ExecuteAsyncOnStream](execute-async-on-stream.md) onto the compute stream.

### How the streams are kept consistent

The split *creates* a correctness obligation: program K's inputs must be on the device before K runs, and K's outputs must not be copied out before K finishes. Since the streams are independent, that ordering is carried **only** by the cross-stream `WaitFor`/`RecordEvent` of §3 — there is no implicit ordering between distinct streams. The canonical hand-off is:

```text
H2D stream :  Memcpy(input)            RecordEvent(in_ready) ─┐
                                                              │  WaitFor(in_ready)
compute    :                                                 └─► LaunchProgram(K)   RecordEvent(done) ─┐
                                                                                                       │ WaitFor(done)
D2H stream :                                                                                           └─► Memcpy(output)
```

Each arrow is one `RecordEvent` on the producing stream paired with one `WaitFor(Event*)` on the consuming stream (or a coarser `WaitFor(Stream*)` snapshot of the producer's tail). On the TPU these resolve to `DeepseaRequestQueue` waiters, so the three streams stay maximally concurrent — only the genuine data dependencies serialize, and they serialize *on the device*, leaving the host free to keep enqueuing. The completion event the runtime watches to know a launch retired is the same `Event` machinery, surfaced through [Completion Loop & AsyncTrackingEvent](completion-loop.md).

> **NOTE —** the *number* and exact role-assignment of the per-executor streams (one compute + one H2D + one D2H, versus a small pool of each) is the design XLA's StreamExecutor pool establishes upstream; in this binary the byte evidence confirms (a) the transfer-vs-compute split exists (`H2DTransferState`, the two `EnqueueTransfer*` entries) and (b) the only cross-stream ordering primitive is `WaitFor`/`RecordEvent`. The precise stream-count per executor was not individually traced. Confidence: HIGH on the split and the ordering mechanism; MEDIUM on the exact stream cardinality.

---

## 5. Considerations for a Reimplementer

- **Intra-stream needs no fences.** Two ops on the same stream are already ordered by the `DeepseaRequestQueue` FIFO. Inserting an event between them is wasteful and, on the host, would CHECK-fail on the second record. Only cross-stream hops need a `WaitFor`/`RecordEvent` pair.
- **`WaitFor(Stream*)` is a tail snapshot, not a permanent link.** Re-issue it (or use a fresh event) for each batch of work you want to depend on; it does not auto-track future enqueues (§3 QUIRK).
- **Register every stream with its executor before depending on it.** TPU `WaitFor(Stream*)` looks the foreign stream up in the executor's `FlatHashMap<Stream*,SE_Stream*>` and throws `out_of_range` on a miss. The map is the only bridge from a C++ `Stream*` to the driver `SE_Stream*`.
- **Host events are single-shot; TPU events are reusable.** Mirror the `host_stream.cc:92` CHECK if you build a synchronous host backend — recording an already-notified event must abort, not silently succeed.
- **Blocking semantics are backend-specific.** TPU `WaitFor` is async (device waiter); host `WaitFor(Event*)` blocks the caller thread. Preserve the *ordering* contract; do not copy one backend's blocking behaviour onto the other.
- **Keep the transfer/compute split.** Collapsing H2D, D2H, and compute onto one stream is functionally correct but destroys the launch-latency hiding the model is built for; the device will idle between copies and launches.

---

## Related Components

| Name | Relationship |
|---|---|
| `stream_executor::Stream` / `StreamCommon` | the abstraction this page documents; parent-ptr layout and the `parent != nullptr` invariant |
| `tensorflow::tpu::TpuStream` | the C-ABI shim implementing `WaitFor`/`RecordEvent` over `TfTpu_ExecutorApiFn` |
| `deepsea::executor::DeepseaStream` | the in-binary driver stream; `DeepseaRequestQueue` is the FIFO that gives intra-stream order, `EnqueueWaitFor` the cross-stream dependency |
| `stream_executor::host::HostStream` | the synchronous host backend; `Notification`-based events, no-op `WaitFor(Stream*)` |
| `TfTpu_ExecutorApiFn` table | C-ABI the TPU shim forwards to (slots `+152`/`+184`/`+192`/`+360`/`+384`/`+392`/`+400`/`+408`) |

## Cross-References

- [Overview](overview.md) — the execute path from PJRT down to the stream, the frame this page sits in
- [ExecuteAsyncOnStream](execute-async-on-stream.md) — the entry that enqueues a program onto the compute stream
- [LoadProgramAndEnqueueToStream](load-program-enqueue.md) — program load + enqueue, the producer of compute-stream work
- [Completion Loop & AsyncTrackingEvent](completion-loop.md) — the completion-event mechanism that fires when a stream drains
- [Host↔Device DMA](../dma/host-device-dma.md) — the H2D/D2H copy transport that runs on the transfer streams
- [Continuation Queue](../dma/continuation-queue.md) — the device-side back-to-back program chaining that keeps the compute stream busy without host round-trips
