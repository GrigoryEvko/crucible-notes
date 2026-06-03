# TpuTopology & TpuCoreLocation

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; IDA-recovered C names and demangled C++ symbols quoted verbatim). `.text` VMA equals file offset. Other versions will differ.*

## Abstract

`TpuTopology_*` and `TpuCoreLocation_*` are the **C-ABI accessor roster** that exposes the physical torus geometry of a TPU pod to the open-source StreamExecutor backend. They are the thinnest layer in the topology stack: each is a one- or two-line `extern "C"` free function that reads a single field of a `tpu::TpuTopology` (a.k.a. `TpuTopologyExternal`) or a `tpu::TpuCoreLocation` object — chip-bounds extents, host/chip counts, per-core-type device counts, a chip-coordinate triple, a core index, a global id. Recovered from `learning/45eac/tfrc/executor/stream_executor/tpu_util_c_api.cc` references in the binary, they are the C face of the geometry that the on-device runtime discovers and the AOT compiler consumes.

These accessors sit *beneath* two richer topology surfaces this wiki documents elsewhere, and the contrast is the point. The PJRT [TopologyDescription extension (type 16)](../pjrt/ext-topology-description.md) is the *modern, framework-facing* topology object: a 272-byte struct of 31 function pointers (`ChipBounds`, `CoreCountPerChip`, `ProcessBounds`, slice configs) that wraps an `xla::PjRtTopologyDescription` and bounces through its vtable. The `TpuTopology_*` roster here is the *legacy SE C-ABI* underneath: no args-struct size negotiation, no `PJRT_Error` return, no vtable bounce — just `return *(uint32*)(topology + offset)`. The PJRT extension and the SE roster expose overlapping geometry (both have a notion of chip bounds and cores-per-chip), but they are independent C surfaces reaching the same silicon facts by different paths. This page owns the SE roster and the geometry-field map; the PJRT page owns the extension.

The roster is reached through the `ExecutorApiFn()` function-pointer table exactly like every other SE C-ABI cluster (the accessor pattern is documented on the [shim overview](overview.md)). The host-side C++ wrapper `tensorflow::tpu::TpuTopologyExternal::LogicalDevicesPerHost` (`0x20818F80`) is literally `ExecutorApiFn()[+632](handle, core_type)` — it loads the table, indexes the slot, and calls the plugin-side `TpuTopology_LogicalDevicesPerHost` (`0xEABBFC0`) which performs the field read. A reimplementer reproduces both halves: the wrapper that indexes a slot, and the C-ABI impl that reads an offset.

For reimplementation, the contract is:

- **The roster** — 17 `TpuTopology_*` and 4 `TpuCoreLocation_*` `extern "C"` functions, their addresses, and the C++ accessor or struct field each backs.
- **The geometry-field map** — which byte offset of `tpu::TpuTopology` / `tpu::TpuCoreLocation` each accessor reads (`ChipBounds_X` → `+88`, `HostCount` → `+108`, `Index` → `+52`, …), and the per-core-type strided array at `+124/+128`.
- **The core-type remapping quirk** — `LogicalDevicesPerHost` / `PerChip` fold the `TpuCoreType` enum through `if t != 1: t = 2*(t==2)` before dispatch; `Version` maps the internal codename ordinal `{0,1,2,3}` → `{1,2,3,4}` and returns `0` for anything else.
- **The two output conventions** — scalar return (`return *(uint32*)(...)`) versus the coordinate-triple out-param form (`ChipCoordinates` / `HostCoordinates` write three `_DWORD*` out-params).

| | |
|---|---|
| **Roster prefixes** | `TpuTopology_*` (17), `TpuCoreLocation_*` (4) |
| **C-ABI block** | `TpuTopology_*` @ `0xEABBFC0`–`0xEABC2A0`; `TpuCoreLocation_*` @ `0xEABC2C0`–`0xEABC340` |
| **Availability extras** | `TpuTopology_AvailableCoreCount/CoresPerChip/MaybeAvailableSparseCoresPerLogicalDevice` @ `0xF6A1CA0`–`0xF6A1EA0` |
| **Source file (recovered)** | `…/stream_executor/tpu_util_c_api.cc` (from `.rodata` log strings) |
| **Reached via** | `ExecutorApiFn()` slots (e.g. `LogicalDevicesPerHost` = `ExecutorApiFn()[+632]`) |
| **Backing C++ object** | `tpu::TpuTopology` (== `tensorflow::tpu::TpuTopologyExternal`) / `tpu::TpuCoreLocation` |
| **Output forms** | scalar `uint32` return; coordinate-triple `_DWORD*` out-params |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

> **Scope —** the `*ApiFn()` accessor pattern, the opaque-handle convention, and the roster map are on the [shim overview](overview.md) (link, not re-derived). The `TpuPlatform_*` / `TpuNodeContext_*` lifecycle that *constructs* the topology is on [TpuPlatform & TpuNodeContext](tpu-platform-and-topology.md). The modern PJRT topology object is on [TopologyDescription Extension](../pjrt/ext-topology-description.md). On-device discovery of the torus shape is on [ICI Topology Discovery](../ici/topology-discovery.md). The full `tpu::TpuTopology` struct layout (the `Target+0x3b8` per-codename geometry that these accessors index) is owned by the silicon pages — [SparseCore Architecture](../sparsecore/architecture.md) — and is summarised here only as far as the offsets these accessors touch.

---

## 1. The Accessor Pattern, Specialised

### Purpose

Every function on this page is the *plugin-side* half of one SE shim call. The *host-side* half is a `TpuTopologyExternal::<Method>` C++ wrapper that loads the `ExecutorApiFn()` table and calls a fixed slot. The two halves are linked at init (the [bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) fills the slot with the address of the matching `TpuTopology_*` impl), and only the `TfTpu_ExecutorApiFn*` pointer crosses the ABI seam. The topology object itself never crosses as a C++ type — the host holds it as an opaque `void*` and passes it back on every call.

### Entry Point

The worked path for `LogicalDevicesPerHost`, confirmed in the decompile:

```text
xla / SE TPU backend
  └─ tensorflow::tpu::TpuTopologyExternal::LogicalDevicesPerHost(core_type)   0x20818F80  (host-side C++ wrapper)
       └─ tbl = stream_executor::tpu::ExecutorApiFn()                          0x20819360  — singleton fn-ptr struct
       └─ tbl[+632]( *(void**)this, core_type )                                — call through the slot
              │  (slot populated at init to point at:)
              ▼
       TpuTopology_LogicalDevicesPerHost                                       0xEABBFC0   — plugin-side C-ABI impl
              └─ tpu::TpuTopology::LogicalDevicesPerHost(topo, core_type')      0x20AD3920  — after enum remap
```

> **NOTE —** the wrapper dereferences the handle once (`*(void**)this`) before passing it: the `TpuTopologyExternal` host object holds the real `tpu::TpuTopology*` at its `+0`, and that inner pointer is the `a1` every `TpuTopology_*` impl receives. The accessors index offsets relative to *that* inner object, not the host wrapper.

### Two output conventions

The roster splits cleanly into two call shapes, and a reimplementer must match each exactly:

```c
// (A) scalar return — the majority. Reads one uint32 field and returns it.
//     e.g. TpuTopology_HostCount @ 0xEABC000
uint32 TpuTopology_HostCount(void* topo):
    return *(uint32*)(topo + 108)

// (B) coordinate-triple out-param — TpuCoreLocation_ChipCoordinates / _HostCoordinates.
//     Three int32* out-params plus a redundant scalar return: _HostCoordinates returns
//     the x read (loc+8), _ChipCoordinates returns the last-written triple element (z).
//     e.g. TpuCoreLocation_HostCoordinates @ 0xEABC300
int32 TpuCoreLocation_HostCoordinates(void* loc, int32* x, int32* y, int32* z):
    *x = *(uint32*)(loc + 8); *y = *(uint32*)(loc + 12); *z = *(uint32*)(loc + 16)
    return *(uint32*)(loc + 8)
```

There is no `PJRT_Error`/`absl::Status` plumbing on this surface and no args-struct size check — those belong to the PJRT layer above. The SE C-ABI trusts its caller and reads the field unconditionally; the only validity gating is the `BUG()`/`return 0` guards inside the availability and version accessors (§4).

---

## 2. The TpuTopology_ Roster

### Function Map

The 14 canonical accessors form a contiguous block `0xEABBFC0`–`0xEABC2A0`; the three "available" accessors live in a separate block at `0xF6A1CA0`–`0xF6A1EA0` because they first fetch the topology from the mesh/ops-util singleton rather than receiving it as `a1`. "Reads" names the field offset on `tpu::TpuTopology` (the inner object) or the C++ member it thunks to.

| Function | Addr | Reads / Backs | Output | Confidence |
|---|---|---|---|---|
| `TpuTopology_LogicalDevicesPerHost` | `0xEABBFC0` | `tpu::TpuTopology::LogicalDevicesPerHost` (`0x20AD3920`) after enum remap | scalar | CERTAIN |
| `TpuTopology_LogicalDevicesPerChip` | `0xEABBFE0` | `tpu::TpuTopology::LogicalDevicesPerChip` after enum remap | scalar | CERTAIN |
| `TpuTopology_HostCount` | `0xEABC000` | `*(uint32*)(topo + 108)` | scalar | CERTAIN |
| `TpuTopology_ChipsPerHost` | `0xEABC020` | `*(uint32*)(topo + 116)` | scalar | CERTAIN |
| `TpuTopology_ChipBounds_X` | `0xEABC040` | `*(uint32*)(topo + 88)` | scalar | CERTAIN |
| `TpuTopology_ChipBounds_Y` | `0xEABC060` | `*(uint32*)(topo + 92)` | scalar | CERTAIN |
| `TpuTopology_ChipBounds_Z` | `0xEABC080` | `*(uint32*)(topo + 96)` | scalar | CERTAIN |
| `TpuTopology_HasChip` | `0xEABC0A0` | `tpu::TpuTopology::HasChip(topo, x, y, z)` | bool | CERTAIN |
| `TpuTopology_CoreForId` | `0xEABC0E0` | thunk → `tpu::TpuTopology::LogicalDeviceForId` | `TpuCoreLocation` | HIGH |
| `TpuTopology_Core` | `0xEABC100` | `tpu::TpuTopology::Core(topo, type, x, y, z, idx)` | `TpuCoreLocation` | CERTAIN |
| `TpuTopology_NumCores` | `0xEABC140` | `tpu::TpuTopology::logical_devices()` (count) | scalar | HIGH |
| `TpuTopology_Cores` | `0xEABC160` | `tpu::TpuTopology::logical_devices()` → fills `TpuCoreLocation*[]` | array fill | HIGH |
| `TpuTopology_IdForHost` | `0xEABC260` | `tpu::TpuTopology::IdForHost(topo, x, y, z)` | scalar | CERTAIN |
| `TpuTopology_Version` | `0xEABC2A0` | `**(uint32**)(topo + 8)` → codename ordinal, remapped | scalar | CERTAIN |
| `TpuTopology_AvailableCoreCount` | `0xF6A1CA0` | `*(uint32*)(GetTpuTopology() + 12*type + 128)` | scalar | HIGH |
| `TpuTopology_AvailableCoresPerChip` | `0xF6A1DE0` | `*(uint32*)(GetTpuTopology() + 12*type + 124)` | scalar | HIGH |
| `TpuTopology_MaybeAvailableSparseCoresPerLogicalDevice` | `0xF6A1EA0` | `xla::jellyfish::NumEmbeddingDevices(...)` (StatusOr<int>) | StatusOr | MEDIUM |

> **QUIRK —** the roster mixes pure field reads with member thunks, and the distinction is critical for a reimplementer. `ChipBounds_X/Y/Z`, `HostCount`, `ChipsPerHost` are flat `*(uint32*)(topo+off)` reads — the bounds and counts are pre-materialised scalars on the topology object. But `HasChip`, `Core`, `IdForHost`, `CoreForId` call into `tpu::TpuTopology` member functions that walk the chip/core layout. Copying only the field offsets reproduces the cheap accessors but not the lookups; copying only the thunks misses that the common geometry is a flat read with no computation.

### The geometry-field map

The offsets these accessors touch are a window into the `tpu::TpuTopology` layout (full struct on the [silicon pages](../sparsecore/architecture.md)). What is directly confirmed by the C-ABI bodies:

| Offset | Field | Read by | Notes |
|---|---|---|---|
| `+8` | `Target*` (embedded) | `Version` (double-deref `**`) | first `uint32` of `Target` is the codename ordinal |
| `+52` | core-on-chip index | `TpuCoreLocation_Index` (on the *core-location*, see §3) | — |
| `+88` | `chip_bounds.x` | `ChipBounds_X` | torus extent, X axis |
| `+92` | `chip_bounds.y` | `ChipBounds_Y` | torus extent, Y axis |
| `+96` | `chip_bounds.z` | `ChipBounds_Z` | torus extent, Z axis |
| `+108` | `host_count` | `HostCount` | total hosts in the pod |
| `+116` | `chips_per_host` | `ChipsPerHost` | chips attached to one host |
| `+124 + 12·type` | available cores-per-chip[type] | `AvailableCoresPerChip` | per-core-type strided entry |
| `+128 + 12·type` | available core-count[type] | `AvailableCoreCount` | per-core-type strided entry |

> **NOTE —** the `+124`/`+128` reads with a `12·type` stride are the same per-core-type geometry array the silicon pages describe at the `Target+0x3b8` offset within the larger structure: each `TpuCoreType` (TensorCore / SparseCore / embedding) gets a 12-byte record holding its cores-per-chip and core-count. The C-ABI exposes two of the three record fields. The full record and the codename→geometry table are owned by [SparseCore Architecture](../sparsecore/architecture.md); this page confirms only that `AvailableCoresPerChip` and `AvailableCoreCount` index it by `type` with a 12-byte stride and a `type < 3` bound (`BUG()` past it).

### Algorithm — the two non-trivial bodies

```c
function TpuTopology_Version(topo):                      // 0xEABC2A0
    ordinal = **(uint32**)(topo + 8)                      // first u32 of the embedded Target
    if ordinal < 4:
        return ordinal + 1                                // internal {0,1,2,3} -> public {1,2,3,4}
    return 0                                              // unknown codename -> 0

function TpuTopology_AvailableCoreCount(mesh_state, core_type):   // 0xF6A1CA0
    if mesh_state != NULL:
        topo = tensorflow::TpuMeshCommonState::tpu_topology(*mesh_state)   // VLOG "from mesh_state"
    else:
        topo = tensorflow::tpu_ops_util::GetTpuTopology()                 // VLOG "from tpu_ops_util"
    if core_type >= 3: BUG()                              // only 3 core types
    return *(uint32*)(topo + 12*core_type + 128)          // strided per-type read
```

> **GOTCHA —** `AvailableCoreCount` takes the **mesh state**, not a topology handle, and resolves the topology two different ways depending on whether `mesh_state` is null (the `else` branch falls back to the process-global `tpu_ops_util::GetTpuTopology()`). The sibling `AvailableCoresPerChip` / `MaybeAvailableSparseCoresPerLogicalDevice` *only* take the ops-util path and return a default (`4`) or an `absl::Status` error (`"TPU system is not available"`) when no topology is registered. A reimplementer who treats all three "available" accessors as pure field reads off a passed-in topology will mis-handle the no-device case these functions are specifically written to survive.

### The core-type remap

`LogicalDevicesPerHost` and `LogicalDevicesPerChip` fold the incoming `TpuCoreType` enum before dispatch:

```c
function TpuTopology_LogicalDevicesPerHost(topo, core_type):   // 0xEABBFC0
    if core_type != 1:
        core_type = 2 * (core_type == 2)                  // {0->0, 1->1, 2->2, other->0}
    return tpu::TpuTopology::LogicalDevicesPerHost(topo, core_type)   // 0x20AD3920
```

The remap collapses every enum value other than `1` and `2` to `0`, normalising an out-of-range or "default" core type to the first slot. This is a defensive clamp at the C-ABI boundary, where the host may pass a `TpuCoreType` from a newer/older enum the plugin does not recognise; the inner member function then indexes a fixed three-entry table without an out-of-bounds read.

### Considerations

`TpuTopology_Cores` (`0xEABC160`) is the one accessor with a non-trivial body: it fetches the `logical_devices()` span and emits a `TpuCoreLocation*` for each, materialising `result + 56·i` pointers (the `TpuCoreLocation` stride is 56 bytes) into a caller buffer. The decompile vectorises the pointer-stride write with AVX (`vpmuludq`/`vpaddq`), but the semantics are a simple `for i in 0..n: out[i] = base + 56*i`. The 56-byte stride is the confirmed `sizeof(tpu::TpuCoreLocation)` and is consistent with the field offsets §3 reads (`+52` is the last 4-byte field). `TpuTopology_NumCores` returns that same span's element count and is the size a caller passes to `_Cores` — the classic count-then-fill C-ABI pair.

---

## 3. The TpuCoreLocation_ Roster

### Purpose

A `tpu::TpuCoreLocation` is the per-core coordinate record minted by `TpuTopology_Core` / `_CoreForId` / `_Cores`: where one logical device sits in the torus. The four `TpuCoreLocation_*` accessors expose its fields to the host — two coordinate triples (chip and host), a core-on-chip index, and a flattened global id. The object is 56 bytes (confirmed by the `_Cores` stride); these accessors read its leading and trailing scalar fields.

### Function Map

| Function | Addr | Reads / Backs | Output | Confidence |
|---|---|---|---|---|
| `TpuCoreLocation_ChipCoordinates` | `0xEABC2C0` | `tpu::TpuCoreLocation::chip_coordinates()` → (x,y,z) | 3× `int32*` out + scalar | CERTAIN |
| `TpuCoreLocation_HostCoordinates` | `0xEABC300` | `*(uint32*)(loc + 8/12/16)` → (x,y,z) | 3× `int32*` out + scalar | CERTAIN |
| `TpuCoreLocation_Index` | `0xEABC320` | `*(uint32*)(loc + 52)` | scalar | CERTAIN |
| `TpuCoreLocation_Id` | `0xEABC340` | thunk → `tpu::TpuCoreLocation::LogicalDeviceId` | scalar | CERTAIN |

### Field map

| Offset | Field | Read by | Confidence |
|---|---|---|---|
| `+0` | chip coords / id base (via member fn) | `ChipCoordinates`, `Id` | HIGH |
| `+8` | `host_coord.x` | `HostCoordinates` (`loc[2]`) | CERTAIN |
| `+12` | `host_coord.y` | `HostCoordinates` (`loc[3]`) | CERTAIN |
| `+16` | `host_coord.z` | `HostCoordinates` (`loc[4]`) | CERTAIN |
| `+52` | core-on-chip index | `Index` | CERTAIN |

### Algorithm

```c
function TpuCoreLocation_ChipCoordinates(loc, x_out, y_out, z_out):   // 0xEABC2C0
    int32 tmp[3]
    tpu::TpuCoreLocation::chip_coordinates(&tmp)        // member computes the chip-coord triple
    *x_out = tmp[0]; *y_out = tmp[1]; *z_out = tmp[2]
    return tmp[2]                                        // (redundant) scalar return = z

function TpuCoreLocation_HostCoordinates(loc, x_out, y_out, z_out):   // 0xEABC300
    *x_out = *(uint32*)(loc + 8)                         // direct field reads — no member call
    *y_out = *(uint32*)(loc + 12)
    *z_out = *(uint32*)(loc + 16)
    return *(uint32*)(loc + 8)

function TpuCoreLocation_Index(loc):                     // 0xEABC320
    return *(uint32*)(loc + 52)
```

> **QUIRK —** `ChipCoordinates` routes through the `chip_coordinates()` member (a computation, not a stored triple) while `HostCoordinates` is three flat field reads at `+8/+12/+16`. The asymmetry tells a reimplementer the storage decision: host coordinates are stored verbatim on the core-location record, but chip coordinates are derived (likely from the flattened id plus the chip bounds) and must be recomputed on each call. Both expose the same `(int32*, int32*, int32*)` out-param shape and a redundant scalar return, so the host signature is uniform even though the implementations differ.

> **NOTE —** `TpuCoreLocation_Id` thunks to `LogicalDeviceId`, the *logical*-device id, not a raw physical core id — consistent with `TpuTopology_CoreForId` thunking to `LogicalDeviceForId`. The "id" on this C surface is the flattened logical-device index that XLA's device-assignment uses, the same id space the PJRT [device descriptions](../pjrt/ext-topology-description.md) and `LogiDeviceIdFromChipCoordAndIdx` extension method operate in. A reimplementer must keep "id" meaning logical-device id throughout this roster.

---

## 4. Validity Gating

Unlike the PJRT extension (which negotiates args-struct sizes and returns `PJRT_Error`), the SE C-ABI accessors trust their inputs and gate only on a few hard invariants:

| Accessor(s) | Guard | Failure behaviour |
|---|---|---|
| `LogicalDevicesPerHost` / `PerChip` | enum remap `if t != 1: t = 2·(t==2)` | clamps unknown core types to `0` |
| `Version` | `ordinal < 4` | returns `0` for unrecognised codename |
| `AvailableCoreCount` | `core_type < 3` | `BUG()` (abort) past the bound |
| `AvailableCoresPerChip` | topology non-null; `type < 3` | returns `4` if no topology; `BUG()` past bound |
| `MaybeAvailableSparseCoresPerLogicalDevice` | topology non-null; `type == 2` | `absl::Status` error: `"TPU system is not available"` / `"Invalid core type queried"` |
| pure field readers (`HostCount`, `ChipBounds_*`, …) | none | unconditional field read |

> **GOTCHA —** the pure field readers perform **no null check on the topology pointer**. They assume the host passed a live, fully-constructed `tpu::TpuTopology` (built by the [TpuPlatform/TpuNodeContext](tpu-platform-and-topology.md) bring-up). The only accessors that defend against a missing topology are the three "available" functions, which resolve it lazily from the mesh/ops-util singleton and so must cope with "no TPU attached." A reimplementer must construct the topology before any pure-reader call; there is no graceful path for `topo == NULL` on `ChipBounds_X` and friends — it dereferences `NULL + 88`.

---

## Related Components

| Name | Relationship |
|---|---|
| `tensorflow::tpu::TpuTopologyExternal::*` | host-side C++ wrappers that index `ExecutorApiFn()` slots into these C-ABI impls |
| `tpu::TpuTopology` / `tpu::TpuCoreLocation` | the C++ objects whose fields the roster reads |
| `tensorflow::TpuMeshCommonState::tpu_topology` / `tpu_ops_util::GetTpuTopology` | the singletons the "available" accessors resolve the topology from |
| `xla::jellyfish::NumEmbeddingDevices` | backs `MaybeAvailableSparseCoresPerLogicalDevice` |
| PJRT type-16 topology extension | the modern parallel surface exposing overlapping geometry via a different C ABI |

## Cross-References

- [The TfTpu C-API Shim](overview.md) — the `*ApiFn()` accessor pattern, opaque-handle convention, and the roster map this page is one entry of
- [TpuPlatform & TpuNodeContext](tpu-platform-and-topology.md) — the `TpuPlatform_*` / `TpuNodeContext_*` lifecycle that constructs the `tpu::TpuTopology` these accessors read
- [TpuExecutor Roster](tpu-executor-roster.md) — the sibling `TpuExecutor_*` per-device runtime C-ABI cluster reached through the same `ExecutorApiFn()` table
- [TopologyDescription Extension (type 16)](../pjrt/ext-topology-description.md) — the *modern PJRT* topology object; contrast: this page is the C-accessor layer beneath it, no args-size negotiation, no `PJRT_Error`
- [ICI Topology Discovery](../ici/topology-discovery.md) — the on-device runtime discovery of the torus geometry these accessors statically expose
- [SparseCore Architecture](../sparsecore/architecture.md) — Part-IV silicon geometry: the full `tpu::TpuTopology` struct and the per-core-type record at `+124/+128` (`Target+0x3b8`) that `AvailableCoreCount` / `AvailableCoresPerChip` index
