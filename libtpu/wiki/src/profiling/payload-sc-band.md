# Payload: SparseCore Band

> *All addresses, widths, and `CHECK` constants on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — full C++ symbols are present, and `.text` VMA equals file offset. Other versions will differ.*

## Abstract

The **SparseCore (SC) trace band** is the on-device profiler's only view of SparseCore execution progress: the per-event payload field maps for the 18 hardware trace events emitted by the SparseCore sequencer (SCS), the TensorE/TensorA compute tiles (TEC/TAC), and the SC stream gather/scatter engine. SparseCore is the sparse-compute substrate that replaced BarnaCore on the Viperfish/Ghostlite/6acc60406 line; its instructions, tasks, streams, and inter-tile messages each pack into the same [fixed 16-byte profiler packet](trace-entries-coder.md) as every other band, behind the same 61-bit framing+header envelope, and are read out by an anonymous-namespace `DecodeSc<Name>(string_view, bool*, TraceEntry*)` per event per chip family.

This page owns two things the [codec page](trace-entries-coder.md) deliberately does not: (1) the **per-event payload bit-decode** — the ordered `GetBits64` width sequence after bit 61, the total-bit `CHECK`, the packet count, and the named proto fields with per-field widths — byte-exact for the three SC-bearing families **vfc** (Viperfish), **glc** (Ghostlite), **gfc** (6acc60406); and (2) the **on-wire-id → trace-point map** — the dense `trace_point_id` (108..133) the hardware stamps for each SC event, which is *distinct* from the proto oneof field number the decoder writes. The SC band lives in the *high* id band reached only through the newer codec's two-level dispatch; [`tracepoints-master-registry.md`](tracepoints-master-registry.md) owns the full cross-band registry, this page owns the SC sub-range.

A structural fact frames everything: the SC trace band **exists only on SparseCore-bearing silicon**. pxc (Pufferfish, BarnaCore generation) and vlc (Viperfish-lite) emit zero `Sc*` proto messages and have zero `DecodeSc*` functions. The events instrument the [SCS sequencer](../sparsecore/scs-engine.md), the [TEC](../sparsecore/tec-engine.md)/[TAC](../sparsecore/tac-engine.md) tiles, and the [stream gather/scatter](../sparsecore/stream-gather-scatter.md) datapath; the SyncFlagCoreType/DestCoreType `TEC_OR_SCS`/`TAC` selectors are the [sequencer-type enum](../sparsecore/getsequencertype.md) surfaced on the wire.

For reimplementation, the contract is:

- **The 18 SC events and their wire shapes** — 11 `ScInstruction*`, 2 `ScTask*`, 3 `ScStream*`, 2 `ScMessage*` — each as an ordered post-bit-61 width sequence + total-bit `CHECK` + packet count, per family.
- **The named proto field maps** — which width is `scs_pc`, `tile_bitmap`, `tec_sync_stalls`, `stream_opcode`, `smem_address`, etc., descriptor-confirmed.
- **The SC selector enum value tables** — `StreamOpcode`, `SyncFlagCoreType`, `TileLocal/OffTile MemoryType`/`StreamType`, `IndirectListType`, `DestCoreType`, `MsgType`, `Opcode` — byte-exact named values, including the per-gen `StreamOpcode` 3→4-bit growth.
- **The on-wire-id → event map** — the dense SC sub-range (108..133), proven distinct from the proto oneof field, recovered from the high-id-band secondary jump table.

| | |
|---|---|
| **Owns** | SC-band per-event payload field-bit maps + the SC on-wire-id table |
| **Codec / framing** | [`trace-entries-coder.md`](trace-entries-coder.md) — 16-byte packet, 2-bit framing, 61-bit header, `GetBits64`/`SkipBits` primitives, `CHECK` mechanism |
| **SC-bearing families** | vfc (Viperfish), glc (Ghostlite), gfc (6acc60406) — 18 `DecodeSc*` each |
| **No SC band** | pxc (Pufferfish/BarnaCore), vlc (Viperfish-lite) — 0 `DecodeSc*`, 0 `Sc*` proto messages |
| **SC event count** | 18 — `11 ScInstruction* + 2 ScTask* + 3 ScStream* + 2 ScMessage*` |
| **SC on-wire id range** | 108..132 (vfc/glc), 108..133 (gfc — a StatsCounter sample shifts ScMessage up by 1) |
| **vfc decoders** | `DecodeScInstructionCoreInterrupt` @ `0xf60a8c0` … `DecodeScMessageInboundInternalMessage` @ `0xf60eb00` |
| **glc decoders** | `0xf63c760` … `0xf6409a0` |
| **gfc decoders** | `0xf6725a0` … `0xf676ae0` |
| **Descriptor pool** | `trace_entries.proto` FileDescriptorProto: vfc @ `0xbf06830`, glc @ `0xbf41210`, gfc @ `0xbf64c80` (named fields + nested `*Values` enums) |
| **Bit primitives** | `GetBits64NoInline` @ `0x21073760`, `SkipBitsNoInline` @ `0x21073580`, `mask_` @ `0xbe79440` |

---

## Reading a SC Payload — the Common Shape

Every SC decoder follows the [generic `Decode<Name>` contract](trace-entries-coder.md#the-per-event-decode-contract-and-the-total-bit-check): gate on `length > 15`, `SkipBits(2)` past the framing, run the per-family `DecodeTraceHeader` (the 59-bit `id/block_id/timestamp`, so framing+header = **61 bits**), optionally read a `TraceIdHeader`, stamp the dense oneof field at `TraceEntry+0x28`, then read the typed payload with one `GetBits64(n)` per field, and finally `CHECK(BitsDecoded() == K)`. The width sequences below are the **payload** bits — what is read *after* bit 61 (and after the `TraceIdHeader` when one is present). The total-bit `CHECK` is the whole packet: `CHECK = 61 + [TraceIdHeader] + Σ payload widths`.

```text
 bit window of an SC packet
 ┌──────────┬───────────────────────────────┬──────────────────┬───────────────────────┐
 │ framing  │  TraceHeader (id/block/ts)     │ TraceIdHeader?   │ SC payload (this page)│
 │ 2 bits   │  59 bits  →  bit 61            │ 38 bits (msg)    │ Σ widths              │
 └──────────┴───────────────────────────────┴──────────────────┴───────────────────────┘
   valid,started                                 only ScMessage*       per-event field set
```

> **NOTE —** the SC `TraceIdHeader` is `21,3,14` (= 38 bits), **not** the `21,3,12` (= 36 bits) of the UHI/OCI/ICI bands — the `chip_id` field is 14 bits here, matching the newer-gen 14-bit `chip_id` of the vfc/glc/gfc header. Only the two `ScMessage*` events carry it; the `ScInstruction*`/`ScTask*`/`ScStream*` events carry **no** `TraceIdHeader` (the core is implied by `trace_point_block_id` in the 59-bit header). Byte-confirmed: `DecodeScMessageOutboundInternalMessage` @ `0xf60e6c0` opens with `GetBits64(21)`, `GetBits64(3)`, `GetBits64(14)`.

Two SC events (`ScTaskCommitOnSct`, `ScMessage*`) exceed one 16-byte packet and are **2-packet** events. They carry a *mid-stream* `CHECK == 128` at the packet-1 boundary (`movq $0x20,0x8(%rbx)` records 32 bytes consumed) in addition to the final total-bit `CHECK`. The fields whose bits straddle that 128-bit boundary are split across two `GetBits64` calls — the `7,1,1,9` group in `ScTaskCommitOnSct` and the `4,1,1,10` group in `ScMessage*` (see the per-event notes).

> **GOTCHA —** the two single-bit `GetBits64(1)` calls inside those straddle groups (`{7, 1, 1, 9}` and `{4, 1, 1, 10}`) are *not* boolean proto fields — they are the two halves of the packet-boundary straddle of a single multi-bit field (`tec_sync_stalls` 7+9, `smem_address` 4+10). A reimplementation that maps each `GetBits64` one-to-one onto a proto field will invent two phantom bools and split one real counter. The width fragments and the `+0xNN` destination offset are confirmed; the exact LSB position each fragment occupies in the reassembled value is **not** tabulated (LOW — same open item as the UHI/OCI straddle fields).

---

## SC_INSTRUCTION_* Band — the Sequencer Control Events (11 events)

### Purpose

The SparseCore sequencer's internal control band — the SC analogue of the TensorCore `TcsInternal*` band. These 11 events fire when an SC sequencer primitive executes: core interrupt, trace marking, and the fence/sync/barrier/sync-watch start/stop pairs. They carry **no** `TraceIdHeader` and **all 11 share one wire shape** per gen — they differ only by *which* primitive fired (i.e. only by `trace_point_id` and oneof field), not by payload layout.

### Algorithm

```c
// DecodeScInstructionCoreInterrupt @0xf60a8c0 (vfc) — representative of all 11
function DecodeScInstruction<Prim>(view, started_out, entry):
    if view.length <= 0xf: return error
    BitDecoder dec(view); SkipBits(dec, 2);          // framing
    DecodeTraceHeader(entry, dec);                    // id/block/timestamp → bit 61
    entry[+0x28] = <oneof field>;                     // vfc 75..85 / glc 67..77 / gfc 66..76
    GetBits64(dec, 32, &data);                        // data      (uint32)
    GetBits64(dec,  1, &done);                        // done      (bool)
    GetBits64(dec,  6, &extra_id);                    // extra_id  (uint32, 6-bit field)
    GetBits64(dec, 13, &index);                       // index     (uint32, 13-bit field)
    GetBits64(dec, 14, &pc);                          // pc        (uint32, 14-bit field)
    CHECK(BitsDecoded() == 127);                       // movl $0x4b,0x28 stamps oneof 75
    *bytes_consumed = 0x10;
```

### Field Map

Proto fields (descriptor-confirmed, identical for all 11 events and all 3 gens): `data(uint32)`, `done(bool)`, `extra_id(uint32)`, `index(uint32)`, `pc(uint32)`.

| Field | Width | Meaning | Confidence |
|---|---|---|---|
| `data` | 32 | sequencer-primitive operand / payload word (e.g. tracemark value, interrupt cause) | HIGH |
| `done` | 1 | primitive-complete flag | HIGH |
| `extra_id` | 6 | per-event correlation tag (the SC band's local "sub-id") | HIGH |
| `index` | 13 | sequencer slot / sync-flag index the primitive targets | MEDIUM |
| `pc` | 14 | SCS program counter at issue | HIGH |

### Per-Gen Width Table

| Gen | payload widths (no `TraceIdHeader`) | `CHECK` | pkts | oneof base | Confidence |
|---|---|---|---|---|---|
| vfc | `32,1,6,13,14` | 127 | 1 | 75 | CERTAIN |
| glc | `32,1,6,13,14` | 127 | 1 | 67 | CERTAIN |
| gfc | `32,1,6,13,14` | 127 | 1 | 66 | CERTAIN |

66 payload bits + 61 frame+header = 127. **Byte-identical across all three SC gens** — only the oneof base differs (the wire shape and `CHECK` are invariant). The 11 primitives, in oneof / on-wire-id order: `CoreInterrupt, SetTracemark, TraceInstruction, SfenceStart, SfenceStop, SyncStart, SyncStop, BarrierStart, BarrierStop, SyncWatchStart, SyncWatchStop`.

> **CONFIRMED (SC-1) —** `DecodeScInstructionCoreInterrupt` @ `0xf60a8c0` decompiles to exactly `SkipBits(2); DecodeTraceHeader; GetBits64(32); GetBits64(1); GetBits64(6); GetBits64(13); GetBits64(14); CHECK(==127)`, with `movl $0x4b,0x28` (= oneof field 75). All 11 share this shape (`0xf60a8c0`..`0xf60bcc0`, oneof 75..85 on vfc).

---

## SC_TASK_* Band — Issue and Commit (2 events)

### Purpose

The SparseCore task lifecycle: a task is **issued** from the SCS (the [SparseCore scalar sequencer](../sparsecore/scs-engine.md)) and later **committed** on the SCT (the SparseCore tile) with rich per-engine stall accounting. `ScTaskCommitOnSct` is the dominant SC progress record — gather/scatter/sort/dedup throughput is read out through its stall and word counters. Neither carries a `TraceIdHeader`.

### ScTaskIssueFromScs — the dispatch record

```c
// DecodeScTaskIssueFromScs @0xf60bec0 (vfc)
function DecodeScTaskIssueFromScs(view, started_out, entry):
    ... SkipBits(2); DecodeTraceHeader; entry[+0x28] = 86; ...   // glc 78 / gfc 77
    GetBits64(dec, 13, &scs_pc);       // SCS program counter
    GetBits64(dec,  8, &tag);          // task tag
    GetBits64(dec, 14, &tec_pc);       // TensorE-Compute engine PC
    GetBits64(dec, 14, &tac_pc);       // TensorA-Compute engine PC
    GetBits64(dec, 16, &tile_bitmap);  // SMEM-bitmap: which tiles the task spans
    CHECK(BitsDecoded() == 126);
```

| Field | Width | Meaning | Confidence |
|---|---|---|---|
| `scs_pc` | 13 | SCS program counter at task issue | HIGH |
| `tag` | 8 | task correlation tag (matched against `ScTaskCommitOnSct.tag`) | HIGH |
| `tec_pc` | 14 | [TEC](../sparsecore/tec-engine.md) program counter | HIGH |
| `tac_pc` | 14 | [TAC](../sparsecore/tac-engine.md) program counter | HIGH |
| `tile_bitmap` | 16 | SMEM-bitmap iteration field — the set of tiles the task spans | HIGH |

| Gen | widths | `CHECK` | pkts | oneof | Confidence |
|---|---|---|---|---|---|
| vfc | `13,8,14,14,16` | 126 | 1 | 86 | CERTAIN |
| glc | `13,8,14,14,16` | 126 | 1 | 78 | CERTAIN |
| gfc | `13,8,14,14,16` | 126 | 1 | 77 | CERTAIN |

65 payload bits + 61 = 126. Byte-identical across gens.

### ScTaskCommitOnSct — the progress record (RESTRUCTURES on gfc)

A 2-packet event with a mid-stream packet-1 boundary `CHECK == 128`. The shape **restructures on gfc (6acc60406)**: the separate TEC/TAC stall accounting is collapsed and a load-store-unit hold-stall counter is added.

```c
// DecodeScTaskCommitOnSct @0xf60c0c0 (vfc) — 2-packet, mid CHECK 128, final CHECK 251
function DecodeScTaskCommitOnSct(view, started_out, entry):
    ... SkipBits(2); DecodeTraceHeader; entry[+0x28] = 87; ...   // glc 79 / gfc 78
    GetBits64(dec,  8, &tag);             // task tag
    GetBits64(dec,  4, &extra_id);        // correlation sub-id
    GetBits64(dec, 32, &total_cycles);    // task wall cycles
    GetBits64(dec, 16, &tec_ibuf_stalls);
    GetBits64(dec,  7, &tec_sync_stalls_lo);   // ── packet-boundary straddle ──
    CHECK(BitsDecoded() == 128);          // packet-1 boundary; movq $0x20 (32 B consumed)
    GetBits64(dec,  1, &straddle_a);      // (high half of tec_sync_stalls)
    GetBits64(dec,  1, &straddle_b);      //  └ the 7+9 split spans the 16-byte boundary
    GetBits64(dec,  9, &tec_sync_stalls_hi);
    GetBits64(dec, 16, &tec_hold_stalls);
    GetBits64(dec, 16, &tac_ibuf_stalls);      // vfc/glc only
    GetBits64(dec, 16, &tac_sync_stalls);      // vfc/glc only
    GetBits64(dec, 16, &tac_hold_stalls);      // vfc/glc only
    GetBits64(dec, 16, &num_spmem_words);
    GetBits64(dec, 32, &num_hbm_words);
    CHECK(BitsDecoded() == 251);
```

| Field (vfc/glc) | Width | Meaning | Confidence |
|---|---|---|---|
| `tag` | 8 | task tag (matches `ScTaskIssueFromScs.tag`) | HIGH |
| `extra_id` | 4 | correlation sub-id | HIGH |
| `total_cycles` | 32 | task duration in device cycles | HIGH |
| `tec_ibuf_stalls` | 16 | TEC instruction-buffer stall cycles | HIGH |
| `tec_sync_stalls` | 7+9 (straddle) | TEC sync-flag wait cycles (spans the packet boundary) | HIGH |
| `tec_hold_stalls` | 16 | TEC hold/backpressure stall cycles | HIGH |
| `tac_ibuf_stalls` | 16 | TAC instruction-buffer stall cycles | HIGH |
| `tac_sync_stalls` | 16 | TAC sync-flag wait cycles | HIGH |
| `tac_hold_stalls` | 16 | TAC hold/backpressure stall cycles | HIGH |
| `num_spmem_words` | 16 | SPMEM word throughput of the task | HIGH |
| `num_hbm_words` | 32 | HBM word throughput of the task | HIGH |

| Gen | widths | mid `CHECK` | final `CHECK` | pkts | proto fields | Confidence |
|---|---|---|---|---|---|---|
| vfc | `8,4,32,16,7,1,1,9,16,16,16,16,16,32` | 128 | 251 | 2 | 11 (TEC+TAC triples) | CERTAIN |
| glc | `8,4,32,16,7,1,1,9,16,16,16,16,16,32` | 128 | 251 | 2 | 11 (identical) | CERTAIN |
| gfc | `8,4,32,16,7,1,1,9,16,16,32,16` | 128 | 219 | 2 | 9 (TAC dropped, `lsu_hold_stalls` added) | CERTAIN |

vfc/glc payload = 190 bits (251−61); gfc payload = 158 bits (219−61).

> **CONFIRMED (SC-2) —** the gfc restructure is byte-exact. After the `7,1,1,9` straddle of `tec_sync_stalls`, gfc reads only `16,16,32,16` = `tec_hold_stalls(16)`, `num_spmem_words(16)`, `num_hbm_words(32)`, `lsu_hold_stalls(16)` — the three TAC counters (`tac_ibuf/sync/hold_stalls`) are **gone**, replaced by a single `lsu_hold_stalls(16)`. Read from `DecodeScTaskCommitOnSct` @ `0xf673da0` (gfc): mid `CHECK == 128`, final `CHECK == 219`. Whether this is a true 6acc60406 (v7x) microarch change (a unified SC tile vs separate TEC/TAC) or a profiler schema rev was not cross-checked against the SC ISA (LOW on the *cause*; the wire shape is CERTAIN).

---

## SC_STREAM_* Band — Gather/Scatter and Progress (3 events)

### Purpose

The SparseCore [stream gather/scatter engine](../sparsecore/stream-gather-scatter.md) band. The stream **issue** event carries the `StreamOpcode` (GATHER/SCATTER family) selector plus the tile-local/off-tile memory and stream-type selectors — it is the on-wire image of an SC stream dispatch. The two **progress** events (`Xbar` = crossbar lane, `Cmn` = memory-network lane) carry the per-progress sync-flag bump. None carry a `TraceIdHeader`.

### ScStreamIssueFromCore — the gather/scatter dispatch (WIDENS on glc/gfc)

13 proto fields, 13 widths (one per field). The `stream_opcode` width grows 3→4 bits on the newer gens because the `StreamOpcode` enum gains the half-width-accumulate variants.

```c
// DecodeScStreamIssueFromCore @0xf60c460 (vfc)
function DecodeScStreamIssueFromCore(view, started_out, entry):
    ... SkipBits(2); DecodeTraceHeader; entry[+0x28] = 88; ...   // glc 80 / gfc 79
    GetBits64(dec, 14, &pc);
    GetBits64(dec,  6, &extra_id);
    GetBits64(dec,  5, &sync_flag_id);
    GetBits64(dec,  1, &sync_flag_core_type);    // enum SyncFlagCoreType
    GetBits64(dec,  3, &stream_opcode);          // enum StreamOpcode (vfc 3-bit; glc/gfc 4-bit)
    GetBits64(dec,  1, &tile_local_memory_type); // enum
    GetBits64(dec,  3, &off_tile_memory_type);   // enum
    GetBits64(dec,  1, &tile_local_stream_type); // enum
    GetBits64(dec,  2, &off_tile_stream_type);   // enum
    GetBits64(dec,  1, &set_done_bit);           // bool
    GetBits64(dec,  1, &sync_flag_count_type);   // bool
    GetBits64(dec,  1, &indirect_list_type);     // enum
    GetBits64(dec, 18, &length_in_4B);           // uint32 (glc re-budgets to 17)
    CHECK(BitsDecoded() == 118);
```

| Field | vfc width | Meaning | Confidence |
|---|---|---|---|
| `pc` | 14 | SC core PC at stream issue | HIGH |
| `extra_id` | 6 | correlation sub-id | HIGH |
| `sync_flag_id` | 5 | sync-flag the stream signals on completion | HIGH |
| `sync_flag_core_type` | 1 | target core class — `TEC_OR_SCS`/`TAC` enum | HIGH |
| `stream_opcode` | 3 (vfc) / 4 (glc,gfc) | GATHER/SCATTER family — see enum table | CERTAIN |
| `tile_local_memory_type` | 1 | `SMEM`/`TILESPMEM` enum | HIGH |
| `off_tile_memory_type` | 3 | `SPMEM`/`TILESPMEMN`/`HBM`/`HBM4B` enum | HIGH |
| `tile_local_stream_type` | 1 | `LINEAR`/`CIRCULARBUFFER` enum | HIGH |
| `off_tile_stream_type` | 2 | `LINEAR`/`STRIDED`/`INDIRECT`/`INDIRECTVREG` enum | HIGH |
| `set_done_bit` | 1 | set the sync flag's done bit on completion | HIGH |
| `sync_flag_count_type` | 1 | count-mode of the sync-flag bump | MEDIUM |
| `indirect_list_type` | 1 | `WORD`/`ROW` indirection granularity enum | HIGH |
| `length_in_4B` | 18 (vfc,gfc) / 17 (glc) | stream length in 4-byte words | HIGH |

| Gen | widths | `CHECK` | pkts | `stream_opcode` | `length_in_4B` | Confidence |
|---|---|---|---|---|---|---|
| vfc | `14,6,5,1,3,1,3,1,2,1,1,1,18` | 118 | 1 | 3-bit (8 values) | 18 | CERTAIN |
| glc | `14,6,5,1,4,1,3,1,2,1,1,1,17` | 118 | 1 | 4-bit (11 values) | 17 | CERTAIN |
| gfc | `14,6,5,1,4,1,3,1,2,1,1,1,18` | 119 | 1 | 4-bit (11 values) | 18 | CERTAIN |

> **CONFIRMED (SC-3) —** the per-gen drift is byte-exact. glc/gfc read `GetBits64(4)` for `stream_opcode` where vfc reads `GetBits64(3)` (verified at `DecodeScStreamIssueFromCore` @ `0xf63e300` for glc). glc absorbs the extra bit by narrowing `length_in_4B` 18→17 (keeping `CHECK == 118`); gfc keeps `length_in_4B == 18` and lets the total grow to `CHECK == 119`. The enum growth that forces the widening is in the [enum tables below](#the-sparsecore-selector-enum-value-tables).

### ScStreamProgressXbar / ScStreamProgressCmn — the lane-progress events

Byte-identical wire shape; the **only** difference is the lane class — `Xbar` is the crossbar progress lane, `Cmn` is the memory-network progress lane.

```c
// DecodeScStreamProgressXbar @0xf60c740 (vfc); DecodeScStreamProgressCmn @0xf60c940 identical shape
function DecodeScStreamProgress<Lane>(view, started_out, entry):
    ... SkipBits(2); DecodeTraceHeader; entry[+0x28] = 89/90; ...
    GetBits64(dec, 6, &extra_id);
    GetBits64(dec, 5, &sync_flag_id);
    GetBits64(dec, 1, &sync_flag_core_type);   // enum TEC_OR_SCS/TAC
    GetBits64(dec, 32, &data);
    GetBits64(dec, 1, &done);
    CHECK(BitsDecoded() == 106);
```

| Field | Width | Meaning | Confidence |
|---|---|---|---|
| `extra_id` | 6 | correlation sub-id | HIGH |
| `sync_flag_id` | 5 | the sync flag this progress event bumps | HIGH |
| `sync_flag_core_type` | 1 | `TEC_OR_SCS`/`TAC` enum | HIGH |
| `data` | 32 | progress count / lane payload word | HIGH |
| `done` | 1 | lane-drained flag | HIGH |

| Gen | widths | `CHECK` | pkts | oneof (Xbar / Cmn) | Confidence |
|---|---|---|---|---|---|
| vfc | `6,5,1,32,1` | 106 | 1 | 89 / 90 | CERTAIN |
| glc | `6,5,1,32,1` | 106 | 1 | 81 / 82 | CERTAIN |
| gfc | `6,5,1,32,1` | 106 | 1 | 80 / 81 | CERTAIN |

45 payload + 61 = 106. The `{sync_flag_id, sync_flag_core_type, done}` triple is the stream's per-progress sync-flag bump — the producer/consumer occupancy signal the stream queue uses.

---

## SC_MESSAGE_* Band — Inter-Tile Internal Messages (2 events)

### Purpose

The SparseCore inter-tile / SCS↔SCT internal-message band: queue-occupancy, sync-update, and SMEM-update messages between tiles. Both `Outbound` and `Inbound` carry the `21,3,14` (38-bit) `TraceIdHeader` and **share one wire shape** per gen. These are the only SC events with a `TraceIdHeader`, and both are 2-packet events.

### Algorithm

```c
// DecodeScMessageOutboundInternalMessage @0xf60e6c0 (vfc) — 2-packet, mid CHECK 128, final 176
function DecodeScMessage<Dir>InternalMessage(view, started_out, entry):
    ... SkipBits(2); DecodeTraceHeader; ...                      // bit 61
    GetBits64(dec, 21, &transaction_id);   // TraceIdHeader f1
    GetBits64(dec,  3, &core_id);          // TraceIdHeader f2 (3-bit enum)
    GetBits64(dec, 14, &chip_id);          // TraceIdHeader f3 (14-bit — note: not 12)
    entry[+0x28] = 98;                      // glc 90 / gfc 90  (Inbound: vfc 99 / glc,gfc 91)
    GetBits64(dec,  6, &extra_id);
    GetBits64(dec,  5, &dest_tile_id);
    GetBits64(dec,  1, &dest_core_type);   // enum TEC_OR_SCS/TAC
    GetBits64(dec, 13, &sync_flag_id);
    GetBits64(dec,  4, &smem_address_lo);  // ── packet-boundary straddle (4+10) ──
    CHECK(BitsDecoded() == 128);           // packet-1 boundary; movq $0x20 (32 B)
    GetBits64(dec,  1, &straddle_a);
    GetBits64(dec,  1, &straddle_b);
    GetBits64(dec, 10, &smem_address_hi);
    GetBits64(dec,  1, &msg_type);         // enum SYNCUPDATE/SMEMUPDATE
    GetBits64(dec,  2, &opcode);           // enum WRITE/INC × NO_DONE/WITH_DONE
    GetBits64(dec, 32, &data);
    GetBits64(dec,  1, &done);
    CHECK(BitsDecoded() == 176);
```

| Field | Width | Meaning | Confidence |
|---|---|---|---|
| `trace_id_header` | 38 (`21,3,14`) | per-transaction identity (`transaction_id`, `core_id`, `chip_id`) | CERTAIN |
| `extra_id` | 6 | correlation sub-id | HIGH |
| `dest_tile_id` | 5 | target tile of the message | HIGH |
| `dest_core_type` | 1 | target core class — `TEC_OR_SCS`/`TAC` enum | HIGH |
| `sync_flag_id` | 13 | sync flag the message updates | HIGH |
| `smem_address` | 4+10 (straddle) | SMEM target address (spans the packet boundary) | HIGH |
| `msg_type` | 1 | `SYNCUPDATE`/`SMEMUPDATE` enum | HIGH |
| `opcode` | 2 | `WRITE`/`INC` × `NO_DONE`/`WITH_DONE` enum | HIGH |
| `data` | 32 | message payload word | HIGH |
| `done` | 1 | done flag | HIGH |

| Gen | `TraceIdHeader` \| payload widths | mid `CHECK` | final `CHECK` | pkts | oneof (Out / In) | Confidence |
|---|---|---|---|---|---|---|
| vfc | `21,3,14` \| `6,5,1,13,4,1,1,10,1,2,32,1` | 128 | 176 | 2 | 98 / 99 | CERTAIN |
| glc | `21,3,14` \| `6,5,1,13,4,1,1,10,1,2,32,1` | 128 | 176 | 2 | 90 / 91 | CERTAIN |
| gfc | `21,3,14` \| `6,5,1,13,4,1,1,10,1,2,32,1` | 128 | 176 | 2 | 90 / 91 | CERTAIN |

payload incl. straddle = 115 bits; + 61 = 176. Byte-identical across all three SC gens. `dest_tile_id` + `dest_core_type` pick the target tile and core; `msg_type` + `opcode` are the message semantic.

---

## The SparseCore Selector Enum Value Tables

Read byte-exact from the `*Values` nested enums of each SC message in `trace_entries.proto` (descriptor pool: vfc @ `0xbf06830`, glc @ `0xbf41210`, gfc @ `0xbf64c80`). These are the *named* values for the enum fields decoded above. vfc values shown; glc/gfc identical **except** `StreamOpcodeValues`, which gains 3 values (and a bit) on the newer gens.

| Enum | Width | Values | Confidence |
|---|---|---|---|
| `StreamOpcodeValues` (vfc) | 3-bit | `0=GATHER, 1=GATHERADDS32, 2=GATHERADDF32, 4=SCATTER, 5=SCATTERADDS32, 6=SCATTERADDF32, 7=RESERVED` (value 3 is a hole) | CERTAIN |
| `StreamOpcodeValues` (glc/gfc) | 4-bit | adds `9=GATHERADDS16, 10=GATHERADDBF16, 13=SCATTERADDS16, 14=SCATTERADDBF16, 15=RESERVED` | CERTAIN |
| `SyncFlagCoreTypeValues` | 1-bit | `0=TEC_OR_SCS, 1=TAC` | CERTAIN |
| `TileLocalMemoryTypeValues` | 1-bit | `0=SMEM, 1=TILESPMEM` | CERTAIN |
| `OffTileMemoryTypeValues` | 3-bit | `0=SPMEM, 1=TILESPMEMN, 2=HBM, 3=HBM4B` | CERTAIN |
| `TileLocalStreamTypeValues` | 1-bit | `0=LINEAR, 1=CIRCULARBUFFER` | CERTAIN |
| `OffTileStreamTypeValues` | 2-bit | `0=LINEAR, 1=STRIDED, 2=INDIRECT, 3=INDIRECTVREG` | CERTAIN |
| `IndirectListTypeValues` | 1-bit | `0=WORD, 1=ROW` | CERTAIN |
| `DestCoreTypeValues` | 1-bit | `0=TEC_OR_SCS, 1=TAC` | CERTAIN |
| `MsgTypeValues` | 1-bit | `0=SYNCUPDATE, 1=SMEMUPDATE` | CERTAIN |
| `OpcodeValues` | 2-bit | `0=WRITE_NO_DONE, 1=WRITE_WITH_DONE, 2=INC_NO_DONE, 3=INC_WITH_DONE` | CERTAIN |

> **QUIRK —** `StreamOpcode` is **not** densely packed. The GATHER family occupies `0..2`, value 3 is a hole, the SCATTER family starts at 4 — so the gather/scatter dichotomy is encoded in the high bit, not a contiguous range. On glc/gfc the half-width-accumulate variants (`+S16`/`+BF16`) are NEW on Ghostlite/6acc60406 and slot in at `9,10,13,14`, forcing the field to 4 bits. A reimplementation that assumes `0..n` contiguity will misdecode SCATTER as a reserved opcode. The `SyncFlagCoreType`/`DestCoreType` `TEC_OR_SCS`/`TAC` split mirrors the [SparseCore sequencer-type enum](../sparsecore/getsequencertype.md).

---

## The On-Wire-id → Trace-Point Map

The codec's [two-level dispatch](trace-entries-coder.md#the-dual-dispatch) splits the dense on-wire `trace_point_id` space into a low band (primary jump table) and a high band (secondary jump table reached on primary overflow). The SC band lives entirely in the **high** band. The on-wire id is the value the hardware stamps and the decoder indexes; it is **distinct** from the proto oneof field number the handler writes to `TraceEntry+0x28`.

> **QUIRK —** `ScInstructionCoreInterrupt` is on-wire id **108** but proto oneof field **75** (vfc). The dense on-wire id space (0..185 vfc / 0..211 glc / 0..206 gfc) is the *decode* key; the dense oneof field is the *encode* key. They count the same events but index differently — a captured device-trace ring stamps the on-wire id, so a decoder that drives off the oneof field cannot map a raw stream.

### SC sub-range — on-wire id → event → oneof (per gen)

The SC band occupies a contiguous run in the high band. on-wire ids are **identical across vfc/glc** (108..132); **gfc** inserts a `StatsCounterSampleIssuedFromScs` at id 129, shifting the two `ScMessage*` events up by one (to 132/133).

| Event | on-wire id (vfc/glc) | vfc oneof | glc oneof | gfc oneof | gfc on-wire id | Confidence |
|---|---|---|---|---|---|---|
| `ScInstructionCoreInterrupt` | 108 | 75 | 67 | 66 | 108 | CERTAIN |
| `ScInstructionSetTracemark` | 109 | 76 | 68 | 67 | 109 | CERTAIN |
| `ScInstructionTraceInstruction` | 110 | 77 | 69 | 68 | 110 | CERTAIN |
| `ScInstructionSfenceStart` | 111 | 78 | 70 | 69 | 111 | CERTAIN |
| `ScInstructionSfenceStop` | 112 | 79 | 71 | 70 | 112 | CERTAIN |
| `ScInstructionSyncStart` | 113 | 80 | 72 | 71 | 113 | CERTAIN |
| `ScInstructionSyncStop` | 114 | 81 | 73 | 72 | 114 | CERTAIN |
| `ScInstructionBarrierStart` | 115 | 82 | 74 | 73 | 115 | CERTAIN |
| `ScInstructionBarrierStop` | 116 | 83 | 75 | 74 | 116 | CERTAIN |
| `ScInstructionSyncWatchStart` | 117 | 84 | 76 | 75 | 117 | CERTAIN |
| `ScInstructionSyncWatchStop` | 118 | 85 | 77 | 76 | 118 | CERTAIN |
| `ScTaskIssueFromScs` | 119 | 86 | 78 | 77 | 119 | CERTAIN |
| `ScTaskCommitOnSct` | 120 | 87 | 79 | 78 | 120 | CERTAIN |
| `ScStreamIssueFromCore` | 121 | 88 | 80 | 79 | 121 | CERTAIN |
| `ScStreamProgressXbar` | 122 | 89 | 81 | 80 | 122 | CERTAIN |
| `ScStreamProgressCmn` | 123 | 90 | 82 | 81 | 123 | CERTAIN |
| `ScMessageOutboundInternalMessage` | 131 | 98 | 90 | 90 | 132 | CERTAIN |
| `ScMessageInboundInternalMessage` | 132 | 99 | 91 | 91 | 133 | CERTAIN |

The 7-id gap between `ScStreamProgressCmn` (123) and `ScMessageOutbound` (131) is filled by the **SC-issued OCI** descriptor/message events — on-wire ids 124..130 = `OciDescriptorCommonIssuedBySc`, `OciDescriptorStride{Src,Dst,Steps}IssuedBySc`, `OciDescriptorAddressMiscIssuedFromSc`, `OciMessage{ReceivedBySc,SentBySc}` (the SC's view of its own OCI traffic, owned by [`payload-uhi-oci-ici-dma.md`](payload-uhi-oci-ici-dma.md)). gfc additionally exposes a SparseCore PMU surface: `StatsCounterSampleIssuedFromScs` @ id 129, and `StatsCounterSampleIssuedFrom{Sctd,Sctc}` @ ids 134/135 (the SC TileData/TileCompute hardware perf-counter samples — gfc-only).

### Two-level dispatch parameters (the path to the SC band)

```text
DecodeEntry(view):
  read framing(2) + trace_point_id(8)
  if id <= bound1:  goto *primary_jt [id]                  // low band
  else:             id2 = id - rebase                       // high band
                    if id2 <= bound2: goto *secondary_jt [id2]
                    else:             goto error
```

| Gen | `DecodeEntry` | primary jt | bound1 | rebase | secondary jt | bound2 | SC sub-range | Confidence |
|---|---|---|---|---|---|---|---|---|
| vfc | `0xf5f7080` | `0xab86ce8` | `0x5f` (95) | −0x60 (96) | `0xab86e68` | `0x59` (89) | 108..132 | CERTAIN |
| glc | `0xf6295c0` | `0xab875a8` | `0x62` (98) | −0x63 (99) | `0xab87734` | `0x70` (112) | 108..132 | CERTAIN |
| gfc | `0xf65ffe0` | `0xab87f20` | `0x64` (100) | −0x6c (108) | `0xab880b4` | `0x62` (98) | 108..133 | CERTAIN |
| vlc | `0xf5d6460` | `0xab86520` | `0x8f` (143) | −0x90 (144) | `0xab86760` | `0x17` (23) | — (no SC) | CERTAIN |

Each secondary arm is a `rel32` relative to the secondary table base; the arm is a thunk inside `DecodeEntry` that stamps the proto oneof tag and tail-calls the matching `DecodeSc<Name>`. The codec page owns the [dispatch mechanism](trace-entries-coder.md#the-dual-dispatch); this page owns the SC sub-range it routes to.

> **CONFIRMED (SC-4) —** in the vfc `DecodeEntry` @ `0xf5f7080`, the decompiler renders the two-level dispatch as nested switches — the outer `switch` bound at `case 0x5f` (primary), the inner `switch` rebased past `case 0x60` reaching `DecodeScInstructionCoreInterrupt`. This is the secondary table the earlier pxc reading (single table only) labeled an "error label": on vfc/vlc/glc/gfc the overflow target is a *second* id-rebased jump table for the high id band (SC/throttle/MGR/CMNUR). pxc and vlc reach the secondary table but it holds **no** SC arms — pxc has the BarnaCore `Bc_Fsm*` band instead, vlc holds only `VdqTransaction*` and MGR OCI.

---

## What Is Not Decoded Here

- **The exact uint64 reassembly bit order for the straddle fields** — `ScTaskCommitOnSct.tec_sync_stalls` (7+9) and `ScMessage*.smem_address` (4+10): the width fragments, the `+0xNN` destination offset, and the packet-1 boundary `CHECK == 128` are CERTAIN, but the precise LSB position each fragment occupies in the reassembled value is **not** tabulated (LOW — same open item as the UHI/OCI straddle fields).
- **The downstream SC scalar → XStat/XEvent mapping** — which decoded SC field (`num_spmem_words`, `tec_sync_stalls`, `tile_bitmap`, `stream_opcode`) becomes which XStat vs is folded into the SparseCore XEvent name happens in the `SparseCoreOverlaySubscriber`, owned by [`trace-entry-to-xevent.md`](trace-entry-to-xevent.md).
- **The gfc-only SC PMU sampling events** (`StatsCounterSampleIssuedFrom{Scs,Sctd,Sctc}` @ ids 129/134/135) and the SC-issued OCI descriptor sub-band (ids 124..130) — adjacent to the SC band but not `DecodeSc*` events; their payloads are not decoded here.
- **The cause of the gfc `ScTaskCommitOnSct` restructure** — the wire shape (9 fields, `CHECK == 219`) is CERTAIN; whether the dropped TAC counters reflect a unified-SC microarch change or a profiler schema rev was not cross-checked against the SC ISA (LOW).

---

## Related Components

| Component | Relationship |
|---|---|
| [TraceEntriesCoder](trace-entries-coder.md) | the codec these SC payloads sit inside — 16-byte packet, 61-bit envelope, `GetBits64`/`CHECK`, two-level dispatch |
| [TracePoints Master Registry](tracepoints-master-registry.md) | the full cross-band wire-id ↔ oneof-field registry; this page owns only the SC sub-range |
| [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) | the neighboring high-band events (SC-issued OCI ids 124..130) and the `21,3,12` `TraceIdHeader` the SC band varies to `21,3,14` |
| [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) | the per-family header deltas (6-bit `block_id`, 45-bit timestamp, 14-bit `chip_id`) the SC payloads inherit |
| [SCS Engine](../sparsecore/scs-engine.md) | the sequencer that issues `ScTaskIssueFromScs` and runs the `ScInstruction*` primitives |
| [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md) | the datapath the `ScStream*` events and `StreamOpcode` enum instrument |

## Cross-References

- [TraceEntriesCoder](trace-entries-coder.md) — the packet/framing/header/dispatch this page's payloads are read behind
- [TracePoints Master Registry](tracepoints-master-registry.md) — the wire-id ↔ oneof-field two-id-space registry the SC sub-range slots into
- [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) — the adjacent bands (and the SC-issued OCI events) in the same high id band
- [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) — the newer-family header layout the SC band is built on
- [Payload: jxc Legacy](payload-jxc-legacy.md) — the separate `PerformanceTraceEntry` schema with no SC band
- [SCS Engine](../sparsecore/scs-engine.md) · [TEC Engine](../sparsecore/tec-engine.md) · [TAC Engine](../sparsecore/tac-engine.md) — the SparseCore sequencer and compute tiles these events instrument
- [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md) — the gather/scatter engine the `ScStream*` band and `StreamOpcode` enum describe
- [getSequencerType](../sparsecore/getsequencertype.md) — the `SCS/TAC/TEC` sequencer-type enum the `SyncFlagCoreType`/`DestCoreType` selectors mirror
- [Profiling and Telemetry Overview](overview.md) — the capture→encode→decode→xplane pipeline this band feeds
