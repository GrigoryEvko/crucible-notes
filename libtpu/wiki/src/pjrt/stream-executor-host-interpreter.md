# StreamExecutor Platform & Executor Model

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). `.text` VMA equals file offset. Other versions will differ.*

## Abstract

`stream_executor` is the **legacy device-abstraction layer** that XLA sits on, the same role CUDA's driver API plays for a CUDA program: a device-agnostic class family — `Platform`, `StreamExecutor`, `Stream`, `Event`, `DeviceDescription` — that the XLA service and the PjRtClient use to enumerate devices, mint per-device executors, allocate device memory, and launch ordered work. It predates PJRT; PJRT is the public C ABI layered *on top* of it (see [PJRT Overview](overview.md)). This page owns the **platform-registration handshake and the SE object model**: how the TPU and host backends register a `Platform` into the global registry, how a `Platform` hands out per-ordinal `StreamExecutor`s through an `ExecutorCache`, and what the concrete host-side SE objects (`HostExecutor`, `HostStream`, host `Event`) actually look like in memory. It deliberately does **not** re-document the per-execution enqueue path, nor the cross-stream wait model — those live elsewhere (see scope note below).

Two backends are linked. The **TPU backend** (`tensorflow::tpu::TpuPlatform` + `stream_executor::tpu::TpuExecutor` + `TpuStream`) is a thin C-ABI shim: every SE call forwards through a singleton `TfTpu_ExecutorApiFn` function table (resolved by `stream_executor::tpu::ExecutorApiFn()`) into libtpu's TPU driver core, and the SE objects hold only opaque `SE_StreamExecutor*` / `SE_Stream*` handles. The **host/CPU backend** (`stream_executor::host::HostPlatform` + `HostExecutor` + `HostStream`) is a real in-binary implementation built on a `tsl::thread::ThreadPool` and `absl::Notification`; it is the *synchronous* variant (no worker queue), used for host-memory staging and trivial host ops. There is **no** `InterpreterExecutor` — XLA's standalone interpreter device is not linked; the host-interpreter role is filled by `xla::HloEvaluator` (compile-time) and the CPU thunk runtime, neither of which is an SE `Platform`.

The page proceeds top-down: the registry (`PlatformManager`), the registration entry that installs `TpuPlatform`, the `Platform` → `ExecutorCache` → `StreamExecutor` minting path, the three-level class hierarchy, and finally the host SE object model (executor, stream, event) at byte level. Each `Platform`/`Executor` unit shares the same `### Purpose` / `### Entry Point` / `### Algorithm` / `### Function Map` vocabulary.

For reimplementation, the contract is:

- **The registry + registration handshake** — a process-global `PlatformManager` singleton (anon-namespace `Impl`, `__cxa_guard`-built), and `RegisterTpuPlatform` which `new`s a `0x98`-byte `TpuPlatform`, stores it in a global, registers it, and CHECKs success — guarded by a once-flag and gated on `IsStreamExecutorEnabled`.
- **The `Platform::ExecutorForDevice` → `ExecutorCache::GetOrCreate` → factory path** — how a per-ordinal `StreamExecutor` is cached in a `FlatHashMap<int, unique_ptr<StreamExecutor>>` behind an `absl::Mutex` and built only on a cache miss.
- **The 3-level class hierarchy** — `StreamExecutor` (abstract) → `StreamExecutorCommon` (holds `const Platform*`) → `{HostExecutor, TpuExecutor}`, with vtable addresses and object sizes.
- **The host SE object model** — `HostExecutor` (thread pool), `HostStream` (`0x80`-byte, `StreamCommon`-derived, synchronous), and host `Event` (`absl::Notification`-backed, two-object split) at byte-level layout.

| | |
|---|---|
| **Registry** | `stream_executor::PlatformManager` — singleton `Impl` @ anon-ns, `__cxa_guard`-built, `0x48`-byte (`absl::Mutex` + two maps) |
| **Register entry (TPU)** | `tensorflow::tpu::RegisterTpuPlatform @ 0xe99a3a0` → CHECK @ `tpu_platform.cc:178` |
| **`PlatformManager::RegisterPlatform`** | `0x1d0fe120` — CHECK `platform != nullptr` @ `platform_manager.cc:93` |
| **TPU platform object** | `tensorflow::tpu::TpuPlatform`, `new(0x98)`, global `tpu_registered_platform` |
| **Host platform object** | `stream_executor::host::HostPlatform` (ctor `0xfe6d380`) |
| **Executor cache** | `stream_executor::ExecutorCache::GetOrCreate @ 0x1d0fd2e0` — `FlatHashMap<int, unique_ptr<SE>>` + `absl::Mutex`, log @ `executor_cache.cc:44` |
| **TpuExecutor** | `new(0x48)`, vtable `off_21612AC8`, `: StreamExecutorCommon`, `SE_StreamExecutor*` @ `+0x38`, ordinal @ `+0x40` |
| **HostExecutor** | `Init @ 0xfe6d780` — `tsl::thread::ThreadPool("host-executor", NumSchedulableCPUs)` |
| **HostStream** | ctor `0xfe6ec80`, `new(0x80)`, vtable `off_217B0228`, `: StreamCommon` |
| **Host Event** | `CreateEvent @ 0xfe6d9e0` — `new(0x18)` wrapper (`off_217B0138`) + `new(0x28)` event (`off_215FC128`, `Notification` @ `+24`) |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

> **Scope —** the *per-execution* enqueue (`ExecuteAsyncOnStream` / `LoadProgramAndEnqueueToStream`) is owned by [ExecuteAsyncOnStream](../runtime/execute-async-on-stream.md) and [LoadProgramAndEnqueueToStream](../runtime/load-program-enqueue.md). The *cross-stream wait/dependency* model (`WaitFor` / `RecordEvent`, the FIFO ordering contract, the compute/transfer stream split) is owned by [Stream Semantics & Dependencies](../runtime/stream-semantics.md). The SE → PJRT bridge (`TpuClient` / CommonPjRt) is owned by [StreamExecutor → PJRT Adapter](stream-executor-pjrt-adapter.md). This page is the *platform-registration + SE object model* layer only; it links those rather than restating them.

---

## 1. The Platform Registry — `PlatformManager`

### Purpose

`stream_executor::PlatformManager` is the process-global registry that maps a *platform name* and a *platform id* to a `Platform*`. XLA's device-enumeration code asks it for "the TPU platform" or "the host platform" by name/id and gets back the singleton `Platform` object that mints executors. There is exactly one `PlatformManager` per process, lazily constructed.

> **Note —** older XLA called this class `MultiPlatformManager`. No `MultiPlatformManager` symbol exists in this binary; upstream XLA renamed `MultiPlatformManager` → `PlatformManager`, dropping the `Multi` prefix, and this build carries only the new name. A reimplementer should drive off `stream_executor::PlatformManager`.

### Object Layout

The registry state is a single anon-namespace singleton — `stream_executor::(anonymous namespace)::Impl()::impl` — a `0x48`-byte object built under a `__cxa_guard`. Every public `PlatformManager` static method begins by materialising it. The layout, read identically from `RegisterPlatform` (`0x1d0fe120`) and `PlatformWithName` (`0x1d0fe5c0`):

```c
// Impl(): the PlatformManager singleton                       (anon-ns, __cxa_guard-built)
struct Impl {                       // operator new(0x48)
    absl::Mutex  mu;                // +0x00  guard (init {0, 1, 0})
    FlatHashMap  id_map;            // +0x10  PlatformId -> Platform*
    FlatHashMap  name_map;          // +0x28  name      -> Platform*   (XMM-zeroed at +0x28)
    // ...
};

// every accessor begins with:
function PlatformManager_AnyAccessor():
    if (!guard_for(impl)):                            // __cxa_guard_acquire
        impl = new Impl();                            // zero-init both maps under the guard
        guard_release();
    ...
```

### Function Map

| Function | Address | Role |
|---|---|---|
| `PlatformManager::RegisterPlatform` | `0x1d0fe120` | Install a `unique_ptr<Platform>`; CHECK `platform != nullptr` @ `platform_manager.cc:93`; query id via `platform->vtable+24` |
| `PlatformManager::PlatformWithName(string_view)` | `0x1d0fe5c0` | Name lookup |
| `PlatformManager::PlatformWithName(string_view, bool)` | `0x1d0fe820` | Name lookup with init flag |
| `PlatformManager::PlatformWithId(const PlatformIdInfo*)` | `0x1d0fe680` | Id lookup |
| `PlatformManager::PlatformsWithFilter(...)` | `0x1d0ff140` / `0x1d0ff160` | Filtered enumeration |
| `(anon)::PlatformManagerImpl::PlatformWithName` | `0x1d0fe8e0` | The real lookup body behind the static wrappers |
| `(anon)::PlatformManagerImpl::InitializedPlatformNames...` | `0x1d100260` | Enumerate already-initialised platforms (a `std::function` filter) |

> **NOTE —** `RegisterPlatform` reads the platform's id through `(*platform)->vtable[+24]` before inserting — id is a *virtual* property of each `Platform`, not a constructor argument. The TPU id comes from `tensorflow::tpu::GetTpuPlatformId() @ 0x20818ec0`. A reimplementation must expose id as a virtual so the registry can key on it without knowing the concrete type.

---

## 2. Registering the TPU Platform

### Purpose

`tensorflow::tpu::RegisterTpuPlatform()` is the one-time entry that constructs the singleton `TpuPlatform` and installs it in the registry. It runs during libtpu initialisation (driven from the TPU-driver bring-up path, not from a PJRT call). After it returns, `PlatformManager::PlatformWithName("TPU")` resolves and the PjRtClient can mint executors.

### Algorithm

```c
// tensorflow::tpu::RegisterTpuPlatform()                      sub_E99A3A0
function RegisterTpuPlatform():
    apifn = ExecutorApiFn()                                    // resolve TfTpu_ExecutorApiFn singleton
    if (IsStreamExecutorEnabled(apifn)                         // SE backend must be enabled
        && !tpu_platform_registered):                          // function-local once-flag
        p = (TpuPlatform*)operator new(0x98)                   // 152-byte platform object
        TpuPlatform::TpuPlatform(p)                            // ctor  sub_E999960
        tpu_registered_platform = p                            // stash in a process global
        st = PlatformManager::RegisterPlatform(p)              // 0x1d0fe120  (moves the unique_ptr)
        if (st != OkStatus):                                   // st != 1
            LOG(FATAL) "stream_executor::PlatformManager::RegisterPlatform( std::move(platform)) is OK"
                       @ tpu_platform.cc:178                   // brings the process down
        tpu_platform_registered = 1
    return 1                                                    // always returns true
```

Three reimplementation-critical facts. (1) Registration is **gated on `IsStreamExecutorEnabled`** — if the SE backend is disabled the platform is never installed and the function is a no-op returning `true`. (2) The once-flag `tpu_platform_registered` is a function-local static, so repeated calls are idempotent. (3) The success of `RegisterPlatform` is a hard CHECK at `tpu_platform.cc:178` — a registration failure is fatal, not recoverable. The constructed object is `0x98` (152) bytes; the global `tpu_registered_platform` is read back by `TpuPlatform::GetRegisteredPlatform() @ 0xe999aa0`, which is a one-line `return tpu_registered_platform`.

### `TpuPlatform::Initialize`

Distinct from *registration*, `Initialize` (`0xe999ac0`) brings the TPU driver up under the platform. It is pure C-shim forwarding:

```c
// tensorflow::tpu::TpuPlatform::Initialize()                  sub_E999AC0
function TpuPlatform_Initialize(this):
    status_obj = ExecutorApiFn()[+360](this)                  // device/status scratch object
    ExecutorApiFn()[+16](this->se_platform/*this+1*/, status_obj)   // TfTpu init the platform
    if (!ExecutorApiFn()[+408](status_obj)):                  // status not OK?
        code = ExecutorApiFn()[+400](status_obj)              // status code
        msg  = ExecutorApiFn()[+392](status_obj)              // status message
        rep  = absl::Status::MakeRep(code, msg, ..., status_helper.h:38)
    ExecutorApiFn()[+384](status_obj)                          // free the status scratch
    return rep_or_Ok
```

The `+360 / +408 / +400 / +392 / +384` slot pattern — *get a status scratch object, do the op, query ok/code/message, free the scratch* — is the universal "TfTpu C-shim → `absl::Status`" idiom; every TPU SE method (executor `Init`, `Allocate`, stream `WaitFor`) uses it, always anchored to `status_helper.h:38`.

### Function Map

| Function | Address | Role |
|---|---|---|
| `tpu::RegisterTpuPlatform` | `0xe99a3a0` | One-time construct + register; once-flag; fatal CHECK @ `:178` |
| `tpu::GetTpuPlatformId` | `0x20818ec0` | The TPU `PlatformId` the registry keys on |
| `TpuPlatform::TpuPlatform` (C2) | `0xe999960` | Ctor of the `0x98`-byte platform |
| `TpuPlatform::Initialize` | `0xe999ac0` | Driver bring-up via `ExecutorApiFn()+16`; status @ `status_helper.h:38` |
| `TpuPlatform::GetRegisteredPlatform` | `0xe999aa0` | `return tpu_registered_platform` |
| `TpuPlatform::FindExisting(int)` | `0xe99a4a0` | Per-ordinal cached executor lookup (no build) |
| `TpuPlatform::VisibleDeviceCount` | `0xe999d20` | Device count via shim (`ExecutorApiFn()+48`) |
| `TpuPlatform::Insert/Lookup/EraseEvent` | `0xe999fa0`/`0xe99a100`/`0xe99a160` | `Event*` ↔ `SE_Event*` registry (used by `WaitFor`/`RecordEvent`) |
| `TpuPlatform::TpuMemoryLimit` | `0xe99a2c0` | HBM size query |
| `TpuPlatform::GetTopologyPtr` | `0xe999f40` | Device topology handle |

> **NOTE —** `TpuPlatform::Insert/Lookup/EraseEvent` maintain the `Event* → SE_Event*` map that the TPU stream's `WaitFor(Event*)`/`RecordEvent` dereference. The *use* of that map is documented on [Stream Semantics](../runtime/stream-semantics.md); the platform *owns* the map, which is why the methods live on `TpuPlatform`.

---

## 3. Minting Executors — `ExecutorForDevice` → `ExecutorCache`

### Purpose

A `Platform` does not create a fresh `StreamExecutor` per request; it caches one per device ordinal. `Platform::ExecutorForDevice(int ordinal)` returns the cached executor or builds it on first use; `GetUncachedExecutor` is the factory the cache invokes on a miss. This is the only path by which a `StreamExecutor` comes into existence.

### Entry Point

```text
TpuPlatform::ExecutorForDevice(int)            0xe999d40   ── public entry
  └─ ExecutorCache::GetOrCreate(cache@+40, ordinal, factory)   0x1d0fd2e0
       ├─ ExecutorCache::Get(ordinal)          0x1d0fd580   ── fast path: hit
       └─ factory()  ==  $_0 lambda  ─────────► TpuPlatform::GetUncachedExecutor(int)   0xe999da0
                                                  └─ ExecutorApiFn()+32  ── mint SE_StreamExecutor*
                                                  └─ new(0x48) TpuExecutor : StreamExecutorCommon
```

### Algorithm

`ExecutorForDevice` (`0xe999d40`) wraps a `GetUncachedExecutor` call in a `std::function` factory and passes the cache (at `platform+40`) to `GetOrCreate`:

```c
// TpuPlatform::ExecutorForDevice(int ordinal)                 sub_E999D40
function TpuPlatform_ExecutorForDevice(this, ordinal):
    factory = bind(TpuPlatform::GetUncachedExecutor, this, ordinal)   // $_0 lambda
    ExecutorCache::GetOrCreate(this->cache /*this+40*/, ordinal, factory)   // 0x1d0fd2e0
    // returns StatusOr<StreamExecutor*>
```

`ExecutorCache::GetOrCreate` (`0x1d0fd2e0`) is the cache core:

```c
// ExecutorCache::GetOrCreate(int ordinal, factory)            sub_1D0FD2E0
function ExecutorCache_GetOrCreate(cache, ordinal, factory):
    if (ExecutorCache::Get(ordinal) is OK):                    // 0x1d0fd580 — lock-free-ish hit
        return cached                                          // fast path
    VLOG(2) "building executor"  @ executor_cache.cc:44
    se = factory()                                             // run GetUncachedExecutor
    if (!se.ok()): return se.status()
    lock(cache->mu /*ordinal-keyed table mutex*/)
    table = FlatHashMap<int, unique_ptr<StreamExecutor>>(cache+8)
    slot  = table.find_or_prepare_insert(ordinal)              // double-check under lock
    if (slot existed):                                         // raced — keep the existing one
        unlock; drop our `se`; return existing
    table[ordinal] = move(se)                                  // install
    unlock
    return se
```

The cache is a `FlatHashMap<int, unique_ptr<StreamExecutor>>` guarded by an `absl::Mutex`, with a double-checked insert: the `Get` fast path runs first, the factory builds outside the lock, then a `find_or_prepare_insert` under the lock keeps whichever object won a race. The VLOG "building executor" at `executor_cache.cc:44` fires exactly once per ordinal in the common case.

### `GetUncachedExecutor` — the factory

```c
// TpuPlatform::GetUncachedExecutor(int ordinal)               sub_E999DA0
function TpuPlatform_GetUncachedExecutor(this, ordinal):
    status_obj = ExecutorApiFn()[+360](this)                  // status scratch
    se_handle  = ExecutorApiFn()[+32](this->se_platform, ordinal, status_obj)  // mint SE_StreamExecutor*
    if (ExecutorApiFn()[+408](status_obj)):                   // ok?
        exec = (TpuExecutor*)operator new(0x48)               // 72-byte executor
        StreamExecutorCommon::StreamExecutorCommon(exec, this/*Platform*/)
        exec->vtable      = off_21612AC8
        exec->platform    = this        // +0x30 (this+6)
        exec->se_handle   = se_handle   // +0x38 (this+7)  opaque SE_StreamExecutor*
        exec->ordinal     = ordinal     // +0x40 (this+16, 32-bit)
        return StatusOr::ok(exec)
    else:                                                      // mint failed
        return harvest_status(status_obj) @ status_helper.h:38
    // (status scratch freed via ExecutorApiFn()+384)
```

The `TpuExecutor` is a `0x48` (72) byte object: the `StreamExecutorCommon` base (which holds the `const Platform*`), the vtable `off_21612AC8`, a *second* `Platform*` copy at `+0x30`, the opaque driver handle `SE_StreamExecutor*` at `+0x38`, and the device ordinal at `+0x40`. It owns no device state of its own — every method forwards through `ExecutorApiFn()` against the `+0x38` handle.

### Function Map

| Function | Address | Role |
|---|---|---|
| `TpuPlatform::ExecutorForDevice` | `0xe999d40` | Public mint-or-cache entry; cache @ `platform+40` |
| `TpuPlatform::GetUncachedExecutor` | `0xe999da0` | Factory: `ExecutorApiFn()+32` mint + `new(0x48)` `TpuExecutor` |
| `ExecutorCache::GetOrCreate` | `0x1d0fd2e0` | Double-checked per-ordinal cache; log @ `executor_cache.cc:44` |
| `ExecutorCache::Get` | `0x1d0fd580` | Fast-path cached lookup |
| `TpuExecutor::Init` | `0xe996980` | Per-executor init via shim |
| `TpuExecutor::CreateStream` | `0xe996ca0` | Mint a `TpuStream` |
| `TpuExecutor::CreateEvent` | `0xe996e60` | Mint a `TpuEvent` |
| `TpuExecutor::Allocate` | `0xe996fa0` | Device memory alloc via shim |

---

## 4. The Class Hierarchy

### The three levels

```text
stream_executor::StreamExecutor                 (abstract base, vtable off_21FDAD98)
  └ stream_executor::StreamExecutorCommon        (concrete intermediate; holds const Platform*)
      ├ stream_executor::host::HostExecutor       (CPU thread pool; in-binary)
      └ stream_executor::tpu::TpuExecutor          (C-shim over TfTpu_ExecutorApiFn)

stream_executor::Stream                          (abstract base)
  └ stream_executor::StreamCommon                 (holds parent StreamExecutor* @ +0x48)
      ├ stream_executor::host::HostStream          (synchronous, in-binary)
      └ tensorflow::tpu::TpuStream                 (: TpuStreamInterface, C-shim)
```

`StreamExecutorCommon` is the device-agnostic middle layer: it holds the `const Platform*` and implements `GetPlatform`, `GetDeviceDescription`, `Activate`, `GetMemoryLimitBytes` — everything that does not require touching a device. The two concrete executors differ only in *where* their primitive operations land: `HostExecutor` on a `tsl::thread::ThreadPool`, `TpuExecutor` on the `TfTpu_ExecutorApiFn` table. The `Stream` hierarchy mirrors this exactly, with `StreamCommon` holding the parent back-pointer (documented in [Stream Semantics §1](../runtime/stream-semantics.md)).

### Base / common layer

| Class | Role | Key symbols |
|---|---|---|
| `StreamExecutor` | Abstract device executor; vtable `off_21FDAD98` | ctor `0x208193e0`, D2 `0x20819440` |
| `StreamExecutorCommon` | Concrete intermediate; holds `const Platform*` | ctor `0x1d0f03e0`, `GetDeviceDescription 0x1d0f04a0` |
| `Platform` | Abstract platform; `FindExisting(int)` | `FindExisting 0xe718b60` |
| `ExecutorCache` | Per-ordinal executor cache | `GetOrCreate 0x1d0fd2e0`, `Get 0x1d0fd580` |
| `DeviceDescription` | Copyable device-info bag | copy-ctor `0xe6b5ee0` |
| `RuntimeAbiVersionManager` | Singleton ABI guard | `GetInstance 0xe6b8040` |

> **QUIRK —** there is **no `stream_executor::interpreter::InterpreterExecutor`** in libtpu. The XLA standalone interpreter device (`xla/backends/interpreter/`) is not linked. The only "interpreter" symbols are ANTLR parser runtime and `mlir::interpreter::*`, both unrelated to device execution. A reimplementer expecting an interpreter `Platform` to fall back to will not find one — the host-interpreter role is split between compile-time `xla::HloEvaluator` (constant folding) and the runtime CPU thunk backend, **neither of which is an SE `Platform`**. When the compiler cannot fold something, the node is simply left in the graph for the real backend.

---

## 5. The Host SE Object Model

### Purpose

The host backend (`stream_executor::host::*`) is the only SE backend with a *real* in-binary implementation rather than a C shim. It exists for host-memory staging, trivial host ops, and as the executor a CPU-only `PjRtClient` would drive. It is the **synchronous** variant: no worker thread, no task queue — every operation runs inline on the calling thread, so the stream is trivially always drained.

### `HostPlatform` and `HostExecutor::Init`

`HostPlatform` (ctor `0xfe6d380`) mirrors `TpuPlatform`'s `ExecutorForDevice` / `GetUncachedExecutor` shape (`0xfe6d580` / `0xfe6d5e0`) but builds a `HostExecutor` instead of a shim wrapper. `HostExecutor::Init` (`0xfe6d780`) is where the host's only real resource — the thread pool — is created:

```c
// stream_executor::host::HostExecutor::Init()                 sub_FE6D780
function HostExecutor_Init(this):
    env = tsl::Env::Default()
    n   = tsl::port::NumSchedulableCPUs()                      // one thread per schedulable CPU
    pool = operator new(0x40)                                  // tsl::thread::ThreadPool
    pool->vtable = off_217B00E8
    construct_at<ThreadPool>(pool+3, env, "host-executor", n)  // named pool
    this->thread_pool      = pool + 3   // +0x38 (this+7)
    old = this->pool_owner              // +0x40 (this+8)
    this->pool_owner       = pool       // refcounted; release the old one
    if (old && --old.refcount == 0): destroy(old)
    return OkStatus
```

`Allocate`/`Deallocate` are plain `operator new` / `free` (host memory is just process memory, routed to tcmalloc); `HostMemoryAllocate` (`0xfe6e0c0`) wraps `new` with an `AnyInvocable` deleter so the returned `MemoryAllocation` frees itself.

### `HostStream` — the synchronous stream object

`HostStream::HostStream` (`0xfe6ec80`) is a two-line ctor: chain to `StreamCommon`, set the vtable.

```c
// stream_executor::host::HostStream::HostStream(StreamExecutor*)   sub_FE6EC80
function HostStream_ctor(this, parent):
    StreamCommon::StreamCommon(this, parent)    // 0x1d100280 — sets parent @ +0x48, base layout
    this->vtable = off_217B0228                 // 0x80-byte object
```

It is a `0x80` (128) byte object (allocated `new(0x80)` by `HostExecutor::CreateStream`). The `StreamCommon` base supplies the parent pointer and the FIFO/sub-stream slots; `HostStream` adds the host-specific notification slot. `CreateStream` (`0xfe6de20`) routes through a pluggable factory and only `new`s a default `HostStream` when none is registered:

```c
// stream_executor::host::HostExecutor::CreateStream(...)      sub_FE6DE20
function HostExecutor_CreateStream(this):
    f = HostStreamFactory::GetFactory()                        // 0xfe6eb40
    if (f):  s = f->CreateStream(this)                         // pluggable override
    else:    s = new(0x80) HostStream(this)                    // default path
    return StatusOr::ok(s)
```

The stream's runtime behaviour is fixed by its synchronous nature: `BlockHostUntilDone` (`0xfe6f000`) is literally `return 1` (everything has already run), and `DoHostCallbackWithStatus` (`0xfe6efe0`) invokes the callback inline on the calling thread. The `WaitFor`/`RecordEvent` surface (no-op `WaitFor(Stream*)`, `Notification`-based `WaitFor(Event*)`/`RecordEvent`) is documented on [Stream Semantics §3](../runtime/stream-semantics.md) — that page owns the wait model.

### Host `Event` — the two-object split

`HostExecutor::CreateEvent` (`0xfe6d9e0`) builds a host event as **two** heap objects:

```c
// stream_executor::host::HostExecutor::CreateEvent()          sub_FE6D9E0
function HostExecutor_CreateEvent(this):
    wrapper = operator new(0x18)                  // outer Event handle
    wrapper->vtable = off_217B0138
    inner   = operator new(0x28)                  // the real event state
    inner->vtable          = off_215FC128
    inner->[+24]           = absl::Notification{}  // single-shot notification
    inner->[+32]           = 0                     // notified flag (byte)
    wrapper[+1]            = &inner[+24]            // ptr to the Notification
    wrapper[+2]            = inner                  // ptr to the inner object (for refcount)
    return StatusOr::ok(wrapper)
```

The split is deliberate: the outer `0x18`-byte wrapper is the `Event` the SE API hands out (vtable `off_217B0138`); the inner `0x28`-byte object (vtable `off_215FC128`) holds the `absl::Notification` at `+24` and a notified-flag byte at `+32`, with a `shared_weak_count` so the inner object can be released exactly once across `WaitFor`/`RecordEvent`. The host event is **single-shot** — recording an already-notified event CHECK-fails at `host_stream.cc:92` (see [Stream Semantics §3](../runtime/stream-semantics.md)).

### Function Map

| Function | Address | Role |
|---|---|---|
| `HostPlatform::HostPlatform` | `0xfe6d380` | Host platform ctor |
| `HostPlatform::ExecutorForDevice` | `0xfe6d580` | Mint-or-cache (mirrors TPU) |
| `HostPlatform::GetUncachedExecutor` | `0xfe6d5e0` | Build a `HostExecutor` |
| `HostExecutor::Init` | `0xfe6d780` | `ThreadPool("host-executor", NumSchedulableCPUs)` |
| `HostExecutor::CreateStream` | `0xfe6de20` | Factory or default `new(0x80)` `HostStream` |
| `HostExecutor::CreateEvent` | `0xfe6d9e0` | Two-object `Notification`-backed event |
| `HostExecutor::HostMemoryAllocate` | `0xfe6e0c0` | `new` + `AnyInvocable` deleter |
| `HostStream::HostStream` | `0xfe6ec80` | `StreamCommon` + vtable `off_217B0228`, `0x80` B |
| `HostStream::BlockHostUntilDone` | `0xfe6f000` | `return 1` (synchronous) |
| `HostStream::DoHostCallbackWithStatus` | `0xfe6efe0` | Inline call on caller thread |
| `HostStreamFactory::GetFactory` | `0xfe6eb40` | Pluggable stream-factory accessor |
| `HostStreamFactory::Register` | `0xfe6ea20` | Install a custom stream factory |

> **NOTE —** `HostStreamFactory::Register` (`0xfe6ea20`) provides a hook to override the default `HostStream`, but no non-default factory registration was found at any traced call site — the `GetFactory` path returns null and `CreateStream` falls through to the default `new HostStream`. Confidence that the override is reachable in this build: LOW. A reimplementer can ignore the factory hook unless a custom host stream is needed.

---

## 6. Considerations for a Reimplementer

- **Registration is a one-time, fatal-on-failure handshake.** Mirror `RegisterTpuPlatform`: gate on the backend-enabled flag, guard with a once-flag, and treat `RegisterPlatform` failure as fatal (the CHECK at `tpu_platform.cc:178`). A silently-failed registration leaves device enumeration broken with no error.
- **Drive off `PlatformManager`, not `MultiPlatformManager`.** The legacy class name is gone from this build (§1 Note). Key the registry on the virtual platform-id.
- **Executors are per-ordinal singletons with a double-checked cache.** Do not build a fresh executor per call. Replicate `ExecutorCache`: fast-path `Get`, build the factory result *outside* the lock, then `find_or_prepare_insert` under the lock and discard the loser of any race.
- **The TPU executor owns no device state.** It is a `0x48`-byte handle holder (`SE_StreamExecutor*` @ `+0x38`, ordinal @ `+0x40`). Every method forwards through `ExecutorApiFn()`; the status idiom is always *scratch / op / ok?-code-msg / free* @ `status_helper.h:38`.
- **The host backend is synchronous by design.** `BlockHostUntilDone` returns `true`, callbacks run inline, host events are single-shot (`host_stream.cc:92` CHECK). If you add an async host worker queue (upstream XLA has one — not linked here), you must change all three. The absence of an async `HostStream` symbol is by inference (no symbol found), so confidence the linked build is purely synchronous: HIGH.
- **There is no interpreter fallback `Platform`.** Do not expect compilation failure to route to an interpreter device. Compile-time evaluation is `xla::HloEvaluator` (constant folding); runtime host execution is CPU thunks; both are outside the SE `Platform` model (§4 QUIRK).

---

## Related Components

| Name | Relationship |
|---|---|
| `stream_executor::PlatformManager` | the process-global registry this page documents (renamed from `MultiPlatformManager`) |
| `tensorflow::tpu::TpuPlatform` | the TPU `Platform`; `RegisterTpuPlatform` installs it, `ExecutorForDevice` mints `TpuExecutor`s |
| `stream_executor::host::HostPlatform` / `HostExecutor` / `HostStream` | the synchronous in-binary host SE object model |
| `stream_executor::ExecutorCache` | the per-ordinal `FlatHashMap<int, unique_ptr<SE>>` cache behind `ExecutorForDevice` |
| `TfTpu_ExecutorApiFn` table | the C-ABI the TPU backend forwards to (`ExecutorApiFn()` slots `+16`/`+32`/`+360`/`+384`/`+392`/`+400`/`+408`) |
| `xla::HloEvaluator` / CPU thunks | the host-*interpreter* roles that are **not** SE `Platform`s (compile-time fold + runtime CPU backend) |

## Cross-References

- [PJRT Overview](overview.md) — the public C ABI layered on top of this SE stack; StreamExecutor is the legacy abstraction PJRT wraps
- [StreamExecutor → PJRT Adapter](stream-executor-pjrt-adapter.md) — the `TpuClient` / CommonPjRt bridge from this SE layer up to PJRT
- [Client and Device](client-and-device.md) — the PjRtClient/device surface that consumes the executors this page mints
- [Stream Semantics & Dependencies](../runtime/stream-semantics.md) — owns the `WaitFor`/`RecordEvent` wait model, the FIFO ordering contract, and the compute/transfer stream split
- [Runtime Overview](../runtime/overview.md) — the execute path from PJRT down to the stream, the frame the per-execution sections sit in
- [ExecuteAsyncOnStream](../runtime/execute-async-on-stream.md) — the per-execution enqueue onto the compute stream (distinct from this page's registration layer)
- [LoadProgramAndEnqueueToStream](../runtime/load-program-enqueue.md) — program load + enqueue, the producer of compute-stream work
