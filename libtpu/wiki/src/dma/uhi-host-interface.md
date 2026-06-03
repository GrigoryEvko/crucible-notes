# UHI Host-Interface DMA

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`, `.rodata` base `0x84a0000`); `.data.rel.ro` carries a `0x200000` VMA→file delta. All addresses are VMA. Other versions will differ.*

## Abstract

The **UHI** (Universal/Unified Host Interface) band is the `pxc` (Pufferfish / BarnaCore) generation's host↔device DMA message family in the device trace stream. Every cross-boundary DMA — an infeed feeding a TensorCore queue, an outfeed draining one, a host direct-write, or a physical-address bridge response — surfaces as a sequence of UHI messages whose `package` is `asic_sw.driver.deepsea.pxc.profiler`. There are **eight** UHI message types: a transaction *start* (`UhiHostDmaTransactionStartedAddressTranslation`), a read/write *request* pair carrying the host physical address (`UhiHostPhysicalRequest{Read,Write}`), a read/write *response* pair that closes the transfer (`UhiHostPhysicalResponse{Read,Write}`), and a read/write *OCI bridge request* pair carrying the on-chip byte address (`UhiOciRequest{Read,Write}`). Each is wired into the `pxc::profiler::TraceEntry` oneof and decoded by a generated `DecodeUhi…` bit-unpacker.

This page owns the **per-message field map** (field number, name, proto type, C++ struct offset, on-wire bit width, embedded `TraceIdHeader`), the **22-value `QueueIdValues` enum** and its host-DMA role per queue name, and how a UHI message **frames** a host↔device transfer (the begin/end identity-pairing surface). The *reassembly* that turns the start+response pair into a rendered span — `DeriveHostDmaTransfers` and the tag-6/7 H2D/D2H selector — is on [Host↔Device DMA](host-device-dma.md); the shared `DmaTransfer` record + per-span XStats and the local-leg descriptor are on [Intra-Chip DMA Descriptor](intra-chip-descriptor.md). This page is the wire-format reference those two link back to.

For reimplementation, the contract is:

- **The eight UHI message types** — their `pxc::profiler::TraceEntry` oneof cases (0–6 for the seven `pxc` UHI messages; the OCI-bridge pair completes the band), and which three the host-DMA timeline actually keys on (`{0, 2, 4}`).
- **The id-0 STARTED field map** — `{trace_id_header, queue_id, sequence_number, dva, size}`, byte-exact C++ offsets and on-wire widths, decoded by `DecodeUhiHostDmaTransactionStartedAddressTranslation` (consumed-bit `CHECK 0xd8 = 216`).
- **The id-2 / id-4 RESPONSE field map** — the identical 3-field `{trace_id_header, is_l2_pte_fetch, chunk_id}` shape, the END marker (`CHECK 0x76 = 118`).
- **The embedded `TraceIdHeader`** — `{transaction_id, core_id, chip_id}`, field 1 of *every* UHI message; `transaction_id` alone is the per-DMA pairing key.
- **The 22-value `QueueIdValues` enum** — every value 0..21 resolved to its host-DMA role (debug / magic / direct-write / infeed / outfeed / reserved), and why this enum is `pxc`-only (the newer HDE generation replaces it with a direction-naming `ThreadIdValues`).
- **The request / OCI-bridge siblings** — the un-consumed address half of the band (`dpa_upper_bits`, `dva_middle_bits`, `f_on_chip_byte_address`) the timeline decodes but drops.

| | |
|---|---|
| **Proto package** | `asic_sw.driver.deepsea.pxc.profiler` (`pxc` = Pufferfish) |
| **UHI message types** | `UhiHostDmaTransactionStartedAddressTranslation`, `UhiHostPhysicalRequest{Read,Write}`, `UhiHostPhysicalResponse{Read,Write}`, `UhiOciRequest{Read,Write}` (8 total) |
| **id-0 STARTED decoder** | `Decode…UhiHostDmaTransactionStartedAddressTranslation…` @ `0xf5b0b80` (consumed-bit `CHECK 0xd8 = 216`) |
| **id-2 RESPONSE_READ decoder** | `Decode…UhiHostPhysicalResponseRead…` @ `0xf5b1300` (`CHECK 0x76 = 118`) |
| **Host-DMA timeline keys** | UHI ids `{0, 2, 4}` only (STARTED + RESPONSE_READ + RESPONSE_WRITE) |
| **Pairing key** | `trace_id_header.transaction_id` (32-bit, one per host DMA) |
| **`QueueIdValues`** | 22 values: DEBUG / MAGIC / DIRECTWRITE0-1 / INFEED0-9 / OUTFEED0-6 / RESERVED |
| **Enum-name render** | `proto2::internal::NameOfDenseEnum<…QueueIdValues_descriptor, 0, 21>` → the `queue` XStat |
| **Cross-gen successor** | HDE `ThreadIdValues` (`THREAD_ID_HOST2CHIP`/`CHIP2HOST`) names direction explicitly |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile + the FDP descriptor pool |

---

## 1. The UHI Message Family and the `TraceEntry` Oneof

The `pxc` device trace stream is a stream of `pxc::profiler::TraceEntry` records, each a proto2 oneof over the trace-point message types. The UHI host-interface band occupies oneof cases 2..8 (trace-point ids 0..6); the eight message *types* below are all present as demangled symbols in the binary (`UhiHost…`, `UhiOci…`, plus their generated `Decode…`/`DefaultConstruct…` helpers).

| id | UHI message (`pxc::profiler::`) | oneof case | shape | role in the host-DMA chain |
|---:|---|---:|---|---|
| 0 | `UhiHostDmaTransactionStartedAddressTranslation` | 2 | 5 fields | **BEGIN** — opens the transfer; carries queue + size + dva |
| 1 | `UhiHostPhysicalRequestRead` | 3 | 7 fields | read request — host physical / device-VA address (un-consumed) |
| 2 | `UhiHostPhysicalResponseRead` | 4 | 3 fields | **END (read)** — closes a read transfer |
| 3 | `UhiHostPhysicalRequestWrite` | 5 | 7 fields | write request — host physical / device-VA address (un-consumed) |
| 4 | `UhiHostPhysicalResponseWrite` | 6 | 3 fields | **END (write)** — closes a write transfer |
| 5 | `UhiOciRequestRead` | 7 | 5 fields | UHI↔OCI bridge read — on-chip byte address (un-consumed) |
| 6 | `UhiOciRequestWrite` | 8 | 5 fields | UHI↔OCI bridge write — on-chip byte address + flags (un-consumed) |

> **NOTE — only three ids drive the timeline.** The `DeriveHostDmaTransfers` host-DMA producer seeds its merged trace-point set with exactly `{0, 2, 4}` (the STARTED + the two RESPONSE messages); see [Host↔Device DMA §2.1](host-device-dma.md). The REQUEST (`1`/`3`) and OCI-bridge (`5`/`6`) events are fully decoded into the proto — their `Decode…` functions are real symbols — but no XPlane pass in `libtpu.so` consumes them. They are documented here as the address-carrying half of the band a reimplementer would need to recover a host↔device address mapping (§5). Confidence: CONFIRMED (the eight message symbols + the `{0,2,4}`-only producer seed).

**Proto-type legend** (`FieldDescriptorProto.type`, used in the field-map tables): `4 = UINT64`, `8 = BOOL`, `11 = MESSAGE`, `13 = UINT32`, `14 = ENUM`.

---

## 2. The Embedded `TraceIdHeader` — Field 1 of Every UHI Message

Every UHI message — and indeed every `pxc` trace message — begins with field 1 = a `TraceIdHeader` submessage. It is the per-event identity tuple; the host-DMA producer reads exactly one field of it.

| f# | name | proto type | C++ off | on-wire | role |
|---:|---|---|---:|---:|---|
| 1 | `transaction_id` | UINT32 (13) | `+0x18` | 21 | **the `dma_id` pairing key** — read by the producer |
| 2 | `core_id` | ENUM `TraceIdHeaderCoreIdValues` (14) | `+0x1c` | 3 | issuing core identity (decoded, dropped) |
| 3 | `chip_id` | UINT32 (13) | `+0x20` | 12 | issuing chip identity (decoded, dropped) |

The `TraceIdHeaderCoreIdValues` enum (8 values, the `pxc` subset):

```text
0 = CORE_ID_RESERVED   1 = CORE_ID_NONCORE   2 = CORE_ID_TC0   3 = CORE_ID_TC1
4 = CORE_ID_BC0        5 = CORE_ID_BC1       6 = CORE_ID_BC2   7 = CORE_ID_BC3
```

> **GOTCHA — the pairing key is `transaction_id` *alone*.** Unlike the on-chip ICR band, which folds `core_id`/`chip_id` into a 38-bit composite `dma_id` (see [OCI Command DMA-id Selectors](oci-command-dma-id.md)), the UHI host-DMA producer keys its `flat_hash_map<uint64, DmaTransfer>` on the bare 32-bit `transaction_id` (zero-extended), read identically in all three arms (`hdr+0x18`). A host DMA has exactly one `transaction_id`; its STARTED and its RESPONSE share it. Confidence: CONFIRMED (the producer reads only `transaction_id` in each arm; the end arms read *nothing* but the key + gtc).

> **NOTE — the strings island holds more than 8 `CORE_ID_*` names** (`CORE_ID_SC0..SC3`, `CORE_ID_RESERVEDCORE*`, etc.). Those belong to *other-generation* CoreId enums (SparseCore-era). The `pxc` `TraceIdHeaderCoreIdValues` is the 8-value `{RESERVED, NONCORE, TC0, TC1, BC0..BC3}` subset above. Confidence: CONFIRMED-PARTIAL (the 8 `pxc` names are pinned; the exact enum boundary between generations was not single-stepped — LOW for the cross-gen split).

---

## 3. The id-0 STARTED Field Map — The Host Endpoint

`UhiHostDmaTransactionStartedAddressTranslation` (id 0, oneof case 2) is the BEGIN event and the only UHI message that carries the host endpoint. Its bit-unpacker is `Decode…UhiHostDmaTransactionStartedAddressTranslation…` @ `0xf5b0b80`, which spans two on-wire packets and finishes with a consumed-bit `CHECK rdi, 0xd8` (= 216 bits).

| f# | name | proto type | C++ off | on-wire | producer use |
|---:|---|---|---:|---:|---|
| 1 | `trace_id_header` | MESSAGE `TraceIdHeader` (11) | `+0x18` | 21,3,12 | `dma_id` = `transaction_id` (pairing key) |
| 2 | `queue_id` | ENUM `QueueIdValues` (14) | `+0x20` | 5 | KIND-tag selector (§4) **and** `queue` XStat (enum name) |
| 3 | `sequence_number` | UINT32 (13) | `+0x24` | 16+10 | decoded, **dropped** |
| 4 | `dva` | UINT64 (4) | `+0x28` | 56* | device virtual address — decoded, **dropped** |
| 5 | `size` | UINT32 (13) | `+0x30` | 32 | `byte_count` = `size` (no granule shift) |

(\* `dva` is a 56-bit value packed into a `uint64`, reassembled across the packet boundary from the decoder's `GetBits` fragments `1,1,53…` around the `cmp rdi, 0x80` straddle. `queue_id`'s 5-bit width = `ceil(log2(22))`.)

The decoder writes each field to its C++ offset with a proto2 has-bit: `queue_id` → `[obj+0x20]` (has-bit `0x2`), `sequence_number` → `[obj+0x24]` (`0x4`, with a 10-bit continuation across the packet boundary), `dva` → `[obj+0x28]` (`0x8`), `size` → `[obj+0x30]` (`0x10`). The on-wire reconciliation: `216 = frame+header(61) + TraceIdHeader(21+3+12=36) + payload(119)`, where the 119-bit payload = `queue_id(5) + sequence_number(16+10=26) + dva(56) + size(32)`.

> **NOTE — `size` is raw bytes, not a granule.** Field 5 is named `size` and is a raw byte count; the producer copies it straight into `byte_count` with **no** shift. Contrast the REQUEST events' `size_units_of_32B` (§5, a 32-byte granule) and the on-chip ICR band's `length << {2,9}` granule logic ([Intra-Chip DMA Descriptor §4](intra-chip-descriptor.md#4-the-descriptor-field-layout)). Confidence: CONFIRMED (`byte_count` ← `[msg+0x30]`, no shift).

> **GOTCHA — the device address is decoded then thrown away.** Field 4 `dva` is the one field that names the device end of the host↔device copy, and the decoder faithfully unpacks it into `[obj+0x28]`. But the producer never reads `+0x28` (nor `+0x24`). Only `trace_id_header` (key), `queue_id` (tag + label), and `size` (byte count) survive into the rendered span — so a captured `pxc` host-DMA `XEvent` shows *how much* and *which queue*, but not the host or device address. See [Host↔Device DMA §2.2](host-device-dma.md). Confidence: CONFIRMED.

---

## 4. The id-2 / id-4 RESPONSE Field Map — The END Marker

`UhiHostPhysicalResponseRead` (id 2, oneof case 4) and `UhiHostPhysicalResponseWrite` (id 4, oneof case 6) are the completion events. They have an **identical** 3-field shape. The read decoder is `Decode…UhiHostPhysicalResponseRead…` @ `0xf5b1300` (consumed-bit `CHECK rdi, 0x76` = 118 bits).

| f# | name | proto type | C++ off | on-wire | producer use |
|---:|---|---|---:|---:|---|
| 1 | `trace_id_header` | MESSAGE `TraceIdHeader` (11) | `+0x18` | 21,3,12 | `dma_id` = `transaction_id` (PAIR — finds the open slot) |
| 2 | `is_l2_pte_fetch` | BOOL (8) | `+0x20`† | 1 | decoded, **dropped** |
| 3 | `chunk_id` | UINT32 (13) | `+0x24` | 20 | decoded, **dropped** |

(† `is_l2_pte_fetch` is the 1-bit bool stored with has-bit `0x2`; `chunk_id` (20 bits) → `[obj+0x24]` has-bit `0x4`.) `UhiHostPhysicalResponseWrite` (id 4) is byte-for-byte the same shape; both close the transfer.

> **NOTE — read-response vs write-response is *not* the direction signal.** It is tempting to read id-2 (READ response) as a device→host read and id-4 (WRITE response) as a host→device write. The producer does **not**: the H2D/D2H lane is fixed at the STARTED event from `queue_id` (§5 below / [Host↔Device DMA §3](host-device-dma.md)); both RESPONSE variants supply only the end timestamp. The producer reads *only* `trace_id_header.transaction_id` from a RESPONSE message, sets `end_gtc`/`end_present`, and leaves `is_l2_pte_fetch`/`chunk_id` untouched. Confidence: CONFIRMED (the end store writes only gtc/present).

### How a UHI message frames a transfer

The two UHI ids that the timeline consumes form a begin/end bracket keyed on one identity:

```text
STARTED (id 0)            transaction_id = T,  queue_id = Q,  size = N
   │  opens DmaTransfer slot[T] = { begin_gtc, byte_count=N,
   │                                queue=NameOf(Q), kind=tag(Q) }   §5
   ▼
RESPONSE_READ (id 2)  OR  RESPONSE_WRITE (id 4)   transaction_id = T
      closes slot[T] = { …, end_gtc }   →  one DmaTransfer span
```

One `transaction_id` per host DMA; the STARTED decides the lane, a single RESPONSE supplies the close. The REQUEST (1/3) and OCI-bridge (5/6) events fall *outside* this bracket — they carry address detail the bracket never reads. The reassembly mechanics (slot map, flush-on-reopen, the shared renderer) are on [Host↔Device DMA](host-device-dma.md).

---

## 5. The `QueueIdValues` Enum — Host-DMA Roles

`queue_id` (id-0 field 2) is a `QueueIdValues` — a dense 22-value enum nested in the STARTED message. All 22 `QUEUE_ID_*` name strings are present in the binary's read-only data; the producer renders the name with `proto2::internal::NameOfDenseEnum<&…QueueIdValues_descriptor, 0, 21>` (fast path indexes the descriptor table for indices 0..21; slow path falls to `NameOfDenseEnumSlow`) into the `queue` XStat, and buckets H2D/D2H by *value*.

| val | name | host-DMA role | lane (per [Host↔Device DMA §3](host-device-dma.md)) |
|---:|---|---|---|
| 0 | `QUEUE_ID_DEBUGQUEUE` | debug / diagnostic host-interface queue | D2H (tag 7, lane 64) |
| 1 | `QUEUE_ID_MAGICQUEUE` | special "magic" control queue | D2H (tag 7, lane 64) |
| 2 | `QUEUE_ID_DIRECTWRITEQUEUE0` | host direct-write into device memory (raw H2D) **[A]** | **H2D (tag 6, lane 63)** |
| 3 | `QUEUE_ID_DIRECTWRITEQUEUE1` | host direct-write into device memory (raw H2D) **[A]** | **H2D (tag 6, lane 63)** |
| 4 | `QUEUE_ID_INFEEDQUEUE0` | infeed (host→device data feed) **[B]** | D2H (tag 7, lane 64) |
| 5–13 | `QUEUE_ID_INFEEDQUEUE1..9` | infeed 1..9 **[B]** | D2H (tag 7, lane 64) |
| 14 | `QUEUE_ID_OUTFEEDQUEUE0` | outfeed (device→host result drain) **[C]** | D2H (tag 7, lane 64) |
| 15–20 | `QUEUE_ID_OUTFEEDQUEUE1..6` | outfeed 1..6 **[C]** | D2H (tag 7, lane 64) |
| 21 | `QUEUE_ID_RESERVED` | reserved (unused queue slot) | D2H (tag 7, lane 64) |

Counts: 1 debug + 1 magic + 2 direct-write + **10** infeed (0..9) + **7** outfeed (0..6) + 1 reserved = **22**, indices 0..21.

**[A]** Direct-write grounded by `pxc::plc::DirectWriteQueueHostQueueFactory` (the host directly writes payload into a device-mapped queue — a pure host→device write). The **only** two values bucketed H2D.
**[B]** Infeed grounded by `pxc::plc::InfeedQueueHostQueueFactory` + `xla::*::TransferToInfeed` (host→device data feed).
**[C]** Outfeed grounded by `pxc::plc::OutfeedQueueHostQueueFactory` (+ `tpu::OutfeedManager`) + `xla::*::TransferFromOutfeed` (device→host result drain). `QUEUE_ID_MAGICQUEUE` correspondingly has a `pxc::plc::MagicQueueHostQueueFactory`.

> **GOTCHA — only the two direct-write queues are H2D; the rule lives off-page.** The byte-exact selector `(queue_id & ~1) == 2 ? 6 : 7` (true only for `queue_id ∈ {2,3}`) is the single tag-6/7 site in `.text`; it is documented in full on [Host↔Device DMA §3](host-device-dma.md). The consequence here: the infeed queues — logically host→device per `TransferToInfeed` — are nonetheless bucketed **D2H**, because the timeline classes only the raw direct-write as H2D and treats every descriptor-managed transfer (infeed/outfeed) as D2H. The 22 names + roles are CONFIRMED (the strings island holds exactly these 22 + the `pxc::plc::*HostQueueFactory` queue factories exist); whether the infeed=D2H bucketing reflects silicon direction or is a trace-design convention is **LOW** (decoded as the literal predicate, not validated against silicon).

> **NOTE — `QueueIdValues` is `pxc`-only; the HDE successor names direction explicitly.** This enum exists only in the `pxc` UHI band. The newer **HDE** (Host DMA Engine) generation — `HdeHostRequest{Read,Write}` / `HdeHostResponse{Read,Write}`, present as symbols — replaces the host-DMA message's `queue_id` with a `ThreadIdValues` enum that names the host↔device direction in the value name itself: `THREAD_ID_HOST2CHIP_0..3` (H2D), `THREAD_ID_CHIP2HOST_0..1` (D2H), `THREAD_ID_RESERVED0/1`. (A wider HDE thread family also names HBM↔core engines, e.g. `THREAD_ID_HBM2SC{0..3}SPMEM`.) So the `pxc` trace must *infer* direction from the queue role via the `(queue_id & ~1) == 2` heuristic, whereas an HDE producer keys on the thread name directly. Confidence: CONFIRMED (the `pxc` `QueueIdValues` + the HDE `THREAD_ID_HOST2CHIP`/`CHIP2HOST`/`RESERVED0/1` + `HdeHost…` symbols all present).

---

## 6. The Request / OCI-Bridge Siblings — The Un-Consumed Address Half

The four sibling UHI messages decode the addresses the timeline drops. They are decoded into the proto (their `Decode…` functions are real symbols) but no XPlane pass reads them; they are the recoverable host↔device address surface.

**`UhiHostPhysicalRequestRead` (id 1) / `UhiHostPhysicalRequestWrite` (id 3)** — identical 7-field shape, carrying the **host physical** address:

| f# | name | proto type | note |
|---:|---|---|---|
| 1 | `trace_id_header` | MESSAGE | pairs with the STARTED `transaction_id` |
| 2 | `is_l2_pte_fetch` | BOOL | L2 page-table-entry fetch flag |
| 3 | `dpa_upper_bits` | UINT64 | **device/host physical address** (upper bits) |
| 4 | `dva_middle_bits` | UINT32 | device-VA middle bits (the half STARTED `dva` omits) |
| 5 | `size_units_of_32B` | UINT32 | size in 32-byte granules (contrast STARTED `size` = raw bytes) |
| 6 | `num_chunks` | UINT32 | chunk count |
| 7 | `chunk_id` | UINT32 | chunk index |

**`UhiOciRequestRead` (id 5) / `UhiOciRequestWrite` (id 6)** — identical 5-field shape, the UHI↔OCI bridge request carrying the **on-chip byte address**:

| f# | name | proto type | note |
|---:|---|---|---|
| 1 | `trace_id_header` | MESSAGE | identity |
| 2 | `f_on_chip_byte_address` | UINT64 | on-chip byte address (the OCI-fabric end) |
| 3 | `id` | UINT32 | bridge transaction id |
| 4 | `write_data_type_is_instruction` | BOOL | write payload is instruction vs data |
| 5 | `write_is_ordered` | BOOL | ordered-write flag |

> **NOTE — the address half is recoverable but un-rendered.** A reimplementer who needs the actual host physical (`dpa_upper_bits`) / device virtual (`dva` + `dva_middle_bits`) / on-chip (`f_on_chip_byte_address`) addresses must re-read these dropped fields plus the STARTED `dva`. The exact LSB-first bit-reassembly of the multi-fragment UINT64 fields across the decoder's packet boundary (`dva` 56-bit, `dpa_upper_bits`) was not single-stepped here — the field widths and C++ destinations are pinned, the per-fragment shift/OR order is the residual. Confidence: CONFIRMED for the field set + types; **LOW** for the exact UINT64 bit-reassembly order.

---

## 7. Reimplementation Notes

- **Decode order per message.** Field 1 is always `TraceIdHeader` (3 sub-fields, on-wire `21,3,12`). For id-0 STARTED, the remaining payload is `queue_id(5) + sequence_number(16+10) + dva(56) + size(32)` = 119 bits, total consumed `0xd8` = 216. For a RESPONSE, the payload is `is_l2_pte_fetch(1) + chunk_id(20)`, total `0x76` = 118.
- **The pairing surface.** To frame a transfer you need only id-0 (begin) + one of id-2/id-4 (end), joined on `trace_id_header.transaction_id`. The direction comes from id-0's `queue_id`, never from which RESPONSE closed it.
- **`queue_id` semantics.** Bucket H2D iff `queue_id ∈ {2,3}` (the two direct-write queues); everything else is D2H. Render the queue name with the dense-enum table (indices 0..21). Do not assume infeed = H2D.
- **`size` is raw bytes.** The STARTED `size` (f5) is a byte count used with no shift. The REQUEST `size_units_of_32B` (f5) is a 32-byte granule — multiply by 32 if you read it.
- **Addresses are optional and un-rendered.** `dva` (STARTED f4), `dpa_upper_bits`/`dva_middle_bits` (REQUEST f3/f4), and `f_on_chip_byte_address` (OCI f2) carry the full host↔device address mapping but no `libtpu.so` consumer reads them — recover them only if you need the address.
- **Generation gate.** `QueueIdValues` is `pxc`-only. On HDE silicon use the `HdeHost…` messages and key the direction off `ThreadIdValues` (`HOST2CHIP`/`CHIP2HOST`) instead of the queue heuristic.

---

## Cross-References

- [Host↔Device DMA](host-device-dma.md) — `DeriveHostDmaTransfers`, the `(queue_id & ~1) == 2 ? 6 : 7` tag-6/7 selector, and the begin/end reassembly that consumes the UHI ids `{0,2,4}` documented here
- [Intra-Chip DMA Descriptor](intra-chip-descriptor.md) — the local-leg `OciDescriptorCommonIssuedFromTcs`; [§5](intra-chip-descriptor.md#5-what-the-renderer-keeps-vs-drops) is the shared `DmaTransfer` record + per-span XStat set the UHI spans render into
- [OCI Command DMA-id Selectors](oci-command-dma-id.md) — the on-chip `dma_id` pairing key (the 38-bit composite the UHI band's bare 32-bit `transaction_id` contrasts with)
- [Continuation Queue](continuation-queue.md) — the SFLAG / continuation-queue runtime model behind the infeed/outfeed queue managers grounding the `QueueIdValues` roles
- [Payload: UHI/OCI/ICI/DMA](../profiling/payload-uhi-oci-ici-dma.md) — the trace-point-id↔message-name registry and on-wire bit decodes for the broader UHI/OCI/ICI payload family
- [ICR DMA Timeline Band](../profiling/icr-dma-timeline-band.md) — the on-chip ICI band (sibling `pxc` lambda), distinct from this host band
