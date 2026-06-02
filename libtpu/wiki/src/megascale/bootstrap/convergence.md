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

```
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
is 16 bytes — 8 bytes function pointer / payload + 8 bytes manager
pointer, matching libc++'s `AnyInvocable` SSO layout). Workers
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
then awaits the response. Other in-process consumers (e.g. the
debug surface that emits `MegaScaleTopologyCoordinator`-related
streamz counters) may also wait on this Notification.

For gRPC clients the Notification is not relevant — the callback
fan in `callbacks_` is the signalling mechanism.

## `ScheduleStatusReport`

When the coordinator transitions from state 0 → 1, it arms a
periodic alarm via the threadpool / fiber executor. The relevant
code path is at `0x1ccb4670..0x1ccb46b1` in the Barrier
`AddRequest`:

```cpp
if (state_ == 0) {
  state_ = 1;
  thread::Executor* exec = thread::DefaultFiberExecutor();
  absl::Time now = absl::Now();
  absl::Time deadline = now + absl::Seconds(1);  // increment in
                                                  // 0x1ccb4694
  ScheduleAt(exec, deadline,
             [this] { this->ScheduleStatusReport(); });
}
```

The body of the `ScheduleStatusReport` lambda (visible at
`Coordinator<...>::ScheduleStatusReport()::{lambda(...)#1}` —
typeinfo at `0x21c33908` slot) calls back into the derived
`ReportStatus()` (vtable `+0x40` on the derived class) and then
re-arms itself unless the state is now 2 or 3.

The effective cadence is set inside `Executor::ScheduleAt` —
`absl::Seconds(1)` initialises the first deadline; subsequent
re-arms typically scale up via the executor's internal backoff to
avoid log floods. The observed cadence in production logs is on
the order of seconds to a minute.

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
  size_t seen = seen_set_.size();                  // +0xe0 size
  int num_workers = this->num_workers_;            // +0xd0
  std::string seen_hosts = GetSeenHosts();
  LOG(INFO) << "MegaScale Barrier in progress. Seen " << seen
            << " of " << num_workers
            << " expected participants. Seen hosts: " << seen_hosts;
}
```

The instructions at `0x213b7d5b..0x213b7daf` load
`seen_set_.size()` (a SwissMap size) and `num_workers_` from
`+0xd0`, then concatenate via `LogMessage::ls<size_t>` and
`LogMessage::ls<int>`.

## Why convergence is single-shot

There is no re-arming after state=2. The coordinator's cached
response is set once and serves every subsequent registration. The
implication: if a worker restarts after bootstrap, its
`GetMultiSliceTopology` re-registration immediately resolves with
the cached response (the same one delivered to its predecessor),
without forcing the coordinator to wait for any other worker.

This is what makes silent worker restart compatible with the
rendezvous: as long as the restarted worker has the same
`(slice_id, host_id)` and produces an equivalent `topology_args`,
the cache stays valid. The drift warnings emitted by
`LogUniqueIds` are the only forensic record that a restart
happened.
