# Fleet Topology Model

The fleet is a five-level hierarchy: **fleet → slice → host → chip →
core**. Each level is owned by a different proto or class, and only the
top four levels cross host boundaries.

## The hierarchy

| Level | Owning proto / class | Key |
|-------|----------------------|-----|
| fleet (multi-slice) | `MultiSliceTopologyAndLocation` (class) / `MultiSliceTopologyAndLocationProto` | `incarnation_id` |
| slice | `SliceInfo {slice_id, tpu_topology_args}` | `slice_id` |
| host | `NetworkAddressMapping {slice_id, host_id, addresses[]}` | `(slice_id, host_id)` |
| host NIC / endpoint | `HostNetworkAddress {address, interface_name, numa_node, host_name_for_debugging}` | — |
| in-slice chip (ICI) | `ChipCoordinate {coordinates: int64[]}` / `asic_sw.proto.ChipLocation` | coordinate |
| core (fleet handle) | `MultiSliceTpuCoreLocation` (class) wrapping `TpuCoreLocationProto{core_type, logical_device_id}` | `(slice_id, logical_device_id, core_type)` |
| core (flat id) | `global_core_id` / `global_chip_id` | computed |

The DCN layer (the top four rows) is what crosses host boundaries. The
ICI layer (the chip rows) lives entirely inside one slice and is
summarized to the DCN layer only as the slice's
[`TpuTopologyArgsProto`](slice-shape.md) shape.

## Fleet level

The fleet is the whole multi-slice job. In memory it is the
`MultiSliceTopologyAndLocation` class; on the wire it is
`MultiSliceTopologyAndLocationProto`:

```
message MultiSliceTopologyAndLocationProto {
  int32              local_slice_id = 1;   // THIS process's slice
  int32              local_host_id  = 2;   // THIS process's host (in slice)
  repeated SliceInfo slice_info     = 3;   // every slice in the fleet
  int64              incarnation_id = 4;   // fleet generation token
}
```

The `local_slice_id` / `local_host_id` fields make the proto
self-locating: a receiver can find its own entry in the `slice_info`
list without out-of-band context.

## Slice level

A slice is a contiguous ICI domain — one toroidal chip network. In the
fleet metadata it is just an index plus a shape:

```
message SliceInfo {
  int32                    slice_id          = 1;
  // field 2 retired (gap)
  tpu.TpuTopologyArgsProto tpu_topology_args = 3;   // slice 3D shape
}
```

Every host in a slice must report an *equivalent* `tpu_topology_args`;
the coordinator rejects a slice whose hosts disagree (it runs
`proto2::util::MessageDifferencer::Compare`). The per-slice host count
the DCN layer needs is `product(tpu_topology_args.host_bounds)`.

## Host level

A host is named `(slice_id, host_id)` and is reachable through one or
more DCN NICs:

```
message NetworkAddressMapping {
  int32  slice_id                = 1;
  int32  host_id                 = 2;
  repeated HostNetworkAddress addresses = 3;   // one per DCN interface
}
```

The `(slice_id, host_id)` pair is the **universal key** of the fleet:
it keys the [endpoint table](field-decode.md), the [barrier](barrier-error-usage.md)
arrival set, the error-aggregator worker key, and the Communicator's
endpoint map `flat_hash_map<tuple<int,int>, NetworkAddressMapping>`.

## Chip and core levels

Below the host, the DCN metadata stops carrying structure: chips are
named only by a per-slice *logical device id*, and the full chip
coordinate lives in the ICI layer (`ChipCoordinate`, a variable-rank
int64 vector inside a `ToroidalTopology`). The fleet-wide *core* handle
is the C++ class `MultiSliceTpuCoreLocation`, which couples a
`TpuCoreLocationProto{core_type, logical_device_id}` with its owning
slice. See [Global Addressing](global-addressing.md) for the full
translation chain and [ICI vs DCN](ici-vs-dcn.md) for the in-slice
coordinate model.

## The cross-slice reduction plan

Distinct from the inventory, `DCNTopology` describes how slices are
arranged into a reduction tree or ring for cross-slice collectives. It
is **not** part of the host inventory — it is computed/configured and
handed into the Communicator alongside the
`MultiSliceTopologyAndLocation`. See [Field Decode](field-decode.md#dcntopology)
for the message and [ICI vs DCN](ici-vs-dcn.md) for where it fits.
