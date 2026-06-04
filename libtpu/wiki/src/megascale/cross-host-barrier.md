# Cross-Host Barrier

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, x86-64, 781,691,048 bytes). Other versions will differ. Confirmed (byte-anchored): the `BarrierCoordinator` object layout, the vtable, the arrival-accounting set, the `IsComplete` test, the client replay/auto-id paths, and every log string below were re-derived from the IDA decompile + demangled `functions.json`.*

## Abstract

The **Megascale cross-host barrier** is the DCN-level (data-centre-network, inter-host) rendezvous by which every host of a multi-slice XLA/TPU job synchronises at a named point. It is a single unary gRPC call — `MegaScaleTransport.Barrier` — fanned in to one coordinator process, which counts arrivals and releases all callers at once. This is the **host-network** barrier; it is a different mechanism, at a different fabric scope, from the on-chip SFLAG barriers ([Barriers Overview](../barrier/overview.md)) and the cross-core ICI [tree-barrier](../barrier/tree-barrier-vsync.md). Those run *inside* a chip / slice over the TensorCore sequencer; this one runs *between* hosts over gRPC/protobuf. Nothing on this page touches an SFLAG — the wait primitive here is an `absl::Notification` plus a vector of gRPC reply callbacks.

The shape is a **star, centralised, one round-trip**: every host sends a `Barrier` RPC to the one coordinator endpoint, the coordinator accumulates per-`(slice_id, host_id)` arrivals into a `flat_hash_set` until the set size equals the declared participant count, then broadcasts a single cached `BarrierResponse` to every queued caller. There is no peer-to-peer phase, no tree reduction, no butterfly, no dissemination — the topology is identical in shape to the topology-discovery rendezvous it shares a base class with. The server object, `xla::megascale::runtime::BarrierCoordinator` (vptr `0x21c9bb70`, `0xf8` = 248 bytes), is a second specialisation of the generic `Coordinator<Req, Resp, CB>` template that also produces `TopologyCoordinator`; it inherits the state machine, the pending-callback fan, the `Notification`, and the periodic status alarm from that base ([Bootstrap Convergence](bootstrap/convergence.md)).

For reimplementation, the contract is:

- **The `BarrierCoordinator` object** — its `0xf8` layout, the shared-`Coordinator<>` base offsets, the `(slice_id, host_id)` tuple set, and the 9-slot vtable.
- **The `Barrier` RPC wire format** — the 4-field `BarrierRequest`, the id-only `BarrierResponse`, and the server callback chain `GrpcAsyncCbServiceImpl::Barrier → OnBarrierRequestReceived → AddRequest`.
- **The centralised algorithm** — lazy per-`barrier_id` coordinator allocation, arrival accounting, count/duplicate validation, early-fire on `seen.size() == num_participants_`, and the callback fan-out release.
- **`barrier_id` keying and the two flavours** — the `flat_hash_map` keyed by id, the named pre-execution barrier with replay protection, and the auto-minted `__global-auto-<N>` collective barrier.
- **Timeout and failure** — there is **no** coordinator-side timeout; the only bound is the per-RPC gRPC deadline from `FLAGS_tf_tpu_preexecution_barrier_timeout` (default 30 s), with a 10-second client retry loop and an "Unable to wait for all slices to connect" destructor log.

| | |
|---|---|
| **Service / method** | `/xla.megascale.runtime.MegaScaleTransport/Barrier` (unary; 3rd of 6 methods) |
| **Coordinator object** | `xla::megascale::runtime::BarrierCoordinator`, vptr `0x21c9bb70`, `0xf8` = 248 bytes |
| **Base class** | `xla::megascale::runtime::Coordinator<BarrierRequest, BarrierResponse, AnyInvocable<…>>` (base vtable `off_21C33918`) |
| **Coordinator map** | `flat_hash_map<string, unique_ptr<BarrierCoordinator>>` @ `CommunicationBackend +0x1b0` |
| **Arrival store** | `flat_hash_set<tuple<int,int>>` (`slice_id`, `host_id`) @ `BarrierCoordinator +0xd8` |
| **Server entry** | `GrpcAsyncCbServiceImpl::Barrier` `0x1ce74280` → `OnBarrierRequestReceived` `0x1ccac5c0` → `AddRequest` `0x1ccb42a0` |
| **Client entry** | `Communicator::Barrier(id, opt<int>, opt<Duration>)` `0x1cca8ee0` → `GetBarrierResponse` `0x1ce79cc0` → `SendRPC` `0x1ce79de0` |
| **Timeout flag** | `FLAGS_tf_tpu_preexecution_barrier_timeout` `0x22256500`, `absl::Duration` default **30 s** |
| **Source TU** | `platforms/xla/megascale/runtime/communication/{topology_coordinator,communication_backend,grpc_transport}.{h,cc}` |

> **NOTE —** the embedded source strings name `topology_coordinator.h/.cc` for the coordinator logic even on the barrier path, because `BarrierCoordinator` and `TopologyCoordinator` share one template. Line numbers like `topology_coordinator.cc:323` cited below are barrier code that lives in that shared TU, not topology code.

---

## 1. The Barrier RPC

### Purpose

`MegaScaleTransport.Barrier` is the single wire operation of the cross-host barrier. It is a blocking unary gRPC call: a host issues it, the coordinator holds the reply open (does not call its finish callback) until the barrier completes, then finishes it. From the caller's view the RPC simply blocks until the rendezvous releases.

### Wire Format

The request is built and parsed via `BarrierRequest::Clear` `0x1cf7f220` (has-bits at C++ `+0x10`); the response via `BarrierResponse::Clear` `0x1cf7f760` / `MergeImpl` `0x1cf7f660`.

| Field # | Name | Type | C++ offset | Meaning |
|--------:|------|------|------------|---------|
| 1 | `barrier_id` | string | `+0x18` | rendezvous key; `ArenaStringPtr` (low-bit tagged, mask `& ~3`) |
| 2 | `slice_id` | int32 | `+0x20` | low half of the `(slice, host)` arrival tuple |
| 3 | `host_id` | int32 | `+0x24` | high half of the arrival tuple |
| 4 | `num_participants` | int32 | `+0x28` | expected arrival count, validated against the coordinator's stored count |

The response carries only `barrier_id` (field 1, `+0x18`), echoed by `CreateResponse`. The C++ object has int fields at `+0x20…` (has-bit at `+0x10`) mirroring the request shape, but the coordinator never writes them — so their wire field numbers are inferred from request symmetry, not observed being set (LOW for fields 2-4 of the response).

> **CORRECTION (P-3-70) —** an earlier reading guessed `BarrierRequest` field 4 was `barrier_timeout_in_ms`. It is **not**. `ProcessRequest` reads `*((int*)req + 10)` (proto field at `+0x28`) and compares it against the stored `num_participants_`; a mismatch is rejected with `INVALID_ARGUMENT`. The barrier timeout is carried entirely out-of-band as the gRPC client deadline — there is no timeout proto field (see [§5](#5-timeout)).

### Server Callback Chain

```text
GrpcAsyncCbServiceImpl::Barrier(ctx, req, resp)            0x1ce74280
  │  VLog(3) "calling barrier_callback_ with request: <req>"  (grpc_transport.cc:1325)
  │  wrap the gRPC reactor-finish closure as
  │    AnyInvocable<void(StatusOr<BarrierResponse> const&)>   (writes resp, Finish()es)
  └─ barrier_callback_(req, cb)   [transport +80/+104]
       └─ CommunicationBackend::OnBarrierRequestReceived     0x1ccac5c0
            └─ BarrierCoordinator::AddRequest(req, cb)        0x1ccb42a0  [vtable +0x10]
```

The server side uses `WithCallbackMethod_Barrier` in the `MegaScaleTransport` callback-service chain; the client side dispatches `MegaScaleTransport::Stub::Barrier` `0x1ce9a7c0` through a pooled channel.

---

## 2. The BarrierCoordinator Object

### Purpose

One `BarrierCoordinator` exists per live `barrier_id` on the coordinator process. It accumulates arrivals and, on the last expected arrival, releases all waiting callers. It is the topology coordinator's sibling — the same `Coordinator<>` base with a `(slice, host)`-tuple counter instead of a per-slice host map.

### Object Layout (`0xf8` = 248 bytes)

Allocated with `operator new(0xf8)` in `OnBarrierRequestReceived` (`0x1ccac5c0`). Offsets confirmed from the ctor `0x1ccb3fa0` plus member accesses in `ProcessRequest` / `IsComplete` / the dtor.

| Offset | Size | Field | Confidence |
|---|---|---|---|
| `+0x00` | 8 | vptr (`0x21c9bb70`) | CERTAIN |
| `+0x08`–`+0x57` | 0x50 | embedded `Coordinator<>` base: `TracedMutex(kind=9)` | HIGH |
| `+0x58` | 1 | `state_` (0 init / 1 ready / 2 completed / 3 error) | CERTAIN |
| `+0x60`–`+0x87` | 0x28 | `StatusOr<BarrierResponse>` (init = `UNAVAILABLE` "IN_PROGRESS") | CERTAIN |
| `+0x88` | 8 | `response_setters_.data` — `vector<AnyInvocable<void(StatusOr<BarrierResponse> const&)>>` | CERTAIN |
| `+0x90` / `+0x98` | 8 / 8 | `response_setters_.size` / `.capacity` | HIGH |
| `+0xa0`–`+0xbf` | 0x20 | `absl::Notification` | CERTAIN |
| `+0xb0` | 8 | status-report cancellable alarm handle | HIGH |
| `+0xb8`–`+0xcf` | 0x18 | `std::string barrier_id` (SSO; cap byte at `+0xcf`) | CERTAIN |
| `+0xd0` | 4 | `num_participants_` (CHECK `> 0` at `topology_coordinator.h:299`) | CERTAIN |
| `+0xd8`–`+0xf7` | 0x20 | `flat_hash_set<tuple<int,int>> seen_workers` (size = `*(this+0xe0) >> 17`) | CERTAIN |

The base-class offsets (`+0x58` `state_`, `+0x60` `StatusOr`, `+0x88` callbacks, `+0xa0` `Notification`, `+0xb0` alarm) are shared verbatim with `TopologyCoordinator`; only the derived tail (`+0xb8` onward) differs.

### Algorithm — Construction

```c
// BarrierCoordinator::BarrierCoordinator(string_view id, int num_participants)   0x1ccb3fa0
function ctor(this, id, num_participants):
    this.vptr        = &off_21C33918                       // base vtable first
    TracedMutex_ctor(this + 0x08, /*kind*/ 9)              // embedded mutex
    this.state_      = 0                                   // +0x58
    this.StatusOr_   = MakeErrorImpl<14>("Coordinator in IN_PROGRESS")  // +0x60, cat 14 = UNAVAILABLE
    zero(this + 0x88, 0x20)                                // callbacks vector + Notification head
    this.alarm_      = 0                                   // +0xb0 (within zeroed range)
    this.vptr        = &off_21C9BB70                       // overwrite with derived vtable
    store_sso_string(this + 0xb8, id)                      // barrier_id (heap if len > 0x16)
    this.num_participants_ = num_participants               // +0xd0
    this.seen_workers_ = empty flat_hash_set               // +0xd8 (+0xd8 = 1, growth-info word @ +0xe0 = 0)
    CHECK(num_participants > 0) @ topology_coordinator.h:299  // FATAL otherwise
```

The two vtable writes (base then derived) are the standard C++ vtable transition during construction; the decompile shows `off_21C33918` written first, then `off_21C9BB70` after the base members are initialised.

### Vtable (vptr `0x21c9bb70`, 9 slots)

Resolved from `.data.rel.ro` `R_X86_64_RELATIVE` relocations.

| Slot | VA | Method | Confidence |
|---|---|---|---|
| `+0x00` | `0x1cf55760` | `~BarrierCoordinator` (D2/D1) | CERTAIN |
| `+0x08` | `0x1cf55960` | `~BarrierCoordinator` (D0, deleting) | CERTAIN |
| `+0x10` | `0x1ccb42a0` | `Coordinator<Barrier>::AddRequest(req, cb)` (base) | CERTAIN |
| `+0x18` | `0x1ccb4a80` | `Coordinator<Barrier>::GetState() const` (base) | CERTAIN |
| `+0x20` | `0x1cf54e60` | `ProcessRequest(req)` — arrival accounting | CERTAIN |
| `+0x28` | `0x1cf559a0` | `IsComplete() const` | CERTAIN |
| `+0x30` | `0x213b7e20` | `CreateResponse()` | CERTAIN |
| `+0x38` | `0x213b7ce0` | `ReportStatus() const` | CERTAIN |
| `+0x40` | `0x22048600` | base accessor (GetNumWorkers-style; not on the barrier hot path) | LOW |

> **NOTE —** `AddRequest` (`+0x10`) and `GetState` (`+0x18`) are *base*-class entries shared with `TopologyCoordinator`; `ProcessRequest`/`IsComplete`/`CreateResponse`/`ReportStatus` (`+0x20`…`+0x38`) are the *derived* overrides that give the base its barrier-specific behaviour. The base calls them through the vtable — this is the template-method pattern.

---

## 3. barrier_id Keying

### Purpose

Each `barrier_id` is an independent rendezvous. The coordinator process owns one map of live barriers, lazily creating a `BarrierCoordinator` the first time it sees an id and routing every later arrival for that id to the same object.

### Algorithm — Lazy Insert

```c
// CommunicationBackend::OnBarrierRequestReceived(req, reply_cb)   0x1ccac5c0
function OnBarrierRequestReceived(backend, req, reply_cb):
    VLog(3) "Received barrier request from (SliceId, HostId, barrier_id) <s>,<h>,<id>"  // :954
    lock(backend.mutex_)                                  // +0xe0
    slot = backend.barrier_map_[req.barrier_id]           // flat_hash_map @ +0x1b0
    if slot == null:                                      // first sight of this id
        slot = new(0xf8) BarrierCoordinator(req.barrier_id, req.num_participants)
    cb = move(reply_cb)
    slot->AddRequest(req, cb)                             // vtable +0x10
```

The participant count for the *ctor* is taken from the **first** request's field 4; every later request's field 4 is validated equal in `ProcessRequest` ([§4](#4-arrival-accounting-and-release)).

> **GOTCHA —** there is **no** coordinator-only guard here. Topology discovery rejects with "TopologyCoordinator not initialized." if a non-coordinator process is hit; the barrier path has no such check — *any* process holding the barrier map can host a `BarrierCoordinator`. A reimplementation that assumes only the elected coordinator ever allocates a `BarrierCoordinator` will diverge; the gate is purely that callers send to the coordinator's endpoint, not a server-side identity check.

> **CORRECTION (P-3-70) —** the barrier map was estimated at `+0x228` with a 192-byte coordinator object. The binary shows the `flat_hash_map<string, unique_ptr<BarrierCoordinator>>` at `CommunicationBackend +0x1b0` (`operator[](backend+432, id)`) and `operator new(0xf8)` — a 248-byte object. The `TopologyCoordinator*` is at `+0x1a0`, the `ErrorReporter*` at `+0x1a8`, and the auto-barrier counter at `+0x1d0`.

### Two Flavours, One Mechanism

Both flavours go through the same 3-arg `Communicator::Barrier` (`0x1cca8ee0`) and the same server-side `BarrierCoordinator`. Only id generation and replay semantics differ.

| Aspect | Pre-execution barrier | Collective-rendezvous barrier |
|---|---|---|
| Caller | `Communicator::Barrier(id, n, timeout)` | `Communicator::Barrier()` (no-arg) `0x1cca8de0` |
| `barrier_id` | explicit, supplied by the executor | auto `"__global-auto-<N>"` (counter @ backend `+0x1d0`) |
| Replay protection | once-per-process used-id set (`ALREADY_EXISTS`) | never collides — fresh id each call |
| `num_participants` | caller-supplied or default `NumHosts()` | default `NumHosts()` |
| Driven by | `tpu_execute.cc`, gated `FLAGS_tf_tpu_enable_preexecution_barrier` | the collective layer at the start of a cross-host collective |

The no-arg form reads-and-increments the monotonic counter at backend `+0x1d0` under the backend mutex, builds `StrCat("__global-auto-", N)`, and delegates to the 3-arg form with `num_participants`/`timeout` unset. Because every collective mints a fresh id, collectives never trip replay protection.

The named form first inserts `barrier_id` into the per-process used-id set at `Communicator +0xa8`; a re-use returns `MakeErrorImpl<6>` (`ALREADY_EXISTS`) "Barrier ID: $0 has already been used." (`communication_backend.cc:379`) before any RPC is sent.

> **QUIRK —** the default participant count is the **whole job**. When the caller omits `num_participants`, it defaults to `MultiSliceTopologyAndLocation::NumHosts()` — the total host count resolved during the bootstrap topology exchange ([Bootstrap Convergence](bootstrap/convergence.md)). An un-parameterised barrier therefore completes only when *every* host in the multi-slice job has arrived. The mechanism supports any count (e.g. sub-group barriers), but no caller passing a non-default count was located in this binary (LOW that sub-group barriers are used in practice).

---

## 4. Arrival Accounting and Release

### Purpose

The coordinator must count each distinct host exactly once, reject mis-declared counts and duplicates, fire the instant the last expected host arrives, and broadcast one response to all waiters. All four happen inside `ProcessRequest` and the base `AddRequest`.

### Algorithm — ProcessRequest (validation + counting)

```c
// BarrierCoordinator::ProcessRequest(req)   0x1cf54e60   [vtable +0x20]
function ProcessRequest(this, req):
    if req.num_participants (+0x28) != this.num_participants_ (+0xd0):
        return INVALID_ARGUMENT,
               "Mismatched number of barrier participants: Expected: $0 Msg: $1"  // :323
    key = tuple<int,int>(req.slice_id (+0x20), req.host_id (+0x24))
    if this.seen_workers_.find(key):                      // flat_hash_set @ +0xd8
        return INVALID_ARGUMENT,
               "Extra barrier participant. Expected: $0 Message $1"               // :331
    this.seen_workers_.insert(key)                        // find_or_prepare_insert_large
    return OK
```

The set is a `flat_hash_set<tuple<int,int>>`, **not** a set of `"slice:host"` strings. Each unique host counts once, so a host whose RPC is re-issued by the 10-second retry loop ([§5](#5-timeout)) cannot double-count — its second arrival hits `find` and is rejected as a duplicate, which the base path then folds into the cached response.

### Algorithm — IsComplete (early-fire)

```c
// BarrierCoordinator::IsComplete() const   0x1cf559a0   [vtable +0x28]
function IsComplete(this):
    return (*(uint64*)(this + 0xe0) >> 17) == this.num_participants_   // +0xd0
```

The `>> 17` extracts the SwissTable element count from the control-block growth-info word at `+0xe0`. Completion fires the moment `seen.size() == num_participants_` — on the arrival of the last expected host, identical to the topology coordinator's "all slots seen" early-fire.

### Algorithm — AddRequest (state machine + release)

```c
// Coordinator<Barrier>::AddRequest(req, cb)   0x1ccb42a0   [vtable +0x10, base]
function AddRequest(this, req, cb):
    VLog(5) "AddRequest: <req>"                           // topology_coordinator.h:75
    lock(this.mutex_)                                     // +0x08, TracedReleasableMutexLock
    if this.state_ == 3:                                  // poisoned (sticky error)
        cb(this.StatusOr_); return                        // +0x60
    rsp = this.vtable[+0x20](req)                         // ProcessRequest -> OK or INVALID_ARGUMENT
    if this.state_ == 2:                                  // already completed
        cb(per-request response/error); return           // serve cached, fast path
    push cb onto this.response_setters_ (+0x88)
    if rsp is error:
        this.state_ = 3; this.StatusOr_ = rsp; serve rsp  // poison the id
    if this.vtable[+0x28]():                              // IsComplete()
        this.state_ = 2
        this.StatusOr_ = this.vtable[+0x30]()             // CreateResponse()
        this.Notification_.Notify()                       // +0xa0
        VLog(5) "Num responses to send: <n>"              // h:131
        for cb_i in response_setters_: cb_i(this.StatusOr_); VLog(5) "Response sent <i>"  // h:135
        clear(response_setters_)
    else if this.state_ == 0:
        this.state_ = 1; ScheduleStatusReport()           // arm 1-second alarm @ +0xb0
    VLog(5) "Done processing Request received at: <t> Duration: <d>"  // h:141
    if duration > ~5 s: LOG "Long running topology coordinator AddRequest: Duration: <d>"  // h:144
```

### Release Mechanism

On completion (under the mutex): `state_ = 2`; `StatusOr_ := CreateResponse()` (a `BarrierResponse` with `barrier_id` echoed from `+0xb8`, logging "MegaScale Barrier completed for id <id>" at `topology_coordinator.cc:364`); `Notification.Notify()`; then iterate `response_setters_` invoking each queued `AnyInvocable<void(StatusOr<BarrierResponse> const&)>` with the cached response. Each invocation is what finishes the corresponding gRPC call and unblocks the remote host's `SendRPC`. The gRPC framework does not signal a client until its callback fires — so "release" is precisely this single fan-out pass, after which the vector is destroyed.

The `Notification` at `+0xa0` additionally wakes any **in-process** caller that bypassed gRPC (the coordinator's own host) and is used by the destructor's settle path; for remote hosts only the callback fan matters. This is the same dual signalling described in [Bootstrap Convergence](bootstrap/convergence.md).

---

## 5. Timeout

### Purpose

The barrier must bound how long a host waits for the others. The bound is entirely client-side: a per-RPC gRPC deadline plus a retry loop. The coordinator imposes no timeout of its own.

### Flags

| Flag (VA) | Type | Default | Effect | Confidence |
|---|---|---|---|---|
| `FLAGS_tf_tpu_enable_preexecution_barrier` `0x222564a0` | bool | (gate) | master switch for the pre-execution barrier in the executor | HIGH |
| `FLAGS_tf_tpu_preexecution_barrier_timeout` `0x22256500` | `absl::Duration` | **30 s** | client gRPC deadline applied to the `Barrier` RPC | CERTAIN |
| `FLAGS_xla_tpu_enable_megascale_barrier` `0x223b4d08` | bool | (gate) | higher-level enable for the megascale barrier path | HIGH |

The 30-second default is written by `AbslFlagDefaultGenFortf_tpu_preexecution_barrier_timeout::Gen` `0xe6fd5c0` as the `absl::Duration {sec=30, ns=0}` (`*this = 30; *(this+2) = 0`). Both the timeout and the enable flag are registered in `learning/45eac/tfrt/tf_tpu/tpu_execute.cc` (the TFRT TPU executor), confirming the pre-execution barrier is driven from the per-launch execute path.

### Algorithm — Client Deadline + Retry Loop

```c
// GrpcTransport::SendRPC<BarrierRequest, BarrierResponse>(...)   0x1ce79de0
function SendRPC(method, peer, req, opt_timeout):
    deadline = opt_timeout ? Now() + opt_timeout : +inf (0x7fff..ff)
    VLog(3) "Calling GetBarrierResponse with deadline: <t>"       // grpc_transport.cc:2197
    while Now() < deadline:
        ctx = ClientContext(); ctx.set_deadline(Timepoint2Timespec(deadline))
        stub = GetOrCreateClient(peer)                            // channel-pooled, round-robin
        status, resp = IssueSyncRPC(stub, req)                    // Stub::Barrier (blocking unary)  0x1ce739c0
        if status.ok():
            VLog(3) "<method> succeeded."                         // :2221
            return resp
        LOG "<method> to <peer> returned with status: <s>. Sleeping for 10 seconds."  // :2216
        AbslInternalSleepFor(10 s)                                // retry
    return last sticky error status                              // typically DEADLINE_EXCEEDED
```

`IssueSyncRPC` (`0x1ce739c0`) round-robins across a pool of pre-opened stubs (80-byte entries, `index = call_count % pool_size`, under the client mutex) and converts the gRPC status via `GrpcStatusToAbslStatus`. With a 30 s budget and a 10 s retry sleep, a caller makes roughly three attempts before its deadline expires.

> **GOTCHA —** there is **no** `WaitForNotificationWithTimeout` on the coordinator's progress path. The coordinator never aborts a barrier on its own clock; an incomplete `BarrierCoordinator` keeps its callbacks pending and re-logs progress every second ([§6](#6-failure-handling)). The *only* bound is each caller's gRPC deadline. A reimplementation that adds a server-side timeout will not match observed behaviour, where the coordinator simply waits — and logs — indefinitely until torn down.

### Status Report Cadence

When `AddRequest` moves the coordinator from state 0 → 1, it arms a periodic alarm at `+0xb0` via the `ScheduleStatusReport` lambda `0x1ccb4ea0`: under the mutex it calls `ReportStatus` (vtable `+0x38`) and, if still incomplete, re-arms at `Now() + 1 s` via `thread::AddCancellableAt` on the `DefaultFiberExecutor`.

> **CORRECTION (P-3-148) —** the cadence is a fixed **1 second**, not "once per minute" / executor-backoff. The re-arm in `ScheduleStatusReport`'s lambda is unconditionally `Now() + absl::Seconds(1)`; there is no backoff multiplier. ([Bootstrap Convergence](bootstrap/convergence.md) describes the generic alarm as "seconds to a minute" — the barrier's concrete re-arm is 1 s.)

`ReportStatus` (`0x213b7ce0`) logs "MegaScale Barrier completed." (`:353`) when done, else "MegaScale Barrier in progress. Seen <K> of <N> expected participants. Seen hosts: <list>" (`:356`). The host list comes from `GetSeenHosts` (`0x1cf55280`), which compacts the `(slice, host)` set into per-slice `gtl::IntervalSet<int>` ranges — e.g. `slice0.hosts[0-3,5], slice1.hosts[0-7]`.

---

## 6. Failure Handling

### Algorithm — Failure Modes

| Failure | Where detected | Result |
|---|---|---|
| A host never arrives | client gRPC deadline (30 s) | each waiter's RPC returns `DEADLINE_EXCEEDED`, its callback cancelled; coordinator stays `state_=1`, re-logs "in progress" every 1 s |
| Incomplete coordinator torn down | derived dtor `~BarrierCoordinator` `0x1cf55760` | logs "BarrierCoordinator: Unable to wait for all slices to connect. Saw <K> of <N> expected participants. Seen hosts: <intervals>" (`:371`) |
| Participant-count mismatch | `ProcessRequest` `0x1cf54e60` | `INVALID_ARGUMENT`; `AddRequest` sets `state_=3`, poisons the id, serves the error to this and all future callers of that id |
| Duplicate arrival (distinct host) | `ProcessRequest` | `INVALID_ARGUMENT` "Extra barrier participant"; same poisoning |
| Re-used named id | `Communicator::Barrier` `0x1cca8ee0` | `ALREADY_EXISTS` client-side, before any RPC |
| No coordinator endpoint set | `Communicator::Barrier` | `INTERNAL` "Barriers not available without coordinator set." (`:384`) |
| Transport uninitialised | `Communicator::Barrier` | `INTERNAL` "Transport not initialized." (`:1473`, source-location stamped at `:929`) |

### The "Unable to wait" Destructor Path

The derived destructor checks `seen.size() >= num_participants_`; if not, it emits the canonical "barrier failed" log "BarrierCoordinator: Unable to wait for all slices to connect. Saw <K> of <N> expected participants. Seen hosts: <intervals>" (`topology_coordinator.cc:371`), cancels the status-report alarm (`thread::Cancel` on `+0xb0`), then chains to the base destructor — which, if `state_ == 2`, settles any in-flight notifications via `WaitForNotificationWithTimeout`.

### Propagation and Poisoning

A failed barrier returns a non-OK `absl::Status` to the executor, surfacing through PJRT to JAX. On a coordinator process the surrounding runtime may additionally route the failure through `MegaScaleTransport.ReportError → ErrorReporter → RapidEye` ([ErrorAggregator](error-aggregator.md)), where a barrier hang typically classifies as `HANG_DETECTED` / `PROGRAM_NOT_QUEUED`. A poisoned barrier (`state_=3` after a count/duplicate mismatch) stays poisoned for the life of that `BarrierCoordinator`; a job restart constructs a fresh map and fresh coordinators.

> **QUIRK —** the `BarrierRequest` carries **no** `incarnation_id` (unlike `GetMultiSliceTopologyRequest`, which has field 5). Staleness is handled structurally: collectives mint fresh `__global-auto-<N>` ids per call, so a barrier from a previous launch can never be confused with the current one; named ids rely on the client-side once-per-process used-id set. There is no cross-process generation counter on the barrier itself.

---

## 7. Relationship to the Other Barriers

The Megascale cross-host barrier is one of three distinct barrier tiers in libtpu, each at a different fabric scope and built on different primitives. They are independent — none is layered on another.

| Aspect | **This page — DCN cross-host** | ICI cross-core tree | tpunetd in-slice |
|---|---|---|---|
| Scope | cross-**host** (whole multi-slice job, over DCN) | cross-**core** within a chip/slice (over ICI) | in-**slice** (peers of one slice) |
| Transport | gRPC / protobuf | TensorCore sequencer Vsync ops on SFLAGs | gRPC (`TpuNetworkSessionBarrier`) |
| Topology | centralised star (counter at coordinator) | up-sweep/down-sweep routing-table tree | distributed (master Notifies all, each peer WaitForReady) |
| Wait primitive | `absl::Notification` + gRPC callback fan-out | `VWaitGeOp` on an SFLAG | `CancellableCondVar` / `Mutex::AwaitCommon` |
| Keying | `(slice_id, host_id)` tuple set, count to N | reserved GLOBAL SFLAG `base+count+4` | single shared `barrier_id` string |
| Timeout | gRPC deadline (`FLAGS_tf_tpu_preexecution_barrier_timeout`, 30 s) | none (hardware rendezvous) | `absl::Duration` arg |
| Implementation | `xla::megascale::runtime::BarrierCoordinator` | `net_util::BarrierCoresTree` `0x1c6a75c0` | `superpod::tpunetd_client::BroadcastBarrier` |

The tpunetd `BroadcastBarrier::SyncWithTimeout` (`0x1ff9bce0`) confirms the in-slice barrier is a master-driven Notify/Wait pattern signalled via a `CancellableCondVar`. The Megascale barrier never calls into it; the dependency is purely temporal — tpunetd's in-slice rendezvous must finish before the Megascale runtime even constructs the `CommunicationBackend` (the bootstrap [ICI handoff](bootstrap/convergence.md)).

---

## Related Components

| Name | Relationship |
|---|---|
| `TopologyCoordinator` | sibling `Coordinator<>` specialisation; shares the base, the state machine, the `Notification`, and the alarm |
| `CommunicationBackend` | owns the barrier map (`+0x1b0`), the auto-id counter (`+0x1d0`), and dispatches `OnBarrierRequestReceived` |
| `MegaScaleTransport` | the gRPC service carrying the `Barrier` RPC (3rd of 6 unary methods) |
| `ErrorReporter` / RapidEye | downstream sink for a barrier hang (`HANG_DETECTED` / `PROGRAM_NOT_QUEUED`) |

---

## Cross-References

- [Megascale Overview](overview.md) — DCN vs ICI, where the cross-host barrier sits in job startup and per-launch execution
- [Bootstrap Convergence](bootstrap/convergence.md) — the generic `Coordinator<>` base (state machine, callback fan, Notification, alarm) this barrier inherits; the initial topology barrier
- [ErrorAggregator](error-aggregator.md) — the `ReportError → ErrorReporter → RapidEye` path a barrier failure propagates into
- [tpunetd Protocol](tpunetd-protocol.md) — the in-slice daemon protocol whose `TpuNetworkSessionBarrier` runs *before* this barrier, at a different scope
- [Barriers Overview](../barrier/overview.md) — the on-chip SFLAG barrier model; the cross-host barrier is a separate tier that never touches an SFLAG
- [Tree-Barrier Vsync](../barrier/tree-barrier-vsync.md) — the ICI cross-core up-sweep/down-sweep tree barrier; the on-pod analogue of this host-network barrier
- [Global-Barrier SFLAG Window](../barrier/global-barrier-window.md) — the reserved GLOBAL SFLAG slot the ICI tree barrier rendezvouses on, contrasted with this barrier's `Notification`
