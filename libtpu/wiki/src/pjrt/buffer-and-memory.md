# PJRT Buffer ABI & Memory Layouts

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 745 MB, ELF x86-64, not stripped). `.text` is mapped at `0xe63c000`; for functions in `.text` the listed VA equals the file offset. Other wheel versions will differ.*

## Abstract

This page documents the **C-ABI buffer wrapper** in libtpu's PJRT plugin: the `PJRT_Buffer` and `PJRT_Memory` opaque handles, the host↔device staging entry point `PJRT_Client_BufferFromHostBuffer`, the readback path `PJRT_Buffer_ToHostBuffer`, the zero-allocation shape accessors (`ElementType`, `Dimensions`, `OnDeviceSizeInBytes`), the two-stage lifetime (`Delete` / `IsDeleted` / `Destroy`), and the memory-space surface (`PJRT_Buffer_Memory`, `PJRT_Buffer_CopyToMemory`, and the `PJRT_Memory` accessors). These are thin C shims over XLA's C++ `xla::PjRtBuffer` / `xla::PjRtMemorySpace` hierarchy: each shim validates the caller's `struct_size`, reads its typed args, bounces a single virtual call into the concrete `xla::CommonPjRtBufferImpl`, and marshals the result back into the args struct. A reader who knows the upstream PJRT C API (`pjrt_c_api.h`) will recognize every slot; the value here is the *exact* libtpu wrapper-object layout, the vtable offsets, and the TPU-specific behavior (HBM is not host-addressable, the strides-layout rejection, the de-tiling readback).

The wrapper is deliberately a *boundary*, not a buffer. The `PJRT_Buffer` C object is 272 bytes (`0x110`) and owns exactly one `xla::PjRtBuffer*` plus an external-reference tracking list; it carries no device bytes. The device-side bytes live behind the inner C++ buffer in HBM, allocated and laid out by the [HBM allocator](../memory/hbm-allocator.md) and the [TPU buffer byte-layout](../memory/tpu-buffer-layout.md) — **owned by those pages, not re-derived here.** This page owns the C-ABI surface, the host-buffer-semantics enum, and the transfer entry points; the untyped raw-byte sibling surface is on [RawBuffer Extension (type 8)](ext-rawbuffer.md), and the StreamExecutor allocator bridge on [Allocator Integration](../runtime/allocator-integration.md).

The page is organized by facet: first the two C wrapper-object layouts and the slot map, then each entry point (`BufferFromHostBuffer`, `ToHostBuffer`, the accessors, the lifetime pair, the memory-space surface) with its `### Algorithm` and `### Function Map`, then the `PJRT_Memory` surface, the lifetime/refcount model, and async readiness.

For reimplementation, the contract is:

- The two wrapper-object layouts: the 272-byte `PJRT_Buffer` (exclusive owner) and the `PJRT_Memory` handle, with the field offsets each slot reads.
- The `PJRT_Client_BufferFromHostBuffer_Args` field layout (37 fields, `struct_size` 120) and the `PJRT_HostBufferSemantics` enum that gates host-buffer lifetime.
- Each slot's single vtable bounce into `xla::CommonPjRtBufferImpl` and what it marshals into the args struct.
- The two-axis lifetime (`Delete` frees device memory eagerly; `Destroy` frees the C wrapper) and the external-reference pin that blocks `Delete`.

| | |
|---|---|
| **PJRT_Api slot range** | Buffer slots 63–81 + late slots 97, 105, 125, 130; `BufferFromHostBuffer` slot 27 |
| **`PJRT_Buffer` wrapper size** | `0x110` (272 bytes) — `{ PjRtBuffer* impl@+0x00; …; ext_ref list@+0xf8/+0x100/+0x108 }` |
| **Concrete inner class** | `xla::CommonPjRtBufferImpl` (vtable `0x21789ec8`, vptr base `0x21789ed8`) |
| **BufferFromHostBuffer** | `pjrt::PJRT_Client_BufferFromHostBuffer` @ `0xf8644c0`, `struct_size` cur 120 / min 37 fields |
| **ToHostBuffer** | `pjrt::PJRT_Buffer_ToHostBuffer` @ `0xf86e640` (de-tiles, returns 80-byte event) |
| **Memory-space kinds** | `tpu_hbm`, `pinned_host`, `unpinned_host`, `device` (CPU staging), cross-pod megascale |
| **Async gate** | every transfer returns an 80-byte `PJRT_Event` wrapping a `PjRtFuture` |

---

## The Two C Wrapper Objects

PJRT's C API hands the caller opaque `PJRT_Buffer*` and `PJRT_Memory*` pointers. In libtpu these are small heap objects that *wrap* a C++ inner pointer; every slot's first action is to dereference the wrapper to reach the inner `xla::PjRtBuffer*`.

> **NOTE —** there are two unrelated buffer wrappers in libtpu. The **272-byte** `PJRT_Buffer` documented here is the canonical *typed/shaped* surface. A separate **16-byte** raw-byte wrapper (`RCReference<PjRtRawBuffer>` + borrowed client) backs the RawBuffer extension and is documented on [ext-rawbuffer.md](ext-rawbuffer.md). They wrap *different* C++ class hierarchies (`xla::PjRtBuffer` vs `xla::PjRtRawBuffer`) and have *different* ownership models. Do not conflate them.

### PJRT_Buffer layout (0x110 = 272 bytes)

```c
struct PJRT_Buffer {                  // sizeof = 0x110, freed as free(wrapper, 0x110)
    /* +0x00  */ xla::PjRtBuffer* impl;              // owned EXCLUSIVELY (CommonPjRtBufferImpl)
    /* +0x08  */ PJRT_Client*     client;            // borrowed; set at construction
    /* +0x10  */ uint8_t          ...flags;          // construction-zeroed bytes
    /* ...    */ /* cached element-type / device / memory wrappers, error/status slots */
    /* +0xf8  */ ExternalReferenceHold* ext_ref_data;   // external-reference tracking list
    /* +0x100 */ size_t                 ext_ref_size;
    /* +0x108 */ size_t                 ext_ref_capacity;
};
```

The size and field offsets are byte-confirmed: `PJRT_Buffer_Destroy` (`0xf86d020`) calls `~PJRT_Buffer()` then `free(wrapper, 0x110)`, the `BufferFromHostBuffer` constructor does `operator new(0x110)` and zeroes the `+0xf0..+0x110` region (the external-ref list) plus the construction flag bytes at `+0x10`, `+0xa8`, `+0xb0`, `+0xc8`, `+0xd0`, `+0xe8`, and `Increase/DecreaseExternalReferenceCount` read/track the list at `+0xf8/+0x100/+0x108`. The wrapper *exclusively* owns `impl`: there is no reference count on the C wrapper itself; `Destroy` runs the inner destructor outright.

### PJRT_Memory handle

`PJRT_Memory` is the C handle for an `xla::PjRtMemorySpace*` (HBM / pinned-host / CPU-device). The C wrapper is produced and cached by the client, not by the buffer: `PJRT_Client_FindMemoryWrapper(PjRtMemorySpace*, PJRT_Client*)` @ `0xf8605e0` linear-scans the client's cached memory-wrapper array (count at `client+0x90`, array base at `client+0x88`) for the wrapper whose inner pointer equals the requested C++ memory space, returning NULL on miss. `PJRT_Buffer_Memory` (slot 71, `0xf86dc60`) reads the inner buffer's memory space (inner vtable+0x58 `memory_space()`) and runs that same scan *inlined*; if no wrapper matches, it returns `xla::Unimplemented("PJRT_Buffer_Memory not implemented for platform '%s'")` (absl code 12), not a successful NULL. The five memory-space classes and their `kind` strings:

| Class | `kind` string | `kind()` addr | vtable |
|---|---|---|---|
| `xla::TpuHbmMemorySpace` | `tpu_hbm` | `0xf817100` | `0x2177b478` |
| `xla::PinnedHostMemorySpace` | `pinned_host` | `0xf90c8e0` | `0x21789978` |
| `xla::UnpinnedHostMemorySpace` | `unpinned_host` | `0xf90c700` | — |
| `xla::CpuDeviceMemorySpace` | `device` (CPU staging) | `0xf90cac0` | — |
| `xla::MegaScalePjRtMemorySpace` | cross-pod / DCN | `0xe6eb460` | — |

> **QUIRK —** `tpu_hbm` is **not host-addressable**. A buffer in HBM has no valid host pointer; the host-pointer accessor (on the RawBuffer surface) returns NULL unless the buffer's memory-space kind is exactly `"pinned_host"`. Code that assumes `OpaqueDeviceMemoryDataPointer` (slot 81) yields a CPU-dereferenceable address is wrong on TPU: that pointer is a raw HBM virtual address valid only on the owning device/core, usable only by foreign device-side consumers (DMA engines, custom kernels), never by `memcpy`.

### Slot map

The 18 slots this page covers, all in the 140-slot `PJRT_Api`. Each wrapper validates `struct_size` via `pjrt::ActualStructSizeIsGreaterOrEqual(name, min_fields, cur_bytes, caller_size)` @ `0xf8a4ec0`, then reads `args+0x10` to reach the wrapper and `wrapper+0x00` to reach the inner `PjRtBuffer*`.

| Slot | Off | Method | C symbol | Addr | vtable bounce / backing |
|---|---|---|---|---|---|
| 27 | — | BufferFromHostBuffer | `PJRT_Client_BufferFromHostBuffer` | `0xf8644c0` | memory-space vtable+0x120 (allocate+stage) |
| 63 | 0x1f8 | Destroy | `PJRT_Buffer_Destroy` | `0xf86d020` | `~PJRT_Buffer()` + `free(0x110)` |
| 64 | 0x200 | ElementType | `PJRT_Buffer_ElementType` | `0xf86d220` | inner vtable+0x10 `element_type()` + `ConvertToPjRtBufferType` |
| 65 | 0x208 | Dimensions | `PJRT_Buffer_Dimensions` | `0xf86d280` | inner vtable+0x18 `dimensions()` → `{ptr,count}` |
| 69 | — | OnDeviceSizeInBytes | `PJRT_Buffer_OnDeviceSizeInBytes` | `0xf86da80` | inner vtable+0x88 `GetOnDeviceSizeInBytes()` |
| 71 | 0x238 | Memory | `PJRT_Buffer_Memory` | `0xf86dc60` | inner vtable+0x58 `memory_space()` + inlined client-side wrapper lookup |
| 72 | 0x240 | Delete | `PJRT_Buffer_Delete` | `0xf86dd80` | inner vtable+0xa0 `Delete()` (eager HBM free) |
| 73 | 0x248 | IsDeleted | `PJRT_Buffer_IsDeleted` | `0xf86dde0` | inner vtable+0xb0 `IsDeleted()` |
| 74 | 0x250 | CopyToDevice | `PJRT_Buffer_CopyToDevice` | `0xf86e360` | dst-device vtable+0x98 (default mem) + src vtable+0xb8 |
| 75 | 0x258 | ToHostBuffer | `PJRT_Buffer_ToHostBuffer` | `0xf86e640` | de-tile + inner vtable+0x78 `ToLiteral()` |
| 76 | 0x260 | IsOnCpu | `PJRT_Buffer_IsOnCpu` | `0xf86ecc0` | inner vtable+0xe8 `IsOnCpu()` |
| 77 | 0x268 | ReadyEvent | `PJRT_Buffer_ReadyEvent` | `0xf86ed20` | inner vtable+0xe0 `GetReadyFuture()` → 0x50 event |
| 79 | 0x278 | IncreaseExternalReferenceCount | `PJRT_Buffer_IncreaseExternalReferenceCount` | `0xf86ef20` | inner vtable+0x70 `AcquireExternalReference` |
| 80 | 0x280 | DecreaseExternalReferenceCount | `PJRT_Buffer_DecreaseExternalReferenceCount` | `0xf86f100` | release tracked hold |
| 81 | 0x288 | OpaqueDeviceMemoryDataPointer | `PJRT_Buffer_OpaqueDeviceMemoryDataPointer` | `0xf86f200` | inner vtable+0x70 + read hold+0x08 (raw ptr) |
| 97 | 0x308 | CopyToMemory | `PJRT_Buffer_CopyToMemory` | `0xf86e500` | inner vtable+0xb8 `CopyToMemorySpace(PjRtMemorySpace*)` |
| 105 | 0x350 | CopyRawToHost | `PJRT_Buffer_CopyRawToHost` | `0xf86de40` | inner vtable+0x90 |
| 130 | 0x410 | DonateWithControlDependency | `PJRT_Buffer_DonateWithControlDependency` | `0xf86f2e0` | inner vtable+0xd8 |

The inherited shape accessors `element_type()`/`dimensions()` point into the abstract `xla::PjRtBuffer` base (`0xe6eaac0` / `0xe6eaae0`), not into `CommonPjRtBufferImpl` — the concrete impl does not override them.

---

## PJRT_Client_BufferFromHostBuffer (slot 27)

### Purpose

The host→device staging entry point. JAX/IFRT calls this to upload a host array into a fresh device buffer, supplying the host data pointer, the public element type, the dimensions (and optional byte strides), the target memory space, an optional device layout, and a `PJRT_HostBufferSemantics` value that decides whether libtpu may alias the host buffer or must copy it. It returns two outputs: the new `PJRT_Buffer*` and a `done_with_host_buffer` `PJRT_Event` telling the caller when the host buffer may be reused or freed.

### Args layout

The wrapper validates `ActualStructSizeIsGreaterOrEqual("PJRT_Client_BufferFromHostBuffer_Args", 37, 120, caller_size)` — 37 named fields, 120 bytes current. The byte offsets, read directly from the decompile:

| Offset | Field | Notes |
|---|---|---|
| `+0x00` | `struct_size` | validated first |
| `+0x10` | `client` | `PJRT_Client*`; inner XLA client at `*(*client)` |
| `+0x18` | `data` | host source pointer |
| `+0x20` | `type` | `PJRT_Buffer_Type`; `ConvertFromPjRtBufferType(*(u32*)(a1+0x20))` |
| `+0x28` / `+0x30` | `dims` ptr / `num_dims` | shape extent array |
| `+0x38` / `+0x40` | `byte_strides` ptr / `num_byte_strides` | optional; present iff `+0x38 != 0` |
| `+0x48` | `host_buffer_semantics` | `ConvertFromPjRtHostBufferSemantics(*(u32*)(a1+0x48))` |
| `+0x50` | `memory` | target `PJRT_Memory*`; the allocate+stage vtable is reached via this |
| `+0x58` | (memory-layout-related input) | dereferenced as `**(a1+0x58)` for the stage call |
| `+0x60` | `device_layout` | `PJRT_Buffer_MemoryLayout*`; its `type` is at `+0x48` of that struct |
| `+0x68` | `done_with_host_buffer` | OUT: `PJRT_Event*` (`operator new(0x50)`) |
| `+0x70` | `buffer` | OUT: new `PJRT_Buffer*` (`operator new(0x110)`) |

The `byte_strides` presence test is the `if (*(a1+0x38))` branch in the decompile: a non-null strides pointer flips an internal "has layout" flag (`v75`) that is later checked before the stage call.

> **GOTCHA —** the `device_layout` field (`+0x60`) is validated against the TPU platform *before* any allocation. If its `type` is `PJRT_Buffer_MemoryLayout_Type_Strides` (enum value 1), the call fails with `"PJRT_Buffer_MemoryLayout_Type_Strides in device_layout is not supported in PJRT_Client_BufferFromHostBuffer for platform <name>"`; any other unexpected type value fails with `"Unexpected PJRT_Buffer_MemoryLayout_Type type: <n>"`. TPU accepts only the *tiled* device layout (type 0). A reimplementation that passes a strides layout will get a clean error, not a silently mislaid buffer.

### The PJRT_HostBufferSemantics enum

The public enum value at `+0x48` is mapped to XLA's internal enum by `pjrt::ConvertFromPjRtHostBufferSemantics` @ `0xf8a3f20` (called at four sites in the decompile — one per memory-space dispatch branch). The enum follows upstream `pjrt_c_api.h` ordering; its meaning gates host-buffer lifetime:

| Value | Name | Meaning for the staging copy |
|---|---|---|
| 0 | `kImmutableOnlyDuringCall` | libtpu must finish reading `data` before returning; host buffer may be mutated immediately after the call. Forces a synchronous copy. |
| 1 | `kImmutableUntilTransferCompletes` | host buffer must stay valid until `done_with_host_buffer` fires; libtpu copies asynchronously. |
| 2 | `kImmutableZeroCopy` | host buffer is immutable for the buffer's whole life; libtpu may alias it (no copy) where the layout permits. |
| 3 | `kMutableZeroCopy` | caller donates the host allocation; libtpu may take ownership and alias it. |

> **NOTE (HIGH confidence) —** the *offset* (`a1+0x48`) and the converter call (`ConvertFromPjRtHostBufferSemantics`) are byte-confirmed; the four enumerator names/values mirror the upstream PJRT C API enum (`PJRT_HostBufferSemantics`), which the converter must match for the ABI to interoperate. The per-value copy-vs-alias behavior is the documented PJRT contract; the exact branch in the TPU stage call that *acts* on each value was not individually byte-traced (the stage is a single vtable+0x120 call that receives the converted value as an argument).

### Algorithm

```c
function PJRT_Client_BufferFromHostBuffer(args):          // 0xf8644c0
    if !ActualStructSizeIsGreaterOrEqual(..., 37, 120, args.struct_size):
        return new PJRT_Error{ size_error }                // operator new(8)

    has_strides = (args.byte_strides != NULL)              // a1+0x38; sets internal flag v75
    layout = args.device_layout                            // a1+0x60
    if layout != NULL:
        switch layout.type:                                // layout+0x48
            case 1 /*Strides*/: return Error("...Strides... not supported... for platform " + name)
            case 0 /*Tiled*/:   break
            default:            return Error("Unexpected PJRT_Buffer_MemoryLayout_Type type: " + n)

    // Build a PjRtFuture promise for done_with_host_buffer (PromiseMaker<void>::Make)
    promise = PromiseMaker<void>::Make()                   // tsl::internal, lines 210-227

    xla_type      = ConvertFromPjRtBufferType(args.type)              // a1+0x20
    semantics     = ConvertFromPjRtHostBufferSemantics(args.host_buffer_semantics)  // a1+0x48
    mem           = args.memory                            // a1+0x50, the target memory space

    // Single virtual call into the memory-space / client allocate+stage path:
    //   vtable+0x120 (offset 288) on the inner client object.
    //   This allocates the device buffer in `mem`, schedules the host->device DMA
    //   under `semantics`, and yields a StatusOr<unique_ptr<PjRtBuffer>>.
    status_or = (*inner_vtable[288])(inner_client, xla_type, dims, num_dims,
                                     byte_strides, semantics, layout_info, mem, promise)
    if !status_or.ok():
        return new PJRT_Error{ status_or.status }

    buf_wrapper = operator new(0x110)                      // LABEL_46
    buf_wrapper.impl   = status_or.value                   // +0x00
    buf_wrapper.client = args.client                       // +0x08
    zero(buf_wrapper + 0xf0 .. 0x110)                      // external-ref list + flags
    args.buffer = buf_wrapper                              // a1+0x70

    done_event = operator new(0x50)                        // 80-byte PJRT_Event
    move promise.future.async_value -> done_event+0x08
    move 2 profiling closures           -> done_event+0x18..0x40
    done_event+0x48 = 0
    args.done_with_host_buffer = done_event                // a1+0x68
    return NULL                                            // success
```

> **QUIRK —** there are *two* events in flight here and they mean different things. `done_with_host_buffer` (`+0x68`) signals when the **host** buffer is safe to touch (governed by `host_buffer_semantics`). The **device** buffer's own readiness is a separate event obtained later via `PJRT_Buffer_ReadyEvent` (slot 77). A reimplementer must not reuse one for the other: a buffer can be "done with host" long before the device DMA completes, or vice-versa.

### Function Map

| Function | Addr | Role |
|---|---|---|
| `pjrt::PJRT_Client_BufferFromHostBuffer` | `0xf8644c0` | C wrapper, args validation + marshalling |
| `pjrt::ConvertFromPjRtBufferType` | `0xf8a3e60` | `PJRT_Buffer_Type` → `xla::PrimitiveType` |
| `pjrt::ConvertFromPjRtHostBufferSemantics` | `0xf8a3f20` | public → XLA semantics enum |
| `tsl::internal::PromiseMaker<void>::Make` | (inlined) | builds the `done_with_host_buffer` promise |
| inner client vtable+0x120 | (per-platform) | allocate + schedule host→device DMA |

---

## PJRT_Buffer_ToHostBuffer (slot 75)

### Purpose

The readback path: copy a device buffer's contents into a caller-supplied host buffer. On TPU this is non-trivial because on-device data is **tiled** (padded into the TPU's native tile shape) while the host expects a dense row-major linear layout. `ToHostBuffer` performs the de-tiling shape conversion before the copy.

### Algorithm

```c
function PJRT_Buffer_ToHostBuffer(args):                  // 0xf86e640
    wrapper = args[+0x10]; inner = wrapper.impl
    shape = build xla::Shape from inner.on_device_shape()
    if !shape.is_static():
        ... (dynamic-shape path)
    host_shape = ShapeUtil::DeviceShapeToHostShape(shape)  // 0x20cec000 — de-tile
    if args.host_layout != NULL:
        layout = ConvertToLayout(args.host_layout)         // 0xf8a5640, tiled-layout struct
    // layout-aware copy into the caller's host literal:
    future = inner.vtable[+0x78].ToLiteral(MutableLiteralBase{host_ptr, host_shape, layout})
    args.event = wrap_as_PJRT_Event(future)                // operator new(0x50)
    return NULL
```

The raw, un-shaped readback variants are `CopyRawToHost` (slot 105, inner vtable+0x90 `CopyRawToHost(void*, off, size)`) and `CopyRawToHostFuture` (slot 125, `0xf86dfe0`, inner vtable+0x98 `CopyRawToHostFuture(Future<void*>, off, size)`); they move bytes without de-tiling and are the typed-buffer mirror of the [RawBuffer extension](ext-rawbuffer.md)'s device→host copy. Use `ToHostBuffer` when the host array must match the logical (linear) shape; use the raw variants for byte-exact device dumps.

> **GOTCHA —** skipping the `DeviceShapeToHostShape` step and `memcpy`-ing the on-device bytes straight to the host gives garbage for any tensor whose extents are not already tile-aligned: the device bytes include tile padding the host layout does not expect. The de-tile is mandatory, not an optimization.

### Function Map

| Function | Addr | Role |
|---|---|---|
| `pjrt::PJRT_Buffer_ToHostBuffer` | `0xf86e640` | C wrapper; de-tile + ToLiteral |
| `xla::ShapeUtil::DeviceShapeToHostShape` | `0x20cec000` | tiled → linear shape |
| `pjrt::ConvertToLayout` | `0xf8a5640` | `PJRT_Buffer_MemoryLayout_Tiled` → `xla::Layout` |
| `CommonPjRtBufferImpl::ToLiteral` | inner vtable+0x78 (`0xf9295a0`) | layout-aware device→host copy |

### Considerations

The full `PJRT_Buffer_ToHostBuffer_Args` field offsets (host destination pointer, destination size, the optional `host_layout`) were **not byte-traced** beyond the shape/layout prologue. The de-tiling and the `ToLiteral` bounce are confirmed; the precise argument offsets are marked LOW and a reimplementer should cross-check against upstream `PJRT_Buffer_ToHostBuffer_Args`.

---

## Shape Accessors (slots 64, 65, 69)

### Purpose

Three zero-allocation accessors that read the buffer's immutable cached shape and write the answer back into the args struct. They take no locks, allocate nothing on success, and are safe to call concurrently with other read-only ops.

### Algorithm

```c
function PJRT_Buffer_ElementType(args):                   // 0xf86d220
    inner = args[+0x10].impl
    prim  = inner.vtable[+0x10].element_type()             // xla::PrimitiveType
    args[+0x18] = ConvertToPjRtBufferType(prim)            // 0xf8a3d80 -> PJRT_Buffer_Type

function PJRT_Buffer_Dimensions(args):                     // 0xf86d280
    inner = args[+0x10].impl
    span  = inner.vtable[+0x18].dimensions()               // {const int64* data, size_t count}
    args[+0x18] = span.data                                // zero-copy into cached shape
    args[+0x20] = span.count

function PJRT_Buffer_OnDeviceSizeInBytes(args):            // 0xf86da80
    if !ActualStructSizeIsGreaterOrEqual("PJRT_Buffer_OnDeviceSizeInBytes_Args", 36, 32, args.struct_size):
        return new PJRT_Error{ size_error }
    sz = inner.vtable[+0x88].GetOnDeviceSizeInBytes()       // StatusOr<int64>
    if sz.ok(): args[+0x18] = sz.value; return NULL
    else:       return new PJRT_Error{ sz.status }
```

`OnDeviceSizeInBytes` is the only one of the three that can fail (it returns a `StatusOr`, hence the `operator new(8)` error path); `ElementType` and `Dimensions` are infallible reads of the cached shape. The dimensions span is a *borrow* into the buffer's internal shape — its lifetime is tied to the buffer, so a caller must copy it before the buffer is destroyed.

> **NOTE —** the on-device size from slot 69 is the **HBM byte count including tile padding**, read from the inner buffer (`TpuBufferBase+0x50` at the device level — see [TPU buffer layout](../memory/tpu-buffer-layout.md)). It is generally larger than `product(dims) * sizeof(element_type)`; do not use it to size a host buffer for `ToHostBuffer`, which expects the *host* (de-tiled) size.

---

## Lifetime: Delete, IsDeleted, Destroy (slots 72, 73, 63)

### Purpose

libtpu separates *device-memory reclamation* from *C-wrapper reclamation*. `Delete` (slot 72) eagerly frees the HBM allocation while leaving the wrapper valid; `Destroy` (slot 63) frees the C wrapper itself. They are independent operations on two different resources.

### Algorithm

```c
function PJRT_Buffer_Delete(args):                         // 0xf86dd80
    inner = args[+0x10].impl
    inner.vtable[+0xa0].Delete()        // eagerly free DEVICE memory; wrapper stays valid

function PJRT_Buffer_IsDeleted(args):                      // 0xf86dde0
    inner = args[+0x10].impl
    args[+0x18] = inner.vtable[+0xb0].IsDeleted()          // true after Delete()

function PJRT_Buffer_Destroy(args):                        // 0xf86d020
    wrapper = args[+0x10]
    ~PJRT_Buffer(wrapper)               // 0xf86d0a0: drain ext-ref list, dtor inner buffer
    free(wrapper, 0x110)                // exclusive ownership — no refcount
```

> **QUIRK —** `Delete` and `Destroy` are *not* the same call and ordering matters. The intended sequence is: `Delete` to reclaim HBM the moment the result is no longer needed on device (cuts peak memory), then `Destroy` later when the host-side handle is dropped. Calling `Destroy` without `Delete` is fine — the inner destructor frees the device memory too — but calling slots on a wrapper after `Destroy` is use-after-free. `IsDeleted` returns true after `Delete`; it does *not* tell you whether `Destroy` has run (there is no "is destroyed" predicate — that is the caller's responsibility).

### External-reference pin

`IncreaseExternalReferenceCount` (slot 79) bounces inner vtable+0x70 `AcquireExternalReference` and stores the returned hold in the wrapper's list at `+0xf8/+0x100/+0x108`; `DecreaseExternalReferenceCount` (slot 80) releases it. While any external reference is held, `Delete` **cannot** free the device allocation — this is how foreign consumers (dlpack, NumPy zero-copy, custom kernels reading `OpaqueDeviceMemoryDataPointer`) keep HBM alive. `~PJRT_Buffer()` drains the list during `Destroy`.

> **GOTCHA —** an unbalanced `IncreaseExternalReferenceCount` is a device-memory leak that `Delete` cannot reclaim: the HBM stays pinned until `Destroy` drains the list. Every Increase needs a matching Decrease before `Delete` will actually free.

---

## Memory-Space Surface (slots 71, 97, 74)

### Purpose

A buffer lives in exactly one memory space; these slots query it and copy the buffer to another. `Memory` (slot 71) returns the buffer's current `PJRT_Memory*`. `CopyToMemory` (slot 97) copies to a caller-named memory space. `CopyToDevice` (slot 74) copies to a device's *default* memory space.

### Algorithm

```c
function PJRT_Buffer_Memory(args):                         // 0xf86dc60, slot 71
    inner   = args[+0x10].impl
    mem_cpp = inner.vtable[+0x58].memory_space()           // xla::PjRtMemorySpace*
    client  = args[+0x10].client                           // inner XLA client at *(client+0x08)
    // inlined equivalent of PJRT_Client_FindMemoryWrapper(mem_cpp, client):
    mem_c   = scan client cache (count @+0x90, array @+0x88) for wrapper whose *wrapper == mem_cpp
    if mem_c == NULL:
        args[+0x18] = NULL
        return Unimplemented("PJRT_Buffer_Memory not implemented for platform '%s'", platform_name)
    args[+0x18] = mem_c

function PJRT_Buffer_CopyToMemory(args):                   // 0xf86e500, slot 97
    inner    = args[+0x10].impl
    dst_mem  = args.dst_memory                             // PjRtMemorySpace* directly
    future   = inner.vtable[+0xb8].CopyToMemorySpace(dst_mem)   // 0xf926c80
    args.dst_buffer = wrap(future.value); args.event = wrap_as_PJRT_Event(future)

function PJRT_Buffer_CopyToDevice(args):                   // 0xf86e360, slot 74
    dst_dev  = args.dst_device
    dst_mem  = dst_dev.vtable[+0x98].default_memory_space()     // TpuDevice::default_memory_space 0xf7feda0
    future   = src_inner.vtable[+0xb8].CopyToMemorySpace(dst_mem)
    ...
```

Both copy slots funnel into the same `CommonPjRtBufferImpl::CopyToMemorySpace(PjRtMemorySpace*)` @ `0xf926c80` (inner vtable+0xb8); the difference is only how the destination memory space is obtained (named directly vs. a device's default). That function routes by source/dest shape and class — same-device, cross-device-over-ICI, and cross-pod megascale all go through it; CPU↔device paths take dedicated branches. The cross-memory-space routing and the DMA engine beneath it are documented on [DMA & cross-host receive](dma-and-cross-host-recv.md); the device-side allocator on [HBM allocator](../memory/hbm-allocator.md). This page stops at the C-ABI bounce.

> **NOTE —** `CopyToDevice` reaches the *device* vtable (`xla::TpuDevice`, vtable `0x2177b4d0`), not the buffer vtable, to find the default memory space (`+0x98` → `0xf7feda0`). That `+0x98` is a `PjRtDevice` slot; do not look for it on the buffer.

---

## Async Readiness

Every transfer slot (`BufferFromHostBuffer`, `ToHostBuffer`, `CopyToMemory`, `CopyToDevice`, `CopyRawToHost`) returns an **80-byte `PJRT_Event`** wrapping a `PjRtFuture`. The layout is fixed:

```c
struct PJRT_Event {                  // operator new(0x50)
    /* +0x00 */ void*  vtable_or_pad;
    /* +0x08 */ tsl::AsyncValueRef<absl::Status> async_value;   // the future's status
    /* +0x18 */ AnyInvocable profiling_closure_0;               // __policy func + state
    /* +0x28 */ ...
    /* +0x38 */ AnyInvocable profiling_closure_1;
    /* +0x48 */ uint64_t zero;
};
```

The buffer's own readiness uses the same machinery: `PJRT_Buffer_ReadyEvent` (slot 77) bounces inner vtable+0xe0 `GetReadyFuture()` (`0xf92aa60`) and wraps the future in an 80-byte event. A consumer polls `PJRT_Event_IsReady` (slot 11), registers `PJRT_Event_OnReady` (slot 14), or blocks via `PJRT_Event_Await` (slot 13) — see [Events & async](events-and-async.md). The device-side completion token underneath is a `tpu::TpuEvent`; the middle layer converts it to a client-tracked `PjRtFuture` via `CommonPjRtClient::MakeTrackedReadyFuture` (`0xf91c2e0`).

---

## Donation (slot 130)

`PJRT_Buffer_DonateWithControlDependency` (slot 130, `0xf86f2e0`) bounces inner vtable+0xd8 `DonateWithControlDependency(tsl::Future<void>)` (`0xf92a740`). It builds a `tsl::Promise` (`PromiseMaker<void>::Make`), passes the control-dependency future, and produces a **new** buffer wrapper whose device memory aliases the donor's — the donor is invalidated. This is how JAX's `donate_argnums` reuses an input buffer's HBM for an output, eliminating one allocation and one copy per step. The buffer-donation/aliasing data model is owned by [Buffer Donation & Aliasing](../memory/buffer-donation-aliasing.md); this page documents only the C-ABI slot.

---

## Cross-References

- [PJRT API Overview](overview.md) — the 140-slot `PJRT_Api`, how buffer slots fit the plugin lifecycle
- [API Vtable Reconstruction](api-vtable-reconstruction.md) — how the slot table and `struct_size` versioning are reconstructed
- [Client & Device](client-and-device.md) — `PJRT_Client`, `PJRT_Device`, the memory-wrapper cache that backs `PJRT_Buffer_Memory`
- [Executable Execution](executable-execution.md) — where output buffers are produced and input buffers donated at execute time
- [Events & Async](events-and-async.md) — the 80-byte `PJRT_Event` / `PjRtFuture` model every transfer returns
- [RawBuffer Extension (type 8)](ext-rawbuffer.md) — the untyped 16-byte raw-byte sibling surface and the host↔device DMA pipeline
- [DMA & Cross-Host Receive](dma-and-cross-host-recv.md) — the cross-memory-space copy routing beneath `CopyToMemory`
- [TPU Buffer Layout](../memory/tpu-buffer-layout.md) — the device-side byte layout, tiling, and `TpuBufferBase` fields (`+0x48` ptr / `+0x50` size)
- [HBM Allocator](../memory/hbm-allocator.md) — the device-side HBM allocation that `BufferFromHostBuffer` invokes
- [Buffer Donation & Aliasing](../memory/buffer-donation-aliasing.md) — the donation/aliasing data model behind slot 130
- [Allocator Integration](../runtime/allocator-integration.md) — the StreamExecutor allocator bridge
