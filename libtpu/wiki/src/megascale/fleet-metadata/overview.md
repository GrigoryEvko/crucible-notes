# Fleet Metadata Schema — Overview

The Megascale **fleet metadata schema** is the data model that names and
describes a multi-slice TPU fleet. It sits *above* the per-slice ICI
fabric (the toroidal chip network configured by tpunetd) and *below* the
XLA collectives layer. It answers four questions for the runtime:

1. **What is in the fleet?** — how many slices, how many hosts per
   slice, what shape each slice is.
2. **Who am I?** — this process's `(slice_id, host_id)` and its
   `incarnation_id`.
3. **How do I reach a peer?** — the per-host DCN endpoint table.
4. **How is a chip named fleet-wide?** — the translation from a flat
   device id to a `(slice, logical-device, core-type)` address.

## The two coordinate systems

The schema deliberately keeps two coordinate systems apart and joins
them at exactly one point.

| Layer | Package | Owner | Names a chip by |
|-------|---------|-------|-----------------|
| **DCN / cross-slice** | `xla.megascale.runtime.*` | Megascale coordinator | `(slice_id, host_id)` + per-slice logical device id |
| **ICI / in-slice** | `superpod.routing.*` + `tpu.*` | tpunetd | `ChipCoordinate` (int64 vector) in a `ToroidalTopology` |

The only datum that crosses from the ICI layer to the DCN layer is the
per-slice shape, encoded as a [`TpuTopologyArgsProto`](slice-shape.md)
and carried inside every `SliceInfo`. The DCN layer treats that shape as
an opaque, validated blob — it never re-derives chip coordinates.

## The authoritative object

The live, in-memory fleet model is a C++ class:

```
xla::megascale::runtime::MultiSliceTopologyAndLocation
```

Note the name has **no** `Proto` suffix. Its serialized form is
`MultiSliceTopologyAndLocationProto`. Everything that needs the fleet
view — the `Communicator`, `MegaScaleMultiSliceConfig`,
`PjrtExecutableContext`, the compiler scheduler, and the cross-host
barrier — holds
this class by `const&` or `shared_ptr<const ...>`.

It is built two ways:

- `FromProto(MultiSliceTopologyAndLocationProto)` — deserialize what the
  coordinator broadcast at bootstrap.
- ctor `(local_slice_id, local_host_id, …, incarnation_id)` followed by
  `AddSlice(slice_id, shared_ptr<const tpu::TpuTopology>)` — incremental
  assembly.

and serialized back with `ToProto()`.

## Where the pieces live

| Concern | Page |
|---------|------|
| fleet → slice → host → chip → core levels | [Topology Model](topology-model.md) |
| `(slice_id, host_id, incarnation_id)` identity | [Host Identity](host-identity.md) |
| flat device id → `(slice, device, core)` | [Global Addressing](global-addressing.md) |
| the two coordinate systems compared | [ICI vs DCN](ici-vs-dcn.md) |
| the 3D-torus slice descriptor | [Slice Shape](slice-shape.md) |
| what each host publishes at startup | [Bootstrap Exchange](bootstrap-exchange.md) |
| how the barrier / error aggregator use it | [Barrier & Error Usage](barrier-error-usage.md) |
| message-by-message wire decode | [Field Decode](field-decode.md) |

## Provenance

The schema is recovered from the `protodesc_cold` FileDescriptorProto
pool inside `libtpu.so` (libtpu-0.0.40), cross-checked against linked
C++ class symbols, accessor method names, and rodata type strings. The
fleet metadata is spread across four cross-slice proto files
(`transport.proto`, `common/runtime.proto`, `megascale/addresses.proto`,
`megascale/dcn_topology.proto`) plus the per-slice
`tpu_topology_args.proto`.
