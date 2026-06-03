# PJRT_Api Function-Pointer Table Reconstruction

> *All addresses on this page apply to libtpu 0.0.40 (`cp314`, `manylinux_2_31_x86_64`), build-id `89edbbe81c5b328a958fe628a9f2207d`. The PJRT C-API surface is **v0.103**. Other libtpu releases pin different minors and will renumber the late-addition slots.*

## Abstract

`libtpu.so` is a PJRT plugin: it exports one symbol, `GetPjrtApi`, and everything a host framework (JAX, PyTorch/XLA) ever calls reaches the TPU through the single `PJRT_Api` struct that symbol returns. That struct is a C ABI vtable — a flat array of 140 qword slots: five scalar header fields followed by 135 function pointers, one per `PJRT_*` C-API entry point. The host reads `api->PJRT_LoadedExecutable_Execute(args)` and the call lands in libtpu's text section. There is no C++ inheritance, no dispatch object, no per-version trampoline table — just this one struct, populated once, immutable for process lifetime.

This page is the **backbone reference** for the PJRT section: the complete, ordered, slot-by-slot reconstruction of that table. It owns the struct header decode (`struct_size`, `extension_start`, the embedded `PJRT_Api_Version`), the full 140-slot map (every slot's field name, the libtpu implementation symbol it points at, and that symbol's virtual address), and the populated-vs-injected map (which five slots are TPU-specialized and which 130 are stock XLA wrappers). The slot ordering matches public `xla/pjrt/c/pjrt_c_api.h` v0.103 exactly, including the late-addition cluster appended in feature-addition order beyond the original v0.40 surface.

The vtable is assembled by one function — `pjrt::CreatePjrtApi @ 0xf874160` — which does nothing but 140 stores into a caller-supplied buffer. That makes reconstruction unusually clean: the slot index *is* the array subscript in the initializer, and the field name *is* the assigned `pjrt::PJRT_*` symbol. Everything below is anchored to those stores. Per-area behavior (how each implementation actually works) lives on the area pages linked at the bottom; the extension linked list dangling off `extension_start` lives on [`extension-chain.md`](extension-chain.md). This page is the index they all hang from.

For reimplementation, the contract is:

- **The struct layout** — 1120 bytes = 140 × 8; five scalar header slots (0..4), then 135 function pointers (5..139), in the exact order below.
- **The version pin** — `PJRT_Api_Version = {struct_size=24, priv=NULL, major=0, minor=103}`, encoded as the qword `0x6700000000` at offset `+0x20`.
- **The slot-to-symbol map** — every populated slot resolves to a text-section symbol; no slot in this build is left null.
- **The five injection points** — the only slots not compile-time-fixed: passed in as parameters to `CreatePjrtApi` by its caller.
- **The per-slot backward-compat guard** — every wrapper's first act is `ActualStructSizeIsGreaterOrEqual`, the mechanism that lets an older-minor host call this v0.103 plugin.

| | |
|---|---|
| **Exported entry** | `GetPjrtApi @ 0xE6A83A0` (thunk → `GetTpuPjrtApi`) |
| **Builder** | `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xE6AA440` |
| **Initializer** | `pjrt::CreatePjrtApi @ 0xF874160` (140 stores, no logic) |
| **Storage** | `_ZZN4pjrt10tpu_plugin13GetTpuPjrtApiEvE8pjrt_api @ 0x227BA840` |
| **Section** | `[47] .lbss` (NOBITS, zero-filled at load, large-model) |
| **Size** | 1120 bytes = 140 qword slots |
| **C-API version** | v0.103 (`major=0, minor=103`) |
| **Populated slots** | 135 / 135 function pointers; 0 null |
| **TPU-specialized slots** | 5 (slots 8, 9, 15, 87, 103) |
| **Backward-compat guard** | `pjrt::ActualStructSizeIsGreaterOrEqual @ 0xF8A4EC0` |

### Slot count per area

| Area | Slots | Slot range |
|---|---|---|
| Header (scalars) | 5 | 0..4 |
| Error | 3 (+1 late) | 5..7, 137 |
| Plugin | 2 | 8..9 |
| Event | 5 (+2 late) | 10..14, 131..132 |
| Client | 13 (+~12 late) | 15..27, 98, 100, 108, 115..118, 120..121, 123, 134 |
| DeviceDescription | 6 | 28..33 |
| Device | 6 (+3 late) | 34..39, 126..127, 133 |
| Memory | 5 (+1 late) | 40..44, 102 |
| Executable | 10 (+6 late) | 45..54, 95..96, 99, 101, 129, 139 |
| LoadedExecutable | 8 (+2 late) | 55..62, 122, 135 |
| Buffer | 19 (+5 late) | 63..81, 97, 105, 125, 130, 136 |
| CopyToDeviceStream | 5 | 82..86 |
| TopologyDescription | 7 (+2 late) | 87..93, 119, 138 |
| Compile | 1 | 94 |
| ExecuteContext | 1 (+1 late) | 103..104 |
| AsyncHostToDeviceTransferManager | 9 (late) | 106..107, 109..114, 124 |
| AsyncTrackingEvent | 1 (late) | 128 |

---

## Population Path and Storage

### Purpose

The table is not a static initializer in a PROGBITS section — it is built lazily, once, on the first `GetPjrtApi` call, into zero-filled `.lbss`. Understanding the population path is the difference between a reimplementation that returns a correct table and one that races or returns garbage.

### Entry Point

```text
dlsym("GetPjrtApi")
  GetPjrtApi @ 0xE6A83A0                       ── thunk; tail-calls the builder
    └─ pjrt::tpu_plugin::GetTpuPjrtApi @ 0xE6AA440
         ── 16 __cxa_guard blocks build the .bss extensions (newest→oldest)
         ── final __cxa_guard wraps:
         └─ pjrt::CreatePjrtApi @ 0xF874160     ── 140 stores into &pjrt_api
              returns &pjrt_api = 0x227BA840
```

The exported symbol is a pure thunk. Its entire decompiled body is `return pjrt::tpu_plugin::GetTpuPjrtApi(a1);` (confirmed at `0xE6A83A0`, marked `thunk` by IDA). `GetTpuPjrtApi` is the orchestrator: it runs one Itanium-ABI `__cxa_guard`-protected constructor per extension to build the extension chain in `.bss`, then a final guard around `CreatePjrtApi`, which writes every slot. The function returns `&pjrt_api`, the address of a function-local static.

### Algorithm

```c
function GetTpuPjrtApi():                       // 0xE6AA440
    // one-shot init of the extension chain (covered on extension-chain.md);
    // each extension is a __cxa_guard-protected function-local static in .bss
    build_extensions_if_needed();               // 16 guards, newest→oldest
    if (guard_for(pjrt_api) not yet set):        // _ZGV... byte == 0
        CreatePjrtApi(&pjrt_api,                  // 0xF874160 — writes all 140 slots
                      Client_Create_impl,         // -> slot 15  (a2)
                      ExecuteContext_Create_impl,  // -> slot 103 (a3)
                      TopologyDescription_Create_impl, // -> slot 87 (a4)
                      Plugin_Initialize_impl,      // -> slot 8   (a5)
                      extension_start,             // -> slot 1   (a6)
                      Plugin_Attributes_Xla_impl); // -> slot 9   (a7)
        mark_guard_set(pjrt_api);
    return &pjrt_api;                             // 0x227BA840
```

```c
function CreatePjrtApi(a1, a2, a3, a4, a5, a6, a7):  // 0xF874160
    *a1     = 1120;                 // slot 0  struct_size
    a1[1]   = a6;                   // slot 1  extension_start  (injected: chain head)
    a1[2]   = 24;                   // slot 2  pjrt_api_version.struct_size
    a1[3]   = 0;                    // slot 3  pjrt_api_version.priv
    a1[4]   = 0x6700000000LL;       // slot 4  {major=0, minor=103}
    a1[5]   = pjrt::PJRT_Error_Destroy;   // slot 5  ... 130 fixed wrappers ...
    a1[8]   = a5;                   // slot 8  Plugin_Initialize    (injected)
    a1[9]   = a7;                   // slot 9  Plugin_Attributes    (injected)
    a1[15]  = a2;                   // slot 15 Client_Create        (injected)
    a1[87]  = a4;                   // slot 87 TopologyDescription_Create (injected)
    a1[103] = a3;                   // slot 103 ExecuteContext_Create (injected)
    // ... all other slots assigned a compile-time-fixed pjrt::PJRT_* symbol ...
    a1[139] = pjrt::PJRT_Executable_ParameterMemoryKinds;  // slot 139
    return a1;
```

> **NOTE —** `CreatePjrtApi` contains no branches and no loop: it is 140 straight-line stores. The slot index *is* the C-array subscript, which is why the reconstruction below can be exact rather than inferred. The five injected slots (`a2`..`a7`) are the only values the function does not hard-code; everything else is a `pjrt::PJRT_*` relocation baked at link time. This was confirmed by reading the initializer in full at `0xF874160`.

### Considerations

The struct lives in `.lbss` (section `[47]`, NOBITS, large-model BSS), at the function-local static `_ZZN4pjrt10tpu_plugin13GetTpuPjrtApiEvE8pjrt_api @ 0x227BA840`. Two consequences for a reimplementer and for any tool that inspects the binary statically:

- **Static disassembly cannot show the populated table.** The 1120 bytes are zero on disk and zero-filled at load; the function pointers only exist after the first `GetPjrtApi` call runs `CreatePjrtApi`. The slot map below is reconstructed from the *stores in the initializer*, not from a data dump — there is nothing to dump until runtime. Independent verification requires reading `/proc/self/mem` at `0x227BA840` inside a live JAX worker after the first call (not done here; runtime-evidence wave).
- **Concurrency is `__cxa_guard`, not a lock the reader holds.** First callers serialize through the Itanium ABI guard (one thread runs the constructor, the rest block on its mutex). After the first call the struct is immutable, so steady-state readers take no lock. A reimplementation that rebuilds the table per call, or that omits the one-shot guard, diverges from this contract.

> **CORRECTION (P-3-10 supersedes P-2-05) —** an earlier reconstruction placed the struct in `.bss`/`.data.rel.ro` at `&stru_2F7EC0 + _GLOBAL_OFFSET_TABLE_`. That expression was an IDA PIC-relocation artifact, not a real address. The struct is in `.lbss` at the concrete VA `0x227BA840`. The earlier note also attributed slot 9 to a TPU-plugin override; it is fed from `CreatePjrtApi`'s `a7` parameter = `pjrt::PJRT_Plugin_Attributes_Xla`, a stock XLA implementation.

---

## Struct Header (Slots 0..4)

### Purpose

The first five qwords are not function pointers. They are the ABI handshake: total size (for forward/backward compat), the extension-chain head, and an embedded `PJRT_Api_Version` substruct. A host reads these before touching any function pointer.

### Layout

| Slot | Offset | Field | Type | Value | Source |
|---|---|---|---|---|---|
| 0 | `+0x000` | `struct_size` | `size_t` | `1120` | `*a1 = 1120` |
| 1 | `+0x008` | `extension_start` | `PJRT_Extension_Base*` | `0x224C3F68` (chain head) | `a1[1] = a6` (injected) |
| 2 | `+0x010` | `pjrt_api_version.struct_size` | `size_t` | `24` | `a1[2] = 24` |
| 3 | `+0x018` | `pjrt_api_version.priv` | `void*` | `NULL` | `a1[3] = 0` |
| 4 | `+0x020` | `pjrt_api_version.{major,minor}` | `int,int` | `{0, 103}` | `a1[4] = 0x6700000000` |

```c
struct PJRT_Api_Version {       // embedded at PJRT_Api +0x010, 24 bytes
    size_t struct_size;          // = 24
    void*  priv;                 // = NULL
    int    major_version;        // = 0
    int    minor_version;        // = 103 (0x67)
};
```

The version qword decodes by little-endian byte layout: bytes at `+0x20` are `00 00 00 00 67 00 00 00`, i.e. `major` = low 32 bits = `0`, `minor` = high 32 bits = `0x67` = 103. `struct_size = 1120` lets a host compiled against a different minor know exactly how many trailing slots it may safely read.

> **QUIRK —** `extension_start` (slot 1) points at the *newest* extension, not the oldest. The chain is built in `__cxa_guard` order and linked head-insert, so walking `.next` goes newest→oldest and terminates at the profiler extension in `.data`. A host that assumes "type 0 first" or any type ordering is wrong; it must iterate to NULL. The chain itself is documented on [`extension-chain.md`](extension-chain.md).

---

## The 140-Slot Map

### Purpose

The complete ordered table. Slots 0..4 are the scalar header above. Slots 5..139 are function pointers; each cell below names the field (per `pjrt_c_api.h` v0.103) and the libtpu symbol the slot points at, with that symbol's virtual address. **All 135 function-pointer slots are populated in this build — there is no null/unimplemented slot.** Confidence is `CERTAIN` for every slot whose store and target symbol were both read in the decompiled initializer and confirmed against the symbol table; the 12 spot-confirmed addresses are flagged.

### Slot ordering note

The order is grouped by area (Error/Plugin/Event/Client/DeviceDescription/Device/Memory/Executable/LoadedExecutable/Buffer/CopyStream/Topology/Compile) through slot 94, then a flat **late-addition cluster** (slots 95..139) appended in feature-addition order across minors 0.40→0.103. The late cluster is *not* re-grouped by area — `Buffer_*`, `Client_*`, `Executable_*`, and `Event_*` entries are interleaved in the order XLA added them. The "Area" column tags each late slot back to its conceptual home; the area deep-dive pages own the behavior.

### Error / Plugin / Event (slots 5..14)

| Slot | Off | Field | Impl symbol (`pjrt::`…) | Addr | Conf |
|---|---|---|---|---|---|
| 5 | `+0x028` | PJRT_Error_Destroy | `PJRT_Error_Destroy` | `0x0F85ECE0` | CERTAIN (spot) |
| 6 | `+0x030` | PJRT_Error_Message | `PJRT_Error_Message` | `0x0F85EDE0` | CERTAIN (spot) |
| 7 | `+0x038` | PJRT_Error_GetCode | `PJRT_Error_GetCode` | `0x0F85EF40` | CERTAIN |
| 8 | `+0x040` | PJRT_Plugin_Initialize | `tpu_plugin::PJRT_Plugin_Initialize` | `0x0E6A9D00` | CERTAIN (spot, injected) |
| 9 | `+0x048` | PJRT_Plugin_Attributes | `PJRT_Plugin_Attributes_Xla` | `0x0F85F080` | CERTAIN (spot, injected) |
| 10 | `+0x050` | PJRT_Event_Destroy | `PJRT_Event_Destroy` | `0x0F86F920` | CERTAIN |
| 11 | `+0x058` | PJRT_Event_IsReady | `PJRT_Event_IsReady` | `0x0F86F9E0` | CERTAIN (spot) |
| 12 | `+0x060` | PJRT_Event_Error | `PJRT_Event_Error` | `0x0F86FBA0` | CERTAIN |
| 13 | `+0x068` | PJRT_Event_Await | `PJRT_Event_Await` | `0x0F86FA80` | CERTAIN |
| 14 | `+0x070` | PJRT_Event_OnReady | `PJRT_Event_OnReady` | `0x0F86FC60` | CERTAIN |

### Client (slots 15..27)

| Slot | Off | Field | Impl symbol (`pjrt::`…) | Addr | Conf |
|---|---|---|---|---|---|
| 15 | `+0x078` | PJRT_Client_Create | `tpu_plugin::PJRT_Client_Create` | `0x0E6A8840` | CERTAIN (spot, injected) |
| 16 | `+0x080` | PJRT_Client_Destroy | `PJRT_Client_Destroy` | `0x0F85F0E0` | CERTAIN |
| 17 | `+0x088` | PJRT_Client_PlatformName | `PJRT_Client_PlatformName` | `0x0F85F4A0` | CERTAIN |
| 18 | `+0x090` | PJRT_Client_ProcessIndex | `PJRT_Client_ProcessIndex` | `0x0F85F440` | CERTAIN |
| 19 | `+0x098` | PJRT_Client_PlatformVersion | `PJRT_Client_PlatformVersion` | `0x0F85F500` | CERTAIN |
| 20 | `+0x0A0` | PJRT_Client_Devices | `PJRT_Client_Devices` | `0x0F85F600` | CERTAIN |
| 21 | `+0x0A8` | PJRT_Client_AddressableDevices | `PJRT_Client_AddressableDevices` | `0x0F85F660` | CERTAIN |
| 22 | `+0x0B0` | PJRT_Client_LookupDevice | `PJRT_Client_LookupDevice` | `0x0F85F6C0` | CERTAIN |
| 23 | `+0x0B8` | PJRT_Client_LookupAddressableDevice | `PJRT_Client_LookupAddressableDevice` | `0x0F85F880` | CERTAIN |
| 24 | `+0x0C0` | PJRT_Client_AddressableMemories | `PJRT_Client_AddressableMemories` | `0x0F85FC60` | CERTAIN |
| 25 | `+0x0C8` | PJRT_Client_Compile | `PJRT_Client_Compile` | `0x0F861820` | CERTAIN |
| 26 | `+0x0D0` | PJRT_Client_DefaultDeviceAssignment | `PJRT_Client_DefaultDeviceAssignment` | `0x0F8630C0` | CERTAIN |
| 27 | `+0x0D8` | PJRT_Client_BufferFromHostBuffer | `PJRT_Client_BufferFromHostBuffer` | `0x0F8644C0` | CERTAIN |

### DeviceDescription / Device (slots 28..39)

| Slot | Off | Field | Impl symbol (`pjrt::`…) | Addr | Conf |
|---|---|---|---|---|---|
| 28 | `+0x0E0` | PJRT_DeviceDescription_Id | `PJRT_DeviceDescription_Id` | `0x0F865360` | CERTAIN |
| 29 | `+0x0E8` | PJRT_DeviceDescription_ProcessIndex | `PJRT_DeviceDescription_ProcessIndex` | `0x0F8653C0` | CERTAIN |
| 30 | `+0x0F0` | PJRT_DeviceDescription_Attributes | `PJRT_DeviceDescription_Attributes` | `0x0F865420` | CERTAIN |
| 31 | `+0x0F8` | PJRT_DeviceDescription_Kind | `PJRT_DeviceDescription_Kind` | `0x0F865480` | CERTAIN |
| 32 | `+0x100` | PJRT_DeviceDescription_DebugString | `PJRT_DeviceDescription_DebugString` | `0x0F865500` | CERTAIN |
| 33 | `+0x108` | PJRT_DeviceDescription_ToString | `PJRT_DeviceDescription_ToString` | `0x0F8658A0` | CERTAIN |
| 34 | `+0x110` | PJRT_Device_GetDescription | `PJRT_Device_GetDescription` | `0x0F8659A0` | CERTAIN |
| 35 | `+0x118` | PJRT_Device_IsAddressable | `PJRT_Device_IsAddressable` | `0x0F865A00` | CERTAIN |
| 36 | `+0x120` | PJRT_Device_LocalHardwareId | `PJRT_Device_LocalHardwareId` | `0x0F865A60` | CERTAIN |
| 37 | `+0x128` | PJRT_Device_AddressableMemories | `PJRT_Device_AddressableMemories` | `0x0F865AC0` | CERTAIN |
| 38 | `+0x130` | PJRT_Device_DefaultMemory | `PJRT_Device_DefaultMemory` | `0x0F865B20` | CERTAIN |
| 39 | `+0x138` | PJRT_Device_MemoryStats | `PJRT_Device_MemoryStats` | `0x0F865CE0` | CERTAIN |

### Memory (slots 40..44)

| Slot | Off | Field | Impl symbol (`pjrt::`…) | Addr | Conf |
|---|---|---|---|---|---|
| 40 | `+0x140` | PJRT_Memory_Id | `PJRT_Memory_Id` | `0x0F865E80` | CERTAIN |
| 41 | `+0x148` | PJRT_Memory_Kind | `PJRT_Memory_Kind` | `0x0F865EE0` | CERTAIN |
| 42 | `+0x150` | PJRT_Memory_DebugString | `PJRT_Memory_DebugString` | `0x0F865FC0` | CERTAIN |
| 43 | `+0x158` | PJRT_Memory_ToString | `PJRT_Memory_ToString` | `0x0F866040` | CERTAIN |
| 44 | `+0x160` | PJRT_Memory_AddressableByDevices | `PJRT_Memory_AddressableByDevices` | `0x0F8660C0` | CERTAIN |

### Executable (slots 45..54)

| Slot | Off | Field | Impl symbol (`pjrt::`…) | Addr | Conf |
|---|---|---|---|---|---|
| 45 | `+0x168` | PJRT_Executable_Destroy | `PJRT_Executable_Destroy` | `0x0F8661C0` | CERTAIN |
| 46 | `+0x170` | PJRT_Executable_Name | `PJRT_Executable_Name` | `0x0F866860` | CERTAIN |
| 47 | `+0x178` | PJRT_Executable_NumReplicas | `PJRT_Executable_NumReplicas` | `0x0F8668C0` | CERTAIN |
| 48 | `+0x180` | PJRT_Executable_NumPartitions | `PJRT_Executable_NumPartitions` | `0x0F866920` | CERTAIN |
| 49 | `+0x188` | PJRT_Executable_NumOutputs | `PJRT_Executable_NumOutputs` | `0x0F866A40` | CERTAIN |
| 50 | `+0x190` | PJRT_Executable_SizeOfGeneratedCodeInBytes | `PJRT_Executable_SizeOfGeneratedCodeInBytes` | `0x0F867240` | CERTAIN |
| 51 | `+0x198` | PJRT_Executable_GetCostAnalysis | `PJRT_Executable_GetCostAnalysis` | `0x0F867B80` | CERTAIN |
| 52 | `+0x1A0` | PJRT_Executable_OutputMemoryKinds | `PJRT_Executable_OutputMemoryKinds` | `0x0F869520` | CERTAIN |
| 53 | `+0x1A8` | PJRT_Executable_OptimizedProgram | `PJRT_Executable_OptimizedProgram` | `0x0F8672A0` | CERTAIN |
| 54 | `+0x1B0` | PJRT_Executable_Serialize | `PJRT_Executable_Serialize` | `0x0F86C5A0` | CERTAIN |

### LoadedExecutable (slots 55..62)

| Slot | Off | Field | Impl symbol (`pjrt::`…) | Addr | Conf |
|---|---|---|---|---|---|
| 55 | `+0x1B8` | PJRT_LoadedExecutable_Destroy | `PJRT_LoadedExecutable_Destroy` | `0x0F866780` | CERTAIN |
| 56 | `+0x1C0` | PJRT_LoadedExecutable_GetExecutable | `PJRT_LoadedExecutable_GetExecutable` | `0x0F86CFA0` | CERTAIN |
| 57 | `+0x1C8` | PJRT_LoadedExecutable_AddressableDevices | `PJRT_LoadedExecutable_AddressableDevices` | `0x0F866980` | CERTAIN |
| 58 | `+0x1D0` | PJRT_LoadedExecutable_Delete | `PJRT_LoadedExecutable_Delete` | `0x0F869A80` | CERTAIN |
| 59 | `+0x1D8` | PJRT_LoadedExecutable_IsDeleted | `PJRT_LoadedExecutable_IsDeleted` | `0x0F869AE0` | CERTAIN |
| 60 | `+0x1E0` | PJRT_LoadedExecutable_Execute | `PJRT_LoadedExecutable_Execute` | `0x0F869B40` | CERTAIN (spot) |
| 61 | `+0x1E8` | PJRT_Executable_DeserializeAndLoad | `PJRT_Executable_DeserializeAndLoad` | `0x0F86CC40` | CERTAIN |
| 62 | `+0x1F0` | PJRT_LoadedExecutable_Fingerprint | `PJRT_LoadedExecutable_Fingerprint` | `0x0F85FBE0` | CERTAIN |

> **NOTE —** slot 62 `PJRT_LoadedExecutable_Fingerprint` is the deprecated fingerprint entry; v0.103 hosts use slot 99 `PJRT_Executable_Fingerprint` instead. Both are populated; the deprecated one is kept for older-minor callers.

### Buffer (slots 63..81)

| Slot | Off | Field | Impl symbol (`pjrt::`…) | Addr | Conf |
|---|---|---|---|---|---|
| 63 | `+0x1F8` | PJRT_Buffer_Destroy | `PJRT_Buffer_Destroy` | `0x0F86D020` | CERTAIN |
| 64 | `+0x200` | PJRT_Buffer_ElementType | `PJRT_Buffer_ElementType` | `0x0F86D220` | CERTAIN |
| 65 | `+0x208` | PJRT_Buffer_Dimensions | `PJRT_Buffer_Dimensions` | `0x0F86D280` | CERTAIN |
| 66 | `+0x210` | PJRT_Buffer_UnpaddedDimensions | `PJRT_Buffer_UnpaddedDimensions` | `0x0F86D300` | CERTAIN |
| 67 | `+0x218` | PJRT_Buffer_DynamicDimensionIndices | `PJRT_Buffer_DynamicDimensionIndices` | `0x0F86D4C0` | CERTAIN |
| 68 | `+0x220` | PJRT_Buffer_GetMemoryLayout | `PJRT_Buffer_GetMemoryLayout` | `0x0F86D5E0` | CERTAIN |
| 69 | `+0x228` | PJRT_Buffer_OnDeviceSizeInBytes | `PJRT_Buffer_OnDeviceSizeInBytes` | `0x0F86DA80` | CERTAIN |
| 70 | `+0x230` | PJRT_Buffer_Device | `PJRT_Buffer_Device` | `0x0F86DB40` | CERTAIN |
| 71 | `+0x238` | PJRT_Buffer_Memory | `PJRT_Buffer_Memory` | `0x0F86DC60` | CERTAIN |
| 72 | `+0x240` | PJRT_Buffer_Delete | `PJRT_Buffer_Delete` | `0x0F86DD80` | CERTAIN |
| 73 | `+0x248` | PJRT_Buffer_IsDeleted | `PJRT_Buffer_IsDeleted` | `0x0F86DDE0` | CERTAIN |
| 74 | `+0x250` | PJRT_Buffer_CopyToDevice | `PJRT_Buffer_CopyToDevice` | `0x0F86E360` | CERTAIN |
| 75 | `+0x258` | PJRT_Buffer_ToHostBuffer | `PJRT_Buffer_ToHostBuffer` | `0x0F86E640` | CERTAIN (spot) |
| 76 | `+0x260` | PJRT_Buffer_IsOnCpu | `PJRT_Buffer_IsOnCpu` | `0x0F86ECC0` | CERTAIN |
| 77 | `+0x268` | PJRT_Buffer_ReadyEvent | `PJRT_Buffer_ReadyEvent` | `0x0F86ED20` | CERTAIN |
| 78 | `+0x270` | PJRT_Buffer_UnsafePointer | `PJRT_Buffer_UnsafePointer` | `0x0F86EE60` | CERTAIN |
| 79 | `+0x278` | PJRT_Buffer_IncreaseExternalReferenceCount | `PJRT_Buffer_IncreaseExternalReferenceCount` | `0x0F86EF20` | CERTAIN |
| 80 | `+0x280` | PJRT_Buffer_DecreaseExternalReferenceCount | `PJRT_Buffer_DecreaseExternalReferenceCount` | `0x0F86F100` | CERTAIN |
| 81 | `+0x288` | PJRT_Buffer_OpaqueDeviceMemoryDataPointer | `PJRT_Buffer_OpaqueDeviceMemoryDataPointer` | `0x0F86F200` | CERTAIN |

### CopyToDeviceStream (slots 82..86)

| Slot | Off | Field | Impl symbol (`pjrt::`…) | Addr | Conf |
|---|---|---|---|---|---|
| 82 | `+0x290` | PJRT_CopyToDeviceStream_Destroy | `PJRT_CopyToDeviceStream_Destroy` | `0x0F86F5E0` | CERTAIN |
| 83 | `+0x298` | PJRT_CopyToDeviceStream_AddChunk | `PJRT_CopyToDeviceStream_AddChunk` | `0x0F86F660` | CERTAIN |
| 84 | `+0x2A0` | PJRT_CopyToDeviceStream_TotalBytes | `PJRT_CopyToDeviceStream_TotalBytes` | `0x0F86F7E0` | CERTAIN |
| 85 | `+0x2A8` | PJRT_CopyToDeviceStream_GranuleSize | `PJRT_CopyToDeviceStream_GranuleSize` | `0x0F86F840` | CERTAIN |
| 86 | `+0x2B0` | PJRT_CopyToDeviceStream_CurrentBytes | `PJRT_CopyToDeviceStream_CurrentBytes` | `0x0F86F8A0` | CERTAIN |

### TopologyDescription / Compile (slots 87..94)

| Slot | Off | Field | Impl symbol (`pjrt::`…) | Addr | Conf |
|---|---|---|---|---|---|
| 87 | `+0x2B8` | PJRT_TopologyDescription_Create | `tpu_plugin::PJRT_TopologyDescription_Create` | `0x0E6A9B20` | CERTAIN (spot, injected) |
| 88 | `+0x2C0` | PJRT_TopologyDescription_Destroy | `PJRT_TopologyDescription_Destroy` | `0x0F870040` | CERTAIN |
| 89 | `+0x2C8` | PJRT_TopologyDescription_PlatformName | `PJRT_TopologyDescription_PlatformName` | `0x0F870200` | CERTAIN |
| 90 | `+0x2D0` | PJRT_TopologyDescription_PlatformVersion | `PJRT_TopologyDescription_PlatformVersion` | `0x0F870260` | CERTAIN |
| 91 | `+0x2D8` | PJRT_TopologyDescription_GetDeviceDescriptions | `PJRT_TopologyDescription_GetDeviceDescriptions` | `0x0F8702C0` | CERTAIN |
| 92 | `+0x2E0` | PJRT_TopologyDescription_Serialize | `PJRT_TopologyDescription_Serialize` | `0x0F870320` | CERTAIN |
| 93 | `+0x2E8` | PJRT_TopologyDescription_Attributes | `PJRT_TopologyDescription_Attributes` | `0x0F8705E0` | CERTAIN |
| 94 | `+0x2F0` | PJRT_Compile | `PJRT_Compile` | `0x0F870640` | CERTAIN (spot) |

### Late-addition cluster (slots 95..139, addition order)

These slots were appended across minors 0.40→0.103, in the order XLA added them — not regrouped by area. The Area column tags each back to its conceptual home.

| Slot | Off | Field | Area | Impl symbol (`pjrt::`…) | Addr | Conf |
|---|---|---|---|---|---|---|
| 95 | `+0x2F8` | PJRT_Executable_OutputElementTypes | Executable | `PJRT_Executable_OutputElementTypes` | `0x0F868560` | CERTAIN |
| 96 | `+0x300` | PJRT_Executable_OutputDimensions | Executable | `PJRT_Executable_OutputDimensions` | `0x0F8689E0` | CERTAIN |
| 97 | `+0x308` | PJRT_Buffer_CopyToMemory | Buffer | `PJRT_Buffer_CopyToMemory` | `0x0F86E500` | CERTAIN |
| 98 | `+0x310` | PJRT_Client_CreateViewOfDeviceBuffer | Client | `PJRT_Client_CreateViewOfDeviceBuffer` | `0x0F865040` | CERTAIN |
| 99 | `+0x318` | PJRT_Executable_Fingerprint | Executable | `PJRT_Executable_Fingerprint` | `0x0F867AC0` | CERTAIN |
| 100 | `+0x320` | PJRT_Client_TopologyDescription | Client | `PJRT_Client_TopologyDescription` | `0x0F85F560` | CERTAIN |
| 101 | `+0x328` | PJRT_Executable_GetCompiledMemoryStats | Executable | `PJRT_Executable_GetCompiledMemoryStats` | `0x0F86CAC0` | CERTAIN |
| 102 | `+0x330` | PJRT_Memory_Kind_Id | Memory | `PJRT_Memory_Kind_Id` | `0x0F865F60` | CERTAIN |
| 103 | `+0x338` | PJRT_ExecuteContext_Create | ExecuteContext | `tpu_plugin::PJRT_ExecuteContext_Create` | `0x0E6A9A80` | CERTAIN (spot, injected) |
| 104 | `+0x340` | PJRT_ExecuteContext_Destroy | ExecuteContext | `PJRT_ExecuteContext_Destroy` | `0x0F866120` | CERTAIN |
| 105 | `+0x348` | PJRT_Buffer_CopyRawToHost | Buffer | `PJRT_Buffer_CopyRawToHost` | `0x0F86DE40` | CERTAIN |
| 106 | `+0x350` | PJRT_AsyncHostToDeviceTransferManager_Destroy | AsyncH2D | `PJRT_AsyncHostToDeviceTransferManager_Destroy` | `0x0F860620` | CERTAIN |
| 107 | `+0x358` | PJRT_AsyncHostToDeviceTransferManager_TransferData | AsyncH2D | `PJRT_AsyncHostToDeviceTransferManager_TransferData` | `0x0F8606A0` | CERTAIN |
| 108 | `+0x360` | PJRT_Client_CreateBuffersForAsyncHostToDevice | Client | `PJRT_Client_CreateBuffersForAsyncHostToDevice` | `0x0F85FCC0` | CERTAIN |
| 109 | `+0x368` | PJRT_AsyncHostToDeviceTransferManager_RetrieveBuffer | AsyncH2D | `PJRT_AsyncHostToDeviceTransferManager_RetrieveBuffer` | `0x0F8611A0` | CERTAIN |
| 110 | `+0x370` | PJRT_AsyncHostToDeviceTransferManager_Device | AsyncH2D | `PJRT_AsyncHostToDeviceTransferManager_Device` | `0x0F861260` | CERTAIN |
| 111 | `+0x378` | PJRT_AsyncHostToDeviceTransferManager_BufferCount | AsyncH2D | `PJRT_AsyncHostToDeviceTransferManager_BufferCount` | `0x0F861380` | CERTAIN |
| 112 | `+0x380` | PJRT_AsyncHostToDeviceTransferManager_BufferSize | AsyncH2D | `PJRT_AsyncHostToDeviceTransferManager_BufferSize` | `0x0F8613E0` | CERTAIN |
| 113 | `+0x388` | PJRT_AsyncHostToDeviceTransferManager_SetBufferError | AsyncH2D | `PJRT_AsyncHostToDeviceTransferManager_SetBufferError` | `0x0F861440` | CERTAIN |
| 114 | `+0x390` | PJRT_AsyncHostToDeviceTransferManager_AddMetadata | AsyncH2D | `PJRT_AsyncHostToDeviceTransferManager_AddMetadata` | `0x0F861500` | CERTAIN |
| 115 | `+0x398` | PJRT_Client_DmaMap | Client | `PJRT_Client_DmaMap` | `0x0F860500` | CERTAIN |
| 116 | `+0x3A0` | PJRT_Client_DmaUnmap | Client | `PJRT_Client_DmaUnmap` | `0x0F860580` | CERTAIN |
| 117 | `+0x3A8` | PJRT_Client_CreateUninitializedBuffer | Client | `PJRT_Client_CreateUninitializedBuffer` | `0x0F863660` | CERTAIN |
| 118 | `+0x3B0` | PJRT_Client_UpdateGlobalProcessInfo | Client | `PJRT_Client_UpdateGlobalProcessInfo` | `0x0F85F940` | CERTAIN |
| 119 | `+0x3B8` | PJRT_TopologyDescription_Deserialize | Topology | `PJRT_TopologyDescription_Deserialize` | `0x0F870B80` | CERTAIN |
| 120 | `+0x3C0` | PJRT_Client_CreateAliasBuffer | Client | `PJRT_Client_CreateAliasBuffer` | `0x0F863D60` | CERTAIN |
| 121 | `+0x3C8` | PJRT_Client_FulfillAliasBuffer | Client | `PJRT_Client_FulfillAliasBuffer` | `0x0F8641A0` | CERTAIN |
| 122 | `+0x3D0` | PJRT_LoadedExecutable_GetDeviceAssignment | LoadedExec | `PJRT_LoadedExecutable_GetDeviceAssignment` | `0x0F870EA0` | CERTAIN |
| 123 | `+0x3D8` | PJRT_Client_CreateErrorBuffer | Client | `PJRT_Client_CreateErrorBuffer` | `0x0F8638A0` | CERTAIN |
| 124 | `+0x3E0` | PJRT_AsyncHostToDeviceTransferManager_TransferLiteral | AsyncH2D | `PJRT_AsyncHostToDeviceTransferManager_TransferLiteral` | `0x0F860960` | CERTAIN |
| 125 | `+0x3E8` | PJRT_Buffer_CopyRawToHostFuture | Buffer | `PJRT_Buffer_CopyRawToHostFuture` | `0x0F86DFE0` | CERTAIN |
| 126 | `+0x3F0` | PJRT_Device_PoisonExecution | Device | `PJRT_Device_PoisonExecution` | `0x0F860D00` | CERTAIN |
| 127 | `+0x3F8` | PJRT_Device_CreateAsyncTrackingEvent | Device | `PJRT_Device_CreateAsyncTrackingEvent` | `0x0F861080` | CERTAIN |
| 128 | `+0x400` | PJRT_AsyncTrackingEvent_Destroy | AsyncTracking | `PJRT_AsyncTrackingEvent_Destroy` | `0x0F861120` | CERTAIN |
| 129 | `+0x408` | PJRT_Executable_GetCompileOptions | Executable | `PJRT_Executable_GetCompileOptions` | `0x0F86C6E0` | CERTAIN |
| 130 | `+0x410` | PJRT_Buffer_DonateWithControlDependency | Buffer | `PJRT_Buffer_DonateWithControlDependency` | `0x0F86F2E0` | CERTAIN |
| 131 | `+0x418` | PJRT_Event_Create | Event | `PJRT_Event_Create` | `0x0F86FE00` | CERTAIN |
| 132 | `+0x420` | PJRT_Event_Set | Event | `PJRT_Event_Set` | `0x0F86FFA0` | CERTAIN |
| 133 | `+0x428` | PJRT_Device_GetAttributes | Device | `PJRT_Device_GetAttributes` | `0x0F873AC0` | CERTAIN |
| 134 | `+0x430` | PJRT_Client_Load | Client | `PJRT_Client_Load` | `0x0F8627E0` | CERTAIN |
| 135 | `+0x438` | PJRT_LoadedExecutable_AddressableDeviceLogicalIds | LoadedExec | `PJRT_LoadedExecutable_AddressableDeviceLogicalIds` | `0x0F8669E0` | CERTAIN |
| 136 | `+0x440` | PJRT_Buffer_Bitcast | Buffer | `PJRT_Buffer_Bitcast` | `0x0F862D00` | CERTAIN |
| 137 | `+0x448` | PJRT_Error_ForEachPayload | Error | `PJRT_Error_ForEachPayload` | `0x0F85EFC0` | CERTAIN |
| 138 | `+0x450` | PJRT_TopologyDescription_Fingerprint | Topology | `PJRT_TopologyDescription_Fingerprint` | `0x0F870520` | CERTAIN |
| 139 | `+0x458` | PJRT_Executable_ParameterMemoryKinds | Executable | `PJRT_Executable_ParameterMemoryKinds` | `0x0F868FC0` | CERTAIN (spot) |

---

## Populated vs Injected Map

### Purpose

Every one of the 135 function-pointer slots is populated; **none is null in this build**. But the slots split into two provenance classes, and the split is the heart of the plugin pattern: 130 slots are generic XLA C-API wrappers baked at link time, and 5 are injected at runtime by `CreatePjrtApi`'s caller.

### The five injection points

The only slots `CreatePjrtApi` does not hard-code are its `a2`..`a7` parameters. `GetTpuPjrtApi` passes the TPU-specific implementations into exactly these positions; everything else is a fixed relocation.

| Slot | Field | Param | Injected impl | Addr | Why TPU-specialized |
|---|---|---|---|---|---|
| 15 | PJRT_Client_Create | `a2` | `tpu_plugin::PJRT_Client_Create` | `0x0E6A8840` | Constructs the TPU `PjRtClient` (device discovery, runtime init) |
| 103 | PJRT_ExecuteContext_Create | `a3` | `tpu_plugin::PJRT_ExecuteContext_Create` | `0x0E6A9A80` | TPU-specific execute context |
| 87 | PJRT_TopologyDescription_Create | `a4` | `tpu_plugin::PJRT_TopologyDescription_Create` | `0x0E6A9B20` | TPU pod/slice topology |
| 8 | PJRT_Plugin_Initialize | `a5` | `tpu_plugin::PJRT_Plugin_Initialize` | `0x0E6A9D00` | TPU runtime bring-up |
| 1 | extension_start | `a6` | (extension chain head) | `0x224C3F68` | TPU extension set |
| 9 | PJRT_Plugin_Attributes | `a7` | `PJRT_Plugin_Attributes_Xla` | `0x0F85F080` | Plugin attribute table (not in `tpu_plugin::`, but injected) |

The four `tpu_plugin::`-namespaced impls (slots 8, 15, 87, 103) are the genuine TPU specializations. Slot 9 is injected but resolves to the stock `pjrt::PJRT_Plugin_Attributes_Xla` — the caller passes the XLA implementation, not a TPU override. Slot 1 is the extension-chain head, also injected (see [`extension-chain.md`](extension-chain.md)). The remaining 130 function pointers are compile-time-fixed `pjrt::PJRT_*` symbols from XLA's `pjrt_c_api_wrapper_impl.cc`.

> **QUIRK —** "TPU-specialized" is a much smaller set than a reimplementer might expect. Only four slots carry TPU-specific code; the other 131 function pointers are byte-for-byte the same generic XLA wrappers any PJRT plugin would ship. The TPU-ness lives almost entirely behind `Client_Create` (which builds the `PjRtClient` the generic wrappers then dispatch through) and in the extension chain — not in the per-call wrappers.

---

## Backward-Compatibility Guard

### Purpose

A host compiled against an older PJRT minor passes a *smaller* `_Args` struct than this v0.103 plugin expects. The plugin must accept it and read only the fields the caller actually provided. The mechanism is a per-slot size check at the top of every wrapper.

### Algorithm

```c
function PJRT_Error_Destroy(args):                  // 0xF85ECE0
    rc = pjrt::ActualStructSizeIsGreaterOrEqual(     // 0xF8A4EC0
            "PJRT_Error_Destroy_Args",                // API name (for the log line)
            23,                                       // min accepted struct_size
            24,                                       // current (v0.103) struct_size
            args->struct_size);                       // what the caller passed
    if (rc != ok):
        LogMessage(".../pjrt_c_api_wrapper_impl.cc", 545) << status;  // diagnostic
    if (args->struct_size < 0x18): return;            // too small: bail
    // ... read only fields within args->struct_size ...
```

Every wrapper's first instruction is this `ActualStructSizeIsGreaterOrEqual` call. The hard-coded `(min, current)` pair bounds the per-`_Args` versions the plugin accepts; for `PJRT_Error_Destroy_Args` it is `(23, 24)`. Fields beyond the caller's `struct_size` are never read. This is what lets one v0.103 plugin serve a range of host minors without per-version trampolines — the size prefix on each args struct, not a version branch, carries the compatibility logic.

> **NOTE —** the source-path string `third_party/tensorflow/compiler/xla/pjrt/c/pjrt_c_api_wrapper_impl.cc` is baked into the diagnostic at `0xF85ECE0`. It confirms the 130 fixed wrappers are XLA's generic `pjrt_c_api_wrapper_impl.cc` code, identical across PJRT plugins — the binary's own provenance string, not an external claim.

---

## Hot-Path Slots

The slots a JAX/PyTorch-XLA step touches most. This ranking is a *semantic estimate* — no call-count instrumentation was run, so confidence is LOW on the ordering, though the slot identities are CERTAIN.

| Rank | Slot | Field | Per-step role |
|---|---|---|---|
| 1 | 60 | PJRT_LoadedExecutable_Execute | Program launch onto TPU; the primary throughput slot |
| 2 | 11 | PJRT_Event_IsReady | Polled in tight loops for async completion |
| 3 | 13 | PJRT_Event_Await | Synchronous wait on an async result |
| 4 | 14 | PJRT_Event_OnReady | Completion callback registration, per buffer |
| 5 | 10 | PJRT_Event_Destroy | Mass destroy after batch await |
| 6 | 63 | PJRT_Buffer_Destroy | Per-buffer release; bursts on graph-output cleanup |
| 7 | 27 | PJRT_Client_BufferFromHostBuffer | Host→TPU input upload, per parameter per step |
| 8 | 75 | PJRT_Buffer_ToHostBuffer | TPU→host downloads (metrics, probes) |
| 9 | 81 | PJRT_Buffer_OpaqueDeviceMemoryDataPointer | Zero-copy DMA pointer fetch for foreign-lib interop |
| 10 | 79/80 | PJRT_Buffer_{In,De}creaseExternalReferenceCount | DLPack-style refcount bump/drop |

> **GOTCHA —** the Top-20 ranking in the source findings is an ordering *heuristic*, not measured data. A reimplementer optimizing the dispatch path should treat slot 60 (Execute) and the Event slots (10..14) as certainly hot, but should not trust the precise rank order of the cooler slots without instrumentation.

---

## Cross-References

- [PJRT Overview](overview.md) — where this table sits in the plugin lifecycle; the `GetPjrtApi` → `GetTpuPjrtApi` → `CreatePjrtApi` chain in context
- [Extension Chain](extension-chain.md) — the linked list dangling off `extension_start` (slot 1); 17 extensions, type-IDs, per-extension layouts
- [Client and Device](client-and-device.md) — slots 15..44: `Client_Create` (the key TPU injection) plus device/memory queries
- [Buffer and Memory](buffer-and-memory.md) — slots 63..81, 97, 105, 125, 130, 136: buffer lifecycle, transfers, DMA
- [Executable Execution](executable-execution.md) — slots 45..62, 122, 135 and slot 60 `LoadedExecutable_Execute`, the hot path
- [Events and Async](events-and-async.md) — slots 10..14, 131..132: the `PJRT_Event` model
- [Callbacks](callbacks.md) — `OnReady` / async-tracking-event slots and the callback extension
- [Collectives Communicator](collectives-communicator.md) — the collectives extension surface reached via the chain, not via these slots
