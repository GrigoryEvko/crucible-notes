# PJRT Executable Loading & Execution

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (`libtpu_lts_20260413_b_RC00`, BuildID `89edbbe81c5b328a958fe628a9f2207d`, ELF x86-64 DYN, ~745 MB). The PJRT C-API surface is **v0.103**. Other builds renumber slots and shift addresses.*

## Abstract

This page reconstructs the **PJRT C-ABI executable surface** of `libtpu.so`: the `pjrt::PJRT_Executable_*` and `pjrt::PJRT_LoadedExecutable_*` wrappers that a host framework (JAX, PyTorch/XLA) calls through the `PJRT_Api` vtable to compile a program, serialize and reload it, query its shape metadata, and launch it onto the TPU. Every one of these entries is a *thin C marshaling shim* over a C++ object: the wrappers unpack a versioned `_Args` struct, dispatch through one virtual slot on a `xla::PjRtExecutable` / `xla::PjRtLoadedExecutable`, and pack the C++ result back into the `_Args` struct. They are byte-for-byte the generic XLA `pjrt_c_api_wrapper_impl.cc` code — none is TPU-specialized (see [API Vtable Reconstruction](api-vtable-reconstruction.md), the populated-vs-injected map).

The contract is identical in shape to upstream XLA's PJRT C-API: the host owns the `_Args` structs, the plugin owns the objects behind the opaque `PJRT_Executable*` / `PJRT_LoadedExecutable*` handles, and a leading `struct_size` field on every `_Args` struct carries the version compatibility (the [backward-compat guard](api-vtable-reconstruction.md#backward-compatibility-guard) `pjrt::ActualStructSizeIsGreaterOrEqual`). What is TPU-specific lives entirely *below* these wrappers, behind the virtual calls: the concrete `xla::TpuExecutable` (compiled) / `xla::TpuLoadedExecutable` (device-loaded) under the `xla::CommonPjRtLoadedExecutable` framework, and the runtime enqueue (`ExecutePrepare` → `ExecuteLaunch` → `tpu::System::Execute`).

This page owns the *C-ABI wrapper layer and its marshaling*: `Compile` → executable, `Serialize` / `DeserializeAndLoad`, `Execute` (args→device-buffer marshaling, multi-device vs `execute_device` dispatch, `PJRT_ExecuteOptions`, output `PJRT_Buffer` lists), and the name / num-outputs / memory-kind accessors. The runtime-internal enqueue the `Execute` wrapper drives down into is on [ExecuteAsyncOnStream](../runtime/execute-async-on-stream.md) and [Load Program and Enqueue](../runtime/load-program-enqueue.md) (the modern PJRT path runs through `CommonPjRtLoadedExecutable::Execute` → `tpu::System::Execute`, **not** the legacy StreamExecutor `ExecuteAsyncOnStream`); the compile cache is on [Compilation Cache](../compiler/compilation-cache.md); the multi-phase compile extension is on [PhaseCompile Extension](ext-compile-phasecompile.md).

For reimplementation, the contract is:

- **The `_Args` struct discipline.** Every entry begins with `ActualStructSizeIsGreaterOrEqual(name, min, current, args->struct_size)`; on failure it returns a heap `PJRT_Error*` and reads nothing. `Execute` is the only entry that validates *two* structs (the outer args and the nested `PJRT_ExecuteOptions`).
- **The handle-wrapping rule.** `GetExecutable` / `DeserializeAndLoad` `operator new` a C-ABI box (`PJRT_Executable` = 0x250 B, `PJRT_LoadedExecutable` = 0x48 B) around a C++ object and store the box pointer into an `_Args` out-field. The host frees it via `PJRT_*_Destroy`.
- **The Execute dispatch fork.** `execute_device == NULL` → multi-device `xla::PjRtLoadedExecutable::Execute` (returns `vector<vector<unique_ptr<PjRtBuffer>>>`); `execute_device != NULL` → single-device path that **requires `num_devices == 1`** and calls the portable (vtable +80) or sharded (vtable +72) virtual, keyed on the executable's `compile_portable_executable` flag.
- **The serialize round-trip.** `Serialize` calls the executable's `SerializeExecutable` virtual into a heap `PJRT_SerializedExecutable` with a deleter callback; `DeserializeAndLoad` parses a `CompileOptionsProto`, reconstructs `CompileOptions`, and reloads through the client.

| | |
|---|---|
| **Builder (`Compile`)** | `pjrt::PJRT_Client_Compile @ 0x0F861820` (slot 25) |
| **Execute** | `pjrt::PJRT_LoadedExecutable_Execute @ 0x0F869B40` (slot 60, 1914 decompiled lines) |
| **Serialize** | `pjrt::PJRT_Executable_Serialize @ 0x0F86C5A0` (slot 54) |
| **Deserialize+Load** | `pjrt::PJRT_Executable_DeserializeAndLoad @ 0x0F86CC40` (slot 61) |
| **GetExecutable** | `pjrt::PJRT_LoadedExecutable_GetExecutable @ 0x0F86CFA0` (slot 56) |
| **Compatibility guard** | `pjrt::ActualStructSizeIsGreaterOrEqual @ 0x0F8A4EC0` (every entry) |
| **C-ABI box sizes** | `PJRT_Executable` = `0x250` (592 B); `PJRT_LoadedExecutable` = `0x48` (72 B) |
| **Compiled C++ object** | `xla::TpuExecutable` (typeinfo `0x21786610`) under `xla::CommonPjRtLoadedExecutable` |
| **Source (asserts)** | `third_party/tensorflow/compiler/xla/pjrt/c/pjrt_c_api_wrapper_impl.cc` |

---

## Object Model: C Handle ↔ C++ Executable

### Purpose

There are two opaque C handles and a three-class C++ hierarchy behind them. The host never sees a C++ type; it holds a `PJRT_Executable*` (compiled-program metadata) or a `PJRT_LoadedExecutable*` (a device-resident, launchable program), each a heap box wrapping a `unique_ptr` to the real object. Understanding which handle owns which virtual surface is the prerequisite for every wrapper below.

### Handle layout

```text
PJRT_Executable        (operator new 0x250 = 592 B)
  └─ unique_ptr<xla::PjRtExecutable>   ──▶  xla::TpuExecutable   (typeinfo 0x21786610)
                                              GetHloModules, name(), num_outputs,
                                              SerializeExecutable, GetOutputMemoryKinds,
                                              GetCostAnalysis, GetCompiledMemoryStats, …

PJRT_LoadedExecutable  (operator new 0x48  = 72 B)
  └─ unique_ptr<xla::PjRtLoadedExecutable> ─▶ xla::TpuLoadedExecutable (typeinfo 0x2177b9b8)
                                              GetExecutable(), Execute(), ExecuteSharded(),
                                              ExecutePortable(), IsDeleted(), Delete()
       (device-bound; under xla::CommonPjRtLoadedExecutable framework, typeinfo 0x2178a0f0)
```

The split mirrors upstream XLA exactly: `PJRT_Executable` is the *ahead-of-launch* artifact (it can be serialized, queried, and shipped to another host), while `PJRT_LoadedExecutable` is *bound to this client's devices* and is the only handle you can `Execute`. `PJRT_LoadedExecutable_GetExecutable` (slot 56) is the bridge: it pulls the compiled `PjRtExecutable` out of a loaded one and boxes it in a fresh `PJRT_Executable`.

### Function Map — the executable surface

| Slot | Field | Symbol (`pjrt::`…) | Addr | Wrapped virtual |
|---|---|---|---|---|
| 25 | PJRT_Client_Compile | `PJRT_Client_Compile` | `0x0F861820` | client compile → loaded exec |
| 45 | PJRT_Executable_Destroy | `PJRT_Executable_Destroy` | `0x0F8661C0` | box dtor |
| 46 | PJRT_Executable_Name | `PJRT_Executable_Name` | `0x0F866860` | `+40` `name()` |
| 47 | PJRT_Executable_NumReplicas | `PJRT_Executable_NumReplicas` | `0x0F8668C0` | `num_replicas()` |
| 48 | PJRT_Executable_NumPartitions | `PJRT_Executable_NumPartitions` | `0x0F866920` | `num_partitions()` |
| 49 | PJRT_Executable_NumOutputs | `PJRT_Executable_NumOutputs` | `0x0F866A40` | cached field `+272` |
| 50 | PJRT_Executable_SizeOfGeneratedCodeInBytes | `…SizeOfGeneratedCodeInBytes` | `0x0F867240` | `SizeOfGeneratedCodeInBytes()` |
| 51 | PJRT_Executable_GetCostAnalysis | `PJRT_Executable_GetCostAnalysis` | `0x0F867B80` | `GetCostAnalysis()` |
| 52 | PJRT_Executable_OutputMemoryKinds | `PJRT_Executable_OutputMemoryKinds` | `0x0F869520` | `+104` `GetOutputMemoryKinds()` |
| 53 | PJRT_Executable_OptimizedProgram | `PJRT_Executable_OptimizedProgram` | `0x0F8672A0` | `GetHloModules()` |
| 54 | PJRT_Executable_Serialize | `PJRT_Executable_Serialize` | `0x0F86C5A0` | `+144` `SerializeExecutable()` |
| 56 | PJRT_LoadedExecutable_GetExecutable | `PJRT_LoadedExecutable_GetExecutable` | `0x0F86CFA0` | `+32` `executable()` |
| 58 | PJRT_LoadedExecutable_Delete | `PJRT_LoadedExecutable_Delete` | `0x0F869A80` | `Delete()` |
| 59 | PJRT_LoadedExecutable_IsDeleted | `PJRT_LoadedExecutable_IsDeleted` | `0x0F869AE0` | `IsDeleted()` |
| 60 | PJRT_LoadedExecutable_Execute | `PJRT_LoadedExecutable_Execute` | `0x0F869B40` | `Execute` / `+80` ExecutePortable / `+72` ExecuteSharded |
| 61 | PJRT_Executable_DeserializeAndLoad | `PJRT_Executable_DeserializeAndLoad` | `0x0F86CC40` | client deserialize+load |
| 95 | PJRT_Executable_OutputElementTypes | `PJRT_Executable_OutputElementTypes` | `0x0F868560` | cached dims/types |
| 96 | PJRT_Executable_OutputDimensions | `PJRT_Executable_OutputDimensions` | `0x0F8689E0` | cached dims/types |
| 99 | PJRT_Executable_Fingerprint | `PJRT_Executable_Fingerprint` | `0x0F867AC0` | `FingerprintExecutable()` |
| 101 | PJRT_Executable_GetCompiledMemoryStats | `PJRT_Executable_GetCompiledMemoryStats` | `0x0F86CAC0` | `GetCompiledMemoryStats()` |
| 129 | PJRT_Executable_GetCompileOptions | `PJRT_Executable_GetCompileOptions` | `0x0F86C6E0` | `GetCompileOptions()` |
| 139 | PJRT_Executable_ParameterMemoryKinds | `PJRT_Executable_ParameterMemoryKinds` | `0x0F868FC0` | `GetParameterMemoryKinds()` |
| 122 | PJRT_LoadedExecutable_GetDeviceAssignment | `…GetDeviceAssignment` | `0x0F870EA0` | `device_assignment()` |
| 135 | PJRT_LoadedExecutable_AddressableDeviceLogicalIds | `…AddressableDeviceLogicalIds` | `0x0F8669E0` | `addressable_device_logical_ids()` |

> **NOTE —** slot 62 `PJRT_LoadedExecutable_Fingerprint` (`0x0F85FBE0`) is the *deprecated* fingerprint entry; v0.103 hosts use slot 99 `PJRT_Executable_Fingerprint`. Both are populated. The deprecated one is kept only so an older-minor host's smaller args struct still resolves a non-null pointer.

> **QUIRK —** none of these 24 slots is TPU-specialized. They are the stock `pjrt_c_api_wrapper_impl.cc` wrappers — the binary's own diagnostic strings name that exact file. The TPU-ness is entirely behind the virtual calls (`xla::TpuExecutable` / `xla::TpuLoadedExecutable`) and behind the injected `Client_Create` (slot 15). A reimplementer porting libtpu to another accelerator rewrites the C++ objects, not these wrappers.

---

## Compile: `PJRT_Client_Compile` → Loaded Executable

### Purpose

Slot 25 is the front door for producing a launchable program. The wrapper unpacks the program (StableHLO/MLIR bytes or an `XlaComputation` proto) and a `CompileOptionsProto`, hands them to the wrapped client, and boxes the returned `PjRtLoadedExecutable` into a `PJRT_LoadedExecutable*`. For the TPU client this drives `TpuClient::CompileAndLoad` → `xla::PjRtCompile` → the jellyfish JIT — covered on the compile pages; this wrapper owns only the C-ABI marshaling.

### Entry Point

```text
PJRT_Client_Compile (0x0F861820, slot 25)
  ── ActualStructSizeIsGreaterOrEqual("PJRT_Client_Compile_Args", …)
  ── parse program (format tag: "hlo" / "mlir" / StableHLO bytecode)
  ── CompileOptions::FromProto(CompileOptionsProto)
  ── (*client_vtable)(…)  ──▶  xla::TpuClient::CompileAndLoad   [compile pages]
  └─ args->executable = new PJRT_LoadedExecutable(loaded)        ── 0x48 B box
```

### Considerations

- **Cache.** The compile path is content-addressed; identical HLO + options + topology can hit the on-disk/in-memory cache instead of re-running the JIT. The keying and store are on [Compilation Cache](../compiler/compilation-cache.md).
- **Phased compile.** A host can drive compilation in explicit phases through the PhaseCompile extension rather than this one-shot slot; see [PhaseCompile Extension](ext-compile-phasecompile.md).
- **Topology.** Compilation needs a `PJRT_TopologyDescription` (TPU pod/slice geometry) to choose device assignments; that handle and its accessors are on [Topology Description](ext-topology-description.md).

---

## Execute: `PJRT_LoadedExecutable_Execute`

### Purpose

Slot 60 is the throughput-critical entry: it launches a loaded program onto one or more TPU devices. It is the largest wrapper on this surface (1914 decompiled lines @ `0x0F869B40`) because it must marshal a ragged 2-D array of input `PJRT_Buffer*` into C++ buffer vectors, translate a 14-field `PJRT_ExecuteOptions`, fork between the multi-device and single-`execute_device` virtuals, and pack a 2-D array of output `PJRT_Buffer*` plus optional per-device completion `PJRT_Event*` back out — all while honoring the `struct_size` of two nested structs.

### `_Args` and `PJRT_ExecuteOptions` layout

The wrapper reads the outer `PJRT_LoadedExecutable_Execute_Args` (current `struct_size` = 80 B = 10 qwords) and the nested `PJRT_ExecuteOptions` it points at (current `struct_size` = 112 B = 14 qwords). Offsets below are recovered directly from the field reads in the decompiled body.

| Struct | Off | Field | Meaning (recovered) |
|---|---|---|---|
| Execute_Args | `+0x00` | `struct_size` | guard input; min 34, cur 80 |
| Execute_Args | `+0x08` | `priv` / extension | walked for an extension ID == 1 (lines 257-268) |
| Execute_Args | `+0x10` | `executable` | `PJRT_LoadedExecutable*` (`v117+16` = wrapped ptr) |
| Execute_Args | `+0x18` | `options` | `PJRT_ExecuteOptions*` (`v14`) |
| Execute_Args | `+0x40` | `num_devices` | replica/partition count; CHECK'd against callbacks |
| Execute_Args | `+0x48` | `num_args` | per-device argument count |
| Execute_Args | `+0x60` | `argument_lists` | `PJRT_Buffer*** ` — `[device][arg]` (line 468-469) |
| Execute_Args | `+0x68` | `output_lists` | `PJRT_Buffer*** ` out — `[device][output]` |
| Execute_Args | `+0x70` | `device_complete_events` | optional `PJRT_Event**` out, one per device |
| Execute_Args | `+0x78` | `execute_device` | optional `PJRT_Device*`; non-null ⇒ single-device fork |
| ExecuteOptions | `+0x00` | `struct_size` | guard input; min 19, cur 112 |
| ExecuteOptions | `+0x30` | `launch_id` | int, copied into the run state (line 311) |
| ExecuteOptions | `+0x50` | `context` (str ptr) | optional `const char*`, `strlen`'d for the TraceMe span (line 312-316) |
| ExecuteOptions | (vec) | `send_callbacks` | CHECK `size() == num_devices` (line 954) |
| ExecuteOptions | (vec) | `recv_callbacks` | CHECK `size() == num_devices` (line 1215) |

> **GOTCHA —** the two `struct_size` checks are chained in one expression (line 250): the outer `Execute_Args` is validated first, then `**(a1+24)` — the *nested* `PJRT_ExecuteOptions` — is validated as a second `ActualStructSizeIsGreaterOrEqual("PJRT_ExecuteOptions", 19, 112, …)`. A host that passes a valid outer struct but a too-small `options` struct still gets a `PJRT_Error*`, not a crash. A reimplementation that validates only the outer struct reads past the caller's `options` buffer.

### Algorithm

```c
function PJRT_LoadedExecutable_Execute(args):                 // 0xf869b40
    if !ActualStructSizeIsGreaterOrEqual(                      // line 249
            "PJRT_LoadedExecutable_Execute_Args", 34, 80, args->struct_size)
       || !ActualStructSizeIsGreaterOrEqual(                   // line 250 (nested)
            "PJRT_ExecuteOptions", 19, 112, args->options->struct_size):
        return new PJRT_Error{status}                          // line 253

    opts = TranslateExecuteOptions(args->options)             // launch_id, context,
                                                              //   send/recv callbacks
    TraceMe span("PJRT_LoadedExecutable_Execute")             // if g_trace_level>0, line 274
    exec = args->executable->wrapped                          // *(args+16)

    if args->execute_device == NULL:                          // ---- multi-device ----
        // marshal argument_lists[device][arg] -> Span<vector<PjRtBuffer*>>
        // (CHECK send_callbacks.size()==num_devices, recv_callbacks.size()==num_devices)
        out = xla::PjRtLoadedExecutable::Execute(             // line 1624 (vtable virtual)
                  exec, args->argument_lists, opts,
                  &returned_futures)                          // StatusOr<vector<vector<...>>>
        if !out.ok(): return new PJRT_Error{out.status}
        // CHECK returned_futures.size() == num_devices (line 1273)
        write out -> args->output_lists[device][output]       // new PJRT_Buffer per leaf
        if args->device_complete_events:
            args->device_complete_events[d] = new PJRT_Event{returned_futures[d]}

    else:                                                     // ---- single device ----
        if args->num_devices != 1:                            // line 1648
            return new PJRT_Error{InvalidArgument(            //   pjrt_c_api_wrapper_impl.cc:2382
                "num_devices and corresponding output list sizes must be 1 "
                "when calling …Execute with non-null execute_device. Got num_devices=%i")}
        if opts.has_send_or_recv_callbacks():                 // line 1823
            return new PJRT_Error{Unimplemented(
                "…doesn't support using send/recv callbacks with `execute_device`.")}
        copts = (*inner_exec.vtable[168])(inner_exec)         // GetCompileOptions() (line 1671-1672)
                                                              //   inner_exec = executable() via +32
        if copts.compile_portable_executable:                 // v244 == 1 (line 1677)
            out = (*exec.vtable[80])(exec, args->argument_lists[0], opts, …)  // ExecutePortable
        else:
            out = (*exec.vtable[72])(exec, args->argument_lists[0], opts, …)  // ExecuteSharded
        write out -> args->output_lists[0][output]
        if args->device_complete_events:
            args->device_complete_events[0] = new PJRT_Event{future}

    return NULL                                               // success: no PJRT_Error
```

### The dispatch fork

The single most important branch is `execute_device`. When it is `NULL`, the host is asking for the executable's full replica/partition fan-out, and the wrapper calls the multi-device `xla::PjRtLoadedExecutable::Execute` (line 1624), which returns `StatusOr<vector<vector<unique_ptr<PjRtBuffer>>>>` — outer index = device, inner = output. When it is non-`NULL`, the host wants a single device's slice; the wrapper enforces `num_devices == 1`, rejects send/recv callbacks, then reads the executable's `CompileOptions` (the inner `PjRtExecutable`'s `GetCompileOptions()` virtual at vtable `+168`, line 1671-1672) and branches on `compile_portable_executable` (line 1677): if set, it calls `ExecutePortable` (vtable +80); otherwise `ExecuteSharded` (vtable +72). Both return a flat `StatusOr<vector<unique_ptr<PjRtBuffer>>>`.

> **QUIRK —** the per-replica / per-partition fan-out is **not** a loop in this wrapper. The wrapper hands the whole 2-D `argument_lists` to the C++ `Execute` and lets the runtime fan out (the runtime's `ExecutePrepare` / `ExecuteLaunch` / `ExecuteSharded`-share path does the per-device dispatch — see [Load Program and Enqueue](../runtime/load-program-enqueue.md)). The C-ABI wrapper's only loops are the marshaling loops that copy buffer pointers in and out. A reimplementer who fans out at the C-ABI layer duplicates work the runtime already does and breaks the single-`Execute`-call completion-event semantics.

> **GOTCHA —** the marshaling uses the `0xAAAAAAAAAAAAAAAB` reciprocal-of-3 magic (line 1610) to recover element counts from a `vector<vector<unique_ptr<PjRtBuffer>>>` whose 24-byte inner-vector stride is 3 qwords. This is the standard libc++ `vector` size computation, not a custom container; a reimplementation that allocates `output_lists` with the wrong leaf count silently truncates outputs.

### Outputs and completion events

On success the wrapper allocates a fresh `PJRT_Buffer` (boxing a `unique_ptr<xla::PjRtBuffer>`) for every leaf of the result and writes it into the caller-provided `output_lists[device][output]` array. If `device_complete_events` is non-null, it allocates one `PJRT_Event` per device wrapping the corresponding `tsl::Future<void>` (the TPU client mints these via `TrackFuture` / `CreateProfiledFuture`); the host awaits them through slots 13/14. The `PJRT_Buffer` lifecycle and the `PJRT_Event` model are on [Buffer and Memory](buffer-and-memory.md) and [Events and Async](events-and-async.md).

---

## Serialize and Deserialize+Load

### Purpose

`PJRT_Executable_Serialize` (slot 54) turns a compiled `PJRT_Executable` into a byte string the host can persist or ship; `PJRT_Executable_DeserializeAndLoad` (slot 61) reverses it into a device-bound `PJRT_LoadedExecutable` on *this* client. Together they are the AOT/cache path: a host can compile once, serialize, and on a later run deserialize-and-load instead of recompiling. The serialized form round-trips through the SE-portable proto the TPU topology `Deserialize` also consumes.

### Algorithm — Serialize

```c
function PJRT_Executable_Serialize(args):                     // 0xf86c5a0
    if !ActualStructSizeIsGreaterOrEqual(
            "PJRT_Executable_Serialize_Args", 30, 56, args->struct_size):
        return new PJRT_Error{status}
    bytes = std::string{}
    (*exec.vtable[144])(&bytes)        // SerializeExecutable -> string (line 29)
    holder = new PJRT_SerializedExecutable{move(bytes)}        // 0x18 B (line 38)
    args->serialized_bytes   = holder.data()                  // args[3] (+0x18)
    args->serialized_bytes_size = holder.size()               // args[4] (+0x20)
    args->serialized_executable = holder                      // args[5] (+0x28)
    args->serialized_executable_deleter = $_0::__invoke        // args[6] (+0x30)
    return NULL
```

### Algorithm — DeserializeAndLoad

```c
function PJRT_Executable_DeserializeAndLoad(args):            // 0xf86cc40
    if !ActualStructSizeIsGreaterOrEqual(
            "PJRT_Executable_DeserializeAndLoad_Args", 39, 64, args->struct_size):
        return new PJRT_Error{status}
    data    = args->serialized_executable          // *(args+24)  (line 47)
    size    = args->serialized_executable_size     // *(args+32)  (line 44)
    options = args->overridden_serialized_compile_options  // *(args+48) (line 50)
    proto   = CompileOptionsProto{}
    if options && !proto.ParseFrom(options):
        return new PJRT_Error{MakeErrorImpl(                  // line 72,
            "PJRT_Client_Compile: failed to deserialize CompileOptionsProto",
            …, pjrt_c_api_wrapper_impl.cc:1113)}              //   shared compile helper
    copts = CompileOptions::FromProto(proto)                 // line 67
    loaded = (*client.vtable)(client, data, size, copts)     // deserialize + load
    if !loaded.ok(): return new PJRT_Error{loaded.status}
    args->loaded_executable = new PJRT_LoadedExecutable(loaded)  // 0x48 B (line 175), args+40
    return NULL
```

> **QUIRK —** `DeserializeAndLoad`'s error message names `PJRT_Client_Compile`, not `…DeserializeAndLoad` (decompile line 72, source `pjrt_c_api_wrapper_impl.cc:1113`). The `CompileOptionsProto` parsing is a *shared helper* used by both compile and deserialize, and the diagnostic carries the helper's name. A reimplementer should not key error handling off the string; it is the same code path.

> **GOTCHA —** `Serialize` returns the bytes by aliasing into a heap `PJRT_SerializedExecutable` and handing back a deleter function pointer (`args[6]`). The host owns the bytes only until it invokes that deleter; the `PJRT_Executable` itself can be destroyed independently. A reimplementation that returns a pointer into the `PjRtExecutable`'s own storage (rather than a separate holder) creates a dangling pointer once the executable is freed.

> **NOTE —** deserialization compatibility is gated by the client config key `executable_compatibility_check_on_deserialization` (parsed in [`PJRT_Client_Create`](client-and-device.md)). When set, the client validates the serialized executable's ABI/topology fingerprint before loading; a mismatch surfaces as a returned `PJRT_Error`.

---

## Metadata Accessors

### Purpose

A cluster of small wrappers expose the compiled program's shape and resource metadata without launching it. They share one shape: guard, dispatch one virtual (or read one cached field), pack a scalar or a `(ptr, len)` pair back into the `_Args` struct. The host calls them to size output buffers and to route outputs to the right memory space before `Execute`.

### Name (slot 46)

```c
function PJRT_Executable_Name(args):                          // 0xf866860
    if !ActualStructSizeIsGreaterOrEqual(
            "PJRT_Executable_Name_Args", 25, 40, args->struct_size):
        return new PJRT_Error{status}
    (name_ptr, name_len) = (*exec.vtable[40])(exec)           // name(), line 14
    args->executable_name      = name_ptr                     // args[3] (+0x18)
    args->executable_name_size = name_len                     // args[4] (+0x20)
    return NULL
```

### NumOutputs (slot 49)

```c
function PJRT_Executable_NumOutputs(args):                    // 0xf866a40
    if !ActualStructSizeIsGreaterOrEqual(
            "PJRT_Executable_NumOutputs_Args", 31, 32, args->struct_size):
        return new PJRT_Error{status}
    if !EnsureExecutableOutputDimensionsPopulated(exec):      // 0xf866ac0, line 11
        return new PJRT_Error{status}                         //   lazily computes & caches
    args->num_outputs = exec->cached_num_outputs              // *(exec+272), line 13
    return NULL
```

> **QUIRK —** `NumOutputs`, `OutputElementTypes` (slot 95), and `OutputDimensions` (slot 96) all go through `EnsureExecutableOutputDimensionsPopulated` (`0xf866ac0`), which computes the per-output shape table *once* and caches it on the executable (the count lands at offset `+272`). The first of these three calls pays the `GetOutputShapes` cost; the rest read the cache. A reimplementation that recomputes per call wastes work on the hot metadata path JAX hits every trace.

### OutputMemoryKinds (slot 52)

```c
function PJRT_Executable_OutputMemoryKinds(args):             // 0xf869520
    if !ActualStructSizeIsGreaterOrEqual(
            "PJRT_Executable_OutputMemoryKinds_Args", 38, 48, args->struct_size):
        return new PJRT_Error{status}
    lock guard(exec->mutex /* exec+56 */)                     // line 64
    if !exec->memory_kinds_cached /* exec+480 */:             // line 67
        kinds = (*exec.vtable[104])(exec)                     // GetOutputMemoryKinds, line 70
        if kinds.size() != 1:                                 // line 187
            return new PJRT_Error{Unimplemented(
                "MPMD execution not supported by PJRT C API "
                "(in function PJRT_Executable_GetOutputMemoryKinds).")}
        exec->cache = kinds[0]; exec->memory_kinds_cached = 1  // line 268
    args->memory_kinds = exec->cache                          // (ptr array, sizes)
    return NULL
```

> **GOTCHA —** `OutputMemoryKinds` rejects any executable whose `GetOutputMemoryKinds()` yields more than one module's worth of kinds with `"MPMD execution not supported by PJRT C API"` (line 187). The C-API surface is **SPMD-only**: a single module replicated across devices. Multi-program-multi-data executables (distinct programs per device) cannot be queried — or executed — through this surface. The cache is guarded by a per-executable `absl::Mutex` at `exec+56` with a `bool` flag at `exec+480`; concurrent first-callers serialize.

### The accessor catalog

The remaining accessors follow the identical guard-dispatch-pack shape; only the virtual and the packed type differ.

| Slot | Field | Returns | Wrapped virtual |
|---|---|---|---|
| 47 | NumReplicas | `size_t` | `num_replicas()` |
| 48 | NumPartitions | `size_t` | `num_partitions()` |
| 50 | SizeOfGeneratedCodeInBytes | `int64` | `SizeOfGeneratedCodeInBytes()` |
| 51 | GetCostAnalysis | named-value list | `GetCostAnalysis()` |
| 95 | OutputElementTypes | `PJRT_Buffer_Type[]` | cached (via Ensure…) |
| 96 | OutputDimensions | dim spans + sizes | cached (via Ensure…) |
| 99 | Fingerprint | `(ptr, len)` | `FingerprintExecutable()` |
| 101 | GetCompiledMemoryStats | `PJRT_Executable_GetCompiledMemoryStats` fields | `GetCompiledMemoryStats()` |
| 129 | GetCompileOptions | serialized `CompileOptionsProto` | `GetCompileOptions()` |
| 139 | ParameterMemoryKinds | `(ptr array, sizes)` | `GetParameterMemoryKinds()` |

> **NOTE —** `GetCompileOptions` (slot 129) round-trips the *original* `CompileOptions` back out as a serialized `CompileOptionsProto` — the same proto `DeserializeAndLoad` consumes. It is how a host recovers the build settings of an executable it received pre-compiled, so it can deserialize-and-load a serialized copy with matching options.

---

## Considerations

- **Error surface.** Every wrapper has exactly one failure shape: on a guard miss or a wrapped-virtual error it `operator new`s an 8-byte `PJRT_Error` holding the `absl::Status` and returns its pointer; on success it returns `NULL`. There are no fatal `CHECK`s on the *host-facing* contract except the internal consistency checks inside `Execute` (callback-count vs `num_devices`, `returned_futures.size()` vs `num_devices`), which are `LogMessageFatal` because a host that violates them has corrupted the args struct, not merely passed bad data.
- **Threading.** The wrappers themselves are stateless and reentrant; the only shared state is the per-executable memory-kinds cache (`OutputMemoryKinds`), guarded by an `absl::Mutex`. Concurrency on the `Execute` hot path is bounded *below* the wrapper by the per-device `xla::Semaphore` (`max_inflight_computations`), not here.
- **What is not on this page.** The actual device enqueue (`CommonPjRtLoadedExecutable::Execute` → `ExecutePrepare` → `ExecuteLaunch` → `tpu::System::Execute`), input-buffer pinning / output-buffer donation (`AllocateOutputBuffersWithInputReuse`, `InferDispatchInfo`), and the `TpuEventIssuer` sequence-point ordering all live below the virtual calls — on the runtime pages cross-referenced below. This page stops at the C-ABI boundary.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::TpuLoadedExecutable` | The C++ object behind `PJRT_LoadedExecutable`; the `Execute` virtual the wrapper dispatches into |
| `xla::TpuExecutable` | The compiled-program object behind `PJRT_Executable`; serialize / shape / cost virtuals |
| `xla::CommonPjRtLoadedExecutable` | Framework that owns the `ExecutePrepare` → `ExecuteLaunch` dispatch the runtime pages document |
| `pjrt::ActualStructSizeIsGreaterOrEqual` | The per-entry version guard shared with every other PJRT wrapper |
| `pjrt::CreatePjrtApi` | The initializer that planted these symbols into the vtable slots |

## Cross-References

- [API Vtable Reconstruction](api-vtable-reconstruction.md) — the slot table these entries occupy (45..62, 95..96, 99, 101, 122, 129, 135, 139); the backward-compat guard and the populated-vs-injected map
- [PJRT Overview](overview.md) — where the executable surface sits in the plugin lifecycle
- [Client and Device](client-and-device.md) — `PJRT_Client_Create` (the injected slot that builds the `TpuClient` these wrappers dispatch through) and the deserialize-compat config key
- [Buffer and Memory](buffer-and-memory.md) — the `PJRT_Buffer` lifecycle for `Execute`'s input/output lists
- [Events and Async](events-and-async.md) — the `PJRT_Event` model behind `device_complete_events` and returned futures
- [PhaseCompile Extension](ext-compile-phasecompile.md) — multi-phase compilation as an alternative to one-shot `PJRT_Client_Compile`
- [Topology Description](ext-topology-description.md) — the `PJRT_TopologyDescription` that `Compile` and `DeserializeAndLoad` consume
- [ExecuteAsyncOnStream](../runtime/execute-async-on-stream.md) — the *legacy* StreamExecutor execution path (LocalClient / Service); the PJRT path here bypasses it
- [Load Program and Enqueue](../runtime/load-program-enqueue.md) — the device enqueue lower half the `Execute` virtual ultimately drives
- [Compilation Cache](../compiler/compilation-cache.md) — content-addressed keying that `Compile` and the serialize round-trip feed
