# Completion Loop & AsyncTrackingEvent

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset. Other versions will differ.*

## Abstract

When a PJRT caller launches a program, libtpu does **not** hand the host a stream handle to poll. It hands back a *future* — a `tsl::Future<void>` wrapping a `tsl::AsyncValue` — and the runtime promises to make that async value *available* when the device retires the launch. The whole completion model is built on `tsl::AsyncValue`: a refcounted, single-assignment cell that holds either "not-yet", a value, or an error, and that fires a list of enqueued waiters the instant it transitions to available. This is the TFRT (TensorFlow Runtime) async-value model, and it is the modern PJRT path — [`xla::TpuClient`](../pjrt/client-and-device.md) over `tpu::System`, surfaced to the C API as the [`PjRtFuture` / `PJRT_Event`](../pjrt/events-and-async.md) async family — **not** the legacy `stream_executor::Stream` + `RecordEvent`/`WaitFor` machinery that [Stream Semantics](stream-semantics.md) documents. There is no host poll loop spinning on a device flag; completion is *push*, delivered as a waiter callback when an `AsyncValue` is fulfilled.

A single launch threads three linked objects. (1) The enqueue path mints a **linked promise pair** with `xla::CommonPjRtClient::CreateLinkedUserPromise` (`0xf918980`): one half is a `tsl::PromiseBase<absl::Status>` whose `Future` is returned to the user; the other half is a device-side `xla::PjRtDeviceEventPromise` (a `TpuTrackedDeviceEventPromise`) wrapping a `tsl::AsyncValueRef<tpu::TpuEvent>`. The two are *linked* so that resolving the device event forwards into the user future. (2) That device event is registered as the launch's *define event* — `xla::TpuHostTransferManager::SetExecuteEvent` (`0xf813760`) stamps it onto the host transfer manager, and `tpu::System::Execute` (`0x1d0b33e0`) receives it in its `define_events` span. (3) When the program completes on device, `tpu::System::Execute`'s completion lambda runs `tpu::TpuEventIssuer::FulfillArgs`, which resolves the define event; `TpuTrackedDeviceEventPromise::SetReady` (`0xf837920`) then `ForwardTo`s the user's async value, firing every waiter — output-buffer releasers and the user's `OnReady` done-callback alike.

This page owns the **completion-event lifecycle**: how the per-launch event is created (`CreateLinkedUserPromise` / `CreateLinkedEventPromise` / `CreateAsyncTrackingEvent`), how it is registered as a define event and carried into `tpu::System::Execute`, how the device-side `TpuEventIssuer` sequence-point engine *fulfills* it when the launch retires, and how fulfilment fans out to the user done-callback (`PJRT_Event_OnReady`) and to output-buffer release. The enqueue that *produces* the launch is [LoadProgramAndEnqueueToStream](load-program-enqueue.md); the cross-stream `WaitFor`/`RecordEvent` wait primitives on the *legacy* SE path belong to [Stream Semantics](stream-semantics.md) (link, do not duplicate); the execute entry is [ExecuteAsyncOnStream](execute-async-on-stream.md).

For reimplementation, the contract is:

- **The `tsl::AsyncValue` completion primitive** — a refcounted single-assignment cell whose state byte gates "available", whose `& 8` allocated flag and `_InterlockedDecrement` drive its destroy, and whose waiter list is the *only* notification channel. No polling.
- **The linked promise pair** — `CreateLinkedUserPromise` returns `{ PromiseBase<absl::Status> user_promise, AsyncValue* device_event }`; the user gets a `tsl::Future`, the runtime keeps the device-side promise and links one into the other.
- **The define-event registration + fulfilment path** — `SetExecuteEvent` registers the per-launch `TpuEvent` define event; `tpu::System::Execute` carries it in `define_events`; `TpuEventIssuer::{NextSequencePoint, RunWhenDepsReady, FulfillArgs}` fulfils it in device-completion order; `TpuTrackedDeviceEventPromise::SetReady` forwards it to the user value via `tsl::IndirectAsyncValue::ForwardTo`.
- **The done-callback fan-out** — fulfilment fires the `AsyncValue` waiter list: `PJRT_Event_OnReady`-registered callbacks run, and the output buffers' definition events become available so their backing HBM is released.

| | |
|---|---|
| **Completion primitive** | `tsl::AsyncValue` (refcounted single-assignment; `& 8` = allocated, `_InterlockedDecrement` destroy) |
| **User-facing handle** | `tsl::Future<void>` → `PJRT_Event` (slots 10–14) |
| **Linked promise pair** | `xla::CommonPjRtClient::CreateLinkedUserPromise` @ `0xf918980` (776 B) |
| **Device-event promise** | `xla::TpuTrackedDeviceEventPromise` (`Set`/`SetReady`/`SetError`) over `tsl::AsyncValueRef<tpu::TpuEvent>` |
| **Per-device event factory** | `xla::TpuDevice::CreateAsyncTrackingEvent` @ `0xf7ff1c0` (891 B) · C-ABI `PJRT_Device_CreateAsyncTrackingEvent` |
| **Define-event register** | `xla::TpuHostTransferManager::SetExecuteEvent` @ `0xf813760` (570 B) |
| **Launch + define events** | `tpu::System::Execute` @ `0x1d0b33e0` (10370 B), `define_events` = last span arg |
| **Device sequencer** | `tpu::TpuEventIssuer::{NextSequencePoint 0x1d0d38e0, Sequence::Next 0x1d0d3940, RunWhenDepsReady 0x1d0d4640, FulfillArgs}` |
| **Resolve → forward** | `TpuTrackedDeviceEventPromise::SetReady` @ `0xf837920` → `tsl::IndirectAsyncValue::ForwardTo` |
| **Done-callback slot** | `pjrt::PJRT_Event_OnReady` @ `0xf86fc60` · `PJRT_Event_Await` @ `0xf86fa80` · `PJRT_Event_IsReady` @ `0xf86f9e0` |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

---

## 1. The Completion Primitive — `tsl::AsyncValue`

### Purpose

Everything on this page reduces to one object: `tsl::AsyncValue`. It is a heap cell, refcounted, that begins life *unavailable* and is assigned exactly once — to a value (`tpu::TpuEvent`, `absl::Status`, `void`) or to an error. Assignment is the *only* edge that fires its waiter list. The runtime never polls a device-resident completion flag from the host; instead the host registers a callback on the async value and the device-completion lambda *fulfils* the value, which runs the callback. This is the inversion that distinguishes the modern PJRT path from the legacy SE `BlockHostUntilDone`/`RecordEvent` poll-and-notify model of [Stream Semantics](stream-semantics.md).

### Layout and the Refcount/State Discipline

Every `AsyncValue` touched on the completion path obeys the same byte discipline, visible identically in `CreateLinkedUserPromise`, `SetReady`, and `tpu::System::Execute`'s waiter nodes:

```c
// The AsyncValue refcount + state idiom (observed at every release site)
function AsyncValue_DropRef(av):                 // e.g. SetReady 0xf837920 lines 36-54
    if av == nullptr: return
    if (av->byte[4] & 8) == 0:                   // bit 3 of +4 = "heap-allocated"
        return                                   // statically-owned: never destroy
    if av->refcount == 1 ||                      // +0x00 = atomic refcount (uint32)
       AtomicDecrement(&av->refcount) == 0:
        AsyncValue::Destroy(av)                  // last ref: free
```

Three reimplementation-critical facts. (1) The 32-bit word at offset `+0` is the atomic refcount; release is `_InterlockedDecrement` with a fast-path that skips the atomic when the count is already 1. (2) The byte at `+4` carries flags; **bit 3 (`& 8`) means "heap-allocated"** — a statically-owned async value (a process-singleton ready event) is recognised by a clear bit and is *never* destroyed, so the same drop-ref code is safe to call on both. (3) Acquiring a ref before handing the value to a logger or waiter list is `_InterlockedIncrement` guarded by the same `& 8` test (`SetReady` line 26). A reimplementation that frees a non-heap async value, or that polls instead of registering a waiter, breaks the model.

> **NOTE —** the concrete waiter-list machinery (`tsl::AsyncValue::EnqueueWaiter<Lambda>` minting per-call `Node` types with `RunWaiterAndDeleteWaiterNode`) is generated once per closure type; the binary carries a distinct instantiation per closure (dozens across the runtime), including the `EnqueueWaiter<…::$_0>` waiters under `tpu::System::Execute` and `SetExecuteEvent` (each emitting its own typeinfo, vtable, `Node` ctor/dtor, and `RunWaiterAndDeleteWaiterNode`). The shape is always the same: a node holding the moved closure, threaded onto the value's waiter list under a `WaitersAndState` CAS, and run-then-deleted on fulfilment.

---

## 2. Creating the Per-Execution Event — The Linked Promise Pair

### Purpose

The enqueue path needs *two* views of the same completion: a `tsl::Future<void>` to return to the user (so JAX/PyTorch-XLA can `await` or attach a done-callback), and a device-side promise the runtime keeps so it can resolve the event when the launch retires. `xla::CommonPjRtClient::CreateLinkedUserPromise` mints both at once and *links* them, so a single resolution propagates from device to user.

### Algorithm

The decompile of `CreateLinkedUserPromise` (`0xf918980`) is byte-clear once the boilerplate `__function::__policy` move/destroy churn is stripped:

```c
// xla::CommonPjRtClient::CreateLinkedUserPromise(memspace, file, line, label)   0xf918980
// returns a 0x50-byte struct: { user_promise, device_event, profiling_callbacks... }
function CreateLinkedUserPromise(out, client, memspace, file, line, label):
    PromiseMaker<void>::Make(&user_promise, &linked_av, profiling_keys)  // line 41
        // user_promise : tsl::PromiseBase<...>; linked_av : the AsyncValue the user's Future observes

    // (vtable +536) CreateLinkedEventPromise(memspace, label) — mint the DEVICE-side promise
    client->vtable[+536](&device_event_av, memspace, file, line, label, &linked_av)   // line 77

    // (vtable +528) CreateUserFuture / TrackFuture — wrap linked_av as the user Future
    client->vtable[+528](client, memspace, file_b, line_b, &device_event_av)          // line 87

    out[0] = user_promise        // PromiseBase<absl::Status> the runtime fulfils
    out[1] = device_event_av     // AsyncValue the device-side promise resolves
    // out[0x10..0x40] = profiling key callbacks (TraceMe span open/close)
    return out
```

The two vtable calls are the link. Slot `+536` is the virtual `CreateLinkedEventPromise`; for the TPU client this is `xla::TpuClient::CreateLinkedEventPromise` (`0xf7faae0`, 610 B), which builds the device-side `TpuTrackedDeviceEventPromise` over a fresh `tsl::AsyncValueRef<tpu::TpuEvent>` and wires it to the just-made `linked_av`. Slot `+528` produces the user-observable future (the `TrackFuture`/profiled-future wrapper, §5). The returned struct's first two qwords are the promise the runtime will fulfil and the async value the user will await — the *same* completion, two handles.

> **QUIRK —** `CreateLinkedEventPromise` is virtual and overridden per client: `CommonPjRtClient` (base), `TpuClient` (TPU device events), `PjRtCpuClient` (CPU), and there is a guard string `"CreateLinkedEventPromise is not supported"` for clients that refuse it. A reimplementation must route through the *client's* override — the linking semantics (which `AsyncValue` forwards into which) live there, not in the common framework.

### The `TpuTrackedDeviceEventPromise` — the Device Half

The device-side promise is `xla::TpuTrackedDeviceEventPromise`, an `xla::PjRtDeviceEventPromise` specialised for TPU. Its surface is three setters plus an accessor:

| Method | Address | Effect |
|---|---:|---|
| `Set(PjRtDeviceEventRef)` | `0xf837a40` | bind a concrete device event (e.g. a `TpuEvent` from a transfer) |
| `SetReady()` | `0xf837920` | resolve to a *ready* (`tpu::ReadyTpuEvent`) event and forward to the user value |
| `SetError(absl::Status)` | `0xf83c580` | resolve the value to an error status (poisons the future) |
| `async_value()` | `0xf83c560` | the underlying `tsl::AsyncValue*` (for waiter registration) |

`SetReady` is the canonical resolution and the byte-clearest evidence of the *forward* step. Its decompile (`0xf837920`):

```c
// xla::TpuTrackedDeviceEventPromise::SetReady()                        0xf837920
function SetReady(this):
    client = this->client_via_device->...()        // this[2] -> device -> client (vtable +16)  line 18
    loc    = this->device->shared_mem_location()    // vtable +24                                line 19
    logger = client->pending_event_logger(loc)      // optional event tracer
    ready  = tpu::ReadyTpuEvent()                    // an already-available TpuEvent             line 21
    if logger:
        logger->Log(ready, " TpuTrackedDeviceEventPromise::SetReady", 39)   // diagnostic string  line 27
    indirect = this->indirect_av                     // this[3] = tsl::IndirectAsyncValue*        line 32
    IndirectAsyncValue::ForwardTo(indirect)          // <-- splice ready event into user value    line 35
    // drop refs on ready + indirect per the §1 discipline
```

The mechanism is `tsl::IndirectAsyncValue::ForwardTo`: the promise holds an *indirect* async value (a placeholder previously returned to the user), and `ForwardTo` splices the now-ready concrete `TpuEvent` into it. The indirect value becomes available; its waiter list fires. `SetError` does the symmetric thing with an error status; `Set` forwards a caller-supplied device event instead of a ready one. This is the single point where device completion becomes host-observable.

---

## 3. CreateAsyncTrackingEvent — The Named Per-Device Event

### Purpose

`CreateLinkedUserPromise` is the in-process mint used inside the execute path. The PJRT C-API also exposes a *standalone* event factory so a framework can create a tracking event, hand it across the boundary, and resolve it later — the explicit counterpart of the page's title. This is `xla::TpuDevice::CreateAsyncTrackingEvent` (`0xf7ff1c0`, 891 B), reached over the C-ABI through `pjrt::PJRT_Device_CreateAsyncTrackingEvent`.

### Function Map

| Function | Address | Role |
|---|---:|---|
| `pjrt::PJRT_Device_CreateAsyncTrackingEvent` | (slot) | C-ABI entry; unwraps `PJRT_Device_CreateAsyncTrackingEvent_Args` |
| `xla::TpuDevice::CreateAsyncTrackingEvent(string_view label)` | `0xf7ff1c0` | mint a labelled device tracking event (returns a promise/future pair) |
| `xla::PjRtCpuDevice::CreateAsyncTrackingEvent` | — | CPU sibling (same surface, CPU events) |
| `xla::MegaScalePjRtDevice::CreateAsyncTrackingEvent` | `0xe6eb780` | multi-slice decorator; forwards to the wrapped device |

The `string_view` argument is a human-readable label (the same label threaded through `CreateLinkedUserPromise`'s `file`/`line`/`label` triple), used by the `pending_event_logger` diagnostic path seen in `SetReady` line 27. The event a tracking-event call mints is the same `tsl::AsyncValue`-backed object; the caller resolves it via the returned promise (`SetReady`/`SetError`) exactly as the execute path does internally.

> **NOTE —** the per-device factory and the per-launch `CreateLinkedUserPromise` mint the *same kind* of object — a linked `{promise, AsyncValue}` pair. They differ only in *who* resolves it: a tracking event is resolved by the framework that created it (e.g. to gate a dependent launch), whereas the execute-path event is resolved by `tpu::System::Execute`'s completion lambda (§4). Both surface to the user as a `tsl::Future` / `PJRT_Event`.

---

## 4. Registering and Fulfilling the Define Event

### Purpose

Minting the event is half the lifecycle; the other half is *binding* it to the launch so the device resolves it on retirement. The per-launch event is registered as a **define event** (it *defines* the launch's completion and the readiness of its output buffers), carried into `tpu::System::Execute`, and fulfilled by the device-side `TpuEventIssuer` when the program drains.

### SetExecuteEvent — Stamping the Define Event

Inside `xla::TpuExecutableLoadState::ExecuteLaunchRaw` (`0xf8109a0`, the per-launch driver), after `CreateLinkedUserPromise` mints the completion pair, the device-side event is registered on the host transfer manager:

```c
// xla::TpuHostTransferManager::SetExecuteEvent(AsyncValueRef<tpu::TpuEvent>&)   0xf813760
function SetExecuteEvent(this, execute_event):
    // store the launch's TpuEvent as THE execute (define) event for this transfer manager,
    // and register a waiter so host-transfer completion is gated on it.
    this->execute_event = execute_event                  // retain (AcquireRef)
    AsyncValue::EnqueueWaiter(execute_event.av, on_execute_ready_lambda, ...)  // $_0 waiter
```

The functions JSON confirms the waiter: `tsl::AsyncValue::EnqueueWaiter<...SetExecuteEvent(...)::$_0>` — `SetExecuteEvent` threads a continuation onto the execute event's async value so that host-side transfer bookkeeping (and the host transfer manager's own teardown) runs when the launch completes. This is how output device→host copies learn the compute is done.

### tpu::System::Execute — Carrying define_events

`tpu::System::Execute` (`0x1d0b33e0`, 10370 B) is the device-runtime entry. Its signature is the definitive evidence of the wait/define event model (byte-confirmed from the demangled symbol):

```c
// tpu::System::Execute(...)                                            0x1d0b33e0
tpu::System::Execute(
    AsyncValueRef<tpu::ProgramHandle>            program,
    api::ExecuteOptions                          options,
    Span<AsyncValueRef<tpu::TpuBufferBase>>      inputs,
    Span<AsyncValueRef<tpu::TpuBufferBase>>      outputs,
    Span<AsyncValueRef<tpu::TpuEvent>>           wait_events,    // launch BLOCKS until these are ready
    Span<AsyncValueRef<tpu::TpuEvent>>           define_events)  // launch FULFILS these on completion
```

`wait_events` is the input-dependency set (the program does not start until those events resolve — input H2D copies, prior launches). `define_events` is the output set — the events *this* launch makes available when it retires. The per-launch event from `CreateLinkedUserPromise` rides in `define_events`. The completion edge is a lambda (`$_0`) registered inside `Execute` that runs on device completion and invokes `tpu::TpuEventIssuer::FulfillArgs`:

```c
// the completion lambda registered inside tpu::System::Execute       Execute::$_1
function on_device_complete(fulfill_args):           // FulfillArgs, run by the TpuEventIssuer
    // success path:
    issuer.Fulfill(fulfill_args) -> {                // resolves each define_event
        for ev in define_events:
            ev.promise.SetReady()                    // §2: ForwardTo user value
    }
    // error path uses the sibling $_3 lambda taking FulfillOnErrorArgs ->
    //   ev.promise.SetError(status)                 // poisons the futures
```

### TpuEventIssuer — the Device Sequencer

The legacy SE path orders work with a per-stream FIFO (`DeepseaRequestQueue`, see [Stream Semantics](stream-semantics.md)). The modern path orders it with `tpu::TpuEventIssuer`: a sequence-point + dependency-DAG engine that decides *when* a launch's `FulfillArgs` lambda may run, expressed entirely over `tsl::AsyncValue` dependencies.

| Function | Address | Role |
|---|---:|---|
| `tpu::TpuEventIssuer::NextSequencePoint(int)` | `0x1d0d38e0` | allocate the next sequence point (ordering token) |
| `tpu::TpuEventIssuer::Sequence::Next()` | `0x1d0d3940` | advance the sequence (chains successive launches) |
| `tpu::TpuEventIssuer::RunWhenDepsReady(...)` | `0x1d0d4640` | register the fulfil/fulfil-on-error callbacks to run once all deps' `AsyncValue`s resolve |
| `tpu::TpuEventIssuer::AggregateDeps<Span<...>>(...)` | — | collapse input buffer/event refs into one dependency vector |
| `tpu::CreateCountTrackingTpuEventIssuer(int, ConcurrentWorkQueue*, SystemEventTracker*)` | `0x1d0d3820` | issuer flavour that counts in-flight events (tracking) |
| `tpu::CreateNonTrackingTpuEventIssuer(int, ConcurrentWorkQueue*)` | `0x1d0d37a0` | lightweight issuer flavour (no counting) |

`RunWhenDepsReady` is the heart: its signature takes the define event (`AsyncValueRef<TpuEvent>`), two spans of `RCReference<AsyncValue>` dependencies, a `FulfillArgs` success callback, a `FulfillOnErrorArgs` error callback, and a `SequencePoint` async value. It registers the callbacks to fire once *all* dependencies are available — exactly the DAG join that SE achieves with `WaitFor`. When the join completes, the success callback runs `FulfillArgs` (resolving the define event → `SetReady` → user value), or the error callback runs `FulfillOnErrorArgs` (→ `SetError`).

> **QUIRK —** there are two issuer flavours and the choice changes completion bookkeeping, not ordering. `CountTrackingEventIssuer` (built with a `SystemEventTracker*`) increments/decrements an in-flight count as events issue and fulfil — used when the runtime must report pending work (e.g. throttling, telemetry). `NonTrackingEventIssuer` skips the count. Both implement the same `IssueEventImpl(IssueArgs)` virtual; a reimplementation that hard-codes one will either lose the in-flight accounting or pay for it where it is not wanted.

---

## 5. The Done-Callback Fan-Out and Buffer Release

### Purpose

Fulfilment of the define event is the *cause*; firing the async value's waiter list is the *effect*. Two distinct consumers sit on that list: the user's done-callback (registered through `PJRT_Event_OnReady`) and the output buffers' release path (their definition events become available, allowing their HBM to be freed once all references drop).

### The User Future and PJRT_Event

The user half of the linked pair is wrapped into a `tsl::Future<void>` by `xla::TpuClient::TrackFuture` (`0xf7fad60`, 258 B) — or `CreateProfiledFuture` (`0xf7fae80`, 445 B) when profiling is on, which wraps the future with TraceMe span open/close callbacks keyed by `tsl::FutureHelpers::ProfilingKeys`. That future is boxed into a `PJRT_Event` (C-API slots 10–14). The done-callback surface:

| C-API slot | Function | Address | Effect |
|---|---|---:|---|
| `PJRT_Event_IsReady` | `pjrt::PJRT_Event_IsReady` | `0xf86f9e0` | non-blocking: is the async value available? |
| `PJRT_Event_Await` | `pjrt::PJRT_Event_Await` | `0xf86fa80` | block the caller until available (the only *blocking* path) |
| `PJRT_Event_OnReady` | `pjrt::PJRT_Event_OnReady` | `0xf86fc60` | register a callback to run *on* fulfilment — the push done-callback |
| `PJRT_Event_Error` | `pjrt::PJRT_Event_Error` | `0xf86fba0` | read the error status if the value resolved to an error |

`PJRT_Event_OnReady` is the canonical completion delivery: it enqueues a waiter on the underlying async value. When `SetReady`'s `ForwardTo` makes the value available (§2), the waiter runs the user's callback on a runtime thread. `PJRT_Event_Await` is the blocking fallback — it is the *only* place the host thread parks on completion, and most callers avoid it in favour of `OnReady`.

### Buffer Release

The output buffers handed back from `ExecuteLaunchRaw` are not yet valid when the call returns — each carries its own definition event (the same define event, or one derived from it). A buffer's backing HBM cannot be reused until its definition event resolves. Because the define event is *the* launch-completion event, fulfilment (§4) simultaneously: (1) makes the output buffers readable (their definition events available), and (2) drops the runtime's references that were held alive for the duration of the launch. When the last `RCReference` to a `TpuBuffer` drops — user release plus the runtime's now-fired internal hold — the `tpu::TpuBuffer` is returned to the allocator. Errors take the parallel route: `SetError` resolves the user future *and* the output definition events to the error status, so dependents see the failure rather than reading uninitialised device memory.

> **GOTCHA —** the user future resolving "ready" means the *launch retired*, not that a device→host output copy finished. Output buffers are device-resident; reading them on the host still requires a `CopyRawDeviceToHostAndReturnEvent` whose own `TpuEvent` is gated on this completion. A reimplementation that treats the execute future as "outputs are in host memory" will read device handles. The execute future gates *device-side* validity; the copy event gates *host-side* availability.

### Error Propagation and the Ready Shortcut

Two edge behaviours a reimplementer must mirror. (1) `xla::TpuClient::CreateErrorEvent(absl::Status, PjRtMemorySpace*)` (`0xf808420`, 493 B) mints an *already-errored* event — used when the launch is rejected before it reaches the device (bad arguments, poisoned executable). It resolves immediately to the error, so dependents fail fast. (2) `tpu::ReadyTpuEvent()` (used inside `SetReady`) returns an *already-available* `TpuEvent` — a process-shared singleton recognised by the cleared `& 8` allocated bit (§1), so dropping a ref on it is a no-op. The ready shortcut means a launch with no real device wait (or a trivially-complete one) resolves its define event without a round trip.

---

## 6. Considerations for a Reimplementer

- **Push, not poll.** Completion is delivered by firing an async value's waiter list, never by spinning on a device flag from the host. Register an `OnReady` waiter; reserve `Await` for the rare blocking caller. A poll loop is the wrong shape for this runtime.
- **The linked pair is one completion, two handles.** Do not model the user future and the device event as independent objects that must be kept in sync. `CreateLinkedUserPromise` links them once (`ForwardTo`); resolving the device side resolves the user side automatically. Two-way sync logic is a bug surface that does not exist here.
- **Respect the `& 8` allocated bit.** The same drop-ref/destroy code runs over both heap async values and process-shared ready/error singletons. Destroying a non-heap value (clear `& 8`) corrupts the singleton. Gate every `Destroy` on the bit.
- **define_events vs wait_events are not interchangeable.** `wait_events` block the launch; `define_events` are fulfilled by it. Swapping them deadlocks (a launch waiting on the event it is supposed to produce) or never signals completion.
- **Pick the issuer flavour deliberately.** `CountTracking` maintains in-flight accounting (telemetry/throttle); `NonTracking` does not. The ordering DAG (`RunWhenDepsReady`) is identical between them — only the bookkeeping differs.
- **Execute-ready ≠ outputs-in-host-memory.** The execute future gates device-side validity; a separate D2H copy event gates host readability (§5 GOTCHA). Surface both to callers, or they will read device handles as if they were host pointers.
- **This is the modern path only.** None of this touches `stream_executor::Stream::RecordEvent`/`WaitFor`/`BlockHostUntilDone`. The legacy SE completion model (`absl::Notification`-backed host events, device-queue waiters) is a parallel stack documented in [Stream Semantics](stream-semantics.md); PJRT launches never enter it.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::CommonPjRtClient::CreateLinkedUserPromise` | mints the linked `{user_promise, device_event}` pair returned by the enqueue path |
| `xla::TpuTrackedDeviceEventPromise` | the device half of the pair; `SetReady`/`Set`/`SetError` resolve it and forward to the user value |
| `xla::TpuDevice::CreateAsyncTrackingEvent` | standalone per-device tracking-event factory; the PJRT C-API counterpart of the in-process mint |
| `xla::TpuHostTransferManager::SetExecuteEvent` | registers the per-launch define event so host transfers gate on completion |
| `tpu::System::Execute` | the device-runtime launch; carries `wait_events` / `define_events` and registers the completion lambda |
| `tpu::TpuEventIssuer` | device sequence-point + dependency-DAG engine; `RunWhenDepsReady` fires `FulfillArgs` on completion |
| `tsl::AsyncValue` / `tsl::Future<void>` / `PJRT_Event` | the completion primitive, its user-facing handle, and the C-ABI event the done-callback registers through |

## Cross-References

- [Overview](overview.md) — the PJRT-down-to-device execute path this completion lifecycle closes
- [ExecuteAsyncOnStream](execute-async-on-stream.md) — the execution entry that begins a launch whose completion this page tracks
- [LoadProgramAndEnqueueToStream](load-program-enqueue.md) — the enqueue that produces the launch and its per-execution define event
- [Stream Semantics](stream-semantics.md) — the *legacy* SE stream model; owns `WaitFor`/`RecordEvent`/`BlockHostUntilDone` (this page is the modern async-value alternative, not a duplicate)
- [Host Callbacks](host-callbacks.md) — `DoHostCallbackWithStatus`, the host-side closures whose `absl::Status` flows back through a completion event
- [PJRT Events & Async](../pjrt/events-and-async.md) — the C-ABI `PJRT_Event` / `PjRtFuture` surface (slots 10–14) this lifecycle drives
- [PJRT Client & Device](../pjrt/client-and-device.md) — `xla::TpuClient` / `CommonPjRtClient`, which mints the linked promise pair
- [PJRT Buffer & Memory](../pjrt/buffer-and-memory.md) — the output buffers whose definition events become available when fulfilment fans out
