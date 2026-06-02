# Global Chip / Core Addressing

There is **no transmitted "global chip id"** in the fleet metadata. A
chip or core is named fleet-wide by *composing* the slice index with an
in-slice logical device id. The flat global identifier exists only at
the compiler/ISA layer, computed from that composition.

## The translation chain

```
flat fleet device_id
     │  MultiSliceTopologyAndLocation::ToSliceAndLogicalDeviceId(
     │       device_id, TpuCoreType)
     ▼
(slice_id, logical_device_id)
     │  MultiSliceTpuCoreLocation::Create(MSTL&, device_id, TpuCoreType)
     ▼
MultiSliceTpuCoreLocation  ──►  TpuCoreLocationProto{core_type, logical_device_id}
                                + owning slice
     │  (compiler / ISA layer)
     │  LloRegionBuilder::ToGlobalCoreId / FromGlobalCoreId
     ▼
flat global_core_id / global_chip_id   (used inside emitted LLO / ISA)
```

## The accessors

The `MultiSliceTopologyAndLocation` class exposes the addressing methods
(all resolved as linked C++ symbols):

| Method | Direction | Purpose |
|--------|-----------|---------|
| `ToSliceAndLogicalDeviceId(int device_id, tpu::TpuCoreType)` | flat → (slice, device) | split a fleet device id into its slice and slice-local device |
| `GetSliceLocalDeviceId(int)` | flat → device | the slice-local id of a fleet device |
| `NumHosts()` | — | total hosts across all slices |
| `NumSlices()` | — | slice count |

The per-core fleet handle is the class `MultiSliceTpuCoreLocation`,
created by:

```
MultiSliceTpuCoreLocation::Create(
    const MultiSliceTopologyAndLocation& mstl,
    int device_id,
    tpu::TpuCoreType core_type)
```

It couples the device's owning slice with a `TpuCoreLocationProto`:

```
message TpuCoreLocationProto {              // xla.megascale.runtime
  tpu.TpuCoreTypeProto core_type         = 1;   // TENSOR / BARNA / SPARSE
  int32                logical_device_id = 2;   // device id within its slice
}
enum tpu.TpuCoreTypeProto {
  TPU_CORE_TYPE_INVALID=0; TPU_CORE_TYPE_TENSOR_CORE=1;
  TPU_CORE_TYPE_BARNA_CORE=2; TPU_CORE_TYPE_SPARSE_CORE=3;
}
```

So a fleet-wide core address is the triple
`(slice_id, logical_device_id, core_type)`.

## Device-assignment resolution

XLA's `DeviceAssignment` (replica × computation → device) is mapped into
the fleet's (slice, device) space by:

- `GetMultiSliceDeviceAssignment(const MultiSliceTopologyAndLocation&,
  DeviceAssignmentProto, optional<int>)` — fleet-wide assignment,
- `GetLocalDeviceAssignmentForSlice(const MultiSliceTopologyAndLocation&,
  DeviceAssignment[, int])` — the slice-local slice of the assignment,
- `GetDeviceIdFromDeviceAssignment(const DeviceAssignment&)` — a single
  device id.

These bind the XLA-level abstract device indices to concrete fleet
positions.

## The flat global_core_id

A *flat* `global_core_id` / `global_chip_id` does exist, but only inside
the compiler and the emitted ISA — never on the bootstrap wire. It is
produced from the (slice, device) pair by the LLO emitter:

- `LloRegionBuilder::GlobalCoreId()` / `ToGlobalCoreId(...)` /
  `FromGlobalCoreId(...)`,
- `xla::tpu::sparse_core::FromGlobalCoreId(...)`,
- `OffloadFactory::GlobalCoreIdToPhysicalChipId(...)`,
- `OffloadFactory::SubsliceToFullSliceGlobalCoreId(...)`.

This is the addressing the kernels themselves use to route SparseCore
collectives and host send/recv; it is recomputed locally from the fleet
metadata, so it never has to be transmitted.

## Why no stored global id

Keeping the global id a *computation* rather than a *field* has two
benefits visible in the schema:

1. The fleet metadata stays small and order-independent — only slice
   indices and host pairs travel on the wire.
2. The flattening rule can differ per chip generation (SparseCore vs
   TensorCore, sub-slice vs full-slice) without changing the wire
   schema; the `core_type` argument selects the rule at the point of
   translation.
