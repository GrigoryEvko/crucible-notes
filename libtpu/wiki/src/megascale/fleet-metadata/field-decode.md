# Field-by-Field Decode

The complete wire schema of the Megascale fleet metadata, recovered from
the `protodesc_cold` FileDescriptorProto pool inside `libtpu.so`. All
messages are package `xla.megascale.runtime` unless noted; the
topology-args family is package `tpu`.

## SliceInfo

`platforms/xla/megascale/common/runtime.proto` — the per-slice fleet
entry.

```
message SliceInfo {
  int32                    slice_id          = 1;   // fleet-wide slice index
  // field 2 retired (gap)
  tpu.TpuTopologyArgsProto tpu_topology_args = 3;   // slice 3D shape
}
```

This is the canonical `SliceInfo`: slice index plus shape, with host
endpoints held separately in the `NetworkAddressMapping` list.

## MultiSliceTopologyAndLocationProto

`runtime.proto` — the serialized FLEET object (the wire form of the
`MultiSliceTopologyAndLocation` class).

```
message MultiSliceTopologyAndLocationProto {
  int32              local_slice_id = 1;   // THIS process's slice
  int32              local_host_id  = 2;   // THIS process's host (in slice)
  repeated SliceInfo slice_info     = 3;   // every slice in the fleet
  int64              incarnation_id = 4;   // fleet generation token
}
```

Accepted by `Communicator::Create`, `MegaScaleMultiSliceConfig::Create`,
and `AotMegaScaleMultiSliceConfig::Create`. The `local_*` fields make it
self-locating.

## MultiSliceTopologyInfo

`platforms/xla/megascale/runtime/communication/transport.proto` — the
wire payload the coordinator serializes into the bootstrap response.

```
message MultiSliceTopologyInfo {
  repeated SliceInfo             slice_info       = 1;   // all slices
  repeated NetworkAddressMapping address_mappings = 2;   // all host endpoints
  int64                          incarnation_id   = 3;
}
```

Pairs the slice inventory with the full endpoint table. Serialized into
`GetMultiSliceTopologyResponse.serialized_topology_info`.

## NetworkAddressMapping / HostNetworkAddress / EndpointAddresses

`third_party/tensorflow/compiler/xla/megascale/addresses.proto` — the
DCN reachability layer.

```
message HostNetworkAddress {
  string address                 = 1;   // ip:port / host:port of a NIC
  string interface_name          = 2;   // NIC name (DCN port)
  int32  numa_node               = 4;   // NUMA affinity of this NIC
  string host_name_for_debugging = 3;   // human-readable host label
}
message NetworkAddressMapping {
  int32  slice_id                = 1;   // owning slice
  int32  host_id                 = 2;   // host within slice
  repeated HostNetworkAddress addresses = 3;   // one per DCN interface
}
message EndpointAddresses {
  repeated NetworkAddressMapping address_mappings = 1;   // whole fleet
}
```

`(slice_id, host_id)` is the universal host key. Note the
non-contiguous field numbers in `HostNetworkAddress` (1, 2, 4, 3) — a
retired field left a gap.

## GetMultiSliceTopologyRequest / Response

`transport.proto` — the bootstrap RPC payloads.

```
message GetMultiSliceTopologyRequest {
  NetworkAddressMapping    address_mapping   = 1;   // THIS host's identity + endpoints
  tpu.TpuTopologyArgsProto tpu_topology_args = 2;   // THIS slice's shape
  int64                    incarnation_id    = 3;   // THIS process's generation
}
message GetMultiSliceTopologyResponse {
  bytes serialized_topology_info = 1;   // serialized MultiSliceTopologyInfo
}
```

The request's `slice_id` / `host_id` live *inside* `address_mapping`;
the response is a single opaque `bytes` blob.

## TpuTopologyArgsProto

`learning/45eac/tpu/runtime/topology/tpu_topology_args.proto` (package
`tpu`, editions) — the slice-shape descriptor. Full decode on the
[Slice Shape](slice-shape.md) page.

```
message TpuTopologyArgsProto {
  tpu.TpuVersionProto      version                  = 1;
  string                   variant                  = 2;
  tpu.TpuPlatformTypeProto platform_type            = 3;
  string                   chip_config_name         = 4;
  tpu.TpuDimensionsProto   chips_per_host_bounds    = 5;
  tpu.TpuDimensionsProto   host_bounds              = 6;
  tpu.TpuWrapProto         wrap                     = 7;
  bool                     twist                    = 8;
  bool                     enhanced_barrier_enabled = 9;
  bool                     use_continuations        = 11;
  tpu.TpuRoutingStrategyProto routing_strategy       = 12;
  SubSlice                 sub_slice                = 10;
  message SubSlice {
    tpu.TpuDimensionsProto chips_per_host_bounds    = 1;
    tpu.TpuDimensionsProto host_bounds              = 2;
  }
}
```

## DCNTopology

`third_party/tensorflow/compiler/xla/megascale/dcn_topology.proto` — the
cross-slice reduction plan.

```
message DCNTopology {
  oneof representation {                       // "representation"
    SymmetricTree symmetric_tree = 1;
    TreeNode      tree           = 2;
  }
  message SymmetricTree {
    repeated int32 branching_per_layer = 1;    // balanced tree fan-out per layer
  }
  message TreeNode {
    string            label             = 1;   // optional node label
    repeated TreeNode nodes             = 2;   // children
    repeated SliceRange slice_ranges    = 3;   // leaf slice ranges
    float             egress_constraint = 4;   // per-node egress cap
    bool              ring_transfers    = 5;   // ring vs tree at this node
    message SliceRange { int32 slice_id_start = 1; int32 slice_id_end = 2; }
  }
}
```

`SymmetricTree` is the compact form; `TreeNode` the explicit form whose
leaves name contiguous `[slice_id_start, slice_id_end]` ranges.

## TpuCoreLocationProto

`platforms/xla/megascale/runtime/common/core_location.proto` — the
per-core handle (coupled with its slice by the
`MultiSliceTpuCoreLocation` class).

```
message TpuCoreLocationProto {
  tpu.TpuCoreTypeProto core_type         = 1;   // TENSOR / BARNA / SPARSE
  int32                logical_device_id = 2;   // device id within its slice
}
enum tpu.TpuCoreTypeProto {
  TPU_CORE_TYPE_INVALID=0; TPU_CORE_TYPE_TENSOR_CORE=1;
  TPU_CORE_TYPE_BARNA_CORE=2; TPU_CORE_TYPE_SPARSE_CORE=3;
}
```

## PerSliceTpuDimensionsProto / Topology

`runtime.proto` — a lightweight per-slice (x,y,z) view used where the
full shape is unnecessary (e.g. the `MEGASCALE_TOPOLOGY` override path).

```
message PerSliceTpuDimensionsProto {
  int32 slice_id = 1; int32 x = 2; int32 y = 3; int32 z = 4;
}
message Topology { repeated PerSliceTpuDimensionsProto slices = 1; }
```

## Supporting dimension and enum types

```
message TpuDimensionsProto   { int32 x=1; int32 y=2; int32 z=3; int32 w=4; }
message TpuWrapProto         { bool  x=1; bool  y=2; bool  z=3; }
message TpuDegradedAxesProto { bool  x=1; bool  y=2; bool  z=3; }

enum TpuRoutingStrategyProto { ROUTING_DEFAULT=0; ROUTING_MESH=1; ROUTING_NHOP=2; }
enum TpuVersionProto {
  TPU_VERSION_INVALID=0; TPU_VERSION_JELLYFISH=1; TPU_VERSION_DRAGONFISH=2;
  TPU_VERSION_PUFFERFISH=3; TPU_VERSION_VIPERFISH=4; TPU_VERSION_GHOSTLITE=5;
  TPU_VERSION_6acc60406=6;
}
enum TpuPlatformTypeProto {
  TPU_PLATFORM_TYPE_INVALID=0; TPU_PLATFORM_TYPE_HARDWARE=1;
  TPU_PLATFORM_TYPE_GRM=2; TPU_PLATFORM_TYPE_ISS=3;
}
message TpuConfiguredPropertiesProto {
  tpu.TpuDegradedAxesProto    degraded_axes           = 1;
  bool                        is_nhop_source_relative = 2;
  tpu.TpuRoutingStrategyProto routing_strategy        = 3;
}
```

## Runtime options and collective slack (auxiliary)

`runtime.proto` carries two more fleet-scoped messages:

```
message MegascaleRuntimeOptions {                 // all fields optional (oneof presence)
  bool abort_on_errors      = 1;
  bool abort_on_hangs       = 2;
  bool debug_dump_on_error  = 3;
}
message CollectiveSlackInformation {
  repeated BudgetInfo     default_budgets         = 1;
  repeated CollectiveInfo collectives             = 2;
  CollectiveInfo          default_collective_info = 3;
  message BudgetInfo {
    repeated google.protobuf.Duration transmission_budget_per_level = 1;
    google.protobuf.Duration          safety_margin                 = 2;
    bool                              add_random_delay              = 3;
    repeated int32                    levels_with_delay             = 4;
    repeated google.protobuf.Duration explicit_delays               = 5;
    int64                             min_size_for_delay            = 6;
  }
  message CollectiveInfo {
    string                   key                  = 1;
    google.protobuf.Duration slack                = 2;
    oneof budget {
      BudgetInfo             transmission_budget   = 3;
      int32                  default_budget_index  = 4;
    }
  }
}
```

`MegascaleRuntimeOptions` mirrors the `--megascale_*_abort_on_*` flags;
`CollectiveSlackInformation` tunes per-collective DCN transmission
budgets (per-tree-level durations) and is passed alongside the fleet
view into the `HostCommandSchedulerFactory`.

## Verified presence in libtpu.so

| Type / string | rg -a count |
|---------------|-------------|
| `MultiSliceTopologyAndLocationProto` (string) | 37 |
| `MultiSliceTopologyAndLocation` (class symbols) | 11 |
| `NetworkAddressMapping` | 102 |
| `HostNetworkAddress` | 58 |
| `EndpointAddresses` | 136 |
| `TpuTopologyArgsProto` | 82 |
| `PerSliceTpuDimensionsProto` | 19 |
| `chips_per_host_bounds` | 25 |
| `host_bounds` | 21 |
| `chip_config_name` | 13 |
| `serialized_topology_info` | 1 |
| `SetGlobalChipId` | 161 |
| `GetChipCoordinates` | 107 |
| `ChipLocationToCoordinate` | 32 |
| `global_core_id` / `global_chip_id` | 8 / 6 |
| `GetSliceLocalDeviceId` / `ToSliceAndLogicalDeviceId` | 2 / 1 |
