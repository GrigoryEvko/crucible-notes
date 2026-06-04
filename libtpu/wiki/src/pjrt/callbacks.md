# PJRT Callbacks & Pre-Fatal Hook

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. PJRT C-API version **0.103**. VA == file analysis address. Other versions will differ.*

## Abstract

This page is the **C-ABI callback registration layer** of the libtpu PJRT plugin: the function-pointer surfaces a framework (JAX, PyTorch-XLA) hands *into* libtpu so that a running TPU program, or a dying process, can call back *out* to host code. There are two distinct callback families, and they are wired into the plugin by completely different routes. The first is the **send/recv host-callback pair** carried through `PJRT_ExecuteOptions`: arrays of `PJRT_SendCallbackInfo` / `PJRT_RecvCallbackInfo` that `PJRT_LoadedExecutable_Execute` (slot 60, `0xf869b40`) translates into C++ `xla::SendCallback` / `xla::RecvCallback` closures, which are then registered into the per-launch `xla::TpuHostTransferManager` keyed by HLO channel id. The second is the **pre-fatal error hook**: a callback the framework installs through the `callback_extension` (extension type 14) so that, just before libtpu aborts on an unrecoverable error, every registered closure runs with the failing `absl::Status` — the framework's last chance to flush trace buffers, dump telemetry, or annotate the crash.

The reference frame is upstream XLA's `xla/pjrt/c/pjrt_c_api.h`. In the open headers, `PJRT_ExecuteOptions` carries `send_callbacks` / `recv_callbacks` as `PJRT_SendCallbackInfo**` (indexed `[device][channel]`), and each `PJRT_SendCallbackInfo` is `{ int64_t channel_id; void* user_arg; PJRT_SendCallback send_callback; }` — a plain C trampoline. libtpu's `pjrt::CSendCallbackToCpp(PJRT_SendCallbackInfo const&)` (`0xf876680`) and `pjrt::CRecvCallbackToCpp(PJRT_RecvCallbackInfo const&)` wrap those C trampolines into `std::function`s whose error channel is a `PJRT_Error_Code` + message string, not a thrown exception — the boundary is pure C. The pre-fatal hook has **no upstream-stable slot**; it lives entirely inside the TPU `callback_extension` (`learning/45eac/research/pjrt/extensions/callback/callback_extension.cc`), a 40-byte struct exposing exactly two entry points: `RegisterCallback` and `InvokeCallback`.

This page owns the **C-ABI shapes and the registration plumbing**: the `PJRT_SendCallbackInfo` / `PJRT_RecvCallbackInfo` structs, the `PJRT_Chunk` wire struct, the `PJRT_Callback_RegisterCallback_Args` / `PJRT_Callback_PrefatalArgs` structs, the channel-id keying as it crosses the C boundary, and which thread fires each callback. The *mechanism below* — the channel-keyed rendezvous, the two `flat_hash_map`s inside `TpuHostTransferManager`, the fatal-on-miss, the device-side sync flag — is owned by [Host Callbacks](../runtime/host-callbacks.md); this page links to it and does not duplicate it. The buffer-ready `PJRT_Event_OnReady` callback (slot 14) is on [Events & Async](events-and-async.md). The cross-host send/recv buffer transfer is on [DMA & Cross-Host Recv](dma-and-cross-host-recv.md).

For reimplementation, the contract is:

- **The two callback families are independent.** Send/recv ride `PJRT_ExecuteOptions` and feed `SetUpHostCallbacksForDevice` → `TpuHostTransferManager`; the pre-fatal / slice-builder hooks ride the `callback_extension` and feed process-global `…CallbackState` registries. Neither path touches the other.
- **The C↔C++ shim shape.** A `PJRT_SendCallbackInfo`'s C function pointer is wrapped by `CSendCallbackToCpp` into `std::function<Status(PjRtTransferMetadata const&, PjRtChunk, size_t, bool)>`; its return value is a `PJRT_Error_Code` (enum) + message that libtpu re-encodes into `absl::Status` at `pjrt_c_api_wrapper_impl.cc:2190`. There are no C++ exceptions across the boundary.
- **The `PJRT_Chunk` wire struct.** 32 bytes: `{ void* data; size_t size; void(*deleter)(void* data, void* deleter_arg); void* deleter_arg; }`. `pjrt::ConvertToCppChunk` (`0xf8a5280`) copies all 32 bytes and replaces the C deleter with a C++ closure that calls the original C deleter on drop.
- **The pre-fatal hook ABI.** `callback_extension` (type 14, struct @ `0x224c3b60`, ctor `CreateCallbackExtension` @ `0xe6b91e0`) has two slots: `RegisterCallback(PJRT_Callback_RegisterCallback_Args*)` @ `0xe6b9220` and `InvokeCallback(PJRT_Callback_InvokeCallback_Args*)` @ `0xe6b94c0`. A `callback_type` discriminator (`1`=SliceBuilder, `2`=Prefatal) routes to `xla::SliceBuilderCallbackState` / `xla::PreFatalErrorCallbackState`.

| | |
|---|---|
| **Send/recv on ExecuteOptions** | `PJRT_LoadedExecutable_Execute` slot 60 @ `0xf869b40` → `SetUpHostCallbacksForDevice` → `xla::TpuHostTransferManager` |
| **Send C→C++ shim** | `pjrt::CSendCallbackToCpp(PJRT_SendCallbackInfo const&)`; inner error lambda @ `0xf876680` |
| **Recv C→C++ shim** | `pjrt::CRecvCallbackToCpp(PJRT_RecvCallbackInfo const&)` (drives `xla::CopyToDeviceStream`) |
| **Chunk struct decode** | `pjrt::ConvertToCppChunk(PJRT_Chunk const&)` @ `0xf8a5280` (32-byte struct) |
| **CopyToDeviceStream AddChunk** | `pjrt::PJRT_CopyToDeviceStream_AddChunk` @ `0xf86f660` (slot 83) → `xla::(anon)::TpuCopyToDeviceStream::AddChunk` @ `0xf8374e0` |
| **Callback extension (type 14)** | struct @ `0x224c3b60`, size 40; ctor `pjrt::CreateCallbackExtension` @ `0xe6b91e0` |
| **Register entry** | `pjrt::(anon)::PJRT_Callback_RegisterCallback(PJRT_Callback_RegisterCallback_Args*)` @ `0xe6b9220` (Args min=35, current=40) |
| **Invoke entry** | `pjrt::(anon)::PJRT_Callback_InvokeCallback(PJRT_Callback_InvokeCallback_Args*)` @ `0xe6b94c0` (Args min=33) |
| **Pre-fatal registry** | `xla::PreFatalErrorCallbackState` — `AddCallback` @ `0xf95dc00`, `InvokeCallbacks` @ `0xf95dc80`, ctor @ `0xf95dbe0` |
| **Slice-builder registry** | `xla::SliceBuilderCallbackState` — `AddCallback` @ `0xf95df80`, `InvokeCallbacks` @ `0xf95e000` |
| **Prefatal args struct** | `PJRT_Callback_PrefatalArgs` (min=26): `{struct_size, PJRT_Error_Code code @+8, char* msg @+16, size_t msg_len @+24}` |
| **Source roots** | `tpu_pjrt_client.cc` (send/recv), `extensions/callback/callback_extension.cc` (hooks) |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile (both families traced; structs, discriminators, struct-size envelopes, and the C↔C++ error mapping confirmed) |

---

## 1. Two Callback Families, Two Routes

A PJRT plugin is a 140-slot vtable plus an extension chain (see [PJRT API Vtable Reconstruction](api-vtable-reconstruction.md)). Callbacks reach libtpu through two of these surfaces, and a reimplementer must keep them apart.

| Aspect | Send/Recv host callbacks | Pre-fatal / slice-builder hooks |
|---|---|---|
| Where the framework passes them | `PJRT_ExecuteOptions` (argument to `PJRT_LoadedExecutable_Execute`, slot 60) | `callback_extension` (extension type 14) → `RegisterCallback` |
| C struct | `PJRT_SendCallbackInfo` / `PJRT_RecvCallbackInfo` | `PJRT_Callback_RegisterCallback_Args` |
| C→C++ shim | `CSendCallbackToCpp` / `CRecvCallbackToCpp` | inline in `PJRT_Callback_RegisterCallback` |
| Keyed by | HLO **channel id** (`int64`) | callback **type** discriminator (`1`/`2`) — no key, append-only list |
| Stored in | per-launch `TpuHostTransferManager`'s two `flat_hash_map`s | process-global `…CallbackState` `std::vector` (per `TpuClient`) |
| Lifetime | one execute launch | process / client lifetime |
| When it fires | device hits a `Send`/`Recv` op mid-execution | just before libtpu aborts (fired via `InvokeCallback` or an internal error path) |
| Firing thread | a `TpuHostTransferManager` drain thread (off the device-completion thread) | the thread that detected the fatal error |
| Owned by | [Host Callbacks](../runtime/host-callbacks.md) (mechanism); this page (C ABI) | this page |

> **GOTCHA — the pre-fatal hook is not an upstream PJRT slot.** It does not appear anywhere in the 140-slot `PJRT_Api` table. It is reachable *only* by walking the extension chain to the `callback_extension` (type 14) and calling its `RegisterCallback` function pointer. A reimplementer who searches the vtable for a "fatal" or "abort" slot will not find one. The chain must be iterated (`extension_start` → follow `.next` until type==14 or NULL); see [Extension Chain](extension-chain.md). Confidence: CONFIRMED — `CreateCallbackExtension` (`0xe6b91e0`) writes `type=14` and the two function pointers; no `PJRT_Api` slot references either.

---

## 2. Send / Recv Callbacks on `PJRT_ExecuteOptions`

### Purpose

A compiled XLA program that does outside-compilation or cross-host `send`/`recv` emits paired `Send`/`Recv` HLO ops keyed by a `channel_id`. At execute time the framework must supply a host callback per channel: a `Send` op (device→host) hands a data chunk *up* to a `SendCallback`; a `Recv` op (host→device) pulls a chunk *down* from a `RecvCallback`. These callbacks are not registered through a vtable slot — they are passed as part of the `PJRT_ExecuteOptions` struct on every `PJRT_LoadedExecutable_Execute` call.

### The C-ABI structs

The open `pjrt_c_api.h` v0.103 layout, confirmed by the wrapper symbols libtpu links against (`pjrt::CSendCallbackToCpp(PJRT_SendCallbackInfo const&)`, `pjrt::CRecvCallbackToCpp(PJRT_RecvCallbackInfo const&)`):

```c
// PJRT_Chunk — the host-side data buffer. 32 bytes; confirmed by
// pjrt::ConvertToCppChunk @ 0xf8a5280 (copies 32 bytes, re-wraps deleter).
typedef struct {
    void*  data;                                    // +0   raw bytes
    size_t size;                                    // +8   byte count
    void  (*deleter)(void* data, void* deleter_arg);// +16  C free hook
    void*  deleter_arg;                             // +24  passed to deleter
} PJRT_Chunk;

// PJRT_SendCallback fires when the device 'Send's a chunk to the host.
typedef PJRT_Error* (*PJRT_SendCallback)(
    PJRT_Chunk* chunk, PJRT_CallbackError* callback_error,
    size_t total_size_in_bytes, bool done, void* user_arg);

typedef struct {
    int64_t            channel_id;     // the HLO Send op's channel id
    void*              user_arg;       // opaque, threaded back to the callback
    PJRT_SendCallback  send_callback;  // the C trampoline
} PJRT_SendCallbackInfo;

// PJRT_RecvCallback fires when the device 'Recv's: the host pushes a
// chunk down a PJRT_CopyToDeviceStream rather than receiving one.
typedef void (*PJRT_RecvCallback)(
    PJRT_CopyToDeviceStream* stream, void* user_arg);

typedef struct {
    int64_t            channel_id;
    void*              user_arg;
    PJRT_RecvCallback  recv_callback;
} PJRT_RecvCallbackInfo;

// In PJRT_ExecuteOptions:
//   PJRT_SendCallbackInfo** send_callbacks;  // [num_devices][num_send_ops]
//   PJRT_RecvCallbackInfo** recv_callbacks;
//   size_t num_send_ops; size_t num_recv_ops;
```

> **QUIRK — "Send" is device→host, "Recv" is host→device.** The naming is from the *device program's* viewpoint, and it inverts what a host engineer expects. A `SendCallback` *receives* a `PJRT_Chunk`; a `RecvCallback` *produces* data by feeding a `PJRT_CopyToDeviceStream`. This inversion is owned and explained in detail by [Host Callbacks §2.1](../runtime/host-callbacks.md); it is repeated here only because the C struct names carry it across the ABI. Confidence: CONFIRMED.

### Algorithm — the C→C++ send shim

`CSendCallbackToCpp` builds a `std::function<absl::Status(PjRtTransferMetadata const&, PjRtChunk, size_t, bool)>` that, when run, marshals the C++ `PjRtChunk` back to a `PJRT_Chunk`, calls the user's C `send_callback`, and converts its `PJRT_Error_Code` result into an `absl::Status`. The error-mapping lambda is the byte-confirmed leaf:

```c
// pjrt::CSendCallbackToCpp(PJRT_SendCallbackInfo const&)::$_0
//   ::{lambda(PJRT_Error_Code, char const*, size_t)}::__invoke   sub_F876680
// a1 = PJRT_Error_Code (the C callback's status code)
// a2 = char* message, a3 = message length
function send_error_to_status(code, msg, msg_len):       // sub_F876680
    rep = operator new(8)                                 // the StatusOr<...> slot
    if msg_len > 0x7FFF...F6: throw length_error           // SSO bound guard
    // copy msg into a fresh std::string (SSO if <= 22 bytes, else heap):
    buf = (msg_len > 0x16) ? operator new(round_up(msg_len)) : inline_buf
    memcpy(buf, msg, msg_len); buf[msg_len] = 0
    *rep = absl::Status::MakeRep(4*code + 1, buf, msg_len, /*line*/2190,
                "third_party/tensorflow/compiler/xla/pjrt/c/pjrt_c_api_wrapper_impl.cc")
    if heap_allocated(buf): free(buf)
    return rep                                            // StatusOr<chunk-ack> holding the Status
```

> **NOTE — the canonical status encoding is `4*code + 1`.** `absl::Status::MakeRep` is called with `4*code + 1`; this is the absl tagged-pointer convention (`code` in the low bits, the inline-vs-heap flag in bit 0). The reverse direction — `absl::Status` → `PJRT_Error_Code` — uses `pjrt::StatusCodeToPjrtErrorCode` (`0xf8a3cc0`, seen in the prefatal trampoline, §3) and its inverse `pjrt::PjrtErrorCodeToStatusCode` (`0xf8a3ca0`, seen in `InvokeCallback`). Both are **identity maps** over the valid `0..16` range — the `PJRT_Error_Code` enum and `absl::StatusCode` share the same integer values, so the numbers do coincide. The converters exist only to police the boundary: `PjrtErrorCodeToStatusCode` is a bare `return a1`, and `StatusCodeToPjrtErrorCode` is `return a1` guarded by a `LOG(FATAL)` on the `INT_MIN/INT_MAX/DO_NOT_USE` sentinel codes (`pjrt_c_api_helpers.cc:251-256`). A reimplementer can pass the code through unchanged but must reject those sentinels.

### Algorithm — the recv shim and `CopyToDeviceStream`

A `RecvCallback` does not receive a chunk; it is handed a `PJRT_CopyToDeviceStream` and pushes chunks into it. `CRecvCallbackToCpp` wraps the C `recv_callback` into `std::function<void(PjRtTransferMetadata const&, unique_ptr<CopyToDeviceStream>)>`. The host feeds the stream via slot 83, `PJRT_CopyToDeviceStream_AddChunk`:

```c
// pjrt::PJRT_CopyToDeviceStream_AddChunk   sub_F86F660   (PJRT_Api slot 83)
// a1 = PJRT_CopyToDeviceStream_AddChunk_Args*  (Args min=37, current=40)
function PJRT_CopyToDeviceStream_AddChunk(args):
    if !ActualStructSizeIsGreaterOrEqual("..._AddChunk_Args", 37, 40, args->struct_size):
        return error
    stream = *args->stream                               // xla::CopyToDeviceStream*
    cpp_chunk = ConvertToCppChunk(args->chunk)            // 32-byte struct -> C++ PjRtChunk
    av = stream->vtable[2](stream, cpp_chunk)             // TpuCopyToDeviceStream::AddChunk @ 0xf8374e0
    // returns a PJRT_Event wrapping the AddChunk completion AsyncValue
```

`xla::(anon)::TpuCopyToDeviceStream::AddChunk(PjRtChunk)` (`0xf8374e0`) hands the chunk to the device-side `tpu::host_commands::CopyToDeviceStream::AddChunk` (`0x1d0a6320`), which is the bottom of the host→device path inside the transfer manager. The granule/size accounting slots (`TotalBytes` `0xf86f7e0`, `GranuleSize` `0xf86f840`, `CurrentBytes` `0xf86f8a0`) let the host callback chunk its writes to the device's required granularity.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `PJRT_LoadedExecutable_Execute` | `0xf869b40` | Slot 60; receives `PJRT_ExecuteOptions` with the callback arrays | CERTAIN |
| `xla::(anon)::SetUpHostCallbacksForDevice` | (`tpu_pjrt_client.cc`) | Translates `Span<SendCallback>`/`Span<RecvCallback>` into the per-launch `TpuHostTransferManager` maps | CONFIRMED |
| `pjrt::CSendCallbackToCpp` | inner @ `0xf876680` | Wraps a C `send_callback` into a C++ `std::function`; maps `PJRT_Error_Code`→`Status` | CONFIRMED |
| `pjrt::CRecvCallbackToCpp` | — | Wraps a C `recv_callback` into a `std::function` driving a `CopyToDeviceStream` | HIGH |
| `pjrt::ConvertToCppChunk` | `0xf8a5280` | Decodes the 32-byte `PJRT_Chunk`, re-wraps the C deleter as a C++ closure | CONFIRMED |
| `pjrt::PJRT_CopyToDeviceStream_AddChunk` | `0xf86f660` | Slot 83; host pushes a chunk into the recv stream | CONFIRMED |
| `xla::(anon)::TpuCopyToDeviceStream::AddChunk` | `0xf8374e0` | Stream-side AddChunk into the transfer manager | CONFIRMED |

> **GOTCHA — an unregistered channel is a fatal crash.** Once `SetUpHostCallbacksForDevice` has populated the `TpuHostTransferManager` maps, a device `Send`/`Recv` whose channel id has no matching callback is a `LOG(FATAL)`, not a silent drop. This is enforced one layer down, in `HandleSendChunk`/`HandleRecvChunk`; see [Host Callbacks §2.3](../runtime/host-callbacks.md). The C-ABI layer's responsibility is to ensure every `PJRT_SendCallbackInfo`/`PJRT_RecvCallbackInfo` the program needs is present in `PJRT_ExecuteOptions` before the launch. Confidence: CONFIRMED.

---

## 3. The Pre-Fatal Error Hook (`callback_extension`, type 14)

### Purpose

When libtpu detects an unrecoverable condition (a SDC checksum mismatch, a slice-builder failure, a driver-level fault), the framework wants to run cleanup *before* the process dies — flush a profiler trace, write a crash annotation, snapshot device state. PJRT has no upstream slot for this, so the TPU plugin exposes it through a private extension. The `callback_extension` (type 14) lets the framework register `std::function<void(absl::Status const&)>` closures that fire with the failing status just before the abort.

### Entry Point

```text
extension_start (PJRT_Api +0x08)  ──> walk .next to type == 14
  callback_extension @ 0x224c3b60  (size 40, built by CreateCallbackExtension @ 0xe6b91e0)
    +0x00  struct_size = 40
    +0x08  type        = 14
    +0x10  next         ──> phase_compile_extension (type 9)
    +0x18  RegisterCallback ──> pjrt::(anon)::PJRT_Callback_RegisterCallback  @ 0xe6b9220
    +0x20  InvokeCallback   ──> pjrt::(anon)::PJRT_Callback_InvokeCallback    @ 0xe6b94c0
```

### The register / invoke ABI

`RegisterCallback` and `InvokeCallback` share a `callback_type` discriminator at `+16` of their args struct:

| `callback_type` | Meaning | Registry | C++ state object |
|---|---|---|---|
| `1` | Slice-builder failure | append to `SliceBuilderCallbackState` | callback `void(SliceFailureType)` |
| `2` | Pre-fatal error | append to `PreFatalErrorCallbackState` | callback `void(absl::Status const&)` |
| other | rejected | — | `MakeErrorImpl<12>("Callback type not supported.")` |

```c
// pjrt::(anon)::PJRT_Callback_RegisterCallback   sub_E6B9220
// a1 = PJRT_Callback_RegisterCallback_Args*  (min=35, current=40)
//   +0x00 struct_size   +0x08 PJRT_Client**   +0x10 callback_type (int)
//   +0x18 callback fn ptr   +0x20 user_arg
function PJRT_Callback_RegisterCallback(args):                 // sub_E6B9220
    if !ActualStructSizeIsGreaterOrEqual("..._RegisterCallback_Args", 35, 40, args->struct_size):
        return error
    client = **args->client                                    // unwrap PJRT_Client -> TpuClient
    if client->vtable.tpu_id() != 0x83D71ADBA77968AA:          // xla::TpuId() guard
        return null                                            // wrong backend -> no-op
    if !args->callback_fn: return null
    state_base = client[81]                                    // the client's callback-state block
    switch args->callback_type:
      case 2:  // Prefatal
        st = state_base + 280                                  // PreFatalErrorCallbackState mutex
        closure = { thunk = RegisterPrefatalCallback::$_0,     // C->C++ status trampoline
                    storage = {args->callback_fn, args->user_arg} }
        xla::PreFatalErrorCallbackState::AddCallback(st, closure)   // sub_F95DC00
      case 1:  // SliceBuilder
        st = state_base + 264
        xla::SliceBuilderCallbackState::AddCallback(st, closure)    // sub_F95DF80
      default:
        return MakeErrorImpl<12>("Callback type not supported.")    // callback_extension.cc:96
    return null   // success
```

> **QUIRK — registration is silently a no-op on the wrong backend.** Before storing anything, `RegisterCallback` checks `client->tpu_id() == 0x83D71ADBA77968AA` (the magic `xla::TpuId()` constant, lazily `__cxa_guard`-initialized). If the `PJRT_Client` is not a TPU client, the function returns `null` (success) *without* registering the callback. A reimplementer who shares this extension across backends must reproduce the magic-id gate, or a non-TPU client will see "registration succeeded" yet never fire. Confidence: CONFIRMED — the same `0x83D71ADBA77968AA` guard appears in `RegisterCallback` and `InvokeCallback`.

### The prefatal trampoline — C → C++ status

The closure stored for a type-2 callback is a `RegisterPrefatalCallback::$_0` trampoline that converts the C++ `absl::Status` into the C `(PJRT_Error_Code, message)` pair the user's C callback expects:

```c
// __call_func<pjrt::(anon)::RegisterPrefatalCallback(...)::$_0>   sub_E6B9700
// a1 = __policy_storage* (holds the user C fn ptr + user_arg)
// a2 = &absl::Status (the failing status)
function prefatal_trampoline(storage, status):                 // sub_E6B9700
    code   = absl::status_internal::MapToLocalCode(status)
    c_code = pjrt::StatusCodeToPjrtErrorCode(code)             // absl code -> PJRT_Error_Code
    msg, msg_len = status.message()                            // inline-or-heap string
    user_fn = storage[0]; user_arg = storage[1]
    user_fn(/*PrefatalArgs{c_code, msg, msg_len}*/ ..., user_arg)
    status.Unref()                                            // drop our ref if heap-backed
```

### `InvokeCallback` — the framework-driven fire path

The extension also lets a *caller* fire the registered callbacks through the C ABI (used by the SDK / telemetry side to trigger a pre-fatal flush). `InvokeCallback` reverses the conversion — it takes a C `PrefatalArgs`, rebuilds an `absl::Status`, and runs every registered closure:

```c
// pjrt::(anon)::PJRT_Callback_InvokeCallback   sub_E6B94C0
// a1 = PJRT_Callback_InvokeCallback_Args*  (min=33)
//   +0x10 callback_type   +0x18 &type-specific args (here PJRT_Callback_PrefatalArgs)
function PJRT_Callback_InvokeCallback(args):                   // sub_E6B94C0
    if !ActualStructSizeIsGreaterOrEqual("..._InvokeCallback_Args", 33, 32, args->struct_size):
        return error
    if args->callback_type != 2:
        return MakeErrorImpl<12>("Callback type can not be invoked.")   // callback_extension.cc:130
    pf = args->prefatal_args                                   // PJRT_Callback_PrefatalArgs*
    if !ActualStructSizeIsGreaterOrEqual("PJRT_Callback_PrefatalArgs", 26, 32, pf->struct_size):
        return error
    code = pjrt::PjrtErrorCodeToStatusCode(pf->code)           // PJRT_Error_Code -> absl code
    status = absl::Status::MakeRep(4*code + 1, pf->msg, pf->msg_len,
                /*line*/111, "...callback_extension.cc")
    if client->tpu_id() == 0x83D71ADBA77968AA:
        xla::PreFatalErrorCallbackState::InvokeCallbacks(client[81]+280, status)  // sub_F95DC80
    status.Unref()
    return null
```

```c
// PJRT_Callback_PrefatalArgs   (Args min=26, current=32)
typedef struct {
    size_t          struct_size;   // +0
    void*           priv;          // (reserved)        -- folded into +0x08 slot in this build
    PJRT_Error_Code code;          // +8   (uint32)     failing status code
    char*           message;       // +16                error message
    size_t          message_size;  // +24
} PJRT_Callback_PrefatalArgs;
```

### `PreFatalErrorCallbackState` — the registry

The registry is a mutex-guarded `std::vector<std::function<void(absl::Status const&)>>`. It is a 32-byte object: a mutex at `+0`, the vector's `{begin, size, capacity}` triplet. Each `std::function` slot is 32 bytes (hence the `32 * count` arithmetic).

```c
// xla::PreFatalErrorCallbackState::AddCallback   sub_F95DC00
function AddCallback(this, fn):                                // sub_F95DC00
    this.mutex.lock()
    if this.size >= this.capacity:
        __emplace_back_slow_path(&this.vec, fn)                // grow + move
    else:
        memcpy(this.vec[this.size], fn, 32)                    // place the std::function inline
        this.size += 1
    this.mutex.unlock()

// xla::PreFatalErrorCallbackState::InvokeCallbacks   sub_F95DC80
function InvokeCallbacks(this, status):                        // sub_F95DC80
    this.mutex.lock()
    for slot in this.vec[0 .. this.size]:                      // 32-byte stride
        slot.invoke(&status)                                   // calls the prefatal_trampoline
    this.mutex.unlock()
    if heap_backed(status): status.Unref()
```

> **NOTE — registration order is firing order, and there is no de-registration.** `AddCallback` only appends; the extension exposes no remove/unregister entry. `InvokeCallbacks` walks the vector front-to-back under the lock. A reimplementer gets ordered, append-only, lock-serialized firing — and must accept that a registered closure lives for the client's lifetime. The same shape backs `SliceBuilderCallbackState` (`AddCallback` `0xf95df80`, `InvokeCallbacks` `0xf95e000`), differing only in the closure signature (`void(SliceFailureType)`). Confidence: CONFIRMED.

> **GOTCHA — the callbacks fire on the failing thread, holding the registry lock.** `InvokeCallbacks` runs every closure inline, on whatever thread hit the fatal condition, while still holding `this.mutex`. A pre-fatal callback that blocks, re-enters PJRT, or tries to register another callback will deadlock or stall the abort. Keep the closure short: flush, annotate, return. Confidence: CONFIRMED — `InvokeCallbacks` (`0xf95dc80`) calls each slot inside the `lock()`/`unlock()` pair.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `pjrt::CreateCallbackExtension` | `0xe6b91e0` | Builds the type-14 extension struct (size 40, two fn ptrs) | CONFIRMED |
| `pjrt::(anon)::PJRT_Callback_RegisterCallback` | `0xe6b9220` | Registers a slice-builder (`1`) or pre-fatal (`2`) callback | CONFIRMED |
| `pjrt::(anon)::PJRT_Callback_InvokeCallback` | `0xe6b94c0` | Fires the pre-fatal callbacks from the C ABI | CONFIRMED |
| `RegisterPrefatalCallback::$_0` trampoline | `0xe6b9700` | C++ `Status` → C `(code, msg)`; calls the user C fn | CONFIRMED |
| `xla::PreFatalErrorCallbackState::AddCallback` | `0xf95dc00` | Append-only, mutex-guarded register | CONFIRMED |
| `xla::PreFatalErrorCallbackState::InvokeCallbacks` | `0xf95dc80` | Fire all, in order, under the lock | CONFIRMED |
| `xla::PreFatalErrorCallbackState` ctor | `0xf95dbe0` | Zero-init the 32-byte registry (vxorps/vmovups) | CONFIRMED |
| `xla::SliceBuilderCallbackState::AddCallback` | `0xf95df80` | Slice-builder analogue (`type==1`) | CONFIRMED |
| `xla::SliceBuilderCallbackState::InvokeCallbacks` | `0xf95e000` | Slice-builder fire | CONFIRMED |
| `pjrt::StatusCodeToPjrtErrorCode` | `0xf8a3cc0` | absl code → `PJRT_Error_Code` (identity for 0–16; `LOG(FATAL)` on the `INT_MIN/INT_MAX/DO_NOT_USE` sentinels, `pjrt_c_api_helpers.cc:251-256`) | CERTAIN |
| `pjrt::PjrtErrorCodeToStatusCode` | `0xf8a3ca0` | `PJRT_Error_Code` → absl code (pure identity: `return a1`) | CERTAIN |

---

## 4. Reimplementation Notes

- **Two surfaces, no overlap.** Send/recv callbacks are arguments to `Execute` (per launch); the pre-fatal hook is an extension method (per client). A reimplementation must expose both: the `PJRT_ExecuteOptions.send_callbacks`/`recv_callbacks` arrays *and* a type-14 extension with `RegisterCallback`/`InvokeCallback`.
- **The chunk struct is the wire contract.** `PJRT_Chunk` is `{data, size, deleter, deleter_arg}` = 32 bytes. The deleter is a C function pointer; libtpu wraps it (`ConvertToCppChunk` @ `0xf8a5280`) in a C++ closure that calls it on drop. Get the field order and the deleter signature `void(void* data, void* arg)` exactly right, or buffers leak or double-free.
- **Errors cross the boundary as codes, not exceptions.** A `SendCallback` returns a `PJRT_Error*`; libtpu turns its `PJRT_Error_Code` + message into `absl::Status` via `4*code + 1` (`pjrt_c_api_wrapper_impl.cc:2190`). Pre-fatal callbacks travel the other way through `StatusCodeToPjrtErrorCode` (`0xf8a3cc0`). The two converters (`0xf8a3cc0` / `0xf8a3ca0`) are identity maps — the enum values coincide — but `StatusCodeToPjrtErrorCode` `LOG(FATAL)`s on the `INT_MIN/INT_MAX/DO_NOT_USE` sentinels, so a reimplementation must still reject those.
- **Channel id keys the send/recv path; a missing channel is fatal.** Register every channel the compiled program uses before launch. The keying and the `LOG(FATAL)`-on-miss live in [Host Callbacks](../runtime/host-callbacks.md); the C-ABI layer's job is to deliver a complete `PJRT_SendCallbackInfo`/`PJRT_RecvCallbackInfo` set in `ExecuteOptions`.
- **Gate registration on the backend id.** `RegisterCallback`/`InvokeCallback` no-op unless `client->tpu_id() == 0x83D71ADBA77968AA`. Reproduce the gate, or cross-backend registration silently does nothing.
- **The pre-fatal hook is append-only and fires under a lock on the dying thread.** No de-registration; closures fire in registration order while the registry mutex is held, on the thread that hit the error. Keep them short and non-reentrant.
- **`callback_type` is the discriminator on both register and invoke.** `1`=slice-builder, `2`=pre-fatal. `InvokeCallback` rejects anything but `2` with `"Callback type can not be invoked."`; `RegisterCallback` rejects unknown types with `"Callback type not supported."`. Honor the `ActualStructSizeIsGreaterOrEqual` envelopes (`RegisterCallback_Args` min 35, `InvokeCallback_Args` min 33, `PrefatalArgs` min 26) for backward compatibility with older callers.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::TpuHostTransferManager` | The per-launch manager the send/recv callbacks register into (mechanism on [Host Callbacks](../runtime/host-callbacks.md)) |
| `xla::PreFatalErrorCallbackState` | Process-global pre-fatal callback registry (this page's subject) |
| `xla::SliceBuilderCallbackState` | Slice-failure analogue registered through the same extension |
| `callback_extension` (type 14) | The extension struct exposing `RegisterCallback`/`InvokeCallback` |
| `PJRT_LoadedExecutable_Execute` (slot 60) | The execute entry that carries `PJRT_ExecuteOptions` with the callback arrays |
| `xla::CopyToDeviceStream` | The stream a `RecvCallback` pushes chunks into via `PJRT_CopyToDeviceStream_AddChunk` |

## Cross-References

- [Host Callbacks](../runtime/host-callbacks.md) — the channel-keyed rendezvous *mechanism* below this page: the two `flat_hash_map`s in `TpuHostTransferManager`, fatal-on-miss, the device-side sync flag. This page is the C-ABI registration above it.
- [PJRT API Vtable Reconstruction](api-vtable-reconstruction.md) — the 140-slot table and the extension chain; how `callback_extension` (type 14) and `Execute` (slot 60) sit in the surface
- [Extension Chain](extension-chain.md) — how to walk `extension_start` to reach the type-14 `callback_extension`
- [Executable Execution](executable-execution.md) — `PJRT_LoadedExecutable_Execute` (slot 60) and the `PJRT_ExecuteOptions` struct that carries the send/recv callback arrays
- [Events & Async](events-and-async.md) — `PJRT_Event_OnReady` (slot 14), the buffer-completion callback (a different callback surface from the ones on this page)
- [DMA & Cross-Host Recv](dma-and-cross-host-recv.md) — cross-host send/recv buffer transfers, the multi-host analogue of the channel send/recv path
- [PJRT Overview](overview.md) — where the callback surfaces sit in the libtpu PJRT plugin stack
