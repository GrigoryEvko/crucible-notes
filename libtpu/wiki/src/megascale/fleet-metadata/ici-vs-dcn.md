# ICI vs DCN Representation

The fleet metadata spans two distinct, non-overlapping coordinate
systems. The **ICI / in-slice** system names chips inside one slice; the
**DCN / cross-slice** system names hosts across the fleet. They share no
coordinate — the only bridge is the per-slice shape blob.

## Side by side

| Aspect | ICI / in-slice | DCN / cross-slice |
|--------|----------------|-------------------|
| Package | `superpod.routing.proto` + `tpu.*` | `xla.megascale.runtime.*` |
| Owned/configured by | tpunetd | Megascale runtime coordinator |
| Chip / node name | `ChipCoordinate {coordinates: int64[]}` / `asic_sw.proto.ChipLocation` (physical) | `(slice_id, host_id)` host name; chip via per-slice `logical_device_id` |
| Topology shape | `ToroidalTopology` (3D/6D torus, wrap, twist, `TwistedTorusShape`, `Faults`) | flat list of `SliceInfo` + `NetworkAddressMapping` + `DCNTopology` reduction tree |
| Link model | `IciLink {src, dest, Direction}`; `faulty_links` | `NetworkAddressMapping` endpoints (DCN NICs) |
| Coordinate vector | `repeated int64` (variable rank) | scalar `(slice_id, host_id)` ints |
| Fault representation | `ToroidalTopology.Faults {faulty_vertices, faulty_links}`; `TpuDegradedAxesProto` | none in inventory; faults surface only in RapidEye `FaultyNetworkLink` |
| Transport | ICI links (on-chip fabric) | gRPC over TCP (DCN) |
| Bridge | the slice's bounding-box shape → `TpuTopologyArgsProto` | embedded as `SliceInfo.tpu_topology_args` |

## The ICI layer (in-slice)

Inside one slice, chips form a toroidal network. The shape proto:

```protobuf
message ToroidalTopology {
  int64 x_dimension = 1;  int64 y_dimension = 2;  int64 z_dimension = 3;
  int64 a_dimension = 9;  int64 b_dimension = 10; int64 c_dimension = 11;  // 6D superpod
  bool  x_wrapping  = 4;  bool  y_wrapping  = 5;  bool  z_wrapping  = 6;
  bool  a_wrapping  = 12; bool  b_wrapping  = 13; bool  c_wrapping  = 14;
  TwistedTorusShape twisted_torus_shape = 7;
  Faults            faults              = 8;
  message IciLink { ChipCoordinate src = 1; ChipCoordinate dest = 2; Direction direction = 3; }
  message Faults  { repeated ChipCoordinate faulty_vertices = 1; repeated IciLink faulty_links = 2; }
}
enum TwistedTorusShape {
  TWIST_SHAPE_UNSPECIFIED=0; TWIST_SHAPE_K_K_2K=1;
  TWIST_SHAPE_K_2K_2K=2; TWIST_SHAPE_K_2K_NK=3;
}
```

A chip is a `ChipCoordinate {repeated int64 coordinates}` — a
variable-rank vector — paired with a physical `asic_sw.proto.ChipLocation`
through:

```protobuf
message ChipLocationToCoordinate { asic_sw.proto.ChipLocation location = 1; ChipCoordinate coordinate = 2; }
message ChipCoordinateMapping    { repeated ChipLocationToCoordinate chip_mapping = 1; }
```

tpunetd establishes these coordinates with `SetGlobalChipId`,
`GetChipCoordinates`, and `SetChipCoordinates` during the in-slice
bringup. See the [tpunetd relationship](../bootstrap/tpunetd-relationship.md)
for the in-slice fabric handoff.

## The DCN layer (cross-slice)

The cross-slice layer drops all of that structure. A slice is just an
index plus a shape; a host is just `(slice_id, host_id)` plus a NIC
list. The inventory is two flat lists:

- `repeated SliceInfo slice_info` — the slices and their shapes,
- `repeated NetworkAddressMapping address_mappings` — the host endpoints.

Plus a separate reduction plan:

```protobuf
message DCNTopology {                       // oneof "representation"
  oneof representation {
    SymmetricTree symmetric_tree = 1;       // {repeated int32 branching_per_layer}
    TreeNode      tree           = 2;       // explicit tree of slice ranges
  }
}
```

`DCNTopology` is the *plan* for how slices reduce together (tree or
ring), not the *inventory* of what slices exist. It is passed into
`MegaScaleMultiSliceConfig::Create`, `Communicator::Create`, and the
`HostCommandSchedulerFactory` alongside the
`MultiSliceTopologyAndLocation`.

## The bridge: the per-slice shape

The single datum that crosses from ICI to DCN is the slice's bounding
shape, encoded as a [`TpuTopologyArgsProto`](slice-shape.md) and embedded
in every `SliceInfo.tpu_topology_args`. The DCN layer reads
`product(host_bounds)` to get the per-slice host count and validates the
shape for equality across hosts — but it never reinterprets the shape
back into chip coordinates. The ICI coordinate space stays entirely
inside the slice.

## Why the split

- **Separation of concerns.** tpunetd owns chip routing and fault
  handling at the ICI level; the Megascale coordinator owns host
  rendezvous and DCN reachability. Neither needs the other's coordinate
  space.
- **Scale.** The cross-slice metadata is O(slices + hosts), not
  O(chips). A pod with thousands of chips per slice still has a small
  DCN inventory.
- **Fault locality.** ICI faults (`faulty_vertices` / `faulty_links`)
  stay in the slice; only host-granular DCN faults
  (`FaultyNetworkLink {src_worker, dst_worker}`) surface to the
  cross-host [error aggregator](barrier-error-usage.md).

## Cross-References

- [Topology Model](topology-model.md) — the in-slice ICI coordinate space this page contrasts with DCN
- [Slice Shape](slice-shape.md) — the opaque per-slice blob that crosses the ICI↔DCN boundary
- [Barrier / Error Usage](barrier-error-usage.md) — where host-granular DCN faults surface
- [Error Aggregator](../error-aggregator.md) — the fleet-wide classifier those DCN faults feed
- [ICI › Overview](../../ici/overview.md) — the in-slice optical torus tier
- [Bootstrap › ICI Handoff](../bootstrap/ici-handoff.md) — the ICI-up → DCN-rendezvous handoff
