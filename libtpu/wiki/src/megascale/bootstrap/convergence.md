# Convergence

The convergence step — "all workers have registered, all callbacks
fire" — is implemented by the generic `Coordinator<Req, Resp,
Callback>` template that both `TopologyCoordinator` and
`BarrierCoordinator` inherit from. The template owns a small state
machine, a pending-callback vector, an `absl::Notification`, and a
periodic alarm that emits human-readable status while the
rendezvous is in progress.

## The state machine

| State | Meaning |
|-------|---------|
| `0`   | initial — no requests received yet, no alarm scheduled |
| `1`   | ready — at least one request received, periodic status report armed |
| `2`   | completed — `IsComplete()` returned true; `cached_response_` holds the success response; every pending callback has been invoked; future requests serve the cache |
| `3`   | error — a fatal condition was set (e.g. `slice_id` out of range); `cached_response_` holds the sticky error; every callback (past and future) serves the same error |

The state byte lives at `+0x58` of every Coordinator instance.
Transitions:

```text
       state=0
         │  first AddRequest
         ▼
       state=1  ──ScheduleStatusReport──┐
         │                              │
         │  AddRequest                  │
         │                              │
         │  IsComplete()=true           │
         ▼                              │
       state=2  ◄──────────────────────┘  CancelAlarm
         │
         │  any AddRequest → serve cached_response_
         ▼
       state=2 (stable)

       state=0 or 1 + fatal request (out-of-range slice_id, etc.)
         │
         ▼
       state=3  → every callback serves the sticky StatusOr
```

## The pending-callback vector

Field `+0x88` of `Coordinator<>` is a
`std::vector<AnyInvocable<void(StatusOr<Resp> const&)>>` (each entry
is 32 bytes — the slow-path helper and the inline push both stride by
`0x20`; the entry holds the `absl::AnyInvocable` invoker pointer,
manager pointer, and the type-erased state buffer). Workers
arrive asynchronously; when the coordinator is still in state 0/1,
each handler pushes its `cb` into this vector and returns immediately
without responding over gRPC. The gRPC framework holds the request
open on the server side because the handler did not call its
`finish` callback.

Once `IsComplete()` returns true (or the state transitions to 3),
the coordinator walks the vector and invokes every callback with
the same `StatusOr<Resp>`. Each invocation triggers the gRPC
framework to serialise the response and complete the corresponding
client RPC.

The vector grows by doubling (standard libc++ behaviour); the
`emplace_back_slow_path` symbol at `0x1ccb4ac0` is the
out-of-capacity helper.

## The Notification

Field `+0xa0` is a 32-byte `absl::Notification`. The completion
broadcast at `Coordinator<>::AddRequest` (e.g. `0x1ccb4638` in the
Barrier instance) calls `Notification::Notify()` after every pending
callback has been served.

The Notification is used by **synchronous in-process callers** that
talk to the coordinator directly without going through gRPC.
Specifically: when `CommunicationBackend::DiscoverTopologyAnd
AddressBindings` is called on the coordinator process itself, the
runtime takes a fast path that calls
`topology_coordinator_->AddRequest(...)` synchronously (no gRPC) and
then awaits the response. Any other in-process consumer that needs to
block until the rendezvous completes can wait on this same
Notification.

For gRPC clients the Notification is not relevant — the callback
fan in `callbacks_` is the signalling mechanism.

## `ScheduleStatusReport`

When the coordinator transitions from state 0 → 1, it arms a
periodic alarm via the fiber executor. In the Barrier `AddRequest`
(`0x1ccb42a0`) the state byte is set at `0x1ccb4654`
(`movb $0x1,0x58(%r14)`), the executor is fetched at `0x1ccb4677`
(`call thread::DefaultFiberExecutor`), the deadline is computed at
`0x1ccb4696` (`call absl::Duration::operator+=`), and the alarm is
armed at `0x1ccb46c9` (`call thread::AddCancellableAt`):

```cpp
if (state_ == 0) {                                 // 0x1ccb4654
  state_ = 1;
  thread::Executor* exec = thread::DefaultFiberExecutor();   // 0x1ccb4677
  absl::Time deadline = absl::Now();
  deadline += /* ~1s */;                           // operator+= @0x1ccb4696
  thread::AddCancellableAt(exec, deadline,         // 0x1ccb46c9
                           [this] { this->ScheduleStatusReport(); },
                           &alarm_handle_ /* this+0xb0 */);
}
```

The arming step is gated by `IsComplete()` (the `(*vtable+40)(this)`
call just before it): the alarm is only scheduled if the rendezvous
is not already complete.

The body of the `ScheduleStatusReport` lambda
(`Coordinator<...>::ScheduleStatusReport()::{lambda()#1}::operator()`
at `0x1ccb4ea0` for the Barrier instantiation) first invokes the
derived `ReportStatus()` through vtable slot `+0x38`
(`(*vtable+56)(this)`), then re-checks `IsComplete()` (vtable slot
`+0x28`, `(*vtable+40)(this)`); if the rendezvous is still
incomplete it re-arms itself via the same `thread::AddCancellableAt`
path, re-targeting the alarm handle at `this+0xb0`.

The re-arm interval is a fixed constant — every re-arm uses the same
`absl::Duration::operator+=` increment (≈1 s) seen at the initial
arm; there is no exponential backoff. The loop terminates the first
time `IsComplete()` returns true, at which point no further alarm is
scheduled.

## `TopologyCoordinator::ReportStatus`

Decompiled at `0x213b7ba0` (in `.text.unlikely`). The body:

```cpp
void TopologyCoordinator::ReportStatus() const {
  if (this->IsComplete()) {
    LOG(INFO) << "MegaScale Topology Discovery completed.";   // 0xa0a3869
    return;
  }

  MissingHostsList missing = GetMissingHosts();
  LOG(INFO) << "MegaScale Topology Discovery in progress. "
               "Missing hosts (num_slices=" << missing.num_slices
            << ", num_hosts=" << missing.num_hosts
            << "): " << missing.text;
}
```

`GetMissingHosts()` at `0x1cf51340` walks `slice_state_` and, for
every slice id `s` in `[0, num_slices_)`, returns the list of host
ids that have either never been seen (slice missing from the map)
or are not yet at quorum. The list is formatted via `absl::StrCat`.

## `BarrierCoordinator::ReportStatus`

Decompiled at `0x213b7ce0`. Same shape but uses
`GetSeenHosts()` for the participant list:

```cpp
void BarrierCoordinator::ReportStatus() const {
  if (this->IsComplete()) {
    LOG(INFO) << "MegaScale Barrier completed.";   // 0xa0a3986
    return;
  }
  size_t seen = *(uint64_t*)(this+0xe0) >> 17;     // SwissMap size: field>>17
  int num_workers = this->num_workers_;            // +0xd0 (32-bit)
  std::string seen_hosts = GetSeenHosts();         // 0x1cf55280
  LOG(INFO) << "MegaScale Barrier in progress. Seen " << seen
            << " of " << num_workers
            << " expected participants. Seen hosts: " << seen_hosts;
}
```

The seen count is not a method call: at `0x213b7d5b` the code does
`mov 0xe0(%rbx),%rax` then `shr $0x11,%rax` at `0x213b7d62` — the raw
SwissMap header field at `+0xe0` shifted right by 17, the same
`size>>17` idiom `IsComplete` uses. `num_workers_` is loaded as a
32-bit value at `0x213b7d8d` (`mov 0xd0(%rbx),%eax`). The two values
are streamed through `LogMessage::operator<<<unsigned long>` and
`LogMessage::operator<<<int>` respectively.

## Why convergence is single-shot

There is no re-arming after state=2. The coordinator's cached
response is set once and serves every subsequent registration. The
implication: if a worker restarts after bootstrap, its
`GetMultiSliceTopology` re-registration immediately resolves with
the cached response (the same one delivered to its predecessor),
without forcing the coordinator to wait for any other worker.

This is what makes silent worker restart compatible with the
rendezvous: as long as the restarted worker re-presents the same
`(slice_id, host_id)`, an equivalent `topology_args`, **and** the
same `incarnation_id`, `ProcessRequest` (which runs before the
state-2 cached-serve path in `AddRequest`) accepts it and the cache
stays valid. A restart that drifts on any of those — a new
`incarnation_id` in particular — is rejected with INVALID_ARGUMENT by
`ProcessRequest` rather than served the cache (see
[Failure Handling](failure-handling.md)). The worker-side
`LogUniqueIds` "Created communicator." line is the forensic record
that the local `(slice_id, host_id, incarnation_id)` triple changed.
