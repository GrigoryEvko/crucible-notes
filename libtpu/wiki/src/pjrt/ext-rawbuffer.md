# PJRT RawBuffer Extension (type 8)

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 745 MB, ELF x86-64, not stripped). `.text` is mapped at `0xe63c000`; for functions in `.text` the listed VA equals the file offset. Other wheel versions will differ.*

## Abstract

The **RawBuffer extension** (`PJRT_Extension_Type` id 8, `struct_size` `0x50` = 80 bytes) is libtpu's *untyped byte-level* device-memory surface. It exposes seven methods — `CreateRawAliasOfBuffer`, `Destroy`, `GetOnDeviceSizeInBytes`, `GetMemorySpace`, `CopyRawHostToDevice`, `CopyRawDeviceToHost`, `GetHostPointer` — that move and address raw bytes in HBM (or pinned host memory) by `(offset, size)` tuples, with no element type, no shape, no tiling, and no de-tiling. It is the deliberate sibling of the typed [`PJRT_Buffer` ABI](buffer-and-memory.md): where that surface validates element types, marshals dimensions, and de-tiles on readback, the RawBuffer surface is a flat `void*`-and-length DMA channel into the same underlying device allocation.

The two surfaces wrap **different C++ class hierarchies**. The typed `PJRT_Buffer` shim wraps `xla::PjRtBuffer` → `xla::CommonPjRtBufferImpl`; the RawBuffer shim wraps `xla::PjRtRawBuffer` (abstract) → `xla::CommonPjRtRawBuffer` → `xla::CommonPjRtRawBufferImpl` (the holder of the shared copy methods) → `xla::TpuRawBuffer` (concrete TPU, vtable `0x2177cfe0`) / `xla::CpuRawBuffer` (concrete CPU staging, vtable `0x21789af8`, identical ordering). The two C wrapper objects also differ in size and ownership: the typed wrapper is 272 bytes (`0x110`) and *exclusively* owns its inner buffer, whereas the RawBuffer wrapper is **16 bytes** (`0x10`) holding a `tsl::RCReference<xla::PjRtRawBuffer>` — a *shared, ref-counted* co-owner of the device allocation. That single difference is why `CreateRawAliasOfBuffer` can hand out a zero-copy alias over an already-live typed buffer.

This page owns the extension struct, its seven-method set, the 16-byte wrapper layout, the per-method args offsets and `struct_size` versioning, the `TpuRawBuffer` vtable that backs the methods, and the raw host↔device DMA semantics down to the `tpu::System::Transfer*` entry. The chain node that links this extension into the `PJRT_Api` is on [Extension Chain](extension-chain.md); the cross-memory-space copy routing and the DMA engine internals are on [DMA & Cross-Host Receive](dma-and-cross-host-recv.md); the StreamExecutor allocator bridge that ultimately backs HBM is on [Allocator Integration](../runtime/allocator-integration.md).

For reimplementation, the contract is:

- The extension struct: `struct_size` `0x50`, `type` 8, seven fn-ptr slots at `+0x18..+0x48`, populated by a single flat table-initializer creator.
- The 16-byte RawBuffer C wrapper (`{ RCReference impl@+0x00; PJRT_Client* client@+0x08 }`) and its ref-counted `Destroy`.
- Each method's `struct_size` (min, cur) literals, its args offsets, and the single `xla::PjRtRawBuffer` vtable bounce it performs.
- The raw copy semantics: an `(offset, size)` slice transfer that returns an 80-byte `PJRT_Event`, with no shape/tile transformation.
- The two device-pointer/host-pointer accessors and the rule that gates host-addressability (`pinned_host` only).

| | |
|---|---|
| **Extension type id** | 8 (`PJRT_Extension_Type` RawBuffer) |
| **Extension struct size** | `0x50` (80 bytes); 7 fn-ptr slots at `+0x18..+0x48` |
| **Creator** | `pjrt::CreateRawBufferExtension(PJRT_Extension_Base* next)` @ `0xe6f52c0` |
| **`.bss` storage** | `0x224c3990` (`raw_buffer_extension`); `next` → profiler `0x22255b98` |
| **C wrapper size** | `0x10` (16 bytes) — `{ RCReference<PjRtRawBuffer> impl@+0x00; PJRT_Client* client@+0x08 }` |
| **Concrete backing (TPU)** | `xla::TpuRawBuffer` (vtable `0x2177cfe0`, vptr base `0x2177cff0`) |
| **Copy-method holder** | `xla::CommonPjRtRawBufferImpl` (vtable `+0x28`/`+0x30` point here) |
| **Async gate** | every raw copy returns an 80-byte `PJRT_Event` wrapping a `PjRtFuture` |

---

## The Extension Struct (type 8, 80 bytes)

### Purpose

The extension struct is a flat function-pointer table sharing the common `PJRT_Extension_Base` header (`struct_size`, `type`, `_pad`, `next`) with seven raw-buffer method pointers appended. It is `.bss`-resident at `0x224c3990` and one-shot initialized on the first `GetTpuPjrtApi` call.

### Layout

```c
struct PJRT_RawBuffer_Extension {            // struct_size 0x50 (80 bytes)
    PJRT_Extension_Base base;                // +0x00 struct_size; +0x08 type=8; +0x0c _pad; +0x10 next
    /* +0x18 */ PJRT_Error* (*CreateRawAliasOfBuffer)(PJRT_RawBuffer_CreateRawAliasOfBuffer_Args*);
    /* +0x20 */ PJRT_Error* (*Destroy)               (PJRT_RawBuffer_Destroy_Args*);
    /* +0x28 */ PJRT_Error* (*GetOnDeviceSizeInBytes)(PJRT_RawBuffer_GetOnDeviceSizeInBytes_Args*);
    /* +0x30 */ PJRT_Error* (*GetMemorySpace)        (PJRT_RawBuffer_GetMemorySpace_Args*);
    /* +0x38 */ PJRT_Error* (*CopyRawHostToDevice)   (PJRT_RawBuffer_CopyRawHostToDevice_Args*);
    /* +0x40 */ PJRT_Error* (*CopyRawDeviceToHost)   (PJRT_RawBuffer_CopyRawDeviceToHost_Args*);
    /* +0x48 */ PJRT_Error* (*GetHostPointer)        (PJRT_RawBuffer_GetHostPointer_Args*);
};
```

### Creator

`pjrt::CreateRawBufferExtension` @ `0xe6f52c0` is a pure table initializer — no allocation, no branching, a single `ret`. The decompile is literal:

```c
function CreateRawBufferExtension(slot, next):       // 0xe6f52c0
    *(u64*)(slot + 0x00) = 80                          // struct_size
    *(u32*)(slot + 0x08) = 8                           // type
    *(u64*)(slot + 0x10) = next                        // chain link (arg)
    *(u64*)(slot + 0x18) = &PJRT_RawBuffer_CreateRawAliasOfBuffer
    *(u64*)(slot + 0x20) = &PJRT_RawBuffer_Destroy
    *(u64*)(slot + 0x28) = &PJRT_RawBuffer_GetOnDeviceSizeInBytes
    *(u64*)(slot + 0x30) = &PJRT_RawBuffer_GetMemorySpace
    *(u64*)(slot + 0x38) = &PJRT_RawBuffer_CopyRawHostToDevice
    *(u64*)(slot + 0x40) = &PJRT_RawBuffer_CopyRawDeviceToHost
    *(u64*)(slot + 0x48) = &PJRT_RawBuffer_GetHostPointer
    return slot
```

Because the table is fully static, the struct can live in zero-initialized `.bss` and only needs a one-shot `__cxa_guard`-protected creator call. The `next` argument is the previously-built node; RawBuffer is the *first* `.bss` node constructed inside `GetTpuPjrtApi`, so its `next` is set to the `.data`-resident Profiler extension at `0x22255b98` — the chain terminator. See [Extension Chain](extension-chain.md) for the full 17-node walk and why RawBuffer ends up at walk position 16 despite being built first.

### The 16-byte C wrapper

```c
struct PJRT_RawBuffer {                  // sizeof = 0x10 (16 bytes)
    /* +0x00 */ tsl::RCReference<xla::PjRtRawBuffer> impl;   // SHARED, ref-counted co-owner
    /* +0x08 */ PJRT_Client*                         client; // borrowed (not owned)
};
```

Both fields are byte-confirmed: `Destroy` (`0xe6f4e40`) reads the inner `RCReference` at wrapper`+0x00` and `free`s the wrapper after the refcount path; `CreateRawAliasOfBuffer` and `GetMemorySpace` read the borrowed `client` at wrapper`+0x08`. The `impl` is a `tsl::RCReference` — the wrapper participates in *shared* ownership of the underlying `xla::PjRtRawBuffer`, in contrast to the typed `PJRT_Buffer` wrapper which owns its inner `PjRtBuffer*` outright. This is the structural enabler for raw aliasing: two RawBuffer wrappers can co-own the same device allocation by sharing the `RCReference`.

> **NOTE —** do not conflate the two buffer wrappers. The typed surface ([buffer-and-memory.md](buffer-and-memory.md)) uses a 272-byte exclusively-owned wrapper; this surface uses a 16-byte ref-counted wrapper over a *different* C++ base (`xla::PjRtRawBuffer`, not `xla::PjRtBuffer`). A reimplementer who reuses one wrapper layout for both will mis-size `free` and mis-model ownership.

### Args convention

Every method's args struct follows the canonical `{ size_t struct_size; void* priv; <handle>; ... }` shape. The RawBuffer wrapper handle is at **args+0x10** (`priv` occupies `+0x08`); each scalar/pointer output is written at **args+0x18** and beyond. The first action of every wrapper is `pjrt::ActualStructSizeIsGreaterOrEqual(name, min_fields, cur_bytes, args->struct_size)` @ `0xf8a4ec0`; on mismatch the wrapper `operator new(8)`-allocates a `PJRT_Error` carrying the size status and returns it without touching the buffer.

---

## Method Set

### Slot Map

All seven methods are byte-confirmed against the decompile: the args-name string, the `(min, cur)` `struct_size` literals, the args-output offset, and the single `xla::PjRtRawBuffer` vtable offset each bounces through. The "vtable bounce" column is the offset into the inner object's vtable (object vptr base `0x2177cff0` for `TpuRawBuffer`).

| Off | Method | C symbol | Addr | min/cur | vtable bounce / backing |
|---|---|---|---|---|---|
| `0x18` | CreateRawAliasOfBuffer | `pjrt::PJRT_RawBuffer_CreateRawAliasOfBuffer` | `0xe6f4d40` | 42 / 32 | static `xla::PjRtRawBuffer::CreateRawAliasOfBuffer` @ `0xf93f540` |
| `0x20` | Destroy | `pjrt::PJRT_RawBuffer_Destroy` | `0xe6f4e40` | 27 / 24 | `RCReference` dec-ref + vtable+0x08 (deleting dtor) + `free(0x10)` |
| `0x28` | GetOnDeviceSizeInBytes | `pjrt::PJRT_RawBuffer_GetOnDeviceSizeInBytes` | `0xe6f4f20` | 42 / 32 | vtable+0x20 `GetOnDeviceSizeInBytes()` |
| `0x30` | GetMemorySpace | `pjrt::PJRT_RawBuffer_GetMemorySpace` | `0xe6f4f80` | 34 / 32 | vtable+0x10 `memory_space()` + `PJRT_Client_FindMemoryWrapper` @ `0xf8605e0` |
| `0x38` | CopyRawHostToDevice | `pjrt::PJRT_RawBuffer_CopyRawHostToDevice` | `0xe6f5040` | 39 / 56 | vtable+0x28 `CommonPjRtRawBufferImpl::CopyRawHostToDevice` @ `0xf91c640` |
| `0x40` | CopyRawDeviceToHost | `pjrt::PJRT_RawBuffer_CopyRawDeviceToHost` | `0xe6f5180` | 39 / 56 | vtable+0x30 `CommonPjRtRawBufferImpl::CopyRawDeviceToHost` @ `0xf91c780` |
| `0x48` | GetHostPointer | `pjrt::PJRT_RawBuffer_GetHostPointer` | `0xe6f4ec0` | 34 / 32 | vtable+0x18 `TpuRawBuffer::GetHostPointer` (pinned_host only) |

The args-name strings are present verbatim in `.rodata` (`"PJRT_RawBuffer_Destroy_Args"`, `"PJRT_RawBuffer_CopyRawHostToDevice_Args"`, etc.), confirming the public API names.

> **QUIRK —** the `(min, cur)` pair for the two copy methods is `(39, 56)` — `min` (the smallest accepted field count) is *larger* in field-count terms than the other methods because the copy args carry the host pointer, offset, size, and out-event. The other five methods accept `min` 27–42 with `cur` 24 or 32 bytes. The validator compares the caller's `struct_size` against `cur` (current byte size) and the field-count `min`; a caller built against an older, smaller header is accepted as long as it reaches `min`.

---

### CreateRawAliasOfBuffer (slot 0x18)

#### Purpose

Create a **raw, untyped alias** over an existing *typed* `PJRT_Buffer`. The alias is a new 16-byte RawBuffer wrapper that shares — via `RCReference` — the donor buffer's underlying device allocation. No bytes are copied; the alias is a second co-owner of the same HBM. This is how a caller obtains a byte-level handle to a buffer that was created through the typed `BufferFromHostBuffer` path.

#### Algorithm

```c
function PJRT_RawBuffer_CreateRawAliasOfBuffer(args):    // 0xe6f4d40
    if !ActualStructSizeIsGreaterOrEqual("PJRT_RawBuffer_CreateRawAliasOfBuffer_Args", 42, 32, args.struct_size):
        return new PJRT_Error{ size_status }              // operator new(8)

    // args.buffer @ +0x10 is a typed PJRT_Buffer wrapper; **(a1+0x10) = inner xla::PjRtBuffer*
    inner_typed = *(*(args + 0x10))                       // typed wrapper -> PjRtBuffer*
    sor = xla::PjRtRawBuffer::CreateRawAliasOfBuffer(inner_typed)   // 0xf93f540 -> StatusOr<RCReference>

    if sor.is_error:                                      // discriminant low bit set
        err = new PJRT_Error(8); *err = sor.status        // and Unref the StatusRep
        return err

    // success: build the 16-byte raw wrapper
    raw_wrapper = operator new(0x10)
    raw_wrapper[+0x00] = sor.value (the RCReference, move-out)
    raw_wrapper[+0x08] = *(args[+0x10] + 0x08)            // copy client from DONOR wrapper+0x08
    args[+0x18] = raw_wrapper                             // OUT
    return NULL
```

The factory `xla::PjRtRawBuffer::CreateRawAliasOfBuffer(PjRtBuffer*)` @ `0xf93f540` walks the global per-platform factory registry `xla::GetFactoryFuncs()::funcs` @ `0x224c70c8` (guard `0x224c70d0`) — each entry a `bool(*)(StatusOr<RCReference>&, PjRtBuffer*)` — and returns the first registered factory that accepts the buffer's platform. If none accepts, it builds a `StrCat` error. The client pointer for the new alias is copied from the *donor's* wrapper `+0x08`, so the alias shares the donor's `PJRT_Client`.

> **QUIRK —** the alias **shares** the donor's device allocation through the `RCReference`; it is not a copy and does not pin a second HBM region. Destroying the alias decrements the shared refcount; the device memory is freed only when the last co-owner drops. A reimplementer must route the alias's `Destroy` through the same ref-counted decrement, not a flat free of the device buffer.

#### Function Map

| Function | Addr | Role |
|---|---|---|
| `pjrt::PJRT_RawBuffer_CreateRawAliasOfBuffer` | `0xe6f4d40` | C wrapper; build raw alias wrapper |
| `xla::PjRtRawBuffer::CreateRawAliasOfBuffer` | `0xf93f540` | static factory; walks platform registry |
| `xla::GetFactoryFuncs()::funcs` | `0x224c70c8` | registry of per-platform raw-alias factories |

---

### Destroy (slot 0x20)

#### Purpose

Release the caller's reference to a RawBuffer. Because the wrapper holds a *ref-counted* `RCReference`, `Destroy` is **not a flat free** — it decrements the shared refcount and only runs the inner deleting destructor when the count reaches zero. Then it frees the 16-byte wrapper unconditionally.

#### Algorithm

```c
function PJRT_RawBuffer_Destroy(args):                   // 0xe6f4e40
    if !ActualStructSizeIsGreaterOrEqual("PJRT_RawBuffer_Destroy_Args", 27, 24, args.struct_size):
        return new PJRT_Error{ size_status }

    wrapper = args[+0x10]                                 // a1[2]
    if wrapper != NULL:
        inner = wrapper[+0x00]                            // the RCReference's pointer
        if inner != NULL:
            // canonical TSL refcount: count at inner+0x08 (4-byte)
            if inner.refcount == 1 || atomic_dec(inner+0x08) == 0:
                inner.vtable[+0x08](inner)                // deleting dtor (~PjRtRawBuffer/D0)
        free(wrapper)                                     // 16-byte wrapper, unconditional
    return NULL
```

The fast-path check `refcount == 1` skips the atomic when the caller holds the sole reference; otherwise a `lock`-prefixed decrement determines whether the device buffer is freed. The deleting destructor lives at the inner object's vtable`+0x08` (`xla::TpuRawBuffer::~TpuRawBuffer` [D0] @ `0xf83c5e0`).

> **GOTCHA —** the wrapper is freed *every time* `Destroy` is called, even when the shared refcount has not reached zero (another alias still co-owns the device memory). The wrapper free and the device-memory free are decoupled: the 16-byte C handle goes away immediately; the HBM survives until the last `RCReference` drops. Treat `Destroy` as "drop my handle," not "free the buffer."

---

### GetOnDeviceSizeInBytes (slot 0x28) and GetHostPointer (slot 0x48)

These two are minimal single-bounce accessors with identical structure: validate `struct_size`, triple-dereference the wrapper to reach the inner vtable, call one virtual method, write the result to args`+0x18`. They never allocate on the success path.

```c
function PJRT_RawBuffer_GetOnDeviceSizeInBytes(args):    // 0xe6f4f20
    // min 42, cur 32
    inner = *(args[+0x10])                                // **(a1+0x10)
    args[+0x18] = inner.vtable[+0x20].GetOnDeviceSizeInBytes()   // int64

function PJRT_RawBuffer_GetHostPointer(args):            // 0xe6f4ec0
    // min 34, cur 32
    inner = *(args[+0x10])
    args[+0x18] = inner.vtable[+0x18].GetHostPointer()   // void* or NULL
```

`GetOnDeviceSizeInBytes` returns the **concrete HBM byte count** (`xla::TpuRawBuffer::GetOnDeviceSizeInBytes` @ `0xf838880`, which reads the resolved `TpuBufferBase+0x50`). This includes any device-side tile padding — it is the *physical* allocation size, not `product(dims) * elem_size`.

`GetHostPointer` (`xla::TpuRawBuffer::GetHostPointer` @ `0xf837b60`) returns a host-dereferenceable pointer **only for `pinned_host` buffers**; for `tpu_hbm` it returns NULL. The implementation gates on the buffer's memory-space kind string (an 11-byte overlapping compare against the `"pinned_host"` magic) and, on a pinned buffer, follows the `AsyncValue` indirect-node chain to return the resolved `TpuBufferBase+0x48`.

> **QUIRK —** `GetHostPointer` returning NULL is **not** an error — it is the correct answer for any HBM-resident buffer, because HBM is not host-addressable. A caller must check for NULL and fall back to `CopyRawDeviceToHost`; treating NULL as a failure code will break on every device buffer. This mirrors the typed surface's `OpaqueDeviceMemoryDataPointer` (slot 81), which returns a raw HBM virtual address valid only on the owning device/core — see the [QUIRK on buffer-and-memory.md](buffer-and-memory.md#pjrt_memory-handle).

---

### GetMemorySpace (slot 0x30)

#### Purpose

Return the C `PJRT_Memory*` wrapper for the memory space the raw buffer lives in. The inner buffer's `memory_space()` yields a C++ `xla::PjRtMemorySpace*`; the wrapper round-trips it through the client's memory-wrapper cache to obtain the C handle.

#### Algorithm

```c
function PJRT_RawBuffer_GetMemorySpace(args):            // 0xe6f4f80
    if !ActualStructSizeIsGreaterOrEqual("PJRT_RawBuffer_GetMemorySpace_Args", 34, 32, args.struct_size):
        return new PJRT_Error{ size_status }

    inner   = *(args[+0x10])                              // **(a1+0x10)
    mem_cpp = inner.vtable[+0x10].memory_space()          // xla::PjRtMemorySpace*
    mem_c   = PJRT_Client_FindMemoryWrapper(mem_cpp, args[+0x10].client)  // 0xf8605e0; client @ wrapper+0x08
    args[+0x18] = mem_c
    if mem_c == NULL:
        return new PJRT_Error{ MakeErrorImpl<12>("Could find memory_space() for RawBuffer") }
    return NULL
```

The error path is byte-confirmed down to the literal: `absl::status_internal::MakeErrorImpl<12>` (code 12 = `Internal`) with message `"Could find memory_space() for RawBuffer"` sourced from `pjrt_c_api_raw_buffer_internal.cc`. The `client` used by the finder is the *borrowed* client at wrapper`+0x08` — the same field copied into a raw alias by `CreateRawAliasOfBuffer`. The five memory-space classes and their `kind` strings (`tpu_hbm`, `pinned_host`, `unpinned_host`, `device`, cross-pod megascale) are documented on [buffer-and-memory.md](buffer-and-memory.md#pjrt_memory-handle); this method just resolves the wrapper, it does not classify.

---

### CopyRawHostToDevice (slot 0x38) and CopyRawDeviceToHost (slot 0x40)

#### Purpose

The byte-granular transfer surface. Each copies `size` bytes between a host `void*` and an `(offset, size)` slice of the device buffer, returning an 80-byte `PJRT_Event` that fires when the DMA completes. There is **no element type, no shape, and no tiling transformation** — the bytes are moved verbatim. This is the defining contrast with the typed `ToHostBuffer` (slot 75), which de-tiles via `ShapeUtil::DeviceShapeToHostShape` before transfer.

#### Algorithm

The two directions are byte-for-byte mirror images; only the vtable offset (`+0x28` vs `+0x30`) and the host-pointer semantics (source vs destination) differ.

```c
function PJRT_RawBuffer_CopyRawHostToDevice(args):       // 0xe6f5040  (mirror: 0xe6f5180, vtable+0x30)
    if !ActualStructSizeIsGreaterOrEqual("PJRT_RawBuffer_CopyRawHostToDevice_Args", 39, 56, args.struct_size):
        return new PJRT_Error{ size_status }

    inner  = *(args[+0x10])                               // **(a1+0x10)
    host   = args[+0x18]                                  // host_src (or host_dst for D2H)
    offset = args[+0x20]                                  // device byte offset
    size   = args[+0x28]                                  // byte count

    // single bounce into the shared copy-method holder:
    future = inner.vtable[+0x28].CopyRawHostToDevice(host, offset, size)   // CommonPjRtRawBufferImpl @ 0xf91c640
                                                          // returns PjRtFuture on stack (-0x58)

    // wrap the future as an 80-byte PJRT_Event:
    event = operator new(0x50)
    event[+0x00] = future.async_value (move)             // tsl::AsyncValue*
    event[+0x08..0x18] = profiling closure 0             // AnyInvocable (NULL-policy patched if empty)
    event[+0x28..0x38] = profiling closure 1             // AnyInvocable
    event[+0x48] = 0
    args[+0x30] = event                                  // OUT: PJRT_Event*

    // tear down the temp future: run any non-empty closure dtors,
    // then AsyncValue::Destroy on refcount->0
    return NULL
```

The args layout for both:

| Offset | Field | Direction |
|---|---|---|
| `+0x00` | `struct_size` | validated first |
| `+0x10` | `buffer` | `PJRT_RawBuffer*` wrapper |
| `+0x18` | `host` | source (H2D) / destination (D2H) host pointer |
| `+0x20` | `offset` | device byte offset of the slice |
| `+0x28` | `size` | byte count to transfer |
| `+0x30` | `event` | OUT: `PJRT_Event*` (`operator new(0x50)`) |

The vtable`+0x28`/`+0x30` slots both point into `xla::CommonPjRtRawBufferImpl` (the shared copy-method holder), *not* into `TpuRawBuffer` directly — the concrete TPU/CPU classes inherit these copy methods. The middle layer in turn calls the `AndReturnEvent` variants (`TpuRawBuffer::CopyRawHostToDeviceAndReturnEvent` @ `0xf8388c0` at vtable`+0x40`, `CopyRawDeviceToHostAndReturnEvent` @ `0xf838ea0` at vtable`+0x48`), which bounds-check the slice (`ValidateSlice` @ `0xf837be0`), take a sub-view (`SliceBuffer` @ `0xf837d80`), wrap the DMA in an RAII `tpu::WithTransferRequirements`, and drive the hardware byte-mover through `tpu::System::TransferToDevice` @ `0x1d0afa20` / `TransferFromDevice` @ `0x1d0b0160`. The full three-layer DMA pipeline and the `tpu::TpuPxcDriver` byte-mover are documented on [DMA & Cross-Host Receive](dma-and-cross-host-recv.md); this page stops at the C-ABI wrapper and the first vtable bounce.

> **GOTCHA —** the `(offset, size)` slice is bounds-checked *inside* the `AndReturnEvent` layer (`ValidateSlice`), not in the C wrapper. The C wrapper performs **no** validation of `offset`/`size` against the buffer's on-device size — it forwards them straight to the inner method. A reimplementer must not assume the wrapper guards against an out-of-range slice; the guard is one layer down, and the failure surfaces as an error inside the returned event/future, not as a synchronous `PJRT_Error` from the wrapper.

#### Function Map

| Function | Addr | Role |
|---|---|---|
| `pjrt::PJRT_RawBuffer_CopyRawHostToDevice` | `0xe6f5040` | C wrapper H2D; wrap future as event |
| `pjrt::PJRT_RawBuffer_CopyRawDeviceToHost` | `0xe6f5180` | C wrapper D2H (mirror) |
| `xla::CommonPjRtRawBufferImpl::CopyRawHostToDevice` | `0xf91c640` | vtable+0x28; PjRtFuture-returning |
| `xla::CommonPjRtRawBufferImpl::CopyRawDeviceToHost` | `0xf91c780` | vtable+0x30 |
| `xla::TpuRawBuffer::CopyRawHostToDeviceAndReturnEvent` | `0xf8388c0` | vtable+0x40; bounds-check + DMA |
| `xla::TpuRawBuffer::CopyRawDeviceToHostAndReturnEvent` | `0xf838ea0` | vtable+0x48 |
| `tpu::System::TransferToDevice` | `0x1d0afa20` | hardware DMA (host→HBM) |
| `tpu::System::TransferFromDevice` | `0x1d0b0160` | hardware DMA (HBM→host) |

---

## TpuRawBuffer Backing Vtable

The seven C wrappers bounce into the concrete `xla::TpuRawBuffer` vtable @ `0x2177cfe0` (object vptr base `0x2177cff0`), reconstructed from `R_X86_64_RELATIVE` relocations. `xla::CpuRawBuffer` (vtable `0x21789af8`) has *identical* ordering — the abstract `xla::PjRtRawBuffer` base fixes the slot layout. Only the slots the C wrappers actually use are reproduced; the full vtable continues with copy/slice/async helpers used by the cross-memory-space path.

| Vtbl off | Target | Method | Used by |
|---|---|---|---|
| `+0x00` | `0xf83c5a0` | `~TpuRawBuffer()` [complete, D2] | — |
| `+0x08` | `0xf83c5e0` | `~TpuRawBuffer()` [deleting, D0] | `Destroy` |
| `+0x10` | `0xf83c640` | `memory_space()` → `this+0x10` | `GetMemorySpace` |
| `+0x18` | `0xf837b60` | `GetHostPointer()` → pinned_host only, else 0 | `GetHostPointer` |
| `+0x20` | `0xf838880` | `GetOnDeviceSizeInBytes()` → `TpuBufferBase+0x50` | `GetOnDeviceSizeInBytes` |
| `+0x28` | `0xf91c640` | `CommonPjRtRawBufferImpl::CopyRawHostToDevice(const void*, l, l)` | `CopyRawHostToDevice` |
| `+0x30` | `0xf91c780` | `CommonPjRtRawBufferImpl::CopyRawDeviceToHost(void*, l, l)` | `CopyRawDeviceToHost` |
| `+0x38` | `0xf83c660` | `OpaqueDeviceMemoryDataPointer()` → `TpuBufferBase+0x48` | (typed slot 81) |
| `+0x40` | `0xf8388c0` | `CopyRawHostToDeviceAndReturnEvent(const void*, l, l)` | copy middle layer |
| `+0x48` | `0xf838ea0` | `CopyRawDeviceToHostAndReturnEvent(void*, l, l)` | copy middle layer |

The relevant `TpuRawBuffer` / `TpuBufferBase` field offsets (decoded from the accessor bodies):

```text
TpuRawBuffer+0x10 = PjRtMemorySpace*                       (returned by memory_space())
TpuRawBuffer+0x18 = tsl::AsyncValueRef<tpu::TpuBufferBase>  (the device handle; accessors
                    BlockUntilReady then follow the indirect-node chain
                    while (state & 3) node = node->[+0x10])
resolved TpuBufferBase+0x48 = raw HBM device pointer
resolved TpuBufferBase+0x50 = on-device size in bytes
```

> **NOTE —** `memory_space()` (vtable`+0x10`) returns `this+0x10` directly — the memory space pointer is a cached field on the `TpuRawBuffer`, not a computed value. The device pointer and size, by contrast, require resolving the `AsyncValueRef<TpuBufferBase>` at `+0x18` (blocking until ready and walking the indirect chain), which is why `GetHostPointer`/`OpaqueDeviceMemoryDataPointer` can block while `GetMemorySpace` cannot.

---

## Ownership and Async Model

### Shared ref-counted ownership

The RawBuffer wrapper holds a `tsl::RCReference<xla::PjRtRawBuffer>` — the wrapper is one co-owner among possibly several. `Destroy` decrements the shared refcount and only deletes the device buffer on the last drop; `CreateRawAliasOfBuffer` adds a co-owner that shares the same allocation. This is the opposite of the typed `PJRT_Buffer` wrapper, which owns its inner `PjRtBuffer*` exclusively and destroys it outright on `Destroy`.

The practical consequence: a raw alias and its donor *share* HBM. Freeing the donor's device memory (via the typed surface's `Delete`) while a raw alias still reads it is a use-after-free at the device level that the C-ABI layer does not prevent — ownership coordination is the caller's responsibility, mediated only by the shared refcount on the `RCReference` (which guards the C++ object's lifetime, not the eager device-memory `Delete`).

### PJRT_Event-gated readiness

Both raw copies return an **80-byte `PJRT_Event`** (`operator new(0x50)`) wrapping a `PjRtFuture`: an `AsyncValue*` plus two profiling `AnyInvocable` closures and a zeroed tail. The exact field split matches the canonical event layout on [events-and-async.md](events-and-async.md). Readiness is therefore polled (`PJRT_Event_IsReady`, slot 11), awaited (`PJRT_Event_Await`, slot 13), or callback-registered (`PJRT_Event_OnReady`, slot 14) — the same machinery the typed buffer's `ReadyEvent` (slot 77) uses. The device-side completion token underneath is a `tpu::TpuEvent` (`tpu::ReadyTpuEvent` @ `0x1d0b62e0`); the middle layer converts it to a client-tracked `PjRtFuture` via `CommonPjRtClient::MakeTrackedReadyFuture` @ `0xf91c2e0`.

> **GOTCHA —** the returned event signals *transfer completion*, not buffer readiness. After `CopyRawHostToDevice` returns NULL (success), the bytes are **not yet** in HBM — the caller must await the event before assuming the device buffer reflects the host data, and before reusing/freeing the host source. A reimplementer who treats the synchronous NULL return as "transfer done" will race the DMA.

---

## Considerations

- **No type/shape safety.** The raw surface trusts the caller's `(offset, size)`. It is intended for foreign consumers that already know the byte layout — dlpack import/export, NumPy zero-copy, custom device kernels — not for general typed I/O. Use the typed [`PJRT_Buffer`](buffer-and-memory.md) surface when element type and de-tiling matter.
- **`CpuRawBuffer` parity.** The CPU staging backing class shares the vtable ordering, so the same seven methods work for CPU-staging raw buffers; only the concrete DMA path differs (`CpuRawBuffer::CopyRaw*AndReturnEvent` vs the TPU `tpu::System::Transfer*`).
- **Aliasing vs donation.** `CreateRawAliasOfBuffer` is a *zero-copy co-ownership* alias (shared `RCReference`), distinct from the typed surface's `DonateWithControlDependency` (slot 130), which *invalidates* the donor and produces a new buffer whose HBM aliases the donor's. The raw alias leaves the donor fully usable.
- **Not byte-traced (LOW).** The exact `AsyncValue`/closure field split inside the 80-byte event is taken from the sibling event page rather than re-derived here; the C wrapper's `operator new(0x50)` and the move-in sequence (async value + two `AnyInvocable` closures + zeroed `+0x48`) are byte-confirmed, but the precise semantic role of each closure offset is owned by [events-and-async.md](events-and-async.md). The `AndReturnEvent`/`tpu::System::Transfer*` chain addresses are confirmed by symbol but the per-instruction DMA body was not re-traced on this page (HIGH, deferred to [dma-and-cross-host-recv.md](dma-and-cross-host-recv.md)).

---

## Cross-References

- [PJRT Buffer ABI & Memory Layouts](buffer-and-memory.md) — the typed/shaped `PJRT_Buffer` sibling: 272-byte exclusive wrapper, de-tiling readback, the contrast this page is defined against
- [Extension Chain](extension-chain.md) — the 17-node `PJRT_Api` extension chain; how the RawBuffer node links to the Profiler terminator
- [PJRT API Overview](overview.md) — the 140-slot `PJRT_Api` and how extensions hang off `extension_start`
- [API Vtable Reconstruction](api-vtable-reconstruction.md) — how `struct_size` versioning and vtable offsets are recovered from the binary
- [DMA & Cross-Host Receive](dma-and-cross-host-recv.md) — the three-layer raw DMA pipeline beneath `CopyRaw*` and the `tpu::TpuPxcDriver` byte-mover
- [Events & Async](events-and-async.md) — the 80-byte `PJRT_Event` / `PjRtFuture` layout every raw copy returns
- [Remaining Extensions](ext-remaining.md) — the other 12 chain extensions and the construction-order rationale
- [Allocator Integration](../runtime/allocator-integration.md) — the StreamExecutor allocator bridge backing the HBM the raw buffer addresses
