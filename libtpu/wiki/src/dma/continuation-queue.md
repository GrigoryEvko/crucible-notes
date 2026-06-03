# Continuation Queue

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`); `.data.rel.ro` carries a `0x200000` VMA→file delta; `protodesc_cold` (the embedded `FileDescriptorProto` text) sits at VMA==offset `0xbe8af30`. Other versions will differ.*

## Abstract

The **continuation queue** is how a TPU program ends *without* halting the chip. A normal LLO program terminates with a `scalar-halt` (`LloOpcode 0x25`), which blocks the sequencer until the host posts the next program. When the TensorCore continuation-queue feature is enabled (bit-0 of `Target+0x628`), that trailing halt is suppressed and replaced by a separately-compiled **continuator program** that advances a descriptor ring, DMAs a per-program control record, fires a host interrupt, and *tailcalls* directly into the next program's entry point. The chain runs program→continuator→program→…→`scalar-halt`; only the program with no enqueued successor actually halts. This is libtpu's answer to launch-latency: the device never idles between back-to-back programs in a Megacore run.

Three cooperating layers carry the mechanism. (1) A **config proto** — `TpuChipConfigProto.ContinuationQueue`, registered per-core into the `Target` as a `xla::jellyfish::Target::ContinuationQueueConfig` region (`Target+0x580 + core*0x38`) — declares the four producer sync-flag word offsets, the completion/error sflags, the host interrupt number, and a `per_core` vector of `{shared_memory_region, consumer_sync_flag}`. (2) The **async descriptor record** is a flat `int32` SMEM control block (the per-program block at `Target+0x808..+0x8b8`), built host-side by `ReservedSmemFiller::FillCoreBuffer` with each field placed at the word slot a `TpuMemoryReservationType` reserves, then DMA'd into the shared-memory ring. (3) The **runtime ring** — `tpu::ContinuationQueue` — is a lock-protected host-side producer/consumer with an async worker thread: `Enqueue` posts a `Request`, `WorkerLoop` pops it and dispatches the continuation (which `WriteMemory`-DMAs the record), and `Completed` is the device-interrupt handler.

This page owns the config proto, the async descriptor record, the runtime drain protocol, and the scalar-halt suppression map. The descriptor *enums* — the `(mem_id, core_id)` memory-space encoding, the src/dst opcode enums, the OCI descriptor field layout — belong to the [Intra-Chip DMA Descriptor](intra-chip-descriptor.md) and are not duplicated here; the host↔device transport that physically moves a record into device SMEM is the [UHI Host Interface](uhi-host-interface.md). The reader who already understands a bounded ring buffer with a producer index, a slot count, and a completion flag will recognize the shape immediately; the two surprises this page documents are (1) the "descriptor" is not a packed struct but a flat `int32` array addressed by reservation-type word slots, the *same memory* the device reads back through `Target` accessors, and (2) program termination is a *split-program* model — `halt` and `continuation` are not either/or but two halves the `Target+0x628` bit toggles between.

For reimplementation, the contract is:

- **The config proto** — the 10-field `ContinuationQueue` message (field #2 is a numbered gap), its `PerCore` sub-message, the `SharedMemoryRegion`, and the `asic_sw.deepsea.SyncFlag` consumer-flag handle — with field numbers, types, and C++ member offsets.
- **The async descriptor record** — the flat `int32` SMEM control block, which `TpuMemoryReservationType` slot holds each field, the value sources, the poison fills, and the record byte size `max(0x200, count) + reserved*4`.
- **The runtime drain** — the `tpu::ContinuationQueue` object layout, the ring-window arithmetic, and the `Enqueue` → `WorkerLoop` → `WriteMemory` → `Completed` path; plus the device-side producer ring-advance `EmitContinuationTailcall`.
- **The scalar-halt map** — the five `ShaltInternal` program-end emit sites, the `Target+0x628` bit-0 suppression in `CompileInternal`, the richer per-sequencer gate in `LowerHloModuleImpl`, and the continuator's own closing halt.

| | |
|---|---|
| **Config proto** | `TpuChipConfigProto.ContinuationQueue` (`DescriptorProto` @ `0xc18e12d`, len 649) |
| **Runtime config region** | `xla::jellyfish::Target::ContinuationQueueConfig` @ `Target+0x580 + core*0x38` (stride `0x38`) |
| **Device producer** | `xla::jellyfish::continuations::EmitContinuationTailcall(LloRegionBuilder, long)` @ `0x12718ca0` |
| **Continuator compile** | `deepsea_compiler_backend::CompileContinuationTailCall` @ `0x10a28b00` |
| **Record builder** | `tpu::ReservedSmemFiller::FillCoreBuffer` @ `0x1d4c1d60`; size @ `GetRequiredDescriptorBytes` `0x1d4c2a20` |
| **Runtime ring** | `tpu::ContinuationQueue` ctor @ `0x1d160ae0`; `Enqueue` `0x1d161160`; `WorkerLoop` `0x1d162ba0`; `Completed` `0x1d1616a0` |
| **Enqueue driver** | `tpu::RealProgramContinuator::EnqueueProgram` @ `0x1d153ca0` |
| **Halt opcode** | `ShaltInternal` @ `0x1d520d20` = `CreateNullaryOp(0x25)` ("scalar-halt") + `AppendInstruction` |
| **Enable bit** | `Target+0x628` bit-0 (TC continuation-queue region present) |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile + the carved `FileDescriptorProto` + `TcParseTable` offset arrays |

---

## 1. The `ContinuationQueueConfig` Proto

### Purpose

`TpuChipConfigProto.ContinuationQueue` is the static configuration record that tells both the host runtime and the device producer where the queue lives. One message is registered **per core** (`TpuChipCommonImpl::RegisterContinuationQueueConfigs(AnySpan<const TpuChipConfig::ContinuationQueue>)` @ `0xe72b340`) and copied into the `Target` as a `Target::ContinuationQueueConfig` region at `Target+0x580 + core*0x38`. The producer sync-flag offsets live on the message head; the descriptor-ring region and the consumer flag live in the `per_core` vector. Everything the producer and consumer need to address sync flags and the ring window is reachable from this one message.

### Encoding

The field map is carved byte-exact from the embedded `FileDescriptorProto` (`protoc --decode_raw` of the `DescriptorProto` body @ `0xc18e12d`, len `0x289`=649) and corroborated by the generated `TcParseTable` member-offset array `TpuChipConfigProto_ContinuationQueue::_table_` @ `0x2200cb48` (12-byte `FieldEntry` stride `{u32 member_offset, u16 has_idx, u16 typecard}`) and `Clear()` @ `0x20b0c5a0`.

| Field # | Name | Proto type | C++ off | Confidence |
|---:|---|---|---:|---|
| 1 | `core_type` | enum `.tpu.TpuCoreTypeProto` (TC=0 / BarnaCore=2) | `+0x28` | CONFIRMED |
| (2) | — (absent — a field-number gap) | — | — | CONFIRMED |
| 3 | `completion_state_sync_flag_word_offset` | int32 | `+0x2c` | CONFIRMED |
| 4 | `error_ack_sync_flag_word_offset` | int32 | `+0x30` | CONFIRMED |
| 5 | `overwritten_error_sync_flag_word_offset` | int32 | `+0x34` | CONFIRMED |
| 6 | `completion_interrupt_number` | int32 | `+0x38` | CONFIRMED |
| 7 | `per_core` (repeated `ContinuationQueue.PerCore`) | message | `+0x18` (count `+0x20`) | CONFIRMED |
| 8 | `producer_sync_flag_min_descriptor_word_offset` | int32 | `+0x3c` | CONFIRMED |
| 9 | `producer_sync_flag_remaining_descriptors_word_offset` | int32 | `+0x40` | CONFIRMED |
| 10 | `producer_sync_flag_count` | int32 | `+0x44` | CONFIRMED |
| 11 | `producer_sync_flag_index_word_offset` | int32 | `+0x48` | CONFIRMED |

`has_bits` is at `struct+0x10`. The four producer sflags (#8–#11) are **top-level** `ContinuationQueue` fields, not per-core — there is one queue config per core, so a per-queue field is already per-core.

> **CORRECTION (CONTQ-1) —** an earlier reading of the memory model placed the `producer_sync_flag_*` fields inside `PerCore`. The carved `DescriptorProto` shows they are `ContinuationQueue` fields #8/#9/#10/#11; `PerCore` holds only #1/#2. This is consistent with the device producer (`EmitContinuationTailcall`, [§4](#4-the-device-side-producer-ring-advance)) reading the producer base/remaining/count/index from the per-*core* queue head at `config+0x0/+0x4/+0x8/+0x1c` — those are the runtime head re-layout of fields #8/#9/#10/#11.

### The `PerCore` sub-message

`ContinuationQueue.PerCore` (`DescriptorProto` @ `0xc18e329`, len 138; `_table_` @ `0x2200cab8`) carries exactly two message-typed fields:

| Field # | Name | Proto type | C++ off | Confidence |
|---:|---|---|---:|---|
| 1 | `shared_memory_region` | message `.tpu.TpuChipConfigProto.SharedMemoryRegion` | `+0x18` | CONFIRMED |
| 2 | `consumer_sync_flag` | message `.asic_sw.deepsea.SyncFlag` | `+0x20` | CONFIRMED |

`SharedMemoryRegion` (`DescriptorProto` @ `0xc18dd41`, len 117; `_table_` @ `0x2200c638`) names the descriptor-ring window:

| Field # | Name | Proto type | C++ off | Confidence |
|---:|---|---|---:|---|
| 1 | `shared_memory` | message `.tpu.TpuSharedMemoryOnChipProto` (HBM / CMEM kind) | `+0x18` | CONFIRMED |
| 2 | `word_count` | **INT64** | `+0x20` | CONFIRMED |
| 3 | `word_offset` | **INT64** | `+0x28` | CONFIRMED |

> **NOTE —** `word_count`/`word_offset` are `int64` in the proto. The runtime `Target` `MemoryPart` carries truncated `int32` copies (`+0x04` = `word_offset`, `+0x08` = `word_count`) because on-chip word offsets fit in 32 bits; the ring-window arithmetic in the runtime ctor ([§3](#3-the-runtime-ring)) reads those `int32` copies. Do not assume `int32` at the proto layer.

The `consumer_sync_flag` is the standard `asic_sw.deepsea.SyncFlag` handle (`DescriptorProto` @ `0xc19228a`, len 226) — the sync flag the *device* waits on before reading a descriptor slot:

| Field # | Name | Proto type | Role | Confidence |
|---:|---|---|---|---|
| 1 | `index` | int32 | sflag word index within the core's tier | CONFIRMED |
| 2 | `core` | enum `Core` | which engine owns the flag | CONFIRMED |
| 3 | `core_index` | int32 | per-core instance index | CONFIRMED |
| 4 | `tile_index` | int32 | tile index (SparseCore tiles) | CONFIRMED |

The nested `Core` enum is `{TENSORCORE=0, BARNACORE=1, HOST_INTERFACE=2, SPARSECORE=3, SPARSECORE_TAC=4, SPARSECORE_TEC=5}`. (This is the same `SCS/TAC/TEC` numbering the SparseCore sequencer enum uses; two *other* `SyncFlag` messages exist in the build — `{is_host, word_offset}` @ `0xc18de3b` and `{word_offset, value, expected_transfer_size}` @ `0xc1792af` — and are distinct types, not the consumer flag.)

### The runtime config region head

When `RegisterContinuationQueueConfigs` copies the proto into the `Target`, the head re-layout the device producer indexes (`Target+0x580 + core*0x38`, stride `0x38`) exposes the four producer sflags plus the `per_core` vector:

| Off | Source field | Role | Confidence |
|---:|---|---|---|
| `+0x00` | #8 `…min_descriptor_word_offset` | producer descriptor-array base sflag (`"…available_count_sync_flag_base"`, len `0x31`) | CONFIRMED |
| `+0x04` | #9 `…remaining_descriptors_word_offset` | remaining-descriptors sflag base (`"…_remaining_descriptors_base"`, len `0x47`) | CONFIRMED |
| `+0x08` | #10 `…count` | ring slot count — power-of-2 (`popcnt==1` CHECK'd) | CONFIRMED |
| `+0x1c` | #11 `…index_word_offset` | producer write-index sflag (`"…available_count_sync_flag_index"`, len `0x32`) | CONFIRMED |
| `+0x20` | #7 `per_core` | vector `.begin` (`Target+0x5a0` for TC core 0) | CONFIRMED |
| `+0x28` | #7 `per_core` | vector `.end` (`Target+0x5a8` for TC) | CONFIRMED |

Each `per_core` element (the 28-byte `Target::ContinuationQueueConfig::PerCore`, stride `0x1c`): `+0x00` MemorySpace, `+0x04` `word_offset`, `+0x08` `word_count`, `+0x0c` consumer-sflag ptr (optional), `+0x14` has-consumer bool, `+0x18` producer-sflag word offset. The C++ member-type name `Target::ContinuationQueueConfig::PerCore` is recovered from the `std::vector<…ContinuationQueueConfig::PerCore>::__throw_length_error` symbol @ `0x1271a660` referenced by the producer's overflow path.

---

## 2. The Async Descriptor Record

### Purpose

A "continuation descriptor" is the data a producer DMAs so the next program knows what to run. It is **not** a packed struct with explicit byte offsets — it is a flat `int32` word array (the per-program SMEM control block) where each program-control field occupies the word slot a `TpuMemoryReservationType` reserves. The host fills it field-by-field; the device reads the *same memory* back through the `Target` word-offset accessors of [§4](#4-the-device-side-producer-ring-advance). One record per enqueued program.

### Layout

`ReservedSmemFiller::FillCoreBuffer(TpuCore*, TpuRunConfig const&, Inputs const&, bool, Span<int>)` @ `0x1d4c1d60` writes the record. Every field write goes through the lambda `$_0(TpuMemoryReservationType type, int value, int idx)` @ `0x1d4c2860`, which stores `span[GetRegionForType(type).word_offset] = value`. `GetRegionForType` @ `0x20b16260` indexes a per-gen region table at `[reservation+0x4c0 + type*0x18]` and returns a 24-byte `{word_offset, word_count, …}` region; the type enum has 50 entries (`< 0x32`).

The verified write set (each row is one `int32` slot; the device read-back accessor is on the [Targets overview](../targets/overview.md) program-descriptor SMEM block, `Target+0x808..+0x8b8`):

| `TpuMemoryReservationType` (idx) | Value source (`FillCoreBuffer` / `Inputs`) | Device read-back (`Target` off) | Confidence |
|---|---|---|---|
| `kXprofProgramId` (0x07) | `TpuRunConfig::xprof_program_id()` | `ProgramIdLocation` (`+0x808`) | CONFIRMED |
| `kRunIdLow` (0x08) | `Inputs+0x08` | `RunIdLowLocation` (`+0x820`) | CONFIRMED |
| `kRunIdHigh` (0x09) | `Inputs+0x0c` | `RunIdHighLocation` (`+0x828`) | CONFIRMED |
| `kSameAsLastProgram` (0x12) | 0 / cache-hit flag | `SameAsLastProgram` (`+0x838`) | HIGH |
| `kLaunchBarrierId` (0x13) | 0 | `LaunchBarrierId` (`+0x840`) | HIGH |
| `kCrossProgramPrefetchSuccess` (0x14) | region / flag | `CrossProgramPrefetchSuccess` (`+0x848`) | HIGH |
| `kProgramDescriptorSize` (0x15) | `descriptor_bytes / words-per` | `ProgramDescriptorSize` (`+0x858`) | CONFIRMED |
| `kProgramDescriptorState` (0x16) | `Inputs+0x4c` (1=initial / 2=continuation) | `ProgramDescriptorState` (`+0x860`) | CONFIRMED |
| `kProgramEntryPointAddress` (0x17) | `Inputs+0x38` (NEXT program entry addr) | `ProgramEntryPointAddress` (`+0x868`) | CONFIRMED |
| `kProgramEntryPointSize` (0x18) | `Inputs+0x3c` (entry size) | `ProgramEntryPointSize` (`+0x870`) | CONFIRMED |
| `kHbmStackOffset` (0x05) / `kCmemStackOffset` (0x06) / `kHostStackOffset` (0x21) | region `word_offset` (stack base) | — | CONFIRMED |
| `kHbmOffset` (0x03) / `kCmemOffset` (0x04) | `GetHeapWordOffset(., HBM/CMEM)` | heap slots | CONFIRMED |
| `kTensorCoreStackSize` (0x1d) / `kSparseCoreStackSize` (0x1e) | per-gen stack-size-in-words | — | CONFIRMED |
| `kTrapId` (0x23) | trap-id (0 if none) | — | HIGH |
| `kSparseCorePTState` (0x30) | `0xFFFFFFFF` (unset sentinel) | — | CONFIRMED |
| `kTensorCoreAssertionArgs` (0x31) | `0xC0C0C0C0` (poison fill) | — | CONFIRMED |

The `kProgramDescriptorState` / `kProgramEntryPointAddress` / `kProgramEntryPointSize` writes are confirmed byte-exact: `$_0(span, 22, *(Inputs+19), 0)` (`Inputs+0x4c`), `$_0(span, 23, *(Inputs+14), 0)` (`Inputs+0x38`), `$_0(span, 24, *(Inputs+15), 0)` (`Inputs+0x3c`); the poison fills are `$_0(span, 48, 0xFFFFFFFF, …)` and `$_0(span, 49, 0xC0C0C0C0, …)`.

> **GOTCHA —** the record has **no framing word, no version word, no field-tag bytes**. It is the bare `int32` SMEM control block; "which field" is encoded purely by *which word slot* a reservation type owns. A reimplementer cannot parse this image without the per-gen reservation table (`GetRegionForType`). The record *structure* (type→slot) is byte-exact here; the *literal* word offsets per slot are gen-specific (they come from the chip-parts table). The consumer sync-flag is **not** in the record — it is carried out-of-band as the `WriteMemory` `optional<SyncFlagInfo>` argument ([§3](#3-the-runtime-ring)).

### The record byte size

`ReservedSmemFiller::GetRequiredDescriptorBytes(TpuCore*, TpuRunConfig const&)` @ `0x1d4c2a20`:

```c
// tpu::ReservedSmemFiller::GetRequiredDescriptorBytes   sub_1D4C2A20
function GetRequiredDescriptorBytes(core, run_config):
    count = chip_config.producer_sync_flag_count          // [chipcfg+0xc8]
    min_bytes = max(count, 0x200)                          // floor at 512
    reserved = filler.user_words + run_config.reserved_words   // [filler+0x20] + [run_config+0x28]
    if reserved != 0:
        return min_bytes + reserved * 4                    // int32 words
    return min_bytes
```

The image is allocated by the `MaybeMappedBuffer` functor: first `PremappedMemoryManager::Allocate` (a pinned, DMA-mappable region — returns the device byte offset `[cont+0xc0]-[[cont+0xb8]+8]`), falling back to `ContinuationDescriptor::DefaultAllocator` @ `0x1d6272e0` (`posix_memalign(&p, 0x20, size)`, 32-byte aligned, `free` deleter). The `ContinuationDescriptor` ctor `memset`s the whole image to 0, then the `fill` functor (`FillCoreBuffer` bound via `__bind_front`) writes the fields.

> **QUIRK —** the constant `0x200`=512 recurs three times: as the byte-size floor here, as the runtime max-in-flight cap (`obj+0x58`, [§3](#3-the-runtime-ring)), and as the clamp in `ContinuationDescriptor::Terminator(int)` @ `0x1d627260`. The queue caps at 512 in-flight descriptors; the `Terminator` descriptor (the one that ends the chain) inherits the same cap. Treat 512 as the architectural in-flight limit, not three independent magic numbers.

---

## 3. The Runtime Ring

### Purpose

`tpu::ContinuationQueue` is the host-side producer/consumer that owns the descriptor ring. It is a lock-protected queue with an async worker thread: the XLA side `Enqueue`s a request, the worker thread pops it and dispatches the continuation (which DMAs the descriptor image into device SMEM), and a device interrupt drives `Completed`. The ring window in device SMEM is `[word_offset, word_offset + word_count)` from the selected core's `PerCore.shared_memory_region`.

### Object layout

From the ctor `tpu::ContinuationQueue::ContinuationQueue(...)` @ `0x1d160ae0` (the `per_core[core]` element is selected by the `TpuCoreOnChip` arg's `int32 @+0x4`, vector stride `0x30`):

| Off | Field | Source / role | Confidence |
|---:|---|---|---|
| `+0x00` | config head (ymm copy) | `ContinuationQueue` descriptor `+0..+0x1f` | CONFIRMED |
| `+0x20` | int32 | descriptor `+0x20` | CONFIRMED |
| `+0x28`/`+0x30`/`+0x38` | `per_core` vector `{begin, size, cap}` | heap copy of descriptor `per_core` | CONFIRMED |
| `+0x40` | ring window **START** byte | `per_core[core]+0x10(word_offset) * wordsize` | CONFIRMED |
| `+0x48` | ring window **END** byte | `(per_core[core]+0x8(word_count) + +0x10) * wordsize` | CONFIRMED |
| `+0x50` | descriptor size | ctor arg | CONFIRMED |
| `+0x58` | max-in-flight | `min(arg, 0x200=512)` | CONFIRMED |
| `+0x60` | ring capacity | `(END − START)/2 − descsize` | CONFIRMED |
| `+0x68` | `TpuHostWorkQueue*` | ctor arg | CONFIRMED |
| `+0x70` | core_type / completion interrupt | descriptor `+0xc` | CONFIRMED |
| `+0x78` | user dispatch function | the `std::function` arg | CONFIRMED |
| `+0x88` | `WriteMemory` writeback functor | ctor arg | CONFIRMED |
| `+0xa0` | worker thread | set by `Initialize` (0x160-B state) | CONFIRMED |
| `+0xa8` | `absl::Mutex` (queue lock) | `Enqueue`/`WorkerLoop`/`Completed` | CONFIRMED |
| `+0xb0` | `SyncFlagRefcounts` | constructed if `count > 1`; valid-flag `+0xc8` | CONFIRMED |
| `+0xe0..+0x100` | in-flight `std::deque<Request>` + count | `+0x100` = producer count (inc on `Enqueue`) | CONFIRMED |
| `+0x110..+0x130` | completed container + idx | `+0x130` = consumer index (inc on dispatch) | CONFIRMED |
| `+0x1b0` | running flag | set true by `Initialize`, polled by `WorkerLoop` | CONFIRMED |

The ctor's ring-window arithmetic is the byte-exact proof that `per_core+0x10` is `word_offset` and `per_core+0x8` is `word_count`: START `= word_offset * wordsize`, END `= (word_count + word_offset) * wordsize` — a half-open `[base, base+size)` region. The decompile shows `obj+0x60 = v24/2 - descsize` (capacity) and a `CHECK(descriptor_state_word_offset_ < min_descriptor_size_ / sizeof(int32_t))`, confirming the record is sized in `int32` words.

### The drain protocol

```c
// the host-side producer/consumer drain
function Enqueue(descriptor, device_byte_offset, completion_cb):     // sub_1D161160
    // bounds-check the ring offset against [max_in_flight, capacity)
    if descriptor[+0x28] not in [obj+0x58, obj+0x60):
        completion_cb(OutOfRange status); return                      // failure path
    append Request{descriptor, completion_cb, buffer functors} to in-flight deque
    obj[+0x100]++                                                     // producer count
    post deferred write onto host work queue obj[+0x68]

function WorkerLoop():                                                // sub_1D162BA0
    acquire obj[+0xa8] (mutex 0x21146de0)
    while obj[+0x1b0] (running):                                      // polled flag
        swap out the in-flight deque<Request>
        for each Request req:
            (*req[+0x58])()    // continuation dispatch -> drives WriteMemory -> DMA
            (*req[+0x50])()    // completion callback
            obj[+0x130]++      // consumer index
    release mutex (0x21147b40)

function WriteMemory(dev_off, image:Span<uchar>, len, sync_flag_info, cb):  // sub_1D163A40
    if obj[+0x70] (core_type) == 2: ...                              // host-interface gate
    memcpy the descriptor image into the shared-memory ring          // sub_1D163DD4
    post the write through host work queue with the optional SyncFlagInfo
    invoke writeback functor obj[+0x88]

function Completed(int idx, bool ok):                                // sub_1D1616A0  (device-VInt ISR)
    acquire obj[+0xa8]
    walk the completed container [obj+0x130], match descriptor by idx
    fire its completion callback (req[+0x50]); free the slot
    emplace follow-ons (EmplaceBackSlow 0x1d164d60)
    // ok=false maps to the error_ack / overwritten_error sflag roles (config #4/#5)
```

The in-flight container is a `std::deque<Request>`; each `Request` is `0x80` bytes (`EmplaceBackSlow` does `shl $0x7`). The `Request` carries the `ContinuationDescriptor` (`MaybeMappedBuffer ptr@+0`, deleter `+8`, span-fill fn `+0x10`, mapped-buffer state `+0x18`, ring byte offset `+0x28`), a `WriteMemory` functor `+0x40`, a completion callback `+0x50`, and the continuation dispatch functor `+0x58`.

### The enqueue driver

`tpu::RealProgramContinuator::EnqueueProgram` @ `0x1d153ca0` is the higher-level driver that builds the descriptor and calls `Enqueue`. It calls `GetRequiredDescriptorBytes` (→ size), `memcpy`s the program image, builds a 0x140-byte `Inputs` struct bound to `FillCoreBuffer` via `__bind_front`, constructs a `ContinuationDescriptor(size, fill_fn, alloc_fn)` (@ `0x1d627220` — the variant that calls `fill(Span<int>{buf, size>>2})`), and calls `Enqueue(descriptor, device_byte_offset, completion_cb)` where the offset is the `MaybeMappedBuffer` device-byte-offset (`$_3+0x30`). `ProgramContinuator` exposes `AttachOnEnqueue`/`AttachOnComplete`/`AttachOnError` hooks (@ `0x1d1453e0`/`0x1d145460`/`0x1d1454e0`) wired to its own FSM (`Init/Working/Draining/Drained/TearingDown/TearedDown`, `operator<<` @ `0x1d145600`) — a *distinct* state machine from the device-side descriptor `State` word ([§4](#4-the-device-side-producer-ring-advance)).

> **NOTE —** the host-side selection of `kProgramDescriptorState` = 1 (initial) vs 2 (continuation) is `Inputs+0x4c`; the single predicate that sets it at enqueue time was not isolated (LOW for the *source* of `Inputs+0x4c`; the *write* into the record is CONFIRMED). The device-side `SetProgramDescriptorState(2)` at the Megacore barrier is byte-confirmed ([§4](#4-the-device-side-producer-ring-advance)).

---

## 4. The Device-Side Producer Ring-Advance

### Purpose

The compile-time counterpart of the runtime ring is `EmitContinuationTailcall` — the LLO the continuator program runs on-device to advance the producer index sync flag, address the descriptor-array sflags, read the next program's entry point out of the SMEM control block, DMA the descriptor, fire a host interrupt, and tailcall. This is what physically drains one slot of the ring on the device side and hands control to the successor program.

### Entry Point

```text
TpuCompactionIsaEmitterCodegen::Create  (bit-0 gate @0x1090eb6e)
  └─ deepsea_compiler_backend::CompileContinuationTailCall  @0x10a28b00  ── builds a fresh continuator program
       ├─ EmitContinuationTailcall  @0x12718ca0              ── the ring-advance + DMA + tailcall body
       └─ ShaltInternal  @0x10a28d21 (iff Emit returned rax==1) ── the continuator's OWN closing halt
```

### Algorithm

```c
// xla::jellyfish::continuations::EmitContinuationTailcall(LloRegionBuilder, long)   sub_12718CA0
function EmitContinuationTailcall(rb, nop_count):
    core = TpuCoreTypeForSequencer(module[+0x268])
    cfg  = Target + 0x580 + core*0x38           // the per-core ContinuationQueueConfig head
    // precheck: platform==iss & Megachip & CoresPerChip(SC)>0 & core==2 & Target+0x628 bit-0
    for nop_count iterations: Vnop(); CompilerBarrier()         // overlay-prelude padding

    idx_ptr = SflagImmPtr(cfg[+0x1c], "continuation queue available_count_sync_flag_index", 50)
    idx     = VsyncRead(idx_ptr)                                // current producer write index
    CHECK(absl::has_single_bit(cfg[+0x8]))                      // count must be power-of-2 (popcnt==1)
    new_idx = SandU32(SaddS32(idx, 1), cfg[+0x8] - 1)           // (idx+1) & (count-1)  — ring wrap
    VsyncSet(idx_ptr, new_idx)                                  // advance the producer index sflag

    base_ptr = SflagImmPtr(cfg[+0x0], "continuation queue available_count_sync_flag_base", 49)
    base     = CalcWordAddr(base_ptr, idx)                      // descriptor-array base, indexed by idx
    rem_ptr  = SflagImmPtr(cfg[+0x4], "...available_count_sync_flag_remaining_descriptors_base", 71)
    rem      = CalcWordAddr(rem_ptr, idx)                       // remaining-descriptors sflag

    // per_core loop: cfg[+0x20]..cfg[+0x28], stride 0x1c; each entry checked (+0x14==1 && +0xc==0)
    state = Sld(SmemWordImmPtr(target.ProgramDescriptorStateWordOffset()))   // Target+0x860
    addr  = Sld(SmemWordImmPtr(target.ProgramEntryPointAddressWordOffset())) // Target+0x868 (next program)
    size  = Sld(SmemWordImmPtr(target.ProgramEntryPointSizeWordOffset()))    // Target+0x870
    run_id= Sld(SmemWordImmPtr(target.RunIdLowLocationWordOffset()))         // Target+0x820

    EnqueueDmaLocalInGranules(...)                              // sub_1D540640 — DMA the descriptor
    VsyncAdd / VwaitEqSV / VwaitDone                            // descriptor-ring handshake
    VInt(0x80000000)                                           // host interrupt -> Completed
    tailcall(addr, size)                                       // jump into the next program
    return status                                              // rax==1 -> continuator emits its closing Shalt
```

The `__popcnt(cfg[+0x8]) != 1` check with the assertion string `"absl::has_single_bit(continuation_queue.available_count_sync_flag_count)"` is byte-confirmed at decompile line 310; the `SflagImmPtr(cfg[+0x1c], "…_index", 50)` / `SflagImmPtr(*cfg, "…_base", 49)` / `SflagImmPtr(cfg[+0x4], "…_remaining_descriptors_base", 71)` reads and the `(idx+1)&(count-1)` wrap are all byte-exact. The `core*0x38 + 0x580` indexing (`v6 = v3 + 56*v5 + 1408`) confirms the `0x38` head stride.

### The descriptor `State` handshake

The device-side `ContinuationDescriptor::State` word (SMEM at `Target+0x860`) is a 2-value handshake:

- `SetProgramDescriptorState(State, rb)` @ `0x1271a5e0` `Sst`s a `SimmU32(State)` into the slot. All three callers (in `BarrierCoresWithIdVerificationInternal`, and `SynchronizeProgramDescriptorStatesMegacore` @ `0x1c697540`) pass **State=2**.
- `GetProgramDescriptorState(rb)` @ `0x1271a580` `Sld`s it. `LowerHloModuleImpl` reads it and predicates the next block on `(State SeqS32 1)`.

So **State 1 = first/initial run**, **State 2 = continuation/next-program-ready** (the Megacore barrier synchronizes both twin cores' `State` to 2 before the tailcall). The enumerator *names* have no standalone descriptor in this build; only the literals 1 and 2 are observed.

> **QUIRK —** two unrelated state machines share the word "State." The **device** descriptor `State` (this section, values 1/2) is what the continuator reads to decide whether to predicate the continuation block. The **host** `tpu::ProgramContinuator::State` (`Init/Working/Draining/…`, `operator<<` @ `0x1d145600`, [§3](#3-the-runtime-ring)) is the *driver* FSM. They are distinct; do not conflate. The host FSM drives `Enqueue`; the device `State` word gates the on-chip predicate.

---

## 5. The Scalar-Halt Map

### Purpose

Program termination is a *split-program* model. Without the continuation queue, a program ends in a blocking `scalar-halt` and the host posts the next program. With the queue enabled, the trailing halt is **suppressed** and a separate continuator program advances the queue and tailcalls the successor. This section maps every `scalar-halt` emit site and the gates that toggle them, so a reimplementer knows exactly when a halt is emitted, suppressed, or replaced.

### The halt opcode

`scalar-halt` is `LloOpcode 0x25`, emitted only by `LloRegionBuilder::ShaltInternal` @ `0x1d520d20` (= `CreateNullaryOp(0x25)` + `AppendInstruction`). The opcode names come from the `LloOpcodeString` table @ `0x21cd0d60` (R_X86_64_RELATIVE addends):

| Opcode | String | Confidence |
|---:|---|---|
| `0x25` | `"scalar-halt"` | CONFIRMED |
| `0x26` | `"scalar-halt-yield-cond"` (NOT nullary-emitted in this build) | CONFIRMED |
| `0x27` | `"scalar-halt-on-error"` (`LloRegionBuilder::ErrorIf` / `Error` — error halt, not program-end) | CONFIRMED |

### Program-end emit / suppress sites

The five `ShaltInternal` callers, whole-binary:

| # | Caller | Site VMA | Gate / role | Confidence |
|---:|---|---|---|---|
| 1 | `barna_core::BcsLloProgramCreator::Build()` | `0xf9ce922` | unconditional — BarnaCore sequencer LLO program end | CONFIRMED |
| 2 | `barna_core::BcsLloProgramCreator::BuildTop()` | `0xf9cebdd` | unconditional — BarnaCore top program end | CONFIRMED |
| 3 | `DeepseaCompilerBase::LowerHloModuleImpl()` | `0x10920035` | per-sequencer gate (below) — main HLO lowering program end | CONFIRMED |
| 4 | `DeepseaCompilerBase::CompileInternal()` | `0x10928095` | `test [Target+0x628],1; jne skip` — bit-0 SET **suppresses** the main halt | CONFIRMED |
| 5 | `deepsea_compiler_backend::CompileContinuationTailCall()` | `0x10a28d21` | the continuator's own closing halt (iff `EmitContinuationTailcall` returned `rax==1`) | CONFIRMED |

### The `Target+0x628` bit-0 consumers

Bit-0 (TC continuation-queue region present) is tested at three sites:

| Site VMA | Enclosing function | Action when bit-0 SET | Confidence |
|---|---|---|---|
| `0x10928083` | `CompileInternal` | SKIP the trailing `ShaltInternal` (main program emits **no** halt) | CONFIRMED |
| `0x1090eb6e` | `TpuCompactionIsaEmitterCodegen::Create` | EMIT the continuator: call `CompileContinuationTailCall` @ `0x1090eba0` | CONFIRMED |
| `0x1091bcc7` | `LowerHloModuleImpl` | a `testb $0x4` IsMegachip recheck adjacent to the descriptor-state predicate | CONFIRMED |

So bit-0 does two things in tandem: it suppresses the main program's halt **and** enables compilation of the continuator. The continuator (`CompileContinuationTailCall`) builds its own `LloRegionBuilder`, emits the producer ring-advance + descriptor DMA + tailcall (`EmitContinuationTailcall`), and ends with its own `Shalt` (byte-confirmed: the decompile shows `EmitContinuationTailcall(...)` then a conditional `ShaltInternal`).

### The `LowerHloModuleImpl` per-sequencer gate

Site #3 above uses a richer predicate than `CompileInternal`'s plain bit-0 test (byte-exact @ `0x1091ffc0..0x10920035`):

```c
// the program-end halt decision in LowerHloModuleImpl
if Megachip() && CoresPerChip(SC) > 0 && sequencer == 2 (BarnaCore)
        && (((Target+0x628 bit-2) >> 2) | Target+0x540) & 1:
    emit Shalt                                   // megachip BarnaCore: blocking halt
else:
    bit = (1 << sequencer) & Target+0x628
    if bit == 0 || sequencer < 3:
        emit Shalt                               // no continuation-queue bit for this sequencer
    else:                                        // bit set && sequencer >= 3
        SKIP Shalt                               // continuation queue present for this sequencer
```

Here `sequencer` is the `TpuSequencerType` per-core index and `Target+0x540` is `platform==iss`. The continuation-queue presence is a *per-sequencer* bit `(1 << sequencer) & Target+0x628`, not a single global flag — a Megachip run can have some sequencers halting and others continuing.

> **GOTCHA —** "halt" and "continuation" are not mutually exclusive at the chip level. A naive reimplementation that treats bit-0 as "no halt anywhere" is wrong: the *main* program emits no halt, but the *continuator* still ends in a `Shalt`, and any sequencer whose `(1 << sequencer)` bit is clear still halts normally. The terminal program in a continuation chain — the one with no enqueued successor, i.e. the `Terminator` descriptor — is what actually halts the chain.

---

## Related Components

| Name | Relationship |
|---|---|
| `tpu::RealProgramContinuator` | the host driver that builds descriptors and calls `Enqueue`; owns the queue and the `OnEnqueue/OnComplete/OnError` hooks |
| `tpu::ReservedSmemFiller` | renders the descriptor record (`FillCoreBuffer`) and computes its size (`GetRequiredDescriptorBytes`) |
| `xla::jellyfish::continuations::EmitContinuationTailcall` | the device-side LLO that drains a slot, DMAs the record, and tailcalls |
| `DeepseaCompilerBase` / `TpuCompactionIsaEmitterCodegen` | the compile-time gate (`Target+0x628` bit-0) that suppresses the main halt and emits the continuator |

## Cross-References

- [Intra-Chip DMA Descriptor](intra-chip-descriptor.md) — the `(mem_id, core_id)` memory-space encoding, the src/dst opcode enums, and the OCI descriptor field layout that the actual on-chip DMA (issued by `EnqueueDmaLocalInGranules` here) uses; this page does not duplicate those enums
- [UHI Host Interface](uhi-host-interface.md) — the host↔device transport that physically moves a continuation descriptor image into device SMEM
- [Host↔Device DMA](host-device-dma.md) — the host-path DMA classification (`DMA_TYPE_CHIP_TO_HOST` / `DMA_TYPE_LOCAL_OR_HOST`) the continuation queue's record transfer rides on
- [OCI Command DMA-ID](oci-command-dma-id.md) — the descriptor begin/end trace-id pairing that profiles the continuation DMAs
- [SFLAG Protocol](../memory/sflag-protocol.md) — the sync-flag tier the producer index / remaining-descriptors / consumer flags live in
- [SMEM Scalar Memory](../memory/smem-scalar-memory.md) — the scalar memory tier the flat `int32` descriptor record and the program-descriptor control block occupy
- [Memory Overview](../memory/overview.md) — the continuation-queue subsection in the broader on-chip memory model
- [Targets Overview](../targets/overview.md) — the program-descriptor SMEM control block (`Target+0x808..+0x8b8`) the record is read back through on-device
