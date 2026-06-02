# TpuChipConfig

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

There are two embedded-proto descriptions of a TPU generation, and they are easy to confuse. `chip_parts` (see [chip_parts.binarypb Decode](chip-parts-binarypb.md)) describes the silicon's *capability geometry* — memory sizes, MXU dimensions, clocks. `TpuChipConfig` describes a runtime *mode*: bounce buffers, sync-flag resources, and on-device transfer windows for a particular `(version, variant)` pair. `TpuChipConfig::Create` resolves an `embed://tpu_chip_config/<version>_chip_configs_<alias>.binarypb` resource, parses it into a `TpuChipConfigProto`, and materializes a `TpuChipConfig` object the driver layer uses to provision per-core queues and DMA buffers.

This page documents that resolution-and-materialization path and, separately, the *consumption* surface that the decoded capability constants reach through the `xla::jellyfish::Target` object. The `Target` is the runtime hardware-abstraction object; `chip_parts` fills its field block at boot, and a family of tiny accessors (`LaneCount`, `SublaneCount`, `ChunksPerTile`, `HbmSizeBytes`, `TensorCoreFrequencyInMegaHertz`, …) read individual fields out of it. Those accessors are the API the cost model, ISA emitter, and tiling passes call; the offset map below is what makes a struct dump from a `Target` instance readable.

For reimplementation, the contract is:

- The **`TpuChipConfig::Create` path**: version+variant → `kChipConfigAliases` lookup → `embed://tpu_chip_config/…` → `ReadBinaryProto` → `FromProto`.
- The **`Target` field block**: which struct offset holds each decoded `chip_parts` constant, and the accessor that reads it.
- The **consumers**: the geometry accessors (`LaneCount`/`SublaneCount`/`ChunksPerTile`/`VexMatrixWidthToSyU32`) and who calls them (cost model, ISA emitter, topology).

| | |
|---|---|
| **Config resolver** | `tpu::TpuChipConfig::Create` @ `0x20AE98E0` |
| **Config parser** | `tpu::TpuChipConfig::FromProto` @ `0x20AEA100` |
| **Source file** | `learning/45eac/tpu/runtime/topology/tpu_chip_config.cc:368` |
| **Alias map** | `kChipConfigAliases` (`gtl::flat_map<TpuVersionAndVariant, …>`, 4 entries) |
| **Geometry descriptor** | `Target+0x3B8` → `tpu::TpuTopology*` |
| **Capability field block** | `Target+0x438..+0x510` (filled by `TpuChipParts::FromProto`) |

---

## TpuChipConfig::Create

### Purpose

`Create` obtains the *mode* configuration for a `(version, variant)` pair: the `TpuChipConfigProto` enumerates `SharedMemoryRegion` records and `SyncFlag` records (parsed by `FromProto`) that the driver uses to lay out bounce buffers and sync-flag resources per core. It is the mode-config sibling of `chip_parts`'s capability-geometry path, and it lives in the same source file (`tpu_chip_config.cc`) one resolver down from `DefaultsForVersion`.

### Algorithm

```c
StatusOr<TpuChipConfig> TpuChipConfig::Create(TpuVersion v, string_view variant,   // sub_20AE98E0
                                              string_view config_variant):
    // 1. Resolve a (version, variant) alias to a config-variant name.
    key = TpuVersionAndVariant{v, variant}
    hit = kChipConfigAliases.find(key)        // 4-entry flat_map of TpuVersionAndVariant
    if hit and hit->value == config_variant:  // bcmp match
        VLOG(1) << "Resolved chip config alias " << variant << "->" << hit->value
        alias = hit->value                    // tpu_chip_config.cc:368
    else:
        alias = variant                       // no alias: use the variant verbatim

    // 2. Build the embed:// resource path and read it.
    name     = AsciiStrToLower(TpuVersionToString(v))
    if variant.non_empty(): StrAppend(&name, "_", variant)
    filename = CatPieces("embed://tpu_chip_config/", name, "_chip_configs_", alias, ".binarypb")
    status   = tsl::ReadBinaryProto(Env::Default(), filename, &proto)   // tpu_chip_config.cc:379-383
    if status != Ok:
        return StatusBuilder(...) << "Failed to obtain chip config \"" << alias
                                  << "\" for version " << v << ", variant \"" << variant << "\""
    return TpuChipConfig::FromProto(proto)    // sub_20AEA100
```

> **QUIRK —** the `embed://` scheme is `tpu_chip_config/` here, but `tpu_chip_parts/` in `DefaultsForVersion`, and the suffix is `_chip_configs_<alias>.binarypb` (note the plural and the alias slot) versus `_chip_parts.binarypb`. The two resolvers target different resource families. `chip_config` blobs are the `*_chip_configs_*.binarypb` entries in the embedded TOC; do not conflate them with the nine `*_chip_parts.binarypb` capability blobs. Unlike `DefaultsForVersion`, `Create` returns a `Status` on failure rather than `CHECK`-failing — a missing config is recoverable, a missing capability description is fatal.

### What FromProto Materializes

`TpuChipConfig::FromProto` (`0x20AEA100`) walks the `TpuChipConfigProto`, iterating its `SharedMemoryRegion` repeated field and a `SyncFlag` repeated field, inserting each into the `TpuChipConfig`'s `flat_hash_map<TpuSharedMemoryOnChip, BounceBuffer>` and sync-flag-resource tables. The `TpuChipConfig` constructor (`0x20AF6300`) takes that map plus three booleans and the per-core layout. The result is the object `TpuPxcDriver::InitializeCores` (`0xE806500`) and `RegisterContinuationQueueConfigs` (`0xE72B340`) consume to provision hardware queues — it is a *resource* object, not the capability `Target`.

---

## The Target Field Block

The capability constants from `chip_parts` do not live in `TpuChipConfig`; they live in the `xla::jellyfish::Target` object, filled at boot by `TpuChipParts::FromProto` (`0x20B1B400`) and `TpuMemoryParts::FromProto` (`0x20B333A0`). Each constant has a fixed struct offset, read by a one-line accessor. The offset map below was read byte-exact from the accessor disassembly (e.g. `HbmSizeBytes` is literally `return *((int64_t*)this + 138)`, i.e. `Target+0x450`).

| Target off | Accessor (VA) | Type | Holds | Confidence |
|---:|---|---|---|---|
| +0x398 | inline (many) | int32 | `tpu_version` (0..5 → JF/DF/PF/VF/GL/Trillium) | CONFIRMED |
| +0x3B8 | `LaneCount`/`SublaneCount`/… (`0x1D60F400`) | `TpuTopology*` | geometry descriptor pointer | CONFIRMED |
| +0x3B8→+0x198 | `LaneCount` (`0x1D60F400`) | int64 | `lane_count` (=128 all gens) | CONFIRMED |
| +0x3B8→+0x1A0 | `SublaneCount` (`0x1D60F300`) | int64 | `sublane_count` (=8 all gens) | CONFIRMED |
| +0x450 | `HbmSizeBytes` (`0x1D615320`) | int64 | HBM size (v7: 102,005,473,280) | CONFIRMED |
| +0x458 | `VmemSizeBytes` (`0x1D615E00`) | int32 | VMEM size (v7: 67,108,864) | CONFIRMED |
| +0x460 | `CmemSizeBytes` (`0x1D615E20`) | int64 | CMEM size (v7: 0) | CONFIRMED |
| +0x468 | `SflagSizeBytes` (`0x1D615E60`) | int32 | SFLAG size (v7: 16,384) | CONFIRMED |
| +0x470 | `SmemSizeBytes` (`0x1D615E40`) | int32 | SMEM size (v7: 1,048,576) | CONFIRMED |
| +0x50C | `VmemWordSizeBytes` (`0x1D617300`) | int32 | VMEM word (v7: 512) | CONFIRMED |
| +0x90C | `TensorCoreFrequencyInMegaHertz` (`0x1D615B60`) | int32 | TC freq MHz (v7: 1900) | CONFIRMED |
| +0x910 | `HbmFrequencyInMegaHertz` (`0x1D615BA0`) | int32 | HBM freq MHz (v7: 7200) | CONFIRMED |

The offsets resolve directly from the accessor bodies: `LaneCount` returns `*(int64_t*)(*((void**)this + 119) + 408)` — `this+119*8 = Target+0x3B8` is the `TpuTopology*`, and `+408 = +0x198` is `lane_count` inside it. `SublaneCount` reads the same descriptor at `+416 = +0x1A0`. `HbmSizeBytes` is `*((int64_t*)this + 138) = Target+0x450`; `VmemSizeBytes` is `*((int32_t*)this + 278) = Target+0x458`; `SmemSizeBytes` is `+284 = +0x470`; `SflagSizeBytes` is `+282 = +0x468`; `VmemWordSizeBytes` is `+323 = +0x50C`; `TensorCoreFrequencyInMegaHertz` is `+579 = +0x90C`; `HbmFrequencyInMegaHertz` is `+580 = +0x910`. See [TpuTopology Struct](tpu-topology-struct.md) for the `+0x3B8` descriptor's full layout.

> **NOTE —** `tpu_version` at `Target+0x398` is the single per-version switch that drives every dispatch — MSA family selection, loop-unroll policy, and the per-codename `*Target` vtable. The capability constants at `+0x438..+0x510` and the geometry at `+0x3B8` are the *data*; `+0x398` is the *selector* that chose which `chip_parts` blob filled them.

---

## Geometry Accessors and Their Consumers

### ChunksPerTile

`Target::ChunksPerTile` (`0x1D60F2C0`) is the canonical example of a derived geometry value: it divides `lane_count` by `sublane_count`.

```c
int64 Target::ChunksPerTile():                 // sub_1D60F2C0
    topo    = *((void**)this + 119)            // Target+0x3B8
    lane    = *(int64_t*)(topo + 0x198)        // lane_count
    sublane = *(int64_t*)(topo + 0x1A0)        // sublane_count
    return lane / sublane                       // 128 / 8 = 16 on every gen in this build
```

A 32-bit fast path is taken when both operands fit in 32 bits (they do), so the division is an unsigned 32-bit divide. With `lane=128`, `sublane=8` on every generation, `ChunksPerTile() == 16` — sixteen 8×128 lane-chunks per HBM tile. The HardwareLayout pass stamps `Tile(SublaneCount(), LaneCount()) = Tile(8, 128)` from the same two fields.

### VexMatrixWidthToSyU32

`pxc::mnemonics::VexMatrixWidthToSyU32` (one specialization per VEX instruction family, e.g. `0x1D2C20A0`) is an ISA-emitter-side consumer of the lane geometry. It converts a `VexMatrixWidth` enum on a TensorCore-vector instruction into the concrete SyU32 matrix width to encode.

```c
uint32 VexMatrixWidthToSyU32(const Instr* a1):  // sub_1D2C20A0 (one of several specializations)
    switch (a1->vex_matrix_width):              // a1[7]
        case 0: return 128                       // the default full MXU lane width
        case 1: return a1->width_field[0]        // explicit per-operand widths
        case 2: return a1->width_field[1]
        ...
        case 7: return a1->width_field[6]
        default: LOG(FATAL) << "Invalid VexMatrixWidth value"  // pf_proto_to_env_utils.h:1550
```

> **QUIRK —** `case 0` returns the literal `128`, not a value read from `Target`. The MXU lane width is hardcoded in the ISA-emitter helper because it is invariant — `lane_count` is 128 on every generation in this build. The non-zero cases read explicit width fields off the instruction for segmented/transpose VEX variants. A reimplementation that drives matrix width purely off `Target::LaneCount` would still get 128 here, but would miss the per-operand override cases.

### Who Reads the Config

The decoded constants fan out to three consumer layers:

- **Cost model.** `NodeCost` computes wall-clock time as `cycles × trip / (TensorCoreFrequencyInMegaHertz() × 1e6)` — reading `Target+0x90C`. Memory cost reads `HbmSizeBytes`/HBM bandwidth and the granule. See [Cost Model Overview](../cost/overview.md).
- **ISA emitter.** `VexMatrixWidthToSyU32` and the bundle encoders read the lane/sublane geometry and register counts to pick instruction encodings. See [ISA Overview](../isa/overview.md).
- **Topology / tiling.** `LaneCount`/`SublaneCount`/`ChunksPerTile` feed `HardwareLayout` tile stamping and the layout-assignment tiling pass. See [TpuTopology Struct](tpu-topology-struct.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `TpuChipParts::FromProto` | fills the `Target+0x438..+0x510` capability block this page's accessors read |
| `TpuChipConfig::FromProto` | parses the mode config (SharedMemoryRegion / SyncFlag) into the resource object |
| `TpuPxcDriver::InitializeCores` | consumes the `TpuChipConfig` resource object to provision per-core queues |
| `xla::jellyfish::Target` | the runtime HAL object whose field block and accessors are mapped here |

## Cross-References

- [chip_parts.binarypb Decode](chip-parts-binarypb.md) — the capability blob whose decoded fields fill the Target block this page maps
- [Per-Codename Constant Table](per-codename-hw-constants.md) — the per-generation values that land in these Target offsets
- [Codename Matrix](tpu-version-codename-matrix.md) — TpuVersion ↔ codename mapping (the `Target+0x398` selector)
- [Per-Gen Comparison Matrix](../appendix/per-gen-comparison-matrix.md) — cross-page consolidated per-generation comparison
- [TpuTopology Struct](tpu-topology-struct.md) — the `Target+0x3B8` geometry descriptor read by the lane/sublane accessors
- [Cost Model Overview](../cost/overview.md) — reads `TensorCoreFrequencyInMegaHertz`, HBM size/bandwidth, granule
- [ISA Overview](../isa/overview.md) — reads lane/sublane geometry and register counts via the emitter
