# PJRT Extension Chain

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`), PJRT C-API **v0.103**. Other versions will differ.*

## Abstract

The PJRT C-API has a fixed-width vtable — 140 function-pointer slots, frozen at the version the plugin was built against (here v0.103). A plugin that wants to expose capabilities *beyond* that frozen surface cannot add slots without bumping the version, so the API carries a second, open-ended channel: a singly-linked list of **extension** nodes hung off `PJRT_Api.extension_start`. Each node begins with a common header — `{ size_t struct_size; uint32 type; uint32 _pad; PJRT_Extension_Base* next; }` — and a framework discovers a feature by walking `next` until it finds a node whose `type` matches a known `PJRT_Extension_Type` id, or hits `NULL`. This is the same pattern LLVM uses for pass-plugin registries and that COM uses for `QueryInterface`: a stable ABI spine plus a self-describing, version-tolerant side table.

libtpu publishes **17 extensions**. Sixteen of them live in `.lbss`/`.bss` as zero-initialized function-local statics inside `pjrt::tpu_plugin::GetTpuPjrtApi` @ `0xE6AA440`; each is populated exactly once, under a C++ `__cxa_guard`, by a flat table-initializer `pjrt::Create<Name>Extension(node, next, ...)` that writes `struct_size`, `type`, `next`, and a fixed run of fn-ptr slots, then returns. The 17th, Profiler (type 1), lives in `.data` with static-init relocations and serves as the chain *seed* — RawBuffer's `next` points at it, and its own `next` is `NULL`. Because each new node's `next` points at the previously-built node, the list is built tail-first; `extension_start` is set to the *last*-built node, so a consumer walking the chain sees the extensions newest-first. The order carries no meaning to consumers — discovery is a type-id linear scan — but it mirrors the source-level declaration order inside `GetTpuPjrtApi`.

This page owns the **node layout**, the **`PJRT_Extension_Type` enum**, the **complete ordered extension inventory**, and the **walk algorithm**. The per-extension deep dives live on dedicated pages (linked in the inventory table). The 140 *non-extension* function-pointer slots — a separate structure entirely — are reconstructed on [API Vtable Reconstruction](api-vtable-reconstruction.md).

For reimplementation, the contract is:

- The `PJRT_Extension_Base` common header: field order, offsets, the 32-bit `type` plus 4-byte pad, and `next` at `+0x10`.
- The `PJRT_Extension_Type` enum: which ids libtpu uses (1, 4, 6, 8, 9, 12–23), which it deliberately omits (0, 2, 3, 5, 7, 10, 11), and what the omissions imply.
- The 17-node inventory in walk order, each node's storage VA, `struct_size`, `type`, and the creator that populates it.
- The construction-order-vs-walk-order inversion and why it is harmless.
- The consumer-side feature-detection loop and its forward-compatibility contract.

| | |
|---|---|
| **Chain head** | `PJRT_Api.extension_start` @ `+0x08` → `0x224C3F68` (HostMemoryAllocator node) |
| **Node header** | `{ size_t struct_size; uint32 type; uint32 _pad; PJRT_Extension_Base* next; }` — 24 bytes, `next` at `+0x10` |
| **Chain length** | 17 nodes; terminator is Profiler (type 1), `next = NULL` |
| **Type ids present** | 1, 4, 6, 8, 9, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23 |
| **Type ids absent** | 0, 2, 3, 5, 7, 10, 11 (incl. canonical FFI, Custom_Partitioner, Stream, Triton) |
| **Builder** | `pjrt::tpu_plugin::GetTpuPjrtApi` @ `0xE6AA440` (16 `__cxa_guard` blocks + a 17th for `pjrt_api`) |
| **Largest node** | Megascale (type 18) @ `0x224C3D08`, 248 bytes (0xF8), 24 live + 5 reserved slots |
| **Smallest node** | HostMemoryAllocator (type 23) @ `0x224C3F68`, 32 bytes, 1 method |

---

## The Node Layout

Every node is a `PJRT_Extension_Base` followed by zero or more 8-byte function pointers. The header is invariant across all 17 extensions; only the `struct_size` and the fn-ptr tail differ.

### Common Header

```c
struct PJRT_Extension_Base {
    /* +0x00 */ size_t                struct_size;   // per-extension total, in bytes
    /* +0x08 */ uint32_t              type;          // PJRT_Extension_Type enum value
    /* +0x0C */ uint32_t              _pad;          // padding to 8-byte align `next`
    /* +0x10 */ PJRT_Extension_Base*  next;          // chain link; NULL terminates
    /*  ...   fn-ptr slots from +0x18, one qword each ... */
};
```

The header is confirmed directly by every creator's store sequence. Two representative ones:

```c
// pjrt::CreateHostMemoryAllocatorExtension @ 0xE6F5340  (type 23, 32 bytes)
function CreateHostMemoryAllocatorExtension(node, next):
    *(size_t*) (node + 0x00) = 32;                     // struct_size
    *(uint32_t*)(node + 0x08) = 23;                    // type  (32-bit store!)
    *(void**)  (node + 0x10) = next;                   // chain link
    *(void**)  (node + 0x18) = HostMemoryAllocator_Allocate;
    return node;

// pjrt::CreateShardingsExtension @ 0xF874980  (type 19, 40 bytes)
function CreateShardingsExtension(node, next):
    *(size_t*) (node + 0x00) = 40;
    *(uint32_t*)(node + 0x08) = 19;
    *(void**)  (node + 0x10) = next;
    *(void**)  (node + 0x18) = PJRT_Shardings_..._ParameterShardings;
    *(void**)  (node + 0x20) = PJRT_Shardings_..._OutputShardings;
    return node;
```

> **GOTCHA —** `type` is a **32-bit** field at `+0x08`, not a 64-bit word. The creators emit `mov dword ptr [node+8], <id>` (e.g. `*(_DWORD*)(a1 + 8) = 23`), leaving `+0x0C` as a 4-byte hole that the zero-initialized `.bss`/`.data` backing storage keeps at zero. A reimplementation that declares `type` as `size_t` will mis-place `next` by 4 bytes on the read side and walk garbage. Read `type` as `uint32`, then skip 4 bytes of pad, then read `next` at `+0x10`.

> **QUIRK —** the creators are *pure table initializers* — a flat run of `mov` stores ending in `ret`, with no allocation and no branch. That is the entire reason the 16 dynamic nodes can live in zero-init `.bss`: there is nothing to construct, only to fill. The one-shot `__cxa_guard` is the only synchronization; after first call the nodes are immutable for the process lifetime.

### Fn-ptr Tail and Args-Struct Convention

Slots from `+0x18` are method pointers. `struct_size` is the *full* node size including this tail, so `(struct_size - 0x18) / 8` gives the method count (e.g. HostMemoryAllocator `(32 - 24)/8 = 1`; Shardings `(40 - 24)/8 = 2`; Megascale `(248 - 24)/8 = 28`, of which 24 are live and 5 are reserved NULL — see [Remaining Extensions](ext-remaining.md)). Each method takes a single `PJRT_<API>_Args*`; like the main vtable, every method body's first action is a backward-compat size check via `pjrt::ActualStructSizeIsGreaterOrEqual("<API>_Args", min, current, args->struct_size)` @ `0xF8A4EC0`. Wrapper-handle args place the opaque handle at `+0x08` (no `priv` field).

---

## The `PJRT_Extension_Type` Enum

The type id is the discovery key. libtpu's ids match the public `xla/pjrt/c/pjrt_c_api.h` v0.103 family exactly; the table below is the full enum as it manifests in this binary, with present/absent status read from the creator `type` stores and the canonical header.

| Type id | Name | Status in libtpu | Documented on |
|---|---|---|---|
| 0 | (unused/reserved) | absent | — |
| 1 | Profiler | present (.data seed, type-1 terminator) | [Profiler](ext-profiler.md) |
| 2 | Custom_Partitioner | **absent** | — (role subsumed; see note) |
| 3 | Stream | **absent** | — |
| 4 | Layouts | present | [Remaining Extensions](ext-remaining.md) |
| 5 | FFI | **absent** | — (role subsumed; see note) |
| 6 | MemoryDescriptions | present | [Remaining Extensions](ext-remaining.md) |
| 7 | (reserved / Memory_Stream in some trees) | **absent** | — |
| 8 | RawBuffer | present | [RawBuffer](ext-rawbuffer.md) |
| 9 | PhaseCompile | present | [PhaseCompile](ext-compile-phasecompile.md) |
| 10 | (unused) | absent | — |
| 11 | (unused) | absent | — |
| 12 | CrossHostTransfers | present | [Remaining Extensions](ext-remaining.md) |
| 13 | ExecutableMetadata | present (TPU factory) | [Remaining Extensions](ext-remaining.md) |
| 14 | Callback | present (TPU slice-builder) | [Remaining Extensions](ext-remaining.md) |
| 15 | HostAllocator | present (TPU) | [Remaining Extensions](ext-remaining.md) |
| 16 | TopologyDescription / TpuTopology | present (TPU) | [Topology Description](ext-topology-description.md) |
| 17 | TpuExecutable | present (TPU) | [Remaining Extensions](ext-remaining.md) |
| 18 | Megascale | present (TPU) | [Remaining Extensions](ext-remaining.md) |
| 19 | Shardings | present | [Remaining Extensions](ext-remaining.md) |
| 20 | AbiVersion | present | [Remaining Extensions](ext-remaining.md) |
| 21 | Collectives | present | [Remaining Extensions](ext-remaining.md) |
| 22 | MultiSlice | present (TPU) | [Remaining Extensions](ext-remaining.md) |
| 23 | HostMemoryAllocator | present | [Remaining Extensions](ext-remaining.md) |

> **NOTE —** the **absences are load-bearing for a reimplementer**. The canonical PJRT C-API registers FFI (5), Custom_Partitioner (2), Stream (3), and Triton ids that libtpu does **not** advertise. libtpu delivers what an FFI / custom-call extension would otherwise provide through two TPU-specific channels: the **Callback** extension (type 14) `RegisterCallback` path installs `xla::SliceBuilderCallbackState` host callbacks, and the **TpuExecutable** extension (type 17) `SetTpuCompilationEnv` path installs a serialized `xla::CompilationEnvironmentsProto` (the TPU compilation-backend/XLA-flags surface). `ExecuteContext` is exposed only through main-table slots 103/104, not as an extension. A framework that *requires* the FFI extension to be present will feature-detect a `NULL` on TPU and must fall back to these channels.

> **GOTCHA —** there are **two** distinct host-memory allocators with near-identical names. `HostMemoryAllocator` (type **23**, 32 bytes, one `Allocate` method) is the generic XLA host-staging allocator at the chain head. `HostAllocator` (type **15**, 48 bytes, three methods, all TPU-injected) is the TPU pinned-host allocator for device-host DMA staging. They have different layouts and serve different layers; matching on the wrong type id gets you the wrong allocator.

---

## The Extension Inventory (Walk Order)

The complete chain, in the order a consumer encounters it when walking `next` from `extension_start`. The walk is newest-first (reverse of construction). All storage VAs, sizes, and type ids are confirmed against the creator `type`/`struct_size` stores and the binary symbol table.

| Walk | Storage VA | Extension (type id) | Size | Methods | Creator @ | Deep dive |
|---|---|---|---|---|---|---|
| 1 | `0x224C3F68` | HostMemoryAllocator (23) | 32 | 1 | `CreateHostMemoryAllocatorExtension` @ `0xE6F5340` | [Remaining](ext-remaining.md) |
| 2 | `0x224C3F20` | MultiSlice (22) | 64 | 5 | `CreateMultiSliceExtension` @ `0xE6F3C40` | [Remaining](ext-remaining.md) |
| 3 | `0x224C3EB8` | Collectives (21) | 96 | 9 | `CreateCollectivesExtension` @ `0xE6F19A0` | [Remaining](ext-remaining.md) |
| 4 | `0x224C3E38` | AbiVersion (20) | 120 | 12 | `CreateTpuAbiVersionExtension` @ `0xE6B7340` → `CreateAbiVersionExtension` @ `0xE6B8960` | [Remaining](ext-remaining.md) |
| 5 | `0x224C3E08` | Shardings (19) | 40 | 2 | `CreateShardingsExtension` @ `0xF874980` | [Remaining](ext-remaining.md) |
| 6 | `0x224C3D08` | Megascale (18) | 248 | 24 (+5 reserved) | `CreateMegascaleExtension` @ `0xE6B97C0` | [Remaining](ext-remaining.md) |
| 7 | `0x224C3CA8` | TpuExecutable (17) | 88 | 7 (+1 reserved) | `CreateTpuExecutableExtension` @ `0xE6DC6E0` | [Remaining](ext-remaining.md) |
| 8 | `0x224C3B90` | TopologyDescription / TpuTopology (16) | 272 | 31 | `CreateTpuTopologyExtension` @ `0xE6DE5E0` | [Topology](ext-topology-description.md) |
| 9 | `0x224C3B60` | Callback (14) | 40 | 2 | `CreateCallbackExtension` @ `0xE6B91E0` | [Remaining](ext-remaining.md) |
| 10 | `0x224C3B18` | PhaseCompile (9) | 64 | 5 | `CreatePhaseCompileExtension` @ `0xE6F42A0` | [PhaseCompile](ext-compile-phasecompile.md) |
| 11 | `0x224C3AD8` | CrossHostTransfers (12) | 56 | 4 | `CreateCrossHostTransfersExtension` @ `0xF85D660` | [Remaining](ext-remaining.md) |
| 12 | `0x224C3AA0` | HostAllocator (15) | 48 | 3 | `CreateHostAllocatorExtension` @ `0xF8A3C20` | [Remaining](ext-remaining.md) |
| 13 | `0x224C3A70` | ExecutableMetadata (13) | 40 | 2 | `CreateExecutableMetadataExtension` @ `0xF8A3BE0` | [Remaining](ext-remaining.md) |
| 14 | `0x224C3A40` | MemoryDescriptions (6) | 40 | 2 | `CreateMemoryDescriptionsExtension` @ `0xF874940` | [Remaining](ext-remaining.md) |
| 15 | `0x224C39E8` | Layouts (4) | 80 | 7 | `CreateLayoutsExtension` @ `0xF8748C0` | [Remaining](ext-remaining.md) |
| 16 | `0x224C3990` | RawBuffer (8) | 80 | 7 | `CreateRawBufferExtension` @ `0xE6F52C0` | [RawBuffer](ext-rawbuffer.md) |
| 17 | `0x22255B98` | Profiler (1) | 40 | 8 | static `.data` init (no `Create*` call) | [Profiler](ext-profiler.md) |

Total = 17 nodes. Profiler (type 1) is the terminator (`next = NULL`). The five TPU-specialized nodes whose creators receive TPU function pointers as parameters — AbiVersion (two `FromProto` factories), ExecutableMetadata (`GetExecutableMetadata`), HostAllocator (all three slots), PhaseCompile (`Get_Compiler`/`Destroy_Compiler`), plus the fully-TPU Megascale/TpuExecutable/MultiSlice/Callback/TpuTopology — are detailed on their deep-dive pages; this page is concerned only with their place in the chain.

---

## How the Chain Is Built

`GetTpuPjrtApi` @ `0xE6AA440` is a straight-line sequence of 16 `__cxa_guard`-protected `Create*Extension` calls followed by a 17th guard around `CreatePjrtApi`. Each `Create*` call takes the node's own address and the *previously*-built node's address as its `next`, so the list grows tail-first.

```c
// pjrt::tpu_plugin::GetTpuPjrtApi @ 0xE6AA440  (one-shot, __cxa_guard per node)
function GetTpuPjrtApi():
    // Built first; its next is the .data Profiler seed (type 1).
    once: CreateRawBufferExtension(&raw_buffer_extn,  &profiler_extension);      // -> type 8
    once: CreateLayoutsExtension(&layouts_extn,       &raw_buffer_extn);         // -> type 4
    once: CreateMemoryDescriptionsExtension(&mem_desc_extn, &layouts_extn);      // -> type 6
    once: CreateExecutableMetadataExtension(&exec_meta_extn, &mem_desc_extn,
                                            GetTpuExecutableMetadata);           // -> type 13 (TPU fn)
    once: CreateHostAllocatorExtension(&host_alloc_extn, &exec_meta_extn,
                                       TPU_HostAllocator_GetPreferredAlignment,
                                       TPU_HostAllocator_Allocate,
                                       TPU_HostAllocator_Free);                  // -> type 15 (TPU fns)
    once: CreateCrossHostTransfersExtension(&xfer_extn,  &host_alloc_extn);      // -> type 12
    once: CreatePhaseCompileExtension(&phase_extn, &xfer_extn,
                                      GetTpuPhaseCompiler, DestroyTpuPhaseCompiler); // -> type 9 (TPU fns)
    once: CreateCallbackExtension(&callback_extn,  &phase_extn);                 // -> type 14
    once: CreateTpuTopologyExtension(&topo_extn,   &callback_extn);             // -> type 16
    once: CreateTpuExecutableExtension(&tpu_exec_extn, &topo_extn);             // -> type 17
    once: CreateMegascaleExtension(&megascale_extn, &tpu_exec_extn);            // -> type 18
    once: CreateShardingsExtension(&shardings_extn, &megascale_extn);           // -> type 19
    once: CreateTpuAbiVersionExtension(&abi_extn,   &shardings_extn);           // -> type 20 (thunk)
    once: CreateCollectivesExtension(&collectives_extn, &abi_extn);            // -> type 21
    once: CreateMultiSliceExtension(&multi_slice_extn,  &collectives_extn);     // -> type 22
    // Built last -> becomes the chain head (extension_start).
    once: CreateHostMemoryAllocatorExtension(&host_mem_alloc_extn, &multi_slice_extn); // -> type 23

    once: CreatePjrtApi(&pjrt_api,
                        PJRT_Client_Create, PJRT_ExecuteContext_Create,
                        PJRT_TopologyDescription_Create, PJRT_Plugin_Initialize,
                        &host_mem_alloc_extn,            // -> PJRT_Api.extension_start
                        PJRT_Plugin_Attributes_Xla);
    return &pjrt_api;                                    // 0x227BA840
```

> **QUIRK —** Profiler (type 1) is built differently from the other 16. It is **not** created by a `Create*Extension` call inside `GetTpuPjrtApi`; it is a `.data`-resident node `pjrt::tpu_plugin::profiler_extension` @ `0x22255B98`, populated by static initializers / `R_X86_64_RELATIVE` relocations at load time. `GetTpuPjrtApi` references it only as the `next` argument handed to `CreateRawBufferExtension` (the first-built node). So Profiler is the seed the whole `.bss` chain is appended in front of, and the chain *terminates* at it because its own `next` is `NULL`.

### Construction Order vs Walk Order

```text
construction (build) order            walk order from extension_start
(GetTpuPjrtApi top -> bottom)         (consumer follows .next)
  RawBuffer(8)   --- built 1st          HostMemoryAllocator(23) <- head (built last)
  Layouts(4)                            MultiSlice(22)
  MemoryDescriptions(6)                 Collectives(21)
  ExecutableMetadata(13)                AbiVersion(20)
  HostAllocator(15)                     Shardings(19)
  CrossHostTransfers(12)                Megascale(18)
  PhaseCompile(9)                       TpuExecutable(17)
  Callback(14)                          TpuTopology(16)
  TpuTopology(16)                       Callback(14)
  TpuExecutable(17)                     PhaseCompile(9)
  Megascale(18)                         CrossHostTransfers(12)
  Shardings(19)                         HostAllocator(15)
  AbiVersion(20)                        ExecutableMetadata(13)
  Collectives(21)                       MemoryDescriptions(6)
  MultiSlice(22)                        Layouts(4)
  HostMemoryAllocator(23) - built last  RawBuffer(8)
        (profiler is the pre-existing seed)  Profiler(1) <- terminator (.next = NULL)
```

The walk order is the exact reverse of construction order because each node prepends itself: `node.next = previously_built`, and `extension_start = last_built`. The ordering is purely an artifact of build sequence — **consumers must not depend on it**.

---

## How a Framework Walks the Chain

Discovery is a type-id linear scan, identical to the canonical `xla/pjrt/c` `pjrt_c_api_helpers` `FindExtension`. A framework that wants, say, the Profiler extension does this:

```c
// Consumer-side feature detection. `wanted` is a PJRT_Extension_Type id.
function FindExtension(const PJRT_Api* api, PJRT_Extension_Type wanted):
    PJRT_Extension_Base* ext = api->extension_start;     // +0x08
    while (ext != NULL):
        if (ext->type == wanted):                        // +0x08, uint32 compare
            return ext;                                  // caller casts to the typed struct
        ext = ext->next;                                 // +0x10
    return NULL;                                          // feature not advertised
```

The contract this loop establishes:

- **Absence is a valid answer.** A `NULL` return means the plugin does not advertise that capability. The framework must degrade gracefully — e.g. on TPU, a missing FFI extension (type 5) is expected, and the framework falls back to the Callback/TpuExecutable channels described above.
- **`struct_size` gates per-method forward-compat, not chain membership.** Finding the node is enough to know the *feature* exists; whether a *specific method* exists is determined by the node's `struct_size` (does the tail reach that slot?) and, at call time, the method's own `ActualStructSizeIsGreaterOrEqual` check against `args->struct_size`. A consumer built against a newer header must verify `ext->struct_size` covers the slot it wants before dereferencing it.
- **Order-independence.** The walk must not assume any node sits at a particular position. libtpu's newest-first ordering is incidental; a different plugin (or a future libtpu) may order differently or add/remove nodes.
- **Termination.** The loop terminates on `NULL`. libtpu's terminator is Profiler's `next`; a reimplementation that forgets to NULL-terminate the last node will walk into adjacent `.bss`/`.data` and read garbage `type` values.

> **NOTE —** the `PJRT_Api` struct itself (the 140-slot vtable that `extension_start` hangs off of) is a separate object at `0x227BA840` in `.lbss`, populated by `pjrt::CreatePjrtApi` @ `0xF874160`. The extension chain and the vtable are populated in the same one-shot `GetTpuPjrtApi` init but are distinct structures; the vtable's slot-by-slot reconstruction is on [API Vtable Reconstruction](api-vtable-reconstruction.md).

---

## Cross-References

- [PJRT Plugin Overview](overview.md) — how `dlsym("GetPjrtApi")` reaches `GetTpuPjrtApi` and the one-shot init path
- [API Vtable Reconstruction](api-vtable-reconstruction.md) — the 140 main-table function-pointer slots that `extension_start` hangs off (a separate structure)
- [Profiler Extension](ext-profiler.md) — type 1, the `.data` seed and chain terminator
- [Topology Description Extension](ext-topology-description.md) — type 16, the largest live extension (31 methods)
- [RawBuffer Extension](ext-rawbuffer.md) — type 8, whose `next` ties the `.data` Profiler to the `.bss` chain tail
- [PhaseCompile Extension](ext-compile-phasecompile.md) — type 9, named-phase partial compilation (distinct from main-table slot 94 `PJRT_Compile`)
- [Remaining Extensions](ext-remaining.md) — the other 12 nodes (Layouts, MemoryDescriptions, CrossHostTransfers, ExecutableMetadata, HostAllocator, TpuExecutable, Megascale, Shardings, AbiVersion, Collectives, MultiSlice, HostMemoryAllocator, Callback)
