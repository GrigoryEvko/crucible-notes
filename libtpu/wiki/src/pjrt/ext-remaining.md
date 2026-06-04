# PJRT Remaining Extensions

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`), PJRT C-API **v0.103**. Other versions will differ.*

## Abstract

libtpu hangs 17 extension nodes off `PJRT_Api.extension_start`. Four of them have dedicated pages — Profiler (type 1), TopologyDescription/TpuTopology (type 16), RawBuffer (type 8), and PhaseCompile (type 9). This page is the deep dive of the **other thirteen**: the function-pointer set, struct size, type id, and behavior of every extension that does not own its own page. The node-header layout, the full `PJRT_Extension_Type` enum, the construction-vs-walk ordering, and the consumer walk loop are owned by [Extension Chain](extension-chain.md) and are not repeated here; this page assumes the reader has that structure in hand and goes straight to *what each node exposes*.

The thirteen split cleanly along one axis a reimplementer cares about: **who supplies the function pointers**. Eight are fully generic XLA surfaces whose creator bakes in `pjrt::`-namespace wrappers (Layouts, MemoryDescriptions, CrossHostTransfers, Shardings, Collectives, HostMemoryAllocator, plus the generic slots of AbiVersion). Three are TPU-only — every method is a `tpu_plugin::` or anonymous-namespace TPU implementation (Megascale, TpuExecutable, MultiSlice, HostAllocator, Callback). And two are hybrids whose creator takes TPU function pointers as parameters and fills the rest generically (ExecutableMetadata, AbiVersion's two `FromProto` factories; PhaseCompile is the third hybrid but is documented on its own page). Every creator is a flat table initializer — a run of `mov` stores ending in `ret`, no allocation, no branch — so the entire node set is recoverable by reading the store sequence, which is exactly how the sizes and slot layouts below were obtained.

The page groups the thirteen by that ownership axis, then catalogs each: the at-a-glance facts, the slot table (offset → method → impl symbol → address), and a `### Behavior` note for the methods whose bodies were decompiled. Two recurring themes anchor the reimplementation: every method's first action is a backward-compat `ActualStructSizeIsGreaterOrEqual` check (the `(min, current)` literal pair is the wire-compat contract for that method), and the generic methods all follow a *one-vtable-bounce* pattern — unwrap the opaque handle at args `+0x08`/`+0x10`, call one slot of a C++ abstract base's vtable, marshal the result back into the args struct.

For reimplementation, the contract is:

- The thirteen node layouts: `struct_size`, `type`, and the ordered fn-ptr tail from `+0x18`, each slot's impl symbol and virtual address.
- The TPU-injected vs generic split per node, and *which slots* receive injected pointers (so a reimplementation knows which to supply from the plugin backend and which are library-provided).
- The two deliberate *absences* — FFI (type 5) and Stream (type 3) are not advertised; their roles are subsumed by Callback and TpuExecutable. A consumer feature-detecting them gets `NULL`.
- The two near-identical host allocators (type 23 vs type 15) and why matching the wrong id is a bug.
- The per-method `ActualStructSizeIsGreaterOrEqual(name, min, current, args->struct_size)` contract for the decompiled bodies.

| | |
|---|---|
| **Owned by this page** | 13 extensions: Layouts (4), MemoryDescriptions (6), CrossHostTransfers (12), ExecutableMetadata (13), Callback (14), HostAllocator (15), TpuExecutable (17), Megascale (18), Shardings (19), AbiVersion (20), Collectives (21), MultiSlice (22), HostMemoryAllocator (23) |
| **Dedicated elsewhere** | Profiler (1) → [ext-profiler](ext-profiler.md); RawBuffer (8) → [ext-rawbuffer](ext-rawbuffer.md); PhaseCompile (9) → [ext-compile-phasecompile](ext-compile-phasecompile.md); TopologyDescription (16) → [ext-topology-description](ext-topology-description.md) |
| **Common header** | `{ size_t struct_size; uint32 type; uint32 _pad; PJRT_Extension_Base* next; }`, fn-ptrs from `+0x18` — see [Extension Chain](extension-chain.md#the-node-layout) |
| **Builder** | `pjrt::tpu_plugin::GetTpuPjrtApi` @ `0xE6AA440` (one `__cxa_guard` + `Create*Extension` call per node) |
| **Per-method compat gate** | `pjrt::ActualStructSizeIsGreaterOrEqual` @ `0xF8A4EC0` |
| **Largest here** | Megascale (18), 248 bytes, 23 live + 5 reserved-NULL slots |
| **Smallest here** | HostMemoryAllocator (23), 32 bytes, 1 method |
| **Deliberately absent** | FFI (5), Stream (3), Custom_Partitioner (2), Triton — roles subsumed by Callback (14) + TpuExecutable (17) |

> **NOTE —** "the remaining extensions" is a catalog, not an algorithm, so this page leans on the slot-table grammar (`Offset | Method | Impl symbol | Addr | Confidence`) rather than per-unit pseudocode. The two methods whose bodies were byte-traced — `HostMemoryAllocator_Allocate` and `Layouts::Client_GetDefaultLayout` — carry an `### Algorithm` block as the canonical example of the one-vtable-bounce pattern every other generic method follows.

---

## On the Two Deliberate Absences

The task title names "Stream" and "FFI" among the remaining extensions. Neither exists in this build. The creators write only the type ids `4, 6, 8, 9, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23` (plus the `.data` Profiler, type 1); ids `2` (Custom_Partitioner), `3` (Stream), `5` (FFI), `7`, `10`, `11`, and any Triton id are never stored — confirmed by the absence of any `Create*Extension` writing them and by the [enum table](extension-chain.md#the-pjrt_extension_type-enum) on the chain page. They are documented here as *absences with substitutes*, because a reimplementer who feature-detects them must know what libtpu does instead:

| Canonical extension | Type id | libtpu status | Role delivered instead by |
|---|---|---|---|
| FFI / custom-call host callbacks | 5 | absent | **Callback** (type 14) `RegisterCallback` — installs `xla::SliceBuilderCallbackState` host callbacks |
| FFI / compilation-env injection | 5 | absent | **TpuExecutable** (type 17) `SetTpuCompilationEnv` — installs `xla::CompilationEnvironmentsProto` |
| Stream (raw device-stream handles) | 3 | absent | not exposed; device-async surfaced via `PJRT_Event` on the main table |
| Custom_Partitioner | 2 | absent | not exposed; SPMD partitioning queried via **Shardings** (type 19) |
| ExecuteContext | (main table) | not an extension | main-table slots 103/104 (`PJRT_ExecuteContext_Create/Destroy`) |

> **GOTCHA —** a framework that *requires* the FFI extension (type 5) to register a custom call will feature-detect a `NULL` on TPU and must fall back to the Callback / TpuExecutable channels. The walk loop on [Extension Chain](extension-chain.md#how-a-framework-walks-the-chain) returns `NULL` for an absent id; absence is a valid answer, not an error.

---

## Group 1 — Generic XLA Surfaces

Eight nodes whose creators bake in only `pjrt::`-namespace wrappers, no TPU-injected pointers. They wrap TPU-backed C++ classes but the wire surface is the canonical XLA one. All eight follow the one-vtable-bounce template: unwrap the handle, call one vtable slot, marshal back.

### Layouts — type 4, 80 bytes

The canonical `PJRT_Layouts` surface: opaque `PjRtLayout` handles, serialize, default-layout queries from client or topology, per-buffer and per-executable layout retrieval. Creator `pjrt::CreateLayoutsExtension` @ `0xF8748C0`.

| | |
|---|---|
| **Storage VA** | `0x224C39E8` |
| **struct_size** | 80 (`(80-24)/8 = 7` methods) |
| **TPU-injected** | none — generic, TPU layout-assignment-backed |

| Offset | Method | Impl symbol | Addr |
|---|---|---|---|
| `+0x18` | `MemoryLayout_Destroy` | `pjrt::PJRT_Layouts_MemoryLayout_Destroy` | `0xF871360` |
| `+0x20` | `MemoryLayout_Serialize` | `pjrt::PJRT_Layouts_MemoryLayout_Serialize` | `0xF871400` |
| `+0x28` | `Client_GetDefaultLayout` | `pjrt::PJRT_Layouts_PJRT_Client_GetDefaultLayout` | `0xF8714A0` |
| `+0x30` | `Buffer_MemoryLayout` | `pjrt::PJRT_Layouts_PJRT_Buffer_MemoryLayout` | `0xF871620` |
| `+0x38` | `Topology_GetDefaultLayout` | `pjrt::PJRT_Layouts_PJRT_Topology_GetDefaultLayout` | `0xF8716A0` |
| `+0x40` | `Executable_GetOutputLayouts` | `pjrt::PJRT_Layouts_PJRT_Executable_GetOutputLayouts` | `0xF871C40` |
| `+0x48` | `Executable_GetParameterLayouts` | `pjrt::PJRT_Layouts_PJRT_Executable_GetParameterLayouts` | `0xF871820` |

#### Algorithm

`Client_GetDefaultLayout` @ `0xF8714A0` is the worked example of the bounce. The args layout: client handle at `+0x10`, element type at `+0x18`, dims pointer at `+0x20`, dims count at `+0x28`, output handle written to `+0x30`.

```c
function Layouts_Client_GetDefaultLayout(args):                 // 0xF8714A0
    // (min 0x2E=46, cur 0x38=56) — wire-compat literal pair
    if !ActualStructSizeIsGreaterOrEqual(
            "PJRT_Layouts_PJRT_Client_GetDefaultLayout_Args",
            46, 56, args->struct_size):
        return new PJRT_Error(status);                          // size too small
    client    = *(void**)(args + 0x10);                         // unwrap PJRT_Client
    elem_type = ConvertFromPjRtBufferType(*(u32*)(args + 0x18)); // 0xF8A3E60: C enum -> xla::PrimitiveType
    // client vtable +0x98 == GetDefaultLayout(elem_type, dims_ptr, dims_count)
    status = (*client->vtable[0x98])(client, elem_type,
                 *(void**)(args + 0x20), *(u64*)(args + 0x28));  // -> StatusOr<Layout>
    if status.ok():
        layout = new xla::Layout(...);                          // 0xF0-byte PjRtLayout wrapper
        *(void**)(args + 0x30) = box(layout);                   // opaque handle, 2-qword box
    return status_to_PJRT_Error(status);
```

> **NOTE —** the client vtable slot `+0x98` is the TPU `PjRtClient::GetDefaultLayout`. The whole Layouts extension never sees TPU code directly; it bounces every call through the live client/buffer/executable/topology handle's vtable. The returned `PjRtLayout` is a heap object (`new(0xF0)`) holding an `xla::Layout` at `+0x18`; the caller frees it through `MemoryLayout_Destroy` (`+0x18`).

---

### MemoryDescriptions — type 6, 40 bytes

Enumerates the memory-space descriptions attached to a `PJRT_DeviceDescription` — the *static* memory taxonomy (`"device"` / `"pinned_host"` / `"unpinned_host"` kinds) available at topology-query / pre-compile time, before any live `PJRT_Memory` object exists. Distinct from the live `PJRT_Memory` surface on [Buffer and Memory](buffer-and-memory.md). Creator `pjrt::CreateMemoryDescriptionsExtension` @ `0xF874940`.

| | |
|---|---|
| **Storage VA** | `0x224C3A40` |
| **struct_size** | 40 (2 methods) |
| **TPU-injected** | none — generic |

| Offset | Method | Impl symbol | Addr |
|---|---|---|---|
| `+0x18` | `DeviceDescription_MemoryDescriptions` | `pjrt::PJRT_DeviceDescription_MemoryDescriptions` | `0xF865580` |
| `+0x20` | `MemoryDescription_Kind` | `pjrt::PJRT_MemoryDescription_Kind` | `0xF865920` |

`DeviceDescription_MemoryDescriptions` returns the array of opaque `PJRT_MemoryDescription*` for a device description; `MemoryDescription_Kind` returns the kind string for one description. Pairing is the standard "list then query" idiom.

---

### CrossHostTransfers — type 12, 56 bytes

The cross-host (DCN) buffer-transfer surface: receive-buffer allocation with a descriptor handshake, point-to-point send/receive, and copy-to-remote-device. Used for pipeline-parallel and cross-pod buffer movement; the canonical `PjRtCrossHostTransfers`, TPU-backed. Creator `pjrt::CreateCrossHostTransfersExtension` @ `0xF85D660`. The companion main-table DMA path is on [DMA and Cross-Host Recv](dma-and-cross-host-recv.md).

| | |
|---|---|
| **Storage VA** | `0x224C3AD8` |
| **struct_size** | 56 (4 methods) |
| **TPU-injected** | none — generic |

| Offset | Method | Impl symbol | Addr |
|---|---|---|---|
| `+0x18` | `Client_MakeCrossHostReceiveBuffers` | `pjrt::PJRT_Transfers_PJRT_Client_MakeCrossHostReceiveBuffers` | `0xF85C9A0` |
| `+0x20` | `Buffer_CopyToRemoteDevice` | `pjrt::PJRT_Transfers_PJRT_Buffer_CopyToRemoteDevice` | `0xF85CE20` |
| `+0x28` | `Client_CrossHostReceiveBuffers` | `pjrt::PJRT_Transfers_PJRT_Client_CrossHostReceiveBuffers` | `0xF85BBA0` |
| `+0x30` | `Client_CrossHostSendBuffers` | `pjrt::PJRT_Transfers_PJRT_Client_CrossHostSendBuffers` | `0xF85C2A0` |

> **NOTE —** `MakeCrossHostReceiveBuffers` (descriptor-returning) and `CrossHostReceiveBuffers` (caller-supplied descriptors) are two distinct receive idioms — the first hands the descriptors back to the sender out-of-band; the second consumes descriptors already exchanged. A reimplementation must wire both.

---

### Shardings — type 19, 40 bytes

Exposes the per-parameter and per-output `xla::OpSharding` / `HloSharding` of a compiled executable, serialized as `OpSharding` protos for SPMD partitioning queries. Creator `pjrt::CreateShardingsExtension` @ `0xF874980`.

| | |
|---|---|
| **Storage VA** | `0x224C3E08` |
| **struct_size** | 40 (2 methods) |
| **TPU-injected** | none — generic |

| Offset | Method | Impl symbol | Addr |
|---|---|---|---|
| `+0x18` | `PJRT_Executable_ParameterShardings` | `pjrt::PJRT_Shardings_PJRT_Executable_ParameterShardings` | `0xF868000` |
| `+0x20` | `PJRT_Executable_OutputShardings` | `pjrt::PJRT_Shardings_PJRT_Executable_OutputShardings` | `0xF868A60` |

This is the substitute for the absent Custom_Partitioner (type 2): a consumer cannot register a custom partitioner, but it can read back the shardings the compiler chose.

---

### Collectives — type 21, 96 bytes

The canonical *in-process* XLA collectives surface — communicator lifecycle plus the seven collective primitives. CPU-executor-backed and generic; the *megascale* (cross-pod) collectives live in the [Megascale](#megascale--type-18-248-bytes) extension instead, not here. Creator `pjrt::CreateCollectivesExtension` @ `0xE6F19A0`. The main-table communicator surface is on [Collectives Communicator](collectives-communicator.md).

| | |
|---|---|
| **Storage VA** | `0x224C3EB8` |
| **struct_size** | 96 (9 methods) |
| **TPU-injected** | none — generic, CPU-executor-backed |

| Offset | Method | Impl symbol | Addr | min/cur |
|---|---|---|---|---|
| `+0x18` | `Collectives_Destroy` | `(anon)::CollectivesDestroy` | `0xE6F1A20` | 0x1D/0x10 |
| `+0x20` | `CreateCommunicators` | `(anon)::CollectivesCreateCommunicators` | `0xE6F1AA0` | 0x29/0x68 |
| `+0x28` | `Communicator_Destroy` | `(anon)::CommunicatorDestroy` | `0xE6F21A0` | — |
| `+0x30` | `Communicator_AllReduce` | `(anon)::CommunicatorAllReduce` | `0xE6F2220` | 0x2C/0x58 |
| `+0x38` | `Communicator_ReduceScatter` | `(anon)::CommunicatorReduceScatter` | `0xE6F2440` | — |
| `+0x40` | `Communicator_AllGather` | `(anon)::CommunicatorAllGather` | `0xE6F2660` | — |
| `+0x48` | `Communicator_CollectivePermute` | `(anon)::CommunicatorCollectivePermute` | `0xE6F2880` | — |
| `+0x50` | `Communicator_AllToAll` | `(anon)::CommunicatorAllToAll` | `0xE6F2CC0` | — |
| `+0x58` | `Communicator_ToString` | `(anon)::CommunicatorToString` | `0xE6F3280` | — |

#### Behavior

`CreateCommunicators` (min 0x29, cur 0x68) copies a caller `int32` array (data at args `+0x10`, count at `+0x18`) into a freshly-`new`'d buffer, then builds N communicators. `Communicator_AllReduce` (min 0x2C, cur 0x58) reads the input buffer descriptor at args `+0x10`/`+0x20` (device-ptr + count packed as two xmm-loaded 16-byte fields), the executor at `+0x48`, the element type at `+0x30` (via `ConvertFromPjRtBufferType` @ `0xF8A3E60`), the reduction op at `+0x40`; it allocates a `PJRT_Collectives_CpuExecutor` (`CreateCpuExecutor` @ `0xE6F3740`) and bounces through the communicator vtable slot `+0x30`, returning a 0x50-byte `PJRT_Event`.

> **NOTE —** only the `AllReduce` vtable index (`+0x30`) was byte-confirmed; `ReduceScatter`/`AllGather`/`CollectivePermute`/`AllToAll` vtable offsets are inferred from declaration ordering (MEDIUM confidence). The CpuExecutor backing is what makes this generic rather than TPU-specific.

---

### HostMemoryAllocator — type 23, 32 bytes

The generic XLA host-staging buffer allocator advertised at the chain head (walk position 1). One method. Creator `pjrt::CreateHostMemoryAllocatorExtension` @ `0xE6F5340`.

| | |
|---|---|
| **Storage VA** | `0x224C3F68` (chain head) |
| **struct_size** | 32 (1 method) |
| **TPU-injected** | none — generic |

| Offset | Method | Impl symbol | Addr | min/cur |
|---|---|---|---|---|
| `+0x18` | `Allocate` | `(anon)::HostMemoryAllocator_Allocate` | `0xE6F5380` | 0x26/0x40 |

#### Algorithm

`Allocate` @ `0xE6F5380` is the canonical worked example of the bounce-with-error-paths idiom. The args layout: memory-space/client handle at `+0x10`, size at `+0x18`, alignment (int) at `+0x20`, output `{data, deleter}` pair written from `+0x28`.

```c
function HostMemoryAllocator_Allocate(args):                    // 0xE6F5380
    // (min 0x26=38, cur 0x40=64)
    if !ActualStructSizeIsGreaterOrEqual(
            "PJRT_HostMemoryAllocator_Allocate_Args",
            38, 64, args->struct_size):
        return new PJRT_Error(status);
    client = *(void**)(args + 0x10);
    if client == NULL:
        return new PJRT_Error(InvalidArgument(                  // MakeErrorImpl<3>
            "Received null client in HostMemoryAllocator_Allocate"));
    // client vtable +0x108 -> the underlying host allocator (may be absent)
    allocator = (*client->vtable[0x108])(client);
    if allocator == NULL:
        return new PJRT_Error(Unimplemented(                    // MakeErrorImpl<12>
            "HostMemoryAllocator not implemented for client"));
    // allocator vtable +0x10 -> Allocate(size, &alignment)
    owned = (*allocator->vtable[0x10])(allocator,
                *(u64*)(args + 0x18), &(int){ *(u32*)(args+0x20) });
    if owned.data != NULL:
        *(pair*)(args + 0x28) = owned;                          // {void* data; deleter} 16-byte store
        *(void**)(args + 0x38) = owned.deleter;
    else:
        *(pair*)(args + 0x28) = {0, 0};                         // empty: zeroed store
    return OK;                                                  // returns 0 (no error)
```

> **GOTCHA —** this is the *generic* host allocator (type 23). Do not confuse it with [HostAllocator](#hostallocator--type-15-48-bytes) (type 15, three TPU-injected methods). Same family name, different node, different layout, different layer — type 23 is XLA host staging via the live client's vtable; type 15 is TPU pinned-host DMA staging with plugin-supplied function pointers.

---

### AbiVersion (generic portion) — type 20, 120 bytes

Cross-version compatibility checking between the runtime ABI and serialized executables: can a saved executable load against the current runtime? Ten of the twelve slots are generic `pjrt::`-namespace wrappers; the two `FromProto` factories are TPU-injected (see [Group 3](#group-3--hybrid-tpu-injected-factory)). Creator `pjrt::CreateAbiVersionExtension(node, runtime_fn, executable_fn, next)` @ `0xE6B8960`, called via the TPU thunk `CreateTpuAbiVersionExtension` @ `0xE6B7340`.

| | |
|---|---|
| **Storage VA** | `0x224C3E38` |
| **struct_size** | 120 (12 methods) |
| **TPU-injected** | 2 slots (`+0x68`, `+0x70`) — see Group 3 |

| Offset | Method | Impl symbol | Addr |
|---|---|---|---|
| `+0x18` | `Client_RuntimeAbiVersion` | `(anon)::ClientRuntimeAbiVersion` | `0xE6B8A00` |
| `+0x20` | `Executable_GetAbiVersion` | `(anon)::ExecutableGetAbiVersion` | `0xE6B8AE0` |
| `+0x28` | `RuntimeAbiVersion_Destroy` | `(anon)::RuntimeAbiVersionDestroy` | `0xE6B8BC0` |
| `+0x30` | `RuntimeAbiVersion_IsCompatibleWithRuntime` | `(anon)::RuntimeAbiVersionIsCompatibleWithRuntime` | `0xE6B8C40` |
| `+0x38` | `RuntimeAbiVersion_IsCompatibleWithExecutable` | `(anon)::RuntimeAbiVersionIsCompatibleWithExecutable` | `0xE6B8CA0` |
| `+0x40` | `RuntimeAbiVersion_ToProto` | `(anon)::RuntimeAbiVersionToProto` | `0xE6B8D00` |
| `+0x48` | `RuntimeAbiVersion_PlatformId` | `(anon)::RuntimeAbiVersionPlatformId` | `0xE6B8EA0` |
| `+0x50` | `ExecutableAbiVersion_Destroy` | `(anon)::ExecutableAbiVersionDestroy` | `0xE6B8F00` |
| `+0x58` | `ExecutableAbiVersion_ToProto` | `(anon)::ExecutableAbiVersionToProto` | `0xE6B8F80` |
| `+0x60` | `ExecutableAbiVersion_PlatformId` | `(anon)::ExecutableAbiVersionPlatformId` | `0xE6B9120` |
| `+0x68` | `RuntimeAbiVersion_FromProto` *(TPU)* | `(anon)::TpuRuntimeAbiVersionFromProto` | `0xE6B7360` |
| `+0x70` | `ExecutableAbiVersion_FromProto` *(TPU)* | `(anon)::TpuExecutableAbiVersionFromProto` | `0xE6B7380` |

> **QUIRK —** the creator writes `next` from its **`a4`** argument, not `a2`: `CreateAbiVersionExtension(node, runtime_fn, executable_fn, next)` stores `runtime_fn` at `+0x68`, `executable_fn` at `+0x70`, and `next` at `+0x10`. The thunk `CreateTpuAbiVersionExtension(node, next)` just tail-calls it with the two TPU `FromProto` functions wired in. A reimplementation that assumes the `next` is always the second creator argument will mis-link this node.

---

## Group 2 — Fully TPU-Specific Surfaces

Five nodes (Megascale, TpuExecutable, MultiSlice, Callback, plus HostAllocator) whose every slot is a TPU implementation — either an anonymous-namespace TPU function baked into the creator, or (for HostAllocator) a `tpu_plugin::` pointer injected as a creator parameter. A reimplementation must supply the backend for all of these.

### Megascale — type 18, 248 bytes

The largest extension here and the multi-pod data-center-network (DCN) runtime control surface used by multi-host JAX training: client-context lifecycle, ahead-of-time (AoT) config, multi-slice config (companion to MultiSlice), the megascale collectives factory, device↔megascale id translation, and a complete asynchronous **error-aggregation subsystem** for fault-tolerant training. Creator `pjrt::CreateMegascaleExtension` @ `0xE6B97C0`. Entirely TPU/megascale-specific.

| | |
|---|---|
| **Storage VA** | `0x224C3D08` |
| **struct_size** | 248 (`(248-24)/8 = 28` slots: 23 live + 5 reserved-NULL) |
| **Reserved slots** | `+0x40`..`+0x60` — zeroed at construction (`vmovups ymm0` over `+0x40..+0x5F`, then `movq $0` at `+0x60`) |
| **TPU-injected** | all 23 live slots are anon-namespace TPU implementations |

```text
+0x18  CreateClientContextFromPjRtClient   0xE6B9920    +0x88  DeviceId_To_MegascaleId            0xE6BA8E0
+0x20  CreateDefaultClientContext          0xE6B9A20    +0x90  MegascaleId_To_DeviceId            0xE6BA980
+0x28  DeleteClientContext                 0xE6B9B20    +0x98  RegisterMegascaleErrorHandler      0xE6BAA60
+0x30  CreateAoTConfig                     0xE6B9BC0    +0xA0  UnregisterMegascaleErrorHandler    0xE6BAB20
+0x38  CreateMultiSliceConfig              0xE6B9CA0    +0xA8  ErrorAggregator_Create             0xE6BAB80
+0x40  (reserved, NULL)                    —            +0xB0  ErrorAggregator_Delete             0xE6BAC00
+0x48  (reserved, NULL)                    —            +0xB8  ErrorDigest_Delete                 0xE6BACE0
+0x50  (reserved, NULL)                    —            +0xC0  ErrorAggregator_AddError           0xE6BAD60
+0x58  (reserved, NULL)                    —            +0xC8  ErrorAggregator_ProcessAndShutdown 0xE6BAE40
+0x60  (reserved, NULL)                    —            +0xD0  ErrorAggregator_LogErrorDigest     0xE6BAEC0
+0x68  ClientContext_Initialize            0xE6B9EC0    +0xD8  ErrorAggregator_Size               0xE6BAF20
+0x70  ClientContext_UnblockPendingWork    0xE6B9F20    +0xE0  ErrorAggregator_Active             0xE6BAF80
+0x78  ClientContext_MegascalePort         0xE6B9FE0    +0xE8  GetInterfaceAddressesHelper        0xE6BAFE0
+0x80  CreateMegascaleCollectives          0xE6BA080    +0xF0  GetOrCreateRuntimeError            0xE6BB620
```

All symbols are `pjrt::(anonymous namespace)::<Name>`. Functional grouping:

- **Client-context lifecycle** (`+0x18`–`+0x28`, `+0x68`–`+0x78`): create a megascale client context from a `PjRtClient` or default-construct one, delete it, initialize it, unblock pending work, query the megascale port.
- **Config factories** (`+0x30`–`+0x38`): AoT config and multi-slice config — the latter produces the `PJRT_MultiSlice_Config` handle that the [MultiSlice](#multislice--type-22-64-bytes) extension queries.
- **Collectives + id maps** (`+0x80`–`+0x90`): the megascale (cross-pod) collectives factory and bidirectional device-id↔megascale-id translation. This is where cross-pod collectives live, *not* in the [Collectives](#collectives--type-21-96-bytes) extension.
- **Error-aggregation subsystem** (`+0x98`–`+0xF0`): register/unregister error handlers, then a full async aggregator — create/delete the aggregator, add per-host errors, process-and-shutdown, log an error digest, query size and active state, plus interface-address discovery and runtime-error fetch. This is the fault-tolerance machinery for multi-host training: collect per-host errors, decide whether to shut down the slice, emit a digest.

> **NOTE —** the five reserved-NULL slots at `+0x40`..`+0x60` are declared-but-unimplemented placeholders (confirmed zeroed at construction; their intended future client-context methods are unknown — LOW). A consumer must not call through them; `struct_size` (248) covers the slots but the pointers are `NULL`. The full args-struct layouts for the rarely-called error-aggregator methods were not byte-traced (the slot table above is from the creator store sequence; method-arg offsets are LOW).

---

### TpuExecutable — type 17, 88 bytes

The most TPU-specific compiled-executable surface: target-argument and HLO-module introspection, compiled-memory and cost analysis, and — most importantly for a reimplementer — the **compilation-env injection** path (`SetTpuCompilationEnv`) and the **predetermined-error classifier** (`IsTpuPredeterminedError`) used by megascale fault tolerance. Creator `pjrt::CreateTpuExecutableExtension` @ `0xE6DC6E0`.

| | |
|---|---|
| **Storage VA** | `0x224C3CA8` |
| **struct_size** | 88 (7 live + 1 reserved-NULL at `+0x28`) |
| **TPU-injected** | all 7 live slots are anon-namespace TPU implementations |

| Offset | Method | Impl symbol | Addr | min/cur |
|---|---|---|---|---|
| `+0x18` | `GetTargetArguments` | `(anon)::GetTargetArguments` | `0xE6DC760` | — |
| `+0x20` | `GetHloModuleWithConfig` | `(anon)::GetHloModuleWithConfig` | `0xE6DC8A0` | — |
| `+0x28` | *(reserved, NULL)* | — | — | — |
| `+0x30` | `GetCompiledMemoryStats` | `(anon)::GetCompiledMemoryStats` | `0xE6DCA40` | — |
| `+0x38` | `RunHloCostAnalysis` | `(anon)::RunHloCostAnalysis` | `0xE6DCC40` | — |
| `+0x40` | `SetTpuCompilationEnv` | `(anon)::SetTpuCompilationEnv` | `0xE6DD400` | 0x2C/0x38 |
| `+0x48` | `GetTpuCompilationEnvFieldAsString` | `(anon)::GetTpuCompilationEnvFieldAsString` | `0xE6DD620` | — |
| `+0x50` | `IsTpuPredeterminedError` | `(anon)::IsTpuPredeterminedError` | `0xE6DD880` | 0x2F/0x19 |

#### Behavior

`SetTpuCompilationEnv` (min 0x2C, cur 0x38) parses a serialized `xla::CompilationEnvironmentsProto` (bytes at args `+0x08`/`+0x10`) via `proto2::MessageLite::ParseFromString` @ `0x21057460`, builds an `xla::CompilationEnvironments` through `CreateFromProto` @ `0x1E63E5A0`, then installs it process-globally via `tpu_executable_extension::SetTpuCompilationEnv(CompilationEnvironments*)` @ `0xE6DE1C0`. This is the TPU compilation-backend config / XLA-flags injection surface — the libtpu substitute for a custom-call/FFI env.

`IsTpuPredeterminedError` (min 0x2F, cur 0x19) parses a serialized `tensorflow::StatusProto` (args `+0x08`/`+0x10`), converts to `absl::Status` via `tsl::StatusFromProto` @ `0xF8BB9E0`, calls `tpu_executable_extension::IsTpuPredeterminedError(absl::Status)` @ `0xE6DE4A0`, and writes a `bool` to args `+0x18`. It classifies whether a runtime error is "predetermined" (detectable before execution) for fault-tolerant restart logic in megascale training.

> **NOTE —** `SetTpuCompilationEnv` and `IsTpuPredeterminedError` share the same `.rodata` source-location string at VA `0x877CD0F` — the `tpu_executable_extension.cc` source-file path passed as the `absl::SourceLocation` argument to their status constructors, not an error message. Both are the FFI-substitute channels named on [Extension Chain](extension-chain.md#the-pjrt_extension_type-enum): a framework that cannot find an FFI extension uses these to push compiler config and read fault classification.

---

### MultiSlice — type 22, 64 bytes

Queries a `PJRT_MultiSlice_Config` opaque wrapper — an 8-byte heap object holding a `unique_ptr<MultiSliceConfig>` at `+0x00`. Slice-topology introspection for multi-slice (cross-pod) training; the config itself is produced by Megascale's `CreateMultiSliceConfig`. Creator `pjrt::CreateMultiSliceExtension` @ `0xE6F3C40`.

| | |
|---|---|
| **Storage VA** | `0x224C3F20` |
| **struct_size** | 64 (5 methods) |
| **TPU-injected** | all anon-namespace TPU implementations |

| Offset | Method | Impl symbol | Addr | min/cur |
|---|---|---|---|---|
| `+0x18` | `Config_Destroy` | `(anon)::ConfigDestroy` | `0xE6F3CA0` | 0x23/0x10 |
| `+0x20` | `Config_NumSlices` | `(anon)::ConfigNumSlices` | `0xE6F3D20` | 0x25/0x14 |
| `+0x28` | `Config_SliceId` | `(anon)::ConfigSliceId` | `0xE6F3D80` | — |
| `+0x30` | `Config_NumDevicesPerSlice` | `(anon)::ConfigNumDevicesPerSlice` | `0xE6F3DE0` | — |
| `+0x38` | `Config_Serialize` | `(anon)::ConfigSerialize` | `0xE6F3FE0` | — |

#### Behavior

`Config_Destroy` reads the wrapper at args `+0x08`; if non-null it derefs `wrapper->impl` vtable `+0x08` (the dtor), nulls the pointer, and `free(wrapper, 8)`. `Config_NumSlices` bounces `wrapper->impl` vtable `+0x10` and writes the `int32` to args `+0x10`. The other readers (`SliceId`, `NumDevicesPerSlice`, `Serialize`) follow the same one-vtable-bounce template at different vtable offsets.

> **NOTE —** the `MultiSliceConfig` serialization proto schema used by `Config_Serialize` @ `0xE6F3FE0` was not recovered (LOW). The handle-at-`+0x08` convention (no `priv`) matches the TpuTopology family; confirmed by the `mov 0x8(%rbx),...` reads.

---

### Callback — type 14, 40 bytes

Host-side callbacks fired on TPU slice-builder fault events — the libtpu host-callback / fault-handler surface that substitutes for a generic FFI host-callback extension. Creator `pjrt::CreateCallbackExtension` @ `0xE6B91E0`.

| | |
|---|---|
| **Storage VA** | `0x224C3B60` |
| **struct_size** | 40 (2 methods) |
| **TPU-injected** | all anon-namespace TPU implementations |

| Offset | Method | Impl symbol | Addr | min/cur |
|---|---|---|---|---|
| `+0x18` | `RegisterCallback` | `(anon)::PJRT_Callback_RegisterCallback` | `0xE6B9220` | 0x23/0x28 |
| `+0x20` | `InvokeCallback` | `(anon)::PJRT_Callback_InvokeCallback` | `0xE6B94C0` | — |

#### Behavior

`RegisterCallback` (min 0x23, cur 0x28) reads a callback-type enum at args `+0x10`. For `type==1` it validates that the target device is a TPU — checking the device-kind id against `xla::TpuId()::kTpuId` @ `0x224C3FC8` (guard `0x224C3FD0`) — then registers a `std::function<void(accel_ssw::deepsea::slice_builder::SliceFailureType)>` into `xla::SliceBuilderCallbackState::AddCallback` @ `0xF95DF80` (via the `RegisterSliceBuilderCallback::$_0` policy thunk @ `0xE6B96C0`, policy @ `0x215FB718`). `type==2` is a second callback class.

> **NOTE —** only the `type==1` (slice-builder) path was fully traced; the `type==2` dispatch target was not (LOW). See [Callbacks](callbacks.md) for the broader host-callback surface.

---

### HostAllocator — type 15, 48 bytes

The TPU pinned-host memory allocator used for device-host DMA staging — distinct from the generic [HostMemoryAllocator](#hostmemoryallocator--type-23-32-bytes) (type 23) at the chain head. All three slots are `tpu_plugin::` TPU implementations injected as creator parameters. Creator `pjrt::CreateHostAllocatorExtension(node, next, prefalign_fn, alloc_fn, free_fn)` @ `0xF8A3C20` — `node` is the return slot, `next` (`a2`) goes to `+0x10`, and the creator writes the three TPU pointers from its `a3`/`a4`/`a5` arguments to `+0x18`/`+0x20`/`+0x28` directly.

| | |
|---|---|
| **Storage VA** | `0x224C3AA0` |
| **struct_size** | 48 (3 methods) |
| **TPU-injected** | all 3 slots (`a3`/`a4`/`a5`) |

| Offset | Method | Impl symbol | Addr |
|---|---|---|---|
| `+0x18` | `GetPreferredAlignment` | `tpu_plugin::TPU_PJRT_HostAllocator_GetPreferredAlignment` | `0xE6AA060` |
| `+0x20` | `Allocate` | `tpu_plugin::TPU_PJRT_HostAllocator_Allocate` | `0xE6AA140` |
| `+0x28` | `Free` | `tpu_plugin::TPU_PJRT_HostAllocator_Free` | `0xE6AA240` |

> **GOTCHA —** the two host allocators are the classic confusion in this chain. Type **23** (`HostMemoryAllocator`, 32 bytes, 1 method, generic, no `priv`, payload pointer at `+0x08`) is XLA host staging via the live client vtable. Type **15** (`HostAllocator`, 48 bytes, 3 methods, all TPU-injected) is TPU pinned-host DMA staging with plugin-supplied pointers. Different struct layouts, different layers; matching on the wrong type id returns the wrong allocator.

---

## Group 3 — Hybrid (TPU-Injected Factory)

Nodes whose creator takes one or two TPU function pointers as parameters and fills the rest with generic wrappers. The reimplementer supplies only the injected factory; the library provides the surrounding methods.

### ExecutableMetadata — type 13, 40 bytes

Returns serialized executable metadata (a TPU-specific blob describing a compiled executable) plus a deleter for the returned buffer. The `Get` path is TPU-supplied; `Destroy` is generic. Creator `pjrt::CreateExecutableMetadataExtension(node, next, get_metadata_fn)` @ `0xF8A3BE0`.

| | |
|---|---|
| **Storage VA** | `0x224C3A70` |
| **struct_size** | 40 (2 methods) |
| **TPU-injected** | 1 slot (`+0x18`, the `Get` path) |

| Offset | Method | Impl symbol | Addr |
|---|---|---|---|
| `+0x18` | `GetExecutableMetadata` *(TPU)* | `tpu_plugin::GetTpuExecutableMetadata` | `0xE6A9E40` |
| `+0x20` | `DestroySerializedMetadata` | `pjrt::DestroySerializedMetadata` | `0xF8A3BA0` |

> **QUIRK —** the injected TPU function lands in the *first* slot (`+0x18`), with the generic deleter at `+0x20`. The creator stores its `a3` (the TPU `get` fn) at `+0x18` and the baked-in `DestroySerializedMetadata` at `+0x20`. This is the inverse arrangement from PhaseCompile (TPU factory first) and matches it conceptually: the producer is TPU, the destructor is generic.

### AbiVersion FromProto factories — type 20

The two TPU-injected slots of [AbiVersion](#abiversion-generic-portion--type-20-120-bytes) (documented above in Group 1 for the bulk of the node). The thunk `CreateTpuAbiVersionExtension` @ `0xE6B7340` supplies them:

| Offset | Method | Impl symbol | Addr | Wraps |
|---|---|---|---|---|
| `+0x68` | `RuntimeAbiVersion_FromProto` | `(anon)::TpuRuntimeAbiVersionFromProto` | `0xE6B7360` | `xla::PjRtRuntimeAbiVersionFromProto` @ `0xE6B73E0` |
| `+0x70` | `ExecutableAbiVersion_FromProto` | `(anon)::TpuExecutableAbiVersionFromProto` | `0xE6B7380` | `xla::PjRtExecutableAbiVersionFromProto` @ `0xE6B7660` |

Each consumes its proto (`xla::PjRtRuntimeAbiVersionProto` / `xla::PjRtExecutableAbiVersionProto`) and produces a `StatusOr<unique_ptr<...AbiVersion>>` through `pjrt::Common{Runtime,Executable}AbiVersionFromProto` @ `0xE6B86A0` / `0xE6B8800`. They are TPU-supplied because the proto carries the TPU platform id; the rest of the AbiVersion surface is platform-agnostic.

### PhaseCompile (Get/Destroy compiler) — type 9

PhaseCompile is the third hybrid: `CreatePhaseCompileExtension(node, next, get_compiler_fn, destroy_compiler_fn)` @ `0xE6F42A0` injects `tpu_plugin::GetTpuPhaseCompiler` @ `0xE6AA320` (`+0x18`) and `tpu_plugin::DestroyTpuPhaseCompiler` @ `0xE6AA400` (`+0x20`); the three driver methods (`Run_Phase`, `Get_Phase_Names`, `C_Buffers_Destroy`, `+0x28`/`+0x30`/`+0x38`) are generic. The full compiler-object internals are on its dedicated page — see [PhaseCompile](ext-compile-phasecompile.md).

---

## TPU-Injected vs Generic — Reimplementer's Cheat Sheet

The single most useful classification for a reimplementer: which nodes need a TPU backend and which are library-provided. (Profiler, RawBuffer, PhaseCompile, TpuTopology are on their own pages and shown here only for completeness of the split.)

| Class | Extensions | What the reimplementer supplies |
|---|---|---|
| **Fully TPU** | Megascale (18), TpuExecutable (17), HostAllocator (15), MultiSlice (22), Callback (14), TpuTopology (16) | every method body |
| **TPU-injected factory** | AbiVersion (20) [2 `FromProto`], ExecutableMetadata (13) [`Get`], PhaseCompile (9) [`Get`/`Destroy` compiler] | only the injected factory fn(s); library provides the rest |
| **Generic XLA (TPU-backed)** | Layouts (4), MemoryDescriptions (6), RawBuffer (8), CrossHostTransfers (12), Shardings (19), Collectives (21), HostMemoryAllocator (23), Profiler (1) | nothing extension-side; bounces through live handle vtables |

---

## Related Components

| Component | Relationship |
|---|---|
| [Extension Chain](extension-chain.md) | owns the node header, the type-id enum, the inventory, and the walk; this page is its per-node deep dive |
| [Buffer and Memory](buffer-and-memory.md) | the live `PJRT_Memory` surface that MemoryDescriptions (static) precedes |
| [Collectives Communicator](collectives-communicator.md) | main-table communicator surface paralleling the Collectives (21) extension |
| [DMA and Cross-Host Recv](dma-and-cross-host-recv.md) | main-table DMA path paralleling CrossHostTransfers (12) |
| [Callbacks](callbacks.md) | the broader host-callback surface the Callback (14) extension feeds |

## Cross-References

- [Extension Chain](extension-chain.md) — node layout, `PJRT_Extension_Type` enum, 17-node inventory, construction-vs-walk order, consumer walk loop
- [PJRT Plugin Overview](overview.md) — how `dlsym("GetPjrtApi")` reaches `GetTpuPjrtApi` and the one-shot init
- [API Vtable Reconstruction](api-vtable-reconstruction.md) — the 140 main-table slots that `extension_start` hangs off (a separate structure; ExecuteContext lives here, not as an extension)
- [Profiler Extension](ext-profiler.md) — type 1, the `.data` seed / chain terminator
- [Topology Description Extension](ext-topology-description.md) — type 16, the largest live extension (31 methods)
- [RawBuffer Extension](ext-rawbuffer.md) — type 8, untyped device-memory surface
- [PhaseCompile Extension](ext-compile-phasecompile.md) — type 9, named-phase partial compilation and the injected TPU compiler factory
