# Topology Exchange

`TopologyCoordinator` is the per-job singleton that accumulates
`GetMultiSliceTopology` registrations and emits a single
byte-stable response broadcast to every blocked worker. It lives
inside the coordinator process at `CommunicationBackend.+0x1a0`,
allocated by `InitializeCoordinator(num_slices)`. Every worker
process has a null slot — its `OnTopologyRequestReceived` rejects
every incoming call.

## Object layout

`TopologyCoordinator` is 264 bytes (0x108), inherits from
`Coordinator<GetMultiSliceTopologyRequest,
GetMultiSliceTopologyResponse,
AnyInvocable<void(StatusOr<...>)>>` (vtable at `0x21c9bbf0`), and
adds three of its own fields. The shape:

| Offset    | Size  | Type / Field                                                                          |
|-----------|------:|--------------------------------------------------------------------------------------|
| `+0x00`   |  8    | `vptr` → derived vtable at `0x21c9baf0`                                              |
| `+0x08`   |  0x50 | `TracedMutex mu_` (kind=9 = "Coordinator")                                            |
| `+0x58`   |  1    | `uint8_t state_` (0=init, 1=ready, 2=completed, 3=error)                               |
| `+0x60`   |  0x60 | `StatusOr<GetMultiSliceTopologyResponse> cached_response_` (initial = UnavailableError) |
| `+0x88`   |  0x18 | `std::vector<AnyInvocable<void(StatusOr<GetMultiSliceTopologyResponse> const&)>> callbacks_` (16 B per entry) |
| `+0xa0`   |  0x20 | `absl::Notification completion_`                                                      |
| `+0xc0`   |  4    | `int32_t num_slices_` (ctor argument)                                                 |
| `+0xc4..` |  ...  | padding                                                                              |
| `+0xe8`   |  0x20 | `absl::flat_hash_map<int, SliceState>` (slice_id → per-slice accumulator)              |

Where `SliceState` is 40 bytes:

```
struct SliceState {
  int32_t              num_hosts_seen;          // +0x00
  int32_t              expected_num_hosts;      // +0x04
  TpuTopologyArgsProto* cached_topology_args;   // +0x08
  HostNetworkAddress*  host_addresses_table;    // +0x10 (arena-owned)
  /* ... */
};
```

The base `Coordinator<>` class is also instantiated for
`BarrierCoordinator` with a different `Req`/`Resp` pair; the two
share the same control flow (`AddRequest`, `ScheduleStatusReport`,
the pending-callback vector, the Notification).

## Construction

`TopologyCoordinator::TopologyCoordinator(int num_slices)` at
`0x213b7a40` (in `.text.unlikely` because called once per job):

```
push %rbp; mov %rsp, %rbp
push %r14; push %rbx
sub $0x20, %rsp
mov %esi, %r14d     // num_slices arg
mov %rdi, %rbx       // this pointer

// Install base vtable, construct the embedded TracedMutex.
lea base_vtable+0x10(%rip), %rax   // 0x21c9bbf0 + 0x10
mov %rax, (%rdi)
TracedMutex::TracedMutex(this+0x08, /*kind=*/9);

// Initialise state slot and the sticky-error StatusOr.
movb $0x0, 0x58(%rbx)
StatusBuilder::CreateStatusAndConditionallyLog(
    line=0x2e, source="topology_coordinator.h");   // template
mov %rax, 0x60(%rbx)

// Zero pending vector + Notification + callbacks tail.
movq $0x0, 0xb8(%rbx)
vmovups %ymm0, 0x90(%rbx)
movb   $0x0, 0xb0(%rbx)

// Install derived vtable.
lea TopologyCoordinator_vtable+0x10(%rip), %rax   // 0x21c9baf0
mov %rax, (%rbx)

// Store num_slices, zero the slice-state map.
mov %r14d, 0xc0(%rbx)
vmovups %xmm0, 0xc8(%rbx)
vmovups %xmm0, 0xe8(%rbx)

// Log iff num_slices > 0:
if (num_slices > 0) {
  LOG(INFO) << "Megascale Topology Coordinator started for "
            << num_slices << " slices";
}
```

The single info-log line (`0xa1e728a` + `0x856f615`) is how an
operator confirms which process is the coordinator. Workers never
emit it because their `InitializeCoordinator` path bails out at
the address mismatch check before reaching this ctor.

## Generic `Coordinator<>::AddRequest`

The shared base template handles all of the rendezvous protocol.
Decompilation of the Barrier instance (`0x1ccb42a0`) maps onto the
Topology instance (`0x1cf559c0`) because both reuse the same
control flow. Pseudocode:

```cpp
void Coordinator<Req, Resp, Callback>::AddRequest(
    Req const& req, Callback cb) {

  // Initial VLOG site (rate-limited).
  if (VLOG_IS_ON(5))
    LOG(INFO) << "Adding request: " << req;

  absl::Time start = absl::Now();   // for TracedMutex contention
  TracedReleasableMutexLock lock(&this->mu_);

  // State 3 = previously failed; serve sticky error immediately.
  if (this->state_ == 3) {
    cb(this->cached_response_);    // a StatusOr holding the error
    return;
  }

  // Always build a per-request response shell first (vtable +0x20).
  // For Topology this writes (slice_id, host_id) into the response
  // so the caller can verify its own slot when the broadcast fires.
  Resp shell = vtable_[+0x20].BuildResponseShell(this, req);

  // State 2 = already complete; serve the cached response.
  if (this->state_ == 2) {
    cb(this->cached_response_);
    return;
  }

  // State 0/1 = still gathering.
  this->callbacks_.push_back(std::move(cb));   // pending fan-out

  // Register the request into the per-coordinator state via
  // vtable +0x28 (= ProcessRequest). Returns true if quorum now met.
  bool complete = vtable_[+0x28].ProcessRequest(this, req);

  if (complete) {
    this->state_ = 2;
    this->cached_response_ = vtable_[+0x30].CreateResponse(this);
    this->completion_.Notify();
    for (auto& pending : this->callbacks_) pending(this->cached_response_);
    this->callbacks_.clear();
  } else if (this->state_ == 0) {
    this->state_ = 1;
    this->ScheduleStatusReport();    // arms periodic ReportStatus
  }
}
```

Disassembly cross-references inside the Barrier instance:

- `0x1ccb4318` — `TracedReleasableMutexLock` ctor.
- `0x1ccb4328` — state-3 fast-path check.
- `0x1ccb4358` — vtable `+0x20` call (`BuildResponseShell`).
- `0x1ccb436b` — state-2 cached-response path.
- `0x1ccb43c6..0x1ccb4451` — `callbacks_.push_back` via
  `__emplace_back_slow_path` (vector grows by doubling).
- `0x1ccb4493` — vtable `+0x28` call (`ProcessRequest`).
- `0x1ccb44a4..0x1ccb44d7` — completion path: set state=2, call
  vtable `+0x30` (`CreateResponse`), copy into cached_response_.
- `0x1ccb4524..0x1ccb45a7` — drain pending callback vector under
  the lock, then `Notification.Notify()` at `0x1ccb4638`.
- `0x1ccb4654..` — non-complete branch: arm the periodic alarm via
  `DefaultFiberExecutor` + `LocalInvoker<...ScheduleStatusReport()...>`.

## `TopologyCoordinator::ProcessRequest`

The derived `ProcessRequest` at `0x1cf524c0` is 6 528 bytes. Its
shape:

```cpp
bool TopologyCoordinator::ProcessRequest(
    GetMultiSliceTopologyRequest const& req) {

  // 1. Bounds check: slice_id must be in [0, num_slices_).
  if (req.slice_id < 0 || req.slice_id >= num_slices_) {
    LOG(ERROR) << "Local SliceID " << req.slice_id
               << " out of bounds. Expected num_slices: "
               << num_slices_;
    cached_response_ = OutOfRangeError("Invalid slice_id.");
    state_ = 3;
    return false;
  }

  // 2. Look up or create the SliceState entry.
  auto& slot = slice_state_[req.slice_id];

  // 3. Topology-args equality check (MessageDifferencer).
  if (slot.first_seen) {
    proto2::util::MessageDifferencer diff;
    diff.set_message_field_comparison(EQUIVALENT);
    std::string text_diff;
    diff.ReportDifferencesToString(&text_diff);
    if (!diff.Compare(req.topology_args, *slot.cached_topology_args)) {
      LOG(ERROR) << "Received topology that differs from previously "
                    "registered topology at same sliceID. "
                    "SliceID: " << req.slice_id
                 << " Previous HostId: " << slot.first_host_id
                 << " New HostId: " << req.host_id
                 << " Diff: " << text_diff;
      return false;   // drop registration; coordinator continues
                      // to serve the originally accepted topology
    }
  } else {
    slot.cached_topology_args = NewInArena(req.topology_args);
    slot.first_host_id = req.host_id;
    slot.expected_num_hosts =
        req.topology_args.host_count();
    slot.first_seen = true;
  }

  // 4. Per-host network mapping check.
  for (auto const& host_addr : req.host_addresses) {
    auto& mapping_slot = slot.host_addresses_table[req.host_id];
    if (mapping_slot.present &&
        !MessageDifferencer::Equivalent(host_addr, mapping_slot.value)) {
      LOG(ERROR) << "Received host address mapping that differs "
                    "from previous mapping "
                    "SliceID: " << req.slice_id
                 << " HostId: " << req.host_id
                 << " Prev Address: " << mapping_slot.value
                 << " New Addresses: " << host_addr;
      // Mapping drift is logged but does NOT abort; coordinator
      // keeps the original mapping.
    } else {
      mapping_slot.value = host_addr;
      mapping_slot.present = true;
    }
  }

  // 5. Increment per-slice host count exactly once.
  if (!slot.host_seen[req.host_id]) {
    slot.host_seen[req.host_id] = true;
    slot.num_hosts_seen++;
  }

  // 6. Capture incarnation drift signal (LogUniqueIds is called
  //    later at response time, not here).

  return this->IsComplete();
}
```

The 3 264 byte `MessageDifferencer::Compare` call chain
(`0x1cf526a4..0x1cf52732`) is the dominant cost; the rest is
flat_hash_map insertion at `0x1cf54280`.

## `TopologyCoordinator::IsComplete`

Decompiled at `0x1cf543a0` (only 0xb4 bytes):

```cpp
bool TopologyCoordinator::IsComplete() const {
  // size() of the slice_state_ map must equal num_slices_.
  if (slice_state_.size() < num_slices_) return false;

  // Walk SwissMap control bytes; for each occupied slot, check that
  // num_hosts_seen >= expected_num_hosts.
  for (auto const& [slice_id, slot] : slice_state_) {
    if (slot.num_hosts_seen < slot.expected_num_hosts) return false;
  }
  return true;
}
```

The tight loop at `0x1cf543e3..0x1cf54453` is the SwissMap
iteration. The completion check is therefore O(num_slices), called
once per `ProcessRequest`.

## `TopologyCoordinator::CreateResponse`

Decompiled at `0x1cf54460` (2 624 bytes). Builds the response in
one pass:

```cpp
GetMultiSliceTopologyResponse TopologyCoordinator::CreateResponse() {
  MultiSliceTopologyInfo info;
  GetMultiSliceTopologyResponse response;

  // Walk slice_state_ in numeric order (sort at the end).
  for (auto const& [slice_id, slot] : slice_state_) {
    SliceInfo* si = info.add_slices();
    si->set_slice_id(slice_id);
    si->mutable_topology_args()->CopyFrom(*slot.cached_topology_args);
    for (int h = 0; h < slot.expected_num_hosts; ++h) {
      si->add_host_addresses()->CopyFrom(slot.host_addresses_table[h]);

      NetworkAddressMapping* ep = info.add_endpoints();
      ep->set_slice_id(slice_id);
      ep->set_host_id(h);
      *ep->add_addresses() = slot.host_addresses_table[h];
    }
  }

  // Sort SliceInfo* by slice_id and NetworkAddressMapping* by
  // (slice_id, host_id) for byte-stable output.
  std::sort(info.mutable_slices()->begin(),
            info.mutable_slices()->end(), $_0);
  std::sort(info.mutable_endpoints()->begin(),
            info.mutable_endpoints()->end(), $_1);

  *response.mutable_multi_slice_topology_info() = std::move(info);
  response.set_shared_seed(util::random::NewGlobalID());
  return response;
}
```

The two `std::__u::__introsort` instantiations at `0x1cf56520`
and `0x1cf57360` are the byte-stable sorters; comparator lambdas
`$_0` and `$_1` capture `*this` and read `+0x20` (slice_id) and
`+0x28` (host_id) of the respective elements.

## `LogUniqueIds`

Once `CreateResponse` returns, the coordinator calls
`xla::megascale::runtime::(anonymous)::LogUniqueIds(int slice_id,
int host_id, MultiSliceTopologyAndLocation const& info)`. The
function maintains three static slots at `0x223717c0..0x223717c8`
plus a guard variable / mutex pair at `0x2257b030 / 0x2257b038`:

- Slot 0: last seen `(slice_id, topology_proto_hash)`.
- Slot 1: last seen `(host_id, network_address_proto_hash)`.
- Slot 2: last seen `(slice_id, host_id, incarnation_id)`.

Any mismatch emits one of the rodata warnings:

- `Received topology that differs from previously registered
  topology at same sliceID. SliceID: $0 Previous HostId: $1 New
  HostId: $2 Addresses: $3 Diff: $4` (`0x9b27486`).
- `Received host address mapping that differs from previous mapping
  SliceID: $0 HostId: $1 Prev Address: $2 New Addresses: $3`
  (`0x9c14204`).
- `Received incarnation ID that is different from previous
  incarnation ID. SliceID: $0 HostId: $1 Prev IncarnationId: $2
  New IncarnationId: $3` (`0x9c14456`).

The warnings are informational — neither aborts the coordinator
nor invalidates the cached response. They show up in
post-mortems when an operator wants to understand "why did the
fleet's address table change at time T".

## Response delivery

Once `CreateResponse` finishes, the base `Coordinator<>::AddRequest`
broadcasts the response to every pending callback (`callbacks_`
vector at `+0x88`) under the still-held `TracedReleasableMutexLock`,
then notifies the `absl::Notification` at `+0xa0`. The gRPC layer
serializes each callback's response to wire format and sends it
back to the corresponding worker.

The success log line on the coordinator is
`"MegaScale Topology Discovery completed."` (rodata `0xa0a3869`),
emitted by `ReportStatus()` once it observes
`IsComplete() == true`.
