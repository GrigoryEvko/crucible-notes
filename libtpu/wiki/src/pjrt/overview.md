# PJRT C-ABI Overview

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

PJRT is the stable C plugin ABI that every XLA front-end — JAX, TensorFlow, PyTorch-XLA — uses to drive a hardware backend without linking against the backend's C++ and without sharing a C++ ABI with it. The contract is deliberately minimal: the framework `dlopen`s `libtpu.so`, `dlsym`s a single exported entry symbol, calls it with no arguments, and receives a pointer to one flat C struct — `PJRT_Api` — whose fields are function pointers. Every subsequent interaction (create a client, compile an HLO module, upload a buffer, launch an executable, wait on an event) is an indirect call through a slot of that struct. Because the struct is plain C and every call is size-checked, a framework built against an older header can drive a newer plugin and vice versa. This is the same role StreamExecutor's C-shim (`TfTpu_*ApiFn`) plays for the legacy stack, but PJRT is the *public, versioned* surface and the only one a modern PJRT client touches.

In this build the entry symbol is `GetPjrtApi @ 0xe6a83a0` — a 5-byte `jmp` thunk into the real engine `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440` (the thunk itself is dissected in [../lifecycle/get-pjrt-api-thunk.md](../lifecycle/get-pjrt-api-thunk.md)). There is **no** exported `GetTpuPjrtApi`; the canonical-cased `GetPjrtApi` is the only `GLOBAL FUNC` export matching `/Pjrt/`, versioned `GetPjrtApi@@VERS_1.0`. The `PJRT_Api` table it returns is **not** a static image in any PROGBITS section — it is a function-local Meyers singleton at `0x227BA840` in `.lbss` (1120 bytes = 140 × 8), zero-filled at load and populated lazily on the first call by `pjrt::CreatePjrtApi @ 0xf874160` under a `__cxa_guard`. The table is PJRT C-API **v0.103**: 140 qword slots — 5 header scalars (`struct_size`, `extension_start`, and a 3-field `PJRT_Api_Version` sub-struct) followed by 135 function pointers. Hanging off `extension_start` is a 17-node singly-linked extension chain that carries the TPU-specific surface (Megascale, MultiSlice, collectives, raw buffers, phase-compile, profiler, …) that does not fit the generic vtable.

This page is the **map** for the PJRT section. It fixes the ABI's shape, the `dlsym` handshake and lazy-build path (by symbol — the deep module-init lifecycle is owned elsewhere), and gives an at-a-glance index of the 140-slot table and the 17-extension chain by *region*, linking the sibling page that owns each region's field-by-field detail. It does **not** reproduce the full slot table ([api-vtable-reconstruction.md](api-vtable-reconstruction.md)) or the extension-node layouts ([extension-chain.md](extension-chain.md)); it tells you which sibling owns what.

For orientation, the contract is:

- **The handshake** — `dlsym("GetPjrtApi")` → call with no args → a `const PJRT_Api*`; what the caller is allowed to assume and what it must discover.
- **The struct shape** — a 1120-byte flat C table: 5 header scalars + 135 fn-ptrs, native-ordered to match the public `xla/pjrt/c/pjrt_c_api.h` v0.103 schema exactly, with per-call `struct_size` versioning.
- **The 5 TPU injection points** — slots 8/9/15/87/103 are the only slots `CreatePjrtApi` takes from its caller; the other 130 are compile-fixed generic XLA wrappers.
- **The extension chain** — a NULL-terminated, newest-first linked list of 17 typed extensions reached only through `extension_start`, never by fixed offset.

| | |
|---|---|
| **Exported entry symbol** | `GetPjrtApi @ 0xe6a83a0` (5-byte `jmp` thunk, `@@VERS_1.0`) |
| **Real engine** | `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440` (1336 B, 17 `__cxa_guard` blocks) |
| **Table constructor** | `pjrt::CreatePjrtApi @ 0xf874160` (1872 B, flat header-write + slot-fill, no loop) |
| **Table storage** | `GetTpuPjrtApi()::pjrt_api @ 0x227BA840`, `.lbss` (NOBITS), 1120 B = 140 × 8 |
| **C-API version** | v0.103 — version qword `0x6700000000` → `{major=0, minor=0x67=103}` |
| **Slot count** | 140 (5 header scalars + 135 fn-ptrs) |
| **Extension chain head** | `extension_start` (slot 1) → `host_memory_allocator_extension @ 0x224C3F68` |
| **Extension count** | 17 (16 `.bss`-resident, lazily built; 1 `.data` static — profiler) |
| **TPU-injected slots** | 8, 9, 15, 87, 103 (Plugin_Initialize, Plugin_Attributes, Client_Create, TopologyDescription_Create, ExecuteContext_Create) |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. The Plugin-Discovery Handshake

### Purpose

PJRT's whole reason to exist is a single, ABI-stable rendezvous point. The framework knows nothing about libtpu's internals; it knows only the entry-symbol name and the layout of the first few `PJRT_Api` fields. Everything else is discovered at run time through `struct_size` fields and the extension chain. This section fixes that rendezvous so a reimplementer can produce a `.so` a stock JAX/PyTorch-XLA build will load.

### The contract

```c
// The one symbol the framework dlsym's. No arguments, returns the table.
const PJRT_Api* GetPjrtApi(void);             // exported, GetPjrtApi@@VERS_1.0
```

```text
framework (JAX / TF / PyTorch-XLA)
  dlopen("libtpu.so")
  dlsym(handle, "GetPjrtApi")  ── the ONLY name that resolves; "GetTpuPjrtApi" is internal
    │
    └─ GetPjrtApi  0xe6a83a0   ── 5-byte: jmp 0xe6aa440
         └─ pjrt::tpu_plugin::GetTpuPjrtApi  0xe6aa440
              ├─ build 16 .bss extensions  (each one-shot __cxa_guard)
              ├─ (17th guard) pjrt::CreatePjrtApi(&pjrt_api, …)  0xf874160  ── writes all 140 slots
              └─ return &pjrt_api  =  0x227BA840   (.lbss)
```

The caller then reads `api->struct_size` to learn how many slots this plugin actually provides, reads `api->pjrt_api_version` to learn the minor version, and walks `api->extension_start` to discover optional capabilities. It then calls `api->PJRT_Plugin_Initialize` (slot 8) — the one-time TPU driver bring-up — and `api->PJRT_Client_Create` (slot 15) to mint a live client. Both of those slots reach deep into module-init and silicon detection, which is the lifecycle section's territory (`PJRT_Plugin_Initialize` → `TryAcquireTpuLock` → `InitializeDriver` → the `GoogleInitializer` module DAG; silicon scan deferred to `PJRT_Client_Create`); this page references them by symbol only — see [../lifecycle/module-init-plugin-discovery.md](../lifecycle/module-init-plugin-discovery.md) and [../lifecycle/do-init-do-fini.md](../lifecycle/do-init-do-fini.md).

> **NOTE —** the table is built on *first call*, not at `dlopen`. `GetTpuPjrtApi`'s `pjrt_api` is a function-local static in `.lbss` (NOBITS), zero until the first `GetPjrtApi`. The 17 `__cxa_guard`-protected blocks (16 extension builders + the final `CreatePjrtApi`) run exactly once; concurrent first-callers serialize through Itanium-ABI `__cxa_guard` semantics, and after the one-shot the struct is immutable for process lifetime — readers take no lock. Static disassembly therefore cannot show populated slot values; the slot→impl mapping is reconstructed from `CreatePjrtApi`'s body, not from the zero-filled `.lbss` image.

> **GOTCHA —** spelling and casing are part of the ABI. The exported symbol is `GetPjrtApi` (lowercase `jrt`), matching the public PJRT plugin convention; `GetTpuPjrtApi` is an *internal* helper and is **not** exported. A loader that `dlsym`s `GetTpuPjrtApi`, or a build that exports only the `Tpu`-prefixed name, will fail discovery. The `Tpu*_*` exports that share this binary (194 symbols, all `FUNC GLOBAL @@VERS_1.0`) are the *legacy* StreamExecutor C-ABI, linked directly by TF-TPU, never reached through PJRT — see [stream-executor-pjrt-adapter.md](stream-executor-pjrt-adapter.md).

---

## 2. The PJRT_Api Struct at a Glance

### Purpose

`PJRT_Api` is one flat C struct of 140 qword slots. A reimplementer needs the *shape* — header vs function-pointer regions, the versioning convention, and which slots are plugin-specific — before drowning in the 135-row field table. This section gives that shape; the field-by-field reconstruction is [api-vtable-reconstruction.md](api-vtable-reconstruction.md).

### Header layout

The first five slots are not function pointers. They are the self-describing header that makes the ABI forward/backward compatible. Confirmed byte-for-byte against `CreatePjrtApi @ 0xf874160` (`*a1 = 1120; a1[2] = 24; a1[4] = 0x6700000000`).

| Slot | Offset | Field | Value | Confidence |
|---|---|---|---|---|
| 0 | +0x00 | `struct_size` | 1120 (= 140 × 8) | CONFIRMED |
| 1 | +0x08 | `extension_start` | → `host_memory_allocator_extension @ 0x224C3F68` | CONFIRMED |
| 2 | +0x10 | `pjrt_api_version.struct_size` | 24 (the sub-struct's own size) | CONFIRMED |
| 3 | +0x18 | `pjrt_api_version.priv` | NULL (reserved) | CONFIRMED |
| 4 | +0x20 | `pjrt_api_version.{major,minor}` | `{0, 103}` (qword `0x6700000000`) | CONFIRMED |

`pjrt_api_version` is an embedded `PJRT_Api_Version { size_t struct_size; void* priv; int major; int minor }` (24 bytes), so slots 2..4 are one logical field. The major/minor pack into the slot-4 qword little-endian: low 32 bits = major (0), high 32 = minor (0x67 = 103).

### Function-pointer regions

Slots 5..139 are 135 function pointers, in exactly the order the public `xla/pjrt/c/pjrt_c_api.h` v0.103 header declares them — including the appended-late slots (95..139) added after the original v0.40 surface, in feature-addition order. Rather than reproduce all 135 here, the table groups them into the regions each sibling page owns. The pre-95 block is the stable v0.40 core; 95..139 are the v0.103 late additions (`Output{ElementTypes,Dimensions}`, `CopyToMemory`, `CreateViewOfDeviceBuffer`, `Executable_Fingerprint`, the `AsyncHostToDeviceTransferManager_*` family, `DmaMap/Unmap`, `CreateAliasBuffer`, `DonateWithControlDependency`, `Event_Create/Set`, `Client_Load`, `Bitcast`, `Error_ForEachPayload`, `Topology_Fingerprint`, `ParameterMemoryKinds`).

| Slot range | Region | Owner page |
|---|---|---|
| 5–7, 137 | Error: Destroy / Message / GetCode / ForEachPayload | [events-and-async.md](events-and-async.md) |
| 8–9 | Plugin: Initialize, Attributes (lifecycle entry) | (lifecycle — referenced by symbol) |
| 10–14, 131–132 | Event: Destroy / IsReady / Error / Await / OnReady / Create / Set | [events-and-async.md](events-and-async.md) |
| 15–27, 98, 100, 108, 115–123, 134 | Client: create/lookup/compile/buffer-from-host/alias/dma/load | [client-and-device.md](client-and-device.md) |
| 28–39, 126–127, 133 | DeviceDescription + Device | [client-and-device.md](client-and-device.md) |
| 40–44, 102 | Memory | [buffer-and-memory.md](buffer-and-memory.md) |
| 45–54, 95–96, 99, 101, 129, 139 | Executable (program metadata, serialize, cost, fingerprint) | [executable-execution.md](executable-execution.md) |
| 55–62, 122, 135 | LoadedExecutable (incl. slot 60 `Execute`, the hot path) | [executable-execution.md](executable-execution.md) |
| 63–81, 97, 105, 125, 130, 136 | Buffer (lifecycle, transfer, refcount, donate, bitcast) | [buffer-and-memory.md](buffer-and-memory.md) |
| 82–86 | CopyToDeviceStream | [dma-and-cross-host-recv.md](dma-and-cross-host-recv.md) |
| 87–93, 100, 119, 138 | TopologyDescription | [ext-topology-description.md](ext-topology-description.md) |
| 94 | `PJRT_Compile` (AOT, no client) | [executable-execution.md](executable-execution.md) |
| 103–104 | ExecuteContext | [executable-execution.md](executable-execution.md) |
| 106–114, 124 | AsyncHostToDeviceTransferManager | [buffer-and-memory.md](buffer-and-memory.md) |
| 127–128 | AsyncTrackingEvent | [events-and-async.md](events-and-async.md) |

> **QUIRK —** the slot ordering is *not* grouped by object the way the table above is. The header's region grouping is a reading aid; the actual `pjrt_c_api.h` order interleaves families because slots are appended in the order features landed upstream. `PJRT_Compile` is slot 94, but the `Executable_OutputElementTypes` that logically belongs with the Executable block is slot 95 — *after* it — because it was added later. A reimplementer must reproduce the wire order from the header schema verbatim; the family grouping is for humans, the slot index is for the ABI. The full ordered list is [api-vtable-reconstruction.md](api-vtable-reconstruction.md).

### Versioning: how an old client talks to a new plugin

The header is forward/backward compatible by two mechanisms working together. First, every reachable `PJRT_Api` field is itself versioned via `struct_size` on its *args* struct: the first instruction of nearly every slot calls `pjrt::ActualStructSizeIsGreaterOrEqual("<API>_Args", min, current, args->struct_size)`, which accepts any caller args struct from a documented `min` up through the plugin's `current`, and leaves fields beyond the caller's `struct_size` unread. Second, the table-level `struct_size` (slot 0) and `pjrt_api_version` (slots 2..4) let the caller learn how many slots exist before indexing one. A client compiled against v0.95 sees `struct_size = 1120` and a minor of 103, never indexes past its own known slots, and passes smaller args structs that the per-slot guard accepts. The reverse — a v0.103 client on an older plugin — is bounded by the older plugin's `struct_size`.

> **GOTCHA —** never read `extension_start` or any late slot by assuming a fixed table size. A reimplementer that hardcodes 140 slots will mis-parse an older or newer plugin. Read `struct_size` first; treat any slot at an offset `>= struct_size` as absent. The same rule applies per-call via the args `struct_size` guard.

---

## 3. The Extension Chain at a Glance

### Purpose

The generic `PJRT_Api` vtable cannot carry backend-specific surface (TPU topology details, Megascale/MultiSlice multi-pod features, collectives, raw-buffer DMA, the profiler) without polluting the public schema. PJRT solves this with an extension chain: a NULL-terminated singly-linked list of typed `PJRT_Extension_Base` nodes reached only through `extension_start` (slot 1). Each node begins `{ size_t struct_size; PJRT_Extension_Type type; PJRT_Extension_Base* next; }` (the `.next` lives at offset +0x10), followed by that extension's own function pointers. This section indexes the chain by type; the node-by-node field maps are [extension-chain.md](extension-chain.md) and the per-extension sibling pages.

### The chain

`extension_start = 0x224C3F68`. The chain walks **newest-first** (reverse of construction order) and terminates at the profiler → NULL. 17 nodes total: 16 are `.bss`-resident and built lazily under `__cxa_guard` inside `GetTpuPjrtApi`; the profiler is the lone `.data` static-init node and seeds the chain tail.

| # | Type ID | Extension | Size (B) | Owner page |
|---|---|---|---|---|
| 1 | 23 | HostMemoryAllocator (chain head) | 32 | [ext-remaining.md](ext-remaining.md) |
| 2 | 22 | MultiSlice | 64 | [ext-remaining.md](ext-remaining.md) |
| 3 | 21 | Collectives | 96 | [collectives-communicator.md](collectives-communicator.md) |
| 4 | 20 | AbiVersion | 120 | [ext-remaining.md](ext-remaining.md) |
| 5 | 19 | Shardings | 40 | [ext-remaining.md](ext-remaining.md) |
| 6 | 18 | Megascale | 248 | [collectives-communicator.md](collectives-communicator.md) |
| 7 | 17 | TpuExecutable | 88 | [executable-execution.md](executable-execution.md) |
| 8 | 16 | TpuTopology | 272 | [ext-topology-description.md](ext-topology-description.md) |
| 9 | 14 | Callback | 40 | [callbacks.md](callbacks.md) |
| 10 | 9 | PhaseCompile | 64 | [ext-compile-phasecompile.md](ext-compile-phasecompile.md) |
| 11 | 12 | CrossHostTransfers | 56 | [dma-and-cross-host-recv.md](dma-and-cross-host-recv.md) |
| 12 | 15 | HostAllocator | 48 | [ext-remaining.md](ext-remaining.md) |
| 13 | 13 | ExecutableMetadata | 40 | [executable-execution.md](executable-execution.md) |
| 14 | 6 | MemoryDescriptions | 40 | [buffer-and-memory.md](buffer-and-memory.md) |
| 15 | 4 | Layouts | 80 | [ext-remaining.md](ext-remaining.md) |
| 16 | 8 | RawBuffer | 80 | [ext-rawbuffer.md](ext-rawbuffer.md) |
| 17 | 1 | Profiler (.data, static) | 40 | [ext-profiler.md](ext-profiler.md) |

Type IDs 0, 2, 3, 5, 7, 10, 11 are unused in this build. Public XLA also registers FFI and Memory_Stream extension types that are **not** present here: libtpu's only custom-op surface is funnelled through the TpuExecutable extension's `SetTpuCompilationEnv`, not a public FFI extension.

> **QUIRK —** the chain order is reverse-of-construction, so iteration yields newest extensions first and the profiler last. A reimplementer **must** discover capabilities by walking `.next` until NULL and matching on `type`, never by position or by a fixed offset into the chain. The construction order (RawBuffer → … → HostMemoryAllocator, with the profiler seeded as RawBuffer's `.next`) is an implementation detail of `GetTpuPjrtApi`; only the type tags are contractual.

---

## 4. The Five TPU Injection Points

### Purpose

`CreatePjrtApi` takes only six function-pointer-ish arguments from its caller and hardcodes the rest. Five of those become TPU-specialized slots; the sixth is the extension chain head. Knowing which five slots are plugin-supplied (vs. generic `pjrt::PJRT_*` wrappers) tells a reimplementer exactly where the TPU backend hooks into an otherwise-generic XLA table.

### The injected slots

Confirmed against `CreatePjrtApi @ 0xf874160`: the body is a flat header-write plus `lea`/`mov` slot-fill with no loop, and exactly five slots are written from incoming register args (`a1[8]=a5`, `a1[9]=a7`, `a1[15]=a2`, `a1[87]=a4`, `a1[103]=a3`); slot 1 (`a1[1]=a6`) is the chain head.

| Slot | Field | libtpu impl | Addr | Role | Confidence |
|---|---|---|---|---|---|
| 8 | `PJRT_Plugin_Initialize` | `tpu_plugin::PJRT_Plugin_Initialize` | `0xE6A9D00` | One-time TPU driver bring-up (lifecycle) | CONFIRMED |
| 9 | `PJRT_Plugin_Attributes` | `pjrt::PJRT_Plugin_Attributes_Xla` | `0xF85F080` | Plugin attribute table — generic XLA impl, not a TPU override | CONFIRMED |
| 15 | `PJRT_Client_Create` | `tpu_plugin::PJRT_Client_Create` | `0xE6A8840` | Silicon scan + live client construction | CONFIRMED |
| 87 | `PJRT_TopologyDescription_Create` | `tpu_plugin::PJRT_TopologyDescription_Create` | `0xE6A9B20` | TPU pod topology (AOT, no client) | CONFIRMED |
| 103 | `PJRT_ExecuteContext_Create` | `tpu_plugin::PJRT_ExecuteContext_Create` | `0xE6A9A80` | Per-execution context | CONFIRMED |

The remaining 130 function-pointer slots are compile-fixed `pjrt::PJRT_*` wrappers (`lea`-loaded constants in `CreatePjrtApi`), shared with the generic XLA PJRT layer. The single most-called slot is **slot 60 `PJRT_LoadedExecutable_Execute @ 0xF869B40`** — the per-step program launch — which is a generic wrapper that bottoms out in the runtime's `CommonPjRtLoadedExecutable::Execute`; see [executable-execution.md](executable-execution.md) and [../runtime/overview.md](../runtime/overview.md).

> **NOTE —** slot 9 (`PJRT_Plugin_Attributes`) is *not* TPU-specialized despite being an injected argument: `CreatePjrtApi`'s caller passes the generic `pjrt::PJRT_Plugin_Attributes_Xla`. It advertises the standard XLA attribute set (version metadata, supported devices, serialization info). The genuinely TPU-specific injection points are only slots 8/15/87/103.

---

## Related Components

| Component | Relationship |
|---|---|
| `GetPjrtApi` / `GetTpuPjrtApi` | The exported entry symbol and its lazy-build engine |
| `pjrt::CreatePjrtApi @ 0xf874160` | The constructor that materializes the 140-slot table into `.lbss` |
| `pjrt::ActualStructSizeIsGreaterOrEqual @ 0xf8a4ec0` | The per-call backward-compat size gate every slot opens with |
| 17-extension chain (`0x224C3F68` head) | The typed, newest-first capability list reached via `extension_start` |
| `xla::TpuClient` / `tpu::System` | The runtime the generic slots bottom out in (modern PJRT stack) |
| `Tpu*_*` C-ABI (194 exports) | The legacy StreamExecutor surface that shares the binary but is not reached through PJRT |

## Cross-References

- [api-vtable-reconstruction.md](api-vtable-reconstruction.md) — the full 140-slot field-by-field table (every slot → impl symbol + address, hot-path ranking)
- [extension-chain.md](extension-chain.md) — the 17-node linked list, node-by-node layout, and `PJRT_Extension_Base` mechanics
- [client-and-device.md](client-and-device.md) — `PJRT_Client_*` / `PJRT_Device*` slots: create, lookup, device assignment, memory stats
- [buffer-and-memory.md](buffer-and-memory.md) — `PJRT_Buffer_*` / `PJRT_Memory_*` / AsyncHostToDevice transfer slots and the MemoryDescriptions extension
- [executable-execution.md](executable-execution.md) — `PJRT_Executable_*` / `PJRT_LoadedExecutable_*` / `PJRT_Compile` / ExecuteContext and the TpuExecutable + ExecutableMetadata extensions
- [events-and-async.md](events-and-async.md) — `PJRT_Event_*`, `PJRT_Error_*`, and AsyncTrackingEvent completion plumbing
- [callbacks.md](callbacks.md) — the Callback extension (type 14)
- [collectives-communicator.md](collectives-communicator.md) — the Collectives (type 21) and Megascale (type 18) extensions
- [dma-and-cross-host-recv.md](dma-and-cross-host-recv.md) — CopyToDeviceStream slots and the CrossHostTransfers extension (type 12)
- [ext-profiler.md](ext-profiler.md) — the Profiler extension (type 1), the only `.data` static-init chain node
- [ext-topology-description.md](ext-topology-description.md) — `PJRT_TopologyDescription_*` slots and the TpuTopology extension (type 16)
- [ext-rawbuffer.md](ext-rawbuffer.md) — the RawBuffer extension (type 8)
- [ext-compile-phasecompile.md](ext-compile-phasecompile.md) — the PhaseCompile extension (type 9)
- [ext-remaining.md](ext-remaining.md) — HostMemoryAllocator/HostAllocator, MultiSlice, AbiVersion, Shardings, Layouts extensions
- [stream-executor-host-interpreter.md](stream-executor-host-interpreter.md) — the StreamExecutor host/interpreter shim beneath the generic slots
- [stream-executor-pjrt-adapter.md](stream-executor-pjrt-adapter.md) — how the legacy StreamExecutor `Tpu*_*` C-ABI relates to the PJRT surface
- [../runtime/overview.md](../runtime/overview.md) — the runtime/execution layer the hot-path slots (esp. slot 60 `Execute`) dispatch into
- [../lifecycle/get-pjrt-api-thunk.md](../lifecycle/get-pjrt-api-thunk.md) — the `GetPjrtApi` thunk and lazy table-build entry
- [../lifecycle/module-init-plugin-discovery.md](../lifecycle/module-init-plugin-discovery.md) — `PJRT_Plugin_Initialize` driver bring-up and module-init DAG behind slots 8/15
