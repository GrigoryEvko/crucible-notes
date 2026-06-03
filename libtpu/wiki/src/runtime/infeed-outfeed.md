# Infeed / Outfeed Queues

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. VA == file analysis address. Other versions will differ.*

## Abstract

Infeed and outfeed are TPU's *streaming* host↔device channels: a running program issues `Infeed`/`Outfeed` HLO ops that block on a hardware FIFO, and the host concurrently pushes input literals into the infeed queue and drains result literals out of the outfeed queue. This is structurally different from a bulk buffer transfer (a `PJRT_Buffer` copy, documented on [Host↔Device DMA](../dma/host-device-dma.md)): a buffer copy targets a specific HBM allocation and completes once; an infeed/outfeed transfer targets a *named per-core queue* identified only by a `TpuCoreLocation` + a small integer queue index, and the device side consumes/produces entries in program order as the executable runs. The XLA reference frame is the same one upstream uses — `TransferManager::TransferLiteralToInfeed` / `TransferLiteralFromOutfeed` and `PjRtDevice::TransferToInfeed` / `TransferFromOutfeed` — but libtpu ships **two parallel implementations** of it, and a reimplementer must not conflate them.

The modern path is `xla::TpuDevice::TransferToInfeed(const LiteralSlice&)` / `TransferFromOutfeed(MutableBorrowingLiteral)` (`learning/45eac/research/pjrt/tpu_pjrt_client.cc`), the PJRT device surface over the TFRT-native `tpu::System` runtime. It linearizes the host literal into device-layout buffers, chops each buffer into hardware *spans* sized by `TransferSizeUtil`, and enqueues each span through `tpu::System::EnqueueInfeed` / `DequeueOutfeed` — a `TpuCoreLocation`-keyed, callback-based, fully blocking transfer. The legacy path is `tensorflow::tpu::TpuTransferManager::TransferLiteralToInfeed` / `TransferLiteralFromOutfeed(StreamExecutor*, …)`, a thin C-ABI shim that marshals the literal through `ApiConverter::ToC` and calls the TPU driver's `TfTpu_ExecutorApiFn` table (`EnqueueInfeed` slot +560, `TransferLiteralFromOutfeed` slot +576), which lands in `deepsea::executor::DeepseaExecutor::EnqueueInfeed`. Both ultimately reach the same on-chip queue hardware; they differ only in the host-side abstraction (TFRT async runtime vs. StreamExecutor C-shim).

This page owns the infeed/outfeed *queue mechanism*, the two transfer-manager entry points, the shape/layout linearization contract, and the blocking/async semantics. The on-chip queue *driver* (`TpuInfeedQueue` / `TpuOutfeedQueue` with their Pxc/Jxc generation impls) is described only to the depth needed to understand the contract; the general bulk-buffer DMA mechanism is on [Host↔Device DMA](../dma/host-device-dma.md); the execute path that interleaves with these queues is on [Execute Async on Stream](execute-async-on-stream.md).

For reimplementation, the contract is:

- **The two entry points** — PJRT `TpuDevice::TransferToInfeed`/`FromOutfeed` over `tpu::System`, vs. legacy `TpuTransferManager` over the `ExecutorApiFn` C-shim — and the fact that they are independent code paths, not one wrapping the other.
- **The queue handle** — a transfer names its target by `tpu::TpuCoreLocation` + an `int` queue index, *not* by a device address. The queue object is resolved per-call (`TpuChipConfig::GetInfeedQueues` / `tpu::System::EnqueueInfeed`'s topology walk), never held by the caller.
- **The layout/linearization contract** — the host literal's shape is converted to device shape (`TransferSizeUtil::HostShapeToDeviceShape`), tiled, and linearized (`LiteralLinearizer::LinearizeToBuffers`); outfeed runs the inverse (`Delinearize`). Element types are gated by `HardwareLayout::SupportedPrimitiveType`.
- **The span-chunking + blocking semantics** — a linearized buffer is split into `TensorCoreInfeedSpanSizeBytes` / `TensorCoreMaxOutfeedSpanSizeBytes` chunks, each enqueued as a callback-completed task into an `AsyncTaskGroup` / `BlockableAsyncTaskGroup`, and the call blocks (`WaitTillDone` / `Mutex::LockWhenCommon` on a remaining-count predicate) until every span completes.

| | |
|---|---|
| **PJRT infeed entry** | `xla::TpuDevice::TransferToInfeed(const LiteralSlice&)` @ `0xf7ff540` (`tpu_pjrt_client.cc:2153`) |
| **PJRT outfeed entry** | `xla::TpuDevice::TransferFromOutfeed(MutableBorrowingLiteral)` @ `0xf7ffca0` (`:2194`) |
| **PJRT outfeed helper** | `xla::TransferFromOutfeedHelper(TpuCoreLocation, Layout, System*, MutableBorrowingLiteral*)` @ `0xf8436e0` (`outfeed_utils.cc`) |
| **PJRT infeed span loop** | `tpu::TransferLinearizedBufferToInfeed(Span<uint8>, TpuCoreLocation, AnyInvocable, System*)` @ `0xf8d5cc0` |
| **Driver enqueue/dequeue** | `tpu::System::EnqueueInfeed` @ `0x1d0b5d00`, `tpu::System::DequeueOutfeed` @ `0x1d0b5f00` |
| **Legacy SE infeed entry** | `tensorflow::tpu::TpuTransferManager::TransferLiteralToInfeed(StreamExecutor*, LiteralSlice&)` @ `0xe9721c0` |
| **Legacy SE outfeed entry** | `tensorflow::tpu::TpuTransferManager::TransferLiteralFromOutfeed(StreamExecutor*, MutableBorrowingLiteral)` @ `0xe972660` |
| **Legacy C-shim slots** | `ExecutorApiFn()+560` (infeed enqueue), `+576` (outfeed dequeue); status `+392/+400/+408/+384` |
| **Legacy driver leaf** | `TpuExecutor_EnqueueInfeed` @ `0xeab9680` → `deepsea::executor::DeepseaExecutor::EnqueueInfeed`; `TpuExecutor_DequeueOutfeed` @ `0xeab96c0` |
| **Queue handle** | `tpu::TpuCoreLocation` + `int` queue index (NOT a device address) |
| **Queue objects** | `tpu::TpuInfeedQueue` / `tpu::TpuOutfeedQueue`, Pxc/Jxc per-generation impls |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile (both paths traced end-to-end) |

---

## 1. Two Transfer Managers, One Queue Hardware

libtpu carries the full XLA transfer-manager surface twice. The byte-confirmed split:

| Aspect | PJRT path | Legacy StreamExecutor path |
|---|---|---|
| Infeed entry | `xla::TpuDevice::TransferToInfeed` @ `0xf7ff540` | `tensorflow::tpu::TpuTransferManager::TransferLiteralToInfeed` @ `0xe9721c0` |
| Outfeed entry | `xla::TpuDevice::TransferFromOutfeed` @ `0xf7ffca0` | `…::TpuTransferManager::TransferLiteralFromOutfeed` @ `0xe972660` |
| Caller surface | `PJRT_Device_*` / `PjRtDevice` (JAX, PyTorch-XLA) | `xla::LocalClient` / `xla::Service`, TF-TPU op kernels |
| Device runtime | `tpu::System` (TFRT async-value native) | `TfTpu_ExecutorApiFn` C-ABI table (`SE_StreamExecutor*`) |
| Marshalling | `LiteralLinearizer` in-process | `ApiConverter::ToC` → C-shim → driver |
| Source root | `learning/45eac/research/pjrt/` | `third_party/tensorflow/compiler/xla/stream_executor/tpu/` |
| Driver leaf | `tpu::System::EnqueueInfeed` / `DequeueOutfeed` | `DeepseaExecutor::EnqueueInfeed` / `DequeueOutfeed` |

> **GOTCHA — these are not layered.** The PJRT `TpuDevice` path has **zero** references to `ExecutorApiFn` / `SE_StreamExecutor` / `TpuExecutor` (byte-confirmed in P-3-190's TpuClient range scan); it talks to `tpu::System` directly. A reimplementer who assumes PJRT infeed forwards through `TpuTransferManager` will be wrong: PJRT linearizes and enqueues itself. The two paths only converge inside the TPU driver core (the on-chip queue), which both reach by a different last hop. Confidence: CONFIRMED.

There is a third, generation-specific variant — `xla::DeepseaTransferManager::TransferLiteralToInfeed` @ `0xeac3cc0` / `TransferLiteralFromOutfeedLocked` @ `0xeac4b80` — which is the jellyfish/Deepsea `TransferManager` subclass; it shares the linearizer/`TransferSizeUtil` machinery with the PJRT path. The CPU backend ships its own (`xla::CpuTransferManager` @ `0xf93fda0`/`0xf93fde0` → `TransferLiteralToInfeedOnCpu` @ `0xf940080`), used only when HLO lands on the host CPU device. This page documents the TPU infeed/outfeed; CPU infeed is a `posix`-style host-memory ring outside scope.

---

## 2. The Queue Handle and the Layout Contract

### 2.1 What names a queue

A transfer does **not** carry a device address. The infeed/outfeed target is named by:

- a **`tpu::TpuCoreLocation`** — which physical TensorCore the queue belongs to; and
- an **`int` queue index** — which of that core's several infeed/outfeed queues to use.

`tpu::System::EnqueueInfeed(const TpuCoreLocation&, int queue_id, Span<const uint8>, AnyInvocable<void(const Status&)>)` (`0x1d0b5d00`) resolves the actual queue object from these on every call:

```c
// tpu::System::EnqueueInfeed   sub_1D0B5D00
// a2 = TpuCoreLocation, a3 = queue_id, a4/a5 = Span<uint8>, a6 = completion callback
chip   = TpuCoreLocation::Chip(core_loc);                 // @0x1d0b5d2f
topo   = system.topology()->chip_to_node(chip);           // vtable +80
node   = topo->core_for(core_loc.core_index);             // vtable +32  (core_loc+0x30)
queue  = node->infeed_queue(queue_id);                    // vtable +48
status = queue->Validate();                               // vtable +32
if (status != OkStatus) { invoke callback(status); return; }
// resolve the per-core TpuAllocator by its shared-memory location
alloc  = system.allocators().at(core_loc.LocalSharedMemory(0));   // flat_hash_map<TpuSharedMemoryLocation, unique_ptr<TpuAllocator>>
view   = alloc->MapDmaBuffer(span_ptr, span_len);          // vtable +168
if (view.mapped)  queue->Enqueue(view, …, callback);       // vtable +56  (mapped DMA buffer)
else              queue->EnqueueImpl(span, callback);      // vtable +40  (raw span)
```

So the queue handle is *ephemeral*: the caller holds a `TpuCoreLocation` and an index, and the runtime walks `topology → chip-node → core → infeed_queue[id]` every enqueue. There is no persistent queue pointer in the PJRT `TpuDevice`. `DequeueOutfeed` (`0x1d0b5f00`) is the mirror over `outfeed_queue(queue_id)`.

> **NOTE — queue index 0 in practice.** Both PJRT span loops (`TransferLinearizedBufferToInfeed` @ `0xf8d5cc0` and the outfeed loop in `TransferFromOutfeedHelper`) pass `queue_id = 0` for every span; the index argument exists for multi-queue cores but the literal-transfer path uses queue 0. The per-core *list* of infeed queues comes from `TpuChipConfig::GetInfeedQueues(TpuCoreType)` @ `0x20afcc80`, and `TpuDevice::TransferToInfeed` RET_CHECKs `!infeed_queues.empty()` (`tpu_pjrt_client.cc:2158`) before linearizing. Confidence: CONFIRMED.

### 2.2 The shape → device-layout linearization

The host literal cannot go to the queue as-is; the device wants its own tiled layout. The contract, identical on both PJRT entry points:

1. **Host shape → device shape.** `xla::jellyfish::TransferSizeUtil::HostShapeToDeviceShape(out_shape, system, host_shape, 0, 0, 1)` computes the on-device shape (tiling, padding). Infeed: `TpuDevice::TransferToInfeed` line ~`0xf7ff540+…`. Outfeed: `TransferFromOutfeed` @ `0xf7ffca0`.
2. **Element-type gate.** Outfeed verifies `xla::jellyfish::HardwareLayout::SupportedPrimitiveType(element_type)`; an unsupported dtype returns `Unimplemented("Transferring data with element type %s has not been implemented on TPUs.")` (`outfeed_utils.cc`). Infeed relies on the linearizer's own checks.
3. **Linearize (infeed) / delinearize (outfeed).** Infeed calls `xla::jellyfish::LiteralLinearizer::LinearizeToBuffers(system, literal, device_shape, &buffers, …)` producing a vector of device-layout byte buffers; the last argument `v12 = (GetInfeedQueues()[0]+64 == 1)` selects a packing variant. Outfeed allocates a `posix_memalign`-aligned receive buffer, dequeues into it, then `xla::jellyfish::LiteralLinearizer::Delinearize(topology, device_shape, raw_buffer, byte_count/4, layout, system)` reshapes the raw device bytes back into the caller's `MutableBorrowingLiteral`.
4. **Tuple handling (outfeed).** `TransferFromOutfeed` walks `ShapeUtil::TupleElementCount` and calls `TransferFromOutfeedHelper` once per tuple leaf (each leaf gets its own `MutableBorrowingLiteral` view at index `i`); a non-tuple shape is a single call. Outfeed also has a fast `TransferX64OrX128FromOutfeedHelper` arm for 64-bit and 128-bit element widths (`ElementHasBitWidth(64)` / `(128)`).

> **GOTCHA — the layout must be tile-populated.** `TransferFromOutfeedHelper` first runs `LayoutUtil::ValidateLayoutForShape`, then `TransferSizeUtil::GetCompactTiles`. If the device layout has no tiles it fails with `"Device layout of an array needs to be populated with tiles, got <layout>"` (`outfeed_utils.cc:92`). A reimplementer feeding an untiled layout into outfeed gets an `InvalidArgument`, not a silent raw copy. Confidence: CONFIRMED.

---

## 3. The PJRT Path — Span Chunking and Blocking

### 3.1 Infeed: `TransferToInfeed` → `TransferLinearizedBufferToInfeed`

After linearizing the literal into a buffer vector, `TpuDevice::TransferToInfeed` schedules every buffer into a `tpu::BlockableAsyncTaskGroup` (sized to the buffer count) under the device's infeed mutex (`this+376`), invokes `tpu::TransferLinearizedBufferToInfeed` per buffer, then blocks the calling thread on `WaitTillDone`:

```c
// xla::TpuDevice::TransferToInfeed   sub_F7FF540   (tpu_pjrt_client.cc:2153)
RET_CHECK(!infeed_queues.empty());                       // :2158
device_shape = TransferSizeUtil::HostShapeToDeviceShape(system, literal.shape());
linear_ok    = LiteralLinearizer::LinearizeToBuffers(system, literal, device_shape,
                                                      &buffers, …, pack_variant);
if (linear_ok != Ok) return InitRep(linear_ok);          // :2176

mutex.lock(this+376);                                     // infeed serialization lock
group = BlockableAsyncTaskGroup{ total = buffers.count }; // util.h:124 CHECK total>0
for (buf : buffers) {
    CHECK(--unscheduled_count_ >= 0);                     // util.h:138 capacity guard
    next_done = group.NextDone();                         // per-task completion callback
    queue = walk to first ready infeed queue node;        // skip nodes with (flags&3)!=0
    tpu::TransferLinearizedBufferToInfeed(buf.ptr, buf.len, core_location,
                                          next_done, queue /*+64*/);   // @0xf8d5cc0
}
group.WaitTillDone();                                     // Mutex::LockWhenCommon on count==0
// destroy group, release buffers, unlock
```

`tpu::TransferLinearizedBufferToInfeed` (`0xf8d5cc0`) is the per-buffer span loop. It splits the device buffer into hardware spans of `TransferSizeUtil::TensorCoreInfeedSpanSizeBytes` bytes and enqueues each:

```c
// tpu::TransferLinearizedBufferToInfeed   sub_F8D5CC0
// a1 = buffer bytes, a2 = byte_count, a3 = TransferSizeUtil*, a4 = callback, a5 = System*
span = TransferSizeUtil::TensorCoreInfeedSpanSizeBytes(util, core, 1, callback);  // @0xf8d5d…
n_full   = byte_count / span;
n_tasks  = n_full + (byte_count % span != 0);            // ceil-div task count
group    = AsyncTaskGroup(operator new(64,16), n_tasks, callback);
ptr = a1;
while (remaining >= span) {                              // full spans: enqueue in place
    CHECK(--unscheduled_count_ >= 0);                    // util.h:52
    tpu::System::EnqueueInfeed(system, core, /*queue=*/0, ptr, span, group.NextDone());
    ptr += span; remaining -= span;
}
if (remaining > 0) {                                     // tail span: pad to span size
    pad_len = TensorCoreInfeedSpanSizeBytes(util, remaining-core, 1, …);
    posix_memalign(&tail, 0x20, pad_len);                // 32-byte aligned
    memcpy(tail, ptr, remaining);
    memset(tail + remaining, 0, pad_len - remaining);    // zero-pad the partial span
    tpu::System::EnqueueInfeed(system, core, 0, tail, pad_len, owning_callback);  // frees tail
}
```

Two details a reimplementer must reproduce: (a) every span enqueue is a separate `EnqueueInfeed` with its own completion callback fed by the `AsyncTaskGroup`; (b) a partial trailing span is copied into a fresh `posix_memalign(32)` buffer and **zero-padded** to a full span width before enqueue (the device reads whole spans). The owning variant of the callback frees that padded buffer on completion.

### 3.2 Outfeed: `TransferFromOutfeed` → `TransferFromOutfeedHelper`

The outfeed mirror computes the padded device byte count, then dequeues span-by-span into one aligned receive buffer, blocks on a count predicate, and delinearizes:

```c
// xla::TransferFromOutfeedHelper   sub_F8436E0   (outfeed_utils.cc)
ValidateLayoutForShape(core, layout);                          // :82 on failure
device_shape = HostShapeToDeviceShape(...);  GetCompactTiles(...);  // tile check :92
if (!HardwareLayout::SupportedPrimitiveType(elt)) return Unimplemented(...);
padded = TransferSizeUtil::ShapeSizeCompactForDma(topology, device_shape);
RET_CHECK(padded % sizeof(uint32_t) == 0);                     // :143
if (elt is x64/x128 array && fast-path layout)                 // direct into literal
    dst = MutableLiteralBase::untyped_data(literal);
else { posix_memalign(&dst, 0x20, padded); }                   // staging buffer
max_span = TransferSizeUtil::TensorCoreMaxOutfeedSpanSizeBytes(topology);
remaining = padded; off = 0; outstanding = 0;
while (remaining > 0) {
    chunk = min(remaining, max_span);
    ++outstanding;                                             // count guarded by mutex
    tpu::System::DequeueOutfeed(core, system, /*queue=*/0, dst+off, chunk, on_chunk_done);
    off += chunk; remaining -= chunk;
}
Mutex::LockWhenCommon(predicate: outstanding == 0);            // block until all spans done
// then reshape device bytes back into the literal
LiteralLinearizer::Delinearize(topology, device_shape, dst, padded/4, layout, system);  // :231
free(dst);
```

`TransferFromOutfeed` @ `0xf7ffca0` holds the device's outfeed mutex (`this+0x180`) across the whole tuple-leaf loop and calls `TransferFromOutfeedHelper` for each leaf. On a layout-validate failure the helper returns at `outfeed_utils.cc:82`; on a dequeue error it surfaces `CreateStatusAndConditionallyLog(212, …)`; a delinearize failure surfaces `:231`.

> **QUIRK — outfeed dequeues into a staging buffer, then delinearizes; infeed linearizes, then enqueues.** The two are not symmetric in their buffer handling. Infeed produces N device buffers up front and streams each as spans. Outfeed receives raw device bytes into one aligned buffer (or, for x64/x128 fast path, straight into the literal's `untyped_data`) and *afterwards* runs `Delinearize` to lay the bytes out as the host literal. A reimplementer who tries to delinearize per-span will corrupt multi-span shapes. Confidence: CONFIRMED.

### 3.3 Blocking / async semantics

Both PJRT entry points are **synchronous to the caller**: `TransferToInfeed` blocks in `BlockableAsyncTaskGroup::WaitTillDone` and `TransferFromOutfeed` blocks in `Mutex::LockWhenCommon` until every span's completion callback has fired. The *device* side is asynchronous — each `EnqueueInfeed`/`DequeueOutfeed` returns immediately after handing the span to the queue, and a TPU-driver completion fires the `AnyInvocable<void(const Status&)>` callback that decrements the outstanding-task count. The host thread is parked on an `absl::Mutex` condition, not spinning. A device-side error is delivered as the `Status` argument to the callback and propagated out as the transfer's return status. Per-device serialization is enforced by the infeed mutex (`TpuDevice+376`) and outfeed mutex (`TpuDevice+0x180`): two concurrent `TransferToInfeed` calls on one device cannot interleave their spans.

---

## 4. The Legacy StreamExecutor Path

`tensorflow::tpu::TpuTransferManager` is the `xla::TransferManager` subclass used by `LocalClient`/`Service` and TF-TPU op kernels. Its infeed/outfeed are pure C-ABI marshalling — no linearization in libtpu's own code; the driver does it behind the shim.

```c
// tensorflow::tpu::TpuTransferManager::TransferLiteralToInfeed   sub_E9721C0
fn  = ExecutorApiFn();
ctx = fn[360]();                          // make SE_Status / status carrier   (slot +360)
ApiConverter::ToC(literal_slice);         // marshal LiteralSlice → XLA_Literal C struct
fn[560]( this->se_handle,                 // ExecutorApiFn slot +560 = infeed enqueue
         executor->se_executor,           // *(StreamExecutor+7)
         &c_literal, ctx );
ApiConverter::Destroy(&c_literal);
status = fn[408](ctx) ? Ok               // slot +408 = StatusOk?
       : MakeRep(fn[400](ctx)*4|1,       // slot +400 = code, +392 = message ptr
                 fn[392](ctx), strlen,   // status_helper.h:38
                 38, "…/status_helper.h");
fn[384](ctx);                            // slot +384 = free status carrier
return status;
```

Outfeed (`TransferLiteralFromOutfeed` @ `0xe972660`) is identical except it marshals **two** C structs (`ApiConverter::ToC(literal.shape())` into a shape carrier *and* `ApiConverter::ToC(LiteralSlice(literal))` into a literal carrier) and calls **slot +576** instead of +560. The leaf exports `TpuExecutor_EnqueueInfeed` @ `0xeab9680` and `TpuExecutor_DequeueOutfeed` @ `0xeab96c0` forward into `deepsea::executor::DeepseaExecutor::EnqueueInfeed` / `DequeueOutfeed` inside the TPU driver core:

```c
// TpuExecutor_EnqueueInfeed   sub_EAB9680   (the C-ABI export the +560 slot targets)
result = deepsea::executor::DeepseaExecutor::EnqueueInfeed(*executor);
// result is an absl::Status; refcount-managed into *out_status
```

> **NOTE — slot numbers are the contract.** The legacy path's only libtpu-visible knobs are the `TfTpu_ExecutorApiFn` table offsets: **+560** = infeed enqueue, **+576** = outfeed dequeue, **+360** = status-carrier ctor, **+392/+400/+408/+384** = message/code/ok/destroy. These are part of the TPU-driver C-ABI (a separate task; not the full table). A reimplementer of the *legacy* path reproduces the marshalling and these slot calls, not the linearizer — the driver owns layout. Confidence: CONFIRMED for the slot numbers and call shape; the driver-internal linearization is opaque from `libtpu.so`. (LOW on the exact `DeepseaExecutor::EnqueueInfeed` body — it is in the driver core, not decompiled here.)

---

## 5. On-Chip Queue Objects (Driver Depth)

Below `tpu::System::EnqueueInfeed`/`DequeueOutfeed` sits a generation-specific queue object. The symbol set confirms a `tpu::TpuInfeedQueue` / `tpu::TpuOutfeedQueue` abstract base with per-silicon impls:

| Class | Generation | Key methods (confirmed symbols) |
|---|---|---|
| `tpu::TpuInfeedQueuePxcDriverImpl` | Pxc (Pufferfish/BarnaCore) | `EnqueueImpl(Span<uint8>, AnyInvocable)` @ `0xe802600` |
| `tpu::TpuOutfeedQueuePxcDriverImpl` | Pxc | `RawDequeue(Span<uint8>/DmaBuffer*, AnyInvocable)` @ `0xe803de0`/`0xe803e80` |
| `tpu::TpuInfeedQueueJxcDriverImpl` | Jxc (Jellyfish) | `EnqueueImpl(TpuMappedDmaBufferView/Span, …)` @ `0xe7380e0`/`0xe737c60` |
| `tpu::TpuOutfeedQueueJxcDriverImpl` | Jxc | `RawDequeue(...)` @ `0xe73a100`/`0xe73a320`, `MapDmaBuffer` @ `0xe73a520` |
| `tpu::TpuPxcDriver` | Pxc driver | `EnqueueInfeed(TpuInfeedQueueOnChip, Span, AnyInvocable)` @ `0xe8109c0`, `DequeueOutfeed` @ `0xe811380` |
| `tpu::JfSoftwareInfeedQueueController` | Jxc software queue | `ThrottledSharedMemoryWrite(...)` @ `0xe738800`/`0xe738900` |

The Pxc infeed enqueue shows the queue-index dispatch and a sparsecore offload fork:

```c
// tpu::TpuInfeedQueuePxcDriverImpl::EnqueueImpl   sub_E802600
queue_index = this[16];                                  // signed int
driver      = this[136];                                 // TpuPxcDriver*
if (queue_index < 0)                                     // negative index = BarnaCore/sparsecore
    TpuPxcDriver::EnqueueBarnaCoreHmf(driver);           // HMF (host-memory-feed) offload
else
    TpuPxcDriver::EnqueueInfeed(driver, on_chip_queue, queue_index, span_ptr, span_len, cb);
```

A reimplementer needs from this layer only: (a) the queue is selected by an integer index resolved through the topology, (b) a negative index reroutes to the BarnaCore/sparsecore host-memory-feed path rather than the TensorCore infeed FIFO, and (c) the actual silicon enqueue/dequeue (DMA buffer mapping, FIFO write) is inside the TPU driver core and is opaque from this binary. The `TpuChipConfig::InfeedQueue::PerCore` / `Hardware::PerCore` variant types (`0xe7274c0`, `0xe7386c0`) confirm the queues are configured per-core with a hardware-or-software representation. Confidence: HIGH on the class structure and dispatch; LOW on the silicon FIFO mechanics.

---

## 6. Reimplementation Notes

- **Pick the right entry.** A PJRT consumer (JAX/PyTorch-XLA) uses `TpuDevice::TransferToInfeed`/`FromOutfeed` and never touches `TpuTransferManager`. Only `LocalClient`/`Service`/TF-TPU kernels use the legacy `TpuTransferManager` + C-shim. Implementing one does not implement the other.
- **The handle is `{TpuCoreLocation, int}`.** Do not model an infeed/outfeed queue as a device pointer. The runtime re-resolves `topology → chip-node → core → queue[id]` on every enqueue; the literal-transfer path always uses `queue_id = 0`.
- **Layout is mandatory, both directions.** Infeed: `HostShapeToDeviceShape` → `LinearizeToBuffers`. Outfeed: dequeue raw → `Delinearize`. Outfeed additionally requires a tile-populated layout (`GetCompactTiles`) and a supported element type (`SupportedPrimitiveType`); the byte count must be `% 4 == 0`.
- **Spans, not buffers, hit the queue.** Chop each device buffer by `TensorCoreInfeedSpanSizeBytes` (infeed) / `TensorCoreMaxOutfeedSpanSizeBytes` (outfeed). Zero-pad the trailing partial infeed span to full span width in a 32-byte-aligned buffer.
- **Blocking is the API.** Both PJRT entries block the caller until all span callbacks fire (`BlockableAsyncTaskGroup::WaitTillDone` / `Mutex::LockWhenCommon`). The device side is async via per-span `AnyInvocable<void(const Status&)>` callbacks; errors arrive as the callback's `Status`. Per-device infeed/outfeed mutexes serialize concurrent transfers.
- **This is a streaming channel, not a buffer copy.** Contrast [Host↔Device DMA](../dma/host-device-dma.md): a bulk `PJRT_Buffer` copy targets one HBM allocation and completes once; infeed/outfeed feed a program-ordered FIFO that the running executable consumes/produces as it hits `Infeed`/`Outfeed` HLO ops.

---

## Cross-References

- [Host↔Device DMA](../dma/host-device-dma.md) — the *bulk-buffer* transfer path (`TpuRawBuffer` copies, UHI/HIB DMA spans); contrast: infeed/outfeed is the streaming-queue path, distinct from bulk transfer
- [Execute Async on Stream](execute-async-on-stream.md) — the execute path whose running program drives the `Infeed`/`Outfeed` HLO ops these queues feed
- [Stream Semantics](stream-semantics.md) — the `tpu::System` ordering / sequence-point model the enqueue/dequeue callbacks complete against
- [Host Callbacks](host-callbacks.md) — the sibling host↔device control channel (`DoHostCallbackWithStatus`), the other way a running program reaches the host
- [UHI Host-Interface DMA](../dma/uhi-host-interface.md) — the host-interface DMA band that carries the on-chip side of these transfers in the profiler
- [Runtime Overview](overview.md) — where the transfer managers and `tpu::System` sit in the libtpu runtime stack
