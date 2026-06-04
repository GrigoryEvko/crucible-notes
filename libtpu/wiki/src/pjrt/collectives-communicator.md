# PJRT Collectives & Distributed-Coordination Surface

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`). Other versions will differ.*

## Abstract

A multi-host TPU job has two cooperation problems, and PJRT solves them in two different places. The first is the **steady-state collective** — `all-reduce`, `all-gather`, `reduce-scatter` over a tensor that is already sharded across chips — which is lowered to ICI ring traffic by the compiler and never crosses the PJRT C-ABI as data. The second is **bootstrap rendezvous**: before any chip can talk to any other chip, every worker process on every host must agree on the cluster topology, exchange network endpoints, and hand off a barrier. That second problem is what a client-supplied key/value store solves, and it is the subject of this page.

This page owns the *PJRT-level distributed-coordination C-ABI surface*: the key/value get/put primitives a host framework (JAX, TF) uses to rendezvous across processes, the coordination agent the plugin bootstraps from, and how client creation threads that agent down into the megascale/ICI bring-up. It is **not** the on-device collective algorithms — those are in [On-Pod Collectives](../collectives/overview.md) — nor the in-process `Collectives` PJRT extension (type 21), which is a CPU-executor-backed surface unrelated to TPU multi-host coordination; that lives in the [PJRT Extension Chain](extension-chain.md). It is also distinct from the chip-fabric rendezvous run by `MegaScaleTransport`, documented in [Megascale Bootstrap](../megascale/bootstrap/overview.md).

The central finding, stated up front so a reimplementer is not misled by the upstream PJRT C header: **this build advertises no `PJRT_KeyValueStore` struct and no `PJRT_KeyValueGetCallback` / `PJRT_KeyValuePutCallback` function-pointer typedefs.** The KV-store surface that the canonical OpenXLA `PJRT_Client_Create` accepts is *not* a discrete extension here. Cross-process coordination is instead reached through the **`tsl::CoordinationServiceAgent`** abstract interface (the XLA distributed-runtime agent), reached over a flat C ABI via the `TF_CoordinationService*KeyValue*` calls — present in libtpu only as `WEAK UND` import stubs bound at runtime to the host TF runtime — and through two in-binary plugin-agent classes. The KV semantics are identical to the upstream `xla::KeyValueStoreInterface`; the binding shape is different.

> **GOTCHA —** a reimplementer who drives off the upstream `pjrt_c_api.h` will look for a `kv_get_callback` / `kv_put_callback` pair inside the `PJRT_Client_Create_Args`. They are absent from libtpu's main table and from all 17 extensions. The coordination wiring is supplied to the plugin out-of-band (via the coordination agent installed on the process), not as create-time callbacks. Do not synthesize a KV-store extension to match the header — document the agent surface that is actually present.

For reimplementation, the contract is:

- **The KV primitive set and its byte-level signatures** — get (blocking, timed, try), insert (with overwrite flag), delete, dir-list, and the async callback shape. This is the rendezvous vocabulary every multi-host backend needs.
- **The three binding layers** that all bottom out in one `CoordinationServiceAgent`: the C-ABI `TF_CoordinationService*` thunks, the `CPluginCoordinationServiceAgent` / `DirectPluginCoordinationServiceAgent` plugin agents, and the `tsl::CoordinationServiceAgent` base.
- **How client creation reaches it** — `GetPjrtApi` → `GetTpuPjrtApi` builds the `PJRT_Api`; the actual `PjRtClient` is produced by `PjrtClientFactoryRegistry::GetPjrtClient(DeviceType, PjrtClientFactoryOptions)`, and the coordination agent is the channel by which the partitioned topology is exchanged before megascale bring-up runs.
- **The separation of concerns** between this Python-level rendezvous channel (`xla.coordination`) and the C++-level chip-fabric rendezvous (`xla.megascale.runtime`) — two protobuf namespaces that never share barrier IDs.

| | |
|---|---|
| **PJRT entry** | `GetPjrtApi` @`0xe6a83a0` → `pjrt::tpu_plugin::GetTpuPjrtApi` @`0xe6aa440` |
| **Client factory** | `xla::PjrtClientFactoryRegistry::GetPjrtClient(DeviceType, PjrtClientFactoryOptions)` @`0x10849c60` |
| **KV interface** | `tsl::CoordinationServiceAgent` (abstract); plugin agents `CPlugin…` / `DirectPlugin…CoordinationServiceAgent` |
| **C-ABI shim** | `TF_CoordinationService{Insert,Get,GetWithTimeout,TryGet,Delete}KeyValue`, `TF_GetCoordinationServiceAgent` (thunks @`0x213f0a10`–`0x213f0bc0`) |
| **KV protos** | `tensorflow::{Get,Insert,Delete,TryGet}KeyValueRequest/Response`, `KeyValueEntry`, `GetKeyValueDirRequest` (`xla.coordination`) |
| **`PJRT_KeyValueStore` struct** | **ABSENT** in this build (HIGH) |
| **`PJRT_KeyValueGet/PutCallback`** | **ABSENT** in this build (HIGH) |
| **Lower layers (link, not duplicate)** | [On-Pod Collectives](../collectives/overview.md), [Megascale Bootstrap](../megascale/bootstrap/overview.md), [ICI fabric](../ici/overview.md) |

---

## 1. Where the KV-store surface lives

Upstream PJRT lets a client hand the plugin a key/value store at `PJRT_Client_Create` time: two callbacks (`kv_get`, `kv_put`) plus a user context, which the plugin wraps into an `xla::KeyValueStoreInterface` and uses to bootstrap its distributed runtime. The canonical struct is `PJRT_KeyValueGetCallback` / `PJRT_KeyValuePutCallback`.

That struct is **not** in this binary. A scan of the decompiled symbol table for `KeyValueStoreInterface`, `PjRtKeyValue`, `KeyValueGetCallback`, `KeyValuePutCallback`, and `CApiKeyValueStore` returns nothing. None of the 17 PJRT extensions ([extension chain](extension-chain.md)) is a KV-store extension, and the `PJRT_Client_Create` options ingest ([client-and-device](client-and-device.md#pjrt_client_create--options-ingest-and-backend-selection)) takes no get/put callback pair. What *is* present is the full **`tsl::CoordinationServiceAgent`** family — the same agent type the upstream `PjRtClient` would have driven *through* a `KeyValueStoreInterface`, here exposed directly. Functionally this is the KV store; structurally it is reached as a process-resident coordination agent rather than as create-time callbacks.

> **QUIRK —** the upstream design layers `PjRtClient → KeyValueStoreInterface → CoordinationServiceAgent`. libtpu collapses the middle layer: the agent is the surface. A reimplementer should provide the agent (or its C-ABI shim) and skip the `KeyValueStoreInterface` wrapper, because nothing in this binary consumes one.

### Why the agent, not callbacks

The coordination agent is a richer contract than a two-callback KV store: besides get/put it carries barriers, heartbeats, task-state, and an async path. The plugin needs all of that for fault-tolerant multi-host training (see the megascale error-aggregator in [extension chain](extension-chain.md) §Megascale). Threading only `kv_get`/`kv_put` would lose the barrier and heartbeat channels, so the binding exposes the whole agent. The cost is that the rendezvous vocabulary is wider than the upstream KV minimum — but every primitive below is a real exported entry point.

---

## 2. The KV primitive set

All key/value primitives are methods on `tsl::CoordinationServiceAgent`. Keys and values are `std::string_view` / `std::string` (UTF-8 byte strings, not null-terminated); there is no typing — the store is an opaque byte map shared cluster-wide. The set below is exhaustive for this build.

### Method map

| Primitive | Agent symbol (addr) | Signature shape | Confidence |
|---|---|---|---|
| Blocking get | `CoordinationServiceAgent::GetKeyValue` @`0x1dafd700` | `(string_view key) → StatusOr<string>` | HIGH |
| Timed get | `…::GetKeyValue` @`0x1dafd720` | `(string_view key, absl::Duration timeout) → StatusOr<string>` | HIGH |
| Try get | `…::TryGetKeyValue` @`0x1dafe0a0` | `(string_view key) → StatusOr<string>` (non-blocking; NotFound if absent) | HIGH |
| Async get | `…::GetKeyValueAsync` @`0x1dafdc20` | `(string_view key, function<void(const StatusOr<string>&)> done)` | HIGH |
| Insert | `…::InsertKeyValue` @`0x1dafe660` | `(string_view key, string_view value) → Status` | HIGH |
| Insert (overwrite) | `…::InsertKeyValue` @`0x1dafe680` | `(string_view key, string_view value, bool allow_overwrite) → Status` | HIGH |
| Delete | `…::DeleteKeyValue` @`0x1dafe9c0` | `(string_view key) → Status` (prefix delete) | HIGH |
| Dir list | `…::GetKeyValueDir` @`0x1dafe2c0` | `(string_view dir) → StatusOr<vector<KeyValueEntry>>` | HIGH |
| Dir list async | `…::GetKeyValueDirAsync` @`0x1dafe3a0` | `(string_view dir, function<void(const StatusOr<vector<KeyValueEntry>>&)> done)` | HIGH |
| Init check | `…::IsInitialized` @`0x1dafd5a0` | `() → bool` | HIGH |
| Own task | `…::GetOwnTask` @`0x1dafd5e0` | `() → StatusOr<CoordinatedTask>` | MEDIUM |

The two `InsertKeyValue` overloads (`@0x1dafe660` and `@0x1dafe680`, mangled suffixes `…S5_` vs `…S5_b`) differ only by the trailing `bool allow_overwrite`. The plain overload rejects a duplicate key with `AlreadyExists`; the overwrite form replaces silently. A rendezvous protocol that publishes each worker's endpoint exactly once uses the plain form so a duplicate publish surfaces a restart.

### The async callback shape

`GetKeyValueAsync` (and its dir twin) is the load path a non-blocking rendezvous uses. The continuation is a `std::function<void(const absl::StatusOr<std::string>&)>` — confirmed by the mangled type `…NS1_8functionIFvRKN4absl8StatusOrINS1_12basic_stringIcS4_NS1_9allocatorIcEEEEEEEE` at `0x1dafdc20`. The decompiled prologue takes `(this, key_ptr, key_len, function*)` and clones the callable into the agent's pending table (the `GetKeyValueAsync` lambdas' `__policy::__large_clone` thunks at `0x1dafee80` (`$_0`) and `0x1daff180` (`$_1`)), so the callback outlives the call frame and fires when the coordinator returns the value. This is the analog of an async `kv_get`.

```c
// tsl::CoordinationServiceAgent::GetKeyValueAsync   @0x1dafdc20
function GetKeyValueAsync(key /*string_view*/, done /*function<void(const StatusOr<string>&)>*/):
    clone done into the agent's pending-request table   // __large_clone @0x1dafee80
    issue GetKeyValueRequest{ key } over the coordination channel
    // on response: invoke done(StatusOr<string>) from the agent's callback thread
```

> **NOTE —** there is no separate "put callback" pair. Insert is synchronous-by-default; the async surface exists only on the *get* side, because rendezvous get is the operation that blocks waiting on a peer. A reimplementer matching the upstream `kv_put` need only provide a blocking insert.

---

## 3. The three binding layers

Every primitive in §2 is reached through one of three layers, all bottoming out in the same `CoordinationServiceAgent`. The layers exist to serve callers at different ABI boundaries; they are not alternative implementations.

### Layer A — C-ABI thunks (`TF_CoordinationService*`)

A flat C ABI mirrors the agent for callers that cannot use C++ name-mangled symbols (the plugin's own C boundary, and any out-of-tree consumer). Each `TF_CoordinationService*` name is a **`WEAK` `UND` import** in this binary (value `0x0` in `.dynsym`, nm class `w`) — libtpu does *not* define these; it carries only a `.plt` stub per name. The stub at `0x213f0…` does an indirect `jmp *[GOT]` through a `.got.plt` slot that the dynamic loader binds at runtime to the definition supplied by the *host* framework's TF runtime (the same process that installs the coordination agent), or stays unbound if the framework is absent. The thunk and its GOT slot:

| C-ABI entry | `.plt` thunk | `.got.plt` slot | Maps to |
|---|---|---|---|
| `TF_GetCoordinationServiceAgent` | `0x213f0a10` | `0x224c2a80` | obtain the process agent handle |
| `TF_CoordinationServiceInsertKeyValue` | `0x213f0b70` | `0x224c2b30` | `InsertKeyValue` |
| `TF_CoordinationServiceGetKeyValue` | `0x213f0b80` | `0x224c2b38` | `GetKeyValue` (blocking) |
| `TF_CoordinationServiceGetKeyValueWithTimeout` | `0x213f0b90` | `0x224c2b40` | `GetKeyValue(key, timeout)` |
| `TF_CoordinationServiceTryGetKeyValue` | `0x213f0ba0` | `0x224c2b48` | `TryGetKeyValue` |
| `TF_CoordinationServiceDeleteKeyValue` | `0x213f0bb0` | `0x224c2b50` | `DeleteKeyValue` |
| `TF_CoordinationServiceIsInitialized` | `0x213f0bc0` | `0x224c2b58` | `IsInitialized` |

There is **no in-binary implementation** to disassemble — the `.plt` stub is the whole of libtpu's footprint for these calls. A reimplementer must supply the definitions out-of-band (they are part of TF's `c_api_coordination` surface), exactly as the runtime that loads libtpu does. The call *shape* is fixed by Layer B below, which is the in-binary caller.

### Layer B — plugin agents

Two concrete `CoordinationServiceAgent` subclasses adapt the agent to the TF plugin op-kernel boundary:

- **`tensorflow::CPluginCoordinationServiceAgent`** — the C-ABI-backed agent. Its `InsertKeyValue` (`0xe71e260`) is fully decompiled and is the proof that the layers compose: it allocates a `TF_Status` (`TF_NewStatus`), calls `TF_CoordinationServiceInsertKeyValue(key_ptr, key_len, val_ptr, val_len, self+8, status)`, converts the result with `tsl::StatusFromTF_Status`, and frees the status. The `self+8` slot is the underlying C handle. Get/TryGet/Delete (`0xe71e2e0` / `0xe71e520` / `0xe71e5a0`) and the timed get (`0xe71e480`) follow the same C-bounce template.
- **`tensorflow::DirectPluginCoordinationServiceAgent`** — the in-process agent (no C ABI), methods at `0xe71c5c0`–`0xe71c640`. It calls the C++ agent directly, used when the coordination service is co-resident in the same address space.

Both are obtained from an op-kernel context via `GetPluginCoordinationServiceAgent` (`CPluginOpKernelContext` @`0xe71b520`, `DirectPluginOpKernelContext` @`0xe71dbc0`).

```c
// tensorflow::CPluginCoordinationServiceAgent::InsertKeyValue   @0xe71e260
function InsertKeyValue(key /*string_view*/, value /*string_view*/) -> Status:
    status = TF_NewStatus()
    TF_CoordinationServiceInsertKeyValue(                 // .plt 0x213f0b70 → host TF runtime
        key.data, key.size, value.data, value.size,
        self->c_agent /* self+8 */, status)
    result = StatusFromTF_Status(status)                 // tsl::StatusFromTF_Status @0x10900bc0
    if (status) TF_DeleteStatus(status)
    return result
```

### Layer C — the abstract base

`tsl::CoordinationServiceAgent` is the abstract interface (vtable-dispatched; the public symbols at `0x1daf…` are virtual dispatch trampolines, which is why several decompile as self-tail-calls). The real coordinator client logic lives in the `coordination_service_agent.cc` translation unit (static initializers `GLOBAL__sub_I_coordination_service_agent.cc` @`0x2121b570` / `0x21362f10`), built alongside `coordination_service.cc` (`GLOBAL__sub_I` @`0x2137a6d0`). The base owns the request/response protos of §4 and the gRPC channel to the coordinator.

---

## 4. The wire protos

The KV primitives serialize to `xla.coordination` protobuf messages — the same package the [megascale bootstrap](../megascale/bootstrap/overview.md) calls out as the *separate* Python-level rendezvous namespace. Each request/response pair is a standard `proto2::MessageLite` with the usual `Clear` / `MergeImpl` / ctor / dtor quartet present in the symbol table:

| Message | Symbol (representative addr) | Role |
|---|---|---|
| `tensorflow::KeyValueEntry` | ctor `0x20811080`, `Clear` `0x208113a0` | one `{key, value}` pair; the dir-list element type |
| `GetKeyValueRequest` / `Response` | `0x20811a20` / `0x20811dc0` | blocking + timed get |
| `InsertKeyValueRequest` (`Response` paired) | (paired with `KeyValueEntry`) | publish |
| `DeleteKeyValueRequest` | ctor `0x20813640`, `Clear` `0x20813820` | prefix delete |
| `TryGetKeyValueRequest` | (present in symbol table) | non-blocking get |
| `GetKeyValueDirRequest` | `Clear` `0x20812ee0` | dir enumeration |

The `tensorflow::` C++ namespace on these protos (vs the `xla.coordination` protobuf *package*) is the historical TF→XLA migration artifact; they are the coordination-service messages. Response handling on the C side runs through `tensorflow::(anonymous namespace)::ProcessGetKeyValueResult(TF_Buffer*, TSL_Status*)` @`0xe71e360`.

> **NOTE —** these `xla.coordination.*KeyValue*` messages are a distinct protobuf type tree from the `xla.megascale.runtime.*` messages used by the chip-fabric rendezvous. The two share *names* (e.g. both define a `BarrierRequest`) but never share a wire schema or a barrier ID. See [Megascale Bootstrap §Cross-References](../megascale/bootstrap/overview.md) for the split.

---

## 5. How client creation reaches coordination

The PJRT C-ABI is obtained through the single exported entry `GetPjrtApi` @`0xe6a83a0`, a thunk straight to `pjrt::tpu_plugin::GetTpuPjrtApi` @`0xe6aa440`, which `__cxa_guard`-initializes the extension chain and returns the static `PJRT_Api`. That table's `PJRT_Client_Create` slot ingests client options ([client-and-device](client-and-device.md#pjrt_client_create--options-ingest-and-backend-selection)); the concrete `PjRtClient` is then built by the platform factory:

```text
GetPjrtApi  @0xe6a83a0
  └─ GetTpuPjrtApi  @0xe6aa440          ── builds PJRT_Api + 17-node extension chain
        └─ PJRT_Client_Create (main table slot)
              └─ xla::PjrtClientFactoryRegistry::GetPjrtClient(DeviceType, PjrtClientFactoryOptions)
                   @0x10849c60   (registry singleton: PjrtClientFactoryRegistry::Get @0x10849a60)
```

`PjrtClientFactoryOptions` is the struct that carries the per-process distributed identity (process index, process count, and the coordination handle) into the factory. The factory registry pattern — `Get()` returns the singleton, `GetPjrtClient(device_type, options)` dispatches to the registered TPU factory — is the seam where a multi-host build attaches its coordination agent. The TF-side path (`tensorflow::GetPjRtClient` @`0x10848a40`, `PjRtState::GetPjRtClient` @`0x10848ca0`, `GetPjRtClientWrapper` @`0xf79caa0`) wraps the same factory for the TF runtime.

### The bootstrap handoff

Once the client exists with a coordination agent, the agent's KV store is the medium for the **Python-level** rendezvous (process index assignment, run id, shard layout) that must complete *before* the C++-level chip-fabric rendezvous. The dependency chain, top to bottom:

```text
[host framework]  JAX / TF distributed init
      │  publishes per-process state via InsertKeyValue / reads peers via GetKeyValue
      ▼
PJRT distributed CoordinationService   (xla.coordination)   ── THIS PAGE
      │  agent.IsInitialized() gates progress; barrier on the agent releases all workers
      ▼
xla::megascale::runtime CommunicationBackend   (xla.megascale.runtime)  ── Megascale Bootstrap
      │  GetMultiSliceTopology cross-slice rendezvous → multi-slice address table
      ▼
tpunetd  ── intra-slice ICI fabric bring-up
      ▼
steady-state on-device collectives over ICI   ── On-Pod Collectives
```

The arrows are one-way: the coordination KV rendezvous must converge before megascale topology discovery runs, which must converge before ICI collectives can issue. The KV store documented here is the *first* link — the only one a host framework touches directly.

> **QUIRK —** the KV store carries no tensor data and is never on the collective hot path. It moves a few hundred bytes per process at startup (endpoints, ids), then goes idle except for heartbeats. A reimplementer must not confuse its throughput needs with the collective layer's: it is a control-plane rendezvous, not a data plane.

---

## 6. What is present vs absent

| Surface | Status in this build | Evidence |
|---|---|---|
| `tsl::CoordinationServiceAgent` KV methods (get/insert/delete/dir/async) | PRESENT | 19 KV method symbols across the 3 agent classes (9 `tsl::` base + 5 `CPlugin` + 5 `DirectPlugin`) |
| `TF_CoordinationService*KeyValue*` C-ABI thunks | PRESENT (as `WEAK UND` imports) | `.plt` stubs `0x213f0a10`–`0x213f0bc0` → `.got.plt` slots `0x224c2a80`/`0x224c2b30`–`0x224c2b58`; definitions resolved from host TF runtime |
| `CPlugin` / `DirectPlugin` coordination agents | PRESENT | `0xe71e260`-family / `0xe71c5c0`-family |
| `xla.coordination` KV protos | PRESENT | `Get/Insert/Delete/TryGet/Dir` request/response + `KeyValueEntry` |
| `PjrtClientFactoryRegistry` + `PjrtClientFactoryOptions` | PRESENT | `0x10849c60`, `0x10849a60` |
| `PJRT_KeyValueStore` C struct | **ABSENT** (HIGH) | no symbol; not in any extension; not in `PJRT_Client_Create_Args` |
| `PJRT_KeyValueGetCallback` / `PutCallback` typedefs | **ABSENT** (HIGH) | no symbol match |
| Canonical `KeyValueStoreInterface` wrapper | **ABSENT** (HIGH) | no symbol match |

> **Note —** the `CreateCommunicators` / cross-host communicator-handle surface is the in-process **Collectives extension** (type 21): a CPU-executor-backed XLA surface that is *not* the TPU multi-host coordination path and carries no KV store. It is documented in [the extension chain](extension-chain.md). This page covers the distinct PJRT distributed-coordination KV surface that bootstraps multi-host execution.

The summary: the PJRT-level *collective communicator* surface — a discrete KV-store extension feeding a communicator factory — is **thin to absent** in this build. What is present and load-carrying is the `CoordinationServiceAgent` KV rendezvous and the client factory that threads it down to megascale. This page documents what is there.

---

## Cross-References

- [On-Pod Collectives](../collectives/overview.md) — the on-device collective algorithms (all-reduce / all-gather / reduce-scatter) over the ICI torus; the data plane this control plane bootstraps
- [Megascale Bootstrap](../megascale/bootstrap/overview.md) — the C++-level chip-fabric rendezvous (`xla.megascale.runtime`) that runs *after* this KV rendezvous; the `xla.coordination` vs `xla.megascale.runtime` namespace split
- [ICI fabric](../ici/overview.md) — the inter-chip interconnect DMA layer the collectives ultimately drive
- [PJRT Client, Device & Topology](client-and-device.md#pjrt_client_create--options-ingest-and-backend-selection) — `PJRT_Client_Create` option ingest and backend selection that precedes the factory call
- [Executable Loading & Execution](executable-execution.md) — what runs on the client once coordination and topology are established
- [StreamExecutor PJRT Adapter](stream-executor-pjrt-adapter.md) — the SE-backed adapter beneath the PJRT client
- [PJRT Extension Chain](extension-chain.md) — the 17-node extension chain, including the in-process Collectives (type 21) and Megascale (type 18) extensions
- [back to index](../index.md)
