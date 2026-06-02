# Worker Registration

Every Megascale worker — including the coordinator's own
host-process — registers itself with the coordinator by issuing a
single `GetMultiSliceTopology` gRPC. The request carries the
worker's `(slice_id, host_id)` identity, its locally bound network
endpoints, the per-slice `TpuTopologyArgsProto` derived from
tpunetd, and an `incarnation_id` that lets the coordinator detect
silent restarts.

## The request

Wire path:
`/xla.megascale.runtime.MegaScaleTransport/GetMultiSliceTopology`

Message: `xla.megascale.runtime.GetMultiSliceTopologyRequest`,
package `xla.megascale.runtime`, syntax `editions`. The proto
descriptor lives at `protodesc_cold` VA `0xbf81634`.

| Field # | Name              | Label    | Type                                                | Meaning |
|--------:|-------------------|----------|-----------------------------------------------------|---------|
|  1      | `slice_id`        | optional | `int32`                                             | This process's slice id (from `MEGASCALE_SLICE_ID`). |
|  2      | `host_id`         | optional | `int32`                                             | This process's host id within the slice (derived from `TPU_WORKER_ID` / `TpuTopologyArgsProto` row indexing). |
|  3      | `host_addresses`  | repeated | `xla.megascale.runtime.HostNetworkAddress`          | The local `MegaScaleTransport` bind point(s). Workers typically register one entry; multi-homed hosts may register multiple. |
|  4      | `topology_args`   | optional | `tpu.TpuTopologyArgsProto`                          | The per-slice topology proto (chip layout, ICI dimensions). Must agree across all hosts of the same slice. |
|  5      | `incarnation_id`  | optional | `fixed64`                                           | Per-process random id from `util::random::NewGlobalID()`. Allows the coordinator to detect process restarts within the same `(slice_id, host_id)` slot. |

`HostNetworkAddress` carries `(string host, int32 port,
optional int32 numa_node, optional string transport_type)`. The
`transport_type` matches `Options.transport_type` ("grpc" or
"chaotic_good_legacy"), so the coordinator can later select the
correct transport per peer.

The request is constructed inside
`CommunicationBackend::DiscoverTopologyAndAddressBindings(
int local_slice_id, TpuTopologyArgsProto args, int local_host_id,
int num_slices)` (decompiled at `0x1ccacb80`). The request
fields are populated in this order:

```
GetMultiSliceTopologyRequest req(/*arena=*/nullptr);

// 1. host_addresses (field 3, RepeatedPtrFieldBase::Add at
//    0x1ccaccaa).
HostNetworkAddress* addr =
    req.add_host_addresses();
addr->set_host(backend.megascale_port_name);   // or hostname
addr->set_port(backend.megascale_port);

// 2. (slice_id, host_id) — populated into req via
//    NetworkAddressMapping ctor at 0x1ccad780; the resulting
//    req->+0x28 / +0x2c hold the int32 fields.
req.set_slice_id(local_slice_id);
req.set_host_id(local_host_id);

// 3. topology_args (field 4)
*req.mutable_topology_args() = args;

// 4. incarnation_id (field 5) — NewGlobalID() from ctor
req.set_incarnation_id(backend.global_id_);  // backend->+0x28
```

## The response

`xla.megascale.runtime.GetMultiSliceTopologyResponse` carries the
assembled cluster description:

| Field # | Name                                | Label    | Type                                                |
|--------:|-------------------------------------|----------|-----------------------------------------------------|
|  1      | `multi_slice_topology_info`         | optional | `xla.megascale.runtime.MultiSliceTopologyInfo`      |
|  2      | `endpoint_addresses`                | repeated | `xla.megascale.runtime.NetworkAddressMapping`       |
|  3      | `multi_slice_topology_and_location_proto` | optional | `xla.megascale.runtime.MultiSliceTopologyAndLocationProto` |
|  4      | `shared_seed`                       | optional | `fixed64`                                           |

Where:

- `MultiSliceTopologyInfo` has a `repeated SliceInfo slices` and a
  `repeated NetworkAddressMapping endpoints`.
- `SliceInfo` has `int32 slice_id`, `tpu.TpuTopologyArgsProto
  topology_args`, and `repeated HostNetworkAddress host_addresses`.
- `NetworkAddressMapping` has the per-`(slice_id, host_id)` slot
  with a `repeated HostNetworkAddress addresses`.
- `shared_seed` lets every worker derive RNG state that needs to
  be cluster-wide-consistent (e.g. randomised collective schedules).

## Server-side: `OnTopologyRequestReceived`

The gRPC server stub
`WithCallbackMethod_GetMultiSliceTopology` (typeinfo at `0xb46ad48`)
dispatches every inbound request to
`CommunicationBackend::OnTopologyRequestReceived(req, reply_cb)`
(decompiled at `0x1ccac380`).

The handler is short and deliberately just a forwarder to the
`TopologyCoordinator`:

```cpp
void OnTopologyRequestReceived(
    GetMultiSliceTopologyRequest const& req,
    AnyInvocable<void(StatusOr<GetMultiSliceTopologyResponse> const&)> cb) {
  VLOG(3) << "OnTopologyRequestReceived: " << req;

  TracedMutexLock lock(&this->mu_);    // backend.+0xe0, kind=6

  TopologyCoordinator* tc = this->topology_coordinator_;  // +0x1a0
  if (tc == nullptr) {
    lock.Release();
    LOG(INFO) << "TopologyCoordinator not initialized. ...";  // line 0x3ad
    cb(InternalError("TopologyCoordinator not initialized.",
                     line 0x3b0));
    return;
  }

  // Move the callback into the coordinator's pending vector.
  // Implementation detail: the coordinator's vtable[+0x10] is
  // `AddRequest`, which both records the registration and stashes
  // the callback for later notification.
  tc->AddRequest(req, std::move(cb));
}
```

The 0x1ccac380..0x1ccac553 disassembly shows:

- `0x1ccac3b4` constructs `TracedMutexLock` over `backend.+0xe0`.
- `0x1ccac3b9` loads `topology_coordinator_` from `backend.+0x1a0`.
- `0x1ccac3c0` branches to the "not initialized" path when
  `topology_coordinator_` is null.
- `0x1ccac3da` copy-constructs the `AnyInvocable` callback into the
  stack slot at `-0x70` (because we'll later transfer it into the
  coordinator's vector).
- `0x1ccac409` calls the coordinator's virtual `AddRequest` via
  `(*tc->vtable)[+0x10]`.

`OnBarrierRequestReceived` at `0x1ccac5c0` follows the same
pattern with two differences:

1. The lookup goes through the `flat_hash_map<string,
   unique_ptr<BarrierCoordinator>>` at `backend.+0x228`, keyed by
   `req.barrier_id`. A missing entry triggers
   `operator[]` which constructs a fresh `BarrierCoordinator(
   barrier_id, num_workers_)` on the fly. The `num_workers_` value
   is the same `num_slices · num_hosts_per_slice` count used by
   `TopologyCoordinator`.
2. The reply type is `BarrierResponse`, and the coordinator's
   `IsComplete()` test counts string keys
   `printf("$0:$1", slice_id, host_id)` in a `flat_hash_set<string>`
   rather than slice-keyed map entries.

## What the request carries that the coordinator validates

The coordinator does not blindly merge incoming requests. Inside
`TopologyCoordinator::ProcessRequest` (`0x1cf524c0`), every
inbound `topology_args` is compared against the first one seen for
this `slice_id` slot:

```cpp
// Pseudocode reconstruction of 0x1cf524c0..0x1cf527a9
auto& slot = slice_state_.find_or_emplace(req.slice_id);
if (slot.first_seen) {
  proto2::util::MessageDifferencer diff;
  diff.set_message_field_comparison(EQUIVALENT);
  std::string differences;
  diff.ReportDifferencesToString(&differences);
  if (!diff.Compare(req.topology_args, slot.cached_topology_args)) {
    LOG(ERROR) << "Received topology that differs from previously "
                  "registered topology at same sliceID. "
                  "SliceID: " << req.slice_id
               << " Previous HostId: ... New HostId: ..."
               << " Diff: " << differences;
    return /* drop registration; do not increment count */;
  }
}
slot.cached_topology_args = req.topology_args;
slot.host_addresses[req.host_id] = req.host_addresses;
slot.num_hosts_seen++;
```

Two more `MessageDifferencer` checks fire on the per-host
`NetworkAddressMapping` (`0x1cf52760..0x1cf527a9`) and on the
`incarnation_id` slot. The corresponding warnings are at rodata
`0x9c14204` (address mapping drift) and `0x9c14456` (incarnation
drift).

## What the request does NOT carry

- **No magic / sanity field.** The protobuf schema validation is
  trusted — there's no equivalent of libnccom's `0x61796c69` "ilya"
  sanity (see P-3-49). A malformed inbound message is rejected by
  proto2 parsing before reaching the handler.
- **No authentication token at the application layer.** Authentication
  is done by the gRPC `ServerCredentials` selected via
  `MEGASCALE_AUTHENTICATION`. Once a peer is authenticated by gRPC,
  every field in its request is trusted.
- **No retry counter.** Megascale's bootstrap is a single-shot RPC
  with a deadline. Retry policy is the caller's responsibility (XLA
  Megascale runtime does not retry GetMultiSliceTopology
  internally).
- **No request-level lease.** The registration stays in the
  coordinator until the response fires; there is no notion of a
  worker "renewing" its registration. HeartBeat runs after
  bootstrap on a separate RPC family and serves only liveness, not
  re-registration.
