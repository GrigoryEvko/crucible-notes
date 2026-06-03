# The TfTpu C-API Shim

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols and IDA-recovered C names quoted verbatim). `.text` VMA equals file offset. Other versions will differ.*

## Abstract

The **TfTpu C-API shim** is the stable C ABI through which the open-source StreamExecutor TPU backend — the C++ code that ships *inside* TensorFlow/XLA, not inside this binary — drives `libtpu.so` without ever linking against a libtpu C++ symbol. It exists for exactly one reason: a closed-source plugin and an open-source host cannot share `std::string`, `absl::Status`, `xla::Shape`, or any other C++ type across the `.so` boundary, because the two are built by different toolchains with no ABI contract. Layer a flat C interface between them and the coupling vanishes. The shim is that interface: a few hundred `extern "C"` free functions, grouped by the C++ class they back, that take and return only opaque struct handles and POD.

The surface has three structural pieces, and this page owns the orientation for all three. First, the **per-class roster of C functions** — `TpuCompiler_*`, `TpuExecutable_*`, `TpuExecutor_*`, `TpuTransferManager_*`, `TpuProgram_*`, `TpuPlatform_*`, `TpuTopology_*`, `TpuEmbeddingEngine_*`, `TpuConfigurationApi_*` (plus smaller `TpuCoreLocation_*`, `TpuNodeContext_*`, `TpuMeshState_*`, `TpuCompile_*` clusters). Each cluster is the C face of one stream-executor abstraction, and each has a dedicated sibling page that inventories its functions. Second, the **`*ApiFn()` accessor pattern**: the host does not call `TpuExecutor_Allocate` by its dynamic-symbol name. It calls through a singleton *struct of function pointers* — `stream_executor::tpu::ExecutorApiFn()` returns a pointer to a function-local-static `TfTpu_ExecutorApiFn` whose members are populated once at initialisation, and every SE shim method indexes a slot of that struct. Third, the **opaque-handle convention**: every C++ object that crosses the boundary is mirrored by an `SE_*` / `XLA_*` / `Tpu*` opaque C struct, and `ApiConverter::ToC` / `FromC` / `Destroy` marshal between the rich C++ type and its flattened C twin.

This is a **map page**. It establishes the ABI model and the accessor pattern, then summarises each roster in one or two sentences and links its sibling page for the per-function detail. It does not duplicate the per-function tables, the table-population bootstrap (owned by [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md)), or the SE platform/executor object model that *consumes* the `ExecutorApiFn` table (owned by [StreamExecutor Platform & Executor Model](../pjrt/stream-executor-host-interpreter.md)). It is the orientation a reimplementer reads first, before descending into any one roster.

For reimplementation, the contract is:

- **The ABI seam itself** — why a flat C interface exists, what crosses it (only opaque handles + POD), and what never does (no `std::`/`absl::`/`xla::` types by value).
- **The `*ApiFn()` accessor pattern** — three accessors (`ExecutorApiFn`, `OpsApiFn`, `ProfilerApiFn`), each returning a singleton function-pointer struct populated by the init bootstrap; how a shim method dereferences a slot; how `IsStreamExecutorEnabled` probes the table before use.
- **The opaque-handle convention** — the `SE_*` / `XLA_*` / `Tpu*` struct families and the `ApiConverter::ToC` / `FromC` / `Destroy` marshalling, with the concrete C++↔C type pairs.
- **The roster map** — which C-function cluster backs which SE abstraction, and where each is documented.

| | |
|---|---|
| **ABI seam** | `extern "C"` free functions; only opaque handles + POD cross the `.so` boundary |
| **Accessor (executor/stream)** | `stream_executor::tpu::ExecutorApiFn() @ 0x20819360` → `&ExecutorApiFn()::executor_api_fn` (function-local static) |
| **Accessor (embedding/ops)** | `stream_executor::tpu::OpsApiFn() @ 0x10900e80` → `&OpsApiFn()::ops_api_fn` |
| **Accessor (profiler)** | `stream_executor::tpu::ProfilerApiFn() @ 0x10900ea0` → `&ProfilerApiFn()::profiler_api_fn` |
| **Enable probe** | `stream_executor::tpu::IsStreamExecutorEnabled(TfTpu_ExecutorApiFn*) @ 0x20819380` |
| **Init probe** | `stream_executor::tpu::IsInitialized(TfTpu_ExecutorApiFn*) @ 0x208193c0` |
| **Opaque-handle marshalling** | `ApiConverter::ToC` (18 overloads) / `FromC` (6) / `Destroy` (3) |
| **C-ABI roster (this binary side)** | 9 named clusters, ~150 free functions (`TpuCompiler_*` 7, `TpuExecutor_*` 25, `TpuTransferManager_*` 19, `TpuProgram_*` 18, `TpuTopology_*` 17, `TpuEmbeddingEngine_*` 15, `TpuPlatform_*` 11, `TpuExecutable_*` 9, `TpuConfigurationApi_*` 8) |
| **Coexisting modern ABI** | PJRT C-API (`PJRT_*`) — see scope note |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

> **Scope —** the *one-time population* of the `*ApiFn` structs (which symbol resolves each slot, and when) is owned by [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md). The *consumer* of `ExecutorApiFn` — the `TpuPlatform` / `TpuExecutor` SE object model that forwards every method through the table — is owned by [StreamExecutor Platform & Executor Model](../pjrt/stream-executor-host-interpreter.md). The modern **PJRT C-API** that coexists with this legacy SE shim is owned by the [PJRT Overview](../pjrt/overview.md). This page is the *shim-ABI orientation + accessor pattern + handle convention + roster map* only; it links those rather than restating them.

---

## 1. Why a C Shim Exists

### The ABI seam

`libtpu.so` is a closed Google build; the StreamExecutor TPU backend (`stream_executor::tpu::TpuExecutor`, `tensorflow::tpu::TpuPlatform`, `xla::TpuCompiler`, and friends) is open source and built by whatever toolchain compiled the surrounding TensorFlow/XLA. If those two halves passed C++ objects by value — an `xla::Shape`, an `absl::Status`, a `std::vector<...>` — they would have to agree bit-for-bit on the layout of every standard-library and XLA type. They do not and cannot: the plugin is frozen at build time and the host moves independently. A flat C interface dissolves the problem. C has a stable calling convention and no name mangling, so a function declared `extern "C"` is callable across the boundary regardless of which compiler emitted each side.

Concretely, **nothing with a C++ ABI crosses the seam.** What crosses is:

- **Opaque handles** — pointers to structs the caller never inspects (`SE_StreamExecutor*`, `XLA_Shape*`, `SE_Event*`, `TpuExecutor*`). The host holds them, passes them back, and frees them through a paired `_Free` call.
- **POD** — integers, sizes, raw `const char*` + length pairs, and small fixed C structs.
- **Status-out scratch objects** — a C `TF_Status`-style object the callee fills and the caller queries for ok/code/message, then frees (the *scratch / op / ok?-code-msg / free* idiom documented on the SE page).

The two sides are wired together not by the dynamic linker resolving `TpuExecutor_Allocate` at load time, but by a **table of function pointers** the plugin hands to the host once, early. That table is the `*ApiFn` struct.

### Where the two halves live

The IDA decompile makes the split visible. The **callee** half — the implementation that actually touches the TPU driver — lives in `libtpu.so` as `extern "C"` free functions whose IDA-recovered names are the roster prefixes: `TpuExecutor_Allocate @ 0xeab9120`, `TpuCompiler_New @ 0xeabc4a0`, `TpuProgram_New @ 0xe8bda60`, `TpuTransferManager_CanBufferBeAccessedNow @ 0xeaba7a0`, and ~150 more. The **caller** half — the thin C++ shim that forwards each SE virtual method into a slot of the `*ApiFn` table — is *also* present in this binary as the mangled `stream_executor::tpu::TpuExecutor::*` / `tensorflow::tpu::TpuTransferManager::*` / `xla::TpuCompiler::*` methods, because XLA is statically linked into `libtpu.so`. A reimplementer reproducing the *host* side reproduces the second half; reproducing a *plugin* reproduces the first.

> **NOTE —** the C-ABI free functions do not appear in the demangled symbol roster as `TfTpu_Compiler_*`; IDA names them `TpuCompiler_*`, `TpuExecutor_*`, etc., recovered from `.rodata` reference strings and call targets. The `TfTpu_` prefix survives only on the table type names (`TfTpu_ExecutorApiFn`) and a few public init entry points (`TfTpu_Initialize`). Treat `Tpu<Class>_<Method>` as the canonical C-ABI roster name in this build.

---

## 2. The `*ApiFn()` Accessor Pattern

### The singleton function-pointer struct

The host never names a C-ABI function directly. Each roster is reached through a **singleton struct of function pointers** obtained from a zero-argument accessor. There are three such accessors, each returning the address of a function-local-static struct instance:

```c
// stream_executor::tpu::ExecutorApiFn()                        sub_20819360
void* ExecutorApiFn():
    return &ExecutorApiFn()::executor_api_fn   // function-local static TfTpu_ExecutorApiFn

// stream_executor::tpu::OpsApiFn()                             sub_10900E80
void* OpsApiFn():
    return &OpsApiFn()::ops_api_fn

// stream_executor::tpu::ProfilerApiFn()                        sub_10900EA0
void* ProfilerApiFn():
    return &ProfilerApiFn()::profiler_api_fn
```

Each is a Meyers-singleton returning a stable pointer to a static struct. The struct's members are typed function pointers — one per C-ABI roster entry — laid out at fixed byte offsets. The SE shim methods address them by offset, not by name; the SE page documents the executor-side slot pattern (`ExecutorApiFn()[+16]`, `[+32]`, `[+360]`, …) where each offset is one member of `TfTpu_ExecutorApiFn`. A reimplementer must keep slot offsets identical between the side that *populates* the table and the side that *reads* it, because the offset is the entire contract — there is no name to fall back on at call time.

The three accessors partition the surface by subsystem rather than one-table-per-class:

| Accessor | Address | Backs | Roster clusters it carries |
|---|---|---|---|
| `ExecutorApiFn` | `0x20819360` | Device runtime: platform, executor, stream, transfer, program, compiler | `TpuExecutor_*`, `TpuTransferManager_*`, `TpuPlatform_*`, `TpuProgram_*`, `TpuCompiler_*`, `TpuExecutable_*`, `TpuTopology_*`, `TpuCoreLocation_*`, `TpuNodeContext_*`, `TpuConfigurationApi_*`, `TpuMeshState_*`, `TpuCompile_*` |
| `OpsApiFn` | `0x10900e80` | Embedding / SparseCore ops | `TpuEmbeddingEngine_*` and the XLA-op-kernel embedding surface |
| `ProfilerApiFn` | `0x10900ea0` | Profiler / trace collection | the TPU profiler C surface |

> **QUIRK —** the per-class roster split is *documentation* structure, not *table* structure. The binary groups all device-runtime classes (`Compiler`, `Executor`, `Stream`, `Program`, `TransferManager`, `Platform`, `Topology`, …) into a single `TfTpu_ExecutorApiFn` struct reached by one accessor; embedding and profiler get their own. A reimplementer who builds one struct per C++ class will not match the binary's offset layout. There are three tables, not nine.

### Probing the table before use

Because the struct is a function-local static, its members are zero until the bootstrap populates them. The shim guards against an unpopulated table with two predicates that take the table pointer directly:

```c
// stream_executor::tpu::IsStreamExecutorEnabled(TfTpu_ExecutorApiFn* a1)   sub_20819380
char IsStreamExecutorEnabled(a1):
    if (!a1->slot[0]):              return 0    // first fn-ptr unset → table not populated
    enabled = a1->slot[0](a1)                   // call the "is enabled" entry
    if (!enabled):                  return 0
    a1->slot[8](enabled)                        // consume/dispose the returned probe object
    return 1
```

`IsStreamExecutorEnabled` (`0x20819380`) and its sibling `IsInitialized` (`0x208193c0`) both take a `TfTpu_ExecutorApiFn*` and dereference its leading slots. The first slot doubling as a null-check is deliberate: an all-zero table reads as "SE backend disabled," and every gated path (e.g. `RegisterTpuPlatform`, which the SE page shows is gated on `IsStreamExecutorEnabled(ExecutorApiFn())`) treats that as a clean no-op rather than a crash. A reimplementer must make the *first* member a valid predicate function and must zero-initialise the whole struct so an un-bootstrapped table fails the probe safely.

> **GOTCHA —** the probe calls `slot[0]` and then `slot[8]`, treating the value returned by `slot[0]` as an owned object that `slot[8]` releases. A reimplementation that makes `slot[0]` return a borrowed/static pointer and then runs it through a `Free` slot will double-free or free a static; one that makes `slot[0]` return `nullptr` on "enabled" will report the backend disabled. The predicate must return a fresh disposable object on success.

The accessor returns a *pointer* to the singleton, so callers can both read slots and (during bootstrap) write them through the same address. The bootstrap that fills the slots is [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md); this page only establishes that the accessor and the probe exist.

---

## 3. The Opaque-Handle Convention

### `SE_*` / `XLA_*` / `Tpu*` mirrors

Every C++ object that must cross the seam has a flattened C twin. The naming is consistent: a `stream_executor::Foo` becomes `SE_Foo`, an `xla::Foo` becomes `XLA_Foo`, and the TPU-specific handles keep their `Tpu*` names. The C side treats these as opaque — it holds the pointer, passes it to the next call, and never reads a field. The marshalling between rich and flat forms is `ApiConverter`, with three verbs:

- **`ToC`** — serialise a C++ object into its C struct (out-param or by-value handle). 18 overloads observed.
- **`FromC`** — reconstruct the C++ object from its C struct. 6 overloads observed.
- **`Destroy`** — free the heap-owned interior of a C struct (`XLA_Shape`, `XLA_Literal`, `XLA_ShapedBuffer`). 3 overloads observed.

The concrete C++↔C pairs recovered from the symbol table:

| C++ type | C struct (handle) | Marshalling verbs | Confidence |
|---|---|---|---|
| `xla::Shape` | `XLA_Shape` | `ToC(const Shape&, XLA_Shape*)`, `FromC(const XLA_Shape*)`, `Destroy(XLA_Shape*)` | CERTAIN |
| `xla::Layout` | `XLA_Layout` | `ToC(const Layout&, XLA_Layout*)`, `FromC(const XLA_Layout*)` | CERTAIN |
| `xla::LiteralSlice` / `Literal` | `XLA_Literal` | `ToC(const LiteralSlice&, XLA_Literal*)`, `FromC(XLA_Literal*)`, `Destroy(XLA_Literal*)` | CERTAIN |
| `xla::ShapedBuffer` | `XLA_ShapedBuffer` | `ToC(const ShapedBuffer&, XLA_ShapedBuffer*)`, `FromC(XLA_ShapedBuffer*)`, `Destroy(XLA_ShapedBuffer*)` | CERTAIN |
| `xla::HloModule` | (C module handle) | `ToC(const HloModule&)` | HIGH |
| `xla::HloModuleConfig` | `XLA_HloModuleConfig` | `ToC(const HloModuleConfig&)`, `FromC(const XLA_HloModuleConfig&)` | CERTAIN |
| `xla::ShapeIndex` | (C index) | `ToC(const ShapeIndex&)` | HIGH |
| `stream_executor::DeviceAddressBase` | `SE_DeviceAddressBase` | `ToC(const DeviceAddressBase&)`, `FromC(const SE_DeviceAddressBase&)` | CERTAIN |
| `stream_executor::ScopedDeviceAddress<uint8>` | `SE_DeviceAddressBase` (raw) | `ToC(ScopedDeviceAddress<uchar>*)` | HIGH |
| `stream_executor::DeviceAddressAllocator` | `SE_DeviceAddressAllocator` | `ToC(DeviceAddressAllocator*)` | HIGH |

### The ownership rule

`ToC` allocates; `Destroy` frees. The `Destroy` overloads exist only for the three types with heap-owned interiors — `XLA_Shape` (nested dimensions/tuple subshapes), `XLA_Literal` (the data buffer), `XLA_ShapedBuffer` (the device-address array). Fixed-size handles (`XLA_Layout`, `SE_DeviceAddressBase`) have no `Destroy` because there is nothing to free beyond the struct the caller owns. A reimplementer must pair every `ToC` of a variable-size type with the matching `Destroy`, and must *not* call `Destroy` on the POD-only handles.

> **QUIRK —** `ToC` for the variable-size types is the out-param form `ToC(const xla::Shape&, XLA_Shape* out)` — it fills a caller-provided struct rather than returning a new one. The pointer-based `Destroy(XLA_Shape*)` then frees the *interior* (the dimension array, the tuple subshapes), not the struct itself if it lives on the caller's stack. Confusing "free the struct" with "free its interior" leaks or double-frees. Follow the overload signature: out-param fill paired with interior free.

This convention is what lets the roster functions take and return rich data without a shared C++ ABI: `TpuTransferManager_TransferLiteralToInfeed` receives an `XLA_Literal*` that the host produced by `ApiConverter::ToC`, and the plugin reconstructs an `xla::Literal` internally with its *own* statically-linked XLA — the two `xla::Literal` layouts never have to match because only the `XLA_Literal` flat form crossed the boundary.

---

## 4. The Roster Map

The C-ABI surface is partitioned into clusters by the SE abstraction each backs. Counts are the number of `extern "C"` free functions IDA recovered for each prefix in this build. Every cluster has a dedicated sibling page that inventories its functions, signatures, and slot offsets; this map gives only the one-line role and the link.

| Roster | C-ABI count | Backs | Detail page | Confidence |
|---|---|---|---|---|
| `TpuCompiler_*` | 7 | XLA→TPU compilation: `New`, `Compile`, `RunHloPasses`, `RunBackend`, `DefaultDeviceShapeRepresentation`, `Free` | [TpuCompiler Roster](tpu-compiler-roster.md) | CERTAIN |
| `TpuExecutable_*` | 9 | Compiled-executable handle: deserialize, fingerprint, query HLO modules / layouts / cost | [TpuExecutable Roster](tpu-executable-roster.md) | CERTAIN |
| `TpuExecutor_*` | 25 | Per-device runtime: `Allocate`, stream/event creation, dependency wiring, memory ops | [TpuExecutor Roster](tpu-executor-roster.md) | CERTAIN |
| `TpuTransferManager_*` | 19 | Host↔device transfer: literal-to/from-device, infeed/outfeed, buffer-access predicates | [TpuTransferManager Roster](tpu-transfer-manager.md) | CERTAIN |
| `TpuProgram_*` | 18 | Serialized program object: `New`, deserialize-from-proto, fingerprint, executable-info, free | [TpuProgram Roster](tpu-program-roster.md) | CERTAIN |
| `TpuPlatform_*` | 11 | Platform lifecycle / device enumeration backing `TpuPlatform` | [TpuPlatform & TpuNodeContext](tpu-platform-and-topology.md) | CERTAIN |
| `TpuTopology_*` | 17 | Mesh geometry: chip bounds (X/Y/Z), core counts, host counts, core lookup | [TpuTopology & TpuCoreLocation](tpu-topology.md) | CERTAIN |
| `TpuEmbeddingEngine_*` | 15 | SparseCore embedding: configure host/memory, connect hosts, dedup-data computation | [TpuEmbeddingEngine ABI](tpu-embedding-engine.md) | CERTAIN |
| `TpuConfigurationApi_*` | 8 | Runtime config: compilation-cache server address, pod-state queries, array frees | [TpuConfigurationApi](tpu-configuration-api.md) | CERTAIN |

Four smaller clusters sit alongside the nine named rosters and are documented within the pages above rather than getting their own:

| Roster | C-ABI count | Backs | Documented on | Confidence |
|---|---|---|---|---|
| `TpuCoreLocation_*` | 4 | Single-core coordinates / id (`TpuCoreLocationExternal`) | [TpuTopology & TpuCoreLocation](tpu-topology.md) | HIGH |
| `TpuNodeContext_*` | 5 | Per-node driver context bring-up | [TpuPlatform & TpuNodeContext](tpu-platform-and-topology.md) | HIGH |
| `TpuMeshState_*` | 3 | Distributed mesh state handle | [TpuPlatform & TpuNodeContext](tpu-platform-and-topology.md) | MEDIUM |
| `TpuCompile_*` | 6 | Standalone compile entry (distinct from `TpuCompiler_*` class methods) | [TpuCompiler Roster](tpu-compiler-roster.md) | MEDIUM |

### How a roster call flows

The path from an open-source SE call to the plugin's implementation is uniform across every cluster. Taking `TpuExecutor::Allocate` as the worked example:

```text
xla / PjRtClient
  └─ stream_executor::tpu::TpuExecutor::Allocate(...)        (host-side C++ shim, in this binary)
       └─ tbl = ExecutorApiFn()                               0x20819360  — get the singleton struct
       └─ tbl->slot[Allocate](se_handle, size, ...)           — call through the fn-ptr slot
              │  (the slot was populated at init to point at:)
              ▼
       TpuExecutor_Allocate                                   0xeab9120   — plugin-side C-ABI impl
              └─ (*se_handle->vtable[+136])(...)               — into libtpu's real TPU driver core
```

The shim method never names `TpuExecutor_Allocate`; it indexes a slot. The slot was filled at init to hold `&TpuExecutor_Allocate`. The C-ABI function takes the opaque `SE_StreamExecutor*` (here read at `**a2`) and dispatches into the real driver vtable. This three-hop shape — *accessor → slot → C-ABI impl → driver* — is the single pattern a reimplementer must reproduce; the rosters differ only in which slots and which handles.

> **NOTE —** the host-side `TpuExecutor::*` shim and the plugin-side `TpuExecutor_*` C-ABI functions are *both* compiled into `libtpu.so` because XLA is statically linked here. In a clean split (a host process loading the plugin via `dlopen`), the `TpuExecutor::*` shim lives in the host and the `TpuExecutor_*` impls live in the plugin; only the `*ApiFn` struct pointer crosses. This binary collapses both halves into one image, which is why the same address space holds both names.

---

## 5. Coexistence with the PJRT C-API

Two C ABIs live in this binary at once. The **TfTpu SE shim** documented here is the *legacy* surface: it predates PJRT and exposes the full StreamExecutor object model (platform / executor / stream / transfer-manager / compiler) as flat C. The **PJRT C-API** (`PJRT_*` functions reached through a versioned vtable) is the *modern* public plugin ABI, and it is what an external framework actually loads `libtpu.so` for. They are not alternatives layered side by side at the same level — PJRT sits *on top of* StreamExecutor. A `PJRT_Client` for TPU is implemented by `TpuClient`, which drives `TpuExecutor`s minted by `TpuPlatform`, which forwards through `ExecutorApiFn()`. So a single `PJRT_Client_Compile` call descends through the PJRT vtable, into the SE adapter, and ultimately bottoms out in `TpuCompiler_*` / `TpuExecutor_*` C-ABI calls through the very `*ApiFn` tables this page maps.

The division of ownership: the PJRT vtable, its extension chain, and the `GetPjRtApi` entry point are owned by the [PJRT Overview](../pjrt/overview.md) and its sub-pages. The SE platform/executor object model that bridges PJRT down to this shim is owned by [StreamExecutor Platform & Executor Model](../pjrt/stream-executor-host-interpreter.md). This page owns only the bottom layer — the C-ABI seam and its accessor/handle conventions.

> **QUIRK —** a reimplementer targeting a *new framework integration* writes against PJRT, never against the TfTpu shim — the shim is an internal detail of how the TPU PJRT plugin is built. The TfTpu shim matters to anyone reimplementing the *plugin's interior* or the *legacy stream-executor TPU backend* in TensorFlow, not to a downstream consumer. The two ABIs coexist because XLA still drives TPUs through StreamExecutor internally even when the outermost call arrived via PJRT.

---

## Related Components

| Name | Relationship |
|---|---|
| `stream_executor::tpu::ExecutorApiFn / OpsApiFn / ProfilerApiFn` | the three singleton accessors that front the C-ABI rosters |
| `TfTpu_ExecutorApiFn` (struct type) | the function-pointer table the device-runtime rosters are reached through |
| `ApiConverter::ToC / FromC / Destroy` | the marshalling between rich C++ types and `SE_*` / `XLA_*` opaque handles |
| `stream_executor::tpu::TpuExecutor` / `tensorflow::tpu::TpuPlatform` / `xla::TpuCompiler` | the host-side SE shims that forward each method through an `*ApiFn` slot |
| `TpuExecutor_* / TpuCompiler_* / TpuProgram_* / …` | the plugin-side C-ABI free functions the slots point at |
| PJRT C-API (`PJRT_*`) | the modern public plugin ABI layered on top of this legacy SE shim |

## Cross-References

- [TpuCompiler Roster](tpu-compiler-roster.md) — the `TpuCompiler_*` / `TpuCompile_*` compilation C surface
- [TpuExecutable Roster](tpu-executable-roster.md) — the `TpuExecutable_*` compiled-executable handle ops
- [TpuExecutor Roster](tpu-executor-roster.md) — the `TpuExecutor_*` per-device runtime + stream/event C functions
- [TpuTransferManager Roster](tpu-transfer-manager.md) — the `TpuTransferManager_*` host↔device transfer C ABI
- [TpuProgram Roster](tpu-program-roster.md) — the `TpuProgram_*` program object + serialization C ABI
- [TpuPlatform & TpuNodeContext](tpu-platform-and-topology.md) — the `TpuPlatform_*` / `TpuNodeContext_*` / `TpuMeshState_*` platform + node-context C ABI
- [TpuTopology & TpuCoreLocation](tpu-topology.md) — the `TpuTopology_*` / `TpuCoreLocation_*` mesh-geometry C ABI
- [TpuEmbeddingEngine ABI](tpu-embedding-engine.md) — the `TpuEmbeddingEngine_*` SparseCore embedding C surface (reached via `OpsApiFn`)
- [TpuConfigurationApi](tpu-configuration-api.md) — the `TpuConfigurationApi_*` runtime-configuration C entry points
- [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) — the one-time population of the `*ApiFn` function-pointer structs
- [StreamExecutor Platform & Executor Model](../pjrt/stream-executor-host-interpreter.md) — the SE object model that consumes `ExecutorApiFn` (the `*ApiFn` slot pattern in action)
- [PJRT Overview](../pjrt/overview.md) — the modern PJRT C-API that coexists with and sits atop this legacy SE shim
