# Topology Exchange

`TopologyCoordinator` is the per-job singleton that accumulates
`GetMultiSliceTopologyRequest` registrations and emits a single
byte-stable response broadcast to every blocked worker. It lives
inside the coordinator process at `CommunicationBackend +0x1a0`
(`*(void**)(backend + 416)`), allocated by
`CommunicationBackend::InitializeCoordinator(int)` at `0x1ccad600`.
Every worker process has a null slot — its
`OnTopologyRequestReceived` (`0x1ccac380`) reaches the
`coordinator == nullptr` branch and returns
`MakeErrorImpl<14>("Topology Coordinator is not ready. Try later.")`.

## Object layout

`TopologyCoordinator` inherits from
`Coordinator<GetMultiSliceTopologyRequest,
GetMultiSliceTopologyResponse,
AnyInvocable<void(StatusOr<...> const&)>>` and adds its own
slice-state and address maps. Two vtables apply: the base
`Coordinator<GetMultiSliceTopology…>` vtable at `0x21c9bbf0` and the
derived `TopologyCoordinator` vtable at `0x21c9baf0` (both confirmed
by `nm -C`). The constructor installs the base vptr `off_21C9BC00`
(= `0x21c9bbf0 + 0x10`) first, then overwrites it with the derived
vptr `off_21C9BB00` (= `0x21c9baf0 + 0x10`). The shape (offsets
read directly out of the ctor at `0x213b7a40` and the base
`AddRequest` at `0x1ccb42a0`):

| Offset    | Size  | Type / Field                                                                          |
|-----------|------:|--------------------------------------------------------------------------------------|
| `+0x00`   |  8    | `vptr` → derived vtable at `0x21c9baf0`                                              |
| `+0x08`   |  0x50 | `TracedMutex mu_` (`TracedMutex::TracedMutex(this+8, /*kind=*/9)`)                    |
| `+0x58`   |  1    | `uint8_t state_` (1=in-progress, 2=completed, 3=error; ctor zeroes it)                |
| `+0x60`   |  0x28 | `StatusOr<GetMultiSliceTopologyResponse> cached_response_` (error rep at `+0x60`, embedded response at `+0x68`; initial = `MakeErrorImpl<14>` "Coordinator in IN_PROGRESS", UNAVAILABLE) |
| `+0x88`   |  0x18 | `std::vector<AnyInvocable<void(StatusOr<…> const&)>> response_setters_` (begin/end/cap at +0x88/+0x90/+0x98, 32 B per entry) |
| `+0xa0`   |  0x10 | `absl::Notification completion_` (`Notify()` at `this+160`)                           |
| `+0xb0`   |  ...  | periodic-report alarm handle (armed via `thread::AddCancellableAt`, stored at `this+176`) |
| `+0xc0`   |  4    | `int32_t num_expected_slices_` (ctor argument; `CHECK(> 0)`)                          |
| `+0xc8`   |  0x20 | `absl::flat_hash_map<std::tuple<int,int>, NetworkAddressMapping> address_map_` ((slice_id, host_id) → address) |
| `+0xe8`   |  0x20 | `absl::flat_hash_map<int, SliceState>` (slice_id → per-slice accumulator)              |

`SliceState` is the flat_hash_map value at `this+0xe8`. The slot
layout observed in `ProcessRequest`/`CreateResponse` stores the
slice's `TpuTopology*` (read as `*(slot+16)`, with the host count at
`*(int*)(topology+108)`) and a nested `flat_hash_map<int, …>` keyed
by host id that tracks the per-host seen count (compared in
`IsComplete` as `slot_count >> 17`).

The base `Coordinator<>` class is also instantiated for
`BarrierCoordinator` with a different `Req`/`Resp` pair; the two
share the same control flow (`AddRequest`, `ScheduleStatusReport`,
the `response_setters_` vector, the Notification).

## Construction

`TopologyCoordinator::TopologyCoordinator(int num_slices)` at
`0x213b7a40` (in `.text.unlikely`, section base `0x21381900`,
because it is called once per job):

```cpp
TopologyCoordinator::TopologyCoordinator(int num_slices) {
  *(void**)this = off_21C9BC00;                  // base vptr (0x21c9bbf0+0x10)
  TracedMutex::TracedMutex(this + 8, /*kind=*/9);

  *(uint8_t*)(this + 88) = 0;                     // state_ = 0
  // Sticky StatusOr seeded with UNAVAILABLE:
  *(void**)(this + 96) = MakeErrorImpl<14>(       // code 14 = UNAVAILABLE
      "Coordinator in IN_PROGRESS", 26, /*line=*/46,
      "platforms/xla/megascale/runtime/communication/topology_coordinator.h");

  *(void**)(this + 184) = 0;                       // response_setters_ tail
  *(__m256*)(this + 0x90) = 0;                      // vector + alarm slots
  *(uint8_t*)(this + 176) = 0;

  *(void**)this = off_21C9BB00;                     // derived vptr (0x21c9baf0+0x10)
  *(int*)(this + 192) = num_slices;                 // num_expected_slices_
  *(__m128*)(this + 0xc8) = 0;                       // address_map_ control
  *(__m128*)(this + 0xe8) = 0;                       // slice_state_ control

  CHECK(num_slices > 0) << "num_expected_slices_ > 0";   // FATAL at line 55
  LOG(INFO) << "Megascale Topology Coordinator started for "
            << num_slices << " slices";                  // line 56, unconditional
}
```

The info-log prefix `"Megascale Topology Coordinator started for "`
lives at rodata `0xa1e728a`; the `" slices"` suffix is a separate
literal appended after the `int`. This line is how an operator
confirms which process is the coordinator. The
`num_expected_slices_ > 0` guard is a `CHECK` (fatal at file line
55), not a conditional around the log — the log at file line 56 is
emitted unconditionally once the ctor is reached.

## Generic `Coordinator<>::AddRequest`

The shared base template handles all of the rendezvous protocol.
The Barrier instance (`0x1ccb42a0`, size `0x7d4`) and the Topology
instance (`0x1cf559c0`, also size `0x7d4`) are byte-for-byte the
same instantiation pattern, so the Barrier decompile reads onto the
Topology one. The three derived hooks are reached through fixed
vtable slots — confirmed by `objdump` of the Barrier `AddRequest`:
`call *0x20(%rax)` = `ProcessRequest`, `call *0x28(%rax)` =
`IsComplete`, `call *0x30(%rax)` = `CreateResponse`. Pseudocode:

```cpp
void Coordinator<Req, Resp, Callback>::AddRequest(
    Req const& req, Callback cb) {

  // VLOG(5) site: "AddRequest: " << ShortFormat(req).
  absl::Time start = absl::Now();
  TracedReleasableMutexLock lock(&this->mu_);

  // State 3 = previously failed; serve sticky error and return
  // (CHECK(response_setters_.empty()) on this path).
  if (this->state_ == 3) {
    cb(this->cached_response_);    // StatusOr holding the error at this+96
    return;
  }

  // Register the request into per-coordinator state via vtable +0x20
  // (= ProcessRequest). Returns a Status: OK accepts, else rejects.
  Status st = vtable_[+0x20].ProcessRequest(this, req);

  // State 2 = already complete; serve the cached response.
  if (this->state_ == 2) {
    cb(this->cached_response_);
    return;
  }

  // State 0/1 = still gathering: queue the response setter.
  this->response_setters_.push_back(std::move(cb));   // 32 B/entry

  // Quorum check via vtable +0x28 (= IsComplete).
  if (vtable_[+0x28].IsComplete(this)) {
    this->state_ = 2;
    this->cached_response_ = vtable_[+0x30].CreateResponse(this);
    for (auto& s : this->response_setters_) s(this->cached_response_);
    this->completion_.Notify();           // absl::Notification at this+160
  } else {
    this->state_ = 1;
    this->ScheduleStatusReport();          // arms periodic ReportStatus
  }
}
```

Disassembly cross-references inside the Barrier instance:

- `0x1ccb4347` — vtable `+0x18` call (`TracedReleasableMutexLock`
  / response-setter helper) on the state-3 fast path.
- `0x1ccb4365` — vtable `+0x20` call (`ProcessRequest`).
- `0x1ccb4499` and `0x1ccb4669` — vtable `+0x28` call
  (`IsComplete`, evaluated twice).
- `0x1ccb44b6` — vtable `+0x30` call (`CreateResponse`).
- `__emplace_back_slow_path` grows the `response_setters_` vector
  (32-byte `AnyInvocable` entries) when it is at capacity.
- non-complete branch: `state_ = 1`, then arm the periodic alarm via
  `thread::DefaultFiberExecutor` + `thread::AddCancellableAt` +
  `LocalInvoker<...ScheduleStatusReport()...>` (handle at `this+176`).
- `absl::Notification::Notify(this+160)` fires after the setter
  fan-out completes.

## `TopologyCoordinator::ProcessRequest`

The derived `ProcessRequest` at `0x1cf524c0` is `0x1dab` (7 595)
bytes. It returns `absl::Status` (not `bool`): a clean accept
returns OK (the `1`/inline-OK rep), and every consistency failure
returns a distinct error that rejects the registration. The base
`AddRequest` does the quorum decision afterward via the separate
`IsComplete` vtable slot. Shape:

```cpp
Status TopologyCoordinator::ProcessRequest(
    GetMultiSliceTopologyRequest const& req) {

  // 1. slice_id bounds: must be in [0, num_expected_slices_).
  int slice_id = req.network_address_mapping().slice_id();
  if (slice_id < 0 || slice_id >= num_expected_slices_) {
    return MakeErrorImpl<3>(  // INVALID_ARGUMENT, file line 183
        Substitute("SliceId out of bounds. Expected num slices: $0. "
                   "Request: $1", num_expected_slices_, req));
  }

  // 2. Look up or create the SliceState entry in slice_state_ (this+0xe8).
  SliceState& slot = slice_state_[slice_id];

  // 3. If a topology was already registered for this slice, compare
  //    it (proto2 MessageDifferencer, EQUIVALENT). On mismatch reject.
  if (slot.has_topology) {
    MessageDifferencer diff;
    diff.set_message_field_comparison(MessageDifferencer::EQUIVALENT);
    std::string text_diff;
    diff.ReportDifferencesToString(&text_diff);
    if (!diff.Compare(slot.topology_args.ToProto(),
                      req.tpu_topology_args())) {
      return MakeErrorImpl<3>(   // INVALID_ARGUMENT, file line 202
        Substitute("Received topology that differs from previously "
          "registered topology at same sliceID. SliceID: $0 "
          "Previous HostId: $1 New HostId: $2 Addresses: $3 Diff: $4",
          ...));
    }
  } else {
    // First registration for this slice: distill+store TpuTopology,
    // record num_expected_slices_-side state.
    slot.topology = TpuTopologySerdes::Construct(req.tpu_topology_args());
    slot.has_topology = true;
  }

  // 4. host_id bounds: must be < topology.host_count()
  //    (= *(int*)(topology + 108)).
  int host_id = req.network_address_mapping().host_id();
  if (host_id < 0 || host_id >= slot.topology->host_count()) {
    return MakeErrorImpl<3>(   // INVALID_ARGUMENT, file lines 235 / 250
        Substitute("HostId out of bounds. hostId: $0. Request: $1",
                   host_id, req));
  }

  // 5. Per-(slice,host) address mapping in address_map_ (this+0xc8).
  //    If already present and the new mapping differs, reject.
  auto& cell = address_map_[{slice_id, host_id}];
  if (cell.present &&
      !MessageDifferencer::Equivalent(cell, req.network_address_mapping())) {
    return MakeErrorImpl<3>(   // INVALID_ARGUMENT, file line 216
      Substitute("Received host address mapping that differs from "
        "previous mapping SliceID: $0 HostId: $1 Prev Address: $2 "
        "New Addresses: $3", ...));
  }
  // Incarnation-id drift on the same (slice,host) is likewise rejected.
  if (incarnation_changed) {
    return MakeErrorImpl<3>(   // INVALID_ARGUMENT, file line 226
      Substitute("Received incarnation ID that is different from "
        "previous incarnation ID. SliceID: $0 HostId: $1 "
        "Prev IncarnationId: $2 New IncarnationId: $3", ...));
  }
  cell.CopyFrom(req.network_address_mapping());

  return absl::OkStatus();
}
```

> **CONFIRMED.** Every consistency check is a hard *reject* that
> returns a non-OK `Status` (all `MakeErrorImpl<3>` =
> INVALID_ARGUMENT). They are not "log and continue" warnings, and
> there is no separate `LogUniqueIds` pass — the topology, host,
> address-mapping, and incarnation comparisons are all inline in
> this one function.

The two `MessageDifferencer::Compare` call chains (topology args
and per-host `NetworkAddressMapping`) dominate the cost; the
slice_state and address-map SwissMap insertions
(`find_or_prepare_insert_large` / `PrepareInsertSmallNonSoo`) make
up most of the rest.

## `TopologyCoordinator::IsComplete`

Decompiled at `0x1cf543a0` (only `0xb6` bytes):

```cpp
bool TopologyCoordinator::IsComplete() const {
  // slice_state_.size() (encoded as size_field >> 17) must reach
  // num_expected_slices_ (this+0xc0, read as *(int*)(this+192)).
  if (slice_state_.size() < num_expected_slices_) return false;

  // Walk SwissMap control bytes; for each occupied slot, check that
  // the per-host seen count has reached the slice's host_count
  // (= *(int*)(topology + 108)).
  for (auto const& [slice_id, slot] : slice_state_) {
    if (slot.seen_count < slot.topology->host_count()) return false;
  }
  return true;
}
```

The completion check is O(num_slices), called once per
`AddRequest` through the base template's vtable `+0x28` slot (not
from inside `ProcessRequest`).

## `TopologyCoordinator::CreateResponse`

Decompiled at `0x1cf54460` (`0x9e6` / 2 534 bytes). Builds a
`MultiSliceTopologyInfo`, serializes it into a `Cord`, and stores
that Cord on the `GetMultiSliceTopologyResponse`:

```cpp
GetMultiSliceTopologyResponse TopologyCoordinator::CreateResponse() {
  MultiSliceTopologyInfo info;
  GetMultiSliceTopologyResponse response;

  // Walk slice_state_ (this+0xe8). For each slice add a SliceInfo and,
  // for every host in [0, topology.host_count()), append the host's
  // NetworkAddressMapping (looked up in address_map_ at this+0xc8;
  // a miss is FATAL: "Missing addresses for SliceID: ...").
  for (auto const& [slice_id, slot] : slice_state_) {
    SliceInfo* si = info.add_slices();
    si->set_slice_id(slice_id);
    si->mutable_topology_args()->CopyFrom(slot.topology.ToProto());
    for (int h = 0; h < slot.topology->host_count(); ++h) {
      auto it = address_map_.find({slice_id, h});      // CHECK != end()
      si->add_host_addresses()->CopyFrom(it->second);
    }
  }

  // Byte-stable ordering: SliceInfo* by slice_id, host-address
  // NetworkAddressMapping* by (slice_id, host_id).
  std::__introsort(slices.begin(),    slices.end(),    $_0);   // 0x1cf56520
  std::__introsort(host_addrs.begin(), host_addrs.end(), $_1); // 0x1cf57360

  VLOG(3) << "Topology Coordinator response: " << info;  // line 311
  LOG(INFO) << "MegaScale Topology Discovery completed."; // line 312

  absl::Cord cord;
  CHECK(info.SerializeToString(&cord));                  // line 315
  response.set_serialized_topology_info(std::move(cord)); // response+24, field 1
  return response;
}
```

> **CONFIRMED.** There is no `shared_seed` / `NewGlobalID()` field
> and no separate `endpoints` repeated field — both were not
> present in the binary. The payload is a single
> `MultiSliceTopologyInfo` serialized into a `Cord` stored at
> `response + 24`. A missing `(slice_id, host_id)` entry in
> `address_map_` is a fatal `CHECK`
> ("`address_mapping != address_map_.end()`",
> "Missing addresses for SliceID: " at rodata `0xa28aff5`).

The two `std::__u::__introsort` instantiations are the byte-stable
sorters: `$_0` over `SliceInfo**` at `0x1cf56520` and `$_1` over
`NetworkAddressMapping**` at `0x1cf57360` (both confirmed by
`nm -C -S`). Their comparators read the `slice_id` (and `host_id`
for `$_1`) of the respective elements.

## Consistency-check error strings

The three rodata strings below are the `MakeErrorImpl<3>`
(INVALID_ARGUMENT) messages emitted *inside* `ProcessRequest`
(`0x1cf524c0`) — they are returned as errors, not logged as
informational warnings:

- `Received topology that differs from previously registered
  topology at same sliceID. SliceID: $0 Previous HostId: $1 New
  HostId: $2 Addresses: $3 Diff: $4` (`0x9b27486`, file line 202).
- `Received host address mapping that differs from previous mapping
  SliceID: $0 HostId: $1 Prev Address: $2 New Addresses: $3`
  (`0x9c14204`, file line 216).
- `Received incarnation ID that is different from previous
  incarnation ID. SliceID: $0 HostId: $1 Prev IncarnationId: $2
  New IncarnationId: $3` (`0x9c14456`, file line 226).

Two more bounds errors share the same code path:
`SliceId out of bounds. Expected num slices: $0. Request: $1`
(`0x9e6f891`, line 183) and `HostId out of bounds. hostId: $0.
Request: $1` (`0x9e6f8cd`, lines 235/250).

## `LogUniqueIds` (worker side)

The real `LogUniqueIds` is *not* a coordinator function. It is an
anonymous-namespace helper
`xla::megascale::runtime::(anonymous namespace)::LogUniqueIds(int
slice_id, int host_id, MultiSliceTopologyAndLocation const& info)`
inlined into `Communicator::Create` (`0x1cca9aa0`) on the worker
side. It deduplicates repeated log spam by remembering the last
ids it saw in three static `int` slots at
`0x223717c0/0x223717c4/0x223717c8`
(`...LogUniqueIds(...)::last_ids.{0,1,2}`), guarded by a static
`absl::Mutex` `unique_id_mutex` at `0x2257b030` with its
`__cxa_guard` variable at `0x2257b038` (all confirmed by `nm -C`).
When the incoming `(slice_id, host_id, …)` triple differs from the
cached one, the worker emits the "Created communicator. …
Slices: … Hosts: …" line (file line 570 / 583) and updates the
slots; otherwise it stays quiet. It has no bearing on the
coordinator's accept/reject decision.

## Response delivery

Once `CreateResponse` finishes, the base `Coordinator<>::AddRequest`
copies the result into `cached_response_` (`+0x60`), sets
`state_ = 2`, then **releases** the `TracedReleasableMutexLock`
(`TracedReleasableMutexLock::Release`) before draining the
`response_setters_` vector (`+0x88`, 32-byte entries) — each setter
serializes the response and hands it to the gRPC layer for the
corresponding worker. After the fan-out it signals the
`absl::Notification completion_` at `+0xa0` (`Notify(this+160)`).
The same path also logs VLOG(5) "Num responses to send: ",
"Response sent ", and "Done processing Request received at: …
Duration: " (and a "Long running topology coordinator AddRequest"
warning past a threshold).

The success log line `"MegaScale Topology Discovery completed."`
(rodata `0xa0a3869`) is emitted by `CreateResponse` itself
(`0x1cf54460`, file line 312), immediately before it serializes the
`MultiSliceTopologyInfo` — not by a separate `ReportStatus()`
observer.

## Cross-References

- [Bootstrap Overview](overview.md) — where this stage sits in the rendezvous sequence and the `Coordinator<>` template it shares with the barrier flow.
- [Worker Registration](worker-registration.md) — the `GetMultiSliceTopologyRequest` schema and server-side callback that feed the `ProcessRequest` path documented here.
- [Convergence](convergence.md) — the generic `Coordinator<>::AddRequest` state machine, pending-callback vector, and `absl::Notification` this page specializes for topology.
