# Per-Host and Per-Slice Identity

A host announces a 3-tuple plus a per-slice shape blob when it joins the
fleet. Everything in the cross-slice layer keys off the first two
components.

## The identity tuple

| Component | Type | Scope | Meaning |
|-----------|------|-------|---------|
| `slice_id` | int32 | fleet | which slice this host belongs to |
| `host_id` | int32 | slice | host index within the slice |
| `incarnation_id` | int64 | process | per-process generation token |
| `tpu_topology_args` | `tpu.TpuTopologyArgsProto` | slice | the slice's 3D shape (must match across hosts) |

`(slice_id, host_id)` is the **universal host key**. It appears as:

- fields 1 and 2 of [`NetworkAddressMapping`](field-decode.md) (the
  endpoint table — `slice_id` field 1 int32, `host_id` field 2 int32),
- fields 2 and 3 of the [barrier](barrier-error-usage.md)'s
  `BarrierRequest` (`slice_id` field 2 int32, `host_id` field 3 int32),
  which the coordinator uses to track arrivals against
  `num_participants` (field 4),
- the [error aggregator](barrier-error-usage.md)'s per-worker key,
  carried in a `MegascaleErrorAggregator::WorkerAndCoreInfo` struct,
- the **Communicator's endpoint map key**
  `flat_hash_map<tuple<int,int>, NetworkAddressMapping>` (confirmed in
  the `Communicator::Communicator` constructor signature @0x1cca9700).

There is no separate "node id" or "rank" field. A flat rank, where one
is needed, is derived by flattening `(slice_id, host_id)` against the
known per-slice host counts; the metadata itself stores only the pair.

## Where the identity comes from

- `slice_id` is the process's `MEGASCALE_SLICE_ID`
  (`--megascale_slice_id`), passed into
  `DiscoverTopologyAndAddressBindings`.
- `host_id` is the host's index within the slice, computed during the
  in-slice tpunetd bringup.
- `incarnation_id` is minted per process via `util::random::NewGlobalID()`.
- `tpu_topology_args` is the slice shape computed during tpunetd's
  in-slice fabric setup (see [ICI vs DCN](ici-vs-dcn.md)).

## The self-locating proto

When the assembled fleet view is serialized as
`MultiSliceTopologyAndLocationProto`, it embeds the *receiving*
process's own identity in `local_slice_id` (field 1) and
`local_host_id` (field 2). A receiver therefore knows which `SliceInfo`
in the list is its own slice without any extra context — useful when the
same serialized blob is broadcast to every host.

## incarnation_id and restart detection

`incarnation_id` is the generation token that lets the coordinator
detect a worker restart or a topology re-key. It is present at three
layers:

- `GetMultiSliceTopologyRequest.incarnation_id` (field 3) — what a host
  publishes,
- `MultiSliceTopologyInfo.incarnation_id` (field 3) — the assembled view,
- `MultiSliceTopologyAndLocationProto.incarnation_id` (field 4) — the
  serialized fleet object.

The re-key detector — the anonymous-namespace helper
`LogUniqueIds(int, int, MultiSliceTopologyAndLocation const&)`, inlined
into `Communicator::Create` — caches the
`(slice_id, host_id, incarnation_id)` triple of the last registration in
a static `last_ids[3]` array behind `unique_id_mutex`, and re-logs the
communicator instance whenever the triple changes (the
`communication_backend.cc` "Created communicator." log). This is the
signal an operator reads when asking "why did the fleet's address table
change at time T". See the
[bootstrap](../bootstrap/topology-exchange.md) documentation for the
re-key detail.

## Per-slice consistency

The `tpu_topology_args` blob is per-slice, not per-host: every host in a
slice must report an equivalent one. The coordinator validates this with
`proto2::util::MessageDifferencer::Compare`; a slice whose hosts report
mismatched shapes (e.g. one v4 and one v5 chip generation) is rejected
and the diff is logged. This is the schema-compatibility gate described
under [Slice Shape](slice-shape.md#versioning).
