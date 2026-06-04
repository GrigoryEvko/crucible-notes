# PJRT Events & Async Tracking

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset. PJRT C-API version is **v0.103**. Other versions will differ.*

## Abstract

`PJRT_Event` is the C-ABI handle through which a PJRT plugin reports the completion of an asynchronous operation — a program launch, a host-to-device upload, a device-to-host copy. It is an opaque pointer the caller receives from `PJRT_LoadedExecutable_Execute`, `PJRT_Buffer_ReadyEvent`, and the transfer paths, then polls (`PJRT_Event_IsReady`), blocks on (`PJRT_Event_Await`), or — the idiomatic path — attaches a done-callback to (`PJRT_Event_OnReady`). When done, it is released with `PJRT_Event_Destroy`. The C-API occupies five contiguous vtable slots (10–14) plus two late additions, `PJRT_Event_Create`/`PJRT_Event_Set` (slots 131–132), that let a *caller* mint and resolve an event explicitly. This page owns that C-ABI surface and the object that backs it.

A `PJRT_Event` is **not** itself the completion primitive. Behind every event is an `xla::PjRtFuture<void>` — concretely a `tsl::internal::FutureBase<absl::Status, false>` wrapping a refcounted, single-assignment `tsl::AsyncValue`. The wrapper functions are thin: each unwraps the C args struct (after a version check), reaches the future at a fixed offset, and forwards to one `FutureBase` method. `IsReady` reads the async value's state byte; `Await` calls `FutureBase::Await`; `OnReady` calls the future's `AndThen`, which either runs the callback inline (value already available) or mints an `AsyncValue` waiter node and threads it onto the value's waiter list. There is no poll loop — completion is *push*: the runtime fulfils the async value, which fires its waiter list, which runs the registered done-callbacks on whatever thread does the fulfilment.

This page is the **C-ABI event wrapper** layer. The *runtime-internal* machinery that fulfils these events — the linked promise pair, the device-side `TpuTrackedDeviceEventPromise`, `tpu::System::Execute`'s define-event registration and the `TpuEventIssuer` that resolves them on device retirement — lives one layer down on [Completion Loop & AsyncTrackingEvent](../runtime/completion-loop.md). The 140-slot table that places these functions is on [API Vtable Reconstruction](api-vtable-reconstruction.md). Read those for what *produces* an event; read this for what a caller *does* with one.

For reimplementation, the contract is:

- **The `PJRT_Event` object layout** — an 0x50-byte heap struct holding `{ AsyncValue* future_av, two profiling callbacks, PromiseBase<absl::Status> promise }`, built by `PromiseMaker<void>::Make` and torn down by `PJRT_Event_Destroy`.
- **The future/async-value backing** — `tsl::internal::FutureBase<absl::Status, false>` over a `tsl::AsyncValue` whose state byte (`& 2` = concrete/available) gates readiness, with an indirect-value chain that `OnReady` must walk.
- **The five-method dispatch contract** — each entry's args-struct min/current sizes, the future offset, the `IsValid()`/`IsReady()` preconditions, and the error-wrapping return convention.
- **The OnReady dispatch** — inline-run-if-ready vs. waiter-node-enqueue; the node carries a `base::Context` so the callback runs in the caller's trace context, not the fulfiller's.

| | |
|---|---|
| **Backing future** | `xla::PjRtFuture<void>` = `tsl::internal::FutureBase<absl::Status, false>` over a `tsl::AsyncValue` |
| **Event object** | 0x50-byte heap struct `{ av@+0, profiling_cb@+8/+40, PromiseBase<absl::Status>@+72 }` |
| **`PJRT_Event_Destroy`** | slot 10 · `pjrt::PJRT_Event_Destroy` @ `0xf86f920` (args min 18 / cur 24) |
| **`PJRT_Event_IsReady`** | slot 11 · `pjrt::PJRT_Event_IsReady` @ `0xf86f9e0` (args min 18 / cur 25) |
| **`PJRT_Event_Error`** | slot 12 · `pjrt::PJRT_Event_Error` @ `0xf86fba0` (args min 16 / cur 24) |
| **`PJRT_Event_Await`** | slot 13 · `pjrt::PJRT_Event_Await` @ `0xf86fa80` (args min 16 / cur 24) |
| **`PJRT_Event_OnReady`** | slot 14 · `pjrt::PJRT_Event_OnReady` @ `0xf86fc60` (args min 18 / cur 40) |
| **`PJRT_Event_Create`** | slot 131 · `pjrt::PJRT_Event_Create` @ `0xf86fe00` (args min 17 / cur 24) |
| **`PJRT_Event_Set`** | slot 132 · `pjrt::PJRT_Event_Set` @ `0xf86ffa0` (args min 14 / cur 48) |
| **Readiness flag** | `AsyncValue` state qword at `av+8`, bit `& 2` = concrete (value available) |
| **Allocated flag** | status-rep / async-value low bit (`& 1`) or `byte[+4] & 8` = heap-owned; gates refcount drop |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

---

## 1. The Backing Object — `PjRtFuture` and the `PJRT_Event` Struct

### Purpose

Every C-ABI event method is a one-page wrapper over a single C++ object. Understanding that object first makes the five methods trivial. A `PJRT_Event*` (the value in `args->event`) points to a heap struct built by `PJRT_Event_Create` / by the runtime's execute path; the struct embeds the user-observable future (an `xla::PjRtFuture<void>`) and the promise the runtime fulfils. The future is `tsl::internal::FutureBase<absl::Status, false>` — the `absl::Status` payload carries error information, the `false` template arg marks it non-copyable, and underneath sits a `tsl::AsyncValue` (the [completion primitive](../runtime/completion-loop.md#1-the-completion-primitive--tslasyncvalue)).

### The Event Object Layout

`PJRT_Event_Create` (`0xf86fe00`) is the byte-clearest evidence of the struct. It `operator new`s **0x50 bytes** and populates them, and `PJRT_Event_Destroy` tears the same layout down field by field. Cross-referencing the two:

| Field | Offset | Type | Meaning |
|---|---:|---|---|
| `future_av` | `+0x00` | `tsl::AsyncValue*` | the async value the future observes (readiness / error live here) |
| `profiling_cb_a` | `+0x08`..`+0x20` | `std::function`-style policy pair | `ProfilingKeys()` open callback (`__policy_func` + policy ptr) |
| `profiling_cb_b` | `+0x28`..`+0x40` | `std::function`-style policy pair | `void(ProfilingKeys)` close callback |
| `promise` | `+0x48` | `tsl::internal::PromiseBase<absl::Status>` | the promise half; `obj[9]` in qword terms |

The two profiling callbacks are the TraceMe span open/close pair keyed by `tsl::FutureHelpers::ProfilingKeys` — the same profiling wrapper the runtime applies in `TpuClient::CreateProfiledFuture` ([completion loop §5](../runtime/completion-loop.md#5-the-done-callback-fan-out-and-buffer-release)). On the C-API mint path (`PJRT_Event_Create`) both default to the empty policy (`__create_empty`), so an explicitly-created event carries no profiling spans.

### How the Object Is Minted

```c
// pjrt::PJRT_Event_Create(PJRT_Event_Create_Args*)                    0xf86fe00
function PJRT_Event_Create(args):
    if !ActualStructSizeIsGreaterOrEqual("PJRT_Event_Create", 17, 24, args->struct_size):
        return wrap_error(...)                       // older/newer header mismatch
    // PromiseMaker<void>::Make -> { promise, async_value }   (line 35)
    PromiseMaker<void>::Make(&promise, empty_profiling_open, empty_profiling_close)
    obj = operator new(0x50)
    obj[0]  = async_value                            // +0x00 : the AsyncValue, refcount transferred in
    obj[1..8] = profiling callback pair (empty)      // +0x08..+0x40
    obj[9]  = promise                                // +0x48 : PromiseBase<absl::Status>
    args->event = obj                                // hand the PJRT_Event back
    drop_ref(async_value)                            // §discipline: & 8 allocated bit guards Destroy
    return ok
```

`PromiseMaker<void>::Make` is the same primitive the runtime's [`CreateLinkedUserPromise`](../runtime/completion-loop.md#2-creating-the-per-execution-event--the-linked-promise-pair) uses (line 41 there). The difference is *who keeps the promise*: in `PJRT_Event_Create` the caller keeps it (boxed in the same event object, resolved later via `PJRT_Event_Set`); on the execute path the runtime keeps the device-side half and links it to the user value.

> **NOTE —** `PromiseMaker<void>` produces a `void`-payload promise/future, but the C-API surface (`Await`, `Error`, `Set`) all type the status channel as `absl::Status`. The future is `FutureBase<absl::Status, false>`: the `void` is the *success* payload (nothing to return), `absl::Status` is the *error* channel. A ready event with no error resolves to OK status; an errored event resolves to a non-OK `StatusRep`.

### The Readiness / Allocated Bit Discipline

Two flag bits recur across all five methods, and a reimplementation that mis-handles either corrupts the model:

```c
// readiness test, observed identically in IsReady / Error / OnReady
is_ready = (async_value->state_byte[+8] & 2) != 0     // bit 1 of the qword at av+8 = "concrete"

// refcount drop, observed in Destroy / Create / Await / OnReady
function drop_ref(av):
    if av == nullptr: return
    if (av->byte[+4] & 8) == 0: return                // not heap-allocated -> never destroy (singletons)
    if av->refcount[+0] == 1 || AtomicDecrement(&av->refcount) == 0:
        AsyncValue::Destroy(av)
```

`& 2` on the qword at `av+8` is the "value is concrete / available" flag. `& 8` on the byte at `av+4` is the "heap-allocated" flag; a statically-owned ready/error singleton has it clear and is never freed. The status-rep path uses a parallel `& 1` low-tag on the `StatusRep*` to distinguish an inline/OK status from a heap `StatusRep` that needs `Unref`. Both disciplines are described in full on the [completion loop page §1](../runtime/completion-loop.md#1-the-completion-primitive--tslasyncvalue); they are reproduced here because the event wrappers exercise them directly.

---

## 2. `PJRT_Event_IsReady` — Non-Blocking State Read

### Purpose

The cheapest query: is the backing async value available? Returns a bool in `args->is_ready` without blocking. JAX/PyTorch-XLA poll this in defensive checks and tight readiness loops (ranked #2 hot-path slot among the 140).

### Algorithm

```c
// pjrt::PJRT_Event_IsReady(PJRT_Event_IsReady_Args*)                  0xf86f9e0
function PJRT_Event_IsReady(args):
    if !ActualStructSizeIsGreaterOrEqual("PJRT_Event_IsReady", 18, 25, args->struct_size):
        return wrap_error(...)
    event = args->event                              // args+16
    future_av = *event                               // event[0] = AsyncValue*
    if future_av == nullptr:
        FATAL("IsValid()", future.h:281)             // event must hold a valid future
    args->is_ready = (future_av->state[+8] & 2) != 0 // args+24 : non-blocking concrete check
    return ok
```

The only logic is the `& 2` state read. The `IsValid()` CHECK (`future.h:281`) traps the misuse of an event whose future was moved-out or never set — a fatal, not a status. A reimplementation must keep that as a hard invariant: a `PJRT_Event` with a null backing future is a programming error, not a runtime condition.

> **GOTCHA —** `IsReady` returning true means the *async value is available* — i.e. the launch retired or the transfer's define event resolved. It does **not** mean device→host output data has landed in host memory; that requires a separate copy event. See the [completion loop §5 GOTCHA](../runtime/completion-loop.md#buffer-release). Treating an execute event's "ready" as "outputs are host-readable" reads device handles as host pointers.

---

## 3. `PJRT_Event_Await` and `PJRT_Event_Error` — Blocking and Status Read

### Purpose

`Await` is the *only* blocking path in the surface: it parks the calling thread until the future resolves, then returns its error status (null `PJRT_Error*` on success). `Error` reads the resolved error *without* blocking, and asserts the event is already ready. They share the same `FutureBase::Await` backing and the same error-wrapping return convention; they differ only in whether they wait.

### `PJRT_Event_Await`

```c
// pjrt::PJRT_Event_Await(PJRT_Event_Await_Args*)                      0xf86fa80
function PJRT_Event_Await(args):
    if !ActualStructSizeIsGreaterOrEqual("PJRT_Event_Await", 16, 24, args->struct_size):
        return wrap_error(...)
    status = FutureBase<absl::Status,false>::Await(args->event)   // a1[2] ; BLOCKS until available
    if (status & 1) == 0:                            // heap StatusRep, not inline-OK
        AtomicIncrement(status)                      // take a ref before returning it
    if status == OK:                                 // low-tag == &dword_0+1 sentinel
        return nullptr                               // success: no PJRT_Error
    return wrap_error(status)                        // box the non-OK status as a PJRT_Error*
```

`FutureBase<absl::Status, false>::Await` is the genuine blocking primitive — it registers an internal waiter and parks the thread until the async value fires, then returns the resolved `absl::Status`. The `& 1` low-tag distinguishes the inline OK sentinel (`&dword_0 + 1`, the canonical "OK" representation) from a heap `StatusRep*` that must be ref-bumped before it escapes through the C-ABI.

### `PJRT_Event_Error`

```c
// pjrt::PJRT_Event_Error(PJRT_Event_Error_Args*)                      0xf86fba0
function PJRT_Event_Error(args):
    if !ActualStructSizeIsGreaterOrEqual("PJRT_Event_Error", 16, 24, args->struct_size):
        return wrap_error(...)
    event = args->event                              // a1[2]
    if *event == nullptr:
        FATAL("IsValid()", future.h:281)
    if (event->future_av->state[+8] & 2) == 0:       // <-- PRECONDITION: must be ready
        FATAL("event->future.IsReady()", pjrt_c_api_wrapper_impl.cc:3032)
    status = FutureBase<absl::Status,false>::Await(event)  // already ready -> returns immediately
    ... same OK/heap-StatusRep return convention as Await ...
```

> **GOTCHA —** `PJRT_Event_Error` is **not** a blocking call and must not be used as one. It CHECK-fails (fatal, `pjrt_c_api_wrapper_impl.cc:3032`) if the event is not already ready. The correct sequence is `IsReady()` (or `OnReady`/`Await`) **then** `Error()`. Although it calls the same `FutureBase::Await` internally, the readiness precondition means that call returns without parking. A reimplementation that lets a caller read the error before the event resolves crashes the process, matching upstream PJRT semantics exactly.

The shared return convention is the C-ABI error idiom for the whole plugin: a method returns `PJRT_Error*` — `nullptr` for success, a boxed `absl::Status` otherwise. The boxing is a bare `operator new(8)` holding the `StatusRep*`; the caller frees it with `PJRT_Error_Destroy` (slot 5). The status's own refcount is managed by the `& 1`/`Unref` discipline so the rep outlives the future that produced it.

---

## 4. `PJRT_Event_OnReady` — The Push Done-Callback

### Purpose

The idiomatic completion path and the heart of this page. The caller passes a C function pointer (`callback`) and an opaque `user_arg`; libtpu arranges for `callback(error, user_arg)` to run exactly once when the future resolves. If the future is *already* available the callback runs inline on the calling thread; otherwise libtpu mints a waiter node and threads it onto the async value's waiter list, and the callback runs on whatever thread *fulfils* the value (the device-completion thread, the transfer thread, or the thread calling `PJRT_Event_Set`).

### Args Layout

`PJRT_Event_OnReady_Args` (min 18 / current 40 bytes):

| Field | Offset | Type | Meaning |
|---|---:|---|---|
| `struct_size` | `+0` | `size_t` | version gate |
| `event` | `+16` | `PJRT_Event*` | the event whose completion to observe |
| `callback` | `+24` | `void(*)(PJRT_Error*, void*)` | the done-callback |
| `user_arg` | `+32` | `void*` | opaque, passed back as `callback`'s 2nd arg |

### Algorithm

```c
// pjrt::PJRT_Event_OnReady(PJRT_Event_OnReady_Args*)                  0xf86fc60
function PJRT_Event_OnReady(args):
    if !ActualStructSizeIsGreaterOrEqual("PJRT_Event_OnReady", 18, 40, args->struct_size):
        return wrap_error(...)
    av       = *(args->event)                        // event[0]
    if av == nullptr: FATAL("IsValid()", future.h:401)
    callback = args->callback                        // args+24
    user_arg = args->user_arg                        // args+32
    state    = av->qword[+8]

    if (state & 2) != 0:                             // ALREADY AVAILABLE -> run inline
        while (av->byte[+4] & 3) != 0:               // walk the indirect-value chain
            av = av->qword[+16]                       //   IndirectAsyncValue -> concrete target
        status = av->qword[+64]                       // the resolved StatusRep* (or OK sentinel)
        if (status & 1) != 0:                         // low-tag set: inline/OK-style status (no refcount)
            if status == OK:                          //   &dword_0 + 1 -> success
                callback(nullptr, user_arg)
            else:
                callback(box_error(status), user_arg) // <-- inline, on THIS thread
        else:                                         // low-tag clear: heap StatusRep -> manage refcount
            AtomicIncrement(status)                   // bump for the boxed PJRT_Error*
            err = box_error(status)
            AtomicIncrement(status)                   // bump for the callback's owned ref
            callback(err, user_arg)                   // <-- inline, on THIS thread
            StatusRep::Unref(status)                  // drop OnReady's own ref
        return ok

    else:                                            // NOT YET -> enqueue a waiter node
        node = operator new(0x80)                     // 128-byte AsyncValue waiter Node
        node.vtable = &TraceContext_node_vtable        // off_2177E068
        base::Context::Context(node + 16)              // capture caller's trace context
        node[13] = av                                  // the value to read when fired
        node[14] = callback
        node[15] = user_arg
        tsl::AsyncValue::EnqueueWaiterListNode(av, node, state)   // CAS onto waiter list
        return ok
```

Three reimplementation-critical mechanisms in this one function.

**(1) The indirect-value walk.** When the value is available, `av` may be an `IndirectAsyncValue` placeholder that was `ForwardTo`'d to a concrete value (the [completion loop's `SetReady`](../runtime/completion-loop.md#the-tputrackeddeviceeventpromise--the-device-half) splices the concrete `TpuEvent` into exactly such an indirect). The `while (av->byte[+4] & 3) != 0: av = av->qword[+16]` loop chases the indirection chain to the concrete value before reading the status at `+64`. A reimplementation that reads the status off the indirect placeholder reads garbage.

**(2) Inline vs. enqueued dispatch.** If the value is already concrete, the callback runs *synchronously on the calling thread* — `OnReady` does not defer to a thread pool. Only the not-yet case allocates a waiter node. A caller must not assume `OnReady` returns before the callback runs.

**(3) The trace context.** The 0x80-byte node embeds a `base::Context` (a `TraceContext`) captured at registration time. When the node fires on the fulfiller's thread, it restores the caller's context so profiling/tracing attributes the callback to the originating request, not to the device-completion thread. This is the same `EnqueueWaiter<...>::Node` / `RunWaiterAndDeleteWaiterNode` machinery the functions table shows instantiated per closure type (e.g. the collectives `FutureBase::AndThen` instantiations under `tsl::AsyncValue::EnqueueWaiter`).

### Relationship to `FutureBase::AndThen`

The C-ABI `OnReady` is a hand-inlined `xla::PjRtFuture<void>::OnReady`, which upstream is `FutureBase<absl::Status>::AndThen(callback)`. The symbol table confirms the linkage: the `tsl::AsyncValue::EnqueueWaiter<...FutureBase<absl::Status,false>::AndThen<...PJRT_Event_OnReady::$_0>...>::Node` type — the waiter node minted in the enqueued path — emits its two out-of-line virtual methods at `0xf87a580` (`~Node`, the deleting destructor) and `0xf87a5c0` (`RunWaiterAndDeleteWaiterNode`, the fire-and-free entry the value's waiter list calls), with its vtable at `0x2177e058` (the stored vptr is `off_2177E068`, +0x10 past the offset-to-top / typeinfo header). The decompile inlines the available-case fast path rather than always calling `AndThen`; the enqueued case allocates exactly this node type.

> **QUIRK —** the callback receives a `PJRT_Error*`, not a bool. On success it is `nullptr`; on error it is a freshly-boxed `StatusRep` the callback **owns and must destroy** with `PJRT_Error_Destroy`. The inline path bumps the status refcount twice and `Unref`s once around the call, leaving exactly one ref for the callback to release — a reimplementation that forgets to box (or that hands the same `StatusRep*` to multiple callbacks without ref-bumping) double-frees the status.

---

## 5. `PJRT_Event_Set` and `PJRT_Event_Destroy` — Caller-Side Resolve and Teardown

### Purpose

`PJRT_Event_Create`/`PJRT_Event_Set` (slots 131/132) are the late-added pair that let a *caller* mint an event and resolve it explicitly — the framework-driven counterpart to the runtime-driven [`TpuTrackedDeviceEventPromise::SetReady`](../runtime/completion-loop.md#the-tputrackeddeviceeventpromise--the-device-half). A caller creates an event (§1), hands it to some asynchronous producer, and later calls `Set` with a status to fulfil it. `Destroy` releases any event regardless of how it was minted.

### `PJRT_Event_Set`

```c
// pjrt::PJRT_Event_Set(PJRT_Event_Set_Args*)                          0xf86ffa0
function PJRT_Event_Set(args):
    if !ActualStructSizeIsGreaterOrEqual("PJRT_Event_Set", 14, 48, args->struct_size):
        return wrap_error(...)
    code   = PjrtErrorCodeToStatusCode(args->error_code)   // args+24 : PJRT_Error_Code -> absl code
    msg    = args->error_message                            // args+32 (ptr)
    msglen = args->error_message_size                       // args+40 (>= 0, else BUG())
    rep    = absl::Status::MakeRep(4*code+1, msg, msglen, 3067, "...pjrt_c_api_wrapper_impl.cc")
    promise = event + 72                                    // obj[9] = PromiseBase<absl::Status>
    PromiseBase<absl::Status>::emplace<absl::Status>(promise, &rep)   // <-- FULFILS the future
    if (rep & 1) == 0: StatusRep::Unref(rep)
    return ok
```

`emplace` is the resolution edge: it assigns the status into the promise's async value, transitioning it to concrete (`& 2` set) and firing its waiter list — which runs every `OnReady` callback and unblocks every `Await`. `PjrtErrorCodeToStatusCode` maps the C-API error enum to an `absl::StatusCode`; the `4*code+1` encoding is absl's tagged-status representation. Passing the OK code yields a success resolution. This is the *exact same fulfilment* the runtime performs internally on the device path — the difference is only the trigger (a C-API call vs. device retirement).

> **NOTE —** `PJRT_Event_Set`'s args min size is **14**, the smallest of any event method, because the original event-resolution args predate the message-size field. The `error_message`/`error_message_size` fields (`+32`/`+40`) push the *current* size to 48; a caller compiled against the older header that passes only `{struct_size, event, error_code}` (size ~24) still resolves the event, just with no message. The `args->error_message_size < 0` guard (`BUG()`) defends against an uninitialised tail when the caller's struct is larger than they populated.

### `PJRT_Event_Destroy`

```c
// pjrt::PJRT_Event_Destroy(PJRT_Event_Destroy_Args*)                  0xf86f920
function PJRT_Event_Destroy(args):
    if !ActualStructSizeIsGreaterOrEqual("PJRT_Event_Destroy", 18, 24, args->struct_size):
        return wrap_error(...)
    obj = args->event                                // a1[2]
    if obj == nullptr: return ok
    PromiseBase<absl::Status>::~PromiseBase(obj + 9)  // +0x48 : destroy the promise half
    destroy_policy(obj[8])(obj[5])                    // run profiling_cb_b destructor if present
    destroy_policy(obj[4])(obj[1])                    // run profiling_cb_a destructor if present
    drop_ref(obj[0])                                  // §1 discipline on the AsyncValue (& 8 gate)
    free(obj)                                         // the 0x50-byte struct itself
    return ok
```

`Destroy` is the mirror of `Create`: destruct the promise (`obj+9` = `+0x48`), run the two profiling-callback policy destructors (the `std::function`-style `{ptr, vtable}` pairs at `obj[1]/obj[4]` and `obj[5]/obj[8]`), drop the async value's refcount under the `& 8` allocated-bit guard, then `free` the heap struct. Destroying an event does **not** cancel its operation — it only releases the handle; any pending `OnReady` waiter the runtime holds still fires (it carries its own ref). A reimplementation that destroys the async value while waiters are queued breaks the runtime; the `& 8` + refcount discipline is what prevents that.

---

## 6. Where Events Come From — The Producers

### Purpose

A caller never constructs the runtime's events with `PJRT_Event_Create`; that is the explicit-resolution path. The events that matter in a training/inference loop are *returned* by other C-API calls, each handing back a `PJRT_Event*` (or boxing a future into one). This section maps the producers so a reimplementer knows which slots mint events and what each event's "ready" means.

### Producer Map

| Producer slot | Function | Event semantics |
|---|---|---|
| `PJRT_LoadedExecutable_Execute` (60) | `pjrt::PJRT_LoadedExecutable_Execute` @ `0xf869b40` | per-launch completion; ready = program retired on device |
| `PJRT_Buffer_ReadyEvent` (77) | `pjrt::PJRT_Buffer_ReadyEvent` @ `0xf86ed20` | buffer definition event; ready = backing HBM is valid |
| `PJRT_Buffer_ToHostBuffer` (75) | `pjrt::PJRT_Buffer_ToHostBuffer` @ `0xf86e640` | D2H copy completion; ready = host bytes landed |
| `PJRT_Buffer_CopyRawToHostFuture` (125) | `pjrt::PJRT_Buffer_CopyRawToHostFuture` @ `0xf86dfe0` | raw D2H copy; ready = host bytes landed |
| `PJRT_Client_BufferFromHostBuffer` (27) | `pjrt::PJRT_Client_BufferFromHostBuffer` @ `0xf8644c0` | H2D upload done-event |
| transfer-manager slots (106–114, 124) | `PJRT_AsyncHostToDeviceTransferManager_*` | per-chunk / per-buffer transfer events |

The execute event is minted by [`CreateLinkedUserPromise`](../runtime/completion-loop.md#2-creating-the-per-execution-event--the-linked-promise-pair) and fulfilled by [`tpu::System::Execute`'s completion lambda](../runtime/completion-loop.md#tpusystemexecute--carrying-define_events) via the `TpuEventIssuer`. The buffer/transfer events ride the same `tsl::AsyncValue` primitive; their fulfilment is a transfer-completion lambda rather than a device-retirement one. In every case the C-API event the caller receives is the *same* 0x50-byte wrapper this page documents — the producer differs, the surface does not.

> **QUIRK —** the execute event and a buffer's `ReadyEvent` are distinct events backed by distinct (but linked) async values. The execute event resolving "ready" makes the output buffers' definition events available, which is what eventually flips `PJRT_Buffer_ReadyEvent`'s future. A reimplementation that conflates "execute done" with "every output buffer ready" misses the case where a buffer is donated or aliased and its readiness is gated on a different define event. See [Buffer and Memory](buffer-and-memory.md) for the buffer-side lifecycle.

---

## 7. Considerations for a Reimplementer

- **Events are wrappers, not primitives.** Implement `PjRtFuture<void>` / `AsyncValue` first ([completion loop](../runtime/completion-loop.md)); the five C-ABI methods are then 10–20 lines each. Do not put completion logic in the C wrappers.
- **`OnReady` is the path; `Await` is the fallback.** Register an `OnReady` waiter and let the runtime push completion. Reserve `Await` for the rare synchronous caller — it is the only method that parks a thread.
- **Honor the args version gate.** Every method's first action is `ActualStructSizeIsGreaterOrEqual("<name>", min, current, args->struct_size)`. The (min, current) pairs are byte-confirmed above; a reimplementation must accept any caller struct ≥ min and read only up to the caller's declared size. Fields beyond `struct_size` are never touched.
- **The readiness CHECKs are fatal, by design.** `IsValid()` (null backing future) and `IsReady()` (calling `Error` before resolution) are hard `LogMessageFatal`s, not status returns. Replicate them — they trap caller bugs early, and upstream PJRT semantics depend on the abort.
- **Walk the indirect chain in `OnReady`.** The available value may be an `IndirectAsyncValue` forwarded to a concrete one. Chase `av+16` while `(av+4 & 3) != 0` before reading the status at `av+64`, or you read the placeholder.
- **The error return is owned by the caller.** A non-OK method returns a boxed `PJRT_Error*` the caller must `PJRT_Event_Destroy`/`PJRT_Error_Destroy`. Manage the underlying `StatusRep` refcount with the `& 1` low-tag + `Unref` discipline so it outlives the future.
- **`Destroy` releases the handle, not the operation.** Destroying an event does not cancel the launch or transfer; pending runtime waiters keep their own refs and still fire. Gate the async-value free on the `& 8` allocated bit so you never free a value with live waiters or a process-shared singleton.
- **Ready ≠ host-readable.** An execute or buffer-definition event resolving means *device-side* validity. Host readability of output data needs a separate D2H copy event. Do not collapse the two.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::PjRtFuture<void>` / `tsl::internal::FutureBase<absl::Status,false>` | the C++ future every `PJRT_Event` wraps; supplies `Await`/`AndThen`/`IsReady` |
| `tsl::AsyncValue` | the refcounted single-assignment cell under the future; its state byte gates readiness |
| `tsl::internal::PromiseMaker<void>` / `PromiseBase<absl::Status>` | mint and fulfil the promise half embedded in the event object |
| `pjrt::ActualStructSizeIsGreaterOrEqual` | the per-method args-version gate every event entry calls first |
| `pjrt::PjrtErrorCodeToStatusCode` / `absl::Status::MakeRep` | error-code mapping and status-rep construction used by `Set` |
| `xla::CommonPjRtClient::CreateLinkedUserPromise` | the runtime mint that produces the execute event (one layer down) |

## Cross-References

- [Completion Loop & AsyncTrackingEvent](../runtime/completion-loop.md) — the runtime-internal promise/device-event machinery that *fulfils* these events; this page is the C-ABI wrapper above it
- [API Vtable Reconstruction](api-vtable-reconstruction.md) — the 140-slot table that places `PJRT_Event_*` at slots 10–14 and 131–132
- [Executable Execution](executable-execution.md) — `PJRT_LoadedExecutable_Execute`, the primary producer of execute completion events
- [Buffer and Memory](buffer-and-memory.md) — `PJRT_Buffer_ReadyEvent` and the buffer-definition events whose readiness these events gate
- [Host Callbacks](callbacks.md) — host-side closures whose `absl::Status` flows back through a completion event
- [Overview](overview.md) — the PJRT-down-to-device path these events sit at the top of
