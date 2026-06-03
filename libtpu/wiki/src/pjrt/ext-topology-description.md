# TopologyDescription Extension (type 16)

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`), exporting PJRT C-API **v0.103**. Other wheels will differ.*

## Abstract

A PJRT *topology description* is the device-fabric geometry **without a live client**. JAX builds one with `PJRT_TopologyDescription_Create("tpu_v4", {chip_bounds=[2,4,4]})` so it can compile an XLA program *ahead-of-time* — before any TPU is attached, on a host that may have no accelerator at all. The compiler needs the torus shape (chips per axis, cores per chip, process layout) to lay out collectives and shard tensors; the topology surface is how it gets that shape without bringing up the runtime. This contrasts with the live path on [Client, Device & Topology](client-and-device.md), where `PJRT_Client_TopologyDescription` (slot 100) hands back the topology already attached to a running `xla::TpuClient` — a *borrowed* handle the caller must not destroy. This page owns the **standalone, AOT topology**: how it is created from a name string and an options map, how its C-ABI wrapper caches device descriptions, and how the TPU-specific extension exposes torus coordinates.

The surface is **two-tier**. The first tier is seven generic `PJRT_TopologyDescription_*` slots in the main 140-slot `PJRT_Api` table (Create, Destroy, PlatformName, PlatformVersion, GetDeviceDescriptions, Serialize, Attributes) plus three v0.103 late additions (Deserialize at slot 119, Fingerprint at slot 138, Client_TopologyDescription at slot 100). These expose the XLA-portable abstraction: a platform name, an opaque protobuf serialization, a device-description list, an attribute key/value list, and a 64-bit fingerprint — identical in shape to the CPU and GPU plugins. The second tier is a **TPU-specific extension** (`PJRT_Extension_Base.type == 16`) hung off the extension chain: a 272-byte struct of 31 function pointers carrying torus geometry (`ChipBounds`, `CoreCountPerChip`, `ProcessBounds`, slice configs, ICI routing flags) that the generic surface cannot express.

The defining structural fact is **eager caching in a heap wrapper**. `PJRT_TopologyDescription` is a 112-byte object that owns one `xla::PjRtTopologyDescription*` and pre-materializes three lookup spans at construction. `GetDeviceDescriptions` and `Attributes` therefore degenerate to two-`mov` field reads with no allocation; only the scalar/geometry methods on the TPU extension dispatch through the implementation vtable. A reimplementer who lazily computes the device list on every `GetDeviceDescriptions` call will diverge from the binary's behavior and its lifetime guarantees.

For reimplementation, the contract is:

- **The create path** in `PJRT_TopologyDescription_Create` (slot 87, TPU-overridden): validate struct size, parse `PJRT_NamedValue[]` options, resolve the topology name via `CustomTpuTopologyNameOverride`, build the abstract `xla::PjRtTopologyDescription` via `GetTpuTopologyDescription`, and wrap it with `CreateWrapperDeviceTopology`.
- **The wrapper layout** (112 bytes): one owned impl pointer plus three pre-built spans (device shared-ptrs, `PJRT_DeviceDescription` array, device-pointer span) and a cached `PJRT_NamedValue` attribute array — so the read slots never allocate.
- **The TPU extension calling convention**: a non-standard args layout (wrapper at `+0x08`, **no** `priv` field) and a uniform vtable-bouncer body that loads `wrapper->impl`, calls a fixed vtable offset, and copies a scalar or bounds-checked vector back into the args struct.
- **The lifetime split**: Create / Deserialize return *owning* wrappers destroyed exactly once via Destroy (slot 88); Client_TopologyDescription returns a *borrowed* one whose lifetime the client owns.

| | |
|---|---|
| **Generic surface** | 7 slots @ `PJRT_Api+0x2B8..+0x2E8` (slots 87–93) + late slots 100 / 119 / 138 |
| **Create (TPU)** | `pjrt::tpu_plugin::PJRT_TopologyDescription_Create` @ `0xE6A9B20` (slot 87) |
| **Wrapper build** | `pjrt::CreateWrapperDeviceTopology(unique_ptr<>)` @ `0xF870E60` (body @ `0xF873260`) |
| **Wrapper size** | 112 bytes (`0x70`), owns `xla::PjRtTopologyDescription*` at `+0x00` |
| **TPU extension** | `type == 16`, 272 bytes (`0x110`), 31 fn-ptrs @ `0x224C3B90` (`.bss`) |
| **Extension populate** | `pjrt::CreateTpuTopologyExtension` @ `0xE6DE5E0` (35-slot store, no calls) |
| **Serialization** | protobuf binary of `xla::PjRtTopologyDescriptionProto` (proto2) |
| **C-API version** | v0.103 (`{major=0, minor=103}`) |

---

## The Two-Tier Surface

### Purpose

The topology is reachable two ways with deliberately different shapes. The **generic tier** is the portable XLA abstraction every PJRT plugin implements; a framework that only knows `pjrt_c_api.h` drives the whole topology through these ten slots without ever learning it is talking to a TPU. The **TPU extension tier** is the escape hatch for geometry the abstract `xla::PjRtTopologyDescription` vtable cannot express — torus bounds, per-chip core counts, ICI routing — reached only by a consumer that walks the extension chain looking for `type == 16`.

### Generic Slot Map

The seven core slots sit contiguously at `PJRT_Api+0x2B8..+0x2E8`; the three v0.103 late additions are scattered at the slot positions assigned when they were appended to the ABI.

| Slot | Field | libtpu impl | Addr | Min/Cur sz | Confidence |
|---|---|---|---|---|---|
| 87 | `_Create` | `pjrt::tpu_plugin::PJRT_TopologyDescription_Create` | `0xE6A9B20` | `0x24` / `0x38` | CERTAIN |
| 88 | `_Destroy` | `pjrt::PJRT_TopologyDescription_Destroy` | `0xF870040` | `0x25` / `0x18` | CERTAIN |
| 89 | `_PlatformName` | `pjrt::PJRT_TopologyDescription_PlatformName` | `0xF870200` | `0x2A` / `0x28` | HIGH |
| 90 | `_PlatformVersion` | `pjrt::PJRT_TopologyDescription_PlatformVersion` | `0xF870260` | `0x2D` / `0x28` | HIGH |
| 91 | `_GetDeviceDescriptions` | `pjrt::PJRT_TopologyDescription_GetDeviceDescriptions` | `0xF8702C0` | `0x33` / `0x28` | CERTAIN |
| 92 | `_Serialize` | `pjrt::PJRT_TopologyDescription_Serialize` | `0xF870320` | `0x27` / `0x38` | HIGH |
| 93 | `_Attributes` | `pjrt::PJRT_TopologyDescription_Attributes` | `0xF8705E0` | `0x28` / `0x28` | HIGH |
| 100 | `Client_TopologyDescription` | `pjrt::PJRT_Client_TopologyDescription` | `0xF85F560` | — | CERTAIN |
| 119 | `_Deserialize` | `pjrt::PJRT_TopologyDescription_Deserialize` | `0xF870B80` | `0x28` / `0x28` | HIGH |
| 138 | `_Fingerprint` | `pjrt::PJRT_TopologyDescription_Fingerprint` | `0xF870520` | `0x29` / `0x20` | HIGH |

`Min sz` is the `min_size` constant passed to `ActualStructSizeIsGreaterOrEqual` — the oldest historic args struct accepted; `Cur sz` is the size this build's own callers pass.

> **QUIRK —** only slot 87 (`_Create`) is TPU-overridden — it points at `pjrt::tpu_plugin::PJRT_TopologyDescription_Create`. The other six generic slots are the *shared* `pjrt::PJRT_TopologyDescription_*` wrappers also used by the CPU and GPU plugins; the TPU specialization happens entirely inside the `xla::PjRtTopologyDescription` (a `tpu::TpuTopology` subclass) those wrappers call, and inside the type-16 extension. `Client_TopologyDescription` (slot 100, the *live* path) is documented on [Client, Device & Topology](client-and-device.md); it is listed here only to complete the construction-paths picture.

> **GOTCHA —** the Destroy slot's `(min=0x25, current=0x18)` pair has `min > current`. `0x25` (37) is the original `min_size` literal baked into the call; current callers truncate the args struct to 24 bytes. The runtime test is `actual >= min`, so a 24-byte args struct from a current caller would *fail* a literal `>= 37` check — but the decompiled `ActualStructSizeIsGreaterOrEqual` treats the smaller of the two as the floor for backward-compat. Copy the exact `(min, current)` constants per slot rather than assuming a uniform rule.

---

## PJRT_TopologyDescription_Create — Standalone Topology

### Purpose

`PJRT_TopologyDescription_Create` is the AOT entry point: it turns a platform-name string plus an options map into a fully-built topology wrapper with no live client and no hardware probe. JAX calls it once per ahead-of-time compilation target. It is the only TPU-overridden generic topology slot, because name resolution (`"v4:2x2x1"` → canonical config) and the chip-bounds lookup are TPU-private.

### Entry Point

```text
PJRT_Api slot 87 (0x2B8)                                ── pjrt::tpu_plugin::PJRT_TopologyDescription_Create (0xE6A9B20)
  ├─ pjrt::ActualStructSizeIsGreaterOrEqual              ── "PJRT_TopologyDescription_Create_Args", min=0x24, cur=0x38
  ├─ pjrt::ConvertFromPjRtNamedValueList (0xF8A43C0)     ── create_options[] → flat_hash_map<string,variant>
  ├─ pjrt::CustomTpuTopologyNameOverride (0xE6AFCE0)     ── platform_name + opts → canonical topology name (StatusOr)
  ├─ pjrt::GetTpuTopologyDescription (0xE6ADB60)         ── name + opts → xla::PjRtTopologyDescription (StatusOr)
  └─ pjrt::CreateWrapperDeviceTopology (0xF870E60)       ── wrap into 112-byte PJRT_TopologyDescription, store at args[6]
```

### Algorithm

```c
function PJRT_TopologyDescription_Create(args):          // 0xE6A9B20
    // args[0]=struct_size, args[2]=platform_name.data, args[3]=platform_name.len,
    // args[4]=create_options (PJRT_NamedValue*), args[5]=num_options,
    // args[6]=PJRT_TopologyDescription** out  (offset +0x30).
    st = ActualStructSizeIsGreaterOrEqual(               // 0xE6A9B20:+0x18
            "PJRT_TopologyDescription_Create_Args", min=0x24, cur=0x38, args[0])
    if st != 1:
        return new PJRT_Error{st}                        // operator new(8)

    // (1) untyped option ingest — same parser as PJRT_Client_Create.
    opts = ConvertFromPjRtNamedValueList(args[4], args[5])   // -> flat_hash_map<string,variant<str,bool,long,vector<long>,float>>
    if args[3] < 0: BUG()                                // platform_name length sanity

    // (2) resolve the canonical topology name. Returns a Status as the
    //     first slot of the StatusOr; an error here aborts before build.
    name_or = CustomTpuTopologyNameOverride(args[2], args[3], opts)   // 0xE6AFCE0
    if name_or is error:
        return new PJRT_Error{name_or.status}            // refcount the StatusRep, then return

    // (3) build the abstract topology: looks up chip_bounds / host_bounds /
    //     slice_config for the named platform, constructs the TpuTopology subclass.
    topo_or = GetTpuTopologyDescription(name_or, opts)   // 0xE6ADB60 -> StatusOr<unique_ptr<xla::PjRtTopologyDescription>>
    if topo_or is error:
        return new PJRT_Error{topo_or.status}

    // (4) wrap for the C ABI; out-param is args[6] (+0x30). Transfers
    //     ownership of the unique_ptr into the wrapper's +0x00 slot.
    args[6] = CreateWrapperDeviceTopology(topo_or.release())   // 0xF870E60
    return NULL                                          // success
```

> **GOTCHA —** if `platform_name` is empty *and* `create_options` is non-empty, `CustomTpuTopologyNameOverride` returns the error string `"TPU PJRT_TopologyDescription_Create does not support extra create_options if no topology_name is given."` (in `.rodata`). A reimplementer must reject options-without-a-name here; the options table is keyed off the resolved name, so there is no default config to apply them to.

> **NOTE —** the out-param index differs from `Client_Create`. Here the topology is written to `args[6]` (byte offset `+0x30` within the args struct), confirmed by the decompiled `a1[6] = pjrt::CreateWrapperDeviceTopology(...)`. The `_Deserialize` path (slot 119) writes its wrapper to `args[5]` (`+0x28`) instead — the args layouts are not interchangeable.

### Considerations

`GetTpuTopologyDescription` (`0xE6ADB60`) is where the platform-name string is mapped to a torus config. The supported names round-trip through the same `kPlatformConfigs` singleton (see [Slice & Platform Configs](#slice--platform-configs)) used by `GetDefaultPlatformConfig`; the canonical set observed in the binary is `"tpu"`, `"tpu_v4"`, `"tpu_v5e"`, `"tpu_v6e"` and similar per-generation keys (MEDIUM confidence — the full key list requires a walk of the `kPlatformConfigs` map storage at `0x224C4108`). The constructed object is `const` after build: a TPU topology is assembled from immutable system queries and never mutated, which is what makes the cached read slots safe to call concurrently.

---

## The Wrapper — PJRT_TopologyDescription (112 bytes)

### Purpose

`CreateWrapperDeviceTopology` (`0xF870E60`) takes ownership of the `unique_ptr<xla::PjRtTopologyDescription>` and produces the 112-byte heap object the C ABI hands out. Its defining behavior, exactly as in the client wrapper, is **eager caching**: at construction it iterates the impl topology's device list once and builds three spans plus a cached attribute array, so the generic `GetDeviceDescriptions` and `Attributes` slots become field reads.

### Wrapper Layout (`PJRT_TopologyDescription`, 112 bytes)

Reconstructed from the destructor (`~PJRT_TopologyDescription` @ `0xF8700C0`) and the seven generic readers. Offsets are within the `operator new(0x70)` block.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `impl` | `+0x00` | `xla::PjRtTopologyDescription*` (owned) | The implementation topology; its vtable drives every uncached slot |
| `_error_slot` | `+0x08` | `shared_ptr<absl::Status>` | Unused / padding in this build (LOW: never observed written) |
| `cached_device_shared_ptrs` | `+0x10` | `vector<shared_ptr<xla::PjRtDeviceDescription>>` | begin/end/cap; owns the impl device-description refcounts |
| `cached_device_descriptions` | `+0x28` | `vector<PJRT_DeviceDescription>` | begin/end/cap; one 0x38-byte wrapper per device |
| `cached_device_pointer_span` | `+0x40` | `vector<PJRT_DeviceDescription*>` | begin/end/cap; the span returned verbatim by `GetDeviceDescriptions` |
| `attributes_data` | `+0x58` | `PJRT_NamedValue*` | Cached attribute array (0x38 bytes each) |
| `attributes_size` | `+0x60` | `size_t` | Returned by `Attributes` |
| `attributes_capacity` | `+0x68` | `size_t` | Backing capacity |

### Algorithm

```c
function CreateWrapperDeviceTopology(impl_uptr):         // 0xF870E60 (thin) -> 0xF873260 (body)
    w = operator new(0x70)                                // 112-byte PJRT_TopologyDescription
    prev = w->impl
    w->impl = impl_uptr                                   // store owned topology at +0x00
    if prev != NULL:                                      // defensive: run dtor on any prior occupant
        prev->vtable[+0x08]( prev )                       // virtual destructor

    // (cached at construction by the body @ 0xF873260)
    // (1) DEVICE DESCRIPTIONS: walk impl->DeviceDescriptions() (vtable +0x30),
    //     a vector<shared_ptr<PjRtDeviceDescription>>; for each, allocate a
    //     PJRT_DeviceDescription wrapper and push into cached_device_descriptions.
    for sp in impl->DeviceDescriptions():                 // vtable +0x30
        w.cached_device_shared_ptrs.push_back(sp)         // keep the refcount alive
        w.cached_device_descriptions.push_back(PJRT_DeviceDescription{ sp.get() })
        w.cached_device_pointer_span.push_back(&w.cached_device_descriptions.back())

    // (2) ATTRIBUTES: snapshot impl->Attributes() into a PJRT_NamedValue[] once.
    w.attributes_data, w.attributes_size = PopulatePjrtAttributes(impl->Attributes())
    return w
```

> **QUIRK —** the device-description span (`+0x40`) and the attribute array (`+0x58`) are built **once** and read directly by slots 91 and 93. The decompiled `GetDeviceDescriptions` is literally `args[3] = *(wrapper+0x40); args[4] = *(wrapper+0x48)` — two `mov`s, no call into the impl topology and no allocation. A reimplementation that calls `impl->DeviceDescriptions()` on every `GetDeviceDescriptions` invocation would re-walk and re-allocate, and would also break the lifetime contract: callers may hold the returned `PJRT_DeviceDescription*` array as long as the wrapper lives, so it must be stable storage, not a per-call temporary.

### Generic Read Slots

```c
function PJRT_TopologyDescription_GetDeviceDescriptions(args):  // 0xF8702C0, slot 91
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_TopologyDescription_GetDeviceDescriptions_Args", 0x33, 0x28, args[0]) != 1:
        return new PJRT_Error{...}
    w = args[2]                                           // PJRT_TopologyDescription*
    args[3] = *(w + 0x40)                                 // cached_device_pointer_span.data
    args[4] = *(w + 0x48)                                 // cached_device_pointer_span.size
    return NULL                                           // never touches w->impl

function PJRT_TopologyDescription_Attributes(args):       // 0xF8705E0, slot 93
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_TopologyDescription_Attributes_Args", 0x28, 0x28, args[0]) != 1:
        return new PJRT_Error{...}
    w = args[2]
    args[3] = *(w + 0x58)                                 // attributes_data
    args[4] = *(w + 0x60)                                 // attributes_size
    return NULL
```

`PlatformName` (slot 89) and `PlatformVersion` (slot 90) are *not* cached — they bounce through the impl vtable: `PlatformName` calls vtable `+0x18` and `PlatformVersion` calls vtable `+0x20`, each returning a `std::string_view` whose `{data, size}` pair is stored into `args+0x18 / args+0x20`. These are cheap virtual calls returning interior pointers into the const topology, so caching would buy nothing.

> **NOTE —** the **AOT topology attributes are where TPU geometry surfaces to a generic consumer.** A framework that does not walk the extension chain still sees slice shape, chip coordinates, and cores-per-chip as named entries in the `Attributes` (slot 93) list — the same mechanism `PJRT_DeviceDescription_Attributes` uses on the live path. A reimplementer building the AOT topology must populate `coords` / `core_on_chip` / `slice_index` / cores-per-chip as `PJRT_NamedValue` attribute entries, in addition to wiring the type-16 extension methods.

---

## The TPU Extension (type 16, 31 methods)

### Purpose

The abstract `xla::PjRtTopologyDescription` vtable exposes only what every backend shares (platform name, device descriptions, serialize, fingerprint). TPU torus geometry — chip bounds, per-chip core counts, process layout, ICI routing strategy, slice configs — has no slot in that vtable. The type-16 extension is the carrier: a 272-byte struct of 31 function pointers, chain-linked off `extension_start`, that a TPU-aware consumer finds by walking the chain for `base->type == 16`.

### Storage and Population

The extension struct is a static at `pjrt::tpu_plugin::GetTpuPjrtApi::tpu_topology_extension` @ `0x224C3B90` (`.bss`), guard @ `0x224C3CA0`, populated once during the `__cxa_guard`-protected `GetTpuPjrtApi` init by `pjrt::CreateTpuTopologyExtension(PJRT_Extension_Base*)` @ `0xE6DE5E0`. That function is 35 `lea`/store pairs and a `ret` — three header slots then 31 function pointers, with the single `rsi` argument written to `+0x10` as the chain `next` (pointing at `callback_extension` @ `0x224C3B60`).

```c
struct PJRT_Extension_Base {                              // 24-byte common header
    /* +0x00 */ size_t                struct_size;         // = 0x110
    /* +0x08 */ uint32_t              type;                // = 16
    /* +0x0c */ uint32_t              _pad0;
    /* +0x10 */ PJRT_Extension_Base*  next;                // → callback_extension @ 0x224C3B60
    /* +0x18 .. +0x108 : 31 function pointers (see method table) */
};
_Static_assert(sizeof(PJRT_TpuTopology_Extension) == 0x110, "3*8 header + 31*8 fn-ptrs");
```

> **GOTCHA —** the TPU extension args structs use a **different layout** from the main `PJRT_Api`. Each places the wrapper pointer at `+0x08`, immediately after `struct_size` — there is **no `priv` field**. The main `PJRT_Api` args follow `pjrt_c_api.h`'s `{size_t struct_size; void* priv; ...}` convention with the payload at `+0x10`. This is visible in the decompile as `mov 0x8(%rbx),%rax` (extension; `a1[1]`) versus `mov 0x10(%rbx),%rax` (main; `a1[2]`). A reimplementation that copies the main-API offset into the extension methods reads the wrong field and dereferences garbage.

### Calling Convention — the Uniform Body

The 31 methods partition into three categories, but every method opens the same way: validate `struct_size`, then load `wrapper = args[1]` (offset `+0x08`) and `topo = wrapper->impl` (`*(wrapper+0x08)` → the `xla::PjRtTopologyDescription*` at wrapper `+0x00` — note the inner deref). The **vtable-bouncer** majority then calls a fixed vtable offset and copies the result back.

```c
PJRT_Error* method(PJRT_TpuTopology_<Name>_Args* args) {  // uniform template
    if (ActualStructSizeIsGreaterOrEqual(
            "PJRT_TpuTopology_<Name>_Args", <min>, <cur>, args->struct_size) != 1)
        return new PJRT_Error{returned_status};            // operator new(8)

    PJRT_TopologyDescription* wrapper = args[1];            // args+0x08 (NO priv)
    xla::PjRtTopologyDescription* topo = wrapper->impl;     // wrapper+0x00
    StatusOr<T> r = topo->vtable[<offset>]();               // fixed per-method offset

    if (r.ok()) { args->output = move(r.value()); return NULL; }
    return new PJRT_Error{r.status()};                      // refcount StatusRep, then wrap
}
```

Two confirmed bodies anchor the template:

```c
function CoreCountPerChip(args):                          // 0xE6DF540, ext slot 9
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_TpuTopology_CoreCountPerChip_Args", 0x26, 0x14, args[0]) != 1:
        return new PJRT_Error{...}
    topo = *(args[1] + 8)                                  // wrapper->impl
    r = topo->vtable[+0x80]()                              // StatusOr<int32>
    if r.ok():
        *(int32*)(args + 0x10) = r.value                   // scalar out at +0x10
        return NULL
    return new PJRT_Error{r.status}

function ChipBounds(args):                                 // 0xE6E0AE0, ext slot 25
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_TpuTopology_ChipBounds_Args", 0x20, 0x28, args[0]) != 1:
        return new PJRT_Error{...}
    topo = *(args[1] + 8)
    sv = topo->vtable[+0xD0]()                             // StatusOr<vector<int32>>  (X,Y,Z bounds)
    if not sv.ok(): return new PJRT_Error{sv.status}
    n = sv.size()
    args[4] = n                                            // actual_count at +0x20
    if args[2] >= n:                                       // capacity at +0x10
        for i in 0..n: ((int32*)args[3])[i] = sv[i]        // buffer at +0x18
        return NULL
    // (caller buffer too small: vector path returns InvalidArgument "needed N, provided M")
```

### Method Table (31 functions)

Offsets are within the 272-byte extension struct; "vtable" is the offset on the abstract `xla::PjRtTopologyDescription` the bouncer calls. Category: **VB** = vtable-bouncer, **HLP** = downcast-to-`tpu::TpuTopology` helper, **CFG** = `kPlatformConfigs` lookup.

| Off | Method | Impl addr | vtable / via | Cat | Confidence |
|---|---|---|---|---|---|
| `+0x18` | `Subslice` | `0xE6DE7A0` | input-array, computed | VB | MEDIUM |
| `+0x20` | `IsSubsliceTopology` | `0xE6DEC20` | `+0x38` (bool) | VB | HIGH |
| `+0x28` | `SubsliceDeviceIdFromFullDeviceId` | `0xE6DEC80` | computed cast | VB | LOW |
| `+0x30` | `ReplaceHostBounds` | `0xE6DEF40` | helper | HLP | MEDIUM |
| `+0x38` | `IsEnhancedBarrierEnabled` | `0xE6DF200` | helper (`TpuTopology+0x188/+0x190`) | HLP | HIGH |
| `+0x40` | `HasLimitedIciConnectivity` | `0xE6DF2A0` | helper | HLP | HIGH |
| `+0x48` | `IsReachableOverLimitedIci` | `0xE6DF340` | helper | HLP | MEDIUM |
| `+0x50` | `ProcessCount` | `0xE6DF400` | `+0x40` (int) | VB | HIGH |
| `+0x58` | `ChipsPerProcess` | `0xE6DF4A0` | `+0x48` (int) | VB | HIGH |
| `+0x60` | `CoreCountPerChip` | `0xE6DF540` | `+0x80` (int) | VB | CERTAIN |
| `+0x68` | `ChipCount` | `0xE6DF5E0` | `+0x50` (int) | VB | HIGH |
| `+0x70` | `CoreCount` | `0xE6DF680` | `+0x58` (int) | VB | HIGH |
| `+0x78` | `LogiDeviceCountPerProcess` | `0xE6DF720` | `+0x60` (int) | VB | HIGH |
| `+0x80` | `LogiDeviceCount` | `0xE6DF7C0` | `+0x68` (int) | VB | HIGH |
| `+0x88` | `LogiDeviceCountPerChip` | `0xE6DF860` | `+0x70` (int) | VB | HIGH |
| `+0x90` | `CoreCountPerProcess` | `0xE6DF900` | `+0x78` (int) | VB | HIGH |
| `+0x98` | `ProcessIds` | `0xE6DF9A0` | `+0x88` (vector) | VB | HIGH |
| `+0xA0` | `LogiDeviceIdsOnProcess` | `0xE6DFB80` | computed (vector) | VB | MEDIUM |
| `+0xA8` | `ProcIdAndIdxOnProcForChip` | `0xE6DFD60` | `+0x98` | VB | MEDIUM |
| `+0xB0` | `ProcIdAndIdxOnProcForLogiDevice` | `0xE6DFE20` | `+0xA0` | VB | MEDIUM |
| `+0xB8` | `ProcessCoordFromId` | `0xE6DFEE0` | `+0xA8` | VB | MEDIUM |
| `+0xC0` | `ChipIdFromCoord` | `0xE6E00A0` | computed | VB | MEDIUM |
| `+0xC8` | `LogiDeviceIdFromChipCoordAndIdx` | `0xE6E03C0` | computed | VB | MEDIUM |
| `+0xD0` | `ChipCoordAndIdxForLogiDevice` | `0xE6E06E0` | `+0xC0` | VB | MEDIUM |
| `+0xD8` | `ChipsPerProcessBounds` | `0xE6E0920` | `+0xC8` (vector) | VB | HIGH |
| `+0xE0` | `ChipBounds` | `0xE6E0AE0` | `+0xD0` (vector) | VB | CERTAIN |
| `+0xE8` | `ProcessBounds` | `0xE6E0CA0` | computed (vector) | VB | HIGH |
| `+0xF0` | `GetRoutingStrategy` | `0xE6E0E60` | helper | HLP | MEDIUM |
| `+0xF8` | `GetSliceConfig` | `0xE6E1080` | `kPlatformConfigs` | CFG | MEDIUM |
| `+0x100` | `GetSliceConfigs` | `0xE6E13A0` | `kPlatformConfigs` | CFG | MEDIUM |
| `+0x108` | `GetDefaultPlatformConfig` | `0xE6E16A0` | `kPlatformConfigs` | CFG | MEDIUM |

> **NOTE —** there is **no `TpuGetMeshShape` / `TpuGetCoreIds` method**. Mesh shape is recovered by composing `ChipBounds` + `ProcessBounds` + `ChipsPerProcessBounds`; core IDs from `LogiDeviceIdFromChipCoordAndIdx` ↔ `ChipCoordAndIdxForLogiDevice` and `ProcIdAndIdxOnProcForLogiDevice`. A dead args string, `PJRT_TpuTopology_ProcessIdAndIndexOnProcessForLogiDeviceOfDefaultType_Args` (file offset `0x8551469`), is the renamed predecessor of `ProcIdAndIdxOnProcForLogiDevice` and is not referenced by any slot.

### Args Layouts

```c
// Scalar-output (counts, IsSubsliceTopology, ProcessCoordFromId, ChipIdFromCoord, ...)
struct PJRT_TpuTopology_<Scalar>_Args {
    size_t                     struct_size;   // +0x00
    PJRT_TopologyDescription*  topology;      // +0x08 (NO priv)
    int32_t                    input_or_id;   // +0x10 (input methods only)
    int32_t /* or i64 / bool */ output;       // +0x10 or +0x14
};

// Vector-output (ChipBounds, ProcessBounds, ProcessIds, LogiDeviceIdsOnProcess,
//                ChipsPerProcessBounds, ChipCoordAndIdxForLogiDevice, Subslice, ReplaceHostBounds)
struct PJRT_TpuTopology_<Vector>_Args {
    size_t                     struct_size;       // +0x00
    PJRT_TopologyDescription*  topology;          // +0x08
    int64_t                    buffer_capacity;   // +0x10  caller-supplied cap
    int32_t*                   buffer;            // +0x18  caller-allocated
    size_t                     actual_count;      // +0x20  written by callee
};
```

The vector body always writes `actual_count` (`+0x20`) first, then bounds-checks against `buffer_capacity` (`+0x10`): if `actual > capacity` it returns `xla::InvalidArgument("<class>: needed %zu, provided %d", ...)` without writing the buffer. A caller therefore probes the required size by calling once with `capacity == 0`, reading `actual_count`, then reallocating and calling again — the classic two-pass C-ABI vector idiom.

### Helper Methods — Downcast to TpuTopology

Five methods (`ReplaceHostBounds`, `IsEnhancedBarrierEnabled`, `HasLimitedIciConnectivity`, `IsReachableOverLimitedIci`, `GetRoutingStrategy`) read TPU-private fields not in the abstract vtable. They replace the vtable call with a free function in `pjrt::tpu_topology_extension::*` that downcasts via `xla::GetTpuTopologyFromDescription(topo)` (`0xF84AC20`) to `tpu::TpuTopology*` and reads private offsets directly:

```c
absl::StatusOr<bool> tpu_topology_extension::IsEnhancedBarrierEnabled(
        const xla::PjRtTopologyDescription& topo) {       // helper @ 0xE6E4C40
    auto t = xla::GetTpuTopologyFromDescription(&topo);    // downcast (StatusOr)
    if (!t.ok()) return t.status();
    return (*t)->_has_eb_field   /* TpuTopology+0x190 */
         ? (*t)->enhanced_barrier_enabled /* +0x188 */ : false;
}
```

`ReplaceHostBounds` is heavier — it re-renders the topology under a constrained host bounding box via `tpu::TpuTopologySerdes::Distill` (`0x20805BC0`). These five are the reason the extension exists: they expose ICI/barrier facts that have no place in the portable XLA abstraction.

---

## Serialization & Deserialization

### Serialize (slot 92)

`PJRT_TopologyDescription_Serialize` produces an **XLA-portable** protobuf blob — the on-wire bytes can be parsed by any PJRT plugin, not just libtpu.

```c
function PJRT_TopologyDescription_Serialize(args):       // 0xF870320, slot 92
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_TopologyDescription_Serialize_Args", 0x27, 0x38, args[0]) != 1:
        return new PJRT_Error{...}
    topo = (args[2])->impl
    proto_or = topo->vtable[+0xF8]()                      // StatusOr<xla::PjRtTopologyDescriptionProto>
    if not proto_or.ok(): return new PJRT_Error{proto_or.status}
    s = new std::string
    MessageLite::SerializeToString(&proto_or.value, s)    // 0x210580C0
    h = operator new(24)                                  // PJRT_SerializedTopology
    h->data    = s->data();  h->size = s->size()
    h->deleter = &PJRT_TopologyDescription_Serialize::$_0  // lambda: delete (std::string*)
    args->serialized_out        = {h->data, h->size}
    args->serialized_handle_out = h
    return NULL
```

```c
struct PJRT_SerializedTopology {                          // sizeof = 24
    /* +0x00 */ const char* data;                          // heap std::string contents
    /* +0x08 */ size_t      size;
    /* +0x10 */ void      (*deleter)(PJRT_SerializedTopology*);  // lambda $_0 -> delete std::string*
};
```

> **GOTCHA —** the caller **must** invoke `serialized->deleter(serialized)` to reclaim the `std::string` body. The blob is not owned by the wrapper; nothing else frees it. The deleter lambda (`...Serialize::$_0::__invoke`, symbol-only) calls `delete std::string*` (LOW: body identified by symbol, not decompiled).

### Deserialize (slot 119)

`PJRT_TopologyDescription_Deserialize` is **not** TPU-overridden — it is the generic `pjrt::PJRT_TopologyDescription_Deserialize`, which routes through the compiler registry by platform name. This is how a topology serialized on one host reconstitutes on another.

```c
function PJRT_TopologyDescription_Deserialize(args):     // 0xF870B80, slot 119
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_TopologyDescription_Deserialize_Args", 0x28, 0x28, args[0]) != 1:
        return new PJRT_Error{...}
    proto = xla::PjRtTopologyDescriptionProto{}           // stack-construct
    if not MessageLite::ParseFromString(&proto, {args->bytes, args->size}):  // 0x21057460
        return new PJRT_Error{InvalidArgument}
    name = proto.platform_name()                          // SSO string
    comp_or = xla::GetDefaultPjRtCompiler(name)           // 0x1D169DA0 -> StatusOr<PjRtCompiler*>
    if not comp_or.ok():
        return new PJRT_Error{ "Topology platform '%s' not supported by any "
                               "compiler. Are you missing a compiler library?" }
    topo_or = comp_or.value->vtable[+0x20]( serialized_bytes )  // DeserializeTopology
    if not topo_or.ok(): return new PJRT_Error{topo_or.status}
    args[5] = CreateWrapperDeviceTopology(topo_or.value)  // 0xF873260 (const* overload), out at +0x28
    return NULL
```

> **NOTE —** the wire bytes are XLA-portable but the *post-parse* topology depends on a compiler being registered for `platform_name`. libtpu registers its compiler under `"tpu"` and per-generation keys; deserializing a TPU topology on a CPU-only host with no TPU compiler library returns the `"not supported by any compiler"` error. The `PjRtTopologyDescriptionProto` schema is only partly recovered — `platform_name`, `platform_version`, and an opaque `serialized` payload byte-string are confirmed; the full proto field-tag map is **not traced** (the descriptor lives near `.data` `0x1D179E40` and needs a proto-descriptor walk).

---

## Slice & Platform Configs

`GetSliceConfig`, `GetSliceConfigs`, and `GetDefaultPlatformConfig` (extension slots 28–30) do not call the topology vtable — they consult a static singleton `kPlatformConfigs` @ `0x224C4108` (`.bss`, guard `0x224C4128`), a `flat_hash_map<string, TpuPlatformConfig>` built lazily by `GetAllPlatformConfigs()::$_0::operator()` @ `0xE6E1DA0`, and convert into wire-format C structs via `ConvertSliceConfig` @ `0xE6E1BC0`.

```c
struct PJRT_TpuTopology_SliceConfig {                     // sizeof = 0x1C
    int64_t  n_dims;          // +0x00  mirrors chip_bounds rank (0..3 dims)
    int32_t  chip_bounds[4];  // +0x08  per-axis chip extent
    bool     ici_polarity[3]; // +0x18  per-axis ICI wrap polarity
};
```

The `SliceConfig` proto (proto2, fields `repeated int32 chip_bounds`, `repeated bool ici_polarity` of matching rank) and the richer `TpuPlatformConfig` proto (nested `SliceConfig` plus `core_layout`, `sparse_cores_per_chip`, `host_per_chip`, `embedding_cores_per_chip` integer enums) are the AOT-compile knobs: they tell the compiler the per-generation silicon geometry without a live device. The canonical key set (`"tpu_v4"`, `"tpu_v5e"`, `"tpu_v6e"`, ...) maps to the per-generation core/chip counts catalogued on the SparseCore silicon pages.

---

## Lifetime Semantics

| Path | Slot | Returns | Destroy responsibility |
|---|---|---|---|
| `_Create` | 87 | owning wrapper (112 B, new) at args `+0x30` | caller, via `_Destroy` exactly once |
| `_Deserialize` | 119 | owning wrapper (112 B, new) at args `+0x28` | caller, via `_Destroy` exactly once |
| `Client_TopologyDescription` | 100 | **borrowed** handle | the live `PJRT_Client` (do NOT call `_Destroy`) |

```c
function PJRT_TopologyDescription_Destroy(args):         // 0xF870040, slot 88
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_TopologyDescription_Destroy_Args", 0x25, 0x18, args[0]) != 1:
        return new PJRT_Error{...}
    if args->topology != NULL:
        ~PJRT_TopologyDescription(args->topology)         // 0xF8700C0
        free(args->topology, /*size=*/112)
    return NULL                                           // success
```

The destructor (`0xF8700C0`) frees, in order: the cached attribute span, the `PJRT_DeviceDescription` vector, the device-pointer span, the device shared-ptr vector, then runs the underlying topology's virtual destructor (vtable `+0x08`). There is **no reference counting and no shared-pointer semantics across the C ABI** — a Create/Deserialize wrapper must be destroyed exactly once.

> **GOTCHA —** calling `PJRT_TopologyDescription_Destroy` on the handle returned by `PJRT_Client_TopologyDescription` (slot 100) is a use-after-free: that topology is owned by the live client and freed by `PJRT_Client_Destroy`. The C ABI carries no flag distinguishing owned from borrowed handles — the distinction lives only in which slot produced the pointer. See [Client, Device & Topology](client-and-device.md#topology-description) for the borrow path.

Thread safety: the underlying topology is `const` after construction and the wrapper's cached spans are never mutated, so concurrent read calls (`Attributes`, `Fingerprint`, `GetDeviceDescriptions`, the extension getters) are safe. `Destroy` is **not** safe to call concurrently with any other slot on the same wrapper.

---

## Related Components

| Component | Relationship |
|---|---|
| `PJRT_Api` 140-slot table | Holds the 10 generic topology slots (87–93, 100, 119, 138); slot→address map on the vtable page |
| `xla::PjRtTopologyDescription` | The abstract impl object the wrapper owns; a `tpu::TpuTopology` subclass on TPU |
| Extension chain | The type-16 extension is one of 17 extensions hung off `extension_start` |
| `kPlatformConfigs` singleton | Per-generation `TpuPlatformConfig` table consulted by the slice-config methods |
| `xla::PjRtCompiler` registry | Backs `_Deserialize`: `GetDefaultPjRtCompiler(platform_name)` → `DeserializeTopology` |

## Cross-References

- [Client, Device & Topology](client-and-device.md) — the *live-client* device/topology accessors and `PJRT_Client_TopologyDescription` (slot 100); contrast: that page owns the borrowed topology, this page owns the standalone AOT topology
- [API & vtable Reconstruction](api-vtable-reconstruction.md) — the full 140-slot table, the `@0x227BA840` `.lbss` storage, and the `ActualStructSizeIsGreaterOrEqual` backward-compat mechanism every slot opens with
- [Extension Chain](extension-chain.md) — how `extension_start` links the 17 extensions; where the type-16 entry sits in the chain and how a consumer walks to it
- [Overview](overview.md) — C-API version, the extension-chain idea, and the `GetPjrtApi` population path that runs `CreateTpuTopologyExtension`
- [Executable & Execution](executable-execution.md) — `PJRT_Compile` consumes a topology for ahead-of-time compilation; the AOT topology built here feeds that path
- [ICI Topology Discovery](../ici/topology-discovery.md) — the on-device runtime discovery of the same torus geometry this page exposes statically for AOT compile
- [SparseCore Architecture](../sparsecore/architecture.md) — Part-IV silicon geometry: the per-generation core/chip counts that `CoreCountPerChip` and the `TpuPlatformConfig` table report
