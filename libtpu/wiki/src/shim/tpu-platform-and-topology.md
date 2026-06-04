# TpuPlatform & TpuNodeContext

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; IDA-recovered C names and demangled C++ symbols quoted verbatim). `.text` VMA equals file offset. Other versions will differ.*

## Abstract

`TpuPlatform_*` and `TpuNodeContext_*` are the two **C-ABI lifecycle rosters** that bring a TPU process up: the first is the platform-singleton face (one process-wide TPU platform, its device count, its topology pointer, its host location), the second is the per-process node-context face (acquire a reference to the TPU node, query whether the host is closed, tear it down). They are the `extern "C"` free functions the open-source StreamExecutor TPU backend calls — through the `ExecutorApiFn()` function-pointer table — to *construct and own* the runtime, as distinct from the topology accessors that merely *read* an already-built `tpu::TpuTopology`. Both clusters live in one contiguous-ish region of the C-ABI block (`TpuPlatform_*` at `0xEAB8B80`–`0xEAB8FC0`, `TpuNodeContext_*` at `0xEACA260`–`0xEACA440`), recovered from `learning/45eac/tfrc/executor/stream_executor/tpu_node_context_c_api.cc` references in the binary.

The single most important structural fact, and the one a reimplementer will get wrong by default, is that the C-ABI `TpuPlatform_*` functions **do not touch `tensorflow::tpu::TpuPlatform`** — the `0x98`-byte SE `Platform` object that [`RegisterTpuPlatform`](../pjrt/stream-executor-host-interpreter.md) installs in `PlatformManager`. They dispatch into a *different*, lower object: `deepsea::executor::DeepseaPlatform`, reached through a Meyers-singleton (`GetUnderlyingDeepseaPlatform::platform`, a `__cxa_guard`-built function-local static populated by `deepsea::executor::GetRegisteredDeepseaPlatform @ 0x1d4935e0`). "Deepsea" is the internal codename for the TPU executor layer; the `TpuPlatform_*` roster is the thin C wrapper over the Deepsea platform's vtable, while the SE `TpuPlatform` C++ class is a *separate* StreamExecutor `Platform` that forwards its own virtual methods through the `ExecutorApiFn()` slots that point at these very functions. Two "platform" objects, one underlying Deepsea singleton.

`TpuNodeContext_*` is backed by yet another object — `tensorflow::TPUNodeInterfaces` and its RAII `ScopedRef` — that owns the per-process driver attachment. `Create` mints a heap `ScopedRef` and runs `InitScopedRef`; `Initialize` resolves the live `TPUNodeInterfaces*`; `CloseTpuHost` shuts the host down; `Free` tears the ref down with two fatal CHECKs. This page owns the per-function impl-symbol map for both rosters and the vtable-offset map for the Deepsea dispatch; the topology *accessors* that read the resulting `tpu::TpuTopology` are on [TpuTopology & TpuCoreLocation](tpu-topology.md).

For reimplementation, the contract is:

- **The two rosters** — 11 `TpuPlatform_*` and 5 `TpuNodeContext_*` `extern "C"` functions, their addresses, and the C++ object/method each backs.
- **The Deepsea-singleton dispatch** — every `TpuPlatform_*` call first ensures the `GetUnderlyingDeepseaPlatform::platform` Meyers-singleton is built, then calls a fixed vtable offset on it (`+16` Id, `+32` VisibleDeviceCount, `+48` Initialize, `+72` GetExecutor, `+96` ShouldRegister…, `+104` GetTopologyPtr).
- **The two status conventions** — POD return (`Initialized` → `1`, `VisibleDeviceCount` → `int`) versus the `absl::Status`-out idiom (`Initialize`, `GetExecutor`, all of `TpuNodeContext_*` write an out-param `StatusRep**` and `Unref` the prior value).
- **The node-context RAII shape** — `Create`/`Free` are a paired heap-`ScopedRef` allocator/destructor with fatal null-CHECKs; `CompactionSupported` re-acquires a scoped ref internally rather than taking one.

| | |
|---|---|
| **Roster prefixes** | `TpuPlatform_*` (11), `TpuNodeContext_*` (5) |
| **C-ABI blocks** | `TpuPlatform_*` @ `0xEAB8B80`–`0xEAB8FC0`; `TpuNodeContext_*` @ `0xEACA260`–`0xEACA440` |
| **Platform backing object** | `deepsea::executor::DeepseaPlatform` via `GetUnderlyingDeepseaPlatform::platform` (Meyers-singleton) |
| **Singleton builder** | `deepsea::executor::GetRegisteredDeepseaPlatform @ 0x1d4935e0` |
| **Node backing object** | `tensorflow::TPUNodeInterfaces` + `tensorflow::TPUNodeInterfaces::ScopedRef` |
| **Source file (recovered)** | `…/stream_executor/tpu_node_context_c_api.cc` (from a CHECK string in `TpuNodeContext_Free`) |
| **Reached via** | `ExecutorApiFn()` slots (the accessor pattern is on the [shim overview](overview.md)) |
| **Status convention** | `absl::Status`-out (`StatusRep**` out-param + `Unref`) for fallible calls; POD return for queries |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

> **Scope —** the `*ApiFn()` accessor pattern, the opaque-handle convention, and the full roster map are on the [shim overview](overview.md) (linked, not re-derived). The *topology accessor* roster (`TpuTopology_*` / `TpuCoreLocation_*`) that reads the `tpu::TpuTopology` this page's `GetTopologyPtr` returns is on [TpuTopology & TpuCoreLocation](tpu-topology.md) — contrast: that page *reads* geometry, this page *owns the platform/node-context lifecycle*. The SE `tensorflow::tpu::TpuPlatform` class registration (`RegisterTpuPlatform @ 0xe99a3a0`, the `0x98`-byte object, `PlatformManager`) is on [StreamExecutor Platform & Executor Model](../pjrt/stream-executor-host-interpreter.md). The one-time population of the `ExecutorApiFn()` slots that point at these functions is on [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md).

> **Note —** three names that look like they belong on this roster are **not** C-ABI `TpuPlatform_*`/`TpuNodeContext_*` functions in this build. There is no `TpuPlatform_GetTpuHostLocation`, no `TpuPlatform_TpuMemoryLimit`, and no `StopChipHeartbeats` symbol at all. `GetTpuHostLocation` and `TpuMemoryLimit` exist only as C++ *methods* on the SE `tensorflow::tpu::TpuPlatform` class (`…TpuPlatform::GetTpuHostLocation() const @ 0xe999f60`, `…TpuPlatform::TpuMemoryLimit(long*) @ 0xe99a2c0`); the memory limit is exposed over the C ABI only under a *different* roster, `TpuConfigurationApi_TpuMemoryLimit @ 0xe8cdc40` (see [TpuConfigurationApi](tpu-configuration-api.md)). The `TpuPlatform_*` roster's nearest equivalent to a host location is `GetHostLocation` (§2); there is no `TpuPlatform_*` memory-limit entry — that is a configuration-API concern. The verified roster is the 11 + 5 functions documented below.

---

## 1. The Deepsea-Singleton Dispatch

### Purpose

Every `TpuPlatform_*` function is a stateless C entry that operates on **one process-global object**: the registered Deepsea platform. There is no platform handle passed in — the functions that take no platform argument (`Id`, `VisibleDeviceCount`, `GetTopologyPtr`, …) reach the singleton directly. This is the defining difference from the topology roster, whose accessors all receive a `topo` handle as `a1`. The platform is a *singleton*; the topology is an *object you hold*.

### Entry Point

The dispatch shape is identical across the roster. Worked path for `VisibleDeviceCount`:

```text
xla / SE TPU backend
  └─ tensorflow::tpu::TpuPlatform::VisibleDeviceCount()        (host-side SE Platform method)
       └─ tbl = stream_executor::tpu::ExecutorApiFn()           0x20819360  — singleton fn-ptr struct
       └─ tbl[slot]( )                                          — call through the slot
              │  (slot populated at init to point at:)
              ▼
       TpuPlatform_VisibleDeviceCount                           0xEAB8E40   — plugin-side C-ABI impl
              └─ EnsureDeepseaSingleton()                        — __cxa_guard + GetRegisteredDeepseaPlatform
              └─ (*platform->vtable[+32])(platform)              0x1d4935e0 built it; +32 = device count
```

### Algorithm — the singleton guard

Every `TpuPlatform_*` body opens with the same Meyers-singleton guard. IDA renders two textually-different but semantically-identical forms (the compiler reorders the early-return); both reduce to "build once, then call the vtable":

```c
function EnsureDeepseaSingleton():                       // inlined prologue of every TpuPlatform_*
    if !guard_byte(GetUnderlyingDeepseaPlatform::platform):
        if __cxa_guard_acquire(&guard):
            platform = deepsea::executor::GetRegisteredDeepseaPlatform()   // 0x1d4935e0
            __cxa_guard_release(&guard)
    return GetUnderlyingDeepseaPlatform::platform        // may be NULL if no TPU registered

function TpuPlatform_VisibleDeviceCount():               // 0xEAB8E40
    p = EnsureDeepseaSingleton()
    return (*(int(**)(void*))(*(void**)p + 32))(p)        // vtable +32

function TpuPlatform_New():                              // 0xEAB8B80
    p = EnsureDeepseaSingleton()
    if !p: return NULL                                    // only New / GetExecutor null-check p
    h = operator new(8); *h = p; return h                 // 8-byte handle box wrapping the singleton ptr
```

> **QUIRK —** `TpuPlatform_New` does not construct anything. It returns an 8-byte heap box holding a copy of the *shared* Deepsea singleton pointer — every `New` hands back a fresh box pointing at the same underlying platform. `TpuPlatform_Free` (`0xEAB8C00`) is correspondingly just `if (h) free(h)`: it frees the 8-byte box, never the singleton. A reimplementer who makes `New` allocate a real platform and `Free` destroy it will double-free the process-global on the second `New`/`Free` pair. The box is a handle, not an owner.

> **GOTCHA —** the singleton can be `NULL`. `GetRegisteredDeepseaPlatform` returns null when no TPU platform was registered (no device, or registration disabled). Only `TpuPlatform_New` (returns `NULL`) and `TpuPlatform_GetExecutor` (returns a failed `StatusOr`) defend against it. The query functions (`Id`, `VisibleDeviceCount`, `GetTopologyPtr`, `GetHostLocation`, `ShouldRegisterTpuDeviceToDeviceCopy`) dereference the singleton's vtable unconditionally — `NULL->vtable[+N]` crashes. The contract is: the host must observe a successful `New`/`Initialize` before calling any query.

### The vtable-offset map

The Deepsea platform vtable offsets the roster dispatches through, confirmed by the call expressions in each body:

| Offset | Deepsea method (inferred) | C-ABI caller | Confidence |
|---|---|---|---|
| `+16` | platform id | `TpuPlatform_Id` (`0xEAB8DE0`) | HIGH |
| `+32` | visible device count | `TpuPlatform_VisibleDeviceCount` (`0xEAB8E40`) | CERTAIN |
| `+48` | initialize (returns `absl::Status`) | `TpuPlatform_Initialize` (`0xEAB8C20`) | CERTAIN |
| `+72` | get executor for ordinal (returns `StatusOr<exec*>`) | `TpuPlatform_GetExecutor` (`0xEAB8CC0`) | CERTAIN |
| `+96` | should register D2D copy | `TpuPlatform_ShouldRegisterTpuDeviceToDeviceCopy` (`0xEAB8EA0`) | CERTAIN |
| `+104` | get topology pointer | `TpuPlatform_GetTopologyPtr` (`0xEAB8F00`) | CERTAIN |

`GetHostLocation` does **not** go through the vtable — it calls the non-virtual `deepsea::executor::DeepseaPlatform::GetHostLocation @ 0x1d0e79a0` directly on the singleton. `Initialized` and `GetRuntimeVersion` do not touch the singleton's vtable at all (see §2).

---

## 2. The TpuPlatform_ Roster

### Function Map

Eleven `extern "C"` functions, three lifecycle and eight query/accessor. "Backs" names the Deepsea vtable offset or helper each dispatches to.

| Function | Addr | Backs | Output / Convention | Confidence |
|---|---|---|---|---|
| `TpuPlatform_New` | `0xEAB8B80` | `EnsureDeepseaSingleton`; box the ptr | `void*` handle (8-byte box) or `NULL` | CERTAIN |
| `TpuPlatform_Free` | `0xEAB8C00` | `free(handle)` — frees the box only | void | CERTAIN |
| `TpuPlatform_Initialize` | `0xEAB8C20` | singleton vtable `+48` | `absl::Status`-out (`StatusRep**`) | CERTAIN |
| `TpuPlatform_Initialized` | `0xEAB8CA0` | nothing — constant | `char` → always `1` | CERTAIN |
| `TpuPlatform_GetExecutor` | `0xEAB8CC0` | singleton vtable `+72` | `StatusOr<executor*>` (boxed) | HIGH |
| `TpuPlatform_Id` | `0xEAB8DE0` | singleton vtable `+16` | scalar id | HIGH |
| `TpuPlatform_VisibleDeviceCount` | `0xEAB8E40` | singleton vtable `+32` | `int` | CERTAIN |
| `TpuPlatform_ShouldRegisterTpuDeviceToDeviceCopy` | `0xEAB8EA0` | singleton vtable `+96` | bool | CERTAIN |
| `TpuPlatform_GetTopologyPtr` | `0xEAB8F00` | singleton vtable `+104` | `tpu::TpuTopology*` (opaque) | CERTAIN |
| `TpuPlatform_GetHostLocation` | `0xEAB8F60` | `DeepseaPlatform::GetHostLocation @ 0x1d0e79a0` (non-virtual) | host-location handle | CERTAIN |
| `TpuPlatform_GetRuntimeVersion` | `0xEAB8FC0` | `BuildData::Timestamp` + `Changelist`; static `kMetadata` | fills caller struct (version + string) | HIGH |

> **NOTE —** `GetTopologyPtr` is the seam between this page and [TpuTopology & TpuCoreLocation](tpu-topology.md). It returns the opaque `tpu::TpuTopology*` (vtable `+104`); the host then hands that pointer to every `TpuTopology_*` accessor as their `a1`. The platform *produces* the topology pointer; the topology roster *consumes* it. Neither side passes the C++ object by value — only the `void*` crosses.

### Algorithm — the two non-dispatching outliers

Two functions break the singleton-dispatch mould and a reimplementer must reproduce them exactly:

```c
function TpuPlatform_Initialized():                      // 0xEAB8CA0
    return 1                                              // hardcoded TRUE — no state inspected

function TpuPlatform_GetRuntimeVersion(out):             // 0xEAB8FC0
    // Meyers-singleton over a static `kMetadata` (NOT the platform singleton):
    if !guard(TpuPlatform_GetRuntimeVersion::kMetadata):
        if __cxa_guard_acquire(&guard):
            kMetadata = StrCat(BuildData::Timestamp(), " cl/", BuildData::Changelist())
            __cxa_guard_release(&guard)
    out[0]  = 0                                           // version major/minor packed
    out[8]  = 1
    out[16] = ptr-to(kMetadata string)                   // SSO-aware: inline vs heap by sign of len byte
    out[24] = kMetadata length
    return out
```

> **QUIRK —** `TpuPlatform_Initialized` always returns `1`. It inspects no state — not the singleton, not a flag. The "is the platform initialized" question is answered statically true at the C-ABI boundary; the real initialization fallibility lives in `TpuPlatform_Initialize` (which can return a failed `absl::Status` from vtable `+48`). A reimplementer must not wire `Initialized` to a real readiness flag: the SE backend treats it as an always-true capability probe, and the actual gate is `Initialize`'s status.

### The absl::Status-out idiom

`Initialize` and `GetExecutor` use the C-ABI status convention shared across the whole shim: the caller passes a `StatusRep**` out-param holding the *previous* status object; the callee computes a new `StatusRep*`, and if it differs, stores it into the out-param and `Unref`s the old one (an `absl::Status` is a tagged pointer — `& 1` distinguishes an inline OK from a heap `StatusRep*` that needs refcounting).

```c
function TpuPlatform_Initialize(self, status_out):       // 0xEAB8C20
    p   = EnsureDeepseaSingleton()
    new = (*(p->vtable[+48]))(p)                          // Deepsea initialize → StatusRep* (tagged)
    old = *status_out
    if new == old:
        if (new & 1) == 0: StatusRep::Unref(new)          // both heap, same ptr → drop one ref
    else:
        *status_out = new
        if (old & 1) == 0: StatusRep::Unref(old)          // replace, release the previous status
```

`GetExecutor` (`0xEAB8CC0`) is the `StatusOr<T>` form of the same idiom: the Deepsea call (vtable `+72`) writes a 2-slot `{status, value}` into a stack temp; if the status slot is the OK sentinel (`&dword_0 + 1`), it boxes `value` into a fresh `operator new(8)` handle and returns it; otherwise it `Unref`s and returns `NULL`, with a `ThrowBadStatusOrAccess` path if the host misuses the result.

---

## 3. The TpuNodeContext_ Roster

### Purpose

A `tensorflow::TPUNodeInterfaces` is the per-process attachment to the TPU node — the object that owns the live driver session, the node's topology, and its core location. The C-ABI `TpuNodeContext_*` roster manages a `ScopedRef` into it: an RAII reference that, while held, keeps the node alive. The host calls `Create` once to obtain the ref, `Initialize` to resolve the underlying `TPUNodeInterfaces*`, `CloseTpuHost` to shut the host down, and `Free` to drop the ref. All four fallible calls use the same `absl::Status`-out idiom as `TpuPlatform_Initialize`. Source file recovered from a CHECK string: `…/stream_executor/tpu_node_context_c_api.cc`.

### Function Map

| Function | Addr | Backs (C++ method) | Output / Convention | Confidence |
|---|---|---|---|---|
| `TpuNodeContext_Create` | `0xEACA260` | `TPUNodeInterfaces::InitScopedRef` | `ScopedRef**` handle + `absl::Status`-out | CERTAIN |
| `TpuNodeContext_Free` | `0xEACA2E0` | `~ScopedRef` + `free`; two fatal CHECKs | void | CERTAIN |
| `TpuNodeContext_CloseTpuHost` | `0xEACA3C0` | `TPUNodeInterfaces::CloseTPUHost` | `absl::Status`-out | CERTAIN |
| `TpuNodeContext_Initialize` | `0xEACA400` | `TPUNodeInterfaces::Get` | `absl::Status`-out (+ resolves `TPUNodeInterfaces**`) | CERTAIN |
| `TpuNodeContext_CompactionSupported` | `0xEACA440` | `InitScopedRef` → `TpuChipConfig::Megacore` | bool | HIGH |

### Algorithm — the RAII pair

`Create` and `Free` are a heap-`ScopedRef` allocator/destructor. The asymmetry — `Create` allocates an 8-byte box holding the `ScopedRef*`, `Free` validates and tears it down — is the reimplementation contract:

```c
function TpuNodeContext_Create(node_index, status_out):  // 0xEACA260
    ref = operator new(8); *ref = NULL                    // 8-byte box for the ScopedRef
    st  = TPUNodeInterfaces::InitScopedRef(node_index, ref)   // fills *ref, returns StatusRep*
    merge_status(status_out, st)                          // same Status-out idiom as §2
    handle = operator new(8); *handle = ref               // box the box
    return handle

function TpuNodeContext_Free(handle):                    // 0xEACA2E0
    CHECK(handle != NULL,  "node_context != nullptr")            // tpu_node_context_c_api.cc:33 (FATAL)
    inner = *handle
    CHECK(inner != NULL,   "node_context->node_ref != nullptr")  // :34 (FATAL)
    *handle = NULL
    ~ScopedRef(inner); free(inner)                        // destroy + free the ScopedRef
    // (second NULL-guarded teardown of *handle handles a re-entrant edge)
    free(handle)
```

> **GOTCHA —** `TpuNodeContext_Free` is **fatal** on a null handle or a null inner ref — it does not return an error, it `LogMessageFatal`s and aborts (the two CHECKs at `tpu_node_context_c_api.cc:33` and `:34`). This is the opposite of `TpuPlatform_Free`, which is a silent `if (h) free(h)`. A reimplementer must keep the node-context teardown fatal-on-misuse: the SE backend relies on the abort to surface a double-free / use-after-free of the node ref rather than corrupting the driver session. Do not "helpfully" make it tolerate null.

> **QUIRK —** `TpuNodeContext_CompactionSupported` does *not* take a node-context handle. It re-acquires its own throwaway `ScopedRef` via `InitScopedRef`, then — only if that ref's status is OK — walks `TPUNodeInterfaces::tpu_topology()` → `tensor_core_location()` → `tpu::TpuChipConfig::GetUserStack(...)`, and if the user-stack byte at `+40` is `1`, returns `tpu::TpuChipConfig::Megacore(chip_config)`. It defaults to `1` (supported) on any failure or non-megacore path, and always destroys its scoped ref before returning. So "compaction supported" is really "is this a megacore chip config", computed fresh each call against a transient node reference.

### The Initialize / CloseTpuHost status forms

`TpuNodeContext_Initialize` (`0xEACA400`) calls `TPUNodeInterfaces::Get(node_index, &out, node_out)` — it resolves the live `TPUNodeInterfaces*` into a caller out-param *and* threads the `absl::Status` back through the standard out-param idiom. `TpuNodeContext_CloseTpuHost` (`0xEACA3C0`) calls `TPUNodeInterfaces::CloseTPUHost(node)` and merges the returned status into the node's leading `StatusRep*` slot. Both are pure status-plumbing wrappers over a single `TPUNodeInterfaces` method; the only logic is the tagged-pointer `Unref` of the prior status.

---

## 4. Validity Gating

The two rosters gate very differently, and the contrast is the reimplementation hazard:

| Function(s) | Guard | Failure behaviour |
|---|---|---|
| `TpuPlatform_New` | singleton non-null after build | returns `NULL` |
| `TpuPlatform_GetExecutor` | `StatusOr` OK sentinel | returns `NULL`; `ThrowBadStatusOrAccess` on misuse |
| `TpuPlatform_Initialize` / `_GetExecutor` | `absl::Status`-out | propagates failed status, `Unref`s prior |
| `TpuPlatform_Id` / `_VisibleDeviceCount` / `_GetTopologyPtr` / `_GetHostLocation` / `_ShouldRegister…` | none | dereferences singleton vtable unconditionally — crashes if singleton is `NULL` |
| `TpuPlatform_Initialized` | none | always `1` |
| `TpuNodeContext_Create` / `_Initialize` / `_CloseTpuHost` | `absl::Status`-out | propagates failed status |
| `TpuNodeContext_Free` | two CHECKs (`:33`, `:34`) | **FATAL abort** on null handle/ref |
| `TpuNodeContext_CompactionSupported` | scoped-ref status OK | defaults to `1` (supported) on any failure |

> **NOTE —** the split mirrors the topology roster's split (pure readers vs. defended accessors). Here, the *platform query* functions assume a successfully-built singleton exactly as the [topology pure-readers](tpu-topology.md#4-validity-gating) assume a live `tpu::TpuTopology`; the *node-context* functions, dealing with a per-process resource that can legitimately be torn down, instead carry status out-params (for the fallible operations) and a fatal CHECK (for the destructor, where a null is always a caller bug). A reimplementer who applies one discipline uniformly will either over-defend the cheap platform queries or under-defend the node-context teardown.

---

## Related Components

| Name | Relationship |
|---|---|
| `deepsea::executor::DeepseaPlatform` | the underlying TPU platform object every `TpuPlatform_*` dispatches into (vtable `+16…+104`) |
| `deepsea::executor::GetRegisteredDeepseaPlatform @ 0x1d4935e0` | builds the `GetUnderlyingDeepseaPlatform::platform` Meyers-singleton |
| `tensorflow::tpu::TpuPlatform` (`0x98` bytes) | the *separate* SE `Platform` class registered by `RegisterTpuPlatform`; forwards its methods through the `ExecutorApiFn()` slots that point at this roster |
| `tensorflow::TPUNodeInterfaces` / `…::ScopedRef` | the per-process node attachment + RAII ref the `TpuNodeContext_*` roster manages |
| `tpu::TpuChipConfig::Megacore` | backs `TpuNodeContext_CompactionSupported`'s megacore probe |
| `TpuConfigurationApi_TpuMemoryLimit @ 0xe8cdc40` | where the C ABI actually exposes the memory limit (not on `TpuPlatform_*`) |

## Cross-References

- [The TfTpu C-API Shim](overview.md) — the `*ApiFn()` accessor pattern, opaque-handle convention, and the roster map this page is one entry of
- [TpuTopology & TpuCoreLocation](tpu-topology.md) — the `TpuTopology_*` / `TpuCoreLocation_*` geometry accessors that read the topology `TpuPlatform_GetTopologyPtr` returns; contrast: that page reads geometry, this owns the platform/node lifecycle
- [TpuExecutor Roster](tpu-executor-roster.md) — the `TpuExecutor_*` per-device runtime cluster minted off the platform, reached through the same `ExecutorApiFn()` table
- [TpuCompiler Roster](tpu-compiler-roster.md) — the `TpuCompiler_*` / `TpuCompile_*` compilation C surface that runs against executors from this platform
- [StreamExecutor Platform & Executor Model](../pjrt/stream-executor-host-interpreter.md) — the SE `tensorflow::tpu::TpuPlatform` class, `RegisterTpuPlatform @ 0xe99a3a0`, and `PlatformManager`; the host-side consumer of this C-ABI roster
- [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) — the one-time population of the `ExecutorApiFn()` slots that point at these functions
