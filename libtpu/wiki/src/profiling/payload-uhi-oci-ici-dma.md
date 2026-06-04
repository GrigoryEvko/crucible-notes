# Trace Payload: UHI / OCI / ICI / DMA Bands

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — full C++ symbols are present, and `.text` VMA equals file offset. Other versions will differ.*

## Abstract

This page is the per-trace-point payload bit-decode for the high-value device-event bands captured by the on-device TPU profiler: **UHI** (host↔chip DMA), **OCI** (on-/off-chip interconnect descriptors, messages, and read/write commands), **ICI** (inter-chip link packets), the **intra-chip DMA** band (CMQ VPU-DMA on pufferfish, CMN-DMA on ghostlite), the **TCS sync-flag** primitives (set/add/read/wait/fence), and the **throttle/stall** events. Each `Decode<Name>(string_view, bool* started_out, TraceEntry* out)` is an anonymous-namespace function reachable from the per-family `DecodeEntry` jump table; the per-event payload is a fixed sequence of `GetBits64` calls, and this page tabulates every width → field-name → meaning mapping, grouped by band.

The framing is owned elsewhere and not repeated here. The universal envelope — the fixed **16-byte (128-bit) packet**, the 2-bit `valid`/`started` framing prefix, the 59-bit `TraceHeader` (`trace_point_id:8 · block_id:3│6 · timestamp:48│45`), the optional 36/38-bit `TraceIdHeader`, the dual decode/encode dispatch, and the per-event total-bit `CHECK` — is the subject of [TraceEntriesCoder](trace-entries-coder.md). Read that page first: every width sequence below begins **after** the universal `SkipBits(2)` + `DecodeTraceHeader`, so the payload always starts at packet bit 61, and the listed `CHECK` constant is the *total* consumed bits including those 61 (payload bits = `CHECK − 61`). The id→name registry is owned by [TracePoints Master Registry](tracepoints-master-registry.md); this page decodes the payloads those ids name.

The single most important reading rule: a width sequence is written `TIDhdr | payload`. When an event carries identity, the leading `21,3,12` (pxc) or `21,3,14` (glc/gfc) is the `TraceIdHeader{transaction_id, core_id, chip_id}`; the `|` separates it from the typed payload scalars. Multi-bit values wider than 32 bits are reassembled by the `BitDecoder` from several `GetBits64` fragments (e.g. a `30,…,29` pair feeds one 64-bit destination), which is why a payload width list contains apparently odd fragment widths interleaved with the 1-bit bool flags.

For reimplementation, the contract this page completes is:

- **The per-trace-point payload field map** — for every event in the six bands: the ordered `GetBits64` width sequence, the named proto field each width writes, and the total-bit `CHECK`.
- **The shape-sharing structure of the OCI band** — three recurring payload shapes (A/B/C) drive ~30 of the 99 pxc events; learn the three, not the thirty.
- **The per-gen width drift** — `chip_id` and `dst_chip_id` widen 12→14 bits, `virtual_channel` narrows 3→2, and the sync-flag payload grows by a 64-bit `lcc` field, pxc→glc.
- **The multi-packet rule** — an event whose `CHECK > 128` spans **two** 16-byte packets; bytes-consumed is `0x20` (32), not `0x10`.

| Band | pxc `trace_point_id`s | Events | Carries `TraceIdHeader` | Confidence |
|---|---|---|---|---|
| UHI host-DMA | 0–6 | 7 | yes (`21,3,12`) | CERTAIN (pxc) |
| OCI descriptor / message / command | 7–10, 20–27, 49–55, 91–96, 141 | ~30 | yes | CERTAIN (pxc) |
| ICI link packet | 40–48 | 9 | yes | CERTAIN (pxc) |
| Intra-chip DMA (CMQ VPU-DMA) | 140–149 | 10 | yes | CERTAIN (pxc) |
| TCS sync-flag | 80–90 | 11 | only id 80 | CERTAIN (pxc) |
| Throttle / stall | 97 | 1 (two-variant) | no | CERTAIN (pxc) |
| BarnaCore FSM (shares throttle word) | 100–119 | 20 | no | HIGH |
| **pxc total decoded** | 0–149, 255 | **99** | mixed | CERTAIN |
| Per-gen successors (glc) | HDE 8–13, CMN-DMA 72–79, cycle-skip 200–218 | sampled | yes (`21,3,14`) | HIGH (glc) / SAMPLED (vfc/vlc/gfc) |

---

## Reading a Payload Row

Every row in the band tables below is the byte-exact output of one `Decode<Name>`. The columns are uniform:

```text
id  EVENT  oneof=F  CHECK=bits(pkts)  widths = [TIDhdr | payload scalars]
```

- **`id`** — the 8-bit on-wire `trace_point_id` (the decode dispatch key).
- **`oneof`** — the dense proto2 oneof field number stamped at `TraceEntry+0x28` (the encode dispatch key). The two id spaces differ; see [TraceEntriesCoder §The Dual Dispatch](trace-entries-coder.md#the-dual-dispatch).
- **`CHECK`** — the hardcoded total-bit constant validated by the per-event `CHECK` (`cmp $CONST,%rdi; jne FATAL`). Payload bits = `CHECK − 61`.
- **`pkts`** — `1` when `CHECK ≤ 128` (one 16-byte packet, bytes-consumed `0x10`), `2` when `128 < CHECK ≤ 256` (two packets, bytes-consumed `0x20`).
- **`widths`** — the `GetBits64` width immediates in stream order. The leading `21,3,12` (pxc) or `21,3,14` (glc) before the `|` is the `TraceIdHeader`; the scalars after `|` are the typed payload.

The consumed-bit count the `CHECK` validates is computed identically in every decoder:

```c
// universal consumed-bit guard, byte-confirmed in every Decode<Name>
consumed = (end_ptr - buffer_start_ptr) * 8 - bits_remaining;   // BitsDecoded()
if (consumed != CHECK)                                          // cmp $CONST,%rdi
    MakeCheckOpString(consumed, CHECK, "decoder.BitsDecoded() == K");  // FATAL
bytes_consumed = (CHECK <= 128) ? 0x10 : 0x20;                 // movq $bytes,0x8(%rbx)
```

> **GOTCHA —** the width fragments are not all separate proto fields. A field wider than 32 bits (`dpa_upper_bits`, `address`, `dva`, `src_operand`, `lcc`) is read as several `GetBits64` calls whose results are shifted/OR'd into one 64-bit destination at a single `TraceEntry+0xNN` slot. A reimplementation that maps one width = one field will over-count the field set. The 1-bit widths interleaved between the wide fragments are the genuine `bool` flags (`is_l2_pte_fetch`, `done`, `multicast`, …) — those *are* one field each. The exact per-fragment shift/OR order was traced only for the named bands (LOW confidence on the precise reassembly bit-positions; the *widths and destinations* are CERTAIN).

---

## UHI — Unified Host Interface Host-DMA (pxc ids 0–6)

The host↔chip memory-DMA band on pufferfish: address translation, physical read/write requests and their responses, and the OCI-bridged variants. Every UHI event carries a `TraceIdHeader{transaction_id 21, core_id 3, chip_id 12}` = 36 bits, so its payload begins at packet bit `61 + 36 = 97`.

### Payload Field Map

| id | event | oneof | CHECK | pkts | widths (TIDhdr \| payload) | Confidence |
|---|---|---|---|---|---|---|
| 0 | `UHI_HOST_DMA_TRANSACTION_STARTED_ADDRESS_TRANSLATION` | 2 | 216 | 2 | `21,3,12 \| 5,16,10,1,1,54,32` | CERTAIN |
| 1 | `UHI_HOST_PHYSICAL_REQUEST_READ` | 3 | 233 | 2 | `21,3,12 \| 1,30,1,1,29,26,8,20,20` | CERTAIN |
| 2 | `UHI_HOST_PHYSICAL_RESPONSE_READ` | 4 | 118 | 1 | `21,3,12 \| 1,20` | CERTAIN |
| 3 | `UHI_HOST_PHYSICAL_REQUEST_WRITE` | 5 | 233 | 2 | `21,3,12 \| 1,30,1,1,29,26,8,20,20` | CERTAIN |
| 4 | `UHI_HOST_PHYSICAL_RESPONSE_WRITE` | 6 | 118 | 1 | `21,3,12 \| 1,20` | CERTAIN |
| 5 | `UHI_OCI_REQUEST_READ` | 7 | 165 | 2 | `21,3,12 \| 31,1,1,19,14,1,1` | CERTAIN |
| 6 | `UHI_OCI_REQUEST_WRITE` | 8 | 165 | 2 | `21,3,12 \| 31,1,1,19,14,1,1` | CERTAIN |

### Named Fields

The named proto fields come from `UhiHostPhysicalRequestRead` and siblings in `trace_entries.proto`:

- **id 0** `queue_id` (enum, 5) · `sequence_number` (uint32, `16+10` split) · `dva` (uint64, `1,1,54` fragments) · `size` (uint32, 32). Widest UHI event — two packets to carry the 54-bit device virtual address plus a 32-bit size.
- **id 1 / id 3** `is_l2_pte_fetch` (bool, 1) · `dpa_upper_bits` (uint64 @ `+0x20`, `30+29` fragments) · `dva_middle_bits` (uint32 @ `+0x2c`, 26) · `size_units_of_32B` (uint32 @ `+0x30`, 8) · `num_chunks` (uint32 @ `+0x34`, 20) · `chunk_id` (uint32 @ `+0x38`, 20). Read and Write requests are byte-identical and share one `Encode` handler (`0xf5c5e91`).
- **id 2 / id 4** the response ack: a flag (1) + a 20-bit id/sequence. The smallest UHI events — one packet. Read and Write responses share `Encode` `0xf5c6029`.
- **id 5 / id 6** the OCI-bridged request: `f_on_chip_byte_address` (uint64, 31 + fragments) · `id` (uint32, 19) · `write_data_type_is_instruction` (bool, 1) · `write_is_ordered` (bool, 1).

> **NOTE —** the read/write pairing (id 1↔3, 2↔4, 5↔6) is structural: each pair has an identical wire shape and is distinguished only by `trace_point_id`. The decode produces the same width sequence; the encode collapses the two oneof fields onto one handler. A reimplementation can model each pair as one codec with a direction tag, exactly as the binary does.

### Per-Gen Successor — HDE (Host DMA Engine), glc ids 8–13

On the newer families the UHI band is replaced by HDE. The `TraceIdHeader` widens to `21,3,14` (= 38 bits) because `chip_id` grows 12→14, re-basing the payload to bit `61 + 38 = 99`.

| glc id | event | oneof | CHECK | pkts | widths (`21,3,14` \| payload) | Confidence |
|---|---|---|---|---|---|---|
| 10 | `HDE_HOST_REQUEST_WRITE` | 10 | 178 | 2 | `21,3,14 \| 3,26,1,1,33,5,10` | HIGH |
| 11 | `HDE_HOST_RESPONSE_WRITE` | 11 | 112 | 1 | `21,3,14 \| 3,10` | HIGH |
| 12 | `HDE_HOST_REQUEST_READ` | 12 | 178 | 2 | `21,3,14 \| 3,26,1,1,33,5,10` | HIGH |
| 13 | `HDE_HOST_RESPONSE_READ` | 13 | 112 | 1 | `21,3,14 \| 3,10` | HIGH |

HDE request fields: `thread_id` (enum, 3) · `address` (uint64, `26+1+1+33` fragments) · `size_units_of_32B` (uint32, 5) · `thread_tracking_id` (uint32, 10). Response carries only `thread_id` (3) + `thread_tracking_id` (10). Decoder anchors: `DecodeHdeHostRequestRead` @ `0xf62e660`, `DecodeHdeHostResponseRead` @ `0xf62ea20`.

---

## OCI — Descriptor / Message / Command (pxc ids 7–10, 20–27, 49–55, 91–96, 141)

The dominant band: ~30 of the 99 pxc events. The key to it is that the events do **not** have 30 distinct layouts — they share **three recurring payload shapes** (plus a few singletons). Identical-shape events share one `Encode` handler, which is how the binary itself collapses them. Learn the three shapes; the per-id table is then just a mapping of id→shape. All carry a `TraceIdHeader{21,3,12}`.

### The Three Recurring Shapes

```text
SHAPE-A  "OCI message"           CHECK=170 (2 pkts)   widths: 21,3,12 | 31,1,1,1,1,1,2,32,3
  fields: trace_id_header, msg_data(u32 31), done(bool 1), msg_type(enum), opcode(enum),
          {flag,flag}, node_type(enum 2), addr(u32 32), node_type_sel(enum 3)

SHAPE-B  "OCI descriptor common" CHECK=179 (2 pkts)   widths: 21,3,12 |
          2,2,3,2,2,3,2,13,2,1,1,1,13,3,13,3,16
  fields: trace_id_header, dma_type(enum 2), src_mem_mem_id(enum 2), src_mem_core_id(enum 3),
          src_opcode(enum 2), dst_mem_mem_id(enum 2), dst_mem_core_id(enum 3), dst_opcode(enum 2),
          src_sync_flag_id(u32 13), src_sync_flag_core_id(enum 2), {flag,flag,flag},
          dst_sync_flag_0_id(u32 13), dst_sync_flag_0_core_id(enum 3),
          dst_sync_flag_1_id(u32 13), dst_sync_flag_1_core_id(enum 3), program_counter(u32 16)

SHAPE-C  "OCI read/write command" CHECK=228 (2 pkts)  widths: 21,3,12 | 21,3,7,1,1,5 | 21,3,12 | 3,17,17,17,3
  fields: 3x trace_id_header (cmd0/cmd1/cmd2; cmd1 interleaves the scalar fields),
          index_valid(u32), id_index0/1/2(u32 17 each), node_type(enum 3)
```

> **QUIRK —** SHAPE-C embeds **three** `TraceIdHeader`s (`cmd0`/`cmd1`/`cmd2`), i.e. `3 × 36 = 108` bits of identity before the scalar payload — the only band that carries more than one. The middle one (`cmd1`, the `21,3,7,1,1,5` group) interleaves the command scalars between the second and third identity records, so a naive "read all three headers, then the payload" parser mis-positions the cursor. Follow the width sequence literally: header, scalars, header, scalars.

### id → Shape Map

| id | event | oneof | shape | CHECK | pkts |
|---|---|---|---|---|---|
| 7 | `OCI_MESSAGE_SENT_BY_UHI_BRIDGE` | 9 | A | 170 | 2 |
| 8 | `OCI_MESSAGE_RECEIVED_BY_UHI_BRIDGE` | 10 | A | 170 | 2 |
| 9 | `OCI_DESCRIPTOR_RECEIVED_BY_UHI_BRIDGE` | 11 | B | 179 | 2 |
| 10 | `OCI_DESCRIPTOR_SENT_BY_UHI_CLIENT` | 12 | B | 179 | 2 |
| 20 | `OCI_DESCRIPTOR_DESC_AT_QNM` | 13 | B | 179 | 2 |
| 21 | `OCI_GENERIC_DESC_ENQUEUED_AT_ENGINE` | 14 | — | 100 | 1 |
| 22 | `OCI_COMMON_READ_CMD_ISSUED_FROM_ENGINE` | 15 | C | 228 | 2 |
| 23 | `OCI_COMMON_MEM_READ_REQ_FROM_ENGINE` | 16 | C | 228 | 2 |
| 24 | `OCI_MESSAGE_MSG_ISSUED_FROM_ENGINE` | 17 | A | 170 | 2 |
| 25 | `OCI_MESSAGE_MSG_ISSUED_FROM_QNM` | 18 | A | 170 | 2 |
| 26 | `OCI_COMMON_WRITE_CMD_ACCEPTED_AT_MN` | 19 | C | 228 | 2 |
| 27 | `OCI_WRITE_REQ_MEM_WRITE_REQ_ISSUED_FROM_ENGINE` | 20 | — | 128 | 1 |
| 49 | `OCI_DESCRIPTOR_ENQUEUED_IN_ICR_EGRESS_DMA` | 30 | B | 179 | 2 |
| 50 | `OCI_MESSAGE_GENERATED_IN_ICR_EGRESS_DMA` | 31 | A | 170 | 2 |
| 51 | `OCI_MESSAGE_GENERATED_IN_ICR_INGRESS_DMA` | 32 | A | 170 | 2 |
| 52 | `OCI_MESSAGE_PACKET_SENT_TO_OCI` | 33 | A | 170 | 2 |
| 53 | `OCI_MESSAGE_PACKET_RECEIVED_IN_ICR` | 34 | A | 170 | 2 |
| 54 | `OCI_COMMON_OCI_WRITE_COMMAND` | 35 | C | 228 | 2 |
| 55 | `OCI_COMMON_OCI_READ_COMMAND` | 36 | C | 228 | 2 |
| 91 | `OCI_DESCRIPTOR_COMMON_ISSUED_FROM_TCS` | 48 | B+2 | 211 | 2 |
| 92–94 | `OCI_DESCRIPTOR_STRIDE_{SRC,DST,STEPS}_ISSUED_FROM_TCS` | 49–51 | stride | 195 | 2 |
| 95 | `OCI_MESSAGE_ISSUED_FROM_TCS` | 52 | A | 170 | 2 |
| 96 | `OCI_COMMON_COMPLETED_IN_TCS` | 53 | C | 228 | 2 |
| 141 | `OCI_MESSAGE_CMQ_VPU_DMA_MSG` | 91 | A | 170 | 2 |

### The OCI Singletons

Three OCI ids do not fit a recurring shape:

- **id 21 `OCI_GENERIC_DESC_ENQUEUED_AT_ENGINE`** (CHECK 100, 1 pkt) — `21,3,12 | 3`: the `TraceIdHeader` plus a single 3-bit enum/counter. The minimal OCI event.
- **id 27 `OCI_WRITE_REQ_MEM_WRITE_REQ_ISSUED_FROM_ENGINE`** (CHECK 128, 1 pkt) — `21,3,12 | 1,15,12,3`: `req_origin` (enum, 1) · `req_id` (uint32, 15) · `src_cmd_id` (uint32, 12) · `node_type` (enum, 3). Exactly fills one 16-byte packet (`61 + 67 = 128`).
- **id 91 `OCI_DESCRIPTOR_COMMON_ISSUED_FROM_TCS`** (CHECK 211, 2 pkts) — SHAPE-B extended by two extra fields (`+31,+1`): the TCS-issued descriptor variant. The BC-issued analogue (id 129, `OCI_DESCRIPTOR_COMMON_ISSUED_BY_BC`, oneof 84) shares this `B+2` layout.

### OCI Stride Descriptors (ids 92–94, 130–132)

```text
OciDescriptorStride{Src,Dst,Steps}IssuedFromTcs   CHECK=195 (2 pkts)
  widths: 21,3,12 | 31,1,1,1,32,32
  fields: trace_id_header, {src,dst,steps}_stride_0/1/2 (3 × uint32 reassembled from 31/32/32)
```

The three stride values are device-address strides for a strided DMA descriptor, carried as 32-bit fragments. The BC-issued analogues (ids 130–132, oneof 85–87) share the layout.

### `node_type` selector

The `node_type` enum recurs across SHAPE-A/-C: `node_type ∈ {TCS, BC, CMQ, HBMQ, UHI, ICR, QNM}` (7 values; 3-bit field). The integer→name table is in the nested `*Values` enum of `trace_entries.proto` and was not enumerated value-by-value here (LOW confidence on the exact integer ordering). Decoder anchors: `DecodeOciCommonOciReadCommand` @ `0xf5b8dc0` (3 `TraceIdHeader`s, oneof `0x24`, CHECK 228), `DecodeOciDescriptorDescAtQnm` @ `0xf5b34a0` (SHAPE-B, oneof 13, CHECK 179).

---

## ICI — Inter-Chip Link Packets (pxc ids 40–48)

The collective-fabric physical substrate. All **nine** ICI events share **one** payload shape per family — they differ only in *which* lifecycle stage fired (received-on-link, transmitted, queued, control/data injected/received by the ICR DMA bridge, control/data queued for local ingress). Each is a single 16-byte packet.

### pxc (pufferfish) — CHECK 125, 1 pkt

```text
TraceIdHeader{21,3,12}, widths: 21,3,12 | 3,3,6,1,1,12,1,1
  router_link_port_id     enum 3    LINK0..LINK5 (6 router ports ⇒ 3 bits)
  virtual_channel         u32  3
  link_targets            u32  6
  local_ingress_target    bool 1
  multicast               bool 1
  dst_chip_id             u32  12
  first_packet_in_dma     bool 1
  last_packet_in_dma      bool 1
```

| id | event | oneof |
|---|---|---|
| 40 | `ICI_PACKET_PACKET_RECEIVED_ON_LINK_INPUT` | 21 |
| 41 | `ICI_PACKET_PACKET_TRANSMITTED_ON_LINK_OUTPUT` | 22 |
| 42 | `ICI_PACKET_PACKET_QUEUED_FOR_LINK_TRANSMISSION` | 23 |
| 43 | `ICI_PACKET_CONTROL_PACKET_INJECTED_BY_ICR_DMA_BRIDGE` | 24 |
| 44 | `ICI_PACKET_DATA_PACKET_INJECTED_BY_ICR_DMA_BRIDGE` | 25 |
| 45 | `ICI_PACKET_CONTROL_PACKET_RECEIVED_BY_ICR_DMA_BRIDGE` | 26 |
| 46 | `ICI_PACKET_DATA_PACKET_RECEIVED_BY_ICR_DMA_BRIDGE` | 27 |
| 47 | `ICI_PACKET_CONTROL_PACKET_QUEUED_FOR_LOCAL_INGRESS` | 28 |
| 48 | `ICI_PACKET_DATA_PACKET_QUEUED_FOR_LOCAL_INGRESS` | 29 |

All nine map to one decoder shape (`DecodeIciPacketPacketReceivedOnLinkInput` @ `0xf5b56c0` is representative, oneof `0x15`=21, CHECK `0x7d`=125). The control-vs-data and injected-vs-received distinctions live entirely in the `trace_point_id`, not the payload.

### glc (ghostlite) — CHECK 128, 1 pkt

```text
TraceIdHeader{21,3,14}, widths: 21,3,14 | 3,2,6,1,1,14,1,1
  same field names; virtual_channel narrows 3→2, dst_chip_id widens 12→14
```

> **QUIRK —** ghostlite's ICI event grows from 125 to 128 bits yet **still fits one packet**. Two compensating drifts: `dst_chip_id` widens 12→14 (tracking the 14-bit `chip_id` in the `TraceIdHeader`), and `virtual_channel` *narrows* 3→2. A reimplementation that hardcodes the pxc `12`-bit `dst_chip_id` or the pxc `3`-bit `virtual_channel` will mis-align every field after it on glc/gfc. Drive the widths from the per-family decoder, never a single constant. Decoder anchor: glc `DecodeIciPacketPacketReceivedOnLinkInput` @ `0xf6322c0` (oneof 25, CHECK `0x80`=128).

---

## Intra-Chip DMA — CMQ VPU-DMA (pxc ids 140–149)

The on-chip DMA-started/completed band on pufferfish is the CMQ (compute-memory-queue) VPU-DMA: eight directional VMEM↔CMEM transfers, plus a descriptor and an OCI-message event. All single-packet. All carry a `TraceIdHeader{21,3,12}`.

| id | event | oneof | CHECK | pkts | widths (`21,3,12` \| payload) |
|---|---|---|---|---|---|
| 140 | `CMQ_VPU_DMA_DESC` | 90 | 105 | 1 | `21,3,12 \| 8` |
| 141 | `OCI_MESSAGE_CMQ_VPU_DMA_MSG` | 91 | 170 | 2 | `21,3,12 \| 31,1,1,1,1,1,2,32,3` (SHAPE-A) |
| 142 | `CMQ_VPU_DMA_REQ_VMEM0_TO_CMEM_READ` | 92 | 123 | 1 | `21,3,12 \| 2,4,20` |
| 143 | `CMQ_VPU_DMA_REQ_VMEM0_TO_CMEM_WRITE` | 93 | 123 | 1 | `21,3,12 \| 2,4,20` |
| 144 | `CMQ_VPU_DMA_REQ_CMEM_TO_VMEM0_READ` | 94 | 123 | 1 | `21,3,12 \| 2,4,20` |
| 145 | `CMQ_VPU_DMA_REQ_CMEM_TO_VMEM0_WRITE` | 95 | 123 | 1 | `21,3,12 \| 2,4,20` |
| 146 | `CMQ_VPU_DMA_REQ_VMEM1_TO_CMEM_READ` | 96 | 123 | 1 | `21,3,12 \| 2,4,20` |
| 147 | `CMQ_VPU_DMA_REQ_VMEM1_TO_CMEM_WRITE` | 97 | 123 | 1 | `21,3,12 \| 2,4,20` |
| 148 | `CMQ_VPU_DMA_REQ_CMEM_TO_VMEM1_READ` | 98 | 123 | 1 | `21,3,12 \| 2,4,20` |
| 149 | `CMQ_VPU_DMA_REQ_CMEM_TO_VMEM1_WRITE` | 99 | 123 | 1 | `21,3,12 \| 2,4,20` |

The eight directional DMA-req events (142–149) are byte-identical — `access_type` (enum, 2) · `vpu_channels` (uint32, 4) · `addr` (uint32, 20) = 62 payload bits — and the **direction is encoded only in the `trace_point_id`, never in the payload**. The descriptor event (140) carries a single 8-bit selector; the message event (141) reuses OCI SHAPE-A. Decoder anchors: `DecodeCmqVpuDmaReqVmem0ToCmemRead` @ `0xf5c4700` (oneof 92, CHECK `0x7b`=123), `DecodeCmqVpuDmaDesc` @ `0xf5c40c0` (oneof 90, CHECK `0x69`=105).

### Per-Gen Successor — CMN-DMA, glc ids 72–79

Ghostlite replaces CMQ with CMN-DMA (chip-memory-network DMA), a far richer record that exposes explicit router routing and opcode/mem-id endpoints. `TraceIdHeader{21,3,14}`.

```text
CmnDmaRequestEastSideLane0   oneof 43   CHECK=205 (2 pkts)
  widths: 21,3,14 | 3,10,1,1,5,5,2,2,1,1,1,32,2,3,32,4,1
  thread_id(enum 3), req_id(u32 10),
  cmn_uncore_router_id_valid0/1(bool,bool), cmn_uncore_router_id0/1(u32 5,u32 5),
  src_opcode(enum 2), src_mem_id(enum 2), src_operand(u32 1+1+1+32 reassembled),
  dst_opcode(enum 2), dst_mem_id(enum 3), dst_addr(u32 32), beats(u32 4), poison(bool 1)
```

The pxc CMQ event records only `{access_type, vpu_channels, addr}`; the newer mem-network trace adds router endpoints, source/dest opcodes and mem-ids, beat count, and a `poison` flag. Decoder anchor: `DecodeCmnDmaRequestEastSideLane0` @ `0xf6368e0` (oneof 43, CHECK `0xcd`=205). The lane/side variants (72–79) share the layout.

---

## TCS Sync-Flag Primitives (pxc ids 80–90)

The TensorCore-Sequencer internal band: the on-chip semaphore/fence primitive (set / add / read / wait / scalar-fence). The "WaitSyncFlag" semantic is split across two events — `UNSUCCESSFUL_SYNC_ATTEMPT` (a wait that blocked) and `SUCCESSFUL_SYNC_ATTEMPT` (a wait that unblocked).

> **NOTE —** the TCS-internal events (ids 81–90) carry **no** `TraceIdHeader` — the core is implied by `trace_point_block_id` in the 59-bit header, so the payload begins directly at bit 61. Only id 80 (`TCS_EXTERNAL_SYNC_FLAG_UPDATE_DMA_DONE`), the cross-chip completion event, carries identity.

### pxc — the 10 internal events share one shape, CHECK 121, 1 pkt

```text
widths (no TIDhdr): 32,1,9,16,1,1
  data_field           u32  32   (some events store sync_flag_number here)
  done_bit             bool 1
  sync_flag_number     u32  9
  program_counter      u32  16
  sfence_end           bool 1
  sfence_start         bool 1
```

| id | event | oneof |
|---|---|---|
| 81 | `TCS_INTERNAL_SET_SYNC_FLAG` | 38 |
| 82 | `TCS_INTERNAL_ADD_SYNC_FLAG` | 39 |
| 83 | `TCS_INTERNAL_HOST_INTERRUPT` | 40 |
| 84 | `TCS_INTERNAL_SET_TRACEMARK` | 41 |
| 85 | `TCS_INTERNAL_TRACE_INSTRUCTION` | 42 |
| 86 | `TCS_INTERNAL_UNSUCCESSFUL_SYNC_ATTEMPT` (wait blocked) | 43 |
| 87 | `TCS_INTERNAL_SUCCESSFUL_SYNC_ATTEMPT` (wait unblocked) | 44 |
| 88 | `TCS_INTERNAL_READ_SYNC_FLAG` | 45 |
| 89 | `TCS_INTERNAL_SCALAR_FENCE_START` | 46 |
| 90 | `TCS_INTERNAL_SCALAR_FENCE_END` | 47 |

> **QUIRK —** `TCS_INTERNAL_SET_SYNC_FLAG` (id 81) has **three** internal proto oneof sub-tags (`0x26`/`0x27`/`0x28`) that all decode the *same* `32,1,9,16,1,1` wire shape into three different proto variants (set / set-and-trace / etc.), all CHECK 121. One wire layout, three proto cases — the discriminator is the sub-tag stamp, not a payload bit. Decoder anchor: `DecodeTcsInternalSetSyncFlag` @ `0xf5b97a0` (sub-tags at `0xf5b985e`/`0xf5b9a7e`/`0xf5b9c9e`, CHECK `0x79`=121).

### id 80 — cross-chip completion, CHECK 163, 2 pkts

```text
TCS_EXTERNAL_SYNC_FLAG_UPDATE_DMA_DONE   oneof 37   CHECK=163 (2 pkts)
  widths: 21,3,12 | 31,1,1,1,1,9,16,1,1,1,1,1,1
  trace_id_header,
  updated_sync_flag_value(u32 31), updated_sync_flag_done(bool), sync_flag_number(u32 9),
  program_counter(u32 16), then 6 bools:
    successful_sync_unblock, successful_sync, last_sync_for_dma, last_sync_was_add,
    was_csr_update, trace_bit_set
```

This is the receive-side completion of a *remote* DMA that bumped a local sync flag — the trace observation of a `BumpRemoteSyncFlag` / atomic set-done. Decoder anchor: `DecodeTcsExternalSyncFlagUpdateDmaDone` @ `0xf5b9340` (oneof 37, CHECK `0xa3`=163).

### Per-Gen — glc SetSyncFlag expanded, CHECK 187, 2 pkts

```text
widths (no TIDhdr): 32,1,9,16,1,1, 7,1,1,57
  base {32,1,9,16,1,1} identical to pxc, then +lcc (uint64) reassembled from {7,1,1,57}
```

Ghostlite appends an `lcc` (loop/cycle-counter) 64-bit field, growing the sync-flag payload from 60 to 126 bits (pxc→glc). Decoder anchor: glc `DecodeTcsInternalSetSyncFlag` @ `0xf6380c0` (oneof 48, CHECK `0xbb`=187). glc `TcsExternalSyncFlagUpdateDmaDone` similarly gains a field (CHECK `0xa5`=165).

---

## Throttle / Stall (pxc id 97)

`THROTTLE_STATE_THERMAL_AND_ELECTRICAL` is a **discriminated two-variant body** — a single `trace_point_id` whose first 4-bit field (`packet_type`) selects which of two proto variants and which payload layout the rest of the packet uses. The decoder branches on `packet_type & 1` and stamps a different oneof tag. No `TraceIdHeader` — power events are chip-global.

```text
VARIANT A  oneof=54 (packet_type bit0=0)   CHECK=120 (1 pkt)
  widths: 4,5,5,10,4,21,5,5
  packet_type(enum 4), num_electrical_throttles(u32 5), num_thermal_throttles(u32 5),
  thermal_sensor_data(u32 10), thermal_sensor_index(u32 4), thermal_total_throttles(u32 21),
  thermal_max_throttle(u32 5), thermal_min_throttle(u32 5)

VARIANT B  oneof=55 (packet_type bit0=1)   CHECK=204 (2 pkts)
  widths: 13,16,16,22,1,1  then  10,16,16,16,13,1,2  (two internal sub-branches, both CHECK 204)
  a wider electrical/voltage throttle record: the 16-bit fields are voltage/current samples,
  the 22-bit is a cycle/throttle accumulator
```

> **GOTCHA —** the same `trace_point_id` (97) decodes to two different proto oneof fields (54 vs 55), two different payload widths, and two different packet counts (1 vs 2), chosen at runtime by a payload bit (`packet_type & 1`). A decoder keyed purely on `trace_point_id` will mis-size variant B. The discriminator is *inside* the payload, read before the branch. Decoder anchor: `DecodeThrottleStateThermalAndElectricalThrottleState` @ `0xf5bc620` — variant A at `0xf5bc6de` (`test $0x1`, CHECK `0x78`=120, bytes `0x10`), variant B at `0xf5bc945` (CHECK `0xcc`=204 at `0xf5bcb73`/`0xf5bcef3`, bytes `0x20`).

> **NOTE —** variant B has two trailing-width sub-branches (`13,16,16,22,1,1` vs `10,16,16,16,13,1,2`) that both validate against the *same* CHECK (204) and the *same* oneof (55). Whether the inner branch is a second `packet_type` bit or a length-conditional re-decode is unresolved (LOW confidence on the precise inner predicate); the two sub-branches read slightly different trailing widths but converge on CHECK 204, so treat them as a discriminated pair. The variant-A/B outer discriminator (`packet_type & 1`) and both CHECK constants are CERTAIN.

### BarnaCore FSM reuse (ids 100–119)

The BarnaCore FSM events reuse throttle variant B's word layout: ids 100–115 (`BC_FSM_CHANNEL_CONTROLLER0..15`) and 116–119 (`BC_FSM_{PROCESS_HOSTID,SPARSE_REDUCE,PROCESS_BCID,CONCAT}`) all decode the `13,16,16,22,1,1,10,16,16,16,13,1,2` shape, CHECK 204, 2 pkts (HIGH confidence — same word layout, oneof fields 55–74).

### Per-Gen — glc cycle-skip throttle (ids 200–218)

Ghostlite ships a much larger throttle family: 19 cycle-skip events, each carrying a `TraceIdHeader{21,3,14}` and a small stall-count payload.

```text
THROTTLE_CYCLE_SKIP_THERMAL   glc   oneof 118   CHECK=104 (1 pkt)
  widths: 21,3,14 | 5     trace_id_header + 5-bit cycle-skip count
```

The cycle-skip count **is** the stall: each event records how many clock cycles the engine skipped (was throttled) due to that cause (`THERMAL`, `EXT_BRAKE`, `EXT_THROTTLE`, `LDIDT_BRAKE`, `LDIDT_DROOP`, `ARBITRATION`, `PPM_*`). Ids 200–217 share the `… | 5` stall-count shape; `THROTTLE_LDIDT_RUNNING_MEAN_VOLTAGE` (218) carries a wider voltage sample (HIGH confidence).

---

## Complete pxc Per-Event Reference (99 events)

The full pufferfish `trace_point_id → oneof → CHECK(pkts) → widths` table, every row read byte-exact from its `Decode<Name>`. Bands above carry the named fields; this is the index. Reserved id ranges (11–19, 28–39, 56–79, 98–99) route to the common error label and have no payload.

```text
id  EVENT                                                  oneof CHECK(pk) widths [TIDhdr|payload]
── HOST / UHI ──────────────────────────────────────────────────────────────────────────────────
 0  UHI_HOST_DMA_TRANSACTION_STARTED_ADDRESS_TRANSLATION    f2  216(2)  21,3,12,5,16,10,1,1,54,32
 1  UHI_HOST_PHYSICAL_REQUEST_READ                          f3  233(2)  21,3,12,1,30,1,1,29,26,8,20,20
 2  UHI_HOST_PHYSICAL_RESPONSE_READ                         f4  118(1)  21,3,12,1,20
 3  UHI_HOST_PHYSICAL_REQUEST_WRITE                         f5  233(2)  21,3,12,1,30,1,1,29,26,8,20,20
 4  UHI_HOST_PHYSICAL_RESPONSE_WRITE                        f6  118(1)  21,3,12,1,20
 5  UHI_OCI_REQUEST_READ                                    f7  165(2)  21,3,12,31,1,1,19,14,1,1
 6  UHI_OCI_REQUEST_WRITE                                   f8  165(2)  21,3,12,31,1,1,19,14,1,1
 7  OCI_MESSAGE_SENT_BY_UHI_BRIDGE                          f9  170(2)  21,3,12,31,1,1,1,1,1,2,32,3        [A]
 8  OCI_MESSAGE_RECEIVED_BY_UHI_BRIDGE                      f10 170(2)  21,3,12,31,1,1,1,1,1,2,32,3        [A]
 9  OCI_DESCRIPTOR_RECEIVED_BY_UHI_BRIDGE                   f11 179(2)  21,3,12,2,2,3,2,2,3,2,13,2,1,1,1,13,3,13,3,16  [B]
10  OCI_DESCRIPTOR_SENT_BY_UHI_CLIENT                       f12 179(2)  21,3,12,2,2,3,2,2,3,2,13,2,1,1,1,13,3,13,3,16  [B]
── OCI engine ──────────────────────────────────────────────────────────────────────────────────
20  OCI_DESCRIPTOR_DESC_AT_QNM                              f13 179(2)  21,3,12,…[B]
21  OCI_GENERIC_DESC_ENQUEUED_AT_ENGINE                     f14 100(1)  21,3,12,3
22  OCI_COMMON_READ_CMD_ISSUED_FROM_ENGINE                  f15 228(2)  21,3,12,21,3,7,1,1,5,21,3,12,3,17,17,17,3  [C]
23  OCI_COMMON_MEM_READ_REQ_FROM_ENGINE                     f16 228(2)  21,3,12,…[C]
24  OCI_MESSAGE_MSG_ISSUED_FROM_ENGINE                      f17 170(2)  21,3,12,…[A]
25  OCI_MESSAGE_MSG_ISSUED_FROM_QNM                         f18 170(2)  21,3,12,…[A]
26  OCI_COMMON_WRITE_CMD_ACCEPTED_AT_MN                     f19 228(2)  21,3,12,…[C]
27  OCI_WRITE_REQ_MEM_WRITE_REQ_ISSUED_FROM_ENGINE          f20 128(1)  21,3,12,1,15,12,3
── ICI + ICR DMA ───────────────────────────────────────────────────────────────────────────────
40  ICI_PACKET_PACKET_RECEIVED_ON_LINK_INPUT               f21 125(1)  21,3,12,3,3,6,1,1,12,1,1
41  ICI_PACKET_PACKET_TRANSMITTED_ON_LINK_OUTPUT           f22 125(1)  21,3,12,3,3,6,1,1,12,1,1
42  ICI_PACKET_PACKET_QUEUED_FOR_LINK_TRANSMISSION         f23 125(1)  21,3,12,3,3,6,1,1,12,1,1
43  ICI_PACKET_CONTROL_PACKET_INJECTED_BY_ICR_DMA_BRIDGE   f24 125(1)  21,3,12,3,3,6,1,1,12,1,1
44  ICI_PACKET_DATA_PACKET_INJECTED_BY_ICR_DMA_BRIDGE      f25 125(1)  21,3,12,3,3,6,1,1,12,1,1
45  ICI_PACKET_CONTROL_PACKET_RECEIVED_BY_ICR_DMA_BRIDGE   f26 125(1)  21,3,12,3,3,6,1,1,12,1,1
46  ICI_PACKET_DATA_PACKET_RECEIVED_BY_ICR_DMA_BRIDGE      f27 125(1)  21,3,12,3,3,6,1,1,12,1,1
47  ICI_PACKET_CONTROL_PACKET_QUEUED_FOR_LOCAL_INGRESS     f28 125(1)  21,3,12,3,3,6,1,1,12,1,1
48  ICI_PACKET_DATA_PACKET_QUEUED_FOR_LOCAL_INGRESS        f29 125(1)  21,3,12,3,3,6,1,1,12,1,1
49  OCI_DESCRIPTOR_ENQUEUED_IN_ICR_EGRESS_DMA              f30 179(2)  21,3,12,…[B]
50  OCI_MESSAGE_GENERATED_IN_ICR_EGRESS_DMA                f31 170(2)  21,3,12,…[A]
51  OCI_MESSAGE_GENERATED_IN_ICR_INGRESS_DMA               f32 170(2)  21,3,12,…[A]
52  OCI_MESSAGE_PACKET_SENT_TO_OCI                         f33 170(2)  21,3,12,…[A]
53  OCI_MESSAGE_PACKET_RECEIVED_IN_ICR                     f34 170(2)  21,3,12,…[A]
54  OCI_COMMON_OCI_WRITE_COMMAND                           f35 228(2)  21,3,12,…[C]
55  OCI_COMMON_OCI_READ_COMMAND                            f36 228(2)  21,3,12,…[C]
── TCS sequencer ───────────────────────────────────────────────────────────────────────────────
80  TCS_EXTERNAL_SYNC_FLAG_UPDATE_DMA_DONE                 f37 163(2)  21,3,12,31,1,1,1,1,9,16,1,1,1,1,1,1
81  TCS_INTERNAL_SET_SYNC_FLAG                             f38 121(1)  32,1,9,16,1,1   (sub-tags 0x26/0x27/0x28)
82  TCS_INTERNAL_ADD_SYNC_FLAG                             f39 121(1)  32,1,9,16,1,1
83  TCS_INTERNAL_HOST_INTERRUPT                            f40 121(1)  32,1,9,16,1,1
84  TCS_INTERNAL_SET_TRACEMARK                             f41 121(1)  32,1,9,16,1,1
85  TCS_INTERNAL_TRACE_INSTRUCTION                         f42 121(1)  32,1,9,16,1,1
86  TCS_INTERNAL_UNSUCCESSFUL_SYNC_ATTEMPT                 f43 121(1)  32,1,9,16,1,1   (wait blocked)
87  TCS_INTERNAL_SUCCESSFUL_SYNC_ATTEMPT                   f44 121(1)  32,1,9,16,1,1   (wait unblocked)
88  TCS_INTERNAL_READ_SYNC_FLAG                            f45 121(1)  32,1,9,16,1,1
89  TCS_INTERNAL_SCALAR_FENCE_START                        f46 121(1)  32,1,9,16,1,1
90  TCS_INTERNAL_SCALAR_FENCE_END                          f47 121(1)  32,1,9,16,1,1
91  OCI_DESCRIPTOR_COMMON_ISSUED_FROM_TCS                  f48 211(2)  21,3,12,…[B]+31,1
92  OCI_DESCRIPTOR_STRIDE_SRC_ISSUED_FROM_TCS              f49 195(2)  21,3,12,31,1,1,1,32,32
93  OCI_DESCRIPTOR_STRIDE_DST_ISSUED_FROM_TCS              f50 195(2)  21,3,12,31,1,1,1,32,32
94  OCI_DESCRIPTOR_STRIDE_STEPS_ISSUED_FROM_TCS            f51 195(2)  21,3,12,31,1,1,1,32,32
95  OCI_MESSAGE_ISSUED_FROM_TCS                            f52 170(2)  21,3,12,…[A]
96  OCI_COMMON_COMPLETED_IN_TCS                            f53 228(2)  21,3,12,…[C]
97  THROTTLE_STATE_THERMAL_AND_ELECTRICAL                  f54/f55 120(1)/204(2)  A: 4,5,5,10,4,21,5,5 | B: 13,16,16,22,…/10,16,16,16,13,1,2
── BarnaCore + CMQ ─────────────────────────────────────────────────────────────────────────────
100-115 BC_FSM_CHANNEL_CONTROLLER0..15                     f55-f70 204(2) 13,16,16,22,1,1,10,16,16,16,13,1,2 (all identical)
116-119 BC_FSM_{PROCESS_HOSTID,SPARSE_REDUCE,PROCESS_BCID,CONCAT} f71-f74 204(2) same BC-FSM word
120-124 BCS_{TRACE_INSTRUCTION,SET_TRACEMARK,SYNC_START_STOP_TRACE,HOST_INTERRUPT,FENCE} f75-f79 127(1) 32,3,16,13,1,1
125-128 BC_OCI_{READ_REQUEST,READ_RESPONSE,WRITE_REQUEST,WRITE_RESPONSE} f80-f83 193(2) 21,3,12,4,16,11,1,1,37,5,1,20
129 OCI_DESCRIPTOR_COMMON_ISSUED_BY_BC                     f84 211(2)  21,3,12,…[B]+31,1
130-132 OCI_DESCRIPTOR_STRIDE_{SRC,DST,STEPS}_ISSUED_BY_BC f85-f87 195(2) 21,3,12,31,1,1,1,32,32
133 OCI_MESSAGE_RECEIVED_BY_BC                             f88 170(2)  21,3,12,…[A]
134 OCI_MESSAGE_SENT_BY_BC                                 f89 170(2)  21,3,12,…[A]
140 CMQ_VPU_DMA_DESC                                       f90 105(1)  21,3,12,8
141 OCI_MESSAGE_CMQ_VPU_DMA_MSG                            f91 170(2)  21,3,12,…[A]
142-149 CMQ_VPU_DMA_REQ_*                                  f92-f99 123(1) 21,3,12,2,4,20 (all 8 identical)
── Sentinel ────────────────────────────────────────────────────────────────────────────────────
255 DUMMY_TRACE_ENTRY_DUMMY_TRACE_POINT                    f100 128(1) 21,3,12,31
```

> **NOTE —** the non-pxc families (vfc/vlc/glc/gfc) decode with the *same* parser — the frame is family-invariant — but their full per-event width tables are not tabulated here. The six high-value bands were decoded on glc to pin the per-gen deltas (`chip_id` 12→14, `dst_chip_id` 12→14, `virtual_channel` 3→2, the `+lcc` sync-flag growth, the HDE/CMN-DMA successors). The residual ~480 variants (122 vfc + 78 vlc + 135 glc + 144 gfc, minus the bands here) are mechanically dumpable from each `Decode<Name>` with the same width-extraction; they are LOW confidence on completeness and not reproduced. The newer-family deltas are owned by [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md); the SparseCore `SC_*` band by [Payload: SparseCore Band](payload-sc-band.md); the jxc `PerformanceTraceEntry` schema by [Payload: jxc Legacy](payload-jxc-legacy.md).

---

## Selector Enums and Reassembly Gaps

Two classes of detail are deliberately not tabulated, because they live outside the decode functions this page reads:

- **Selector integer→name tables.** The bit *widths* of every selector are decoded above; the integer→name mappings (`dma_type`, `node_type ∈ {TCS,BC,CMQ,HBMQ,UHI,ICR,QNM}`, `router_link_port_id ∈ {LINK0..LINK5}`, `core_id ∈ {RESERVED,NONCORE,TC0,TC1,BC0..BC3}`, `access_type`, `queue_id`, `thread_id`, `src/dst_opcode`, `src/dst_mem_id`) live in the nested `*Values` enums of each `trace_entries.proto`, recoverable from the FDP descriptor pool but not enumerated value-by-value here (LOW confidence on exact integer ordering).
- **uint64 fragment reassembly.** For fields wider than 32 bits read as several `GetBits64` fragments (`dpa_upper_bits` 30+29, `address` 26+33, `dva` 1+1+54, `src_operand`, `lcc` 7+1+1+57), the *widths and the destination slot* (`TraceEntry+0xNN`) are CERTAIN, but the precise per-fragment shift/OR bit-position inside the 64-bit value was traced only for the named bands (LOW confidence on the exact reassembly order; a re-encoder must recover it per field to be byte-exact for the >32-bit fields).

---

## Decoder Evidence Anchors

| Event / group | Decoder | Address | CHECK | Confidence |
|---|---|---|---|---|
| UHI address translation | `DecodeUhiHostDmaTransactionStartedAddressTranslation` | `0xf5b0b80` | 216 | CERTAIN |
| UHI physical request read | `DecodeUhiHostPhysicalRequestRead` | `0xf5b0f20` | 233 | CERTAIN |
| UHI physical response read | `DecodeUhiHostPhysicalResponseRead` | `0xf5b1300` | 118 | CERTAIN |
| UHI OCI request read | `DecodeUhiOciRequestRead` | `0xf5b1be0` | 165 | CERTAIN |
| OCI read command (SHAPE-C) | `DecodeOciCommonOciReadCommand` | `0xf5b8dc0` | 228 | CERTAIN |
| OCI descriptor (SHAPE-B) | `DecodeOciDescriptorDescAtQnm` | `0xf5b34a0` | 179 | CERTAIN |
| ICI link packet (pxc) | `DecodeIciPacketPacketReceivedOnLinkInput` | `0xf5b56c0` | 125 | CERTAIN |
| ICI link packet (glc) | `DecodeIciPacketPacketReceivedOnLinkInput` | `0xf6322c0` | 128 | HIGH |
| TCS set sync flag (pxc) | `DecodeTcsInternalSetSyncFlag` | `0xf5b97a0` | 121 | CERTAIN |
| TCS set sync flag (glc, +lcc) | `DecodeTcsInternalSetSyncFlag` | `0xf6380c0` | 187 | HIGH |
| TCS external sync done | `DecodeTcsExternalSyncFlagUpdateDmaDone` | `0xf5b9340` | 163 | CERTAIN |
| Throttle two-variant | `DecodeThrottleStateThermalAndElectricalThrottleState` | `0xf5bc620` | 120 / 204 | CERTAIN |
| CMQ VPU-DMA req | `DecodeCmqVpuDmaReqVmem0ToCmemRead` | `0xf5c4700` | 123 | CERTAIN |
| CMQ VPU-DMA desc | `DecodeCmqVpuDmaDesc` | `0xf5c40c0` | 105 | CERTAIN |
| HDE host request (glc) | `DecodeHdeHostRequestRead` | `0xf62e660` | 178 | HIGH |
| CMN-DMA request (glc) | `DecodeCmnDmaRequestEastSideLane0` | `0xf6368e0` | 205 | HIGH |
| bit-codec primitive | `BitDecoder::GetBits64NoInline` | `0x21073760` | — | CERTAIN |
| width mask table | `mask_` (`mask_[k]=(1<<k)-1`) | `0xbe79440` | — | CERTAIN |

All `Decode<Name>` start addresses above were confirmed against the binary's symbol table (`…_functions.json`): every anonymous-namespace decoder is present with its full mangled name (`asic_sw::driver::deepsea::pxc::profiler::(anonymous namespace)::Decode<Name>(string_view, bool*, TraceEntry*)`) at the listed address. `mask_` holds every used width byte-exact (`mask_[1..3,8,12,14,16,21,30,45,48]`), confirming the field widths are real read masks, not inferred.

---

## Related Components

| Component | Relationship |
|---|---|
| [TraceEntriesCoder](trace-entries-coder.md) | owns the 16-byte packet, framing prefix, `TraceHeader`/`TraceIdHeader`, dual dispatch, and the per-event `CHECK` mechanism this page's payloads sit inside |
| [TracePoints Master Registry](tracepoints-master-registry.md) | owns the `trace_point_id` → name and id ↔ oneof-field registry; this page decodes the payloads those ids name |
| [Payload: SparseCore Band](payload-sc-band.md) | the SparseCore `SC_*` band payload field maps |
| [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) | the newer-family payload deltas (6-bit `block_id`, 45-bit timestamp, 14-bit `chip_id`) and the residual per-event tables |
| [Payload: jxc Legacy](payload-jxc-legacy.md) | the separate `PerformanceTraceEntry` schema and its own codec |
| [UHI Host Interface](../dma/uhi-host-interface.md) | the host-DMA engine whose transactions the UHI trace band observes |
| [OCI Command DMA ID](../dma/oci-command-dma-id.md) | the OCI command/descriptor DMA identity these OCI trace events carry |
| [ICI DMA Descriptor](../ici/dma-descriptor.md) | the inter-chip DMA descriptor the ICI link-packet events transport |

## Cross-References

- [Profiling and Telemetry Overview](overview.md) — the five-stage capture→encode→decode→xplane pipeline; these payloads are the device-event content of stage 3
- [TraceEntriesCoder](trace-entries-coder.md) — read first: the universal frame (16-byte packet, 61-bit envelope, dual dispatch, total-bit `CHECK`) every payload here extends
- [TracePoints Master Registry](tracepoints-master-registry.md) — the wire-id / oneof-field id spaces; this page is the payload appendix to that registry's names
- [Payload: SparseCore Band](payload-sc-band.md) · [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) · [Payload: jxc Legacy](payload-jxc-legacy.md) — the sibling payload pages for the bands this page does not own
- [UHI Host Interface](../dma/uhi-host-interface.md) · [OCI Command DMA ID](../dma/oci-command-dma-id.md) · [ICI DMA Descriptor](../ici/dma-descriptor.md) — the subsystem pages for the hardware engines these trace bands instrument
