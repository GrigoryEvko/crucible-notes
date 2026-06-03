# Trace Payload: vfc / vlc / gfc Per-Gen Bands

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — full C++ symbols are present, and `.text` VMA equals file offset. Other versions will differ.*

## Abstract

This page is the per-silicon-generation completion of the device-event payload decode. [Trace Payload: UHI / OCI / ICI / DMA](payload-uhi-oci-ici-dma.md) decodes the **pxc** (pufferfish) family byte-exact and the **glc** family as a representative sample; this page decodes the same six high-value bands on the three remaining families — **vfc** (Viperfish), **vlc** (Viperfish-lite), and **gfc** (Trillium) — and pins the per-generation *deltas*: the bit widths that drift, the fields that appear and disappear, and the framing budget that shifts. It then enumerates the **selector enum value tables** that the prior pages left as bit-widths only: the integer→name maps that every `enum`-typed payload field keys into.

The parser is family-invariant — the same `Decode<Name>(string_view, bool* started_out, TraceEntry* out)` shape, the same `BitDecoder::GetBits64NoInline` width immediates, the same total-bit `CHECK` (`cmp $CONST,%rdi; jne FATAL`), the same `TraceEntry+0x28` oneof stamp. What changes per family is the *widths* and the *field set*. Each `Decode<Name>` lives in the family's anonymous namespace (`asic_sw::driver::deepsea::vxc::vfc::profiler`, `…::vxc::vlc::profiler`, `…::gxc::gfc::profiler`) and is reachable from that family's `DecodeEntry` jump table. The C++ namespaces are themselves the generation evidence: vfc and vlc share the `vxc` parent (Viperfish and its lite cut), gfc sits under `gxc` (Trillium).

Three facts drive every table below and must be held simultaneously. First, the `TraceIdHeader.chip_id` widens **12→14** at Viperfish (not at glc): `vfc`/`vlc`/`glc`/`gfc` all carry `TraceIdHeader{transaction_id 21, core_id 3, chip_id 14}` = 38 bits; only pxc uses 12. Second, **vlc is the anomaly** — its `DecodeTraceHeader` reads `block_id 3` **and** `timestamp 45`, a 56-bit header and a **58-bit** frame+header, so every vlc payload starts at packet bit 58 and every vlc `CHECK` is re-based on 58, not 61. Third, the field set itself grows per generation: the TCS sync-flag band gains a 64-bit `lcc` field from glc onward, and gfc adds three bands (STATS_COUNTER, O2CUR, FLL) that exist on no earlier silicon.

For reimplementation, the contract this page completes is:

- **The per-gen width deltas** for the six bands (HDE, OCI, ICI, intra-chip DMA, TCS sync-flag, throttle/stall) on vfc/vlc/gfc — every `GetBits64` width sequence and total-bit `CHECK`, expressed as the delta from the pxc/glc baselines.
- **The vlc 58-bit-header re-basing rule** — `payload_bits = CHECK − 58` for vlc, `CHECK − 61` for every other family.
- **The selector enum value tables** — the integer→name map for every `enum`-typed selector field (`core_id`, `node_type`/`extra_id`, `thread_id`, `dma_type`, `src/dst_opcode`, `src/dst_mem_id`, `router_link_port_id`, sync `opcode`, `final_throttle_source`, gfc `size`/`cmn_router_type`, …), read byte-exact from the descriptor-pool string tables.

| Family | C++ namespace | `chip_id` | Header | Frame+hdr | Payload @ bit | New / removed bands | Confidence |
|---|---|---|---|---|---|---|---|
| pxc | `…deepsea::pxc::profiler` | 12 | 59 | 61 | 61 | baseline (UHI, CMQ, BC-FSM) | CERTAIN |
| **vfc** | `…deepsea::vxc::vfc::profiler` | 14 | 59 | 61 | 61 | UHI→HDE, CMQ→CMN-DMA | CERTAIN |
| **vlc** | `…deepsea::vxc::vlc::profiler` | 14 | **56** | **58** | **58** | UHI→HDE, CMQ→VDQ | CERTAIN |
| glc | `…deepsea::glc::profiler` | 14 | 59 | 61 | 61 | +lcc sync-flag | HIGH |
| **gfc** | `…deepsea::gxc::gfc::profiler` | 14 | 59 | 61 | 61 | +STATS_COUNTER, +O2CUR, +FLL, +sync `lcc` | CERTAIN |

---

## Reading a Per-Gen Row

The notation is identical to the sibling [UHI/OCI/ICI/DMA page](payload-uhi-oci-ici-dma.md): a width sequence is written `TIDhdr | payload`. The leading `21,3,14` (all four newer families) is the `TraceIdHeader{transaction_id 21, core_id 3, chip_id 14}` = 38 bits; the `|` separates it from the typed payload scalars. The `oneof` column is the proto2 oneof field number stamped at `TraceEntry+0x28` — the stable cross-gen dispatch key. The `CHECK` is the hardcoded total-bit constant the decoder validates.

The single re-basing rule that governs every vlc row:

```c
// payload-bit derivation from CHECK, per family
payload_bits = CHECK - (family == vlc ? 58 : 61);   // vlc header is 56→58 frame+header
pkts         = (CHECK <= 128) ? 1 : 2;               // movq $0x10 / $0x20 → 0x8(%rbx)
```

> **GOTCHA —** the vlc 58-bit header is the trap that breaks every cross-family assumption. `DecodeTraceHeader` @ `0xf5f5b40` reads `GetBits64(8)` (trace_point_id), `GetBits64(3)` (block_id), `GetBits64(45)` (timestamp) = 56 header bits → 58 frame+header. Every other family reads a 59-bit header (block_id 6 *or* timestamp 48). A reimplementation that hardcodes the 61-bit payload origin will mis-position **every** vlc field and fail **every** vlc `CHECK` by exactly 3 bits. Confirmed against vlc `DecodeTcsInternalSetSyncFlag` @ `0xf5e1460`: payload `32,1,9,16,1,1` = 60 bits, CHECK `0x76`=118 = 58+60, `movq $0x10` (1 pkt).

> **CORRECTION (VGG-1) —** an earlier per-gen header table (the device-trace-wire-format draft) listed vlc `timestamp = 48` (a 59-bit header). The binary shows `GetBits64(…, 45, …)` @ `0xf5f5b40+0x?` — vlc's timestamp is **45** (0x2d), its header **56** bits, its frame+header **58**. This page uses the byte-exact 58. The pin is CERTAIN: the `45` immediate is in the `DecodeTraceHeader` disassembly.

---

## HDE — Host DMA Engine (vfc / vlc / gfc)

HDE replaces the pxc **UHI** band from Viperfish onward — the host↔chip memory-DMA engine, observed as four core events: REQUEST/RESPONSE × READ/WRITE. Every HDE event carries `TraceIdHeader{21,3,14}` = 38 bits. The request fields are `thread_id` (enum, 3) · `address` (uint64, fragmented) · `size_units_of_32B` (uint32, 5) · `thread_tracking_id` (uint32); the response carries only `thread_id` (3) + `thread_tracking_id`.

### Payload Field Map

| gen | event | oneof | CHECK | pkts | widths (`21,3,14` \| payload) | Confidence |
|---|---|---|---|---|---|---|
| vfc | `HdeHostRequestWrite` | 10 | 178 | 2 | `21,3,14 \| 3,26,1,1,33,5,10` | CERTAIN |
| vfc | `HdeHostRequestRead` | 12 | 178 | 2 | `21,3,14 \| 3,26,1,1,33,5,10` | CERTAIN |
| vfc | `HdeHostResponseWrite` | 11 | 112 | 1 | `21,3,14 \| 3,10` | CERTAIN |
| vfc | `HdeHostResponseRead` | 13 | 112 | 1 | `21,3,14 \| 3,10` | CERTAIN |
| vlc | `HdeHostRequestWrite` | 8 | 175 | 2 | `21,3,14 \| 3,29,1,1,30,5,10` | CERTAIN |
| vlc | `HdeHostRequestRead` | 10 | 175 | 2 | `21,3,14 \| 3,29,1,1,30,5,10` | CERTAIN |
| vlc | `HdeHostResponseWrite` | 9 | 109 | 1 | `21,3,14 \| 3,10` | CERTAIN |
| vlc | `HdeHostResponseRead` | 11 | 109 | 1 | `21,3,14 \| 3,10` | CERTAIN |
| gfc | `HdeHostRequestWrite` | 3 | 179 | 2 | `21,3,14 \| 3,26,1,1,33,5,11` | CERTAIN |
| gfc | `HdeHostRequestRead` | 5 | 179 | 2 | `21,3,14 \| 3,26,1,1,33,5,11` | CERTAIN |
| gfc | `HdeHostResponseWrite` | 4 | 113 | 1 | `21,3,14 \| 3,11` | CERTAIN |
| gfc | `HdeHostResponseRead` | 6 | 113 | 1 | `21,3,14 \| 3,11` | CERTAIN |

### Per-Gen Delta

- **vfc == glc**: the [glc reference](payload-uhi-oci-ici-dma.md#per-gen-successor--hde-host-dma-engine-glc-ids-813) decodes `21,3,14 | 3,26,1,1,33,5,10`, CHECK 178 — byte-identical to vfc. The `address` is the `26,1,1,33` fragment group (one uint64 reassembled from a 26-bit and a 33-bit fragment plus two 1-bit flags interleaved).
- **vlc narrows the address**: `29,1,1,30` instead of `26,1,1,33` — the device virtual address fits a narrower split on the lite cut. Combined with the 58-bit header, the request CHECK drops to 175 (58 + 117) and the response to 109 (58 + 51).
- **gfc widens `thread_tracking_id` 10→11**: the only gfc HDE delta. Request CHECK rises to 179, response to 113. Everything else matches vfc.

The read/write pairing is structural exactly as on pxc UHI: each pair (request-read ↔ request-write, response-read ↔ response-write) has an identical wire shape and is distinguished only by `trace_point_id` / oneof. Decoder anchors: vfc `DecodeHdeHostRequestRead` @ `0xf5fbde0` (oneof 12, CHECK 178), vfc `HdeHostResponseRead` @ `0xf5fc1a0` (oneof 13, CHECK 112), vlc `DecodeHdeHostRequestWrite` @ `0xf5d9580` (oneof 8, CHECK 175), gfc `DecodeHdeHostRequestWrite` @ `0xf662780` (oneof 3, CHECK 179).

---

## OCI — Descriptor / Message / Command (vfc / vlc / gfc)

The dominant band — its event count grows pxc 32 → vfc 52 / vlc 38 / gfc 47. The shape-sharing structure is identical to pxc: three recurring payload shapes (A message, B descriptor-common, C read/write-command) drive most events. On the newer families every `trace_id_header` is 38 bits (`chip_id` 14). The shapes **drift** per generation — the sub-field widths change while the field *set* holds. vfc and gfc share OCI sub-field widths almost exactly; vlc consistently differs (wider `msg_data`/`addr`, a different cmd1 scalar split) **and** carries the 58-bit header, so its CHECKs run a few bits lower.

### SHAPE-C — OCI read/write command (3× `TraceIdHeader`)

The richest OCI shape: three `trace_id_header` records (`cmd0`/`cmd1`/`cmd2`), with the command scalars interleaved between the second and third header. Fields: `index_valid`, `id_index0/1/2` (uint32, 17 each), trailing node-identity enum (3 bits — `node_type` on pxc, `extra_id` on the newer gens).

| gen | `OciCommonReadCmdIssuedFromEngine` widths | CHECK | Confidence |
|---|---|---|---|
| vfc | `21,3,14, 21,3,5,1,1,9, 21,3,14, 3,17,17,17,3` | 234 | CERTAIN |
| gfc | `21,3,14, 21,3,5,1,1,9, 21,3,14, 3,17,17,17,3` | 234 | CERTAIN |
| vlc | `21,3,14, 21,3,8,1,1,6, 21,3,14, 3,17,17,17,3` | 231 | CERTAIN |

> **QUIRK —** the cmd1 scalar group is where vlc breaks ranks. vfc/gfc encode the cmd1 interleave as `5,1,1,9`; vlc as `8,1,1,6` — the two scalar widths (a counter and an id) are re-apportioned 5/9 → 8/6. The total payload-bit count differs by only the header re-base (vlc 231 = 58+173; vfc/gfc 234 = 61+173), so a decoder that reads "five bits then nine" will mis-read vlc's cmd1 even though the overall CHECK looks consistent. The pxc reference is `21,3,12, 21,3,7,1,1,5, …` CHECK 228 — narrower headers, different cmd1 split again. Decoder anchor: vfc `DecodeOciCommonReadCmdIssuedFromEngine` @ `0xf5fd560` (CHECK 234).

### SHAPE-B — OCI descriptor common

`dma_type` + src/dst `{mem_mem_id, core_id, opcode}` + sync-flag ids/core-ids + `program_counter`. The descriptor grew versus pxc's CHECK 179.

| gen | `OciDescriptorCommon` / `DescAtQnm` widths | CHECK | Confidence |
|---|---|---|---|
| vfc | `21,3,14, 1,2,3,2,2,3,2,13,1,1,1,2,13,3,13,3,2,1,1,16,32` | 216 | CERTAIN |
| gfc | `21,3,14, 1,2,3,2,2,3,2,13,1,1,1,2,13,3,13,3,3,1,1,16,32` | 217 | CERTAIN |
| vlc | `21,3,14, 1,2,3,2,2,3,2,13,3,1,1,1,12,3,13,3,1,16,32` | 210 | CERTAIN |

vfc→gfc differ by a single bit (one trailing enum field widens 2→3, CHECK 216→217). vlc restructures the sync-flag id group (a `12` where vfc/gfc carry a `13`) and re-bases on the 58-bit header.

### SHAPE-A — OCI message

`msg_data` + `done` + `msg_type` + node-selectors + `addr`.

| gen | `OciMessagePacketSentToOci` widths | CHECK | note | Confidence |
|---|---|---|---|---|
| vfc | `21,3,14, 29,1,1,3,1,1,2,33,3` | 173 | msg_data 29, addr 33 | CERTAIN |
| gfc | `21,3,14, 29,1,1,3,1,1,2,33,3` | 173 | == vfc | CERTAIN |
| vlc | `21,3,14, 32,1,1,1,1,2,34,3` | 171 | msg_data 32, addr 34, 58-bit hdr | CERTAIN |

The pxc reference SHAPE-A is `21,3,12, 31,1,1,1,1,1,2,32,3` CHECK 170. vlc widens both `msg_data` (32 vs 29) and `addr` (34 vs 33) and drops one of the 1-bit flag positions — the largest per-gen SHAPE-A drift.

> **NOTE — two id spaces (confirmed on vfc).** The on-wire `trace_point_id` that `DecodeEntry` indexes is a **dense** space 0..0x5f=95 (jump table @ `0xab86ce8`, 96 arms; gaps at 15–19 / 27–29 route to the error label @ `0xf5f800f`), distinct from the proto `TracePointId` enum value (which the [TracePoints registry](tracepoints-master-registry.md) lists up to 185). Worked arm: vfc on-wire id 14 → `DecodeOciMessageSentByHde` @ `0xf5fcf20` (oneof field 16). The **oneof field number** is the stable cross-gen key used in every row on this page; the on-wire id is per-gen and per-family. The full per-gen on-wire-id → oneof arm map was not exhaustively dumped (LOW confidence on completeness — see [Open Items](#open-items)).

Per-family `DecodeEntry` bounds: vfc @ `0xf5f7080` (`cmp $0x5f,%rax`), vlc @ `0xf5d6460` (`cmp $0x8f`), glc @ `0xf6295c0` (`cmp $0x62`), gfc @ `0xf65ffe0` (`cmp $0x64`).

---

## ICI — Inter-Chip Link Packet (vfc / vlc / gfc)

All **nine** ICI events share one payload shape per family — they differ only by lifecycle stage (received/transmitted/queued, control/data injected/received by the ICR DMA bridge). Fields, descriptor-confirmed and identical every gen: `trace_id_header`, `router_link_port_id` (enum), `virtual_channel` (uint32), `link_targets` (uint32), `local_ingress_target` (bool), `multicast` (bool), `dst_chip_id` (uint32), `first_packet_in_dma` (bool), `last_packet_in_dma` (bool). One 16-byte packet each.

| gen | widths (TIDhdr \| payload) | CHECK | pkts | `virtual_channel` | `dst_chip_id` | Confidence |
|---|---|---|---|---|---|---|
| pxc | `21,3,12 \| 3,3,6,1,1,12,1,1` | 125 | 1 | 3 bits | 12 bits | CERTAIN |
| vfc | `21,3,14 \| 3,2,6,1,1,14,1,1` | 128 | 1 | 2 bits | 14 bits | CERTAIN |
| vlc | `21,3,14 \| 3,3,6,1,1,14,1,1` | 126 | 1 | **3 bits** | 14 bits | CERTAIN |
| glc | `21,3,14 \| 3,2,6,1,1,14,1,1` | 128 | 1 | 2 bits | 14 bits | HIGH |
| gfc | `21,3,14 \| 3,2,6,1,1,14,1,1` | 128 | 1 | 2 bits | 14 bits | CERTAIN |

> **QUIRK —** vlc keeps the 3-bit `virtual_channel` that every other newer family narrowed to 2. The mainline drift at Viperfish is two compensating changes: `dst_chip_id` widens 12→14 (tracking the 14-bit `chip_id`) and `virtual_channel` narrows 3→2, so vfc/glc/gfc land at CHECK 128 and still fit one packet. vlc — alone — widens `dst_chip_id` to 14 yet **retains** the 3-bit `virtual_channel`, so its payload is one bit wider (CHECK 126 = 58 + 68 vs vfc's 128 = 61 + 67). A reimplementation that keys `virtual_channel` width on "newer family ⇒ 2 bits" will mis-decode every vlc ICI packet. Drive the width from the per-family decoder. `link_targets` stays 6 bits and `router_link_port_id` stays 3 bits (6 ports, LINK0..LINK5) across all gens.

Decoder anchors: vfc `DecodeIciPacketPacketReceivedOnLinkInput` @ `0xf5ff460` (oneof 24, CHECK 128), vlc @ `0xf5dd340` (oneof 23, CHECK 126, the vc=3 anomaly), gfc @ `0xf667c60` (oneof 22, CHECK 128). `virtual_channel` and `link_targets` are bare uint32 fields, **not** enums — their meaning is the physical VC index and the per-port target bitmask.

---

## Intra-Chip DMA — CMN-DMA (vfc / gfc), VDQ (vlc)

The pxc CMQ VPU-DMA band has no direct successor on the newer families. Each replaces it differently, and this is the band where the three generations diverge most.

### vfc — CMN-DMA, East/West side × Lanes 0..3 (8 events)

The chip-memory-network DMA, observed per side and lane. Fields (descriptor): `trace_id_header`, `thread_id` (enum, 14-value), `req_id`, `cmn_uncore_router_id_valid0/1` (bool), `cmn_uncore_router_id0/1`, `src_opcode`/`src_mem_id` (enum), `src_operand`, `dst_opcode`/`dst_mem_id` (enum), `dst_addr`, `beats`, `poison`. All 8 lanes are byte-identical:

```text
vfc CmnDmaRequestEastSideLane0 (oneof 42) / WestSideLane0 (oneof 46)
  widths: 21,3,14 | 4,10,1,1,5,5,2,1,1,1,2,32,2,3,32,4,1
  CHECK 206 (2 pkts)
  thread_id(enum 4), req_id(u32 10), router_id_valid0/1(bool,bool),
  router_id0/1(u32 5,5), src_opcode(enum 2), … src_operand/dst_addr (32-bit fragments),
  dst_mem_id(enum 3), beats(u32 4), poison(bool 1)
```

> **NOTE —** the glc analogue ([sibling page](payload-uhi-oci-ici-dma.md#per-gen-successor--cmn-dma-glc-ids-7279)) decodes `21,3,14 | 3,10,1,1,5,5,2,2,1,1,1,32,2,3,32,4,1`, CHECK 205 — glc's `thread_id` is 3 bits, vfc's is **4**. The extra `thread_id` bit (and one fewer interior field) is the only vfc↔glc CMN-DMA delta; both expose the dual `cmn_uncore_router_id` endpoints that pxc CMQ lacked. Anchor: vfc `DecodeCmnDmaRequestEastSideLane0` @ `0xf603a80` (oneof 42, CHECK 206).

### gfc — CMN-DMA, Set0/Set1 × Lane0/1 (4 events), restructured

gfc keeps the CMN-DMA name but **restructures** the record. Fields (descriptor): `trace_id_header`, `req_id`, `cmn_router_id`, `cmn_router_type` (enum CMNUR/O2CUR), `src_mem_id` (enum), `src_addr` (uint64), `dst_mem_id` (enum), `dst_addr` (uint64), `beats`, `poison`. All 4 byte-identical:

```text
gfc CmnDmaRequestSet0Lane0 (oneof 41)
  widths: 21,3,14 | 10,5,1,4,9,1,1,24,4,33,4,1
  CHECK 196 (2 pkts)
  req_id(u32 10), cmn_router_id(u32 5), cmn_router_type(enum 1→CMNUR/O2CUR),
  src_mem_id(enum 4), src_addr(9+…), dst_mem_id … dst_addr(24+33 fragments), beats(4), poison(1)
```

gfc drops the dual `cmn_uncore_router_id_valid` bits that vfc/glc carried and adds `cmn_router_type` to select between the CMNUR router and the **O2CUR** address-translation router on the unified memory network. Anchor: gfc `DecodeCmnDmaRequestSet0Lane0` @ `0xf66c5c0` (oneof 41, CHECK 196).

### vlc — VDQ, vector DMA queue, READ/WRITE × REQ/RESP × CHAN0/1 (8 events)

Viperfish-lite has no chip-memory-network; its only intra-chip DMA observable is the **VDQ** (vector DMA queue), a minimal `{flag, id/addr}` record. All 8 channels byte-identical:

```text
vlc VdqTransactionReadReqChan0 (oneof 64)
  widths: 21,3,14 | 1,18
  CHECK 115 (1 pkt; 58 + 57)
  a 1-bit flag + an 18-bit id/addr
```

The VDQ is the smallest intra-chip DMA payload on any family — one flag and one 18-bit value behind the 38-bit identity header. Anchor: vlc `DecodeVdqTransactionReadReqChan0` @ `0xf5e56e0` (oneof 64, CHECK 115).

---

## TCS Sync-Flag Band (vfc / vlc / gfc)

The TensorCore-Sequencer internal semaphore/fence/interrupt band. The internal events carry **no** `TraceIdHeader` — the core is implied by `block_id` in the header — so the payload begins directly at the family's payload origin (bit 61 for vfc/gfc, bit **58** for vlc). Base fields (descriptor): `data_field` (uint32), `done_bit` (bool), `sync_flag_number` (uint32), `program_counter` (uint32), `sfence_end` (bool), `sfence_start` (bool).

### Per-Gen Growth of the `lcc` Field

| gen | `TcsInternalSetSyncFlag` widths | CHECK | pkts | `lcc`? | `sync_flag_number` | Confidence |
|---|---|---|---|---|---|---|
| pxc | `32,1,9,16,1,1` | 121 | 1 | NO | 9 bits | CERTAIN |
| vfc | `32,1,9,16,1,1` | 121 | 1 | NO | 9 bits | CERTAIN |
| vlc | `32,1,9,16,1,1` | 118 | 1 | NO | 9 bits | CERTAIN |
| glc | `32,1,9,16,1,1,7,1,1,57` | 187 | 2 | YES (uint64) | 9 bits | HIGH |
| gfc | `32,1,12,16,1,1,4,1,1,60` | 190 | 2 | YES (uint64) | **12 bits** | CERTAIN |

> **QUIRK —** the TCS sync-flag payload grows in two independent steps. pxc/vfc/vlc share the 6-field, 60-bit payload with **no** `lcc` (proto field count 6); the only vlc delta is the 58-bit header (CHECK 118 = 58 + 60). From glc onward a 7th field, `lcc` (uint64), is appended — reassembled from the trailing fragments (glc `{7,1,1,57}`, gfc `{4,1,1,60}`) — pushing the event to two packets. gfc **additionally** widens `sync_flag_number` 9→12. So a single decoder cannot serve all five families: the field *count* differs (6 vs 7), the payload origin differs (vlc), and one interior width differs (gfc). Anchors: vfc `DecodeTcsInternalSetSyncFlag` @ `0xf6065e0` (oneof 51, CHECK 121, no lcc), vlc @ `0xf5e1460` (oneof 40, CHECK 118), gfc @ `0xf66db20` (oneof 46, CHECK 190, `12`-bit sync_flag_number + lcc).

All sibling TCS internal events share the SetSyncFlag shape in their generation: `AddSyncFlag`, `CoreInterrupt` (renamed from pxc `HostInterrupt`), `SetTracemark`, `TraceInstruction`, `Read/Successful/UnsuccessfulSyncAttempt`, `ScalarFenceStart/End`. The rename keeps the wire shape — vfc `TcsInternalCoreInterrupt` @ `0xf606a20` (oneof 53) decodes the same `32,1,9,16,1,1`, CHECK 121.

### External Sync-Flag DMA-Done (carries `TraceIdHeader`)

`TCS_EXTERNAL_SYNC_FLAG_UPDATE_DMA_DONE` is the cross-chip set-done completion — the receive-side observation of a remote DMA that bumped a local sync flag — and is the one TCS event that carries identity. Fields (descriptor): `updated_sync_flag_value`, `updated_sync_flag_done`, `sync_flag_number`, `program_counter`, then `successful_sync_unblock`, `successful_sync`, `last_sync_for_dma`, `last_sync_was_add`, `was_csr_update`, `trace_bit_set`.

| gen | widths (`21,3,14` \| payload) | CHECK | Confidence |
|---|---|---|---|
| vfc | `21,3,14 \| 29,1,1,3,1,9,16,1,1,1,1,1,1` | 165 | CERTAIN |
| vlc | `21,3,14 \| 32,1,1,1,9,16,1,1,1,1,1,1` | 162 | CERTAIN |
| gfc | `21,3,14 \| 29,1,1,3,1,12,16,1,1,1,1,1,1` | 168 | CERTAIN |

The pxc reference is `21,3,12 | 31,1,1,1,1,9,16,1,1,1,1,1,1` CHECK 163; glc CHECK 165 (== vfc). gfc again carries `sync_flag_number` 12 (vs 9). Anchors: vfc `DecodeTcsExternalSyncFlagUpdateDmaDone` @ `0xf606180` (oneof 50, CHECK 165), gfc @ `0xf66d6c0` (oneof 45, CHECK 168).

---

## Throttle / Stall Band (vfc / vlc / gfc)

pxc had **one** throttle event (id 97) with a [discriminated two-variant body](payload-uhi-oci-ici-dma.md#throttle--stall-pxc-id-97) (variant A CHECK 120, variant B CHECK 204). From Viperfish onward the throttle band is **reshaped** into a single TCS-state event plus a family of small cycle-skip events — the discriminated body is gone.

### TCS-state thermal/electrical event (no `TraceIdHeader`)

A single, undiscriminated thermal/electrical state record. Fields (descriptor): `packet_type` (enum), `num_electrical_throttles`, `num_thermal_throttles`, `thermal_total_throttles`, `thermal_max_throttle`, `thermal_min_throttle`.

| gen | `ThrottleTcsState…ThermalAndElectrical` widths | CHECK | note | Confidence |
|---|---|---|---|---|
| vfc | `3,5,5,21,5,5` | 105 | no TIDhdr; packet_type 3 bits | CERTAIN |
| vlc | `3,5,5,21,5,5` | 102 | 58-bit hdr → CHECK 102 | CERTAIN |

The wide electrical/voltage record that pxc's variant B carried is gone; its values migrate to the cycle-skip and LDIDT-voltage events below. Anchors: vfc `DecodeThrottleTcsStateTcsThermalAndElectricalThrottleState` @ `0xf609840` (oneof 68, CHECK 105), vlc @ `0xf5e4660` (oneof 57, CHECK 102).

### Cycle-skip / stall band (each carries `TraceIdHeader{21,3,14}`)

The cycle-skip count **is** the stall: each event records how many clock cycles the engine was throttled by that cause. The wire payload of each is the small `{TraceIdHeader | stall-count}` shape (5 bits typical).

| gen | event | widths (`21,3,14` \| payload) | CHECK | meaning | Confidence |
|---|---|---|---|---|---|
| vfc | `ThrottleCycleSkipThermal` | `21,3,14 \| 5` | 104 | 5-bit cycle-skip count | CERTAIN |
| vfc | `ThrottleCycleSkipExtBrake` | `21,3,14 \| 1` | 100 | 1-bit brake flag | HIGH |
| vfc | `ThrottleCycleSkipArbitration` | `21,3,14 \| 5,3` | 107 | count + arbitration source | HIGH |
| vlc | `ThrottleCycleSkipThermal` | `21,3,14 \| 5` | 101 | 58-bit hdr | HIGH |
| gfc | `ThrottleCycleSkipThermal` | `21,3,14 \| 5` | 104 | | HIGH |
| gfc | `ThrottleCycleSkipPpmSustainedAggr` | `21,3,14 \| 5` | 104 | PPM brake cycle-skip | HIGH |
| gfc | `ThrottleLdidtRunningMeanVoltage` | `21,3,14 \| 7` | 106 | 7-bit voltage sample | HIGH |
| gfc | `ThrottleMaximumTemperature` | `21,3,14 \| 10,5` | 114 | temp(10) + sensor(5) [gfc NEW] | CERTAIN |

The throttle band grows pxc 1 → vfc/vlc 7 → gfc 20 events; gfc adds the max-temperature and running-mean-voltage records. Anchors: vfc `ThrottleCycleSkipThermal` @ `0xf609a60` (oneof 69, CHECK 104), gfc `ThrottleMaximumTemperature` @ `0xf6801a0` (oneof 138, CHECK 114).

---

## gfc-Only Bands — STATS_COUNTER / O2CUR / FLL

gfc (Trillium) adds three bands that exist on no earlier silicon — the on-device observability surface unique to this generation.

### STATS_COUNTER — in-band hardware perf-counter sampling

Fields (descriptor): `extra_id` (bool), `size` (enum SIZE_8/16/32/64BITS), `scaling`, `num_counters`, `sample_id`, `payload_low` (uint64), `payload_high` (uint64).

```text
gfc StatsCounterSampleIssuedFromTcs (oneof 65)
  widths: 1,2,6,4,32,22,1,1,64,42
  CHECK 236 (2 pkts; no TraceIdHeader)
  extra_id(1), size(enum 2 → SIZE_8/16/32/64BITS), scaling(6), num_counters(4),
  sample_id(32), … payload_high/low (uint64s reassembled from the 64+42 fragments)
```

This is the on-device equivalent of PMU sampling: a 2-bit `size` selector plus paired 64-bit counter payloads. Six issuer variants exist (TCS/SCS/SCTD/SCTC/CMNUR/ICR_DATA); only the TCS issuer was decoded here. Anchor: gfc `DecodeStatsCounterSampleIssuedFromTcs` @ `0xf672260` (oneof 65, CHECK 236).

### O2CUR — OCI→memory-controller address-translation DMA

The logical→physical address-translation router's DMA. Fields: `trace_id_header`, `vc_id` (enum), `dst_type` (bool), `dst_id`, `mem_id` (enum), `mem_type`.

```text
gfc O2curL2pRdReq (oneof 119) / WrReqFirst (oneof 117)
  widths: 21,3,14 | 1,1,6,4,4
  CHECK 115 (1 pkt)
```

Anchor: gfc `DecodeO2curL2pRdReq` @ `0xf67d2a0` (oneof 119, CHECK 115).

### FLL — frequency-locked-loop lock / select

Fields: `trace_id_header` + `required_count_value` (uint32).

```text
gfc FllLockFll0Lock (oneof 143)     widths: 21,3,14 | 9    CHECK 108 (1 pkt)
gfc FllSelectFllSelect (oneof 145)  widths: 21,3,14 | 1    CHECK 100 (1 pkt)
```

Anchors: gfc `DecodeFllLockFll0Lock` @ `0xf680da0` (oneof 143, CHECK 108), `DecodeFllSelectFllSelect` @ `0xf681260` (oneof 145, CHECK 100).

---

## Selector Enum Value Tables

The bit *widths* of every selector are decoded above; this section enumerates the integer→name maps. These are **CONFIRMED, not inferred** — every value string below is present byte-exact in the binary's `.rodata` descriptor-pool string tables (the `*Values` nested enums of each family's `trace_entries.proto` FileDescriptorProto; pxc @ `0xbef0d50`, vfc @ `0xbf06830`, vlc @ `0xbf28fd0`, glc @ `0xbf41210`, gfc @ `0xbf64c80`). They resolve the LOW-confidence "exact integer ordering not enumerated" gap the [sibling page](payload-uhi-oci-ici-dma.md#selector-enums-and-reassembly-gaps) left open.

### Core identity — `core_id` (3-bit, 8 values)

The BarnaCore→SparseCore rename is the clearest generation marker in the enum tables:

| value | pxc (BarnaCore gen) | vfc / vlc / glc / gfc (SparseCore gen) |
|---|---|---|
| 0 | `RESERVEDCORESELF` | `RESERVEDCORESELF` |
| 1 | `NONCORE` | `NONCORE` |
| 2 | `TC0` | `TC0` |
| 3 | `TC1` | `TC1` |
| 4 | `BC0` | `SC0` |
| 5 | `BC1` | `SC1` |
| 6 | `BC2` | `SC2` |
| 7 | `BC3` | `SC3` |

This is `TraceIdHeader.core_id` and the OCI-descriptor `Src/DstMemCoreId` / `Src/DstSyncFlag{0,1}CoreId` fields — two TensorCores plus four sparse/Barna cores. Confidence: CERTAIN (`TC0/TC1/SC0..SC3` and `BC0..BC3` strings all in rodata).

### ICI — `router_link_port_id` (3-bit, 6 ports, all gens)

`0=LINK0, 1=LINK1, 2=LINK2, 3=LINK3, 4=LINK4, 5=LINK5`. Confidence: CERTAIN (`LINK0..LINK5` in rodata). `virtual_channel` and `link_targets` are bare uint32 fields, not enums.

### OCI node identity — the SHAPE-C trailing enum (3-bit)

The pxc→newer rename of the node-identity selector:

| | pxc `node_type` (`NodeTypeValues`) | gfc/glc `extra_id` (`ExtraIdValues`) |
|---|---|---|
| 0 | `TCS` | `TCS` |
| 1 | `BC` | `SCS` |
| 2 | `CMQ` | `HDE` |
| 3 | `HBMQ` | `MGR` |
| 4 | `UHI` | `ICR` |
| 5 | `ICR` | `CMNUR` |
| 6 | `QNM` | `CMNDE` |

Confidence: HIGH (the pxc set confirms the sibling page's inferred ordering; the newer set tracks the band renames UHI→HDE, BC→SCS).

### HDE `thread_id` (3-bit, 8 values; vfc/vlc/glc/gfc)

`0=HOST2CHIP_0, 1=HOST2CHIP_1, 2=HOST2CHIP_2, 3=HOST2CHIP_3, 4=CHIP2HOST_0, 5=CHIP2HOST_1, 6=RESERVED0, 7=RESERVED1`. Confidence: CERTAIN (`HOST2CHIP_0..3`, `CHIP2HOST_0..1` in rodata).

### CMN-DMA `thread_id` (vfc, 4-bit, 14 values — the memory-network DMA classes)

| | name | | name |
|---|---|---|---|
| 0 | `TC0VMEM2HBMDEMAND` | 7 | `SC1SPMEM2HBM` |
| 1 | `HBM2TC0VMEMDEMAND` | 8 | `SC2SPMEM2HBM` |
| 2 | `TCXVMEM2HBMEVICT` | 9 | `SC3SPMEM2HBM` |
| 3 | `TC1VMEM2HBMDEMAND` | 10 | `HBM2SC0SPMEM` |
| 4 | `HBM2TC1VMEMDEMAND` | 11 | `HBM2SC1SPMEM` |
| 5 | `HBM2TCXVMEMPREFETCH` | 12 | `HBM2SC2SPMEM` |
| 6 | `SC0SPMEM2HBM` | 13 | `HBM2SC3SPMEM` |

Confidence: CERTAIN (`TC0VMEM2HBMDEMAND`, `HBM2SC0SPMEM` confirmed in rodata).

### `dma_type` (OCI descriptor DMA class)

- pxc (`DmaTypeValues`): `0=LOCAL, 1=CHIP2HOST, 2=REMOTEUNICAST, 3=REMOTEMULTICAST` (4 values).
- vfc/glc/gfc (`DmaTypeValues`): `0=LOCALORHOST, 1=REMOTEUNICAST` (collapsed to 2). Confidence: CERTAIN (`LOCALORHOST`, `REMOTEUNICAST`, `REMOTEMULTICAST` in rodata).

### Opcode / mem-id selectors (OCI descriptor & CMN-DMA)

| Enum | Values |
|---|---|
| OCI `SrcOpcode` (pxc/vfc) | `0=READ, 1=RESERVED, 2=INSTRUCTIONMEMSET, 3=DATAMEMSET` |
| OCI `DstOpcode` (pxc) | `0=WRITE, 1=RESERVED, 2=WRITESPECIAL0, 3=WRITESPECIAL1` |
| CMN-DMA `SrcOpcode` (vfc) | `0=READ, 1=SRCRESERVED, 2=INTMEMSET, 3=DATAMEMSET` |
| CMN-DMA `DstOpcode` (vfc) | `0=WRITE, 1=WRITE4B, 2=WRITESPECIAL0, 3=WRITESPECIAL1` |
| sync `Opcode` (2-bit, done semantics) | `0=WRITE_NO_DONE, 1=WRITE_WITH_DONE, 2=INC_NO_DONE, 3=INC_WITH_DONE` |
| CMN-DMA `Src/DstMemId` (vfc, 3-bit) | `0=TC0VMEM, 1=TC1VMEM, 2=SC0SPMEM, 3=SC1SPMEM, 4=SC2SPMEM, 5=SC3SPMEM, 6=HBM, 7=TCAVMEM` |

OCI descriptor `Src/DstMemMemId` is a polymorphic 2-bit (4-value) mem-class whose meaning depends on the issuing core; pxc and vfc give it different name triples (e.g. vfc `0=HBM_TCVMEM_SCSPMEM`, `1=HOST_TCSMEM_SCSMEM`, …). Confidence: CERTAIN for the sync `Opcode` and CMN opcodes (`WRITE_WITH_DONE`, `INC_WITH_DONE`, `INTMEMSET`, `WRITESPECIAL0` in rodata); HIGH for the polymorphic mem-class ordering.

### Throttle / power selectors

| Enum | Values |
|---|---|
| pxc `PacketType` (bitmask) | `1=ELECTRICAL_THROTTLE, 2=THERMAL_THROTTLE, 4=THERMAL_SENSOR, 8=THROTTLING_STATISTICS` |
| vfc `PacketType` (3-bit) | `1=ELECTRICAL_THROTTLE, 2=THERMAL_THROTTLE, 4=THROTTLING_STATISTICS` |
| glc/gfc `FinalThrottleSource` (3-bit) | `0=THERMAL, 1=EXTERNAL_BRAKE, 2=EXTERNAL_THROTTLE, 3=ELECTRICAL_LDIDT_BRAKE, 4=ELECTRICAL_LDIDT_DROOP, 5=SOFTWARE, 6=PPM_AGGRESSIVE_BRAKE, 7=PPM_NOMINAL_BRAKE` |
| glc/gfc `PowerRail` (SPI sampler) | `0=UNSPECIFIED, 1=VDD_CORE, 2=HBM` |

The pxc `packet_type & 1` test (the `ELECTRICAL` bit) is what selected the pxc discriminated two-variant body; the newer families replace it with the named `FinalThrottleSource` cause. Confidence: CERTAIN (`EXTERNAL_BRAKE`, `ELECTRICAL_LDIDT_BRAKE`, `PPM_AGGRESSIVE_BRAKE` in rodata).

### Sync / SparseCore-stream selectors (glc/gfc)

| Enum | Values |
|---|---|
| `SyncMode` | `0=SYNCMODECOUNTWORDS, 1=SYNCMODECOUNTDONE` |
| `SyncFlagCoreType` / `DestCoreType` | `0=TEC_OR_SCS, 1=TAC` |
| `CoreType` | `0=UNSPECIFIED, 1=TENSOR_CORE, 2=SPARSE_CORE, 3=UNCORE` |
| `RelaxedOrdering` | `0=STRICTCROSSDMAORDERING, 1=RELAXEDORDERING` |
| `StreamOpcode` (SC stream) | `0=GATHER, 1=GATHERADDS32, 2=GATHERADDF32, 4=SCATTER, 5=SCATTERADDS32, 6=SCATTERADDF32, 7=RESERVED` |

Confidence: CERTAIN (`SYNCMODECOUNTWORDS`/`SYNCMODECOUNTDONE`, `TEC_OR_SCS`, `GATHERADDF32`, `SCATTERADDS32` in rodata). The `SyncFlagCoreType` `{TEC_OR_SCS, TAC}` pair is the same SparseCore sequencer-type taxonomy the [SparseCore band page](payload-sc-band.md) decodes.

### gfc-only new-band selectors

| Enum | Values |
|---|---|
| StatsCounter `Size` (2-bit) | `0=SIZE_8BITS, 1=SIZE_16BITS, 2=SIZE_32BITS, 3=SIZE_64BITS` |
| CMN-DMA `CmnRouterType` | `0=CMNUR, 1=O2CUR` |

`CmnRouterType` selects the CMNUR router versus the **O2CUR** logical→physical address-translation router on the gfc unified memory-network DMA. Confidence: CERTAIN (`SIZE_64BITS`, `CMNUR`, `O2CUR` in rodata).

---

## Decoder Evidence Anchors

| Event / group | gen | Decoder | Address | CHECK | Confidence |
|---|---|---|---|---|---|
| trace-header (58-bit anomaly) | vlc | `DecodeTraceHeader` | `0xf5f5b40` | 8/3/45 | CERTAIN |
| HDE host request read | vfc | `DecodeHdeHostRequestRead` | `0xf5fbde0` | 178 | CERTAIN |
| HDE host response read | vfc | `DecodeHdeHostResponseRead` | `0xf5fc1a0` | 112 | CERTAIN |
| HDE host request write | vlc | `DecodeHdeHostRequestWrite` | `0xf5d9580` | 175 | CERTAIN |
| HDE host request write | gfc | `DecodeHdeHostRequestWrite` | `0xf662780` | 179 | CERTAIN |
| OCI read cmd (SHAPE-C) | vfc | `DecodeOciCommonReadCmdIssuedFromEngine` | `0xf5fd560` | 234 | CERTAIN |
| OCI message sent by HDE | vfc | `DecodeOciMessageSentByHde` | `0xf5fcf20` | — | HIGH |
| ICI link packet (vc=2) | vfc | `DecodeIciPacketPacketReceivedOnLinkInput` | `0xf5ff460` | 128 | CERTAIN |
| ICI link packet (vc=3) | vlc | `DecodeIciPacketPacketReceivedOnLinkInput` | `0xf5dd340` | 126 | CERTAIN |
| ICI link packet | gfc | `DecodeIciPacketPacketReceivedOnLinkInput` | `0xf667c60` | 128 | CERTAIN |
| CMN-DMA east-side lane0 | vfc | `DecodeCmnDmaRequestEastSideLane0` | `0xf603a80` | 206 | CERTAIN |
| CMN-DMA set0 lane0 | gfc | `DecodeCmnDmaRequestSet0Lane0` | `0xf66c5c0` | 196 | CERTAIN |
| VDQ read-req chan0 | vlc | `DecodeVdqTransactionReadReqChan0` | `0xf5e56e0` | 115 | CERTAIN |
| TCS set sync flag (no lcc) | vfc | `DecodeTcsInternalSetSyncFlag` | `0xf6065e0` | 121 | CERTAIN |
| TCS set sync flag (no lcc) | vlc | `DecodeTcsInternalSetSyncFlag` | `0xf5e1460` | 118 | CERTAIN |
| TCS set sync flag (+lcc) | gfc | `DecodeTcsInternalSetSyncFlag` | `0xf66db20` | 190 | CERTAIN |
| TCS core interrupt | vfc | `DecodeTcsInternalCoreInterrupt` | `0xf606a20` | 121 | CERTAIN |
| TCS external sync done | vfc | `DecodeTcsExternalSyncFlagUpdateDmaDone` | `0xf606180` | 165 | CERTAIN |
| TCS external sync done | gfc | `DecodeTcsExternalSyncFlagUpdateDmaDone` | `0xf66d6c0` | 168 | CERTAIN |
| throttle TCS state | vfc | `DecodeThrottleTcsState…ThermalAndElectrical…` | `0xf609840` | 105 | CERTAIN |
| throttle TCS state | vlc | `DecodeThrottleTcsState…ThermalAndElectrical…` | `0xf5e4660` | 102 | CERTAIN |
| throttle cycle-skip thermal | vfc | `DecodeThrottleCycleSkipThermal` | `0xf609a60` | 104 | CERTAIN |
| throttle max temperature | gfc | `DecodeThrottleMaximumTemperature` | `0xf6801a0` | 114 | CERTAIN |
| stats-counter sample (TCS) | gfc | `DecodeStatsCounterSampleIssuedFromTcs` | `0xf672260` | 236 | CERTAIN |
| O2CUR L2P read req | gfc | `DecodeO2curL2pRdReq` | `0xf67d2a0` | 115 | CERTAIN |
| FLL lock | gfc | `DecodeFllLockFll0Lock` | `0xf680da0` | 108 | CERTAIN |
| FLL select | gfc | `DecodeFllSelectFllSelect` | `0xf681260` | 100 | CERTAIN |
| bit-codec primitive | all | `BitDecoder::GetBits64NoInline` | `0x21073760` | — | CERTAIN |
| width mask table | all | `mask_` | `0xbe79440` | — | CERTAIN |

The `GetBits64` width sequence and `CHECK` of every CERTAIN row above were read byte-exact from the decoder's decompiled C (the `BitDecoder::GetBits64NoInline(…, WIDTH, …)` immediates and the `MakeCheckOpString<…>(consumed, CHECK, "decoder.BitsDecoded() == CHECK")` guard). The selector enum value strings were confirmed present in the binary's `.rodata`. The descriptor-pool FDP entries (vfc @ `0xbf06830`, vlc @ `0xbf28fd0`, glc @ `0xbf41210`, gfc @ `0xbf64c80`) supply the field labels and the `*Values` enum names.

---

## Open Items

Three classes of detail remain LOW confidence or untraced, all mechanically recoverable with the same parser:

1. **uint64 fragment reassembly order.** The split fields here (HDE `address` 26+1+1+33, CMN `src_operand`/`dst_addr`, glc/gfc sync `lcc` 57/60, gfc StatsCounter `payload_high/low` 64+42) have CERTAIN widths and destination slots, but the precise per-fragment shift/OR bit-position inside the reassembled uint64 — and whether a fragment straddles the 128-bit packet boundary — was not tabulated (the same gap the [sibling page](payload-uhi-oci-ici-dma.md#selector-enums-and-reassembly-gaps) leaves). A byte-exact re-encoder needs it.
2. **Full per-gen on-wire-id → oneof arm map.** The on-wire `trace_point_id` space (dense 0..0x5f vfc / 0..0x8f vlc / 0..0x64 gfc) is confirmed distinct from the proto `TracePointId` enum value space, and one arm was walked (vfc id 14 → `DecodeOciMessageSentByHde`), but the complete arm→`Decode<Name>` table per gen (jump table @ `0xab86ce8` for vfc, the vlc/glc/gfc equivalents) was not exhaustively dumped.
3. **vlc 58-bit header — HW vs codec.** The `DecodeTraceHeader` esi-widths are byte-exact (`8/3/45`), but whether Viperfish-lite silicon emits a 45-bit timestamp or the codec just reads 45 of a wider field was not determined from the codec alone. The decode behavior is CERTAIN; the hardware/software-boundary attribution is open.

The non-named bands not decoded here — the SparseCore `SC_*` family on vfc/glc/gfc, the manager-FW (MGR), and the CMNUR/CMNUCB/CMNDE memory-controller hierarchy — are owned by or referenced from the [SparseCore band page](payload-sc-band.md) and remain the largest residual.

---

## Related Components

| Component | Relationship |
|---|---|
| [TraceEntriesCoder](trace-entries-coder.md) | owns the 16-byte packet, framing prefix, `TraceHeader`/`TraceIdHeader`, dual dispatch, and the per-event `CHECK` mechanism; this page corrects its per-gen vlc header to 58 bits |
| [Trace Payload: UHI / OCI / ICI / DMA](payload-uhi-oci-ici-dma.md) | the pxc baseline + glc representative this page deltas against; owns the band concepts and the shape-A/B/C structure |
| [Payload: SparseCore Band](payload-sc-band.md) | the `SC_*` band payload field maps; shares the `TEC_OR_SCS`/`TAC` sequencer-type and `StreamOpcode` selectors enumerated here |
| [Payload: jxc Legacy](payload-jxc-legacy.md) | the separate `PerformanceTraceEntry` schema and its own codec — outside the `Decode<Name>` family this page decodes |
| [TracePoints Master Registry](tracepoints-master-registry.md) | owns the `trace_point_id` ↔ oneof-field id spaces; this page is the per-gen payload appendix to those names |

## Cross-References

- [Profiling and Telemetry Overview](overview.md) — the capture→encode→decode→xplane pipeline; these per-gen payloads are the device-event content of the decode stage
- [TraceEntriesCoder](trace-entries-coder.md) — read first: the universal frame every payload here extends; the vlc 58-bit-header correction re-bases its per-gen table
- [Trace Payload: UHI / OCI / ICI / DMA](payload-uhi-oci-ici-dma.md) — the pxc/glc baseline this page completes for vfc/vlc/gfc; the shape-A/B/C and band definitions live there
- [Payload: SparseCore Band](payload-sc-band.md) — the SparseCore `SC_*` band, the largest residual non-named band on the newer gens
- [Payload: jxc Legacy](payload-jxc-legacy.md) — the legacy `PerformanceTraceEntry` codec, decoded separately from the per-gen `Decode<Name>` family
- [TracePoints Master Registry](tracepoints-master-registry.md) — the wire-id / oneof-field id spaces; this page decodes the payloads those per-gen ids name
