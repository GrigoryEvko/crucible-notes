# TpuTransferManager Roster

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols and IDA-recovered C names quoted verbatim). `.text` VMA equals file offset. Other versions will differ.*

## Abstract

`TpuTransferManager_*` is the C-ABI cluster that backs `xla::TransferManager` across the [TfTpu C-API shim](overview.md). It is the host↔device *data-movement* surface: marshal an `xla::Literal` from the host into a TPU `xla::ShapedBuffer` (and back), push/pull streaming infeed/outfeed literals, ask the byte size of a shape on-device, linearize a literal into raw device-layout buffers, and answer the two "can I touch this buffer right now" predicates. Nineteen `extern "C"` free functions, recovered by IDA from `.rodata` references and call targets, all named `TpuTransferManager_<Method>` and all living in one tight `0xeaba0a0`–`0xeabb827` band of `.text` (one outlier, `GetInfeedLayout @ 0xf6a1a80`, sits in a different translation unit). The IDA source-path string `learning/45eac/tfrc/executor/stream_executor/tpu_transfer_manager_c_api.cc` (`GetInfeedLayout` line 28) pins the cluster's origin file.

The defining structural fact — and the one that separates this roster from [`TpuExecutor_*`](tpu-executor-roster.md) — is **how the C function reaches the real implementation**. `TpuExecutor_*` functions receive an opaque `SE_StreamExecutor*` and dispatch through *its* vtable into the deepsea driver. `TpuTransferManager_*` functions receive **no** executor handle; instead each one resolves the *singleton* `xla::TransferManager` for the TPU platform on the fly — `GetRegisteredDeepseaPlatform()` (cached behind a `GetUnderlyingDeepseaPlatform::platform` Meyers-static guard) → `xla::TransferManager::GetForPlatform(platform)` @ `0x1342f180` (a `StatusOr<TransferManager*>`) — and then bounces the call through that manager's C++ vtable at a fixed byte offset. So the whole cluster is a *resolve-then-bounce* shim: marshal the C structs into C++ via `ApiConverter::FromC`, look up the per-platform `TransferManager` singleton, invoke one vtable slot, marshal results back with `ApiConverter::ToC`, and destroy the temporaries.

This page owns the **function roster + per-function impl-symbol / vtable-slot map**. The opaque-handle / `ApiConverter::ToC`/`FromC` convention and the three-table `*ApiFn` accessor model are on [the shim overview](overview.md). The *runtime-level* infeed/outfeed queue mechanism — the PJRT-native `tpu::System` path, span chunking, blocking semantics, the on-chip queue driver — is on [Infeed / Outfeed Queues](../runtime/infeed-outfeed.md); the two `TpuTransferManager_TransferLiteral{ToInfeed,FromOutfeed}` rows here are the *legacy StreamExecutor* entry into that subsystem and that page documents them as the legacy half. The PJRT buffer/memory ABI that the device-side `ShapedBuffer` belongs to is on [PJRT Buffer & Memory](../pjrt/buffer-and-memory.md).

For reimplementation, the contract is:

- **The resolve-then-bounce idiom** — no `SE_*` handle is passed; each function resolves the per-platform `xla::TransferManager` singleton (`GetForPlatform @ 0x1342f180`) and calls one vtable slot. The C++ `TransferManager` vtable offset *is* the dispatch key.
- **The marshalling discipline** — every C argument that is a shape / layout / literal / shaped-buffer is `ApiConverter::FromC`'d into a stack C++ object before the bounce and `~Dtor`'d after; out-params come back through `ApiConverter::ToC`; `absl::Status` results are returned as a refcounted `StatusRep*` written into a caller out-pointer.
- **The vtable-slot map** — the table below pins each C function to its `xla::TransferManager` virtual offset (`+16`, `+24`, `+32`, `+40`, `+48`, `+56`, `+64`, `+72`, `+80`, `+88`, `+104`, `+112`, `+120`, `+136`), which is the single thing a reimplementer must keep byte-stable.
- **The two non-vtable members** — `New`/`Free` are trivial heap ops, and `LinearizeToBuffers` / `GetInfeedLayout` bypass the manager vtable entirely and call `xla::jellyfish` linearizer / `TransferSizeUtil` directly off the resolved `TpuTopology`.

| | |
|---|---|
| **Roster size** | 19 `extern "C"` `TpuTransferManager_*` free functions (matches overview count) |
| **Address band** | `0xeaba0a0`–`0xeabb827` contiguous (18 fns; `ReadDynamicShapes` ends at `0xeabb827`, then `TpuComputationPlacer_New @ 0xeabb840`) + `GetInfeedLayout @ 0xf6a1a80` (outlier TU) |
| **Backing C++ class** | `xla::TransferManager` (TPU subclass; resolved per call, not held) |
| **Singleton resolve** | `GetRegisteredDeepseaPlatform` → `xla::TransferManager::GetForPlatform @ 0x1342f180` (`StatusOr`) |
| **Platform cache** | `GetUnderlyingDeepseaPlatform::platform` (function-local static, `__cxa_guard`-protected) |
| **Dispatch key** | C++ `xla::TransferManager` vtable byte offset (`*(*manager + off))(manager, …)`) |
| **Marshalling** | `ApiConverter::FromC` (in) / `ToC` (out); `xla::Shape` / `Layout` / `LiteralSlice` / `MutableBorrowingLiteral` / `ShapedBuffer` temporaries |
| **Status out** | `absl::status_internal::StatusRep*` written to caller out-ptr, old value `Unref`'d |
| **Reached via** | `ExecutorApiFn` table slots (populated by [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md)) |
| **Origin file** | `learning/45eac/tfrc/executor/stream_executor/tpu_transfer_manager_c_api.cc` |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile (19/19 bodies inspected) |

> **Scope —** the per-function `ExecutorApiFn` *slot* that points at each of these (and when it is populated) belongs to [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md). The *streaming-queue mechanism* the infeed/outfeed pair bottoms out in is owned by [Infeed / Outfeed Queues](../runtime/infeed-outfeed.md). This page documents the C-ABI roster and the C++-vtable bounce only.

---

## 1. The Resolve-then-Bounce Shape of Every Function

### Purpose

Every non-trivial `TpuTransferManager_*` function has the *same* skeleton, and a reimplementer who internalises it once can read all fourteen vtable-backed members by inspecting only their slot offset and argument marshalling. There is no per-call executor handle to thread; the platform is a process-global singleton.

### Algorithm

```c
// canonical body — sub_EABA*  (e.g. GetByteSizeRequirement @ 0xEABA4C0)
function TpuTransferManager_<Method>(<C args...>):
    // 1. marshal every rich C struct arg into a stack C++ object
    Shape         host_shape;  ApiConverter::FromC(&host_shape, c_shape)    // 320-byte xla::Shape
    // (literals → MutableBorrowingLiteral/LiteralSlice; buffers → 784-byte xla::ShapedBuffer)

    // 2. resolve the per-platform TransferManager singleton (cached after first call)
    if !GetUnderlyingDeepseaPlatform::platform.guard:                       // __cxa_guard_acquire
        GetUnderlyingDeepseaPlatform::platform =
            deepsea::executor::GetRegisteredDeepseaPlatform()               // one-time
    StatusOr<TransferManager*> mgr =
        xla::TransferManager::GetForPlatform(platform)                      // 0x1342F180
    if mgr.is_error():                                                      // payload ptr != &dword_0+1
        absl::internal_statusor::ThrowBadStatusOrAccess(mgr)               // never returns on TPU

    // 3. bounce through ONE C++ vtable slot — the offset is the dispatch key
    TransferManager* m = mgr.value();
    result = (*(*(void**)m + <VTABLE_OFFSET>))(m, <unwrapped args>, host_shape)

    // 4. marshal results back out; destroy temporaries (reverse order)
    ApiConverter::ToC(&out_cpp, c_out)         // for out-param functions
    xla::Shape::~Shape(&host_shape)            // every FromC'd temp gets its ~Dtor
    return result
```

The `mgr.is_error()` check is the recovered `StatusOr` idiom: the success sentinel is a payload pointer equal to `&dword_0 + 1` (a tagged "ok" value). Anything else is a status, so the code refs/throws `ThrowBadStatusOrAccess`. On a correctly-initialised TPU platform the manager always resolves, so this branch is dead in practice but must be reproduced for ABI parity.

### Why no executor handle

`xla::TransferManager` in upstream XLA is a *per-platform* object retrieved from a static registry (`TransferManager::GetForPlatform`), not a per-device object. The TPU build keeps that model: there is exactly one TPU `TransferManager` for the process, so the C shim has nothing device-specific to pass and resolves the singleton itself. Contrast [`TpuExecutor_*`](tpu-executor-roster.md), where the executor *is* the per-device handle and must be threaded through every call.

> **QUIRK —** the manager pointer is never freed by these functions. `New`/`Free` allocate and release a 1-byte placeholder (see §2) that the host treats as the "transfer manager handle," but the *real* `xla::TransferManager` is the process singleton resolved on every call and outlives every handle. A reimplementer who tries to store device state in the `New`'d object will find it is a dummy; all state lives in the singleton.

---

## 2. Lifecycle (`New` / `Free`)

### Function Map

| Function | Address | Size | Impl | Confidence |
|---|---|---|---|---|
| `TpuTransferManager_New` | `0xeaba0a0` | 10 | `return operator new(1u);` — a 1-byte opaque placeholder handle | CERTAIN |
| `TpuTransferManager_Free` | `0xeaba0c0` | 16 | `if (h) free(h);` — releases the placeholder | CERTAIN |

```c
// TpuTransferManager_New   sub_EABA0A0
void* TpuTransferManager_New():  return operator new(1)   // dummy handle, no fields

// TpuTransferManager_Free  sub_EABA0C0
void TpuTransferManager_Free(void* h):  if (h) free(h)
```

> **GOTCHA —** the handle from `New` carries no state. The actual transfer machinery is the `GetForPlatform` singleton, lazily resolved on first method call and cached in `GetUnderlyingDeepseaPlatform::platform`. `New`/`Free` exist only so the host's `TpuTransferManager` C++ shim has an opaque `this` to pass; mismatching `New`/`Free` calls leak/double-free 1 byte, harmless to device state but a host-side allocator bug.

---

## 3. Host → Device

### Purpose

Move a host `xla::Literal` into device memory (`ShapedBuffer`), and answer the layout/size questions a caller needs before allocating that device buffer. All four bounce through the `TransferManager` vtable.

### Function Map

| Function | Address | Size | Vtable slot | C++ call (unwrapped) | Confidence |
|---|---|---|---|---|---|
| `TpuTransferManager_TransferLiteralToDeviceAsync` | `0xeaba240` | 287 | `+40` | `m->TransferLiteralToDevice(stream, LiteralSlice(lit), shaped_buf, opts=0)` | CERTAIN |
| `TpuTransferManager_GetByteSizeRequirement` | `0xeaba4c0` | 165 | `+80` | `m->GetByteSizeRequirement(host_shape)` → `int64` | CERTAIN |
| `TpuTransferManager_HostShapeToDeviceShape` | `0xeaba160` | 207 | `+24` | `m->HostShapeToDeviceShape(host_shape)` → `Shape` (out via `ToC`) | CERTAIN |
| `TpuTransferManager_ChooseCompactLayoutForShape` | `0xeaba580` | 339 | `+88` | `m->ChooseCompactLayoutForShape(host_shape)` → `StatusOr<Shape>` | CERTAIN |

### Algorithm — `TransferLiteralToDeviceAsync`

```c
// TpuTransferManager_TransferLiteralToDeviceAsync   sub_EABA240
// args: (a1=handle, a2=&SE_Stream, a3=XLA_Literal*, a4=XLA_ShapedBuffer*, a5=StatusRep** out)
ApiConverter::FromC(&lit,  a3)          // 24-byte MutableBorrowingLiteral
ApiConverter::FromC(&buf,  a4)          // 784-byte xla::ShapedBuffer
m      = GetForPlatform(platform).value()
stream = *a2                            // raw SE_Stream pointer, not converted
LiteralSlice slice(&lit)                // wrap the borrowing literal as a slice
status = (*(*m + 40))(m, stream, &slice, &buf, /*opts*/0)   // vtable +40
write_status_out(a5, status)            // *a5 = status; Unref(old) unless aliased
~LiteralBase(&slice); ~ShapedBuffer(&buf); ~MutableBorrowingLiteral(&lit)
```

The `XLA_Literal` arrives as a `MutableBorrowingLiteral` (it borrows host memory the caller still owns) and is re-wrapped as a `LiteralSlice` for the transfer call — the transfer reads, never writes, the host literal. The `SE_Stream` is passed raw (`*a2`), the only place this cluster touches a stream handle; it is the async-ordering token, the transfer enqueues against it.

> **NOTE —** `GetByteSizeRequirement` (`+80`) and `HostShapeToDeviceShape` (`+24`) are pure shape→scalar / shape→shape queries: they `FromC` only the host `Shape` (320-byte stack object), bounce, and (for `HostShapeToDeviceShape`) `ToC` the resulting device `Shape` into the out-param. They allocate no device memory and touch no stream. `ChooseCompactLayoutForShape` is the third shape-only query; its `+88` slot was read directly from the `(*(*m + 88))(…)` bounce (out `StatusRep**` first arg, `StatusOr<Shape>` shape).

---

## 4. Device → Host

### Purpose

Pull a device `ShapedBuffer` back into a host literal, and read the dynamic dimensions a device buffer carries (for dynamic-shape programs). Both are completion-callback based.

### Function Map

| Function | Address | Size | Vtable slot | C++ call (unwrapped) | Confidence |
|---|---|---|---|---|---|
| `TpuTransferManager_TransferLiteralFromDevice` | `0xeaba360` | 352 | `+32` | `m->TransferLiteralFromDevice(stream, shaped_buf, MutableBorrowingLiteral, done_cb, opts=0)` | CERTAIN |
| `TpuTransferManager_ReadDynamicShapes` | `0xeabb660` | 455 | `+48` | `m->ReadDynamicShapes(stream, shaped_buf, &out_shape)` | CERTAIN |

### Algorithm — `TransferLiteralFromDevice`

```c
// TpuTransferManager_TransferLiteralFromDevice   sub_EABA360
// args: (a1, a2=&SE_Stream, a3=XLA_ShapedBuffer*, a4=XLA_Literal*, a5=StatusRep*, a6=callback ctx)
ApiConverter::FromC(&buf, a3)           // 784-byte ShapedBuffer
ApiConverter::FromC(&lit, a4)           // MutableBorrowingLiteral (host dest)
m      = GetForPlatform(platform).value()
stream = *a2
copy_construct(&lit2, &lit); copy_construct(&lit3, &lit2)   // 2 MBL copies for the closure
// build std::function<void(absl::Status)> closure capturing (a5=StatusRep out, a6=ctx)
done_cb = { __call_func = TpuTransferManager_TransferLiteralFromDevice::$_0,
            policy      = …::__create<$_0>() }
(*(*m + 32))(m, stream, &buf, &lit3, &done_cb, /*opts*/0)   // vtable +32, async
if done_cb.policy.dtor: done_cb.policy.dtor(captured)        // tear down closure
~MutableBorrowingLiteral(×3); ~ShapedBuffer(&buf)
```

Unlike the host→device path, this one builds a real `std::function<void(absl::Status)>` completion callback (the `$_0` lambda + a `__policy_func` thunk; both lambda thunks survive in the symbol table as `_ZNSt3__u…TransferLiteralFromDeviceE3$_0E…`). The callback writes the final status into the caller's `StatusRep*` slot when the async device read completes. The double `MutableBorrowingLiteral` copy is the closure capturing the destination literal by value-of-borrow so it outlives the synchronous return.

> **NOTE —** `ReadDynamicShapes` (`+48`) reads the runtime-resolved dimensions of a dynamic-shape device buffer; it `FromC`s the `ShapedBuffer`, bounces, and `ToC`s an out `Shape`. Slot `+48` was read directly from the `call *0x30(%rax)` (`*(*m + 48)`) bounce — it sits just above `TransferLiteralToDevice` (`+40`), not between the predicates as a naïve roster ordering would suggest.

---

## 5. Infeed / Outfeed (legacy StreamExecutor path)

### Purpose

The streaming host↔device channels: enqueue a host literal into the on-chip infeed FIFO, dequeue a result literal from the outfeed FIFO. These are the **legacy** entry into the queue subsystem — they marshal through the C-shim and the `TransferManager` vtable into the deepsea driver. The **modern** PJRT-native path (`tpu::System::EnqueueInfeed`) bypasses this cluster entirely. See [Infeed / Outfeed Queues](../runtime/infeed-outfeed.md) for the queue mechanism, span chunking, and blocking semantics; this section documents only the two C-ABI rows and their vtable bounce.

### Function Map

| Function | Address | Size | Vtable slot | C++ call (unwrapped) | Confidence |
|---|---|---|---|---|---|
| `TpuTransferManager_TransferLiteralToInfeed` | `0xeabafa0` | 241 | `+56` | `m->TransferLiteralToInfeed(executor, LiteralSlice(lit))` | CERTAIN |
| `TpuTransferManager_TransferBuffersToInfeed` | `0xeabb0a0` | 638 | `+136` | infeed of pre-linearized device buffers | CERTAIN |
| `TpuTransferManager_TransferLiteralFromOutfeed` | `0xeabb320` | 260 | `+64` | `m->TransferLiteralFromOutfeed(executor, MutableBorrowingLiteral)` | CERTAIN |
| `TpuTransferManager_GetInfeedLayout` | `0xf6a1a80` | 163 | (no vtable — see §7) | `TransferSizeUtil::ChooseGoodInfeedLayout(topology, shape)` | CERTAIN |

### Algorithm — `TransferLiteralToInfeed` / `FromOutfeed`

```c
// TpuTransferManager_TransferLiteralToInfeed   sub_EABAFA0
// args: (a1, a2=&SE_StreamExecutor, a3=XLA_Literal*, a4=StatusRep** out)
ApiConverter::FromC(&lit, a3)                  // MutableBorrowingLiteral
m  = GetForPlatform(platform).value()
ex = *a2                                       // the executor IS the queue selector here
LiteralSlice slice(&lit)
status = (*(*m + 56))(m, ex, &slice)           // vtable +56  → driver EnqueueInfeed
write_status_out(a4, status); ~LiteralBase(&slice); ~MutableBorrowingLiteral(&lit)

// TpuTransferManager_TransferLiteralFromOutfeed   sub_EABB320
// args: (a1, a2=&SE_StreamExecutor, a3=XLA_Shape*, a4=XLA_Literal*, a5=StatusRep** out)
ApiConverter::FromC(&shape, a3)                // 320-byte Shape (the expected outfeed shape)
m  = GetForPlatform(platform).value()
ex = *a2
ApiConverter::FromC(&lit, a4)                  // MutableBorrowingLiteral (host dest)
status = (*(*m + 64))(m, ex, &lit)             // vtable +64  → driver DequeueOutfeed
write_status_out(a5, status); ~MutableBorrowingLiteral(&lit); ~Shape(&shape)
```

Both pass the `SE_StreamExecutor*` (`*a2`) into the vtable call — here the executor names *which device's* infeed/outfeed queue, the only role the executor plays in this cluster besides the device-transfer pair. The infeed call wraps the literal as a read-only `LiteralSlice`; the outfeed call passes a writable `MutableBorrowingLiteral` for the result. Slot `+56` lands in `DeepseaExecutor::EnqueueInfeed`, `+64` in `DequeueOutfeed` (the driver leaves are mapped on the [Infeed / Outfeed page](../runtime/infeed-outfeed.md), §legacy path).

> **CORRECTION (none) —** the [Infeed / Outfeed page](../runtime/infeed-outfeed.md) anchors the *host-side `TpuTransferManager::TransferLiteralToInfeed` C++ shim* at `0xe9721c0` and `FromOutfeed` at `0xe972660`, reached through `ExecutorApiFn()+560`/`+576`. Those are the **caller** half (the SE shim that forwards into a `*ApiFn` slot). The `0xeabafa0`/`0xeabb320` functions documented here are the **callee** half — the C-ABI implementations that slot points at. Both halves live in this binary because XLA is statically linked; do not conflate the two addresses.

> **NOTE —** `TransferBuffersToInfeed` (`0xeabb0a0`, 638 bytes) is the pre-linearized variant: instead of a literal it takes an array of already-device-layout buffers and enqueues them, skipping the linearizer. It bounces through vtable slot `+136` (`call *0x88(%rax)`) — a *separate, higher* slot than `TransferLiteralToInfeed`'s `+56`, not a shared infeed arm. The buffer pointer/length array argument (like `LinearizeToBuffers`' output) is `FromC`'d in a loop before the bounce.

---

## 6. Shape, Layout & Buffer-Access Predicates

### Purpose

The synchronous metadata side: write a tuple index table into a device buffer (so the device can find each tuple element), and the two predicates that ask whether a buffer can be read/written on the host *right now* without a device sync.

### Function Map

| Function | Address | Size | Vtable slot | C++ call (unwrapped) | Confidence |
|---|---|---|---|---|---|
| `TpuTransferManager_WriteSingleTupleIndexTable` | `0xeaba840` | 689 | `+120` | `m->WriteSingleTupleIndexTable(stream, device_addrs[], shape, &out_addr)` | CERTAIN |
| `TpuTransferManager_CanShapedBufferBeAccessedNow` | `0xeaba6e0` | 174 | `+104` | `m->CanShapedBufferBeAccessedNow(executor, shaped_buf)` → `bool` | CERTAIN |
| `TpuTransferManager_CanBufferBeAccessedNow` | `0xeaba7a0` | 141 | `+112` | `m->CanBufferBeAccessedNow(executor, device_addr)` → `bool` | CERTAIN |
| `TpuTransferManager_PlatformId` | `0xeaba0e0` | 117 | `+16` | `m->PlatformId()` → `se::Platform::Id` | CERTAIN |
| `TpuTransferManager_ResetDevices` | `0xeabb440` | 525 | `+72` | `m->ResetDevices(executors[])` | CERTAIN |

### Algorithm — the predicates and `WriteSingleTupleIndexTable`

```c
// TpuTransferManager_CanBufferBeAccessedNow   sub_EABA7A0
// args: (a1, a2=&SE_StreamExecutor, a3=SE_DeviceAddressBase*)
ApiConverter::FromC(&addr, a3)                 // 24-byte DeviceAddressBase
m  = GetForPlatform(platform).value()
return (*(*m + 112))(m, *a2, &addr)            // vtable +112 → bool

// TpuTransferManager_CanShapedBufferBeAccessedNow   sub_EABA6E0
ApiConverter::FromC(&buf, a3)                   // 784-byte ShapedBuffer
m  = GetForPlatform(platform).value()
r  = (*(*m + 104))(m, *a2, &buf)                // vtable +104 → bool
~ShapedBuffer(&buf); return r

// TpuTransferManager_WriteSingleTupleIndexTable   sub_EABA840
// builds a heap vector<DeviceAddressBase> from the C address array (24 bytes each),
ApiConverter::FromC per element into operator new(24 * count)   // FromC each region
ApiConverter::FromC(&shape, a_shape)
m  = GetForPlatform(platform).value()
(*(*m + 120))(m, stream, addrs_vec, count, &shape, &out_c_addr, opts)   // vtable +120
```

`PlatformId` (`+16`) is the simplest vtable member — no marshalling, just resolve and return the platform id integer. The two `CanBe...AccessedNow` predicates are the cheap host-side checks XLA uses to decide whether a host pointer into device-visible memory is coherent without forcing a stream sync; both take the `SE_StreamExecutor*` plus the buffer/address and return a bool. `WriteSingleTupleIndexTable` is the heaviest vtable member (689 bytes) because it builds a heap `std::vector<DeviceAddressBase>` from the C array (each element `FromC`'d into a 24-byte slot) before the `+120` bounce.

> **GOTCHA —** `CanBufferBeAccessedNow` (`+112`) takes a *single* `SE_DeviceAddressBase` (24 bytes); `CanShapedBufferBeAccessedNow` (`+104`) takes a whole `XLA_ShapedBuffer` (784 bytes) and must `~ShapedBuffer` it after the call. They are adjacent vtable slots with swapped numeric order (the shaped-buffer variant is the *lower* offset `+104`); a reimplementer who assumes monotonic naming↔offset will mis-wire the table. `ResetDevices` (`+72`) takes an array of executors; its `call *0x48(%rax)` (`*(*m + 72)`) bounce was read directly from the disassembly.

---

## 7. The Two Non-Vtable Members — Direct Linearizer Calls

### Purpose

`LinearizeToBuffers` and `GetInfeedLayout` are the cluster's odd pair: they do **not** dispatch through the `xla::TransferManager` vtable. Instead they resolve the `tpu::TpuTopology` directly and call `xla::jellyfish` machinery — the same linearizer / size-util the PJRT-native infeed path uses (see [Infeed / Outfeed Queues](../runtime/infeed-outfeed.md), §layout contract). This is where the C-shim and the modern runtime share code.

### Function Map

| Function | Address | Size | Direct callee | Confidence |
|---|---|---|---|---|
| `TpuTransferManager_LinearizeToBuffers` | `0xeabab00` | 594 | `xla::jellyfish::LiteralLinearizer::LinearizeToBuffers(topology, …)` | CERTAIN |
| `TpuTransferManager_GetInfeedLayout` | `0xf6a1a80` | 163 | `xla::jellyfish::TransferSizeUtil::ChooseGoodInfeedLayout(topology, shape)` | CERTAIN |

### Algorithm

```c
// TpuTransferManager_GetInfeedLayout   sub_F6A1A80   (tpu_transfer_manager_c_api.cc:28)
// args: (a1=XLA_Shape* in, a2=XLA_Shape* out)
ApiConverter::FromC(&shape, a1)
topology = GetTopology(GetUnderlyingDeepseaPlatform::platform)
CHECK(topology != nullptr)        // FATAL "topology != nullptr" if null
TransferSizeUtil::ChooseGoodInfeedLayout(&out_shape, topology, &shape)
ApiConverter::ToC(&out_shape, a2)
~Shape(&out_shape); ~Shape(&shape)

// TpuTransferManager_LinearizeToBuffers   sub_EABAB00
// args: (…, a3=XLA_Literal*, a4=XLA_Shape*, a5/a6 = out buffer ptr/size arrays, a8=StatusRep**)
ApiConverter::FromC(&lit,   a3)
ApiConverter::FromC(&shape, a4)
topology = platform->topology   // *(*(platform+8)+184)
status = LiteralLinearizer::LinearizeToBuffers(topology, &lit, &shape, &out_vec,
                                               InvokeObject<$_0…unique_ptr<uchar[]>>)
// hand the device-layout buffer vector back to C as two parallel new[] arrays:
*a6 = operator new(8 * count)   // sizes
*a5 = operator new(8 * count)   // pointers; each buffer memcpy'd into a fresh operator new
// (each entry copied from the linearizer's internal 56-byte-stride buffer descriptors)
```

`GetInfeedLayout` is a pure shape→layout helper (its FATAL-check source string `tpu_transfer_manager_c_api.cc:28` pins the cluster's TU). `LinearizeToBuffers` is the heavyweight: it linearizes a host literal into device-tiled byte buffers and copies each into a freshly `operator new`'d block, returning two parallel `new[]` arrays (pointers + sizes) the host must later release with `FreeBuffers`.

### `FreeBuffers` — the matching deallocator

```c
// TpuTransferManager_FreeBuffers   sub_EABAF20
// args: (ptr_array, size_array, count)
void TpuTransferManager_FreeBuffers(void** ptrs, void* sizes, int64 count):
    for i in 0..count:  if ptrs[i]: free(ptrs[i])   // each buffer
    free(ptrs)                                       // the pointer array
    if sizes: free(sizes)                            // the size array
```

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `TpuTransferManager_FreeBuffers` | `0xeabaf20` | 114 | frees the `LinearizeToBuffers` pointer array + each buffer + size array | CERTAIN |

> **GOTCHA —** `LinearizeToBuffers` allocates with `operator new` (per-buffer + two `new[]` arrays) but `FreeBuffers` releases with `free()`. On this glibc build `operator new` forwards to `malloc`, so the pair is balanced — but a reimplementer who wires `operator new`/`operator delete[]` to a *different* allocator than `malloc`/`free` will corrupt the heap. The ABI contract is "allocate so that `free()` releases it." Pair every `LinearizeToBuffers` with exactly one `FreeBuffers(ptrs, sizes, count)`.

---

## 8. Complete Vtable-Slot Map

The single table a reimplementer needs: each C function, its address, and the `xla::TransferManager` vtable byte offset it bounces through. Every offset was read directly from the decompiled `(*(*m + N))(…)` expression and cross-checked against the `call *0xNN(%rax)` disassembly, so all rows are CERTAIN.

| C function | Address | Vtable off | C++ method (inferred) | Conf |
|---|---|---|---|---|
| `New` | `0xeaba0a0` | — | `operator new(1)` | CERTAIN |
| `Free` | `0xeaba0c0` | — | `free` | CERTAIN |
| `PlatformId` | `0xeaba0e0` | `+16` | `PlatformId()` | CERTAIN |
| `HostShapeToDeviceShape` | `0xeaba160` | `+24` | `HostShapeToDeviceShape(Shape)` | CERTAIN |
| `TransferLiteralFromDevice` | `0xeaba360` | `+32` | `TransferLiteralFromDevice(…, cb)` | CERTAIN |
| `TransferLiteralToDeviceAsync` | `0xeaba240` | `+40` | `TransferLiteralToDevice(…)` | CERTAIN |
| `ReadDynamicShapes` | `0xeabb660` | `+48` | `ReadDynamicShapes(…)` | CERTAIN |
| `TransferLiteralToInfeed` | `0xeabafa0` | `+56` | `TransferLiteralToInfeed(exec, LiteralSlice)` | CERTAIN |
| `TransferLiteralFromOutfeed` | `0xeabb320` | `+64` | `TransferLiteralFromOutfeed(exec, MBL)` | CERTAIN |
| `ResetDevices` | `0xeabb440` | `+72` | `ResetDevices(executors)` | CERTAIN |
| `GetByteSizeRequirement` | `0xeaba4c0` | `+80` | `GetByteSizeRequirement(Shape)` | CERTAIN |
| `ChooseCompactLayoutForShape` | `0xeaba580` | `+88` | `ChooseCompactLayoutForShape(Shape)` | CERTAIN |
| `CanShapedBufferBeAccessedNow` | `0xeaba6e0` | `+104` | `CanShapedBufferBeAccessedNow(exec, buf)` | CERTAIN |
| `CanBufferBeAccessedNow` | `0xeaba7a0` | `+112` | `CanBufferBeAccessedNow(exec, addr)` | CERTAIN |
| `WriteSingleTupleIndexTable` | `0xeaba840` | `+120` | `WriteSingleTupleIndexTable(…)` | CERTAIN |
| `TransferBuffersToInfeed` | `0xeabb0a0` | `+136` | infeed of device buffers | CERTAIN |
| `LinearizeToBuffers` | `0xeabab00` | — (direct) | `jellyfish::LiteralLinearizer::LinearizeToBuffers` | CERTAIN |
| `FreeBuffers` | `0xeabaf20` | — (free) | n/a | CERTAIN |
| `GetInfeedLayout` | `0xf6a1a80` | — (direct) | `jellyfish::TransferSizeUtil::ChooseGoodInfeedLayout` | CERTAIN |

> **QUIRK —** the vtable offsets are *not* contiguous-by-roster-order. The C functions are emitted in source order (`New`, `Free`, `PlatformId`, …) but their slots track the `xla::TransferManager` base-class vtable layout (`+16`, `+24`, `+32`, …), which interleaves base-class virtuals the C shim does not expose. A reimplementer building the C++ `TransferManager` subclass must reproduce the *base-class* vtable order, not the C-roster order, or every slot offset above is wrong by a frame.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::TransferManager` (TPU subclass) | the C++ class every vtable-backed row dispatches into |
| `xla::TransferManager::GetForPlatform @ 0x1342f180` | the per-platform singleton resolver every function calls |
| `deepsea::executor::GetRegisteredDeepseaPlatform` | resolves the TPU `Platform`, cached in `GetUnderlyingDeepseaPlatform::platform` |
| `ApiConverter::ToC / FromC` | marshals `XLA_Shape` / `XLA_Literal` / `XLA_ShapedBuffer` / `SE_DeviceAddressBase` across the seam |
| `xla::jellyfish::LiteralLinearizer` / `TransferSizeUtil` | the direct callees of `LinearizeToBuffers` / `GetInfeedLayout` (no vtable) |
| `ExecutorApiFn` table | the function-pointer struct whose slots point at these C functions |
| `TpuExecutor_*` roster | the *contrasting* shim: passes an `SE_StreamExecutor*` handle instead of resolving a singleton |

## Cross-References

- [The TfTpu C-API Shim](overview.md) — the `*ApiFn` accessor model, opaque-handle convention, and `ApiConverter` marshalling this roster relies on
- [TpuExecutor Roster](tpu-executor-roster.md) — the contrasting per-device cluster that *does* thread an `SE_StreamExecutor*` handle through every call
- [TpuProgram Roster](tpu-program-roster.md) — the sibling serialized-program C-ABI cluster reached through the same `ExecutorApiFn` table
- [Infeed / Outfeed Queues](../runtime/infeed-outfeed.md) — the runtime queue mechanism the `TransferLiteral{ToInfeed,FromOutfeed}` rows are the legacy StreamExecutor entry into
- [PJRT Buffer & Memory](../pjrt/buffer-and-memory.md) — the PJRT buffer/memory ABI the device-side `ShapedBuffer` belongs to
- [TfTpu_Initialize Bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) — the one-time population of the `ExecutorApiFn` slots that point at these functions
