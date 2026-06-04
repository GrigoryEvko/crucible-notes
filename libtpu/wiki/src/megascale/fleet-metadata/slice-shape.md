# Slice-Shape Descriptor

A slice's shape — its 3D (occasionally 4D) torus of chips and how they
distribute across hosts — is described by `tpu.TpuTopologyArgsProto`.
The same proto is embedded by every Megascale-runtime message that needs
the per-slice shape: it is field 3 of `SliceInfo`
([field decode](field-decode.md)), field 2 of
`xla.megascale.runtime.NetworkAddressMapping`, and field 6 of
`TargetArgumentsProto`; the runtime also carries it inside
`TPUHostConfiguration` (field 8) and `TpuPjRtTopologyDescriptionProto`
(field 1). The slice shape is computed once during in-slice bringup and
propagated up as this single, validated blob.

## TpuTopologyArgsProto

```
message TpuTopologyArgsProto {              // package tpu, editions
  tpu.TpuVersionProto      version                = 1;  // chip generation
  string                   variant                = 2;  // chip variant name
  tpu.TpuPlatformTypeProto platform_type          = 3;  // HW / GRM / ISS
  string                   chip_config_name       = 4;  // tpunetd chip config
  tpu.TpuDimensionsProto   chips_per_host_bounds  = 5;  // chips per host (x,y,z,w)
  tpu.TpuDimensionsProto   host_bounds            = 6;  // hosts per slice (x,y,z,w)
  tpu.TpuWrapProto         wrap                   = 7;  // per-axis torus closure
  bool                     twist                  = 8;  // twisted-torus
  bool                     enhanced_barrier_enabled = 9; // per-slice capability
  bool                     use_continuations      = 11;
  tpu.TpuRoutingStrategyProto routing_strategy     = 12; // DEFAULT / MESH / NHOP
  SubSlice                 sub_slice              = 10;  // sub-slice override
  message SubSlice {
    tpu.TpuDimensionsProto chips_per_host_bounds  = 1;
    tpu.TpuDimensionsProto host_bounds            = 2;
  }
}
```

## Reading the shape

The slice shape is the product of two 4D bounds:

```
slice_shape = chips_per_host_bounds ⊗ host_bounds
```

with each bound a `TpuDimensionsProto {x, y, z, w}` (the `w` axis is
usually unused). The two quantities the rest of the system needs:

- **chips per host** = `product(chips_per_host_bounds)`
- **hosts per slice** = `product(host_bounds)` — this is what the DCN
  layer uses as the slice's host count and what the coordinator uses to
  decide when a slice has fully registered.

Supporting dimension types:

```
message TpuDimensionsProto   { int32 x=1; int32 y=2; int32 z=3; int32 w=4; }
message TpuWrapProto         { bool  x=1; bool  y=2; bool  z=3; }   // torus closure per axis
message TpuDegradedAxesProto { bool  x=1; bool  y=2; bool  z=3; }   // axes lost to faults
```

`wrap` marks which axes close into a torus (vs an open mesh); `twist`
(a bool, field 8) selects a twisted torus. The concrete twist variants
live in a separate `superpod.routing.proto.TwistedTorusShape` enum
(`TWIST_SHAPE_UNSPECIFIED=0`, `TWIST_SHAPE_K_K_2K=1`,
`TWIST_SHAPE_K_2K_2K=2`, `TWIST_SHAPE_K_2K_NK=3`).

## Routing and configured properties

`routing_strategy` selects how traffic is routed within the slice:

```
enum TpuRoutingStrategyProto { ROUTING_DEFAULT=0; ROUTING_MESH=1; ROUTING_NHOP=2; }
```

A companion proto carries the *configured* (post-fault) properties:

```
message TpuConfiguredPropertiesProto {
  tpu.TpuDegradedAxesProto    degraded_axes           = 1;  // axes degraded by faults
  bool                        is_nhop_source_relative = 2;
  tpu.TpuRoutingStrategyProto routing_strategy        = 3;
}
```

`degraded_axes` is how a partially-faulted slice advertises which axes
have lost wrap/connectivity — the ICI-layer fault model summarized for
the routing planner.

## Chip generation and platform

```
enum TpuVersionProto {
  TPU_VERSION_INVALID=0; TPU_VERSION_JELLYFISH=1; TPU_VERSION_DRAGONFISH=2;
  TPU_VERSION_PUFFERFISH=3; TPU_VERSION_VIPERFISH=4; TPU_VERSION_GHOSTLITE=5;
  TPU_VERSION_6acc60406=6;
}
enum TpuPlatformTypeProto {
  TPU_PLATFORM_TYPE_INVALID=0; TPU_PLATFORM_TYPE_HARDWARE=1;
  TPU_PLATFORM_TYPE_GRM=2; TPU_PLATFORM_TYPE_ISS=3;
}
```

`version` is the silicon generation; `platform_type` distinguishes real
hardware from the GRM functional model and the ISS instruction-set
simulator (so a simulated fleet carries the same shape descriptor as a
real one).

## Versioning

The slice shape is also the schema-compatibility gate. Every host in a
slice reports its `TpuTopologyArgsProto`; the coordinator validates that
all hosts in a slice report an *equivalent* one with
`proto2::util::MessageDifferencer::Compare`. A slice whose hosts disagree
— for example mixing two `version` values — is rejected and the diff is
logged. `enhanced_barrier_enabled` (field 9) and `use_continuations`
(field 11) are per-slice capability bits that ride along in the same
blob. See [Host Identity](host-identity.md#per-slice-consistency).

## Provenance

`chip_config_name`, `chips_per_host_bounds`, `host_bounds`, `wrap`, and
`twist` are derived from the in-slice fabric brought up during slice
bringup: the driver's global chip-id assignment
(`asic_sw::driver::deepsea::GlobalConfig::SetGlobalChipId`), tpunetd's
`GetChipCoordinates` RPC, and the routing layer's
`superpod::routing::proto::ChipLocationToCoordinate` map together place
each chip in the slice's `ToroidalTopology`, and the slice shape is the
bounding box of those coordinates. The Megascale layer consumes the
shape as an opaque, validated blob — see [ICI vs DCN](ici-vs-dcn.md).
