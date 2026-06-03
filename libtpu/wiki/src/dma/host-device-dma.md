# Host↔Device DMA

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`); `.data.rel.ro` carries a `0x200000` VMA→file delta. Other versions will differ.*

## Abstract

A host↔device DMA is a transfer whose *other* endpoint lives in host memory: an infeed (host→device) feeding a TensorCore queue, an outfeed (device→host) draining one, a direct-write of activation data, or a physical-address response from the host-interface bridge. The on-chip descriptor for the *local* leg of such a transfer is the `OciDescriptorCommonIssuedFromTcs` documented on [Intra-Chip DMA Descriptor](intra-chip-descriptor.md); this page documents what happens to the *cross-boundary* leg in the profiler — how the device trace stream's host-interface events are reassembled into one host↔device transfer span, which direction (H2D vs D2H) it is bucketed as, and what the host endpoint contributes to the rendered event.

The reassembly is `DeriveHostDmaTransfers` — a pair of timeline producers (one per silicon generation) that scan the merged device trace, pair a "started" host-DMA event with its "completion" event on a per-transaction key, and emit one `XEvent` onto a host-DMA lane. The `pxc` (Pufferfish/BarnaCore) generation does this through the **UHI host-interface band** (trace points `{0, 2, 4}`) and produces a `DmaTransfer` record carrying **kind tag 6 (`MemcpyH2D`) or 7 (`MemcpyD2H`)** — the only place in the entire binary that writes those two tags. The `jxc` (Jellyfish) generation does it through a **different mechanism entirely**: a `SyncFlagUpdate`-deque pairing over the HIB (host-interface block) descriptor band, keyed on the sync-flag target rather than a transaction id, rendered onto the Sync-Flag lanes. This page owns the derivation, the tag-6/7 semantics, and the host endpoint; the UHI message *wire format* and the full `QueueIdValues` enum are on [UHI Host-Interface DMA](uhi-host-interface.md), and the `DmaTransfer` record + per-span XStat set are on [Intra-Chip DMA Descriptor §5](intra-chip-descriptor.md#5-what-the-renderer-keeps-vs-drops).

For reimplementation, the contract is:

- **The two `DeriveHostDmaTransfers` passes** — the `pxc` UHI host-DMA pass (the source of `MemcpyH2D`/`MemcpyD2H` spans) and the `jxc` HIB host-DMA pass (the `SyncFlagUpdate`-deque pairing), and why they differ structurally.
- **The tag-6/7 selector** — the byte-exact `(queue_id & ~1) == 2 ? 6 : 7` rule that buckets a host DMA as H2D vs D2H from the `queue_id` of its STARTED event, and the resulting lane (63 / 64).
- **The transfer build** — how a STARTED event opens a `DmaTransfer` slot (begin gtc, byte count, queue label, kind tag) and a RESPONSE event closes it (end gtc), paired on `trace_id_header.transaction_id`.
- **The host endpoint** — what the host-side fields (`queue_id`, `dva`, `sequence_number`) contribute: `queue_id` becomes the H2D/D2H selector and the `queue` XStat; `dva` (the device virtual address) and `sequence_number` are decoded but dropped, so no host↔device address is rendered.

| | |
|---|---|
| **`pxc` host-DMA producer** | `xprof::tpu::ConvertTpuTraceToXPlane<…pxc::profiler::TraceEntry>{lambda}{lambda}` @ `0xf26afa0` |
| **`pxc` on-chip (ICI) producer** | same template, sibling lambda @ `0xf26c6e0` (does **not** write tags 6/7) |
| **`jxc` host-DMA producer** | `DeriveHostDmaTransfers`, inlined @ `0xf252f00`–`0xf25303b` into the `jxc` lambda @ `0xf252260` |
| **Shared renderer** | `xprof::tpu::ConvertDmaTransfersToXPlane(absl::Span<DmaTransfer>, …)` @ `0xf254bc0` |
| **`jxc` populate side** | `xprof::tpu::ConvertDmaDescriptorToXPlane` @ `0xf253620` |
| **`jxc` consume side** | `xprof::tpu::ConvertDmaEndsToXPlane<…jxc…>` @ `0xf25c500` |
| **Tag-6/7 selector** | `(((queue_id & ~1) == 2) ? 6 : 7)` @ `0xf26b5a6`; the **sole** match in `.text` |
| **Host-DMA lanes** | `TpuComponent` 63 `MemcpyH2D`, 64 `MemcpyD2H` (`pxc`); 17 `Tensor Core Sync Flag`, 23 `Barna Core Fabric Sync` (`jxc`) |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile + the FDP descriptor pool |

---

## 1. Two Generations, Two Host-DMA Derivations

`ConvertDmaTransfersToXPlane` @ `0xf254bc0` — the shared renderer that turns a span of `DmaTransfer` records into device `XEvent`s — has **16 callers** (an `e8`-rel32 scan of `.text`), one host-DMA producer plus one on-chip ICI producer per silicon generation, plus merge/helper sites. Of these, exactly **one** writes a kind tag of 6 or 7: a byte-scan of all of `.text` for the tag selector pattern (`83 e0 fe` = `and ~1`, then `83 f1 07` = `xor 0x7`) returns a **single** hit, at `0xf26b5aa`, inside the `pxc` host-DMA producer. The newer SparseCore generations (`vfc`/`vlc`/`glc`/`gfc`) call the shared renderer but never emit a `MemcpyH2D`/`MemcpyD2H` span; `jxc` routes host DMA through a different pass entirely.

| Aspect | `pxc` host-DMA (UHI band) | `jxc` host-DMA (HIB band) |
|---|---|---|
| Producer | lambda @ `0xf26afa0` | `DeriveHostDmaTransfers` (inlined) @ `0xf252f00` |
| Trace points | UHI `{0, 2, 4}` | `nf_descriptor_hib` + 4 HIB bands |
| Pairing key | `transaction_id` (32-bit) | `sync_flag_target` (`UpdatedSyncFlagTarget`) |
| Begin marker | STARTED (id 0) | `nf_descriptor` source-sync-flag target |
| End marker | RESPONSE_READ/WRITE (id 2 / id 4) | `LastSyncForTracedDma` completion |
| Byte count | `size` (f5, raw) | `length << 10` (1 KiB units, `GetDmaSize`) |
| KIND / lanes | tag 6/7 → `MemcpyH2D` 63 / `MemcpyD2H` 64 | `kind` → `DMA Local`/`DMA Remote`/`DMA H2D` |
| Render lanes | 63 / 64 | 17 `Tensor Core Sync Flag` / 23 `Barna Core Fabric Sync` |
| `queue` XStat | `QueueId` enum name (FILLED) | (carried on the descriptor XEvent) |
| Renderer | shared `ConvertDmaTransfersToXPlane` | `ConvertDmaDescriptorToXPlane` + `ConvertDmaEndsToXPlane` |

> **NOTE —** the `pxc` template `ConvertTpuTraceToXPlane<…pxc::profiler::TraceEntry>` instantiates *two* nested lambdas that both feed the shared renderer: the **first** (@ `0xf26afa0`, call @ `0xf26b9b7`) is the UHI host-DMA pass documented here; the **second** (@ `0xf26c6e0`) is the ICR Node-Fabric *on-chip* pass that emits ICI tags 2/3 onto lanes 54/55 (see [ICR DMA Timeline Band](../profiling/icr-dma-timeline-band.md)). They share the renderer and the `DmaTransfer` record shape but key on different trace points and write different tags. Confidence: CONFIRMED (the 16-caller split and the single tag-6/7 byte-scan hit).

---

## 2. The `pxc` UHI Host-DMA Derivation

### 2.1 The three trace points

The producer builds a 3-element `TracePoints` list (one packed `{band, id}` entry per UHI host-interface event), passes it to `GetMerged`, then iterates the merged, time-ordered stream. The list is byte-visible in the decompile: each entry is a packed 64-bit `(band << 32) | id`. The first entry is `0x400000000` = `(band 4) | (id 0)` — the STARTED event; entries for ids 2 and 4 are appended, and the list size is set to 3 before the call:

```c
// xprof::tpu::ConvertTpuTraceToXPlane<pxc::…TraceEntry>{lambda}{lambda}   sub_F26AFA0
*v10        = 0x400000000LL;     // @0xf26afee  entry[0] = (band 4)|(id 0) = STARTED
v11[2]      = 2;                 // @0xf26b03d  entry[1] id 2  (Read response)
// + entry[2] id 4 (@0xf26b04e), list size = 3 (@0xf26b063)
TpuTraceEntries<…pxc…TraceEntry>::GetMerged(&v90, v8, &ptr);  // @0xf26b07f, ids {0, 2, 4}
```

| `trace_point_id` | UHI message (`pxc::profiler::`) | oneof case | Role |
|---:|---|---:|---|
| 0 | `UhiHostDmaTransactionStartedAddressTranslation` | 2 | BEGIN + byte count + queue + tag |
| 2 | `UhiHostPhysicalResponseRead` | 4 | END (read response) |
| 4 | `UhiHostPhysicalResponseWrite` | 6 | END (write response) |

The per-entry dispatch reads `trace_point_id` (`TraceHeader+0x18`) and branches on it (decompile confirms `if (v21 == 4)` → Write-response, `if (v21 == 2)` → Read-response, fall-through `== 0` → Started), then resolves the matching message global (`UhiHostDmaTransactionStartedAddressTranslation_globals_` @ `0x2237ac40`, `UhiHostPhysicalResponseRead_globals_` @ `0x2237ab98`, `UhiHostPhysicalResponseWrite_globals_` @ `0x2237ab70`) with the standard proto2 oneof-case guard. The id↔name binding (id 0 = `UHI_HOST_DMA_TRANSACTION_STARTED_ADDRESS_TRANSLATION`, id 2 = `UHI_HOST_PHYSICAL_RESPONSE_READ`, id 4 = `UHI_HOST_PHYSICAL_RESPONSE_WRITE`) is the same as in the UHI band's trace-point registry. Confidence: CONFIRMED (byte-exact seed + the `== 4 / == 2 / == 0` dispatch + the three message globals).

### 2.2 The STARTED event — opening the transfer (the host endpoint)

The id-0 STARTED event is the only one that carries the host endpoint. Its field layout (see [UHI Host-Interface DMA](uhi-host-interface.md) for the full wire format) and what the producer does with each field:

| f# | Field | Type | cpp off | Producer use |
|---:|---|---|---:|---|
| 1 | `trace_id_header` | `TraceIdHeader` | `+0x18` | `dma_id` = `transaction_id` (`hdr+0x18`) — the pairing key |
| 2 | `queue_id` | `QueueIdValues` | `+0x20` | KIND-tag selector (§3) **and** the `queue` XStat (enum name) |
| 3 | `sequence_number` | u32 | `+0x24` | decoded, **dropped** |
| 4 | `dva` | u64 | `+0x28` | device virtual address — decoded, **dropped** |
| 5 | `size` | u32 | `+0x30` | `byte_count` = `size` (no granule shift) |

The begin-store block (`@0xf26b560`, byte-exact) opens a `DmaTransfer` slot in the `transaction_id`-keyed map (the slot is the map value form; offsets are within it):

```c
// id-0 STARTED begin store   @0xf26b560
slot.begin_gtc      = gtc;                  // [slot+0x8]  = TraceHeader.timestamp (+0x20)
slot.begin_present  = 1;                    // [slot+0x10]
slot.byte_count     = msg.size;             // [slot+0x28] = [msg+0x30], NO shift   @0xf26b568
slot.queue.ptr      = NameOfDenseEnum(queue_id);  // [slot+0x30]  (QueueIdValues name / SSO)
slot.queue.len      = …;                    // [slot+0x38]
slot.kind_tag       = (((queue_id & ~1) == 2) ? 6 : 7);  // [slot+0x40]   §3
```

The queue label is fetched with `proto2::internal::NameOfDenseEnum<&…QueueIdValues_descriptor, 0, 21>` (the dense 22-value enum, indices 0..21) — fast path indexes the descriptor table, slow path falls to `NameOfDenseEnumSlow`. This is the per-DMA "queue" string the renderer attaches as XStat 79; it is **non-empty** on host-DMA spans (it is empty on the ICI on-chip band).

> **GOTCHA — the host endpoint is dropped.** The STARTED event carries a *device virtual address* (`dva`, f4 @ `+0x28`) — the one field that names the device end of the host↔device copy — but the producer never reads it. Neither does it read `sequence_number`. Only `trace_id_header` (for the key), `queue_id` (for the tag + label), and `size` (for the byte count) survive into the rendered span. So a captured `pxc` host-DMA `XEvent` shows *how much* and *which queue*, but **not** the host or device addresses. This mirrors the on-chip band, which also discards its `src`/`dst` endpoints ([Intra-Chip DMA Descriptor §5](intra-chip-descriptor.md#5-what-the-renderer-keeps-vs-drops)). No downstream symbolizer that re-reads `dva` is linked into `libtpu.so`. Confidence: CONFIRMED (the producer reads only `+0x18`/`+0x20`/`+0x30` of the STARTED message).

### 2.3 The RESPONSE event — closing the transfer

The id-2 (`UhiHostPhysicalResponseRead`) and id-4 (`UhiHostPhysicalResponseWrite`) events are END markers. Each carries `{f1 trace_id_header @+0x18, f2 is_l2_pte_fetch (bool) @+0x24, f3 chunk_id (u32)}`; the producer reads only the `trace_id_header` (to find the slot) and the event gtc. The `is_l2_pte_fetch`/`chunk_id` fields are decoded but dropped. The end-store block (`@0xf26b0d0`):

```c
// id-2 / id-4 RESPONSE end store   @0xf26b0d0
slot.end_gtc      = gtc;   // [slot+0x18] = TraceHeader.timestamp
slot.end_present  = 1;     // [slot+0x20]
```

**Pairing.** The slot key is `trace_id_header.transaction_id` *alone* — a full 32-bit per-transaction tag, zero-extended to the `flat_hash_map<u64, DmaTransfer>` key (policy @ `0x21646fe8`), **not** masked to the 38-bit composite the on-chip ICR band uses (which folds `core_id`/`chip_id`). A host DMA has exactly one `transaction_id`; its STARTED (id 0) and its RESPONSE (id 2 read / id 4 write) share that id and write into the same slot, producing one `DmaTransfer {begin, end, byte_count, queue, tag}`. If the slot is already full when a STARTED arrives, the producer flushes the old transfer and reopens (`@0xf26b52f`). Confidence: CONFIRMED.

> **NOTE — read-response vs write-response is *not* the H2D/D2H signal.** It is intuitive to assume id-2 (READ response) closes a device→host read and id-4 (WRITE response) closes a host→device write, but the producer keys the *direction* entirely off `queue_id` from the STARTED event (§3) — both response variants merely supply the end timestamp. A reimplementer must not infer direction from which RESPONSE id closed the slot. Confidence: CONFIRMED (the end store writes only gtc/present; the tag is fixed at begin time).

---

## 3. The Tag-6/7 (H2D vs D2H) Selector

The central line of the host-DMA path is the kind-tag computation, byte-exact at `0xf26b5a6` and the **only** such site in `.text`:

```c
// @0xf26b5a6 — the sole tag-6/7 producer in the binary
// asm: mov eax, queue_id ; and eax, 0xFFFFFFFE ; cmp eax, 2 ; sete cl ; xor ecx, 0x7
slot.kind_tag = (((queue_id & ~1) == 2) ? 6 : 7);   // decompile: (((v22[4] & 0xFFFFFFFE) == 2) ^ 7)
```

`(queue_id & ~1) == 2` is true for exactly `queue_id ∈ {2, 3}` — `QUEUE_ID_DIRECTWRITEQUEUE0` and `QUEUE_ID_DIRECTWRITEQUEUE1`. `sete` produces 1, then `xor 1, 7` = 6; for every other `queue_id`, `sete` produces 0, then `xor 0, 7` = 7.

| `queue_id` | Queue class | `(queue_id & ~1) == 2` | KIND tag | Lane |
|---:|---|:---:|---:|---|
| 2, 3 | `DIRECTWRITEQUEUE0/1` | yes | **6** (`MemcpyH2D`) | 63 |
| 0, 1, 4..21 | debug / magic / infeed / outfeed / reserved | no | **7** (`MemcpyD2H`) | 64 |

So **only the two direct-write queues are bucketed as host→device**; every other queue — including the infeed queues, which one might naively expect to be H2D — falls to D2H. The full 22-value `QueueIdValues` enum and the rationale live on [UHI Host-Interface DMA](uhi-host-interface.md).

> **GOTCHA — the direction is a trace-design bucketing, not a physical direction.** The rule is byte-fixed (`and ~1; cmp 2`), but whether classifying `INFEEDQUEUE*`/`OUTFEEDQUEUE*` as D2H reflects the hardware's true data direction or is a deliberate timeline-bucketing choice is **not** provable from `libtpu.so` — only a captured XSpace would show the resulting lane placement. The literal predicate is CONFIRMED; the firmware queue→direction convention it encodes is LOW (decoded as the predicate, not validated against silicon).

### The tag → lane / event-metadata map

The shared renderer's kind switch indexes a jump table @ `0xab589bc` by `(kind_tag - 2)` (`add $0xfffffffe,%eax; cmp $5,%eax; ja skip; movslq (r9,rax,4),rax` @ `0xf255041`–`0xf25504c`, so the valid tag range is 2..7), giving distinct arms for tags 6 and 7 at table indices 4 and 5:

| tag | jt idx | jt arm | `TpuComponent` line | XEvent metadata | Source |
|---:|---:|---|---|---|---|
| 6 | 4 | `0xf25507e` | 63 `MemcpyH2D` | `"MemcpyH2D"` | `queue_id ∈ {2,3}` (direct-write) |
| 7 | 5 | `0xf255340` | 64 `MemcpyD2H` | `"MemcpyD2H"` | all other `queue_id` |

The decompile confirms the renderer interns `GetOrCreateEventMetadata("MemcpyH2D")` (@ `0xf254e5f`, length 9) and `GetOrCreateEventMetadata("MemcpyD2H")` (@ `0xf254e82`, length 9) ahead of the kind switch, then routes tags 6/7 to the H2D/D2H arms. Confidence: CONFIRMED. (This corrects an earlier sibling note that listed tags 6/7 as "unused on `pxc`" and a transposed tag/lane table: tag 6 = H2D = lane 63, tag 7 = D2H = lane 64.)

### The rendered span

The per-span XStats are the shared `DmaTransfer` set documented on [Intra-Chip DMA Descriptor §5](intra-chip-descriptor.md#5-what-the-renderer-keeps-vs-drops), with two host-DMA specifics:

- `bytes_transferred` (78) = `size`, the **raw** byte count — *no* granule shift, unlike the on-chip band's `length << {2,9}`.
- `queue` (79) = the `QueueId` enum name (e.g. `QUEUE_ID_DIRECTWRITEQUEUE0`) — **non-empty** on host-DMA spans. `details` (string) stays **empty** (the producer writes `[slot+0x30]`/`[slot+0x38]` = queue, never `[slot+0x48]` = details).

---

## 4. The `jxc` HIB Host-DMA Derivation

`jxc` is proto2-self-describing and emits **no** `MemcpyH2D`/`MemcpyD2H` span. Its host-interface (HIB) DMA timeline is built by `DeriveHostDmaTransfers` — a `SyncFlagUpdate`-deque pairing that records descriptor-staged "begins" and matches them to sync-flag-update "completions". The intra-chip descriptor that stages each begin is the one on [Intra-Chip DMA Descriptor](intra-chip-descriptor.md); this section documents the host↔device *pairing* built on top of it.

### 4.1 The five trace points

`DeriveHostDmaTransfers` is inlined into the `jxc` `ConvertTpuTraceToXPlane` lambda @ `0xf252260`; its static-init block @ `0xf252f00`–`0xf25303b` constructs five function-local `TracePoint` statics (five `__cxa_guard_acquire`/`release` pairs), then merges them via a `TpuTraceEntries::GetMerged` call @ `0xf252382` (callee @ `0xf253080`):

| Static | Band / id | Role |
|---|---|---|
| `nf_descriptor_hib` | `nf_descriptor` (case 3), id 2 (HIB) | host-side staged descriptor — the begin |
| `nf_hib_write_cmd` | `nf` (case 6), `HIB_WRITE_COMMAND` (id 22) | HIB write command |
| `nf_hib_write_data_end` | `nf` (case 6), `HIB_WRITE_DATA_END` (id 23) | HIB write-data end |
| `hib_hbm_write` | `hib_hbm_write` (case 19), `HBM_WRITE` (id 87) | HIB→HBM write |
| `hib_sync_update` | `hib_sync_update` (case 18), `SYNC_UPDATE` (id 86) | sync-flag-update — the completion |

These five are the `jxc` twin of the `pxc` UHI host-DMA band. Confidence: CONFIRMED (five guard-var symbols + the inlined init).

### 4.2 The `SyncFlagUpdate` deque map

The pairing structure is `absl::flat_hash_map<uint64 sync_flag_target, std::deque<SyncFlagUpdate>>` (policy @ `0x21646f10`; the policy type is byte-visible in the decompile). `SyncFlagUpdate` is a 0x38-byte (56-byte) POD (stride confirmed by `MergeSyncFlagUpdates`' `add r15, 0x38`):

| Offset | Field | Use |
|---:|---|---|
| `+0x00` | `sync_flag_target` | the PAIRING KEY (`SourceSyncFlagTarget` / `UpdatedSyncFlagTarget` result) |
| `+0x08` | `dma_id` | the flow-stat value source (`& 0xff_ffff_ffff_ffff` → XFlow id) |
| `+0x10` | `kind` | the `DMA Local`/`Remote`/`H2D` event-metadata selector (0..4) |
| `+0x18`..`+0x37` | begin_gtc / size / endpoint | recorded begin metadata (reflection-filled) |

Per-core maps are combined by `MergeSyncFlagUpdates` @ `0xf248ee0` (iterates each map's `{key, deque}` and merges the deques) before rendering.

> **NOTE —** the `+0x18`..`+0x37` slots (begin_gtc, dma size, endpoint chip/node) are filled by the standard reflection walk in the populate side; `target`/`dma_id`/`kind` are byte-pinned, but a field-by-field map of the remaining 0x20 bytes was not single-stepped. The pairing *semantics* are decoded; the exact begin-metadata slot layout is CONFIRMED-PARTIAL.

### 4.3 The populate side — `ConvertDmaDescriptorToXPlane`

`ConvertDmaDescriptorToXPlane` @ `0xf253620` renders each staged HIB descriptor as a descriptor `XEvent` **and** enqueues a pending begin:

```c
// xprof::tpu::ConvertDmaDescriptorToXPlane   sub_F253620
// interns 4 event names: "DMA Local"(9), "DMA Remote"(10), "DMA H2D"(7), "DMA D2H"(7) + flow StatType 0x38
for (wrapper : descriptors):
    dma_id = wrapper.GetDmaId();                  // jxc composite   @0xf698180
    if (wrapper.EntryDataCase() != 3) continue;   // gate: nf_descriptor   @0xf25388b
    key    = wrapper.SourceSyncFlagTarget();      // sync_flag_target   @0xf6982e0
    slot   = map.find_or_prepare_insert_large(key);   // @0xf24f320
    slot.push_back(SyncFlagUpdate{ .target = key, .dma_id = dma_id, .kind = …, … });
    bytes  = wrapper.GetDmaSize();                // = length(@descr+0x48) << 10 (1 KiB)   @0xf6982a0
```

`SourceSyncFlagTarget` @ `0xf6982e0` is the descriptor's source-sync-flag-update target (the OCI SyncFlag fold over `descr+0x60`); each staged HIB descriptor enqueues a pending begin under that target. `GetDmaSize` is byte-confirmed: `nf_descriptor` (case 3) → `length(@descr+0x48) << 10`. Confidence: CONFIRMED (the four event names, the `find_or_prepare_insert_large` on the `deque<SyncFlagUpdate>` policy, `GetDmaId`/`SourceSyncFlagTarget`/`GetDmaSize` calls all present in the decompile).

### 4.4 The consume side — `ConvertDmaEndsToXPlane<jxc>`

`ConvertDmaEndsToXPlane<jxc>` @ `0xf25c500` consumes the completions and emits the paired host-DMA span:

```c
// xprof::tpu::ConvertDmaEndsToXPlane<…jxc…>   sub_F25C500
// interns 3 event names: "DMA Local"(9), "DMA Remote"(10), "DMA H2D"(7) + flow StatType 0x38
for (wrapper : completions):
    if (!wrapper.LastSyncForTracedDma()) continue;     // gate: only the LAST sync   @0xf6986a0
    key  = wrapper.UpdatedSyncFlagTarget();             // completion target   @0xf698400
    begin = map.lookup(key).pop_front();                // SwissTable lookup of pending begin
    name = event_meta[begin.kind];                      // cmp kind,5; jae skip   @0xf25c807
    span = GtcSpan{ begin.recorded_gtc, this.gtc };
    line.AddEvent(span, name);                           // @0xf25c843
    span.flow = begin.dma_id & 0xff_ffff_ffff_ffff;      // flow XStat 0x38   @0xf25c861
```

The end gate `LastSyncForTracedDma` @ `0xf6986a0` is byte-confirmed: case `0xb` (`hib_sync_update`) → `field(@submsg+0x28).f@0x1c >> 31` (the `last` top-bit); case 9 (`cs_external` sync-flag-update) → `(f@0x30 != 0) && (f@0x3c != 0)` (the `last_sync_for_dma && successful_sync` gate). `UpdatedSyncFlagTarget` @ `0xf698400` is the completion's target (the `EntryDataCase`-keyed sync-flag-target pack), used as the deque key. The flow value is masked to 56 bits (`& 0xFFFFFFFFFFFFFF`) before becoming the XFlow id. Confidence: CONFIRMED (byte-exact gate + flow mask + the `kind < 5` selector + `AddEvent`).

`ConvertDmaEndsToXPlane<jxc>` is called **twice** — once from a lambda (`@0xf25b1e0`) with `TpuComponent` 23 `Barna Core Fabric Sync` (`edi = 0x17`, call @ `0xf25b8db`), once (`@0xf25d6e0`) with `TpuComponent` 17 `Tensor Core Sync Flag` (`edi = 0x11`, call @ `0xf25ee62`). So the `jxc` host-DMA span lands on the Sync-Flag lanes (17/23), named `DMA Local`/`DMA Remote`/`DMA H2D` per the matched begin's `kind`, flow-linked to its descriptor by `dma_id` — the `jxc` analog of the `pxc` UHI begin/end pairing, but keyed on the sync-flag target and gated by the DMA's *last* sync-flag-update rather than a RESPONSE trace point.

> **NOTE — the `kind` → direction label.** The host-DMA span's name (`DMA Local`/`Remote`/`H2D`) is picked by the recorded `SyncFlagUpdate.kind` (`+0x10`, indexing the event-metadata array with a `cmp kind, 5; jae skip` bound). Which `nf_descriptor` field (`descriptor_source` enum / dma locality) sets each `kind` was read structurally in the populate side but not tabulated to the descriptor's locality enum — the `kind` index source is CONFIRMED, the exact descriptor-field → `kind` mapping is LOW.

---

## 5. Reimplementation Notes

- **Detecting a host DMA.** On `pxc`, a transfer is host↔device iff it originated from the UHI band (trace points `{0,2,4}`) — there is no flag on the `DmaTransfer` record beyond the tag (6/7 = host, 2/3 = on-chip ICI). On `jxc`, host DMA is whatever the HIB `nf_descriptor_hib` band stages and a `hib_sync_update` completion closes; it is rendered on the Sync-Flag lanes, not a dedicated Memcpy lane.
- **Direction.** `pxc`: `(queue_id & ~1) == 2 ? H2D : D2H` — only `DIRECTWRITEQUEUE0/1` are H2D. `jxc`: the `kind` field of the matched begin (`DMA Local`/`Remote`/`H2D`) — there is no separate H2D/D2H tag; "Local" covers the on-chip-staged case.
- **Byte count.** `pxc`: raw `size` (no shift). `jxc`: `length << 10` (1 KiB granule). Do **not** apply the on-chip band's `<< {2,9}` granule logic to either host path.
- **Pairing key.** `pxc`: a 32-bit `transaction_id`, one per host DMA. `jxc`: a `sync_flag_target` (the `SourceSyncFlagTarget`/`UpdatedSyncFlagTarget` fold), and the *last* sync-flag-update per DMA closes the span.
- **The host endpoint is summary-only.** Neither path renders a host or device address. `pxc` decodes `dva` and drops it; `jxc` records an endpoint slot in `SyncFlagUpdate` but no symbolizer reads it back. A reimplementer who needs the host↔device address must re-read the dropped proto fields (`pxc` `dva` @ STARTED `+0x28`) — no consumer in `libtpu.so` does.

---

## Cross-References

- [UHI Host-Interface DMA](uhi-host-interface.md) — the UHI message wire format and the full 22-value `QueueIdValues` enum the tag-6/7 selector keys on
- [Intra-Chip DMA Descriptor](intra-chip-descriptor.md) — the `OciDescriptorCommonIssuedFromTcs` local-leg descriptor; §5 documents the shared `DmaTransfer` record + per-span XStat set this page's spans use
- [OCI Command DMA-ID](oci-command-dma-id.md) — the `trace_id_header` DMA-id pairing key and the per-band id helpers (the `pxc` `transaction_id` analog)
- [Rolled / Strided / General Emitters](rolled-strided-general.md) — the transfer-body emitters that issue the descriptors the `jxc` host-DMA pass stages
- [ICR DMA Timeline Band](../profiling/icr-dma-timeline-band.md) — the on-chip ICI band (tags 2/3, lanes 54/55) produced by the *sibling* `pxc` lambda @ `0xf26c6e0`, distinct from this host band
- [DMA Endpoint Rendering](../profiling/dma-endpoint-rendering.md) — how `ConvertDmaTransfersToXPlane` renders a paired `DmaTransfer` into a device `XEvent` and which fields it keeps vs. drops
- [JXC DMA / HBM-Mux / BrnPerf](../profiling/jxc-dma-hbmmux-brnperf.md) — the `jxc` DMA band the `SyncFlagUpdate` pairing renders onto the Sync-Flag lanes (17/23)
