# Worker Registration

Every Megascale worker — including the coordinator's own
host-process — registers itself with the coordinator by issuing a
single `GetMultiSliceTopology` gRPC. The request carries a single
`NetworkAddressMapping` (the worker's `(slice_id, host_id)`
identity plus its locally bound network endpoints), the per-slice
`TpuTopologyArgsProto`, and an `incarnation_id` that lets the
coordinator detect silent restarts.

## The request

Wire path:
`/xla.megascale.runtime.MegaScaleTransport/GetMultiSliceTopology`
(string at rodata `0x84b9e72`).

Message: `xla.megascale.runtime.GetMultiSliceTopologyRequest`,
package `xla.megascale.runtime`, syntax `proto3`. The serialized
`FileDescriptorProto` lives in `descriptor_table_protodef_0ymGVhRfurb`
(symbol at `0xbf814f0`); the request's descriptor text begins at VA
`0xbf81634`.

> **CONFIRMED** — field numbers, labels and types decoded byte-for-byte
> from the descriptor blob at `0xbf81634`: `address_mapping` =
> `18 01 20 01 28 0b`, `tpu_topology_args` = `18 02 20 01 28 0b`,
> `incarnation_id` = `18 03 20 01 28 03` (type 3 = INT64).

| Field # | Name                | Label    | Type                                          | Meaning |
|--------:|---------------------|----------|-----------------------------------------------|---------|
|  1      | `address_mapping`   | optional | `xla.megascale.runtime.NetworkAddressMapping` | This process's `(slice_id, host_id)` plus the `MegaScaleTransport` bind point(s). |
|  2      | `tpu_topology_args` | optional | `tpu.TpuTopologyArgsProto`                    | The per-slice topology proto (chip layout, ICI dimensions). Must agree across all hosts of the same slice. |
|  3      | `incarnation_id`    | optional | `int64`                                       | Per-process id. Allows the coordinator to detect process restarts within the same `(slice_id, host_id)` slot. |

`NetworkAddressMapping` (defined in `addresses.proto`, descriptor at
`0xc1795bd`) carries `(int32 slice_id [1], int32 host_id [2],
repeated HostNetworkAddress addresses [3])`. `HostNetworkAddress`
(descriptor at `0xc17954a`) carries `(string address [1], string
interface_name [2], string host_name_for_debugging [3], int32
numa_node [4])` — there is no `port` or `transport_type` field on the
wire; the address is the full endpoint string.

The request is constructed inside
`CommunicationBackend::DiscoverTopologyAndAddressBindings(
int local_slice_id, TpuTopologyArgsProto args, int local_host_id,
int num_slices)` (decompiled at `0x1ccacb80`). The request is built
field-by-field:

```c
GetMultiSliceTopologyRequest req(/*arena=*/nullptr);

// 1. address_mapping (field 1) — a NetworkAddressMapping default-
//    constructed on the request's arena. Its scalar int32 fields
//    are stored at offsets +40 / +44 of the sub-message, and the
//    addresses[] repeated field is appended into via
//    proto2::Arena::DefaultConstruct<HostNetworkAddress>.
NetworkAddressMapping* am = req.mutable_address_mapping();
am->set_slice_id(/*+40*/ ...);
am->set_host_id(/*+44*/ ...);
//    addresses[] is populated from the locally bound
//    HostNetworkAddress list (built into NetworkAddressMapping v85
//    at function entry, then copied/swapped element-wise).

// 2. tpu_topology_args (field 2) — CopyFrom / InternalSwap of args.
*req.mutable_tpu_topology_args() = args;
```

The request is then logged with `VLOG(3) << "Sending topology
discovery request: " << req` (line 1046) and dispatched through the
transport's virtual send at `*(transport->vtable + 40)`.

> **INFERRED** — the field decompiles as a nested
> `NetworkAddressMapping` write (offsets `+40`/`+44`) rather than
> direct `set_slice_id`/`set_host_id` calls on the request, because
> `slice_id`/`host_id` live on the `address_mapping` sub-message, not
> on `GetMultiSliceTopologyRequest` itself. `0x1ccad780` is
> `PopulateMockMultiSliceTopologyInfo` (the fake-discovery path), not a
> `NetworkAddressMapping` constructor.

## The response

`xla.megascale.runtime.GetMultiSliceTopologyResponse` carries a
single field — the assembled cluster description serialized as bytes:

| Field # | Name                       | Label    | Type    |
|--------:|----------------------------|----------|---------|
|  1      | `serialized_topology_info` | optional | `bytes` |

> **CONFIRMED** — descriptor at `0xbf8179a`: `serialized_topology_info`
> = `18 01 20 01 28 0c` (type 12 = BYTES) followed by field options
> `42 02 08 01`. There is no `shared_seed`, `endpoint_addresses`, or
> top-level `MultiSliceTopologyInfo`/`...AndLocationProto` field on the
> response message.

The `bytes` payload is a serialized `MultiSliceTopologyInfo` (the
caller parses it via `MessageLite::MergeFromString`, seen in
`DiscoverTopologyAndAddressBindings` at line 1064 logging "Received
topology discovery response: "). `MultiSliceTopologyInfo` (descriptor
at `0xbf816ea`) has:

- `repeated SliceInfo slice_info` (field 1),
- `repeated NetworkAddressMapping address_mappings` (field 2),
- `int64 incarnation_id` (field 3).

## Server-side: `OnTopologyRequestReceived`

The gRPC server stub
`WithCallbackMethod_GetMultiSliceTopology` (typeinfo *name* `_ZTS...`
at `0xb46ad48`; the typeinfo object itself is at `0x21c46e50`)
dispatches every inbound request to
`CommunicationBackend::OnTopologyRequestReceived(req, reply_cb)`
(decompiled at `0x1ccac380`, source `communication_backend.cc`).

The handler is short and just forwards to the `TopologyCoordinator`
through its vtable:

```cpp
void OnTopologyRequestReceived(
    GetMultiSliceTopologyRequest const& req,
    AnyInvocable<void(StatusOr<GetMultiSliceTopologyResponse> const&)> cb) {
  VLOG(3) << "Received topology request: " << req;   // line 937

  TracedMutexLock lock(&this->mu_);    // backend.+0xe0 (224)

  TopologyCoordinator* tc = this->topology_coordinator_;  // +0x1a0 (416)
  if (tc == nullptr) {
    // status #1: INTERNAL "TopologyCoordinator not initialized." (line 1514)
    Status err = InternalError("TopologyCoordinator not initialized.");
    lock.Release();
    LOG(INFO) << "Received toplogy request message with no topology "
                 "coordinator. This occurs if CreateCommunicator has "
                 "not been called.";                  // line 941
    // status #2 actually delivered to the caller:
    cb(MakeError<UNAVAILABLE/*14*/>(
        "Topology Coordinator is not ready. Try later.", line 944));
    return;
  }

  // Forward to the coordinator via its virtual at vtable[+0x10],
  // moving the callback in (the request's registration and callback
  // bookkeeping happen inside the coordinator).
  (*tc->vtable[+0x10])(tc, req, std::move(cb));
}
```

> **CONFIRMED** — the null path constructs **two** statuses:
> `MakeErrorImpl<13>` (INTERNAL) "TopologyCoordinator not initialized."
> at line `0x5ea`=1514 (call `0x1ccac441`), and the status actually
> handed to the callback is `MakeErrorImpl<14>`
> "Topology Coordinator is not ready. Try later." at line `0x3b0`=944
> (call `0x1ccac49e`, invoked via `*(cb+0x18)` at `0x1ccac4ae`). The
> INFO log between them is at line `0x3ad`=941. All strings live in
> `communication_backend.cc` rodata (`0x878fb6f`).

The 0x1ccac380..0x1ccac553 disassembly shows:

- `0x1ccac3a9` loads `lea 0xe0(%r15)` — the `TracedMutex` at
  `backend.+0xe0` — and `0x1ccac3b4` constructs `TracedMutexLock`.
- `0x1ccac3b9` loads `topology_coordinator_` from `backend.+0x1a0`.
- `0x1ccac3c0`/`0x1ccac3c3` (`test`/`je 1ccac426`) branch to the
  "not initialized" path when `topology_coordinator_` is null.
- `0x1ccac3dd` (`call *0x10(%rbx)`) moves the inbound `AnyInvocable`
  callback out; `0x1ccac3e5` stashes its manager into stack slot
  `-0x70`.
- `0x1ccac409` (`call *0x10(%rax)`) forwards to the coordinator's
  virtual at `(*tc->vtable)[+0x10]`.

`OnBarrierRequestReceived` at `0x1ccac5c0` follows the same
pattern with two differences:

1. The lookup goes through the `flat_hash_map<string,
   unique_ptr<BarrierCoordinator>>` at `backend.+0x1b0` (432, set up
   as `add $0x1b0,%r15` at `0x1ccac5fc` before `operator[]` at
   `0x1ccac60e`), keyed by `req.barrier_id` (the request's
   `barrier_id` string at `req+0x18`). A missing entry triggers
   `operator[]`, which constructs a fresh `BarrierCoordinator(
   barrier_id, req.num_participants)` on the fly (ctor at
   `0x1ccb3fa0`); the count is the request's own `num_participants`
   field (`req+0x28`), not a backend `num_workers_` member.
2. The reply type is `BarrierResponse`. The request's
   `(slice_id, host_id, barrier_id)` are read from `req+0x20` /
   `req+0x24` / `req+0x18` (used for the VLOG(3) at line 954,
   "Received barrier request from (SliceId, HostId, barrier_id) ").

## What the request carries that the coordinator validates

The coordinator does not blindly merge incoming requests. Inside
`TopologyCoordinator::ProcessRequest` (`0x1cf524c0`,
`topology_coordinator.cc`), `req.address_mapping.slice_id` keys two
maps on the coordinator object:

- `flat_hash_map<int, SliceState>` at **coordinator+232** (`0xe8`),
  keyed by `slice_id` — the per-slice cached topology (insert via
  `PrepareInsertSmallNonSoo((char*)this + 232, ...)` at the
  `FlatHashMapPolicy<int, TopologyCoordinator::SliceState>` call).
- `flat_hash_map<tuple<int,int>, NetworkAddressMapping>` at
  **coordinator+200** (`0xc8`), keyed by `(slice_id, host_id)` — the
  per-host address mapping.

Validation is strict: a mismatch returns an error `Status` rather than
silently dropping. Pseudocode reconstruction:

```cpp
int slice = req.address_mapping.slice_id;   // sub-message +40
int host  = req.address_mapping.host_id;    // sub-message +44

// SliceId bounds check (line 183):
if (slice < 0 || slice >= num_slices_)
  return MakeError<INVALID_ARGUMENT/*3*/>(
      "SliceId out of bounds. Expected num slices: $0. Request: $1");

// If this slice already registered, compare topology_args:
if (auto* slot = slice_state_.find(slice)) {
  MessageDifferencer diff; diff.set_message_field_comparison(EQUIVALENT);
  if (!diff.Compare(args_from(req.tpu_topology_args), slot->args))
    return MakeError<INVALID_ARGUMENT/*3*/>(            // line 202
        "Received topology that differs from previously registered "
        "topology at same sliceID. SliceID: $0 Previous HostId: $1 "
        "New HostId: $2 Addresses: $3 Diff: $4");

  // ... and compare the per-host NetworkAddressMapping:
  if (!diff.Compare(req.address_mapping, addr_map_[{slice,host}]))
    return MakeError<INVALID_ARGUMENT/*3*/>(            // line 216
        "Received host address mapping that differs from previous "
        "mapping SliceID: $0 HostId: $1 Prev Address: $2 "
        "New Addresses: $3");

  // ... and compare incarnation_id:
  if (req.incarnation_id != slot->incarnation[host])
    return MakeError<INVALID_ARGUMENT/*3*/>(            // line 226
        "Received incarnation ID that is different from previous "
        "incarnation ID. SliceID: $0 HostId: $1 "
        "Prev IncarnationId: $2 New IncarnationId: $3");
}
// HostId bounds check (lines 235 / 250) also returns INVALID_ARGUMENT.
```

> **CONFIRMED** — every mismatch path ends in `MakeErrorImpl<3>`
> (INVALID_ARGUMENT), not a log-and-drop. Source lines in
> `topology_coordinator.cc`: SliceId-OOB 183, topology-diff 202,
> address-mapping-diff 216, incarnation-diff 226, HostId-OOB 235/250.
> The `incarnation_id` mismatch → `MakeErrorImpl<3>` matches the
> documented INVALID_ARGUMENT canon.

The `substitute_internal::SubstituteAndAppend` format strings live at
rodata `0x9b27486` (topology drift), `0x9c14204` (address mapping
drift) and `0x9c14456` (incarnation drift).

## What the request does NOT carry

- **No magic / sanity field.** The protobuf schema validation is
  trusted — there's no equivalent of libnccom's `0x61796c69` "ilya"
  sanity field. A malformed inbound message is rejected by
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

## Cross-References

- [Bootstrap Overview](overview.md) — where worker registration sits in the rendezvous sequence and the gRPC server machinery behind it.
- [Topology Exchange](topology-exchange.md) — the `TopologyCoordinator::ProcessRequest` path that consumes the `GetMultiSliceTopologyRequest` this page documents.
- [Coordinator Election](coordinator-election.md) — how the single coordinator process every worker registers against is selected.
