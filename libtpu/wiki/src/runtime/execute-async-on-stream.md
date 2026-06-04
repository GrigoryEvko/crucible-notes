# ExecuteAsyncOnStream

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (`libtpu_lts_20260413_b_RC00`, BuildID `89edbbe81c5b328a958fe628a9f2207d`, ELF x86-64, ~745 MB). Other builds will differ.*

## Abstract

`ExecuteAsyncOnStream` is the single per-execution C++ entry point of libtpu's **legacy StreamExecutor execution path** — the `xla::Executable` virtual that turns a vector of `xla::ExecutionInput` buffers into a populated `xla::ExecutionOutput` and enqueues the device program. It is the TPU analogue of upstream XLA's `xla::Executable::ExecuteAsyncOnStream`, reached through `xla::LocalClient::Compile` → `xla::LocalExecutable::RunAsync`, not through the modern PJRT C-API. The concrete override is `xla::legacy::TpuExecutableInterface::ExecuteAsyncOnStream` @ `0x1342cd20` (3650 B), and it is the function this page reconstructs.

> **NOTE —** the task framing calls this "`TpuExecutable::ExecuteAsyncOnStream`" and references `xla::PjRtStreamExecutorLoadedExecutable`. Neither symbol exists in this binary (HIGH — exhaustive `rg` over 884,843 decompiled functions returns zero hits for `PjRtStreamExecutorLoadedExecutable`). The PJRT client in this build is `xla::TpuClient` (derived from `xla::CommonPjRtClient : xla::PjRtClient`) over the TFRT-native `tpu::System` async-value runtime, which does **not** route through `ExecuteAsyncOnStream` at all — its execute path is `PJRT_LoadedExecutable_Execute` → `CommonPjRtLoadedExecutable::Execute` → `tpu::System::Execute`. The `ExecuteAsyncOnStream` virtual that *is* present belongs to the parallel `xla::LocalClient` / `xla::Service` StreamExecutor stack, whose TPU executable is `xla::legacy::TpuExecutableInterface` (and its subclass `xla::jellyfish::DeepseaExecutable`). This page documents that real entry; where the contract names a PJRT concept, it is mapped to the StreamExecutor object that fills the role.

The entry does four things in order: marshal the host `ExecutableRunOptions` and `ExecutionInput` arguments into a buffer tree, allocate the output `ScopedShapedBuffer` with input-buffer donation/aliasing, dispatch the device work through a vtable slot into the enqueue lower half, and assemble an `ExecutionOutput` (or an `absl::Status`) for the caller. The enqueue lower half — `DeepseaExecutable::LoadProgramAndEnqueueToStream` @ `0x13426260` — is on [Load Program and Enqueue](load-program-enqueue.md); this page owns the *entry, the argument/output marshaling, and the dispatch into that layer*.

For reimplementation, the contract is:

- **The dispatch lattice.** Three callers reach one virtual: `LocalExecutable::RunAsync` → `Executable::ExecuteAsyncOnStreamWrapper` → vtable slot **+24** (`ExecuteAsyncOnStream`), and a second public door, the C-ABI shim `TpuExecutable_ExecuteAsyncOnStream` @ `0xeabd500`, which calls the same +24 slot after un-marshaling C structs.
- **Argument marshaling.** How an `xla::ExecutionInput` vector is walked into a flat `DeviceAddressBase` array indexed by `IndexTable`, plus the dynamic-shape side channel.
- **Output construction.** `AllocateOutputMemoryWithInputReuse` (@ `0x1342ba00`) building a `ScopedShapedBuffer`, the input→output aliasing fixups driven by `HloInputOutputAliasConfig`, and `MarkToBeReleasedArguments`.
- **The hand-off.** The vtable **+96** indirect call into `LoadProgramAndEnqueueToStream` and the move of the returned root buffer into the caller's `ExecutionOutput`.

| | |
|---|---|
| **Entry point** | `xla::legacy::TpuExecutableInterface::ExecuteAsyncOnStream` @ `0x1342cd20` (3650 B) |
| **C-ABI shim** | `TpuExecutable_ExecuteAsyncOnStream` @ `0xeabd500` (4708 B) — `tpu_executor_c_api.cc` |
| **Public C++ wrapper** | `xla::Executable::ExecuteAsyncOnStreamWrapper` @ `0x1dad98a0` (579 B, `ExecutionInput` overload) |
| **Client driver** | `xla::LocalClient` → `xla::LocalExecutable::RunAsync` @ `0x1084d140` (2489 B) |
| **Vtable dispatch slot** | `+24` (enter `ExecuteAsyncOnStream`); `+96` (leaf `LoadProgramAndEnqueueToStream`) |
| **Output allocator** | `TpuExecutableInterface::AllocateOutputMemoryWithInputReuse` @ `0x1342ba00` (4828 B) |
| **Enqueue lower half** | `xla::jellyfish::DeepseaExecutable::LoadProgramAndEnqueueToStream` @ `0x13426260` (7512 B) |
| **Arg type** | `std::vector<xla::ExecutionInput>` (192 B/element) |
| **Result type** | `xla::ExecutionOutput` (wraps a `ScopedShapedBuffer`) returned by value as `StatusOr` |
| **Source file (asserts)** | `stream_executor/tpu/tpu_executable_interface.cc` |

---

## Object Model and Class Hierarchy

### Purpose

`ExecuteAsyncOnStream` is a virtual on the upstream `xla::Executable` base. On TPU the override lives on `xla::legacy::TpuExecutableInterface`, an abstract class that implements argument/output marshaling once and defers the actual device enqueue to a pure-virtual leaf implemented by the concrete `xla::jellyfish::DeepseaExecutable`.

### Inheritance

```text
xla::Executable                              (upstream base; ExecuteAsyncOnStream is virtual @ vtable+24)
  └─ xla::legacy::TpuExecutableInterface      ── implements ExecuteAsyncOnStream @ 0x1342cd20
       │                                          (marshal args, allocate outputs, dispatch via +96)
       └─ xla::jellyfish::DeepseaExecutable    ── implements LoadProgramAndEnqueueToStream @ 0x13426260
                                                   (the leaf invoked through vtable+96; load-program-enqueue.md)
```

The `legacy::` namespace on `TpuExecutableInterface` is the binary's own label (mangled `ZN3xla6legacy22TpuExecutableInterface…`), and is the clearest single signal that this whole code path is the deprecated StreamExecutor execution model retained for `LocalClient`/`Service` and the TF-TPU op kernels. The modern PJRT front door uses `xla::TpuClient` instead.

> **QUIRK —** the two vtable slots are different objects. `ExecuteAsyncOnStream` is dispatched at **+24** on the `Executable` vtable (called by `ExecuteAsyncOnStreamWrapper` line 45 and the C shim line 703). Inside `ExecuteAsyncOnStream` the device work is dispatched at **+96** (`(*(...)(*v89 + 96))`, interface line 650) — that is the pure-virtual `LoadProgramAndEnqueueToStream`. A reimplementer who collapses the two into one method loses the abstract/concrete split that lets `DeepseaExecutable` swap the device backend without touching marshaling.

### Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `xla::legacy::TpuExecutableInterface::ExecuteAsyncOnStream` | `0x1342cd20` | 3650 B | The entry: marshal → allocate → dispatch → output |
| `xla::legacy::TpuExecutableInterface::AllocateOutputMemoryWithInputReuse` | `0x1342ba00` | 4828 B | Build output `ScopedShapedBuffer`, honor donation/aliasing |
| `xla::jellyfish::DeepseaExecutable::LoadProgramAndEnqueueToStream` | `0x13426260` | 7512 B | Pure-virtual leaf (vtable+96) — device enqueue |
| `xla::Executable::ExecuteAsyncOnStreamWrapper` (`ExecutionInput`) | `0x1dad98a0` | 579 B | Profiled public wrapper around the +24 virtual |
| `xla::Executable::ExecuteAsyncOnStreamWrapper` (`ShapedBuffer`) | `0x1dad9780` | 259 B | Legacy `ShapedBuffer`-span overload |
| `xla::ExecuteWrapperAfterExecution` | `0x1dad9b00` | 266 B | Post-exec profiling/HLO-profile finalize |
| `xla::LocalExecutable::RunAsync` | `0x1084d140` | 2489 B | `LocalClient` driver → wrapper |
| `TpuExecutable_ExecuteAsyncOnStream` | `0xeabd500` | 4708 B | C-ABI shim: C structs ↔ C++ objects |

---

## Entry Point and Dispatch Lattice

### Purpose

There is exactly one execution virtual, reached from two directions: the in-process C++ client (`LocalExecutable::RunAsync`) and the C-ABI boundary (`TpuExecutable_ExecuteAsyncOnStream`, exported for the StreamExecutor `TpuExecutor` shim).

### Entry Point

```text
xla::LocalExecutable::RunAsync (0x1084d140)             ── LocalClient per-call driver
  └─ xla::Executable::ExecuteAsyncOnStreamWrapper (0x1dad98a0)
       ├─ xla::ExecuteWrapperBeforeExecution            ── start HLO execution profile
       ├─ [vtable+24] ExecuteAsyncOnStream  ───────────┐
       └─ xla::ExecuteWrapperAfterExecution (0x1dad9b00)│ ── finalize profile, stamp Status
                                                        │
TpuExecutable_ExecuteAsyncOnStream (0xeabd500)          │ ── C-ABI: FromC args, build RunOptions
  └─ [vtable+24] ExecuteAsyncOnStream ──────────────────┤    then ToC the ExecutionOutput
                                                        │
  xla::legacy::TpuExecutableInterface::ExecuteAsyncOnStream (0x1342cd20)  <─┘
    ├─ AllocateOutputMemoryWithInputReuse (0x1342ba00)  ── ScopedShapedBuffer + aliasing
    ├─ xla::Executable::MarkToBeReleasedArguments       ── donation bookkeeping
    └─ [vtable+96] DeepseaExecutable::LoadProgramAndEnqueueToStream (0x13426260)  ── device enqueue
```

### Algorithm — the wrapper

`ExecuteAsyncOnStreamWrapper` is thin: it brackets the virtual call with the profiling hooks. Both hooks exist even when profiling is disabled (they no-op on a null `HloExecutionProfile`).

```c
function ExecuteAsyncOnStreamWrapper(self, run_options, args):   // 0x1dad98a0
    state = ExecuteWrapperBeforeExecution(run_options)           // start span; capture stream
    out   = (*self.vtable[24])(self, run_options, &args)         // -> ExecuteAsyncOnStream; moves args
    stream = run_options->stream()                               // line 70
    status = ExecuteWrapperAfterExecution(self, &state,          // 0x1dad9b00
                                          out.status, stream)    //   finalize profile
    return out                                                   // ExecutionOutput by value
```

> **NOTE —** the wrapper *moves out* of the caller's `args` vector (lines 36-44 zero the source vector header, then destroy the moved-from `ExecutionInput`s). The argument vector is consumed by the call; a reimplementer must not reuse it afterward.

### The C-ABI shim

`TpuExecutable_ExecuteAsyncOnStream` (`0xeabd500`, from `tpu_executor_c_api.cc`) is the boundary the StreamExecutor `TpuExecutor` C-shim crosses. It is pure marshaling around the same +24 virtual:

```c
function TpuExecutable_ExecuteAsyncOnStream(self, c_run_opts, c_args[], n, c_out, status_out):  // 0xeabd500
    run_options.set_device_ordinal(c_run_opts->device_ordinal)        // a2+32
    if c_run_opts->allocator:                                          // a2+8
        run_options.set_allocator(new DeviceAddressAllocator{          // 0x18 B object
            GetUnderlyingDeepseaPlatform(), c_run_opts })              //   wraps deepsea platform
    run_options.set_stream(*c_run_opts->stream)                        // a2+40
    if c_run_opts->host_to_device_stream:                             // a2+48
        run_options.set_host_to_device_stream(...)
    if c_run_opts->device_assignment:                                 // a2+56
        proto = DeserializeProto<DeviceAssignmentProto>(...)          // TpuSerializedProto
        run_options.set_device_assignment(DeviceAssignment::Deserialize(proto))
    run_options.set_rng_seed(c_run_opts->rng_seed)                    // a2+72
    run_options.set_run_id(c_run_opts->run_id)                        // a2+80
    // -- marshal each C SE_ExecutionInput into an xla::ExecutionInput --
    for i in 0..n:                                                    // line 261
        arg = ExecutionInput(ApiConverter::FromC(c_args[i]))          // shape
        TF_CHECK_OK(arg.SetDynamicShape(FromC(c_args[i]+560)))        // dynamic shape side channel
        for each buffer in c_args[i].buffers (stride 72, base +536):  // line 312
            arg.SetUnownedBuffer / SetBuffer(FromC(buffer))           //   MaybeOwningDeviceAddress
        for each aliased index (stride 72, base +544, count +552):    // line 504
            arg.MutableBuffers()->insert(ShapeIndex)                  //   IndexTable entry
        args.push_back(move(arg))
    out = (*self.vtable[24])(&out, self, &run_options, &args)         // line 703 -> ExecuteAsyncOnStream
    if out.ok():
        scoped = ScopedShapedBuffer(out.result)
        ApiConverter::ToC(c_out, scoped.release())                    // populate SE_ExecutionOutput
    else:
        *status_out = out.status                                     // line 887
    // destroy args, run_options, allocator
```

> **GOTCHA —** `SetDynamicShape` is asserted with `TF_CHECK_OK` (the shim asserts at `tpu_executor_c_api.cc:1190`). A malformed dynamic-shape blob from the C side is a hard `LogMessageFatal`, not a returned error. The dynamic shape lives at a fixed `+560` offset from each C argument struct, separate from the static shape at offset `0`.

---

## Argument Marshaling

### Purpose

The interface entry receives `args` already as `std::vector<xla::ExecutionInput>`. Its first job is to flatten every leaf buffer of every argument into a single contiguous `DeviceAddressBase` array that the enqueue layer consumes positionally, while preserving the tree shape via the per-argument `IndexTable`.

### Algorithm

```c
function TpuExecutableInterface::ExecuteAsyncOnStream(self, run_options, args):  // 0x1342cd20
    n = args.size()                                              // a4[1]
    // ---- Stage 1: flatten argument leaf buffers ----
    flat = new DeviceAddressBase[n]                              // 24 B/elem (line 142)
    for i in 0..n:                                               // walk arg[i].Buffers()
        entry = IndexTable::GetEntry(arg[i].buffers, root, /*index*/0)   // tuple_tree.h:332 CHECK
        addr  = entry.AsDeviceAddress()                         // MaybeOwningDeviceAddress
        flat[i] = addr                                          // moved into flat array
    // ---- Stage 2: fetch result shape + aliasing config from the program ----
    if program (a2):
        result_shape = program->result_shape()                  // vtable+40 (line 238)
        alias_config = program->input_output_alias_config()     // HloInputOutputAliasConfig (+2840…)
    else:
        result_shape = Shape{}                                  // empty (line 249)
        alias_config = ShapeTree<optional<Alias>>{}             // line 334
    CHECK(run_options->allocator() != nullptr)                  // tpu_executable_interface.cc:219
    ...
```

The argument vector element stride is **192 bytes** (an `xla::ExecutionInput`), visible in every destructor loop (`192 * count`, e.g. lines 666, 711, 945 of the C shim and 53 of the wrapper). Each `ExecutionInput` carries a `Shape`, a dynamic `Shape`, and a `ShapeTree<MaybeOwningDeviceAddress>` of leaf buffers indexed through `xla::internal::IndexTable`. The leaf device addresses are 24-byte `DeviceAddressBase` records (opaque pointer + size + memory-space tag); the flatten loop asserts each `AsDeviceAddress().opaque() != nullptr` (`tuple_tree.h:332`).

> **QUIRK —** the flatten array uses a hand-rolled growable vector with the `2*cap` doubling policy (`v16 = 2*v7`, interface line 195) and a `0xAAAAAAAAAAAAAAAA` length-error guard (interface lines 139, 196 — the max element count for the 24-byte `DeviceAddressBase` stride). This is not a `std::vector<DeviceAddressBase>` with default growth — the leaf count is known up front (`24 * n`, line 142), so a reimplementation can pre-size exactly and skip the reallocation path entirely (interface lines 142–218). (The `0xAAAAAAAAAAAAAAAB` division magic that recovers a /192 element count from a byte span belongs to the *wrapper's* `ExecutionInput` vector teardown at `0x1dad98a0` line 64, not to this 24-byte flat array.)

---

## Output Construction and Aliasing

### Purpose

The output `ScopedShapedBuffer` is allocated *before* the device runs, so the enqueue layer can write results directly into it. Donation lets an input buffer become an output buffer in place, avoiding an allocation and a copy.

### Algorithm

```c
    // ---- Stage 3: allocate outputs, reusing donated input buffers ----
    out_or = AllocateOutputMemoryWithInputReuse(                // 0x1342ba00
                 result_shape, alias_config,
                 run_options->allocator(),
                 args,                                          // donor source
                 run_options->stream(),
                 run_options->host_to_device_stream())
    if !out_or.ok():
        return out_or.status.AddSourceLocation(tpu_executable_interface.cc:228)
    output = ScopedShapedBuffer(out_or)                         // line 344

    // ---- Stage 4: re-wire donated input buffers into the output tree ----
    if program->aliasing_table (a2+3488, count a2+3496):        // line 382
        for (param, output_index) in aliasing_table:            // 6-qword stride
            CHECK(param < args.size())                          // ...interface.cc:236
            in_entry  = IndexTable::GetEntry(args[param], index) //  ...:242
            CHECK(!in_entry.AsDeviceAddress().is_null())         //  ...:243
            CHECK(in_entry.is_owning /* offset */)               //  ...:244
            aliased.push_back(in_entry)                          //  donor address list
            buffers_to_release.push_back(param_index)            //  uint32 vector

    // ---- Stage 5: bookkeeping + dispatch ----
    MarkToBeReleasedArguments(program, args[0], n, output)       // line 643
    root = output.root_buffer()                                  // line 644
    status = (*program.vtable[96])(                              // line 650 -> LoadProgramAndEnqueueToStream
                 program, run_options,
                 flat /*device addresses*/, n,
                 aliased /*donor list*/, buffers_to_release,
                 root.opaque())
    if status.ok():
        result.set_result(move(output))                         // ScopedShapedBuffer into ExecutionOutput
    else:
        result.status = status.AddSourceLocation(...:266)
    return result                                                // ExecutionOutput
```

`AllocateOutputMemoryWithInputReuse` (`0x1342ba00`) walks the result `Shape` with `ShapeUtil::ForEachMutableSubshapeHelper` (callback at `0x1342dc00`); for each leaf subshape it consults the `HloInputOutputAliasConfig` to decide whether to allocate fresh device memory through the `DeviceAddressAllocator` or to claim a donated input buffer. The result is a `ScopedShapedBuffer` — an owning `ShapedBuffer` whose leaf addresses are RAII-freed on the supplied stream unless explicitly `release()`d into the `ExecutionOutput`.

> **GOTCHA —** the aliasing fixup re-reads the input buffer through `IndexTable::GetEntry` and asserts `it != arguments[parameter].MutableBuffers()->end()` (`...interface.cc:242`). A donation declared in the HLO `input_output_alias_config` for a parameter index that the caller did not actually pass (or passed without that leaf) is a fatal `CHECK`, not a graceful fallback. The donor must be present *and* owning (`offset` truthy, `:244`).

> **QUIRK —** `MarkToBeReleasedArguments` runs **before** the device enqueue, not after. It records which argument buffers the program is allowed to consume so the caller's `ExecutionInput` destructors do not double-free buffers the executable now owns. The actual release is deferred to whoever holds the resulting `ExecutionOutput`. Reimplementing this after the enqueue would race the device against argument teardown.

---

## The Dispatch into the Enqueue Layer

### Purpose

The single hand-off from marshaling to device work. Everything above is target-independent buffer plumbing; everything below the +96 call is the `DeepseaExecutable` device backend.

### What crosses the boundary

The vtable+96 call (`LoadProgramAndEnqueueToStream`) receives, in order: the `ServiceExecutableRunOptions` (carrying the stream, allocator, device assignment, run id, rng seed), the flat `DeviceAddressBase` argument array plus its count, the donor-buffer span, the `buffers_to_release` index vector, and the opaque pointer of the output root buffer. The leaf returns an `absl::Status`; on success the pre-allocated `output` is now populated on the device and is moved into the returned `ExecutionOutput`.

```text
ExecuteAsyncOnStream                       LoadProgramAndEnqueueToStream (load-program-enqueue.md)
─────────────────────                      ──────────────────────────────────────────────────────
  run_options  ───────────────────────────▶  stream / allocator / device_assignment / run_id
  flat[] (DeviceAddressBase, n)  ──────────▶  positional input device addresses
  aliased[] (donated input addrs) ─────────▶  in-place output reuse
  buffers_to_release[] (uint32)  ──────────▶  donation index list
  output.root_buffer().opaque()  ──────────▶  output root device pointer (written by device)
  ◀──────────────────────────────  absl::Status (ok ⇒ output is live)
```

Stream ordering, the on-device program load (`tpu::System::LoadProgram` / `TpuCoreProgram`), and the completion event are *not* established here — they live entirely below the +96 boundary. See the cross-references.

### Considerations

- **Replica / partition handling.** `ExecuteAsyncOnStream` is single-device per call. The device assignment arrives via `run_options->device_assignment()` (deserialized from a `DeviceAssignmentProto` in the C shim, interface line 236-245). Multi-replica fan-out is the caller's responsibility — `LocalExecutable::RunAsync` resolves the stream and device ordinal from the run options before the wrapper call (RunAsync line 195). There is no replica loop inside the entry itself.
- **Error surface.** Two error styles coexist. Buffer-shape and aliasing violations are fatal `CHECK`/`LogMessageFatal` (the binary treats a malformed buffer tree as a programming error). Allocation failure and device-enqueue failure are returned `absl::Status` with `AddSourceLocationImpl` stamps (`:228` for allocation, `:266` for enqueue) — these propagate to the caller as a failed `ExecutionOutput`.
- **No StreamExecutor `Stream` creation.** Despite the name, this path does not create streams. `run_options->stream()` is supplied by the caller (`LocalExecutable::RunAsync` or the C shim's `c_run_opts->stream`). The "async" is the StreamExecutor stream model, distinct from the PJRT `tpu::System` async-value model documented under the adapter pages.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::jellyfish::DeepseaExecutable::LoadProgramAndEnqueueToStream` | The vtable+96 leaf this entry dispatches into — the device enqueue lower half |
| `xla::Executable::ExecuteAsyncOnStreamWrapper` | The profiled public C++ wrapper that calls the +24 virtual |
| `xla::LocalExecutable::RunAsync` | The `LocalClient` per-call driver that resolves stream/ordinal and calls the wrapper |
| `TpuExecutable_ExecuteAsyncOnStream` | The C-ABI shim that marshals C structs across the StreamExecutor boundary |
| [`xla::TpuClient`](../pjrt/client-and-device.md) (PJRT path) | The *modern* execution path; bypasses `ExecuteAsyncOnStream` entirely via `tpu::System::Execute` |

## Cross-References

- [Load Program and Enqueue](load-program-enqueue.md) — the vtable+96 leaf (`LoadProgramAndEnqueueToStream`); device program load and command-stream enqueue
- [Stream Semantics](stream-semantics.md) — how `run_options->stream()` orders the enqueued work
- [Completion Loop](completion-loop.md) — the async completion event the enqueue layer produces
- [Allocator Integration](allocator-integration.md) — the `DeviceAddressAllocator` that `AllocateOutputMemoryWithInputReuse` and the C shim build
- [Host Callbacks](host-callbacks.md) — host-side infeed/outfeed and callback dispatch during execution
- [PJRT Executable Execution](../pjrt/executable-execution.md) — the modern `PJRT_LoadedExecutable_Execute` → `CommonPjRtLoadedExecutable::Execute` path this entry is the legacy counterpart of
- [Runtime Overview](overview.md) — where the StreamExecutor execution path sits relative to the PJRT path
