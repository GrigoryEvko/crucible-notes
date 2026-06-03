# PJRT Client, Device & Topology

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`), exporting PJRT C-API **v0.103**. Other wheels will differ.*

## Abstract

`libtpu.so` is a PJRT plugin: it exports a single C symbol, `GetPjrtApi`, that hands back a 140-slot vtable of function pointers (the `PJRT_Api` struct, documented on [API & vtable Reconstruction](api-vtable-reconstruction.md)). Frameworks — JAX, PyTorch/XLA — drive the whole TPU runtime through that flat C ABI. This page documents the *behavior* behind the client, device, device-description, memory-space, and topology slots: what each slot does to the heap objects it wraps, and how a reimplementer would rebuild the call graph from `PJRT_Client_Create` down to a fully-wired `PJRT_Client` object holding device, memory-space, and topology handles.

The shape mirrors upstream `pjrt_c_api_wrapper_impl.cc` exactly — the binary still carries that path string (referenced from `pjrt::CreateWrapperClient` at `0xf872060`). Three families of objects live behind the C ABI: the **wrapper** structs (`PJRT_Client`, `PJRT_Device`, `PJRT_Memory`, `PJRT_DeviceDescription`, `PJRT_TopologyDescription`), each a small POD allocated with `operator new` that holds a raw pointer to the real C++ implementation; the **implementation** objects (`xla::PjRtClient` and its TPU subclass `xla::TpuClient`, `xla::PjRtDevice`, `xla::PjRtMemorySpace`, `xla::PjRtDeviceDescription`); and the **options** plumbing that turns a flat `PJRT_NamedValue[]` array into a typed `PjRtTpuClientConfig`. Only five of the 140 slots are TPU-specialized (`tpu_plugin::*`); the device/memory/topology *accessors* are all generic `pjrt::PJRT_*` wrappers shared with the CPU and GPU plugins — the TPU specialization happens entirely inside the implementation objects those wrappers call.

For reimplementation, the contract is:

- **The options-kv ingest** in `PJRT_Client_Create`: parse `PJRT_NamedValue[]` → flat-hash-map, merge a 10-entry default-options table, validate, parse into a `PjRtTpuClientConfig`, then dispatch through `GetTpuPjRtClient` plus the MegaScale / TfPjRtClient client-selection branches.
- **The wrapper-client construction** in `CreateWrapperClient`: a 208-byte `PJRT_Client` that eagerly materializes the device vector, the addressable-device subset, the memory-space vector, and two `PjRt*→PJRT_*` lookup hash-maps — so every later accessor is an O(1) field read.
- **The accessor pattern**: every slot opens with `ActualStructSizeIsGreaterOrEqual(name, min, current, args->struct_size)`; on success it is either a field read from the wrapper or a single virtual call into the implementation object.

| | |
|---|---|
| **Plugin entry** | `GetPjrtApi` @ `0xE6A83A0` → `pjrt::tpu_plugin::GetTpuPjrtApi` @ `0xE6AA440` |
| **Client create** | `pjrt::tpu_plugin::PJRT_Client_Create` @ `0xE6A8840` (slot 15, TPU-specialized) |
| **Client backend** | `xla::GetTpuPjRtClient(const PjRtTpuClientConfig&)` @ `0xF8008C0` → `xla::TpuClient` |
| **Wrapper build** | `pjrt::CreateWrapperClient(unique_ptr<xla::PjRtClient>)` @ `0xF872060` |
| **`PJRT_Client` size** | 208 bytes (`0xD0`), `operator new` |
| **C-API version** | v0.103 (`{major=0, minor=103}`) |
| **Wrapper impl path** | `third_party/tensorflow/compiler/xla/pjrt/c/pjrt_c_api_wrapper_impl.cc` (in-binary string) |

---

## PJRT_Client_Create — Options Ingest and Backend Selection

### Purpose

`PJRT_Client_Create` is the plugin's heaviest single function: it converts the framework's untyped option array into a typed TPU client config, brings up the underlying `xla::TpuClient` (which probes hardware, builds the device list, and connects to the TPU runtime), optionally layers a MegaScale or TF-PjRt client on top, and finally wraps the result for the C ABI. It is one of only five TPU-overridden slots — the generic `pjrt::PJRT_Client_Create` is *not* installed; slot 15 points at `tpu_plugin::PJRT_Client_Create` (`0xE6A8840`).

### Entry Point

```text
GetPjrtApi (0xE6A83A0)                                  ── exported thunk, 5-byte JMP
  └─ pjrt::tpu_plugin::GetTpuPjrtApi (0xE6AA440)         ── one-shot __cxa_guard init of the 140-slot table
       └─ slot 15 = pjrt::tpu_plugin::PJRT_Client_Create (0xE6A8840)
            ├─ pjrt::ConvertFromPjRtNamedValueList        ── PJRT_NamedValue[] → flat_hash_map<string,variant>
            ├─ pjrt::ValidateCreateOptions                ── checks keys against the 10-entry default table
            ├─ libtpu::telemetry::UptimeMetric::MaybeRefreshInstance  ── if ml_framework_name/version present
            ├─ pjrt::tpu_plugin::ParseTpuClientConfig     ── typed PjRtTpuClientConfig
            ├─ xla::GetTpuPjRtClient (0xF8008C0)          ── builds xla::TpuClient
            ├─ xla::MegaScalePjRtClient::CreateMegaScalePjRtClient   ── multi-slice overlay (conditional)
            ├─ xla::TfPjRtClient::CreateTfPjRtClient       ── if use_tf_pjrt_client == 1
            └─ pjrt::CreateWrapperClient (0xF872060)       ── wraps into the 208-byte PJRT_Client, stores at args[8]
```

### Algorithm

```c
function PJRT_Client_Create(args):                       // 0xE6A8840
    // args[0]=struct_size, args[2]=PJRT_NamedValue* options,
    // args[3]=num_options, args[8]=PJRT_Client** out.
    st = ActualStructSizeIsGreaterOrEqual(               // 0xE6A8840:+0
            "PJRT_Client_Create_Args", min=23, current=88, args[0])
    if st != 1:
        return new PJRT_Error{st}                        // caller compiled too old

    // (1) untyped ingest: caller's flat array -> typed map keyed by string.
    user_opts = ConvertFromPjRtNamedValueList(args[2], args[3])  // -> flat_hash_map<string,variant>

    // (2) build the DEFAULT-OPTIONS table inline on the stack: 10 entries,
    //     each a {key, PJRT_NamedValue_Type, default} record. Keys are
    //     pjrt::tpu_configs::k* string constants. See "Default Options" below.
    default_opts = { kMaxInflightComputations:i64=1, kUseTfPjrtClient:i64=1,
                     kMlFrameworkName:str="", kMlFrameworkVersion:str="",
                     kUseGlobalTpuSystem:bool, kTpuAllowAsyncAllocations:bool,
                     kExecutableCompatibilityCheckOnDeserialization:bool,
                     kThrottleLowPriorityHostTransfers:bool,
                     kPinnedHostAllocationMode:str, kPremappedBufferSize:i64,
                     kMaximumPremappedBufferSizeForTransfersInBytes:i64,
                     kNumPremappedPartitions:i64, kSkipMegascalePjrtClient:bool }
    merged = insert_range(default_opts, user_opts)       // user values win on key collision

    // (3) validate: reject unknown keys / wrong types against default table.
    vstatus = ValidateCreateOptions(merged, default_table)   // 0xE6A8840:LABEL_15
    if vstatus != 1:
        return new PJRT_Error{vstatus}

    // (4) telemetry: only if ml_framework_name AND ml_framework_version set.
    if FLAGS_enable_runtime_uptime_telemetry and merged has both keys:
        UptimeMetric::MaybeRefreshInstance(framework_name, framework_version)

    // (5) typed parse: produce a PjRtTpuClientConfig from the merged map.
    cfg = ParseTpuClientConfig(merged)                   // LABEL_69
    if cfg is error:
        return new PJRT_Error{cfg.status}

    // (6) bring up the underlying TpuClient. This is the heavy step:
    //     hardware enumeration, runtime attach, device/core construction.
    client = GetTpuPjRtClient(cfg)                       // 0xF8008C0 -> xla::TpuClient
    if client is error:
        return new PJRT_Error{client.status}

    // (7) MegaScale overlay: skip if kSkipMegascalePjrtClient,
    //     env SKIP_MEGASCALE_PJRT_CLIENT, or FLAGS_megascale_port < 0.
    skip_ms = merged[kSkipMegascalePjrtClient] | (getenv("SKIP_MEGASCALE_PJRT_CLIENT") != NULL)
    if FLAGS_megascale_port >= 0 and not skip_ms:
        client = MegaScalePjRtClient::CreateMegaScalePjRtClient(client)

    // (8) optional TF-PjRt wrapping (legacy client). use_tf_pjrt_client default = 1.
    if merged[kUseTfPjrtClient] == bool && value == 1:
        client = TfPjRtClient::CreateTfPjRtClient(client)
    // (variant type-mismatch on any lookup -> __throw_bad_variant_access -> LABEL_125)

    // (9) wrap for the C ABI and hand back through the out-param.
    args[8] = CreateWrapperClient(client)                // 0xF872060
    return NULL                                          // success
```

> **GOTCHA —** the `merged[kUseTfPjrtClient]` and `merged[kSkipMegascalePjrtClient]` lookups read the variant *type tag* before the value (`*(byte)(slot+48)`). If a caller passes the right key with the wrong PJRT_NamedValue type, the code falls to `LABEL_125` → `std::__throw_bad_variant_access`, which aborts the process rather than returning a `PJRT_Error`. Validation in step (3) is the only guard; a reimplementation must type-check during `ValidateCreateOptions` or it inherits this crash.

### Default Options Table

The default table is built field-by-field on the stack (no static data section — it is constructed inline with `vmovaps` from the `pjrt::tpu_configs::k*` string constants and a per-entry type byte). These are the *only* option keys libtpu accepts; `ValidateCreateOptions` rejects anything else.

| Key (`pjrt::tpu_configs::`) | Type | Default | Effect |
|---|---|---|---|
| `kMaxInflightComputations` | i64 | **1** | Concurrent in-flight executions per device |
| `kUseTfPjrtClient` | i64 | **1** | Wrap in `TfPjRtClient` (legacy compat shim) |
| `kMlFrameworkName` | str | **""** | Telemetry tag (e.g. `"JAX"`) |
| `kMlFrameworkVersion` | str | **""** | Telemetry tag; pairs with name to fire `UptimeMetric` |
| `kUseGlobalTpuSystem` | bool | — | Attach to a shared/global TPU system vs. a private one |
| `kTpuAllowAsyncAllocations` | bool | — | Permit async device allocation in the allocator |
| `kExecutableCompatibilityCheckOnDeserialization` | bool | — | Verify exec compat on deserialize-and-load |
| `kThrottleLowPriorityHostTransfers` | bool | — | Rate-limit low-priority H2D/D2H transfers |
| `kPinnedHostAllocationMode` | str | — | Pinned-host allocator policy |
| `kPremappedBufferSize` | i64 | — | DMA-premapped staging region size |
| `kMaximumPremappedBufferSizeForTransfersInBytes` | i64 | — | Cap on premapped transfer buffers |
| `kNumPremappedPartitions` | i64 | — | Premapped-region partition count |
| `kSkipMegascalePjrtClient` | bool | — | Force-disable the MegaScale multi-slice overlay |

> **NOTE —** the type byte stored alongside each default key (`1`=bool? `4`=i64, etc. in the decompile) is the `PJRT_NamedValue_Type` enum. The `i64`-typed booleans (`kUseTfPjrtClient` is type 4 with value 1) are deliberate: this build encodes "use the TF client" as an integer flag, not a `PJRT_NamedValue_kBool`, so a caller passing a bool here trips the `bad_variant_access` path above.

### Backend Selection — Three Client Layers

`GetTpuPjRtClient` (`0xF8008C0`) is the real entry into the TPU runtime; it delegates to a static `GetTpuPjRtClientInternal` that owns a `tfrt::HostContext` and a `TpuSystemState`, then constructs `xla::TpuClient`. The `TpuClient` constructor signature (`0xF801980`) is:

```c
TpuClient(const PjRtTpuClientConfig& cfg,
          int process_index,
          std::string platform_version,
          std::vector<unique_ptr<TpuDevice>> devices,
          ...)
```

so the device list and process index are decided inside the runtime bring-up, not by the C-API layer. The C-API layer then chooses how many wrapper layers to stack:

| Condition | Resulting client | Symbol |
|---|---|---|
| Always | base `xla::TpuClient` | `xla::GetTpuPjRtClient` @ `0xF8008C0` |
| `megascale_port >= 0` and not skipped | `MegaScalePjRtClient` wrapping the base | `CreateMegaScalePjRtClient` @ `0xE6EA680` |
| `use_tf_pjrt_client == 1` (default) | `TfPjRtClient` wrapping whatever (8) produced | `TfPjRtClient::CreateTfPjRtClient` |

The MegaScale layer is the multi-slice (cross-pod) overlay; it consumes the base `unique_ptr<TpuClient>` and an optional `MultiSliceConfig`. The TF layer is a legacy compatibility client. Because both consume-and-replace `client`, the final wrapped object can be one, two, or three layers deep — but the C ABI sees only the outermost `xla::PjRtClient*`.

---

## CreateWrapperClient — Building the PJRT_Client

### Purpose

`CreateWrapperClient` (`0xF872060`) takes ownership of the `unique_ptr<xla::PjRtClient>` and materializes the 208-byte `PJRT_Client` wrapper that the C ABI hands out. Its defining behavior is **eager caching**: it walks the implementation client's device and memory lists *once*, allocates `PJRT_Device` / `PJRT_Memory` wrapper objects for each, and builds two flat-hash-maps (`xla::PjRtDevice* → PJRT_Device*` and `xla::PjRtMemorySpace* → PJRT_Memory*`) plus the addressable-device subset vector. Every later device/memory accessor is therefore a pointer-chase with no allocation.

### Wrapper Layout (`PJRT_Client`, 208 bytes)

Reconstructed from the field stores in `CreateWrapperClient`; offsets are within the `operator new(0xD0)` block. The decompiler indexes the object as a `_QWORD[]`, so qword index `n` = byte offset `8n`.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `client` | `+0x00` | `xla::PjRtClient*` (owned) | The implementation object; vtable drives everything |
| (reserved) | `+0x08` | `_QWORD[3]` (qw 1..3) | Scratch/auxiliary fields written during construction; not read by the device accessors |
| `devices` | `+0x20` | `vector<PJRT_Device>` (begin/end/cap at qw 4..6) | All wrapper devices; each entry 9 qwords (72 B). `PJRT_Client_Devices` reads begin=qw4, end=qw5 |
| `addressable_devices` | `+0x38` | `vector<PJRT_Device*>` (begin/end/cap at qw 7..9) | Subset where `device->IsAddressable()` is true. `PJRT_Client_AddressableDevices` reads begin=qw7, end=qw8 |
| `device_map` | `+0x50` | `flat_hash_map<PjRtDevice*, PJRT_Device*>` (qw 10..13) | O(1) impl→wrapper; read by `GetCDevice` at wrapper+0x50 |
| `memories` | `+0x70` | `vector<PJRT_Memory>` (qw 14..16) | All wrapper memory spaces; entries 5 qwords (40 B) |
| `addressable_memories` | `+0x88` | `vector<PJRT_Memory*>` (qw 17..19) | Per-device addressable subset |
| `memory_map` | `+0xA0` | `flat_hash_map<PjRtMemorySpace*, PJRT_Memory*>` (qw 20..23) | O(1) impl→wrapper; read by `GetCMemory` at wrapper+0xA0 |

### Algorithm

```c
function CreateWrapperClient(client_uptr):               // 0xF872060
    w = operator new(0xD0)                                // 208-byte PJRT_Client
    PJRT_Client::PJRT_Client(w, client_uptr)              // store owned client at +0x00

    // (1) DEVICES: walk client->devices() (vtable+40), reserve, wrap each.
    devs = w.client->devices()                            // vtable +40
    w.devices.reserve(devs.size())
    for d in devs:
        attrs = d->description()->Attributes()            // PjRtDevice vtable+32 -> desc vtable+56
        pjrt_dev = PJRT_Device{ device=d, description_attrs=PopulatePjrtAttributes(attrs) }
        w.devices.push_back(pjrt_dev)
        if d->IsAddressable():                            // PjRtDevice vtable+24
            w.addressable_devices.push_back(&w.devices.back())
        w.device_map[d] = &w.devices.back()               // SwissTable insert

    // (2) INVARIANT CHECK (fatal on mismatch):
    CHECK(w.addressable_devices.size() == w.client->addressable_device_count())
        // vtable+32, file pjrt_c_api_wrapper_impl.cc:3435, msg quoted below

    // (3) MEMORIES: walk client->memory_spaces() (vtable+80), wrap each.
    mems = w.client->memory_spaces()                      // vtable +80
    w.memories.reserve(mems.size())
    for m in mems:
        w.memories.push_back(PJRT_Memory{ memory=m })
        w.memory_map[m] = &w.memories.back()              // CHECK iter != c_memory_map.end()

    // (4) cross-link addressable memories per addressable device
    //     (client->addressable_memory_spaces, vtable+144) and
    //     fix up each device's addressable-memory list via the device_map
    //     (CHECK "iter != c_device_map.end()", line 101 / 110).
    for d in w.addressable_devices:
        for m in d->memory_spaces():                      // vtable+144
            d_wrapper.addressable_memories.push_back(memory_map[m])
    return w
```

> **GOTCHA —** `CreateWrapperClient` aborts (via `LogMessageFatal` in `pjrt_c_api_wrapper_impl.cc`) on three invariants, not error-returns: `addressable_devices.size() == client->addressable_device_count()` (line 3435), `iter != c_memory_map.end()` (line 110), and `iter != c_device_map.end()` (line 101). A reimplementation of `xla::PjRtClient` whose `devices()`, `addressable_device_count()`, and `memory_spaces()` are mutually inconsistent will crash the host process at client-create time, before any execution.

> **QUIRK —** device attributes are snapshotted at wrap time (`PopulatePjrtAttributes` copies the description's attribute map into the `PJRT_Device`). The C-ABI `PJRT_DeviceDescription_Attributes` slot reads this cached copy, not the live implementation map — attribute mutation after `Client_Create` is invisible across the boundary.

---

## Device Enumeration & Lookup

### Purpose

Once the wrapper holds its eager caches, the client-level device slots are trivial: `PJRT_Client_Devices` and `PJRT_Client_AddressableDevices` return spans into the cached vectors; `PJRT_Client_LookupDevice` / `LookupAddressableDevice` index by global device id. None allocate; none touch the implementation object.

### Algorithm

```c
function PJRT_Client_Devices(args):                      // 0xF85F600, slot 20
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_Client_Devices_Args", 24, 40, args[0]) != 1:
        return new PJRT_Error{...}
    client = args[2]                                      // PJRT_Client*
    args[3] = client->devices.begin                       // +0x20 within wrapper = qw 4
    args[4] = client->devices.end                         // +0x28 = qw 5; count = (end-begin)/sizeof(PJRT_Device)
    return NULL
```

`PJRT_Client_AddressableDevices` (`0xF85F660`, slot 21) is byte-identical except it returns the `addressable_devices` span (qw 7..8). The caller computes the count by pointer subtraction; the entry stride is `sizeof(PJRT_Device)` (72 bytes) for `Devices` and 8 bytes (a pointer) for `AddressableDevices` — a subtle distinction a reimplementer must preserve because PJRT clients iterate the two arrays with different element types.

### Function Map

| Slot | Function | Addr | Behavior | Confidence |
|---|---|---|---|---|
| 20 | `PJRT_Client_Devices` | `0xF85F600` | span of `PJRT_Device` (begin/end) | CERTAIN |
| 21 | `PJRT_Client_AddressableDevices` | `0xF85F660` | span of `PJRT_Device*` | CERTAIN |
| 22 | `PJRT_Client_LookupDevice` | `0xF85F6C0` | id → `PJRT_Device*` via cached lookup | HIGH |
| 23 | `PJRT_Client_LookupAddressableDevice` | `0xF85F880` | id → addressable `PJRT_Device*` | HIGH |
| 18 | `PJRT_Client_ProcessIndex` | `0xF85F440` | int from `client->process_index()` | HIGH |
| 17 | `PJRT_Client_PlatformName` | `0xF85F4A0` | string view (`"tpu"`) | HIGH |
| 19 | `PJRT_Client_PlatformVersion` | `0xF85F500` | string view (TPU gen / topology) | HIGH |

---

## Device & DeviceDescription Accessors

### Purpose

A `PJRT_Device` holds the implementation `xla::PjRtDevice*` at `PJRT_Device+0x00`, with an inlined `PJRT_DeviceDescription` sub-object at `PJRT_Device+0x08`; that description in turn holds the `xla::PjRtDeviceDescription*` it dispatches through. The accessor split mirrors upstream PJRT: `PJRT_Device_*` slots are about runtime state (addressability, memory, stats); `PJRT_DeviceDescription_*` slots are about static identity (id, process index, kind, coords). `PJRT_Device_GetDescription` (`0xF8659A0`, slot 34) is the bridge — it simply returns `PJRT_Device+0x08`, a pointer to the inlined description sub-object (`a1[3] = a1[2] + 8`).

### Algorithm — the accessor idiom

Every `DeviceDescription` accessor follows one shape: validate the args struct size, then make a single virtual call through the implementation object's vtable and store the result in the out-field.

```c
function PJRT_DeviceDescription_Id(args):                // 0xF865360, slot 28
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_DeviceDescription_Id_Args", 30, 28, args[0]) != 1:
        return new PJRT_Error{...}
    // args[2] = PJRT_DeviceDescription*; **(args+16) = xla::PjRtDeviceDescription*
    desc = *(args[2])                                     // load impl ptr
    args[3] = desc->vtable[+16]( desc )                   // virtual: id() -> int32
    return NULL
```

`ProcessIndex` (slot 29) calls vtable `+...`, `Kind` (slot 31) returns a string view (the device-kind string, e.g. `"TPU v4"`), `DebugString`/`ToString` return formatted strings. **Coordinates** are not a dedicated v0.103 slot — torus `(x,y,z)` coords are exposed through `PJRT_DeviceDescription_Attributes` (slot 30) as named attributes, populated from the description's attribute map and cached at wrap time (see `PopulatePjrtAttributes` above). A reimplementer wiring a TPU device must surface `coords` / `core_on_chip` / `slice_index` as attribute entries, not as first-class accessor slots.

### Function Map

| Slot | Function | Addr | Returns | Confidence |
|---|---|---|---|---|
| 28 | `PJRT_DeviceDescription_Id` | `0xF865360` | `int32` device id (vtable +16) | CERTAIN |
| 29 | `PJRT_DeviceDescription_ProcessIndex` | `0xF8653C0` | `int32` process index | HIGH |
| 30 | `PJRT_DeviceDescription_Attributes` | `0xF865420` | named-value list (carries coords) | HIGH |
| 31 | `PJRT_DeviceDescription_Kind` | `0xF865480` | string view (device kind) | HIGH |
| 32 | `PJRT_DeviceDescription_DebugString` | `0xF865500` | string view | HIGH |
| 33 | `PJRT_DeviceDescription_ToString` | `0xF8658A0` | string view | HIGH |
| 34 | `PJRT_Device_GetDescription` | `0xF8659A0` | `PJRT_DeviceDescription*` | CERTAIN |
| 35 | `PJRT_Device_IsAddressable` | `0xF865A00` | bool (vtable +24) | CERTAIN |
| 36 | `PJRT_Device_LocalHardwareId` | `0xF865A60` | `int32` local hw id | HIGH |
| 37 | `PJRT_Device_AddressableMemories` | `0xF865AC0` | span of `PJRT_Memory*` | HIGH |
| 38 | `PJRT_Device_DefaultMemory` | `0xF865B20` | `PJRT_Memory*` (vtable +152 → `GetCMemory`) | CERTAIN |
| 39 | `PJRT_Device_MemoryStats` | `0xF865CE0` | HBM stats struct | MEDIUM |

> **NOTE —** the `(min, current)` pair in each `ActualStructSizeIsGreaterOrEqual` call is the backward-compat envelope: `PJRT_DeviceDescription_Id_Args` accepts down to 30 bytes against a current 28 — note `min > current` here, an artifact of how the v0.103 args struct shrank a reserved field relative to the recorded minimum; the check is `actual >= min`. A reimplementer should copy the exact constants per slot rather than assume a uniform rule.

---

## Device ↔ Memory-Space Wiring

### Purpose

PJRT memory spaces (`PJRT_Memory`, wrapping `xla::PjRtMemorySpace*`) model the distinct memory kinds a TPU device can address (device HBM, pinned host, etc.). The wiring is bidirectional and entirely pre-cached by `CreateWrapperClient`: the client holds the full `memories` vector and a `memory_map`; each addressable device holds its own `addressable_memories` subset.

### Algorithm — DefaultMemory

`PJRT_Device_DefaultMemory` shows the impl→wrapper translation through the client's cached `memory_map`:

```c
function PJRT_Device_DefaultMemory(args):                // 0xF865B20, slot 38
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_Device_DefaultMemory_Args", 30, 32, args[0]) != 1:
        return new PJRT_Error{...}
    device = *(args[2])                                   // xla::PjRtDevice*
    statusor = device->vtable[+152]()                     // default_memory_space() -> StatusOr<PjRtMemorySpace*>
    if statusor.ok():
        // translate impl memory-space ptr to its wrapper via the CLIENT's map.
        args[3] = GetCMemory( device->client (at +0x40), statusor.value )
        return NULL
    return new PJRT_Error{ statusor.status }              // refcount the StatusRep
```

`GetCMemory` looks up the `xla::PjRtMemorySpace*` in the owning client's `memory_map` (reachable from the device's back-pointer to its client at impl-offset `+0x40`) and returns the cached `PJRT_Memory*`. This is why the wrapper must build `memory_map` eagerly: a `default_memory_space()` result is an *implementation* pointer that has to be resolved back to its C-ABI wrapper, and doing that lookup lazily would require either a per-call allocation or a back-reference the C ABI does not carry.

### Memory Accessors (slots 40–44)

The contiguous `PJRT_Memory_*` slots 40–44 (`Id`, `Kind`, `DebugString`, `ToString`, `AddressableByDevices`) are generic vtable bounces into `xla::PjRtMemorySpace`, identical in shape to the device-description accessors. A sixth memory accessor, `PJRT_Memory_Kind_Id` (`0xF865F60`), is a *late addition* and lives at slot **102**, not in the 40–44 cluster. `PJRT_Memory_AddressableByDevices` (`0xF8660C0`, slot 44) returns the span of `PJRT_Device*` that can address the memory space, again resolved through the client's `device_map`. These are documented at the slot level on [Buffer & Memory](buffer-and-memory.md); this page owns only the *wiring* — the fact that the maps exist and that every memory↔device cross-reference is a cached lookup, never a live scan.

---

## Topology Description

### Purpose

A topology description is the device fabric's geometry *without* a live client — JAX uses it for ahead-of-time compilation, where it must know the TPU torus shape before any hardware is attached. libtpu exposes a two-tier surface: seven generic `PJRT_TopologyDescription_*` slots that any PJRT consumer understands, plus a TPU-specific extension (type id 16) carrying torus geometry. This page covers the client→topology link (slot 100); the generic slots and the extension are fully reconstructed on [Topology Description Extension](ext-topology-description.md).

### PJRT_Client_TopologyDescription

`PJRT_Client_TopologyDescription` (`0xF85F560`, slot 100) returns the topology already attached to a live client — a *borrowed* handle the caller must **not** destroy.

```c
function PJRT_Client_TopologyDescription(args):          // 0xF85F560, slot 100
    if ActualStructSizeIsGreaterOrEqual(
           "PJRT_Client_TopologyDescription_Args", 36, 32, args[0]) != 1:
        return new PJRT_Error{...}
    client = args[2]                                      // PJRT_Client*
    // the topology lives as a StatusOr<topology*> at client+192/+200:
    statusrep = *(client + 192)                           // StatusRep* (or the ok-sentinel)
    if (statusrep & 1) == 0:                              // heap StatusRep -> bump refcount
        atomic_inc(statusrep)
    if statusrep != ok_sentinel:                          // error case
        return new PJRT_Error{ statusrep }
    if *(client + 192) != ok_sentinel:                    // belt-and-braces ok check
        StatusOr::Helper::Crash(...)
    args[3] = *(client + 200)                             // borrowed PJRT_TopologyDescription*
    return NULL
```

> **QUIRK —** the topology handle is stored as a `StatusOr` *inside* the `PJRT_Client` wrapper (at `+192`/`+200`, past the device/memory caches), populated when the client is created. If the underlying TPU client could not produce a topology, this slot returns the stored error — so a failure to build the topology surfaces only when `PJRT_Client_TopologyDescription` is called, not at `Client_Create` time. The returned pointer is owned by the client; calling `PJRT_TopologyDescription_Destroy` on it is a use-after-free.

### Topology Slot Map (link, do not re-list)

| Concern | Slot(s) | Owner page |
|---|---|---|
| Generic create / destroy / serialize / 7-slot surface | 87–93 | [ext-topology-description](ext-topology-description.md) |
| Topology fingerprint, deserialize, late additions | 100, 119, 138 | [ext-topology-description](ext-topology-description.md) |
| TPU torus extension (type 16, 31 methods, 272 B) | extension chain | [ext-topology-description](ext-topology-description.md) |
| Client→topology borrow (this page) | 100 | here |

---

## Related Components

| Component | Relationship |
|---|---|
| `PJRT_Api` 140-slot table | The vtable these accessors populate; slot→address map lives on the vtable page |
| `xla::TpuClient` | The implementation client; built by `GetTpuPjRtClient`, owned by the wrapper |
| `MegaScalePjRtClient` / `TfPjRtClient` | Optional client layers stacked by `PJRT_Client_Create` |
| `PjRtTpuClientConfig` | Typed config produced by `ParseTpuClientConfig` from the merged options map |
| Extension chain | 17 extensions hung off `extension_start`; topology (type 16) is one |

## Cross-References

- [API & vtable Reconstruction](api-vtable-reconstruction.md) — the full 140-slot table: slot → libtpu impl VA, the `@0x227BA840` `.lbss` storage, the `ActualStructSizeIsGreaterOrEqual` compat mechanism
- [Overview](overview.md) — C-API version, the extension-chain idea, `GetPjrtApi` population path
- [Topology Description Extension](ext-topology-description.md) — generic topology slots + the TPU torus extension (type 16) this page links to from slot 100
- [Buffer & Memory](buffer-and-memory.md) — `PJRT_Buffer_*` and the `PJRT_Memory_*` slot-level details; the memory-space objects this page wires to devices
- [Executable & Execution](executable-execution.md) — `PJRT_LoadedExecutable_Execute` and friends, the hot path that consumes the devices enumerated here
- [Runtime Overview](../runtime/overview.md) — the `xla::TpuClient` runtime side: device construction, the TPU system state behind `GetTpuPjRtClient`
