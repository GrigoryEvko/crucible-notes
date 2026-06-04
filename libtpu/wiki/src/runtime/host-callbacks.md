# Host Callbacks

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. VA == file analysis address. Other versions will differ.*

## Abstract

A host callback is how a *running* TPU program reaches back into host code mid-execution: the HLO compiler emits a `Send`/`Recv` pair (the lowering of `xla.host_compute` / outside-compilation, and of cross-host `send`/`recv` custom-calls), and at runtime the device pauses on a sync flag, the host runs a registered callback, and the device resumes once the host signals completion and hands back (or consumes) a data chunk. The XLA reference frame is the SendCallback/RecvCallback surface upstream calls `PjRtClient::ExecuteOptions::send_callbacks` / `recv_callbacks`: each callback is keyed by an integer **channel id** carried in the HLO `Send`/`Recv` op, and a *rendezvous* matches the device side of a channel to the host callback registered for that same channel id. This is fundamentally a **channel-keyed rendezvous**, not a FIFO — contrast [Infeed / Outfeed Queues](infeed-outfeed.md), where a transfer names a `{TpuCoreLocation, int queue_index}` and the device consumes entries in program order with no per-transfer identity.

As with infeed/outfeed, libtpu ships the mechanism **twice**, and a reimplementer must not conflate the two. The modern PJRT path is `xla::TpuHostTransferManager` (`learning/45eac/research/pjrt/tpu_pjrt_client.cc`): one transfer manager per execution, constructed with a `{TpuClient*, TpuCoreLocation}` pair, holding **two channel-keyed callback maps** — a send map (device→host, "CopyFromDevice") and a recv map (host→device, "CopyToDevice") — each an `absl::flat_hash_map<uint32_t channel, callback>`. It is registered onto the execute path by `TpuExecutableLoadState::ExecuteLaunchRaw` via `TpuHostTransferManager::SetExecuteEvent(AsyncValueRef<tpu::TpuEvent>)`, and it drains chunks on two dedicated detached threads (`TpuHostTransferManagerSendThread` / `TpuHostTransferManagerRecvThread`). The legacy path is `tensorflow::TpuHostTransferManagerBase` (`learning/45eac/google/xla/tpu_host_transfer_manager.cc`), used by `xla::LocalClient` / TF-TPU op kernels: it parses a `rendezvous_key_base` string into a TF `RendezvousInterface`, keeps a `std::map<int channel, HostSendRecvInfo>`, and is driven by the TPU driver firing `HandleHostCommand`, which decodes a 32-bit command word `{high byte = Send|Recv, low 24 bits = channel}` and matches it through `rendezvous_->Send` / `rendezvous_->RecvAsync(transfer.parsed_key, …)`.

This page owns the host-callback *rendezvous + the transfer-manager registration + the channel-id keying*. The on-device `Send`/`Recv` sync-flag mechanics bottom out in the TPU driver (`deepsea::executor::DeepseaStream::EnqueueOnDeviceSendRecvLocal` / `EnqueueOnDeviceSend` / `EnqueueOnDeviceRecv`) and are described only to the contract depth. The general `Stream::DoHostCallbackWithStatus` host-callback shim (the StreamExecutor-level `host_compute` trampoline) is the leaf the legacy path can also reach and is covered here; the execute path that registers the transfer manager is on [Execute Async on Stream](execute-async-on-stream.md); the bulk infeed/outfeed FIFO is on [Infeed / Outfeed Queues](infeed-outfeed.md).

For reimplementation, the contract is:

- **The two transfer managers** — PJRT `xla::TpuHostTransferManager` (per-launch, `{TpuClient*, TpuCoreLocation}`, two `flat_hash_map<uint32_t,callback>`), vs. legacy `tensorflow::TpuHostTransferManagerBase` (`std::map<int,HostSendRecvInfo>` + TF `RendezvousInterface`) — and the fact that they are independent code paths, not one wrapping the other.
- **The channel-id key** — a callback is found by the integer channel id from the HLO `Send`/`Recv`. A device-side command with no registered callback is **fatal** (`LogMessageFatal`), not a silent drop. PJRT keys on `uint32_t` in a SwissTable; legacy keys on `int` in a red-black tree.
- **The registration → fire → resume rendezvous** — registration happens before launch (`SetExecuteEvent` on PJRT, `Initialize(rendezvous_key_base, RendezvousInterface*)` on legacy); the device fires the host side when it hits the `Send`/`Recv`; the host callback runs, signals via the sync flag, and the device resumes. The execute event the manager holds completes the whole launch.
- **The data-direction asymmetry** — a `Send` op is device→host (the host *receives* a `PjRtChunk` via `HandleSendChunk`, a `CopyFromDeviceCallback`); a `Recv` op is host→device (the host *provides* data via `HandleRecvChunk` driving a `CopyToDeviceStream`, a `CopyToDeviceCallback`). The names invert what a naive reader expects.

| | |
|---|---|
| **PJRT transfer manager** | `xla::TpuHostTransferManager` (`tpu_pjrt_client.cc`), ctor @ `0xf8150c0`, vtable `off_2177B988` |
| **PJRT register-on-execute** | `xla::TpuHostTransferManager::SetExecuteEvent(AsyncValueRef<tpu::TpuEvent>)` @ `0xf813760`, called from `TpuExecutableLoadState::ExecuteLaunchRaw` @ `0xf8109a0` |
| **PJRT send (dev→host) chunk** | `xla::TpuHostTransferManager::HandleSendChunk(uint32_t channel, tsl::Future<PjRtChunk>)` @ `0xf815720` |
| **PJRT recv (host→dev) chunk** | `xla::TpuHostTransferManager::HandleRecvChunk(uint32_t channel, unique_ptr<CopyToDeviceStream>)` @ `0xf815a20` |
| **PJRT callback registration** | `xla::(anon)::SetUpHostCallbacksForDevice(Span<SendCallback>, Span<RecvCallback>, flat_hash_map<long,PjRtTransferMetadata>, flat_hash_map<long,vector<long>>, TpuClient*, TpuDevice*, bool, TpuHostTransferManager*)` (`tpu_pjrt_client.cc`) |
| **PJRT channel key** | `uint32_t` channel id → `absl::flat_hash_map` (send map slots @ `this+8/16/24/32`, recv map slots @ `this+40/48/56/64`) |
| **PJRT drain threads** | `TpuHostTransferManagerSendThread` / `TpuHostTransferManagerRecvThread` (two detached `concurrent::LoopExecutor`, @ `this+192` / `this+200`) |
| **Legacy transfer manager** | `tensorflow::TpuHostTransferManagerBase` (`tpu_host_transfer_manager.cc`); impl `TpuHostTransferManagerImpl` ctor @ `0xeab4080` |
| **Legacy init / rendezvous** | `TpuHostTransferManagerBase::Initialize(Span<HostTransferProto>, rendezvous_key_base, …, RendezvousInterface*, TpuTopology)` @ `0xeab6780` |
| **Legacy device→host dispatch** | `TpuHostTransferManagerBase::HandleHostCommand(HostCommandParams, TpuStackBases)` @ `0xeab5b20` → `HandleSendSF(int,long)` @ `0xeab5520` / `HandleRecvSF(int,long)` @ `0xeab5780` |
| **Legacy channel key** | `int` channel id → `std::map<int, HostSendRecvInfo>` (red-black tree @ `this+24`) |
| **Legacy StreamExecutor shim** | `tpu::TpuStream::DoHostCallbackWithStatus` @ `0xe998fa0` → `HostCallbackTrampoline` @ `0xe999660` (ExecutorApiFn `+440` enqueue, `+368` status return) |
| **Driver send/recv leaf** | `deepsea::executor::DeepseaStream::EnqueueOnDeviceSendRecvLocal` / `EnqueueOnDeviceSend` / `EnqueueOnDeviceRecv` (TPU driver core, opaque) |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile (both managers traced; channel keying, registration, rendezvous, and fatal-on-miss confirmed) |

---

## 1. Two Transfer Managers, One Channel Model

libtpu carries the host-callback machinery twice, exactly as it does for infeed/outfeed. The byte-confirmed split:

| Aspect | PJRT path | Legacy StreamExecutor / TF path |
|---|---|---|
| Class | `xla::TpuHostTransferManager` | `tensorflow::TpuHostTransferManagerBase` (+ `…Impl`) |
| Lifetime | Per execution launch | Per `LocalClient` executable load |
| Constructed with | `{TpuClient*, TpuCoreLocation}` (`0xf8150c0`) | `{int ordinal, xla::Backend*, StreamExecutor*}` (`0xeab4080`) |
| Channel key | `uint32_t` → two `absl::flat_hash_map` (send / recv) | `int` → `std::map<int, HostSendRecvInfo>` |
| Registration | `SetExecuteEvent` (`0xf813760`) on the execute path; callbacks installed by `SetUpHostCallbacksForDevice` | `Initialize(rendezvous_key_base, RendezvousInterface*, …)` (`0xeab6780`) |
| Device-side trigger | TPU completion fulfils `tsl::Future<PjRtChunk>` (send) / drives `CopyToDeviceStream` (recv) | TPU driver fires `HandleHostCommand` with a packed command word |
| Match | hash-map probe on channel id | `RendezvousInterface::Send` / `RecvAsync(parsed_key, …)` |
| Drain | Two detached threads + `concurrent::LoopExecutor` | Inline on the driver-callback thread + TF rendezvous |
| Source root | `learning/45eac/research/pjrt/tpu_pjrt_client.cc` | `learning/45eac/google/xla/tpu_host_transfer_manager.cc` |
| Driver leaf | `tpu::System` events → `DeepseaStream::EnqueueOnDevice*` | `tpu::TpuStream` → `ExecutorApiFn+440` → `DeepseaStream::EnqueueOnDevice*` |

> **GOTCHA — these are not layered.** The PJRT `TpuHostTransferManager` never constructs a `TpuHostTransferManagerBase` and never touches a TF `RendezvousInterface`; it does its own channel matching in two `flat_hash_map`s and fulfils `tsl::AsyncValue`s. The legacy `TpuHostTransferManagerBase` never references `TpuClient` / `tpu::System` directly; it goes through the StreamExecutor `TpuStream` and a TF rendezvous. A reimplementer who assumes the PJRT path forwards through the legacy manager will be wrong. The two only converge inside the TPU driver core's on-device send/recv (`DeepseaStream::EnqueueOnDevice*`), reached by a different last hop. Confidence: CONFIRMED.

There is a third, TFRT-flavoured variant `tensorflow::tfrt_tpu::HostTransferManager` (`TransferDeviceToHost(int, MutableBorrowingLiteral*, function<void(Status)>)` @ `0xe70bae0`, `TransferHostToDevice(int, BorrowingLiteral const&, …)` @ `0xe70c5c0`) — a thin literal-shaped wrapper keyed by `int` channel that forwards into the same machinery. It is the literal-API analogue of the chunk-API PJRT path and is not on the main execute hot path; this page documents the chunk-based PJRT manager and the legacy rendezvous manager.

---

## 2. The Channel-ID Key and the Rendezvous Contract

### 2.1 What names a callback

A host callback does **not** carry a device address or a queue index. It is named by the **integer channel id** that the HLO `Send`/`Recv` op carries (the same `channel_id` field XLA stamps onto the op). The compiler emits the `Send`/`Recv` pair around the host computation; both the device side (the op) and the host side (the registered callback) name the same channel id, and the runtime matches them.

PJRT keys on `uint32_t`. Each `TpuHostTransferManager` holds **two** `absl::flat_hash_map`s laid out inline in the object:

| Map | Direction | HLO op | Callback role | Slots in object |
|---|---|---|---|---|
| Send map | device → host | `Send` | `CopyFromDeviceCallback` (host receives a `PjRtChunk`) | size `this+8`, seed `this+16`, ctrl `this+24`, slots `this+32` |
| Recv map | host → device | `Recv` | `CopyToDeviceCallback` (host supplies data via `CopyToDeviceStream`) | size `this+40`, seed `this+48`, ctrl `this+56`, slots `this+64` |

> **QUIRK — "Send" is device→host, "Recv" is host→device.** The naming is from the *device program's* point of view: the program's `Send` op sends data off-chip to the host (so the host-side callback is a `CopyFromDeviceCallback` and the chunk flows toward the host); the program's `Recv` op receives data from the host (so the host-side callback is a `CopyToDeviceCallback` and the host pushes a chunk down a `CopyToDeviceStream`). A reimplementer who wires `HandleSendChunk` to the host-input path and `HandleRecvChunk` to the host-output path will get the data direction exactly backwards. Confidence: CONFIRMED — the two callback maps and their fatal-miss messages name the directions explicitly (§2.3).

Legacy keys on `int`. `TpuHostTransferManagerBase` holds a single `std::map<int, HostSendRecvInfo>` (a red-black tree rooted at `this+24`), populated by `AddHostTransfer` from the `HostTransferProto`s during `Initialize`. `HostSendRecvInfo` carries the channel id, a `to_host` direction flag (offset +40), the sync-flag value (`+166`), and the parsed rendezvous key.

### 2.2 PJRT registration: `SetUpHostCallbacksForDevice` → `SetExecuteEvent`

For a PJRT execute, the user passes `ExecuteOptions::send_callbacks` / `recv_callbacks` (spans of `SendCallback` / `RecvCallback`). `xla::(anon)::SetUpHostCallbacksForDevice` is the registration entry — its signature is the contract:

```c
// xla::(anon)::SetUpHostCallbacksForDevice   (tpu_pjrt_client.cc)
// builds the per-launch TpuHostTransferManager's two channel maps
SetUpHostCallbacksForDevice(
    Span<const SendCallback>        send_callbacks,   // one per dev->host channel
    Span<const RecvCallback>        recv_callbacks,   // one per host->dev channel
    const flat_hash_map<long, PjRtTransferMetadata>&  metadata_by_channel,  // device/host shapes
    const flat_hash_map<long, vector<long>>&          channel_groups,
    TpuClient*, TpuDevice*, bool use_major_to_minor_data_layout_for_callbacks,
    TpuHostTransferManager* mgr);   // the manager whose maps get populated
```

Each `SendCallback` becomes an entry in the send map keyed by its channel id; the closure stored is invoked with the received `PjRtChunk` (the `__call_func<…SetUpHostCallbacksForDevice…::$_0>` thunk @ `0xf81f900` is the send-callback body — it delinearizes the device chunk to host layout before calling the user callback). Each `RecvCallback` becomes an entry in the recv map (the `…::$_1` thunk handles the `CopyToDeviceStream`).

The manager is bound to the launch by `TpuExecutableLoadState::ExecuteLaunchRaw` (@ `0xf8109a0`), which calls `TpuHostTransferManager::SetExecuteEvent` immediately before `tpu::System::Execute`:

```c
// xla::TpuHostTransferManager::SetExecuteEvent   sub_F813760
// a1 = TpuHostTransferManager, a2 = &AsyncValueRef<tpu::TpuEvent>  (the launch's completion event)
core_loc   = a1+96;                                       // the {TpuCoreLocation} copied at ctor
shm        = TpuCoreLocation::LocalSharedMemory(core_loc, 0);
logger     = client.system().pending_event_loggers();     // *(*(a1+88)+648)+160
if (logger && (lg = logger->get(shm.index_on_host())))    // attach a "done" marker to the event
    lg->vtable+56(done_async_value@a1+176, &execute_event);   // chain the manager's done AV
// then enqueue this manager as a waiter on the execute TpuEvent:
issuer = lock(weak_ptr @ a1+80);                          // throws bad_weak_ptr if expired
if (execute_event.IsConcrete())                           // already done?
    a1->vtable+40(a1, status_from(execute_event));        // fire MaybeCallDoneCallback now
else
    AsyncValue::EnqueueWaiterListNode(execute_event, node, …);  // fire when execution completes
```

So `SetExecuteEvent` makes the transfer manager a *waiter* on the launch's `tpu::TpuEvent`: when execution finishes (or errors), the manager's done path runs (`MaybeCallDoneCallback` @ `0xf815500`) and the manager is torn down once all outstanding chunk transfers have drained. This is how a per-launch transfer manager's lifetime is tied to the execute event rather than to the caller.

### 2.3 PJRT chunk dispatch and fatal-on-miss

When the device hits a `Send`, the TPU completion fulfils a `tsl::Future<PjRtChunk>` and the send-drain thread calls `HandleSendChunk(channel, future)`. When it hits a `Recv`, the recv-drain thread calls `HandleRecvChunk(channel, CopyToDeviceStream)`. Both do a hash-map probe on the channel id and **CHECK-fail** on a miss:

```c
// xla::TpuHostTransferManager::HandleSendChunk   sub_F815720   (dev->host)
// a1 = this, a2 = uint32 channel, a3 = &tsl::Future<PjRtChunk>
mutex.lock(this+184); ++this->outstanding_count[this+168]; mutex.unlock(this+184);
slot = flat_hash_map_find(send_map @ this+8, /*key=*/a2);   // CRC32 hash + SIMD ctrl probe
if (!slot)                                                  // tpu_pjrt_client.cc:4712
    LOG(FATAL) << "No CopyFromDeviceCallback registered for channel " << a2;
issuer = lock(weak_ptr @ this+72);                          // bad_weak_ptr if expired
RET_CHECK(future.IsValid());                                // future.h:420
future.AndThen([slot.callback](StatusOr<PjRtChunk> chunk){ /* run user send callback */ });

// xla::TpuHostTransferManager::HandleRecvChunk   sub_F815A20   (host->dev)
// a1 = this, a2 = uint32 channel, a3 = unique_ptr<CopyToDeviceStream>
slot = flat_hash_map_find(recv_map @ this+40, /*key=*/a2);  // separate map, same probe
if (!slot)                                                  // tpu_pjrt_client.cc:4732
    LOG(FATAL) << "No CopyToDeviceCallback registered for channel " << a2;
mutex.lock(this+184); ++this->outstanding_count[this+168]; mutex.unlock(this+184);
wrapper = operator new(0x2C8);                              // wraps the CopyToDeviceStream
wrapper->vtable = off_2177CF00; wrapper->stream = a3; …
post wrapper to recv LoopExecutor @ this+200;              // invoke recv callback off-thread
```

> **GOTCHA — an unregistered channel is a fatal crash, not a no-op.** Both `HandleSendChunk` (`:4712`) and `HandleRecvChunk` (`:4732`) call `LogMessageFatal` if the channel id is absent from the corresponding map. A reimplementer must register *every* channel the compiled program will use before launch; a `Send`/`Recv` op whose channel has no callback aborts the process. The legacy path is identical — `HandleSendSF` aborts with `"Channel id not found in transfer map."` (`tpu_host_transfer_manager.cc:110`). Confidence: CONFIRMED.

The `outstanding_count` at `this+168` (guarded by the mutex at `this+184`) tracks in-flight chunk transfers; the manager's done callback (`MaybeCallDoneCallback`) only completes the launch once this drains to zero, so a slow host callback holds the launch's completion event open.

### 2.4 Legacy rendezvous: `Initialize` → `HandleHostCommand`

The legacy manager is set up from the compiled program's `HostTransferProto`s and a `rendezvous_key_base` string:

```c
// tensorflow::TpuHostTransferManagerBase::Initialize   sub_EAB6780
// (Span<HostTransferProto> transfers, string_view rendezvous_key_base,
//  string_view device_type, RendezvousInterface* rendezvous, TpuTopology)
collect transfers into InlinedVector<HostTransferProto*,4>;     // 120 B per proto
log "TpuHostTransferManagerBase::Initialize rendezvous_key_base=" << rendezvous_key_base;
for (proto : transfers)
    AddHostTransfer(proto, &transfer_map /*flat_hash_map<string, HostTransfer*>*/, topology);
    // each AddHostTransfer parses the rendezvous key:
    //   "host_compute_rendezvous:" + key, SimpleAtoi(*rendezvous_key, &channel_int),
    //   inserts {channel_int -> HostSendRecvInfo} into the std::map @ this+24
store rendezvous_ @ this+5;                                      // the TF RendezvousInterface*
```

At runtime the TPU driver fires `HandleHostCommand` with a packed 32-bit command word:

```c
// tensorflow::TpuHostTransferManagerBase::HandleHostCommand   sub_EAB5B20
// a2 = &command_word, a3 = &TpuStackBases  (sync-flag pointer)
cmd  = *a2;
type = cmd >> 24;            // high byte: 1 = Send, 2 = Recv
chan = cmd & 0xFFFFFF;       // low 24 bits: channel id
if (type == 1) { HandleSendSF(chan, *a3); return true; }   // dev->host
if (type == 2) { HandleRecvSF(chan, *a3); return true; }   // host->dev
return false;                                              // unknown command -> not handled
```

`HandleSendSF` (`0xeab5520`) walks the `std::map<int, HostSendRecvInfo>` by channel, CHECKs the transfer is `to_host` (`:119`), records the sync flag into the `HostSendRecvInfo`, and matches the host side through the TF rendezvous:

```c
// tensorflow::TpuHostTransferManagerBase::HandleSendSF   sub_EAB5520
info = transfer_map.find(channel);                          // rbtree walk @ this+24
if (!info) LOG(FATAL) << "Channel id not found in transfer map.";   // :110
CHECK(info->to_host);                                       // :119
info->sync_flag_slot->sflag = info->sflag_value;            // *(v4[78]+28) = *(v4+166)
info->sync_flag_slot->ptr   = sync_flag_ptr;                // *(v4[78]+32) = a3
++this->pending_count;                                      // InterlockedIncrement
TF_CHECK_OK(rendezvous_->Send(info->parsed_key, args, val, /*is_dead=*/false));   // :137
```

`HandleRecvSF` is the mirror over `rendezvous_->RecvAsync(parsed_key, …)`, completing into `RendezvousDone` (`0xeab59e0`). The rendezvous key strings are the canonical XLA host-compute keys: `host_compute_channel_{0}_args` / `host_compute_channel_{0}_retvals`, prefixed `host_compute_rendezvous:`.

---

## 3. The StreamExecutor Host-Callback Shim

Below the legacy `TpuHostTransferManagerBase` (and reachable from any StreamExecutor consumer) sits the `Stream::DoHostCallbackWithStatus(absl::AnyInvocable<absl::Status()>)` primitive — the lowest-level "run this closure on the host" hook. It has two backends.

**Host (CPU) — inline.** `stream_executor::host::HostStream::DoHostCallbackWithStatus` @ `0xfe6efe0` runs the closure immediately on the calling thread (`return (*callback)();`). The linked `HostStream` is the synchronous variant, so `BlockHostUntilDone` (`0xfe6f000`) trivially returns `true`. This is the host-memory-staging / trivial-host-op path, not a host-callback rendezvous.

**TPU — C-shim trampoline.** `tensorflow::tpu::TpuStream::DoHostCallbackWithStatus` @ `0xe998fa0` is the legacy outside-compilation realisation:

```c
// tensorflow::tpu::TpuStream::DoHostCallbackWithStatus   sub_E998FA0
holder = operator new(32, 16);                              // closure holder
move AnyInvocable(callback) into holder+0x10;              // 16-byte trivial state + manager/invoker
// enqueue on the TPU command stream; driver fires it once preceding work completes:
ExecutorApiFn()+440( SE_Stream@this+0x88, stream_handle@this+0x80,
                     &TpuStream::HostCallbackTrampoline, holder );
// on failure: MakeErrorImpl<13>("Failed to  host callback.")   (tpu_stream.h:177)

// tensorflow::tpu::TpuStream::HostCallbackTrampoline   sub_E999660   (the C-side fire)
status = (*holder->closure)();                              // run user closure -> absl::Status
ExecutorApiFn()+368( status.code(), StatusMessageAsCStr(status) );  // report into SE_Status
destroy holder->closure; operator delete(holder, 32, 16);
```

> **NOTE — the ExecutorApiFn slots are the contract.** The legacy host-callback path's only libtpu-visible knobs are the `TfTpu_ExecutorApiFn` table offsets: **+440** = enqueue host callback on the TPU stream, **+368** = report the resulting status back into the driver's `SE_Status`. These are part of the TPU-driver C-ABI (a separate task; not the full table). When the TPU stream reaches the enqueued callback, the driver pauses the stream, runs the trampoline on a host thread, flows the `absl::Status` back, and resumes. A reimplementer of the legacy path reproduces the 32-byte closure-holder lifecycle and these two slot calls. Confidence: CONFIRMED for the slot numbers and closure lifecycle; the driver-internal stream-pause/resume is opaque from `libtpu.so` (LOW on the exact driver mechanics).

---

## 4. On-Device Send/Recv (Driver Depth)

Both managers ultimately drive the same on-device send/recv inside the TPU driver core. The device side of a channel is a **sync flag** (`tpu::TpuSyncFlagOnChip`): a `Send` op writes its data and raises a sync flag the host waits on; a `Recv` op blocks on a sync flag the host raises after delivering data. The driver leaves are:

| Driver symbol | Role |
|---|---|
| `deepsea::executor::DeepseaStream::EnqueueOnDeviceSendRecvLocal(DeviceAddressBase, DeviceAddressBase)` | local same-host send↔recv (the fast on-device path) |
| `deepsea::executor::DeepseaExecutor::EnqueueOnDeviceSendLocal(DeviceAddressBase, DeviceAddressBase, TpuSyncFlagOnChip, AnyInvocable<void(Status)>)` | device→host send, with completion callback |
| `deepsea::executor::DeepseaExecutor::EnqueueOnDeviceRecv(DeviceAddressBase, function<void(TpuSyncFlagOnChip)>, AnyInvocable<void(Status)>)` | host→device recv |
| `OnDeviceSendRecvLocalRequest::ExecuteImpl(DeepseaStream*)` | the queued request object the driver dispatches |

The PJRT path reaches these through `tpu::System` events (the manager fulfils `tsl::AsyncValue`s that the driver consumes); the legacy path reaches them through `TpuStream` + `ExecutorApiFn`. A reimplementer needs from this layer only: (a) the device side is a sync-flag wait/raise, not a blocking memcpy; (b) "local" send/recv (`EnqueueOnDeviceSendRecvLocal`) is an optimised same-host shortcut that bypasses the host callback when both ends are on the same host (`"EnqueueOnDeviceSend using same host fast path."`); and (c) the actual silicon sync-flag write and the stream pause/resume are inside the TPU driver core and are opaque from this binary. Confidence: HIGH on the symbol set and the sync-flag model; LOW on the silicon mechanics.

> **QUIRK — host_compute can be disabled, turning Send/Recv into host-to-host copies.** The flag `xla_disable_automatic_host_compute_offload` (string: *"Allow host-to-host copy when automatic host compute offload is disabled…"*) changes whether the compiler offloads a host computation at all. When automatic offload is off, the runtime may satisfy a channel via a host-to-host copy rather than a device-mediated rendezvous. A reimplementer modelling only the device-rendezvous path will mis-handle the disabled-offload configuration. Confidence: HIGH (string + flag confirmed; the exact host-to-host path not traced).

---

## 5. Reimplementation Notes

- **Pick the right manager.** A PJRT consumer (JAX / PyTorch-XLA) passing `send_callbacks` / `recv_callbacks` uses `xla::TpuHostTransferManager`, registered per launch by `SetExecuteEvent`. Only `LocalClient` / TF-TPU op kernels use `tensorflow::TpuHostTransferManagerBase` with its TF `RendezvousInterface`. Implementing one does not implement the other.
- **The key is the channel id, and it is mandatory.** Model the callback table as `map<channel_id, callback>`, two of them on PJRT (send / recv), keyed by `uint32_t`; one on legacy keyed by `int`. Register every channel before launch — an unmatched device command is a `LOG(FATAL)`, not a dropped message.
- **Mind the direction inversion.** `Send` (HLO) = device→host = `HandleSendChunk` = `CopyFromDeviceCallback` (host receives a `PjRtChunk`). `Recv` (HLO) = host→device = `HandleRecvChunk` = `CopyToDeviceCallback` (host pushes via a `CopyToDeviceStream`). Wire them by HLO op semantics, not by the English word.
- **Registration ties the manager to the execute event.** `SetExecuteEvent` makes the manager a waiter on the launch's `tpu::TpuEvent`; the manager's done callback completes the launch only after all outstanding chunk transfers drain (`outstanding_count` at `this+168` under the mutex at `this+184`). A slow host callback delays the launch's completion future.
- **Two drain threads on PJRT.** The PJRT manager spawns `TpuHostTransferManagerSendThread` and `TpuHostTransferManagerRecvThread` (detached `concurrent::LoopExecutor`s at `this+192` / `this+200`); chunk callbacks run on these, off the device-completion thread. The legacy path runs the callback on the driver-callback thread via the rendezvous.
- **The legacy command word is packed.** `HandleHostCommand` decodes `{cmd>>24 = Send|Recv, cmd&0xFFFFFF = channel}`. Reproduce that 8/24-bit split; the high byte is the direction, the low 24 bits the channel.
- **This is a channel-keyed rendezvous, not a FIFO.** Contrast [Infeed / Outfeed Queues](infeed-outfeed.md): infeed/outfeed names a `{TpuCoreLocation, queue_index}` and the device consumes entries in program order with no per-transfer identity; a host callback names a channel id and the runtime matches the device `Send`/`Recv` to the host callback registered for that exact channel. Use host callbacks for `xla.host_compute` / outside-compilation and cross-host `send`/`recv`; use infeed/outfeed for streaming bulk input/output.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::TpuHostTransferManager` | PJRT per-launch channel-keyed callback manager (this page's subject) |
| `tensorflow::TpuHostTransferManagerBase` | Legacy TF-rendezvous channel-keyed manager (this page's subject) |
| `tensorflow::tfrt_tpu::HostTransferManager` | TFRT literal-API variant (`TransferHostToDevice`/`TransferDeviceToHost(int channel, …)`) |
| `TpuExecutableLoadState::ExecuteLaunchRaw` | The execute leaf that calls `SetExecuteEvent` to register the manager |
| `tpu::TpuStream::DoHostCallbackWithStatus` | The StreamExecutor host-callback shim the legacy path can reach |
| `deepsea::executor::DeepseaStream::EnqueueOnDeviceSendRecvLocal` | The TPU-driver on-device send/recv leaf both paths bottom out in |

## Cross-References

- [Infeed / Outfeed Queues](infeed-outfeed.md) — the *streaming-FIFO* host↔device channel; contrast: host callbacks are the channel-id-keyed *rendezvous* path, distinct from the program-ordered infeed/outfeed queues
- [Execute Async on Stream](execute-async-on-stream.md) — the execute path whose `ExecuteLaunchRaw` registers the `TpuHostTransferManager` via `SetExecuteEvent` before `tpu::System::Execute`
- [Completion Loop](completion-loop.md) — how the launch's `tpu::TpuEvent` (the event the transfer manager waits on) completes and propagates status
- [Stream Semantics](stream-semantics.md) — the `tpu::System` / `TpuEventIssuer` ordering model the send/recv completion callbacks fire against
- [Runtime Overview](overview.md) — where the transfer managers and `tpu::System` sit in the libtpu runtime stack
