# Runtime & Execution Overview

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, clang/LLVM trunk). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. Other versions will differ.*

## Abstract

The *runtime* layer of libtpu is the execution engine that sits **below the PJRT C API and above the TPU driver core**. It is the libtpu analogue of XLA's StreamExecutor + PjRt execution stack: it takes an already-compiled TPU program plus a vector of argument buffers, marshals those buffers into the shape the device expects, loads the program onto a physical TPU core, pushes a request onto that core's command stream, and tracks completion asynchronously so the caller's `PJRT_Event` resolves when the device finishes. Compilation (HLO → `TpuCoreProgram`) is upstream of this layer; the on-chip queue silicon and the `TfTpu_*ApiFn` driver tables are downstream of it. Everything between "a compiled executable and its input buffers exist" and "the outputs are live in HBM and the completion event has fired" is what these pages reconstruct.

The single most important structural fact — and the one a reimplementer must internalize before reading any sibling page — is that libtpu ships **two parallel execution stacks compiled into the same image**, and they are *front doors*, not run-time alternatives. The **modern PJRT path** is `xla::TpuClient` (derived from `xla::CommonPjRtClient : xla::PjRtClient`) sitting on the TFRT-native device runtime `tpu::System`; it is what every PJRT client (JAX, PyTorch-XLA) drives, it is async-value (`tsl::AsyncValueRef`) native, and it has **zero** references to `stream_executor::Stream`, `TpuExecutor`, or `ExecutorApiFn` anywhere in its code range. The **legacy StreamExecutor path** is `xla::legacy::TpuExecutableInterface` / `xla::jellyfish::DeepseaExecutable` over `stream_executor::tpu::TpuExecutor` + `TpuStream`; it is what `xla::LocalClient` / `xla::Service` and the TF-TPU op kernels drive through the `Tpu*_*` C-ABI. Both bottom out at the same `deepsea` driver core (`asic_sw::driver::deepsea::jxc::Queue::EnqueueRequest`), but they never share a host-side object: the modern path orders work with a `TpuEventIssuer` dependency graph; the legacy path orders work with a `stream_executor::Stream` FIFO. The task framing's "`TpuExecutable::ExecuteAsyncOnStream`" names the *legacy* virtual; the symbol the modern path actually uses (`tpu::System::Execute`) reaches the equivalent enqueue without ever entering `ExecuteAsyncOnStream`.

This page is the **section map** for the runtime layer. It fixes the two-stack architecture, gives the canonical SE-concept → TPU-runtime mapping, lays out the end-to-end execution lifecycle as a single diagram with both spellings side by side, and summarizes each sub-area in one or two sentences with a link to the page that owns the detail. It does **not** reproduce the argument/output marshaling ([execute-async-on-stream.md](execute-async-on-stream.md)), the program-load + enqueue internals ([load-program-enqueue.md](load-program-enqueue.md)), the `WaitFor`/`RecordEvent` dependency model ([stream-semantics.md](stream-semantics.md)), the infeed/outfeed queue mechanism ([infeed-outfeed.md](infeed-outfeed.md)), or the host-side allocator and completion plumbing — each of those is a dedicated sibling.

For orientation, the contract is:

- **The two-stack model** — which front door (`TpuClient`/`tpu::System` vs `TpuExecutableInterface`/`TpuExecutor`) a given client drives, and that they share only the driver core.
- **The execution lifecycle** — the PJRT `Execute` → prepare/launch/raw → load → enqueue → device run → completion → outputs chain, in both spellings, and where the layer boundaries fall.
- **The SE-concept → TPU-runtime mapping** — the table a reader who knows GPU/StreamExecutor uses to translate `StreamExecutor`/`Stream`/`Event`/`DeviceMemoryBase` into `tpu::System`/`TpuEventIssuer`/`TpuEvent`/`TpuSharedMemoryLocation`.
- **The sub-area map** — the eight runtime pages and what each owns, so the reader can navigate by reflex.

| | |
|---|---|
| **Modern PJRT entry** | `PJRT_LoadedExecutable_Execute` (slot 60) @ `0xf869b40` → `CommonPjRtLoadedExecutable::Execute` |
| **Modern device runtime** | `tpu::System` — `Execute` @ `0x1d0b33e0`, `LoadProgram` @ `0x1d0b2240` (`system.cc:1804`) |
| **Legacy SE entry** | `xla::legacy::TpuExecutableInterface::ExecuteAsyncOnStream` @ `0x1342cd20` (3650 B) |
| **Legacy enqueue leaf (vtable+96)** | `xla::jellyfish::DeepseaExecutable::LoadProgramAndEnqueueToStream` @ `0x13426260` (1360 lines) |
| **Legacy command stream** | `deepsea::executor::DeepseaStream::EnqueueRequest` @ `0x1d0e9840` |
| **Shared driver core (both stacks)** | `asic_sw::driver::deepsea::jxc::Queue::EnqueueRequest` @ `0xe7d9be0` (DmaBuffer + completion) |
| **Modern client class** | `xla::TpuClient` : `CommonPjRtClient` : `PjRtClient` (ctor `0xf801980`, vtable `0x2177b598`) |
| **Modern device sequencing** | `tpu::TpuEventIssuer` sequence points (`IssueArgs`/`FulfillArgs`/`AddDepsNoReserve`) |
| **Device-memory identity** | `tpu::TpuSharedMemoryLocation` + `tpu::TpuBuffer` (modern) / `stream_executor::DeviceAddressBase` (legacy) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## 1. The Two Execution Stacks

### Purpose

A reimplementer's first decision is *which* of two device abstractions to model, because the two never converge above the driver. Both are present in this single binary; the choice is dictated by which client API the front-end speaks, not by a run-time flag. This section fixes the split; every sibling page is written against one or both of these stacks.

### The split

```text
                         PJRT C API (140 slots, P-3-10 surface)
                                       │
              ┌────────────────────────┴───────────────────────┐
   MODERN (every PJRT client: JAX, PyTorch-XLA)        LEGACY (LocalClient / Service / TF-TPU op kernels)
              │                                                 │
   xla::TpuClient : CommonPjRtClient : PjRtClient       xla::legacy::TpuExecutableInterface
   (ctor 0xf801980, vtable 0x2177b598)                  └─ xla::jellyfish::DeepseaExecutable
              │                                                 │   (ExecuteAsyncOnStream 0x1342cd20)
   tpu::System  (TFRT async-value native)               stream_executor::tpu::TpuExecutor + TpuStream
   ├─ LoadProgram 0x1d0b2240                            ├─ over TfTpu_ExecutorApiFn C-ABI table
   ├─ Execute     0x1d0b33e0                            └─ DeepseaExecutable::LoadProgramAndEnqueue… 0x13426260
   └─ TpuEventIssuer sequence points                          └─ DeepseaStream::EnqueueRequest 0x1d0e9840
              │                                                 │
              └──────────────────────┬──────────────────────────┘
                                     ▼
                    asic_sw::driver::deepsea::jxc::Queue::EnqueueRequest  0xe7d9be0
                            (the one shared TPU driver core; DmaBuffer + completion)
```

The `legacy::` namespace on `TpuExecutableInterface` (mangled `_ZN3xla6legacy22TpuExecutableInterface…`) is the binary's own label and is the clearest single signal that the `ExecuteAsyncOnStream` chain is the deprecated StreamExecutor model. The modern path's TFRT shape is equally explicit: `tpu::System::Execute` takes a `tsl::AsyncValueRef<tpu::ProgramHandle>` and `absl::Span`s of `AsyncValueRef`-wrapped buffers and events, not a `Stream*`.

> **GOTCHA —** the two stacks are not layered, and PJRT does **not** wrap `ExecuteAsyncOnStream`. The modern `TpuClient`/`tpu::System` path has zero references to `ExecuteAsyncOnStream`, `TpuExecutor`, or `ExecutorApiFn` (HIGH — byte-confirmed by the absence of those symbols across the entire `TpuClient`/`TpuDevice`/`TpuRawBuffer`/`TpuLoadedExecutable` code range). A reimplementer who models PJRT execute as a thin wrapper over the StreamExecutor `Executable::ExecuteAsyncOnStream` virtual will reproduce the wrong stack entirely. Model `tpu::System::Execute` for the PJRT front door; model `ExecuteAsyncOnStream` only if reimplementing the `LocalClient`/TF-TPU-op-kernel path.

> **NOTE —** neither the host interpreter (`xla::HloEvaluator`) nor the CPU thunk backend (`xla::cpu::*Thunk`) is on either execute hot path. `HloEvaluator` runs at *compile* time inside the HLO simplifier (`HloConstantFolding`); the CPU thunks run only when HLO is placed on the host CPU device. There is no `InterpreterExecutor` device in this binary at all. The "host" mechanisms that *are* part of runtime execution are infeed/outfeed streaming ([infeed-outfeed.md](infeed-outfeed.md)) and host callbacks ([host-callbacks.md](host-callbacks.md)).

---

## 2. The Execution Lifecycle

### Purpose

This is the spine the whole section hangs on: one logical lifecycle — *load a program, marshal arguments, enqueue, run on device, signal completion, surface outputs* — realized two ways. The diagram places the two spellings side by side so a reader can see exactly where they diverge (host-side abstraction) and where they reconverge (the driver core). Each labelled stage links to the page that owns it.

### End-to-end, both spellings

```text
                       a compiled TpuExecutable + input PjRtBuffers exist
                                            │
   ── MODERN (PJRT / TpuClient over tpu::System) ──        ── LEGACY (StreamExecutor / LocalClient) ──
   PJRT_LoadedExecutable_Execute (slot 60, 0xf869b40)      xla::LocalExecutable::RunAsync (0x1084d140)
     └ CommonPjRtLoadedExecutable::Execute                   └ Executable::ExecuteAsyncOnStreamWrapper (0x1dad98a0)
        ├ ExecutePrepare  (0xf920be0)  ── pin inputs,            └ [vtable+24]
        │   alloc outputs, donation/aliasing                        TpuExecutableInterface::ExecuteAsyncOnStream (0x1342cd20)
        └ ExecuteLaunch   (0xf921f00)                                ├ marshal ExecutionInput → DeviceAddressBase[]
           └ TpuRawLoadedExecutable::Execute (0xf80f580)             ├ AllocateOutputMemoryWithInputReuse (0x1342ba00)
              └ TpuExecutableLoadState::ExecuteLaunchRaw (0xf8109a0) └ [vtable+96]
                 ├ (once) LoadInternal (0xf80c1c0)                       DeepseaExecutable::LoadProgramAndEnqueueToStream
                 │   └ tpu::System::LoadProgram (0x1d0b2240) ────────►       (0x13426260)
                 │                                                            ├ DeepseaExecutor::LoadProgram → TpuCoreProgramHandle
                 └ tpu::System::Execute (0x1d0b33e0) ──────────────►         └ DeepseaStream::EnqueueRequest (0x1d0e9840)
                    └ TpuEventIssuer::IssueArgs / FulfillArgs                        │
                       (wait/define event sequence points)                          │
                                            └──────────────┬───────────────────────┘
                                                           ▼
                                  jxc::Queue::EnqueueRequest (0xe7d9be0) — device runs
                                                           │
                                                 device completion signal
                                                           │
   define events fulfilled (TpuEventIssuer::FulfillArgs)   request completion callback fires
   → PJRT_Event (tsl::Future<void>) resolves               → ExecutionOutput status stamped
                                                           │
                                          output buffers live in HBM (donated/aliased honored)
```

The two paths reconverge at exactly one point — `jxc::Queue::EnqueueRequest` — and diverge at exactly one axis above it: the host-side *ordering primitive*. Legacy uses a `stream_executor::Stream` FIFO (`DeepseaRequestQueue`); modern uses a `TpuEventIssuer` dependency DAG of `TpuEvent`s. Everything else (program load, buffer binding, output construction, completion) has a one-to-one counterpart across the two columns.

> **QUIRK —** the modern path *prepares* a launch before its program is physically resident. `tpu::System::LoadProgram` returns immediately with an *unresolved* `AsyncValueRef<tpu::ProgramHandle>`; the DMA-to-core completes asynchronously, and `tpu::System::Execute` registers the launch with `TpuEventIssuer` as *depending on* that async value. The sequence-point engine guarantees the program is loaded before the request executes, so the host never blocks on the load. The legacy path, by contrast, loads synchronously inside `DeepseaExecutable::LoadProgramAndEnqueueToStream` before building the `Request`. See [load-program-enqueue.md](load-program-enqueue.md) §3.

### The layer boundaries

| Stage | Owner page | What it covers |
|---|---|---|
| Argument marshal + output alloc (legacy) | [execute-async-on-stream.md](execute-async-on-stream.md) | `ExecutionInput` → flat `DeviceAddressBase[]`; `ScopedShapedBuffer` allocation; donation/aliasing; the vtable+24 → vtable+96 dispatch |
| Program load + enqueue (both) | [load-program-enqueue.md](load-program-enqueue.md) | `LoadProgram` (sync legacy / async modern), per-core Megacore fan-out, `Request` build, `EnqueueRequest`, unload bookkeeping |
| Ordering / dependencies (both) | [stream-semantics.md](stream-semantics.md) | The `Stream` FIFO invariant, `WaitFor`/`RecordEvent`, `TpuEventIssuer` sequence points, the compute/H2D/D2H stream split |
| Completion → `PJRT_Event` (both) | [completion-loop.md](completion-loop.md) | define-event fulfillment, `AsyncTrackingEvent`, the host-side resolution of `tsl::Future<void>`, deferred resource teardown |

---

## 3. The SE-Concept → TPU-Runtime Mapping

### Purpose

A reader who knows GPU/StreamExecutor needs a translation table to read the modern path, because the modern path keeps the *roles* of `StreamExecutor`/`Stream`/`Event`/`DeviceMemoryBase` but realizes none of them with a StreamExecutor type. The title's "StreamExecutor → PJRT adapter" is precisely this mapping: the SE abstraction maps onto `tpu::System`, not onto a SE wrapper.

### The map

| StreamExecutor / PjRt concept | libtpu modern realization | libtpu legacy realization |
|---|---|---|
| `StreamExecutor` (device executor) | `tpu::System` (`Initialize` `0x1d0ae420`) | `stream_executor::tpu::TpuExecutor` (over `ExecutorApiFn`) |
| `PjRtClient` | `xla::TpuClient` (ctor `0xf801980`) | `xla::LocalClient` / `xla::Service` |
| `PjRtDevice` / SE device | `xla::TpuDevice` (`tpu::System*` + `TpuCoreLocation` + `Semaphore`) | per-ordinal `TpuExecutor` |
| `Stream::ThenLaunch` | `tpu::System::Execute` @ `0x1d0b33e0` | `DeepseaStream::EnqueueRequest` @ `0x1d0e9840` |
| program load (no SE analogue) | `tpu::System::LoadProgram` @ `0x1d0b2240` | `DeepseaExecutor::LoadProgram` (in `0x13426260`) |
| `Stream::ThenMemcpy` | `tpu::System::TransferTo/FromDevice` (`0x1d0afa20`/`0x1d0b0160`) | `TpuStream::Memcpy` (3 overloads) |
| `DeviceMemoryBase` | `tpu::TpuSharedMemoryLocation` + `tpu::TpuBuffer` | `stream_executor::DeviceAddressBase` (24 B `(ptr,size)`) |
| `stream_executor::Event` | `tpu::TpuEvent` (`tsl::AsyncValueRef`) | `stream_executor::tpu::TpuEvent` |
| stream ordering FIFO | `tpu::TpuEventIssuer` sequence points + `AsyncValue` deps | `DeepseaRequestQueue` FIFO + `WaitFor`/`RecordEvent` |
| `Allocator::Allocate` | `tpu::AllocateBuffer` @ `0xf8d51c0` | `DeviceAddressAllocator` (over backend) |
| `PJRT_Event` | `tsl::Future<void>` (`TpuClient::TrackFuture` `0xf7fad60`) | `xla::ExecutionOutput` status |

> **QUIRK —** there is no `LocalDeviceState` / per-stream object on the modern path at all — the cell is empty by design. Where SE pours compute, H2D, and D2H onto three role-specific `Stream`s and serializes hand-offs with `WaitFor`/`RecordEvent`, the modern path expresses the *same* producer/consumer ordering as a `TpuEventIssuer` DAG: a launch's `wait_events` are its dependencies and its `define_events` are fulfilled on completion. A reimplementer who reaches for a stream object on the PJRT path is modeling the wrong stack. The stream-type split survives only on the legacy side — see [stream-semantics.md](stream-semantics.md).

---

## 4. Sub-Area Map

### Purpose

The runtime section is eight detail pages plus this opener. Each owns one coherent slice of the lifecycle and is written to reimplementation grade; this map is the index a reader scans to find the right one. The four execution-flow pages (this section's core) are listed first, then the supporting reference pages.

### The execution-flow pages

| Page | Owns | Stack(s) |
|---|---|---|
| [execute-async-on-stream.md](execute-async-on-stream.md) | The legacy per-execution C++ entry: marshal `ExecutionInput` → `DeviceAddressBase[]`, allocate the output `ScopedShapedBuffer` with donation/aliasing, dispatch through vtable+24 then vtable+96. The PJRT counterpart (`ExecutePrepare`/`ExecuteLaunch`) is summarized here, owned by the adapter pages. | Legacy (PJRT prepare/launch summarized) |
| [load-program-enqueue.md](load-program-enqueue.md) | The lower half: bind the loaded `TpuCoreProgram` to a physical core (per-core Megacore fan-out), build the device `Request` from run options + buffer arrays, push it to the command stream, and record unload bookkeeping. Both `DeepseaExecutable::LoadProgramAndEnqueueToStream` and `tpu::System::LoadProgram`+`Execute` are traced. | Both |
| [stream-semantics.md](stream-semantics.md) | The ordering model: the per-stream FIFO invariant, `WaitFor(Stream*)`/`WaitFor(Event*)`/`RecordEvent` at byte level, `TpuEventIssuer` sequence points, and the compute/H2D/D2H stream split. | Both |
| [completion-loop.md](completion-loop.md) | How a launch's completion is detected and surfaced: define-event fulfillment, the `AsyncTrackingEvent`, resolution of the user-visible `tsl::Future<void>`/`PJRT_Event`, and deferred resource teardown. | Both |

### The supporting reference pages

| Page | Owns |
|---|---|
| [infeed-outfeed.md](infeed-outfeed.md) | The streaming host↔device channels: `TpuDevice::TransferToInfeed`/`FromOutfeed` (modern) vs `TpuTransferManager` (legacy), the `TpuCoreLocation`+index queue handle, layout linearization, and span-chunked blocking semantics. |
| [host-callbacks.md](host-callbacks.md) | `DoHostCallbackWithStatus` — inline on the host stream, C-shim trampoline on the TPU stream — the `host_compute` / outside-compilation realization and status marshalling. |
| [allocator-integration.md](allocator-integration.md) | The `DeviceAddressAllocator` and `tpu::AllocateBuffer` glue the execute path uses to allocate output buffers and host staging, plus the OOM defragment/retry loop. |
| [error-templates.md](error-templates.md) · [hint-strings.md](hint-strings.md) · [internal-pass-names.md](internal-pass-names.md) | The runtime's diagnostic surface: `absl::Status` source-location templates, operator hint strings, and the internal pass-name catalog the runtime stamps into errors and traces. |

> **NOTE —** the on-device program (`tpu::TpuCoreProgram`) and its loaded-state cache (`LoadedProgramState` via `TpuExecutableLoadState::LoadInternal` `0xf80c1c0`) sit at the join between this section and compilation: the *compiled* program is produced upstream (the jellyfish JIT, `TpuJitCompileHloWithOptions`), but it becomes *executable* only after `LoadProgram` binds it to a core. The buffer offsets the runtime binds were frozen at compile time by Memory-Space Assignment and replayed by the allocator — see [../memory/overview.md](../memory/overview.md).

---

## Related Components

| Component | Relationship |
|---|---|
| `xla::TpuClient` / `tpu::System` | The modern PJRT execution stack the runtime section primarily documents |
| `xla::legacy::TpuExecutableInterface` / `DeepseaExecutable` | The legacy StreamExecutor stack the `ExecuteAsyncOnStream` chain belongs to |
| `tpu::TpuEventIssuer` | The sequence-point engine that orders modern loads/enqueues/transfers against each other |
| `asic_sw::driver::deepsea::jxc::Queue` (`0xe7d9be0`) | The single TPU driver core both stacks bottom out at |
| `stream_executor::DeviceAddressBase` / `tpu::TpuSharedMemoryLocation` | The HBM buffer identity bound as a program's arguments |
| `xla::jellyfish` JIT (`TpuJitCompileHloWithOptions`) | The upstream compiler that produces the `TpuCoreProgram` this layer loads and runs |

## Cross-References

- [execute-async-on-stream.md](execute-async-on-stream.md) — the legacy per-execution entry; argument marshaling, output allocation, donation/aliasing, and the vtable+24 → vtable+96 dispatch
- [load-program-enqueue.md](load-program-enqueue.md) — the lower half: program load (sync legacy / async modern), per-core fan-out, `Request` build, enqueue, and unload bookkeeping
- [stream-semantics.md](stream-semantics.md) — the ordering model: per-stream FIFO, `WaitFor`/`RecordEvent`, `TpuEventIssuer` sequence points, and the compute/H2D/D2H stream split
- [completion-loop.md](completion-loop.md) — completion detection and the resolution of the user-visible `PJRT_Event` / `tsl::Future<void>`
- [infeed-outfeed.md](infeed-outfeed.md) — the streaming host↔device queues that interleave with a running program
- [host-callbacks.md](host-callbacks.md) — `DoHostCallbackWithStatus` and the `host_compute` / outside-compilation realization
- [allocator-integration.md](allocator-integration.md) — the `DeviceAddressAllocator` / `tpu::AllocateBuffer` glue and OOM defrag/retry
- [error-templates.md](error-templates.md) — the runtime's `absl::Status` source-location diagnostic templates
- [hint-strings.md](hint-strings.md) — operator hint strings surfaced by the runtime
- [internal-pass-names.md](internal-pass-names.md) — the internal pass-name catalog stamped into runtime errors and traces
- [../memory/overview.md](../memory/overview.md) — the compile-time → runtime buffer-offset hand-off; the HBM/VMEM allocators behind the bound `DeviceAddressBase`s
- [../dma/host-device-dma.md](../dma/host-device-dma.md) — the bulk host↔device buffer-copy transport beneath `TransferTo/FromDevice`
- [back to index](../index.md) — Part XI — Runtime & Execution
