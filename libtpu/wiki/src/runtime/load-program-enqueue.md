# LoadProgramAndEnqueueToStream

> *All addresses, struct offsets, and source-line citations on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, clang/LLVM trunk). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. Other versions will differ.*

## Abstract

`LoadProgramAndEnqueueToStream` is the lower half of a TPU execution: given a compiled program and a fully resolved set of input/output HBM addresses, it (a) binds the compiled `TpuCoreProgram` to a physical TPU core — getting back a per-core `TpuCoreProgramHandle` — and (b) builds a device request from the run options plus the buffer-address arrays and pushes it onto the core's command stream. The PJRT-facing entry that produces those resolved addresses and decides *which* replicas run is [`execute-async-on-stream.md`](execute-async-on-stream.md); this page owns everything from "the executable and its buffers are ready" down to "the request is in the driver queue."

`LoadProgramAndEnqueueToStream` is a real symbol in this binary, and there are **two** of them, because libtpu ships two parallel device stacks. The legacy C-ABI shim `TpuExecutable_LoadProgramAndEnqueueToStream` (`0xeaafba0`, source `tpu_execute_c_api.cc`) — a free `T`-exported function registered at index 7 of the TF-TPU executable-API function struct, not a method of class `xla::TpuExecutable` — marshals an `xla::ExecutableRunOptions` from a C launch struct and forwards through a virtual call (vtable+96) to the jellyfish core. That core is `xla::jellyfish::DeepseaExecutable::LoadProgramAndEnqueueToStream` (`0x13426260`, source `deepsea_executable.cc`), which does the actual `DeepseaExecutor::LoadProgram` → `DeepseaStream::EnqueueRequest`. The modern PJRT path (`xla::TpuClient` over `tpu::System`, see [`overview.md`](overview.md)) reaches the *same* device runtime through a TFRT-async-value spelling: program load is `tpu::System::LoadProgram` (`0x1d0b2240`, source `system.cc:1804`) and enqueue is `tpu::System::Execute` (`0x1d0b33e0`), both ordered by [`TpuEventIssuer`](stream-semantics.md) sequence points rather than a StreamExecutor FIFO.

The reader who knows XLA-on-GPU should hold the analogy that `LoadProgramAndEnqueueToStream` is the moral equivalent of `Executable::ExecuteOnStream` → `gpu::GpuExecutable::ExecuteThunks` → `stream->ThenLaunch(kernel, args)`, but with three TPU-specific twists worth stating up front. First, "load" is not a no-op: a compiled `TpuCoreProgram` must be *loaded onto a core* (DMA'd to its instruction memory and ref-counted in a per-core program cache) before it can run, and the load returns a `TpuCoreProgramHandle` that the enqueue addresses. Second, a single launch fans out to one program-handle *per physical core* — Megacore chips host two cores per chip and the loop must place a handle on each. Third, the buffer "arguments" are not pointers in a kernel-arg buffer; they are `stream_executor::DeviceAddressBase` records (HBM `(opaque_ptr, size)` pairs) carried in side vectors, marshalled from the C ABI by `ApiConverter::FromC`.

For reimplementation, the contract is:

- **The launch-struct → run-options marshal** — how `TpuExecutable_LoadProgramAndEnqueueToStream` rebuilds an `HloModule` shape signature, resolves the device ordinal, and fills an `xla::ExecutableRunOptions` (`set_stream` / `set_device_assignment` / `set_rng_seed` / `set_allocator`).
- **The buffer binding** — input/output/aliased `DeviceAddressBase` arrays via `ApiConverter::FromC`, plus the `uint32` argument-index vector, handed to the core executable's virtual launch slot.
- **The program load** — `DeepseaExecutor::LoadProgram` (legacy) / `tpu::System::LoadProgram` (modern): chip resolution from `TpuCoreLocation`, per-core program-handle creation, the per-core fan-out gated by `TpuChipConfig::Megacore`, and the fingerprint logged on completion.
- **The enqueue** — `DeepseaStream::EnqueueRequest` (legacy) / `tpu::System::Execute` + `TpuEventIssuer::IssueArgs/FulfillArgs` (modern): the device `Request` object, the per-replica core selection (`TpuCoreProgramHandle::core` → `TpuCoreLocation::LogicalDeviceId`), and the completion event.
- **The completion / unload bookkeeping** — `ProgramUnloadInfo` per loaded program, and the host-transfer-manager teardown posted as a host callback when the stream drains.

| | |
|---|---|
| **Legacy SE entry (C-ABI marshal)** | `TpuExecutable_LoadProgramAndEnqueueToStream` @ `0xeaafba0` (867 decompiled lines, `tpu_execute_c_api.cc`) |
| **Jellyfish core (load + enqueue)** | `xla::jellyfish::DeepseaExecutable::LoadProgramAndEnqueueToStream` @ `0x13426260` (1360 lines, `deepsea_executable.cc`) |
| **Modern program load** | `tpu::System::LoadProgram(TpuCoreLocation, shared_ptr<const TpuCoreProgram>)` @ `0x1d0b2240` (`system.cc:1804`) |
| **Modern enqueue** | `tpu::System::Execute(AsyncValueRef<ProgramHandle>, ExecuteOptions, inputs, outputs, wait, define)` @ `0x1d0b33e0` |
| **Legacy enqueue** | `deepsea::executor::DeepseaStream::EnqueueRequest(unique_ptr<Request>)` @ `0x1d0e9840` |
| **Hardware queue (driver)** | `asic_sw::driver::deepsea::jxc::Queue::EnqueueRequest(DmaBuffer, fn, bool)` @ `0xe7d9be0` |
| **Modern launch driver** | `xla::TpuExecutableLoadState::ExecuteLaunchRaw` @ `0xf8109a0`; `LoadInternal` @ `0xf80c1c0` |
| **Buffer-address marshal** | `ApiConverter::FromC(SE_DeviceAddressBase*)` → `stream_executor::DeviceAddressBase` |
| **Program handle type** | `tpu::TpuCoreProgramHandle` (carries `core()`, `fingerprint()`) |
| **Per-core fan-out gate** | `tpu::TpuChipConfig::Megacore()`; `deepsea::executor::DeepseaPlatform::GetCoreType()` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## 1. Where This Sits in the Launch

### Purpose

`LoadProgramAndEnqueueToStream` is the seam between the *device-agnostic* part of a PJRT execute (argument pinning, output-buffer allocation, donation/aliasing, replica fan-out — all in [`execute-async-on-stream.md`](execute-async-on-stream.md)) and the *device-specific* part (loading the compiled binary onto silicon and pushing a request onto the hardware command stream). It is reached once per `(executable, replica)` pair, after the caller has resolved every input and output to a concrete HBM `DeviceAddressBase`.

### The two device stacks

There are two code paths that both bear the name, and a reimplementer must know which one their front-end drives. Both end at the same TPU driver core (`asic_sw::driver::deepsea`), differing only in the host-side abstraction.

```text
LEGACY (StreamExecutor / LocalClient / TF-TPU op kernels):
  TfTpu_ExecutableApiFn[7]  (C ABI fn ptr, tpu_execute_c_api.cc)
    └─ TpuExecutable_LoadProgramAndEnqueueToStream   0xeaafba0
         ├─ rebuild HloModule shape sig  (xla::Shape::FromProto x N)
         ├─ resolve device_ordinal via TPUNodeInterfaces::Get
         ├─ build xla::ExecutableRunOptions (stream/assignment/seed/alloc)
         ├─ construct xla::jellyfish::DeepseaExecutable
         └─ vtable+96  ──►  DeepseaExecutable::LoadProgramAndEnqueueToStream
                              0x13426260  (deepsea_executable.cc)
                                ├─ DeepseaExecutor::LoadProgram  ──► TpuCoreProgramHandle
                                └─ DeepseaStream::EnqueueRequest  0x1d0e9840
                                     └─ jxc::Queue::EnqueueRequest 0xe7d9be0 (DmaBuffer)

MODERN (PJRT / TpuClient over tpu::System, TFRT async-values):
  PJRT_LoadedExecutable_Execute (slot 60, 0xf869b40)
    └─ CommonPjRtLoadedExecutable::Execute → ExecutePrepare → ExecuteLaunch
         └─ TpuRawLoadedExecutable::Execute   0xf80f580
              └─ TpuExecutableLoadState::ExecuteLaunchRaw  0xf8109a0
                   ├─ (once) TpuExecutableLoadState::LoadInternal  0xf80c1c0
                   │     └─ tpu::System::LoadProgram   0x1d0b2240 (system.cc:1804)
                   └─ tpu::System::Execute             0x1d0b33e0
                        └─ TpuEventIssuer::IssueArgs / FulfillArgs (sequence points)
```

> **NOTE —** the two stacks are not alternatives a reimplementer chooses between at run time; they are two front doors compiled into the same image. The legacy `TpuExecutable` path is driven by `xla::LocalClient` / `xla::Service` and the TF-TPU op kernels through the `Tpu*_*` C-ABI; the modern path is driven by every PJRT client (JAX, PyTorch-XLA). They share the `deepsea` driver core but never share host-side objects — there is no StreamExecutor `Stream` anywhere on the PJRT path. If you reimplement the PJRT front door, model `tpu::System::LoadProgram` + `tpu::System::Execute`; if you reimplement the TF-TPU op kernel path, model `TpuExecutable_LoadProgramAndEnqueueToStream`.

### Inputs the lower half receives

Both spellings take, in effect, the same five things — the legacy version unpacks them from a C launch struct, the modern version receives them as already-typed C++ objects:

| Input | Legacy source (`0xeaafba0`) | Modern source (`ExecuteLaunchRaw`) | Meaning |
|---|---|---|---|
| Stream / sequencing handle | `a1[12]` → `TpuStream` | `TpuEventIssuer` wait/define sets | Where to enqueue, and the ordering deps |
| Compiled program | `a1[2]` (TpuExecutable proto) → `DeepseaExecutable` | `TpuExecutable*` → `LoadedProgramState` | The thing to load onto the core |
| Output shape + param shapes | `tpu_program+112` / `+24` (`ShapeProto`s) | `ComputationLayout` in the executable | Rebuilds the `HloModule` signature |
| Input/output `DeviceAddressBase[]` | `v71[3..7]` arrays via `ApiConverter::FromC` | `Span<RCReference<CommonPjRtRawBuffer>>` | The resolved HBM addresses to bind |
| Device assignment / replica | `DeviceAssignmentProto` at `a1[11]` | `DeviceAndAssignment`, `replica` arg | Which logical device this launch targets |

---

## 2. The Launch-Struct → Run-Options Marshal (Legacy)

### Purpose

`TpuExecutable_LoadProgramAndEnqueueToStream` (`0xeaafba0`) is the C-ABI marshalling layer. Its job is to turn the flat launch struct that the TF-TPU op kernel passes through the executable-API function struct into the typed C++ objects the jellyfish core expects: an `HloModule` (for its shape signature only), an `xla::ExecutableRunOptions`, a deserialized `xla::DeviceAssignment`, and two `DeviceAddressBase` vectors. It is a long function (867 lines) mostly because every `absl::Status` and `xla::Shape` move is spelled out, but the algorithm is linear.

### Algorithm

```c
function TpuExecutable_LoadProgramAndEnqueueToStream(launch /*a1*/):   // 0xeaafba0
    tpu_program = launch[2]                                  // the serialized TpuExecutable

    // ---- Step 1: rebuild the HloModule shape signature ----------------
    out_proto = tpu_program+112  (or ShapeProto default if null)
    out_shape = Shape::FromProto(out_proto)
    CHECK(out_shape is OK)                                   // "output_shape is OK", line 101
    program_shape = { result: out_shape, params: [] }
    for p in tpu_program.parameter_shape_protos:             // tpu_program+24 (count +32)
        s = Shape::FromProto(p)
        CHECK(s is OK)                                       // "shape is OK", line 105
        program_shape.params.push_back(ShapeLayout(s))       // vector<ShapeLayout>, 320 B stride
    config = HloModuleConfig(program_shape)
    module = make_unique<HloModule>("DeepseaExecutableModule", config)

    // ---- Step 2: resolve the device ordinal + backend -----------------
    stream  = TpuStream::Stream(launch[12])                  // the SE Stream wrapper
    se      = stream->parent()                               // vtable+144
    ordinal = se->device_ordinal()                           // vtable+40
    CHECK(TPUNodeInterfaces::Get(ordinal, &interfaces) is OK) // line 169
    backend = interfaces.backend()

    // ---- Step 3: optional host-transfer manager (outside-compilation) -
    if launch[13] /*outside_compilation_params*/:
        htm = new TpuHostTransferManagerImpl(se, backend, ...)   // 0x98 bytes
        info = TPUHostTransferInfoProto()
        if !info.ParseFromString(params->host_transfers):
            return Error("Could not call ParseFromArray() on host_transfers")  // line 91
        htm.Initialize(host_transfers, platform=DeepseaPlatform::GetTopology, ...)

    // ---- Step 4: construct the jellyfish core executable --------------
    deepsea_exec = new DeepseaExecutable(module, backend, htm, ...)   // 0x98 bytes

    // ---- Step 5: deserialize the device assignment --------------------
    if launch[11].device_assignment_proto:
        proto = DeserializeProto<DeviceAssignmentProto>(...)
        assignment = DeviceAssignment::Deserialize(proto)    // StatusOr, throws on bad

    // ---- Step 6: fill ExecutableRunOptions ----------------------------
    run_opts = ExecutableRunOptions()
    run_opts.set_stream(stream)
    run_opts.set_device_assignment(assignment)
    run_opts.set_rng_seed(launch[20])                        // *((u32*)launch+20)
    run_opts.set_allocator(backend.allocator)                // backend+96

    // ---- Step 7: marshal the buffer-address arrays --------------------
    outputs = []                                             // DeviceAddressBase, 24 B each
    for i in [0 .. launch[4]):  outputs.push(ApiConverter::FromC(launch[3] + 24*i))
    output0 = ApiConverter::FromC(launch[5])                 // the single root output
    inputs  = []
    for i in [0 .. launch[6]):  inputs.push(ApiConverter::FromC(launch[7] + 24*i))
    arg_idx = copy_u32_array(launch[9], launch[8])           // uint32 argument indices

    // ---- Step 8: the virtual launch ----------------------------------
    status = deepsea_exec->vtable[96]( run_opts, outputs, output0,
                                       inputs, arg_idx )      // DeepseaExecutable::LoadProgramAndEnqueueToStream

    // ---- Step 9: post a completion callback to free the HTM -----------
    if status OK and htm:
        stream->DoHostCallback( FreeHostTransferManager(stream, htm) )  // line ~768
    return status
```

> **GOTCHA —** the `HloModule` rebuilt in Step 1 is **not** recompiled or re-optimized; it exists only to carry the `ProgramShape` (result + parameter `ShapeLayout`s) into the `DeepseaExecutable` constructor so the core can validate buffer counts and shapes. A reimplementation that tries to *run* this module on a host evaluator will mis-model the design — the compiled `TpuCoreProgram` is already inside `tpu_program`, and the module is a metadata shell named `"DeepseaExecutableModule"`. The two `CHECK(... is OK)` failures here are fatal `LogMessageFatal` calls (`tpu_execute_c_api.cc:101`, `:105`), not recoverable errors: a malformed shape proto aborts the process.

> **QUIRK —** the device ordinal is recovered by walking the stream, not passed directly: `stream->parent()` (the `StreamExecutor`, vtable slot +144) then `device_ordinal()` (vtable slot +40), then `TPUNodeInterfaces::Get(ordinal)`. This is done **twice** in the function — once for the main path (line 169) and once inside the host-transfer-manager branch (line 124) — because the HTM needs the same `(StreamExecutor, backend)` pair. A reimplementer can resolve it once and reuse it; the binary's duplication is a side effect of the inlined error-handling expansion.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuExecutable_LoadProgramAndEnqueueToStream` | `0xeaafba0` | C-ABI marshal → jellyfish core | CONFIRMED |
| `xla::Shape::FromProto` | (OSS) | Rebuild each `Shape` from its proto | CONFIRMED |
| `tensorflow::TPUNodeInterfaces::Get` | (in `0xeaafba0`) | ordinal → `{backend, ...}` | CONFIRMED |
| `tensorflow::TpuHostTransferManagerImpl` ctor | (in `0xeaafba0`) | Outside-compilation host transfers | HIGH |
| `xla::DeviceAssignment::Deserialize` | (OSS) | proto → `DeviceAssignment` | CONFIRMED |
| `xla::ExecutableRunOptions::set_*` | (inline) | stream / assignment / rng_seed / allocator | CONFIRMED |
| `ApiConverter::FromC(SE_DeviceAddressBase*)` | (OSS-mirror) | C-ABI address → `DeviceAddressBase` | CONFIRMED |
| `FreeHostTransferManager` (anon `$_0`) | (in `0xeaafba0`) | Host-callback HTM teardown | HIGH |

---

## 3. Program Load — Binding the Compiled Program to a Core

### Purpose

A compiled TPU program is a `tpu::TpuCoreProgram` (the on-device instruction image plus its ABI metadata, `TpuCoreProgramAbi`). It cannot be enqueued directly; it must first be *loaded* onto a specific physical core, which DMAs its code into the core's instruction memory, registers it in a per-core program cache, and yields a `tpu::TpuCoreProgramHandle`. The enqueue in §4 addresses that handle, not the program. The legacy core does this inline; the modern path does it once via `tpu::System::LoadProgram` and caches a `LoadedProgramState`.

### Algorithm — DeepseaExecutable (legacy core)

The jellyfish core `DeepseaExecutable::LoadProgramAndEnqueueToStream` (`0x13426260`) does load and enqueue in one function. The load portion fans out per core:

```c
function DeepseaExecutable_LoadAndEnqueue(run_opts, outputs, out0, inputs, arg_idx):  // 0x13426260
    exec    = run_opts.stream()->...->DeepseaExecutor
    plat    = DeepseaPlatform                                // GetTopology, GetCoreType
    config  = plat.GetChipConfig()                           // TpuChipConfig

    // ---- per-core program-handle fan-out ------------------------------
    handles = []                                             // vector<TpuCoreProgramHandle>
    if config.Megacore() and plat.GetCoreType() != 2:        // line 200: dual-core chip
        for core in chip.cores():                            // one LoadProgram per core
            h = DeepseaExecutor::LoadProgram(core_program, core)
            handles.push(h)
            fp = h.fingerprint()                             // line 277 — for the unload record
            unload_info.push_back(ProgramUnloadInfo{h, fp})  // line 307
    else:                                                    // single-core (line 412)
        h = DeepseaExecutor::LoadProgram(core_program)       // line 430
        fp = h.fingerprint()                                 // line 441
        handles.push(h); unload_info.push_back(ProgramUnloadInfo{h, fp})

    // on load failure:
    //   return Status.AddSourceLocation(deepsea_executable.cc:311/324)

    ... continue to enqueue (§4) ...
```

### Algorithm — tpu::System::LoadProgram (modern)

`tpu::System::LoadProgram` (`0x1d0b2240`, `system.cc:1804`) is the TFRT-native load. It resolves the per-core sub-object from the `TpuCoreLocation`, allocates an async-value program handle, and posts the load through the event issuer.

```c
function tpu_System_LoadProgram(out /*AsyncValueRef<ProgramHandle>*/, loc, program):  // 0x1d0b2240
    chip      = TpuCoreLocation::Chip(loc)                   // physical chip id
    chip_obj  = system.impl->chip_for(chip)                  // vtable+80
    core_id   = chip_obj->core_index(loc.index)              // vtable+32
    // fingerprint: inline rep (loc+671>=0) or out-of-line {ptr@648, len@656}
    fp        = read_fingerprint(program)

    host_idx  = TpuCoreLocation::LocalSharedMemory(loc, 0).index_on_host()
    core      = system.cores[host_idx]                       // per-core runtime object

    // allocate a ConcreteAsyncValue<tpu::ProgramHandle> (128 B, 64-aligned)
    handle_av = new AsyncValue<ProgramHandle>(unconstructed)
    *out      = handle_av

    // build an IssueArgs closure carrying TpuEventIssuer::FulfillArgs and
    // the TraceContext, reserve the dependency vector, and issue:
    issue = IssueArgs{ ctx, fp, RunWhenDepsReady→FulfillArgs }
    core->vtable[40](issue)                                  // enqueue the load on the core

    // stamp the fingerprint into the resolved async value (walk indirection chain)
    final_av = follow_indirect(handle_av)                    // skip forwarding nodes
    final_av->fingerprint = fp                               // +72

    VLOG(1) "TPU System::LoadProgram completed fingerprint: <hex>"   // system.cc:1804
    return out
```

> **NOTE —** the modern load returns immediately with an *unresolved* `AsyncValueRef<tpu::ProgramHandle>`; the actual DMA-to-core happens asynchronously and the handle becomes available when `TpuEventIssuer::FulfillArgs` fires. The enqueue in §4 then depends on that async value, so a launch can be *prepared* before its program is physically resident — the sequence-point engine ([`stream-semantics.md`](stream-semantics.md)) guarantees the program is loaded before the request executes. The `LoadedProgramState` cache in `TpuExecutableLoadState::LoadInternal` (`0xf80c1c0`) means the per-core DMA happens once per `(executable, device)`, not once per launch.

> **QUIRK —** `Megacore` chips (two TensorCores sharing one HBM stack) require a program handle *per core*, and the loop is gated by `TpuChipConfig::Megacore()` **and** `GetCoreType() != 2` (line 200). Core type 2 is excluded from the dual-core fan-out — on this build that is the SparseCore/sequencer family, which loads through a different path. A reimplementation that always loads a single handle will run only one of a Megacore chip's two cores and silently halve throughput; one that always loads two will double-load on a single-core part and corrupt the program cache.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `tpu::System::LoadProgram` | `0x1d0b2240` | Modern async program load (`system.cc:1804`) | CONFIRMED |
| `deepsea::executor::DeepseaExecutor::LoadProgram` | (in `0x13426260`) | Legacy synchronous core load | CONFIRMED |
| `tpu::TpuExecutableLoadState::LoadInternal` | `0xf80c1c0` | Cache `LoadedProgramState` per device | CONFIRMED |
| `tpu::TpuCoreLocation::Chip` / `LocalSharedMemory` | (in `0x1d0b2240`) | Resolve chip + per-core sub-object | CONFIRMED |
| `tpu::TpuChipConfig::Megacore` | (in `0x13426260`) | Dual-core fan-out gate (line 200) | CONFIRMED |
| `deepsea::executor::DeepseaPlatform::GetCoreType` | (in `0x13426260`) | Core-type discriminator (`!= 2`) | HIGH |
| `tpu::TpuCoreProgramHandle::fingerprint` | (in `0x13426260`) | Program identity for unload record | CONFIRMED |
| `ProgramUnloadInfo` (anon struct) | (in `0x13426260`) | Per-handle unload bookkeeping | HIGH |

---

## 4. Enqueue — Building the Request and Pushing It to the Stream

### Purpose

With the program loaded and the buffer addresses bound, the final step assembles a device `Request` — program handle + input/output `DeviceAddressBase`s + run options + sync events — and pushes it onto the core's command stream. The legacy path enqueues a `deepsea::executor::Request` via `DeepseaStream::EnqueueRequest`; the modern path calls `tpu::System::Execute` and lets `TpuEventIssuer` order it against prior work. The driver-private end is `jxc::Queue::EnqueueRequest`, which takes a `DmaBuffer` and a completion callback.

### Algorithm — DeepseaExecutable enqueue (legacy)

```c
... (continuing DeepseaExecutable_LoadAndEnqueue from §3) ...

    // ---- per-replica core selection -----------------------------------
    for h in handles:
        core_loc  = h.core()                                 // line 607 — TpuCoreLocation*
        logical_id = TpuCoreLocation::LogicalDeviceId(core_loc)  // line 608
        // logical_id picks the device-assignment row for this replica

        // ---- build the device request ---------------------------------
        req = new deepsea::executor::Request               // line 572: operator new(0xD0) = 208 B
        req.program_handle = h
        req.fingerprint    = h.fingerprint()                // line 665
        req.inputs         = inputs                          // DeviceAddressBase span
        req.outputs        = outputs
        req.arg_indices    = arg_idx
        req.completion     = $_2 closure (RemoteInvoker)     // line 711-712

        // ---- enqueue on the core's stream -----------------------------
        DeepseaStream::EnqueueRequest(stream, move(req))     // line 593 → 0x1d0e9840
        // on failure: Status.AddSourceLocation(deepsea_executable.cc:367/387)

    return OkStatus()    // or MakeErrorStream(deepsea_executable.cc:278)
```

`DeepseaStream::EnqueueRequest` (`0x1d0e9840`) hands the `unique_ptr<Request>` to a `DeepseaRequestQueue` (`EnqueueRequest` `0x1d0f23a0` → `EnqueueRequestLocked` `0x1d0f0e80`), which the driver dispatch thread drains into `jxc::Queue::EnqueueRequest` (`0xe7d9be0`) — the hardware queue that takes a `DmaBuffer` and an `AnyInvocable<void(absl::Status)>` completion.

### Algorithm — tpu::System::Execute (modern)

The modern enqueue is `tpu::System::Execute` (`0x1d0b33e0`), reached from `TpuExecutableLoadState::ExecuteLaunchRaw` (`0xf8109a0`). Rather than a synchronous queue push, it threads everything through async values:

```c
function ExecuteLaunchRaw(exec, per_launch_args, opts, ...):   // 0xf8109a0
    program_av = LoadInternal(exec).program_handle             // AsyncValueRef<ProgramHandle>
    user_promise = client.CreateLinkedUserPromise()            // PJRT completion event
    htm = TpuHostTransferManager::SetExecuteEvent(tpu_event)   // host transfers (if any)
    // (BarnaCoreManager: sparsecore offload, if the program has a SC partition)

    tpu::System::Execute(                                       // 0x1d0b33e0
        program_av,                                             // the loaded program
        ExecuteOptions{rng_seed, launch_id, ...},
        inputs  = Span<AsyncValueRef<TpuBufferBase>>,           // bound HBM buffers
        outputs = Span<AsyncValueRef<TpuBufferBase>>,
        wait_events   = Span<AsyncValueRef<TpuEvent>>,          // ordering deps
        define_events = Span<AsyncValueRef<TpuEvent>>)          // fulfilled on completion
    // System::Execute fulfils each define event via
    // TpuEventIssuer::FulfillArgs when the program completes on device.
```

> **NOTE —** the `wait_events` / `define_events` spans are the entire ordering contract. `System::Execute` does not block; it registers the launch with `TpuEventIssuer`, which runs it only once every `wait_event` is fulfilled (its inputs are produced and its program loaded), and fulfils every `define_event` when the device signals completion. This is the TPU analogue of `stream->WaitFor(event)` + `stream->RecordEvent(event)` collapsed into one dependency-graph submission — covered in depth on [`stream-semantics.md`](stream-semantics.md), with the host-side completion wiring on [`completion-loop.md`](completion-loop.md).

> **GOTCHA —** the per-replica core selection in the legacy path is `TpuCoreProgramHandle::core()` → `TpuCoreLocation::LogicalDeviceId()` (lines 607-608), **not** the host device ordinal from §2. The ordinal in §2 names which TPU node the host is talking to; the `LogicalDeviceId` here names which row of the `DeviceAssignment` (which replica/partition) this particular loaded handle serves. On a Megacore chip the two handles have the *same* chip but *different* logical device ids, so they consume different assignment rows and (potentially) different input buffers. Conflating the two ids will route every replica's work to the same core.

### The DeviceAddressBase argument arrays

The bound buffers travel as `stream_executor::DeviceAddressBase` records — the same `(opaque_ptr, size)` HBM identity stored in the [residency record](../memory/tpu-buffer-layout.md#6-the-device-buffer-residency-record). The legacy marshal (§2 Step 7) copies them from the C-ABI launch struct into three vectors with a 24-byte stride per entry, via `ApiConverter::FromC`:

```text
launch struct (C ABI)                 marshalled C++ (DeviceAddressBase, 24 B each)
  launch[3] = output addr array  ───►  vector<DeviceAddressBase> outputs   (count launch[4])
  launch[5] = root output addr   ───►  DeviceAddressBase output0
  launch[7] = input addr array   ───►  vector<DeviceAddressBase> inputs    (count launch[6])
  launch[9] = arg-index array    ───►  vector<uint32> arg_idx              (count launch[8], 4 B each)
```

> **QUIRK —** the `size` field of each `DeviceAddressBase` is **not** the user's logical byte count; it is the *padded device-shape* size — exactly the `ShapeSizeBytesRaw` of the tiled HBM buffer ([tpu-buffer-layout.md](../memory/tpu-buffer-layout.md#4-byte-sizes--shapesizebytesraw-and-friends)). The program reads and writes those addresses as tiled, padded buffers; passing a logical (un-padded) size will under-run the buffer and corrupt the trailing tile. The `arg_idx` `uint32` vector exists because input buffers can be reordered or aliased to outputs (donation) — it maps each physical input slot to its parameter index, so a donated input that becomes an output is bound once.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `xla::jellyfish::DeepseaExecutable::LoadProgramAndEnqueueToStream` | `0x13426260` | Legacy load + enqueue core | CONFIRMED |
| `deepsea::executor::DeepseaStream::EnqueueRequest` | `0x1d0e9840` | Push `Request` onto core stream | CONFIRMED |
| `deepsea::executor::DeepseaRequestQueue::EnqueueRequest` | `0x1d0f23a0` | Queue insert (locks) | CONFIRMED |
| `deepsea::executor::DeepseaRequestQueue::EnqueueRequestLocked` | `0x1d0f0e80` | Locked queue insert | CONFIRMED |
| `asic_sw::driver::deepsea::jxc::Queue::EnqueueRequest` | `0xe7d9be0` | Hardware queue (DmaBuffer + completion) | CONFIRMED |
| `tpu::System::Execute` | `0x1d0b33e0` | Modern async enqueue | CONFIRMED |
| `tpu::TpuExecutableLoadState::ExecuteLaunchRaw` | `0xf8109a0` | Modern per-launch driver | CONFIRMED |
| `tpu::TpuCoreProgramHandle::core` | (in `0x13426260`) | Handle → `TpuCoreLocation` (replica) | CONFIRMED |
| `tpu::TpuCoreLocation::LogicalDeviceId` | (in `0x13426260`) | Core → device-assignment row | CONFIRMED |
| `tpu::TpuEventIssuer::IssueArgs` / `FulfillArgs` | (in `0x1d0b2240` / `0x1d0b33e0`) | Sequence-point submit / completion | CONFIRMED |

---

## 5. Completion and Unload Bookkeeping

### Purpose

A launch that loads programs and enqueues a request must also arrange for cleanup: the loaded program handles are ref-counted in a per-core cache and may be unloaded when no launch references them, and any `TpuHostTransferManagerImpl` allocated for outside-compilation must be freed only after the stream has drained past the request that uses it. Doing this on the issuing thread would race the device; both paths defer it.

### The unload record

The legacy core records one `ProgramUnloadInfo{ handle, fingerprint }` per loaded program (`deepsea_executable.cc`, line 307 for the Megacore branch, line 471 for single-core). The fingerprint (`TpuCoreProgramHandle::fingerprint`) keys the program in the per-core cache so a later unload (`TpuCoreCommonImpl::UnloadProgram`, `0x1d13e6a0` / `UnloadProgramWithFingerprintLegacy`, `0x1d141580`) matches the right cache entry. The handle's ref-count keeps the program resident while in flight.

### The host-callback teardown

When the executable used outside-compilation, the marshal in §2 posts a host callback at line ~768:

```c
// after a successful enqueue, on the SAME stream:
stream->DoHostCallback( FreeHostTransferManager(stream, htm) )   // anon $_0
```

`DoHostCallback` enqueues the closure *behind* the request on the stream, so the TPU driver fires it on a host thread only after the request completes — at which point freeing the host-transfer manager is safe. On the legacy `TpuStream` this routes through the C-shim trampoline (`TpuStream::DoHostCallbackWithStatus`, `0xe998fa0`); on the synchronous host stream it would run inline. The closure holder is a 32-byte `operator new(32, 16)` allocation moved into an `AnyInvocable`.

> **NOTE —** the modern PJRT path replaces this with `CommonPjRtClient::CreateLinkedUserPromise` plus `TpuHostTransferManager::SetExecuteEvent` (both in `ExecuteLaunchRaw`, `0xf8109a0`): the user-visible `PJRT_Event` (a `tsl::Future<void>`) and the host-transfer lifetime are tied to the same `TpuEvent` the launch defines, so cleanup is driven by async-value resolution rather than an explicit host callback. The result is the same — resources free after the device finishes — but the mechanism is the dependency graph, not a stream callback. See [`completion-loop.md`](completion-loop.md).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ProgramUnloadInfo` push_back | (in `0x13426260`) | Per-handle unload record | HIGH |
| `tpu::TpuCoreCommonImpl::UnloadProgram` | `0x1d13e6a0` | Drop a loaded program (with callback) | CONFIRMED |
| `tpu::TpuCoreCommonImpl::UnloadProgramWithFingerprintLegacy` | `0x1d141580` | Fingerprint-keyed unload | CONFIRMED |
| `FreeHostTransferManager` (anon `$_0`) | (in `0xeaafba0`) | Deferred HTM free via host callback | HIGH |
| `tensorflow::tpu::TpuStream::DoHostCallbackWithStatus` | `0xe998fa0` | Legacy host-callback trampoline | CONFIRMED |
| `xla::CommonPjRtClient::CreateLinkedUserPromise` | (in `0xf8109a0`) | Modern user-completion event | HIGH |
| `TpuHostTransferManager::SetExecuteEvent` | (in `0xf8109a0`) | Tie HTM lifetime to launch event | HIGH |

---

## Related Components

| Component | Relationship |
|---|---|
| `TpuExecutable_LoadProgramAndEnqueueToStream` (`0xeaafba0`) | The legacy C-ABI shim that builds run-options and forwards (vtable+96) to the jellyfish core |
| `xla::jellyfish::DeepseaExecutable` (`0x13426260`) | The core that loads program handles per core and enqueues the request |
| `tpu::System` (`LoadProgram` `0x1d0b2240`, `Execute` `0x1d0b33e0`) | The modern TFRT-native runtime the PJRT path uses for load + enqueue |
| `deepsea::executor::DeepseaStream` / `DeepseaRequestQueue` | The legacy command-stream + request queue |
| `asic_sw::driver::deepsea::jxc::Queue` (`0xe7d9be0`) | The hardware queue both paths bottom out at (DmaBuffer + completion) |
| `tpu::TpuEventIssuer` | The sequence-point engine ordering loads/enqueues against each other |
| `stream_executor::DeviceAddressBase` | The HBM `(ptr, size)` records bound as the program's buffer arguments |

## Cross-References

- [execute-async-on-stream.md](execute-async-on-stream.md) — the PJRT-facing upper half: argument pinning, output allocation, donation/aliasing, and replica fan-out that produce the resolved buffers this page binds
- [stream-semantics.md](stream-semantics.md) — `TpuEventIssuer` sequence points, wait/define events, and how the enqueue is ordered against prior work
- [completion-loop.md](completion-loop.md) — the host-side completion wiring that resolves the launch's define events and frees deferred resources
- [overview.md](overview.md) — the dual-stack runtime architecture (modern PJRT `TpuClient`/`tpu::System` vs legacy StreamExecutor `TpuExecutor`) this page's two entry points belong to
- [../memory/tpu-buffer-layout.md](../memory/tpu-buffer-layout.md) — the on-device padded/tiled buffer and the `DeviceAddressBase` residency record whose `(ptr, size)` pairs are the bound arguments
- [../memory/hbm-allocator.md](../memory/hbm-allocator.md) — the `BestFitAllocator` that produced the HBM offsets bound here; the `set_allocator(backend+96)` reference in the run options
- [back to index](../index.md) — Part XI — Runtime & Execution
