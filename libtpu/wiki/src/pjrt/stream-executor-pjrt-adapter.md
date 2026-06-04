# StreamExecutor → PJRT Adapter

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, `libtpu_lts_20260413_b_RC00`). Other wheels will differ.*

## Abstract

In upstream XLA, the bridge from a StreamExecutor `LocalClient` / `Platform` to the modern `PjRtClient` is `xla::PjRtStreamExecutorClient` (and the TPU-specific `TfrtTpuClient`): a `PjRtClient` subclass that holds a `LocalDeviceState` per device, each owning a `stream_executor::StreamExecutor` and a small pool of `stream_executor::Stream`s, and forwards execution through `Stream::ThenLaunch` / `Stream::ThenMemcpy`. A reimplementer arriving at `libtpu.so` expecting to find that class will not find it. **There is no `xla::PjRtStreamExecutorClient` in this binary** — the symbol is undefined (confirmed: no defined `PjRtStreamExecutorClient` symbol exists; the only residual SE-PjRt symbol is `PjRtStreamExecutorDeviceDescription`, dead GPU-path code). The "StreamExecutor → PJRT adapter" is instead `xla::TpuClient`, derived from `xla::CommonPjRtClient : xla::PjRtClient`, and it sits directly on the TFRT-native TPU device runtime `tpu::System` through `tsl::AsyncValue` events — bypassing the legacy `stream_executor::tpu::TpuExecutor` / `TpuStream` / `TpuTransferManager` stack entirely.

This page documents the *bridge layer*: the `GetTpuPjRtClient` construction path that turns a parsed config into a wired `TpuClient`, the `TpuClient : CommonPjRtClient : PjRtClient` class shape and its 3-arg-then-vector TFRT constructor, the device-enumeration loop that wraps each `tpu::TpuCoreLocation` into an `xla::TpuDevice`, and how an SE `Stream`/`Executor` would have been bound under each `PjRtDevice` — except that here a `TpuDevice` binds a *shared* `tpu::System*` plus a `TpuCoreLocation` and a throttle `Semaphore`, not a per-device executor. The C-ABI surface above this (`PJRT_Client_Create`, the 208-byte `PJRT_Client` wrapper, the device/memory accessor slots) is owned by [Client, Device & Topology](client-and-device.md); the SE platform registration and host interpreter below it by [StreamExecutor & Host Interpreter](stream-executor-host-interpreter.md); the per-execution hot path by [Execute Async On Stream](../runtime/execute-async-on-stream.md). This page owns the seam in the middle: `GetTpuPjRtClient` → `TpuClient` → `TpuDevice`-wraps-`tpu::System`.

For reimplementation, the contract is:

- **The construction chain.** `GetTpuPjRtClient(config)` → one-shot `HostContext` build → `TpuStatesManager` / `TpuSystemState` singleton → `GetTpuPjRtClientInternal` device enumeration → `make_unique<TpuClient>(…)` → optional MegaScale / Tf decorator → `CreateWrapperClient`.
- **The class hierarchy and ctor shape.** `TpuClient : CommonPjRtClient : PjRtClient`; the eight-argument TFRT constructor that takes the device vector, host context, host allocator, system state, and states manager.
- **The device-wrapping rule.** One `xla::TpuDevice` per `tpu::TpuCoreLocation` from `TpuTopology::logical_devices`, each holding a *shared* `tpu::System*` (not a private executor), bound to the client by a post-construction `SetClient` back-edge that also installs the `AsyncWorkRunner`.
- **The SE→TPU concept mapping.** Which SE abstraction each TPU-runtime object replaces, so a reader carrying the upstream model can translate it forward.

| | |
|---|---|
| **Bridge entry** | `xla::GetTpuPjRtClient(const PjRtTpuClientConfig&)` @ `0xF8008C0` (1226 B) |
| **Device enumeration** | `xla::GetTpuPjRtClientInternal(…)` @ `0xF800DA0` |
| **Adapter client class** | `xla::TpuClient` — ctor `0xF801980`, vtable `0x2177B598`, typeinfo `0x2177BA40` |
| **Client base framework** | `xla::CommonPjRtClient` — vtable `0x2178A108`, typeinfo `0x21789EB0` |
| **Abstract PJRT base** | `xla::PjRtClient` — vtable `0x21CA9C98`, typeinfo `0x21CA9E28` |
| **Adapter device class** | `xla::TpuDevice` — ctor `0xF7FDC40` (object `0x1E0` = 480 B), vtable `0x2177B4D0` |
| **Device→client bind** | `xla::TpuDevice::SetClient(TpuClient*, AsyncWorkRunner*)` @ `0xF7FE520` |
| **Device runtime backing** | `tpu::System` — `Initialize` @ `0x1D0AE420` (the SE `StreamExecutor` analogue) |
| **C-ABI wrap** | `pjrt::CreateWrapperClient(unique_ptr<PjRtClient>)` @ `0xF872060` |

---

## The SE → TPU-Runtime Concept Mapping

### Purpose

The fastest way to read this layer is as a delta from the upstream `PjRtStreamExecutorClient` model the reader already owns. Every StreamExecutor abstraction has a TPU-runtime counterpart, but the *shape* differs: SE is a synchronous-stream model (a FIFO of operations per `Stream`, with `Event`s to cross-stream-order); the TPU runtime is an async-value DAG model (a graph of `tpu::TpuEvent` dependencies issued through `tpu::TpuEventIssuer` sequence points). The adapter does not translate one into the other at runtime — it never constructs an SE object at all. It is built natively against the async model.

### The Mapping Table

This is the single most important table for a reader carrying the upstream model. Each row is "what you would have looked for" → "what `libtpu` uses instead" → "the underlying TPU-runtime object."

| Upstream SE / PjRt concept | libtpu adapter object | TPU-runtime backing |
|---|---|---|
| `PjRtStreamExecutorClient` / `TfrtTpuClient` | `xla::TpuClient` (`: CommonPjRtClient : PjRtClient`) | `tpu::System` (singleton, via `TpuSystemState` / `TpuStatesManager`) |
| `PjRtStreamExecutorDevice` | `xla::TpuDevice` | `tpu::System*` + `tpu::TpuCoreLocation` + `xla::Semaphore` |
| `LocalDeviceState` + `StreamExecutor` (per device) | *(none — no per-device executor)* | shared `tpu::System` + `AsyncWorkRunner` + `tfrt::ConcurrentWorkQueue` |
| `stream_executor::StreamExecutor` | *(none)* | `tpu::System` (one, shared across all devices) |
| `Stream::ThenLaunch` | *(no `Stream`)* | `tpu::System::Execute(AsyncValueRef<ProgramHandle>, …)` @ `0x1D0B33E0` |
| `Stream::ThenMemcpy` | *(no `Stream`)* | `tpu::System::TransferTo/FromDevice` @ `0x1D0AFA20` / `0x1D0B0160` |
| `stream_executor::Event` | `xla::TpuTrackedDeviceEvent` | `tpu::TpuEvent` (a `tsl::AsyncValueRef`) |
| `Stream` ordering FIFO | *(no FIFO)* | `tpu::TpuEventIssuer` sequence points + `tsl::AsyncValue` deps |
| `DeviceMemoryBase` | `tpu::TpuSharedMemoryLocation` | TPU HBM address |
| `PjRtStreamExecutorBuffer` | `xla::TpuRawBuffer` (`: CommonPjRtRawBuffer`) | `tpu::TpuBuffer` at a `TpuSharedMemoryLocation` |
| `TransferManager` | *(inline in `TpuRawBuffer` / `TpuClient`)* | `tpu::System::TransferTo/FromDevice` |
| SE `Compiler` (registered via factory) | `xla::TpuCompiler` (`: PjRtCompiler`) | jellyfish JIT (`TpuJitCompileHloWithOptions`) |

> **QUIRK —** the empty cells are the point of the table. A reimplementer who builds a `LocalDeviceState`-style object per device, each owning a `StreamExecutor` and a stream pool, will have reproduced *upstream XLA*, not `libtpu`. The TPU adapter shares **one** `tpu::System` across every `TpuDevice`; there is no per-device executor and no `Stream` at all. The per-device state that does exist is small: a `TpuCoreLocation` (which physical core this device maps to) and a `Semaphore` (an in-flight-launch throttle). Ordering that an SE backend would get from a per-stream FIFO comes instead from a process-wide `TpuEventIssuer` DAG.

### Why the SE Stack Is Absent, Not Adapted

The legacy SE TPU stack does exist in the binary — `stream_executor::tpu::TpuExecutor`, `tensorflow::tpu::TpuStream`, `tensorflow::tpu::TpuTransferManager`, and the `Tpu*_*` / `TpuExecutor_*` / `TpuStream_*` C-ABI exports — but it is a **parallel** path, not a layer underneath PJRT. It backs `xla::LocalClient` / `xla::Service` and the TF-TPU op kernels, reached through the SE `TpuPlatform` registered at module init (`RegisterTpuPlatform` → `PlatformManager`, documented on [StreamExecutor & Host Interpreter](stream-executor-host-interpreter.md)). The PJRT `TpuClient` adapter never touches it: across the entire `TpuClient` / `TpuDevice` / `TpuRawBuffer` / `TpuLoadedExecutable` code range (`0xF800DA0`–`0xF816EC0`) there is **no** reference to `ExecutorApiFn`, `SE_StreamExecutor`, or `TpuExecutor`. The two host abstractions share the same TPU driver core but use different host-side scheduling: TFRT async-values (PJRT) versus SE Streams (legacy). This page documents only the PJRT branch.

---

## GetTpuPjRtClient — The Construction Bridge

### Purpose

`GetTpuPjRtClient` is where the C-ABI options layer hands off to the runtime. By the time it is called, `PJRT_Client_Create` has already validated the option map and parsed it into a typed `PjRtTpuClientConfig` (that path is owned by [Client, Device & Topology](client-and-device.md)). `GetTpuPjRtClient` brings up the *process-wide* infrastructure — the TFRT `HostContext` and the `tpu::System` singleton — exactly once, then enumerates devices and constructs the `TpuClient`. It returns a `StatusOr<unique_ptr<TpuClient>>` that the C-ABI layer optionally decorates and then wraps.

### Entry Point

```text
pjrt::tpu_plugin::PJRT_Client_Create (0xE6A8840)        ── slot 15; owns option parse (see client-and-device.md)
  └─ xla::GetTpuPjRtClient (0xF8008C0)                   ── THIS PAGE: brings up HostContext + tpu::System, builds TpuClient
       ├─ xla::CreateDefaultHostContext (lambda, __cxa_guard one-shot)
       │    ├─ tfrt::CreateMallocAllocator
       │    └─ tfrt::CreateMultiThreadedWorkQueue(DefaultThreadPoolSize(), …)
       ├─ xla::GetSingletonTpuStatesManager(bool)        (0xF958360)  ── mutex+guard singleton
       │    └─ TpuStatesManager::GetOrCreateTpuSystemState (0xF956E40)
       │         └─ xla::CreateTpuSystemState (0xF95A4A0)  ── lazily brings up tpu::System
       └─ xla::GetTpuPjRtClientInternal(config, host_ctx, system_state, states_mgr) (0xF800DA0)
            ├─ tpu::System::topology() / hal_location()
            ├─ for each TpuCoreLocation in TpuTopology::logical_devices:
            │    ├─ xla::TpuDeviceDescription(core_loc, topology, "TPU", "TpuDevice")
            │    └─ operator new(0x1E0) + xla::TpuDevice::TpuDevice(desc, name, ordinal, tpu::System*)
            ├─ make_unique<xla::TpuClient>(config, process_index, platform_version, devices,
            │                              host_ctx, host_allocator, system_state, states_mgr)
            └─ for each device: TpuDevice::SetClient(TpuClient*, AsyncWorkRunner*)  (0xF7FE520)
```

### Algorithm

```c
function GetTpuPjRtClient(config):                       // 0xF8008C0
    // (1) One-shot process-wide TFRT HostContext. Guarded by the
    //     CreateDefaultHostContext::kInstance __cxa_guard so every client
    //     in the process shares one HostContext / thread pool.
    if !kInstance:                                        // guard var, lines 70-83
        if cxa_guard_acquire(&kInstance_guard):
            alloc = tfrt::CreateMallocAllocator()         // line 85
            queue = tfrt::CreateMultiThreadedWorkQueue(   // line 92
                        DefaultThreadPoolSize(config), …) //   thread count from config
            kInstance = make_shared<tfrt::HostContext>(alloc, queue)   // line 94
            cxa_guard_release(&kInstance_guard)
    host_ctx = kInstance                                  // shared_ptr; refcount bumped (line 116)

    // (2) The tpu::System singleton, reached through the states manager.
    //     GetOrCreateTpuSystemState lazily runs CreateTpuSystemState the
    //     first time, which initialises tpu::System (Initialize @ 0x1D0AE420).
    states_mgr  = GetSingletonTpuStatesManager(use_global_tpu_system)  // 0xF958360, line 212
    system_state = states_mgr.GetOrCreateTpuSystemState(opts)          // 0xF956E40, line 214

    // (3) Tail into device enumeration + client construction.
    //     The 'a6' boolean selects an own-vs-borrow path for system_state.
    return GetTpuPjRtClientInternal(config, host_ctx,    // 0xF800DA0, lines 179/236
                                    system_state, states_mgr)
```

> **NOTE —** the `HostContext` is a *process-wide singleton*, not per-client. The `__cxa_guard` over `CreateDefaultHostContext::kInstance` means a second `PJRT_Client_Create` in the same process reuses the same malloc allocator, the same multi-threaded work queue, and therefore the same `AsyncWorkRunner` thread pool. A reimplementer must not allocate a fresh thread pool per client or the throttle/scheduling assumptions in the execute path break.

### Device Enumeration — GetTpuPjRtClientInternal

`GetTpuPjRtClientInternal` (`0xF800DA0`) is the loop that turns physical TPU cores into PjRt devices. It is the closest analogue to upstream's "build a `LocalDeviceState` per device," except no executor or stream pool is created — only a description and a `TpuDevice`, each pointed at the *one* shared `tpu::System`.

```c
function GetTpuPjRtClientInternal(config, host_ctx, system_state, states_mgr):  // 0xF800DA0
    system   = system_state->system()                    // tpu::System* (line 136: j+64)
    topology = system->topology()                         // tpu::TpuTopology* (line 138)
    hal_loc  = system->hal_location()                     // line 132

    devices = vector<unique_ptr<TpuDevice>>{}
    cores   = topology->logical_devices(TpuCoreType)      // line 140; stride 56 bytes (line 148/323)
    for core_loc in cores:                                // each TpuCoreLocation, 56 B apart
        // (a) static identity: id/process/kind/attributes/memory
        desc = TpuDeviceDescription(core_loc, topology,   // line 155
                                    type_prefix="TPU", name="TpuDevice")
        // (b) the PjRt device object — 0x1E0 (480) bytes, holds a SHARED system ptr
        dev_mem = operator new(0x1E0)                      // line 156
        device  = TpuDevice::TpuDevice(desc, name, ordinal, system)   // line 206, 0xF7FDC40
        devices.push_back(unique_ptr<TpuDevice>{device})   // grows the vector (line 260 = length-error guard)
        // record local-device-id -> TpuSharedMemoryLocation (LocalSharedMemory, line 374)

    // (c) build the client; ctor takes ownership of the device vector
    client = make_unique<TpuClient>(                       // line 545
                 config, process_index, platform_version,
                 devices, host_ctx, host_allocator,
                 system_state, states_mgr)

    // (d) close the back-edge: each device learns its client + work runner
    for device in client->devices():
        device->SetClient(client.get(), client->async_work_runner())   // 0xF7FE520
    return client
```

> **GOTCHA —** the device→client link is a *post-construction* back-edge (`SetClient`), not a constructor argument. The `TpuClient` ctor receives the device vector, but each `TpuDevice` does not know its owning client (or its `AsyncWorkRunner`) until `SetClient` runs afterward. A reimplementer who tries to use `device->client()` from inside the `TpuClient` constructor will read an uninitialised pointer. The two-phase wiring exists because the `AsyncWorkRunner` is a member of the fully-constructed client; it cannot be handed to the devices before the client object exists.

> **QUIRK —** the `TpuCoreLocation` array is walked with a hard-coded 56-byte stride (lines 148, 323). `TpuTopology::logical_devices` returns a contiguous block, and the loop advances `v18 += 56` rather than calling an iterator. That 56-byte `TpuCoreLocation` size is an ABI fact a reimplementer of the topology side must match exactly, or the device-enumeration loop reads garbage cores.

### After Internal Returns — Decorators and the C Wrapper

Back in `PJRT_Client_Create`, the bare `unique_ptr<TpuClient>` is optionally wrapped:

- `MegaScalePjRtClient::CreateMegaScalePjRtClient` (`0xE6EA680`) when multi-slice is active (`FLAGS_megascale_port >= 0` and not skipped).
- `TfPjRtClient::CreateTfPjRtClient` (`0x108524E0`) when `use_tf_pjrt_client == 1` (the default).

Then `CreateWrapperClient` (`0xF872060`) boxes the outermost `xla::PjRtClient*` into the 208-byte C-ABI `PJRT_Client`. These three steps and the wrapper layout are owned by [Client, Device & Topology](client-and-device.md); this page stops at the `unique_ptr<TpuClient>` that `GetTpuPjRtClient` produces.

---

## TpuClient — The Adapter Client Class

### Purpose

`xla::TpuClient` is the concrete `PjRtClient` for TPU. It is the object every C-ABI client slot ultimately virtual-calls into (after unwrapping any MegaScale/Tf decorator and the `PJRT_Client` POD). It overrides roughly 75 of the `CommonPjRtClient` framework methods — buffer creation, executable load, device assignment, transfer plumbing — and owns the per-process runtime handles a StreamExecutor client would have split across `LocalDeviceState`s.

### Class Hierarchy

```text
xla::PjRtClient                 (abstract base; vtable 0x21CA9C98, typeinfo 0x21CA9E28)
   ▲
xla::CommonPjRtClient           (async-value framework; vtable 0x2178A108, typeinfo 0x21789EB0)
   │   BufferFromHostBuffer, DefineBuffer, PrepareArguments, CreateOutputs,
   │   TrackFuture, ShouldRetryOnOom, GetTransposePlan, … (~79 methods)
   ▲
xla::TpuClient                  (the adapter; ctor 0xF801980, vtable 0x2177B598, typeinfo 0x2177BA40)
       overrides ~75 methods; holds devices, HostContext, HostMemoryAllocator,
       TpuSystemState, TpuStatesManager, Semaphore
```

Two optional decorators sit *outside* this hierarchy as wrappers, not subclasses:

| Decorator | Role | Engage condition | Symbol |
|---|---|---|---|
| `xla::MegaScalePjRtClient` | Multi-slice overlay over a `unique_ptr<TpuClient>` + `MultiSliceConfig`; forwards/augments compile+execute across slices | `FLAGS_megascale_port >= 0`, not skipped | `CreateMegaScalePjRtClient` @ `0xE6EA680`; typeinfo `0x215FD068` |
| `xla::TfPjRtClient` | TF-thread-safety decorator (`DestroyWrappedBuffersAndClient`); owns wrapped buffers | `use_tf_pjrt_client == 1` (default) | `CreateTfPjRtClient` @ `0x108524E0`; typeinfo `0x217EFED0` |

`xla::AbstractTpuClient` (typeinfo `0x215FD080`) is a thin base sharing topology helpers — e.g. `GetSubsliceTopologyForCompilation` (`0xE6EE120`) — shared between `TpuClient` and the MegaScale path.

### The TFRT Constructor

The constructor signature is byte-confirmed from the demangled symbol at `0xF801980`. It is the TFRT shape — async-value-native, taking pre-built infrastructure rather than constructing executors:

```c
TpuClient::TpuClient(                                    // 0xF801980
    const PjRtTpuClientConfig&        config,
    int                               process_index,
    std::string                       platform_version,
    std::vector<unique_ptr<TpuDevice>> devices,          // takes ownership of the enumerated devices
    std::shared_ptr<tfrt::HostContext> host_context,     // the process-wide singleton
    std::unique_ptr<HostMemoryAllocator> host_allocator, // host-staging allocator
    std::unique_ptr<TpuSystemState, MaybeOwningDeleter> system_state,  // may own or borrow
    std::shared_ptr<TpuStatesManager> states_manager)
```

> **NOTE —** the `MaybeOwningDeleter` on the `TpuSystemState` is the hinge of single-process-many-clients behaviour. When `use_global_tpu_system` is set, the system state is *borrowed* from the global singleton (the deleter is a no-op); otherwise the client owns it. This is why the `a6` boolean flows from `GetTpuPjRtClient` into `GetTpuPjRtClientInternal` — it selects the own-vs-borrow construction of the `MaybeOwningDeleter`.

### Object Field Layout — Not Fully Decoded

The constructor body's exact field stores were not dereferenced from the binary. From the ctor *signature* and the call sites, the object holds: the device vector, the `HostContext` shared_ptr, the `HostMemoryAllocator`, the `TpuSystemState` pointer (with its maybe-owning deleter), the `TpuStatesManager` shared_ptr, and a per-client `Semaphore` (the in-flight-computation throttle). The concrete byte offsets of these members are **LOW confidence** — a reimplementer needs a full decompile of `0xF801980` to place them. What *is* certain is the membership set and that `async_work_runner()` and `devices()` are accessible immediately after construction (they are read in `GetTpuPjRtClientInternal`'s `SetClient` loop).

### Function Map

| Function | Addr | Role |
|---|---|---|
| `TpuClient::TpuClient` (ctor) | `0xF801980` | Build the adapter from enumerated devices + infra |
| `TpuClient::LookupDevice` | `0xF8033A0` | global device id → `PjRtDevice*` |
| `TpuClient::platform_name` | `0xF816D20` | `"tpu"` |
| `TpuClient::platform_id` | `0xF816D00` | TPU platform id constant |
| `TpuClient::process_index` | `0xF816C40` | host process index |
| `TpuClient::memory_spaces` | `0xF816CE0` | HBM + pinned/unpinned host spaces |
| `TpuClient::AllocateBuffer` | `0xF7FC5A0` | device HBM alloc → `tpu::AllocateBuffer` |
| `TpuClient::AllocateRawBuffer` | `0xF7FB1E0` | host/CPU-space alloc dispatch |
| `TpuClient::CompileAndLoad` (XlaComputation) | `0xF804F20` | compile entry → jellyfish JIT |
| `TpuClient::CompileAndLoad` (MLIR) | `0xF8068E0` | MLIR compile entry |
| `TpuClient::TrackFuture` | `0xF7FAD60` | `tsl::AsyncValue` → `PJRT_Event` future |
| `TpuClient::CreateProfiledFuture` | `0xF7FAE80` | profiled completion future |
| `TpuClient::CreateErrorEvent` | `0xF808420` | `tpu::TpuEvent` set to error status |
| `TpuClient::CreateDeviceEventSet` | `0xF813D40` | mint wait/define event sets |

> **NOTE —** the compile, execute, buffer, and event surfaces above are documented in depth on their owning pages ([Executable & Execution](executable-execution.md), [Buffer & Memory](buffer-and-memory.md), [Events & Async](events-and-async.md)). They appear here only to show what the *adapter client* exposes; this page owns their wiring into the SE→PJRT bridge, not their slot-level details.

---

## TpuDevice — Wrapping tpu::System Under Each PjRtDevice

### Purpose

`xla::TpuDevice` is the `PjRtDevice` the C-ABI device slots wrap. It is the object that, in the upstream SE model, *would* have held a `StreamExecutor` and a stream pool. Here it holds no executor: it points at the *shared* `tpu::System`, names the one physical core it maps to, and carries a launch-throttle semaphore. This is the crux of the "SE → PJRT adapter" claim — the device-level binding is to an async runtime, not to a stream-based executor.

### What a TpuDevice Holds

The `TpuDevice` object is `0x1E0` (480) bytes (the `operator new(0x1E0)` in the enumeration loop). Its members, recovered from the ctor signature (`0xF7FDC40`) and `SetClient` (`0xF7FE520`):

| Member | Source | Role |
|---|---|---|
| `tpu::System*` | ctor arg 4 | The **shared** device runtime — same pointer in every `TpuDevice` |
| `tpu::TpuCoreLocation` | from the enumeration loop's `core_loc` | The physical core this PjRt device maps to |
| `xla::TpuDeviceDescription` | ctor arg 1 | Static identity: id/process/kind/attributes/memory |
| `xla::TpuClient*` | `SetClient` arg 1 | Back-pointer to the owning client |
| `xla::AsyncWorkRunner*` | `SetClient` arg 2 | Host scheduler for this device's continuations |
| `xla::Semaphore` | constructed; fenced by `ExecutablesStart`/`Complete` | In-flight-computation throttle (bounded by `max_inflight_computations`) |

The `tpu::System*` being *identical* across all devices is the structural difference from SE. In upstream XLA, `device[i]->local_device_state()->executor()` returns a distinct `StreamExecutor` per device. Here, `device[i]` and `device[j]` share one `tpu::System`; what distinguishes them is the `TpuCoreLocation`, which `tpu::System::Execute` and `LoadProgram` consume to target the right core.

### The SetClient Back-Edge

```c
function TpuDevice::SetClient(client, work_runner):      // 0xF7FE520
    this->client       = client                          // back-pointer used by C-ABI device→client lookups
    this->work_runner  = work_runner                     // the client's AsyncWorkRunner (host scheduler)
```

This runs once per device immediately after the `TpuClient` is constructed. It is the only way a `TpuDevice` learns its scheduler. The `client()` accessor that the C-ABI `PJRT_Device` slots use to translate an implementation device back to its wrapper (via the client's `device_map`) reads this field — so it is invalid before `SetClient`, which is why the construction order in `GetTpuPjRtClientInternal` (build client, *then* loop `SetClient`) is load-critical.

### The Launch Throttle — The Closest Thing to a Stream

`TpuDevice` carries a per-device `xla::Semaphore` bounded by `max_inflight_computations` (default 1, from the config). `ExecutablesStart` (`0xF800300`) acquires it before a launch; `ExecutablesComplete` (`0xF800740`) releases it when the launch's completion event fires. This is the functional stand-in for an SE stream's depth limit: it is what stops the host from queueing unboundedly many executions ahead of the device. It is *not* a FIFO — ordering between launches is enforced by the `TpuEventIssuer` sequence points, not by the semaphore. The semaphore only bounds concurrency.

> **QUIRK —** with `max_inflight_computations == 1` (the default), a single `TpuDevice` behaves much like a single SE stream: at most one execution in flight, the next blocked until the previous completes. But the resemblance is shallow — raising the knob makes the device accept multiple concurrent launches with no FIFO ordering between them, which an SE stream could never do. A reimplementer must treat the throttle as a concurrency cap, not as a serialising queue.

### Device Memory Spaces

Each `TpuDevice` attaches three memory spaces (the per-device `AttachMemorySpace` calls):

- `xla::TpuHbmMemorySpace` (kind `"tpu_hbm"`) — device HBM.
- `xla::PinnedHostMemorySpace` / `xla::UnpinnedHostMemorySpace` — host staging.

The canonical kind constants are `xla::kBuiltinTpuMemorySpaces` / `xla::kTpuHbmMemorySpaceKind`. The slot-level memory accessors and the buffer objects that live in these spaces are owned by [Buffer & Memory](buffer-and-memory.md).

### Function Map

| Function | Addr | Role |
|---|---|---|
| `TpuDevice::TpuDevice` (ctor) | `0xF7FDC40` | Build device from desc + name + ordinal + `tpu::System*` |
| `TpuDevice::SetClient` | `0xF7FE520` | Install client back-pointer + `AsyncWorkRunner` |
| `TpuDevice::ExecutablesStart` | `0xF800300` | Acquire the in-flight semaphore before launch |
| `TpuDevice::ExecutablesComplete` | `0xF800740` | Release the in-flight semaphore on completion |

> **NOTE —** the `TpuDeviceDescription` (vtable `0x21787AC0`, typeinfo `0x21787C38`) is the PjRt device description carrying id / process_index / device_kind / Attributes / memory. The SE-flavoured `PjRtStreamExecutorDeviceDescription` (vtable `0x2177D950`, typeinfo `0x2177D9B0`) is GPU-only dead code in this binary — its presence is the *only* residual SE-PjRt symbol, and it is never reached on the TPU path. Do not confuse the two when reading the binary.

---

## The Runtime Backing — tpu::System as the StreamExecutor Analogue

### Purpose

`tpu::System` (`Initialize` @ `0x1D0AE420`) is the object the whole adapter sits on — the TFRT-native TPU device runtime. It is what the page title calls "StreamExecutor," mapped forward: it is the surface that offers launch, transfer, allocate, and event primitives, but async-value-shaped instead of stream-shaped. One `tpu::System` is shared by every `TpuClient` and every `TpuDevice` in the process (it is reached through the `TpuStatesManager` / `TpuSystemState` singleton built in `GetTpuPjRtClient`).

### The API Surface (the SE-Stream-equivalent operations)

| Operation | VA | SE equivalent |
|---|---|---|
| `Initialize(shared_ptr<TpuTopology>, InitOptions, ConcurrentWorkQueue*)` | `0x1D0AE420` | `StreamExecutor::Init` |
| `Execute(AsyncValueRef<ProgramHandle>, ExecuteOptions, Span inputs, outputs, wait, define)` | `0x1D0B33E0` | `Stream::ThenLaunch` |
| `LoadProgram(TpuCoreLocation, shared_ptr<TpuCoreProgram>)` | `0x1D0B2240` | `StreamExecutor::GetKernel` (load once) |
| `TransferToDevice(AsyncValueRef<TpuBuffer>, Span<uint8>, opt<TpuSyncFlagOnChip>)` | `0x1D0AFA20` | `Stream::ThenMemcpy` (H2D) |
| `TransferFromDevice(Span<uint8>, AsyncValueRef<TpuBuffer>, opt<…>)` | `0x1D0B0160` | `Stream::ThenMemcpy` (D2H) |
| `AllocateHostBuffer` / `MakeTpuHostBuffer` | `0x1D0AF180` / `0x1D0AF660` | host-pinned alloc |
| `EnqueueInfeed` / `DequeueOutfeed` | `0x1D0B5D00` / `0x1D0B5F00` | infeed/outfeed |

`Execute` takes the `TpuCoreLocation` (indirectly, via the `ProgramHandle` loaded for that core) — this is how one shared `tpu::System` services many `TpuDevice`s without per-device executors. The internal hardware command-stream / ring layout behind `Execute` is private to `tpu::System::Impl` and the TPU driver core; it was not traced (it is a separate concern from this bridge layer).

> **NOTE —** the device-side ordering primitive is `tpu::TpuEventIssuer` (`AddDepsNoReserve`, `AggregateDeps`, `RunWhenDepsReady`, `NextSequencePoint`, `FulfillArgs`, `ChainScope`). It is the sequence-point engine that orders `Execute` / `Transfer` / `Allocate` against each other — the functional replacement for an SE per-stream FIFO plus cross-stream `Event`s, expressed as a DAG of `tpu::TpuEvent` (each a `tsl::AsyncValueRef`). Its exact sequencing algorithm was not byte-traced. The host-side scheduler that drives dependent continuations is `xla::ThreadPoolAsyncWorkRunner` (`AsyncWorkRunner::ExecuteWhenReady`), fed by the `HostContext`'s `tfrt::ConcurrentWorkQueue`. Both are reached through `TpuClient::async_work_runner()`.

---

## Considerations for a Reimplementer

- **Do not build a `LocalDeviceState`.** The single most likely mistake is to reproduce upstream XLA's per-device `StreamExecutor` + stream pool. The TPU adapter has none. Build one shared `tpu::System`, hand its pointer to every `TpuDevice`, and distinguish devices by `TpuCoreLocation` alone.
- **Singletons are process-wide, not per-client.** Both the `HostContext` (via `CreateDefaultHostContext::kInstance`) and the `tpu::System` (via `TpuStatesManager`) are `__cxa_guard` / mutex one-shots. A second client in the process must reuse them, not rebuild them. The `MaybeOwningDeleter` on `TpuSystemState` encodes the borrow.
- **The device↔client wiring is two-phase.** Construct the client with the device vector, then loop `SetClient` to install the back-pointer and the `AsyncWorkRunner`. Devices are not fully usable until that loop runs.
- **Concurrency comes from a semaphore, ordering from an event DAG.** The per-device `Semaphore` caps in-flight launches; `TpuEventIssuer` sequence points order them. These are orthogonal — conflating the throttle with a FIFO will produce wrong ordering at `max_inflight_computations > 1`.
- **The legacy SE stack is a decoy.** `TpuExecutor` / `TpuStream` / `TpuTransferManager` exist in the same binary but serve `LocalClient` / TF kernels, never PJRT. A reimplementer of the PJRT path should ignore every `ExecutorApiFn` / `SE_*` symbol.

---

## Related Components

| Component | Relationship |
|---|---|
| `PJRT_Client_Create` (slot 15) | Calls `GetTpuPjRtClient`; owns the option parse and the C-ABI wrapper above this bridge |
| `xla::CommonPjRtClient` | The async-value framework `TpuClient` derives from; supplies buffer/executable/event plumbing |
| `MegaScalePjRtClient` / `TfPjRtClient` | Optional decorators stacked on the bare `TpuClient` by `PJRT_Client_Create` |
| `tpu::System` | The shared device runtime every `TpuDevice` points at; the SE-`StreamExecutor` analogue |
| `tpu::TpuEventIssuer` | Device-side sequence-point engine; replaces SE per-stream FIFO + events |
| Legacy SE `TpuPlatform` | The parallel, non-PJRT device layer; never referenced by `TpuClient` |

## Cross-References

- [Client, Device & Topology](client-and-device.md) — owns `PJRT_Client_Create`, the option-kv ingest, and the 208-byte `PJRT_Client` wrapper that boxes the `TpuClient` this page builds
- [StreamExecutor & Host Interpreter](stream-executor-host-interpreter.md) — the legacy SE `TpuPlatform` registration and host-interpreter path that runs parallel to (never under) this adapter
- [Executable & Execution](executable-execution.md) — `PJRT_LoadedExecutable_Execute` and the `TpuExecutable` / `TpuLoadedExecutable` / `TpuRawLoadedExecutable` framework the adapter client loads programs into
- [Execute Async On Stream](../runtime/execute-async-on-stream.md) — the per-execution path down to `tpu::System::Execute`, the runtime side of the launch primitive this page maps from `Stream::ThenLaunch`
- [Buffer & Memory](buffer-and-memory.md) — `TpuRawBuffer` over `tpu::TpuBuffer`, the memory spaces each `TpuDevice` attaches, and the allocator glue
- [Events & Async](events-and-async.md) — `PJRT_Event` ↔ `tsl::Future<void>`, the `TpuTrackedDeviceEvent` / `TpuEventIssuer` model this page references for ordering
- [Overview](overview.md) — the PJRT plugin entry, extension chain, and `GetPjrtApi` population
