# Bootstrap-Exchanged Metadata

At job startup each host publishes its own identity and endpoints to the
coordinator, and receives back the assembled fleet view. This page
covers *what metadata travels* during that exchange; the rendezvous
machinery itself (coordinator election, the `Coordinator<>` template,
convergence wait) is documented under
[Bootstrap Rendezvous](../bootstrap/overview.md).

## What a host publishes

Each worker calls
`DiscoverTopologyAndAddressBindings(local_slice_id, TpuTopologyArgsProto
args, local_host_id, num_slices)` and builds a single request:

```protobuf
message GetMultiSliceTopologyRequest {
  NetworkAddressMapping    address_mapping   = 1;  // THIS host's identity + endpoints
  tpu.TpuTopologyArgsProto tpu_topology_args = 2;  // THIS slice's shape
  int64                    incarnation_id    = 3;  // THIS process's generation
}
```

The `address_mapping` carries the host's own identity and reachability:

```text
NetworkAddressMapping {
  slice_id  = local_slice_id,
  host_id   = local_host_id,
  addresses = [ HostNetworkAddress {
                 address, interface_name, numa_node,
                 host_name_for_debugging } per DCN NIC ]
}
```

So one host publishes exactly four facts: its `(slice_id, host_id)`, its
DCN endpoints, its slice shape, and its incarnation. Nothing about other
hosts or chips.

> **Note on field placement.** Earlier reconstruction estimated a
> 5-field request with explicit top-level `slice_id` / `host_id` /
> `host_addresses` / `topology_args` / `incarnation_id`. The
> descriptor-pool ground truth folds `slice_id` + `host_id` *inside*
> `address_mapping`, leaving the request with three fields.

## What the coordinator assembles

The coordinator's `TopologyCoordinator` accumulates one request per
`(slice_id, host_id)`, validating that all hosts in a slice report an
equivalent `tpu_topology_args`. When all
`num_slices · hosts_per_slice` registrations arrive it assembles:

```protobuf
message MultiSliceTopologyInfo {
  repeated SliceInfo             slice_info       = 1;  // every slice + shape
  repeated NetworkAddressMapping address_mappings = 2;  // every host's endpoints
  int64                          incarnation_id   = 3;
}
```

This pairs the slice inventory (field 1) with the full endpoint table
(field 2). The coordinator serializes it to bytes and returns:

```protobuf
message GetMultiSliceTopologyResponse {
  bytes serialized_topology_info = 1;   // serialized MultiSliceTopologyInfo
}
```

The response is a single opaque `bytes` blob — not a structured message.
This keeps the response byte-stable (the coordinator sorts the slice and
endpoint lists before serializing, so identical inputs produce identical
bytes) which the [re-key detector](../bootstrap/topology-exchange.md)
relies on.

## What a host installs

On receiving the response each worker:

1. Parses `serialized_topology_info` back into a `MultiSliceTopologyInfo`.
2. Rebuilds its `MultiSliceTopologyAndLocation` from the `slice_info`
   (via `FromProto` on a `MultiSliceTopologyAndLocationProto`), stamping
   in its own `local_slice_id` / `local_host_id`.
3. Turns `address_mappings` into an `EndpointAddresses` and installs the
   endpoint map into the `Communicator`
   (`flat_hash_map<tuple<int,int>, NetworkAddressMapping>`).

At that point steady-state Send/Recv collectives become possible: every
host knows every other host's `(slice_id, host_id)` and the DCN NICs to
reach it.

## Summary of the data flow

```text
PUBLISH (per host)                RECEIVE (whole fleet)
──────────────────                ─────────────────────
(slice_id, host_id)        ──►    repeated SliceInfo         (all slices + shapes)
DCN endpoints              ──►    repeated NetworkAddressMapping (all host endpoints)
tpu_topology_args (shape)         incarnation_id
incarnation_id
```

A host announces a single row; it receives the entire table.

## What is NOT exchanged at bootstrap

- **Chip coordinates.** The ICI `ChipCoordinate` space stays in the
  slice; only the slice's bounding shape travels. See
  [ICI vs DCN](ici-vs-dcn.md).
- **The DCNTopology reduction tree.** That plan is computed/configured
  separately and handed into the Communicator, not gathered from hosts.
- **Per-chip physical handles.** A chip is named physically
  (`chip_id`, `core_idx`, `physical_location`) only in the
  [error aggregator](barrier-error-usage.md)'s diagnostic output, never
  in the bootstrap inventory.
