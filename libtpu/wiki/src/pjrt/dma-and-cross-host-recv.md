# DMA Map & Cross-Host Receive

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 745 MB, ELF x86-64, not stripped). `.text` is mapped at `0xe63c000`; for functions in `.text` the listed VA equals the file offset. Other wheel versions will differ.*

## Abstract

This page documents two related PJRT **data-plane** surfaces that move bytes *outside* the ordinary host↔device staging path: the **DMA-map API** and **cross-host receive**.

The **DMA-map API** is a pair of *main-table* slots — `PJRT_Client_DmaMap` and `PJRT_Client_DmaUnmap` — that pin a region of host virtual memory so a TPU DMA engine can read or write it without an intermediate copy. They are the libtpu analogue of `cudaHostRegister`/`cudaHostUnregister`: the caller hands in a `(void* data, size_t size)` pair, libtpu maps that region into *every* TPU host-shared-memory location on the client, and a later `DmaUnmap` tears it down. Unlike the buffer slots on [buffer-and-memory.md](buffer-and-memory.md), these slots carry **no buffer object**; they operate directly on the client and a raw host pointer.

The **cross-host receive** surface is the `PJRT_CrossHostTransfers` extension (type 12, struct_size 56, four methods, created by `pjrt::CreateCrossHostTransfersExtension` @ `0xf85d660`). It lets a buffer on one host be filled by a DMA originating on another host across the data-center network (DCN). The model is a descriptor handshake: the receiving host calls `MakeCrossHostReceiveBuffers` to allocate empty destination buffers and obtain opaque *recv descriptors*; it ships those descriptors to the sending host out-of-band; the sender calls `Buffer_CopyToRemoteDevice` with a serialized-descriptor future to push its buffer into the receiver's allocation. Two lower-level point-to-point variants (`CrossHostReceiveBuffers` / `CrossHostSendBuffers`) round out the extension. This is the C-ABI plumbing under pipeline-parallel and cross-pod buffer movement; the device-side ICI/megascale transport that actually carries the bytes is owned by [`../ici/overview.md`](../ici/overview.md) and [`../megascale/overview.md`](../megascale/overview.md).

For reimplementation, the contract is:

- The two DmaMap/DmaUnmap arg structs (`struct_size` literals + the `{data,size}` offsets) and the single client-vtable bounce each makes (`+0x160` / `+0x168`).
- That `DmaMap` fans the host region out across *all* TPU host-shared-memory locations on the client — it is not a single mapping.
- The `PJRT_CrossHostTransfers` extension struct (type 12, size 56) and its four fn-ptr slots, with the C wrapper for each.
- The receive-side descriptor handshake: how `MakeCrossHostReceiveBuffers` builds `xla::Shape`s from C, calls the client, adapts the C notifier callback to C++, and wraps the returned buffers in the **same** 272-byte `PJRT_Buffer` object the typed surface uses.
- The send-side path: `CopyToRemoteDevice` builds a serialized-descriptor `PjRtFuture<std::string>` promise and an `on_done(absl::Status, bool)` callback, then bounces the inner buffer's `CopyToRemoteDevice`.

| | |
|---|---|
| **DmaMap slot** | `pjrt::PJRT_Client_DmaMap` @ `0xf860500` — args min 23 / cur 40; inner client vtable `+0x160` |
| **DmaUnmap slot** | `pjrt::PJRT_Client_DmaUnmap` @ `0xf860580` — args min 25 / cur 32; inner client vtable `+0x168` |
| **DmaMap backing** | `xla::TpuClient::DmaMap(void*, size_t)` @ `0xf80ba80` → per-location `tpu::System::DmaMap` @ `0x1d0b6260` |
| **CrossHostTransfers ext** | type **12**, size **56**, 4 methods; storage `.bss` `0x224c3ad8`; creator `0xf85d660` |
| **Recv allocate** | `pjrt::PJRT_Transfers_PJRT_Client_MakeCrossHostReceiveBuffers` @ `0xf85c9a0` — args min 44 / cur 104 |
| **Remote send** | `pjrt::PJRT_Transfers_PJRT_Buffer_CopyToRemoteDevice` @ `0xf85ce20` → inner buffer vtable `+0xc8` |
| **Recv-buffer wrapper** | the canonical 272-byte (`0x110`) `PJRT_Buffer` — same object as [buffer-and-memory.md](buffer-and-memory.md) |

---

## DMA Map (main-table slots)

### Purpose

`PJRT_Client_DmaMap` registers a host memory region for direct device DMA. After the call succeeds, a TPU DMA engine can stream bytes into or out of `[data, data+size)` with no bounce through a staging buffer — the region is *pinned* (its physical pages are wired down and given device-visible IO addresses). `PJRT_Client_DmaUnmap` reverses it. The pair exists to support zero-copy ingest/egress paths (frameworks that keep large host arrays resident and DMA repeatedly), and it is the precondition for the `pinned_host` memory-space buffers documented on [buffer-and-memory.md](buffer-and-memory.md) being usable as DMA endpoints.

> **QUIRK —** these are **client** slots, not buffer or extension slots. They take no `PJRT_Buffer`. The handle at `args+0x10` is the `PJRT_Client`, and the region is a bare `(void* data, size_t size)` host pointer the caller owns. There is no PJRT object that represents the mapping; the only handle to it is the original host pointer, which the caller must hand back verbatim to `DmaUnmap`. Lose the pointer and you leak the pin.

### Entry Point

```text
pjrt::PJRT_Client_DmaMap (0xf860500)            ── slot; validate + bounce
  └─ inner_client.vtable[+0x160] (=xla::TpuClient::DmaMap @ 0xf80ba80)
       └─ for each TpuSharedMemoryLocation on the client's host:
            tpu::System::DmaMap(loc, data, size)  (0x1d0b6260)
              └─ TpuSharedMemory::DmaMap (0x1d4bdac0)
                   └─ {Pxc,Vxc,Jxc}DriverImpl::DmaMapImpl   ── kernel DMA-mapper ioctl
```

### Algorithm

```c
function PJRT_Client_DmaMap(args):                       // 0xf860500
    if !ActualStructSizeIsGreaterOrEqual(
            "PJRT_Client_DmaMap_Args", 23, 40, args.struct_size):   // a1+0x00
        return new PJRT_Error{ size_error }              // operator new(8)
    client = *(args + 0x10)                              // PJRT_Client wrapper
    inner  = *client                                     // xla::PjRtClient* (TpuClient)
    data   = *(args + 0x18)                              // host pointer
    size   = *(args + 0x20)                              // byte count
    status = inner.vtable[+0x160](inner, data, size)     // DmaMap(void*, size_t)
    if status == ok: return NULL
    return new PJRT_Error{ status }

function PJRT_Client_DmaUnmap(args):                     // 0xf860580
    if !ActualStructSizeIsGreaterOrEqual(
            "PJRT_Client_DmaUnmap_Args", 25, 32, args.struct_size):
        return new PJRT_Error{ size_error }
    inner  = *(*(args + 0x10))                           // TpuClient
    data   = *(args + 0x18)                              // the SAME host pointer
    status = inner.vtable[+0x168](inner, data)           // DmaUnmap(void*)
    if status == ok: return NULL
    return new PJRT_Error{ status }
```

The two wrappers are near-identical thin shims: validate `struct_size`, dereference the client wrapper twice to reach the concrete `xla::TpuClient`, read the args, make a single virtual call, and box any non-OK `absl::Status` into an `operator new(8)` `PJRT_Error`. The vtable offsets (`0x160` = `352`, `0x168` = `360`) are read directly from the `call qword ptr [rax+160h]` / `[rax+168h]` in each decompile.

### The TpuClient::DmaMap fan-out

`xla::TpuClient::DmaMap` @ `0xf80ba80` is where the work happens, and it does something a CUDA-trained reader would not expect: it maps the region into **every** host-shared-memory location, not one.

```c
function TpuClient::DmaMap(this, data, size):            // 0xf80ba80
    // Resolve the tpu::System for this client's host. The System handle is
    // an AsyncValueRef stored at this+81*8 (+256); walk the indirect chain
    // until the value is concrete:
    node = *(this[+0x288] + 0x100)
    while (node.state & 3) != 0:                          // not-yet-resolved bits
        node = node[+0x10]
    sys = node + 0x40                                     // tpu::System*
    host_loc = tpu::System::host_location(sys)            // TpuHostLocation
    shmems   = TpuHostLocation::SharedMemories(host_loc)  // span of TpuSharedMemoryLocation
    result = OkStatus()
    for each loc (56 bytes) in shmems:                    // stride 56
        if loc.is_valid:                                  // (loc+0x30 dword == 0)
            s = tpu::System::DmaMap(sys, loc, data, size) // 0x1d0b6260
            if result is still Ok: result = s             // keep the FIRST status
            else if s is not Ok: StatusRep::Unref(s)      // drop later non-Ok dups
    return result
```

`TpuHostLocation::SharedMemories` returns a span of `TpuSharedMemoryLocation` records (each 56 bytes — the same struct width the megascale recv path uses), and `DmaMap` iterates the whole span, mapping `[data,size)` into each valid location. Per location it calls `tpu::System::DmaMap(TpuSharedMemoryLocation, void*, size_t)` @ `0x1d0b6260`, which routes through `tpu::TpuSharedMemory::DmaMap` @ `0x1d4bdac0` to the concrete driver's `DmaMapImpl` (`Pxc` physical / `Vxc` virtual / `Jxc` — `0xe804460` / `0x1d12d860` / `0xe73ad60`) and ultimately the kernel DMA-mapper ioctl behind `asic_sw::driver::KernelDmaMapper::MapMemory` (`0xe896dc0`).

> **GOTCHA —** the status-aggregation is **first-wins**, not all-or-nothing. The loop keeps the status of the *first* location and `Unref`s every later one — including later *failures*. If location 0 maps cleanly but location 3 fails, `DmaMap` returns OK and the partial mapping is silent. A reimplementer who needs all-locations-or-nothing semantics must add their own rollback; the binary does not unwind earlier mappings on a later failure, and it does not surface the later failure at all.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `pjrt::PJRT_Client_DmaMap` | `0xf860500` | slot wrapper; validate + bounce vtable `+0x160` | CERTAIN |
| `pjrt::PJRT_Client_DmaUnmap` | `0xf860580` | slot wrapper; validate + bounce vtable `+0x168` | CERTAIN |
| `xla::TpuClient::DmaMap(void*, size_t)` | `0xf80ba80` | fan-out over all host shared-memory locations | CERTAIN |
| `xla::TpuClient::DmaUnmap(void*)` | `0xf80bb80` | unmap fan-out (mirror) | HIGH |
| `xla::PjRtClient::DmaMap` (base default) | `0xf8fff60` | abstract-base default (returns Unimplemented unless overridden) | HIGH |
| `xla::MegaScalePjRtClient::DmaMap` | `0xe6eda80` | cross-pod client override | HIGH |
| `tpu::System::DmaMap(TpuSharedMemoryLocation, void*, size_t)` | `0x1d0b6260` | per-location map | CERTAIN |
| `tpu::System::DmaUnmap(TpuSharedMemoryLocation, void*)` | `0x1d0b62a0` | per-location unmap | HIGH |
| `tpu::TpuSharedMemory::DmaMap(Span<const uint8>)` | `0x1d4bdac0` | shared-memory map entry | HIGH |
| `tpu::TpuSharedMemoryPxcDriverImpl::DmaMapImpl` | `0xe804460` | physical-driver kernel map | HIGH |
| `asic_sw::driver::KernelDmaMapper::MapMemory` | `0xe896dc0` | kernel DMA-mapper ioctl | HIGH |

> **NOTE —** the exact `args` field count vs. byte size differs between the two slots (`DmaMap` min 23 fields / 40 bytes; `DmaUnmap` min 25 fields / 32 bytes). The "min" value passed to `ActualStructSizeIsGreaterOrEqual` is the source-line count of named fields the wrapper was compiled against, the "cur" value is the current `struct_size` in bytes; they are *not* the same quantity and should not be reconciled. Both are read verbatim from the `mov esi/edx` immediates in the validation call. See [api-vtable-reconstruction.md](api-vtable-reconstruction.md) for how this two-number versioning works across the whole API.

---

## CrossHostTransfers Extension (type 12)

### Purpose

The cross-host receive surface is delivered as a chain extension, not a main-table slot, because it is optional: a single-host plugin does not advertise it (the consumer discovers it by walking the [extension chain](extension-chain.md) for type id 12). When present it provides four C functions: allocate receive buffers (`MakeCrossHostReceiveBuffers`), push a local buffer to a remote allocation (`Buffer_CopyToRemoteDevice`), and the two lower-level point-to-point primitives (`CrossHostReceiveBuffers`, `CrossHostSendBuffers`). All four are generic-XLA wrappers (the canonical `PjRtClient`/`PjRtBuffer` cross-host API) backed by TPU implementations; the TPU/megascale specialization lives below the C-ABI, in `xla::TpuClient` and `xla::MegaScalePjRtBuffer`.

### Extension struct

The creator is a flat table initializer — no branches, no allocation, ends in `ret` — exactly the pattern every libtpu extension creator follows (see [ext-remaining.md](ext-remaining.md)).

```c
struct PJRT_CrossHostTransfers_Extension {       // struct_size 56 (0x38); .bss @ 0x224c3ad8
    PJRT_Extension_Base base;                    // +0x00 size=56, +0x08 type=12, +0x10 next
    /* +0x18 */ PJRT_Error* (*Client_MakeCrossHostReceiveBuffers)(PJRT_Transfers_PJRT_Client_MakeCrossHostReceiveBuffers_Args*);
    /* +0x20 */ PJRT_Error* (*Buffer_CopyToRemoteDevice)        (PJRT_Transfers_PJRT_Buffer_CopyToRemoteDevice_Args*);
    /* +0x28 */ PJRT_Error* (*Client_CrossHostReceiveBuffers)   (PJRT_Transfers_PJRT_Client_CrossHostReceiveBuffers_Args*);
    /* +0x30 */ PJRT_Error* (*Client_CrossHostSendBuffers)      (PJRT_Transfers_PJRT_Client_CrossHostSendBuffers_Args*);
};
```

The four offsets, sizes, and `type=12` / `struct_size=56` are read directly from the `mov` stores in `CreateCrossHostTransfersExtension` @ `0xf85d660`:

```c
*(a1+0x00) = 56;  *(a1+0x08) = 12;  *(a1+0x10) = next;          // header
*(a1+0x18) = PJRT_Transfers_PJRT_Client_MakeCrossHostReceiveBuffers;
*(a1+0x20) = PJRT_Transfers_PJRT_Buffer_CopyToRemoteDevice;
*(a1+0x28) = PJRT_Transfers_PJRT_Client_CrossHostReceiveBuffers;
*(a1+0x30) = PJRT_Transfers_PJRT_Client_CrossHostSendBuffers;
```

### Method Map

| Off | Method | C symbol | Addr | Backing (inner client/buffer vtable) | Confidence |
|---|---|---|---|---|---|
| `+0x18` | Client_MakeCrossHostReceiveBuffers | `PJRT_Transfers_PJRT_Client_MakeCrossHostReceiveBuffers` | `0xf85c9a0` | client vtable `+0x150` (`MakeCrossHostReceiveBuffers`) | CERTAIN |
| `+0x20` | Buffer_CopyToRemoteDevice | `PJRT_Transfers_PJRT_Buffer_CopyToRemoteDevice` | `0xf85ce20` | buffer vtable `+0xc8` (`CopyToRemoteDevice`) | CERTAIN |
| `+0x28` | Client_CrossHostReceiveBuffers | `PJRT_Transfers_PJRT_Client_CrossHostReceiveBuffers` | `0xf85bba0` | point-to-point recv | HIGH |
| `+0x30` | Client_CrossHostSendBuffers | `PJRT_Transfers_PJRT_Client_CrossHostSendBuffers` | `0xf85c2a0` | point-to-point send | HIGH |

> **NOTE —** the canonical upstream PJRT C API names this extension `PJRT_CrossHostTransfers`; in libtpu the implementing symbols are prefixed `pjrt::PJRT_Transfers_*` (the namespace short-name), and the args structs are `PJRT_Transfers_*_Args`. They are the same surface; the symbol prefix is a libtpu naming artifact, confirmed by the `MakeCrossHostReceiveBuffers` validator string `"PJRT_Client_MakeCrossHostReceiveBuffers_Args"`.

---

## MakeCrossHostReceiveBuffers (extension +0x18)

### Purpose

Called on the **receiving** host. Given a set of shapes and a target device, it allocates that many empty device buffers and hands back (via a notifier callback) an opaque *recv descriptor* per buffer. The descriptors are the rendezvous tokens: the receiver ships them — over any out-of-band channel — to the sending host, which uses them to address the receiver's allocation in a later `CopyToRemoteDevice`. The function returns the allocated `PJRT_Buffer*` array synchronously; the descriptors arrive asynchronously through the C notifier.

### Algorithm

```c
function PJRT_Transfers_PJRT_Client_MakeCrossHostReceiveBuffers(args):  // 0xf85c9a0
    if !ActualStructSizeIsGreaterOrEqual(
            "PJRT_Client_MakeCrossHostReceiveBuffers_Args", 44, 104, args.struct_size):
        return new PJRT_Error{ size_error }

    // 1. Rebuild an xla::Shape[] (320 bytes each) from the C shape arrays.
    n = args[+0x18]                                    // num_shapes (a1[3])
    shapes = vector<xla::Shape>(); shapes.reserve(n)
    for i in 0..n:
        // element_type @ a1[6]+4*i, dims @ a1[5]+8*i, num_dims @ a1[4]+8*i,
        // layout/extra @ a1[7]+8*i
        s = pjrt::BuildXlaShapeFromC(type[i], dims[i], num_dims[i], layout[i])
        if s is error: cleanup; return new PJRT_Error{ s }
        shapes.push_back(s)

    // 2. Adapt the C notifier (CrossHostRecvNotifierInfo @ a1+0x48) to a
    //    std::function<void(StatusOr<PjRtCrossHostRecvState>)>:
    notifier = CCrossHostRecvNotifierToCpp(args.notifier_info)   // 0xf85d6e0 thunk

    // 3. Call the inner client. device = a1[2] inner @ *device; bounce +0x150:
    inner = *(*(args + 0x10))                          // TpuClient (a1[2] = device handle)
    status_or_recv = inner.vtable[+0x150](inner, shapes.data, shapes.size,
                                          device, notifier)      // StatusOr<vector<PjRtBuffer*>>
    if status_or_recv is error: cleanup; return new PJRT_Error{ status }

    // 4. Wrap each returned PjRtBuffer* in a fresh 272-byte PJRT_Buffer.
    buffers = status_or_recv.value                     // vector<unique_ptr<PjRtBuffer>>
    args[+0x60] = buffers.size                         // num_buffers OUT (a1[12])
    for i in 0..buffers.size:
        w = operator new(0x110)                        // PJRT_Buffer wrapper
        w[+0x00] = buffers[i]                          // inner buffer (moved out)
        w[+0x08] = args.client                         // borrowed client (a1[2])
        zero w[+0x10], +0xa8, +0xb0, +0xc8, +0xd0, +0xe8   // ctor flags
        zero w[+0xf0 .. +0x110]                        // external-ref list
        args.buffers[i] = w                            // a1[11] OUT array
    return NULL
```

Two facts pin this down. First, the per-buffer wrapper is allocated with `operator new(0x110)` and zeroed at exactly the same byte offsets (`+0x10`, `+0xa8`, `+0xb0`, `+0xc8`, `+0xd0`, `+0xe8`, and the `+0xf0..+0x110` external-ref region) as the [`BufferFromHostBuffer` constructor on buffer-and-memory.md](buffer-and-memory.md) — so cross-host receive buffers are **indistinguishable** from ordinary device buffers once allocated; the C consumer sees the same `PJRT_Buffer` object and uses the same slots on it. Second, each `xla::Shape` is constructed at a 320-byte stride (`320 * v7` allocation, `Shape::~Shape` walked at `-320`), the standard `xla::Shape` width in this build.

> **QUIRK —** there are two unrelated "make receive" entry points and they must not be confused. `MakeCrossHostReceiveBuffers` (extension `+0x18`) is the **descriptor-handshake** form — it allocates buffers *and* produces recv descriptors via the notifier. `CrossHostReceiveBuffers` (extension `+0x28`) is the lower-level **point-to-point** form that receives into already-known peers. The descriptor form is what pipeline-parallel JAX uses; the point-to-point form is the building block beneath it.

### The C-to-C++ notifier adapter

The recv-descriptor delivery is a callback bridge. The C caller supplies a `PJRT_Transfers_CrossHostRecvNotifierInfo` (function pointer + user data) at `args+0x48`; `pjrt::CCrossHostRecvNotifierToCpp` @ `0xf85d6e0` wraps it in a `std::function<void(StatusOr<PjRtCrossHostRecvState>)>` whose body marshals the C++ `PjRtCrossHostRecvState` (the descriptor set) back into the C `PJRT_Transfers_CrossHostRecvNotifierInfo` callback. The `__policy` thunks at `0xf85dbc0` / `0xf85de00` are the `std::function` clone/destroy machinery for that adapter. When the inner client has gathered the recv descriptors (after the underlying ICI/megascale endpoints are established), it invokes the C++ function, which calls the original C callback with the descriptors the receiver must ship to the sender.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `pjrt::PJRT_Transfers_PJRT_Client_MakeCrossHostReceiveBuffers` | `0xf85c9a0` | C wrapper; build shapes, call, wrap buffers | CERTAIN |
| `pjrt::BuildXlaShapeFromC` | (called) | C shape tuple → `xla::Shape` (320 bytes) | HIGH |
| `pjrt::(anon)::CCrossHostRecvNotifierToCpp` | `0xf85d6e0` | C notifier → `std::function` adapter | CERTAIN |
| `xla::TpuClient::MakeCrossHostReceiveBuffers` | `0xf808e60` | TPU inner impl (client vtable `+0x150`) | CERTAIN |
| `xla::MegaScalePjRtClient::MakeCrossHostReceiveBuffers` | `0xe6ed9e0` | cross-pod inner impl | HIGH |

---

## Buffer_CopyToRemoteDevice (extension +0x20)

### Purpose

Called on the **sending** host. Pushes a local buffer's contents into a remote allocation previously created by the receiver's `MakeCrossHostReceiveBuffers`. The send is keyed by a *serialized descriptor* (the bytes the receiver shipped over), delivered as a `PjRtFuture<std::string>` so the send can be issued before the descriptor has arrived and fire when it does. The caller also supplies an `on_done(absl::Status, bool)` callback signalling completion (the `bool` is the sent/aborted flag).

### Algorithm

```c
function PJRT_Transfers_PJRT_Buffer_CopyToRemoteDevice(args):   // 0xf85ce20
    // 1. Build a PjRtFuture<std::string> promise for the serialized descriptor.
    promise = PromiseMaker<std::string>::Make()                 // tsl::internal

    // 2. If the caller passed a ready serialized descriptor (args+0x18 != NULL),
    //    fulfil the promise immediately by copying the descriptor string
    //    (data @ *(args+0x20), len @ *(args+0x28)) into it via small/large
    //    string emplace; otherwise the promise stays pending until AndThen fires.
    serialized = args[+0x18]
    if serialized != NULL:
        str = copy_string(*(args+0x20) /*data*/, *(args+0x28) /*len*/)
        promise.emplace(StatusOr<string>{ str })
        free the caller's descriptor buffers

    // 3. Resolve the inner buffer and the on_done callback (args+0x30 region),
    //    then bounce CopyToRemoteDevice:
    inner = *(*(args + 0x10))                                   // CommonPjRtBufferImpl
    on_done = wrap args.on_done as std::function<void(absl::Status,bool)>
    inner.vtable[+0xc8].CopyToRemoteDevice(promise.future, on_done)   // 0xf91c8c0
    return NULL   (errors delivered through on_done, not the return)
```

The inner `xla::CommonPjRtBufferImpl::CopyToRemoteDevice` @ `0xf91c8c0` does the real work:

```c
function CommonPjRtBufferImpl::CopyToRemoteDevice(this, desc_future, on_done):  // 0xf91c8c0
    dev_event   = this.vtable[+0x68].device_event_or_promise(this)   // +104
    raw_hold    = CommonPjRtBuffer::AcquireScopedRawBuffer(this)      // pin the source bytes
    if raw_hold is error:
        on_done(raw_hold.status, false); return
    transfer_mgr = this.vtable[+0x58](this)                          // +88, the transfer manager
    // bounce the device-event object's +0x260 slot to issue the actual DCN send:
    transfer_mgr.vtable[+0x260](transfer_mgr, raw_buffer, desc_future, on_done, dev_event, ...)
```

`AcquireScopedRawBuffer` pins the source buffer's device bytes for the duration of the send (the same external-reference mechanism the [RawBuffer extension](ext-rawbuffer.md) uses), so the source cannot be `Delete`d mid-flight. The final bounce at device-event vtable `+0x260` (`608`) hands the raw buffer, the descriptor future, and the `on_done` callback to the transfer manager, which schedules the cross-host DMA over the DCN. The actual wire transport — ICI links, megascale routing, the DMA descriptor format — is owned by [`../ici/dma-descriptor.md`](../ici/dma-descriptor.md) and [`../megascale/overview.md`](../megascale/overview.md); this page stops at the C-ABI and the buffer-side pin.

> **GOTCHA —** `CopyToRemoteDevice` reports errors **only** through `on_done`, never through its return value (the C wrapper returns `NULL` once the send is *scheduled*, even if the send later fails). A reimplementer that checks the returned `PJRT_Error*` for transfer success will miss every runtime failure. The `bool` second argument to `on_done` distinguishes a completed send from an aborted one; the `absl::Status` carries the failure. Wire your completion logic to the callback, not the call.

> **QUIRK —** the descriptor is carried as a `PjRtFuture<std::string>`, not a struct. The serialized recv-descriptor bytes the receiver produced are opaque to libtpu's C-ABI — they are just a string the sender forwards. This is deliberate: it lets the descriptor format evolve (it encodes ICI/megascale endpoint addresses) without changing the C API. Do not attempt to parse it at the PJRT layer.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `pjrt::PJRT_Transfers_PJRT_Buffer_CopyToRemoteDevice` | `0xf85ce20` | C wrapper; descriptor promise + on_done bridge | CERTAIN |
| `tsl::internal::PromiseMaker<std::string>::Make` | (inlined) | builds the serialized-descriptor future | HIGH |
| `xla::CommonPjRtBufferImpl::CopyToRemoteDevice` | `0xf91c8c0` | buffer vtable `+0xc8`; pin + schedule DCN send | CERTAIN |
| `xla::CommonPjRtBuffer::AcquireScopedRawBuffer` | (called) | pin source device bytes for the transfer | HIGH |
| `xla::MegaScalePjRtBuffer::CopyToRemoteDevice` | `0xe6eb100` | cross-pod buffer override | HIGH |
| `xla::TfPjRtBuffer::CopyToRemoteDevice` | `0x10852700` | TF-wrapper override (non-TPU client) | HIGH |
| `std::function<void(absl::Status,bool)>::__call_func<...$_2>` | `0xf85ec60` | on_done C-callback policy thunk | HIGH |

---

## Point-to-Point Send / Receive (extension +0x28 / +0x30)

`Client_CrossHostReceiveBuffers` (`0xf85bba0`) and `Client_CrossHostSendBuffers` (`0xf85c2a0`) are the lower-level primitives beneath the descriptor handshake. They move buffers between explicitly-named peers without the `MakeCrossHostReceiveBuffers` descriptor round-trip — the peer set is supplied in the args (a span of `GlobalDeviceId`, seen in the backing symbol `xla::PjRtClient::CrossHostSendBuffers(Span<const PjRtBuffer*>, Span<const GlobalDeviceId>, ...)` @ `0xe6edac0`). They are the building blocks the descriptor form composes; JAX's pipeline-parallel path uses the descriptor form, while collective-style bulk movement can use these directly.

These two were not byte-traced beyond their C wrappers and backing symbols; the args-struct field layouts are **not** confirmed (marked HIGH for the symbol/backing identity, LOW for the precise offsets). A reimplementer should cross-check the args structs against upstream `pjrt_c_api.h`'s `PJRT_Transfers_PJRT_Client_CrossHost{Send,Receive}Buffers_Args`.

| Function | Addr | Backing | Confidence |
|---|---|---|---|
| `pjrt::PJRT_Transfers_PJRT_Client_CrossHostReceiveBuffers` | `0xf85bba0` | `xla::PjRtClient::CrossHostReceiveBuffers` @ `0xe6edb00` | HIGH |
| `pjrt::PJRT_Transfers_PJRT_Client_CrossHostSendBuffers` | `0xf85c2a0` | `xla::PjRtClient::CrossHostSendBuffers` @ `0xe6edac0` | HIGH |

---

## How the Two Surfaces Relate

DmaMap and cross-host receive are independent APIs that solve adjacent problems, and a reimplementer should keep them distinct:

- **DmaMap** pins *host* memory so a *local* TPU DMA engine can reach it directly. It is a client-wide, buffer-less operation; the unit of work is a host virtual-address range. It underpins the `pinned_host` memory space (see [buffer-and-memory.md](buffer-and-memory.md)) and zero-copy host ingest/egress.
- **Cross-host receive** moves *device* bytes between *different hosts* over the DCN. The unit of work is a `PJRT_Buffer`, and the transport is ICI/megascale, not a local DMA-mapper ioctl.

They can compose — a cross-host receive ultimately lands bytes in a device buffer, and a pinned host region can be the staging endpoint for the host side of such a transfer — but neither requires the other, and they bounce through entirely different backing stacks (`KernelDmaMapper` vs. the transfer manager + ICI/megascale).

| | DmaMap / DmaUnmap | CrossHostTransfers (type 12) |
|---|---|---|
| **API form** | main-table slots (always present) | chain extension (optional, type id 12) |
| **Handle** | `PJRT_Client` + raw `(void*, size_t)` | `PJRT_Client` / `PJRT_Buffer` |
| **Scope** | local host ↔ local TPU DMA engine | host ↔ remote host over DCN |
| **Unit** | host VA range | device buffer + recv descriptor |
| **Async** | synchronous (returns mapped status) | descriptor future + `on_done` callback |
| **Backing** | `TpuClient::DmaMap` → `KernelDmaMapper` | inner `CopyToRemoteDevice` / `MakeCrossHostReceiveBuffers` → ICI/megascale |

---

## Cross-References

- [PJRT API Overview](overview.md) — the 140-slot `PJRT_Api` and the extension-chain model these surfaces plug into
- [API Vtable Reconstruction](api-vtable-reconstruction.md) — the two-number `struct_size` versioning and how slot/vtable offsets are recovered
- [Buffer ABI & Memory Layouts](buffer-and-memory.md) — the 272-byte `PJRT_Buffer` recv buffers reuse, the `pinned_host` memory space DmaMap underpins
- [RawBuffer Extension (type 8)](ext-rawbuffer.md) — the untyped raw-byte surface and the `AcquireScopedRawBuffer` pin `CopyToRemoteDevice` relies on
- [Extension Chain](extension-chain.md) — how a consumer discovers the type-12 CrossHostTransfers node
- [Remaining Extensions](ext-remaining.md) — the flat-creator pattern and the type-12 chain entry summary
- [Client & Device](client-and-device.md) — the `PJRT_Client` wrapper DmaMap and MakeCrossHostReceiveBuffers dereference
- [Collectives & Communicator](collectives-communicator.md) — the in-process collectives surface; contrast with cross-host point-to-point movement
- [Host Callbacks](../runtime/host-callbacks.md) — the host-side send/recv rendezvous for execute-time transfers
- [ICI Overview](../ici/overview.md) — the device-side inter-chip transport that carries cross-host bytes
- [ICI DMA Descriptor](../ici/dma-descriptor.md) — the on-wire descriptor format beneath `CopyToRemoteDevice`
- [Megascale Overview](../megascale/overview.md) — the cross-pod DCN runtime backing the MegaScale client/buffer overrides
