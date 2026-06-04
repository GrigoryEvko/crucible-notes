# ICR Node-Fabric DMA Timeline Band

> *All addresses on this page apply to `libtpu.so` from the wheel `libtpu-0.0.40` (build-id `89edbbe81c5b328a958fe628a9f2207d` — the unambiguous anchor; the runtime-reported `0.103` is not statically verifiable in the binary). Other builds will differ. All offsets are VMA; `.text` and `.rodata` are mapped VMA == file offset.*

## Abstract

The TPU profiler renders inter-chip-router (ICR / node-fabric) DMA traffic as two timeline lanes — **"From ICI Router"** (component 54) and **"To ICI Router"** (component 55) — in the device XPlane. Unlike the OCI *command* bands (trace-point ids 22/26/96), which name a command and resolve a `dma_id` only on demand, the DMA-timeline producer keys on exactly **four** trace-point ids — **48, 50, 51, 91** — and stitches them into begin/end DMA spans. Each id carries a *different* node-fabric trace message, contributes a *different* part of the span (begin marker, end marker, byte count, or end timestamp), and lands in one of two `flat_hash_map<uint64,DmaTransfer>` keyed by a 38-bit `dma_id` extracted from the message's `TraceIdHeader`.

This page owns the variant decode on top of the shared 16-byte `TraceEntry` packet (the 2-bit frame + 59-bit `TraceHeader` + 36-bit `TraceIdHeader`, see [Trace Entries Coder](trace-entries-coder.md)). For these four ids the producer reads the on-wire `trace_point_id` from `TraceHeader+0x18`, then dereferences the message submessage (`TraceEntry+0x10` → submessage at `+0x20`) and decodes the proto fields packed at fixed C++ offsets. The four messages split cleanly into two directions: **egress** (data leaving to the router) uses id 91 (descriptor) + id 50 (egress message); **ingress** (data arriving from the router) uses id 48 (ICI data packet) + id 51 (ingress message). Two independent maps keep the egress and ingress span sets separate, then each is merged and rendered to its lane.

The producer is the third nested lambda of `ConvertTpuTraceToXPlane<pxc::profiler::TraceEntry>` at `0xf26c6e0`; the per-id `dma_id` extractor is `TraceEntryWrapper<pxc>::GetDmaId` at `0xf699ca0`; the lane renderer is `ConvertDmaTransfersToXPlane` at `0xf254bc0`. All three carry full C++ symbols in this build, so the message class names, field names, and the `DmaTransfer` layout are recovered directly, not inferred.

For reimplementation, the contract is:

- **The four-id key set** `{48, 50, 51, 91}` and how the producer builds it (a 4-element `GetMerged` selector), with the precise dispatch order.
- **Each id's message class, proto oneof case, and field layout** — the C++ offset of every field the producer reads.
- **Each id's role in a span** — which of `{begin_gtc, begin_present, end_gtc, end_present, byte_count, kind_tag}` it writes.
- **The byte-count rule** — OCI message `msg_data << 9` (fixed 512 B granule) versus descriptor `length << {2|9}` selected by `length_granule`.
- **The 38-bit `dma_id` extraction** and how begin/end pair through the two egress/ingress maps.

| | |
|---|---|
| **DMA-timeline producer** | `0xf26c6e0` — `ConvertTpuTraceToXPlane<pxc>::{lambda#1}::{lambda#3}` |
| **`dma_id` extractor** | `0xf699ca0` — `TraceEntryWrapper<pxc>::GetDmaId(int)`, single caller `@0xf26c8d9` |
| **Lane renderer** | `0xf254bc0` — `ConvertDmaTransfersToXPlane` |
| **Span merger** | `0xf26dae0` — `MergeOverlappingTransfers` (run per map) |
| **Key set** | `{48, 50, 51, 91}` — built from `0x320000005B` + two stores |
| **Span value type** | `DmaTransfer`, 0x58 (88) bytes; `push_back` `@0xf2547e0` |
| **Egress lane** | component 55 "To ICI Router", XEvent "ICI Egress" (kind tag 3) |
| **Ingress lane** | component 54 "From ICI Router", XEvent "ICI Ingress" (kind tag 2) |

---

## At a Glance — The Four ICR DMA Bands

| Band ID | Message class (`asic_sw::driver::deepsea::pxc::profiler::`) | Oneof | Direction / map | Producer role | Payload it carries | Confidence |
|---|---|---|---|---|---|---|
| **48** | `IciPacketDataPacketQueuedForLocalIngress` | 29 (0x1d) | ingress / MAP_B | begin **or** end marker | first/last-packet bools | CERTAIN |
| **50** | `OciMessageGeneratedInIcrEgressDma` | 31 (0x1f) | egress / MAP_A | end timestamp only | `done` gate, `msg_data` (unused here) | CERTAIN |
| **51** | `OciMessageGeneratedInIcrIngressDma` | 32 (0x20) | ingress / MAP_B | byte-count accumulate | `msg_data` << 9 | CERTAIN |
| **91** | `OciDescriptorCommonIssuedFromTcs` | 48 (0x30) | egress / MAP_A | begin + byte-count | `length` << granule, `dma_type` gate | CERTAIN |

> **NOTE —** the four ids are *not* contiguous and do *not* share a message class. They share only the `TraceIdHeader` (proto field 1, C++ offset `+0x18`) that yields the `dma_id`. The grouping is behavioural: these are the four trace points the DMA-timeline pass keys on. Every other id in the registry that `GetDmaId` can decode (the OCI command band 22/26/96, etc.) is never fed to this pass — `GetDmaId` has exactly one caller (`@0xf26c8d9`), and it keys on only these four.

---

## Building the Key Set

### Purpose

Before walking the per-core merged trace entries, the producer constructs the 4-element set of trace-point ids it cares about and hands it to `GetMerged`, which returns only entries whose id is in the set. This is the gate that restricts the entire pass to ids `{48, 50, 51, 91}`.

### Algorithm

```c
function BuildKeySetAndMerge(entries):           // 0xf26c6e0, top of loop body
    // Pack two ids into one qword on the heap, then grow to a 4-element span.
    p   = operator_new(8)
    *p  = 0x320000005B                            // @0xf26c7bf: { 0x5B=91, 0x32=50 }
    set = operator_new(0x10)
    set[2] = 48                                   // @0xf26c7d9: third element
    set[0..1] = *p                               // copies {91,50} into the span
    set[3] = 51                                   // @0xf26c7ff: fourth element
    count = 4                                     // @ v155
    free(p)
    GetMerged(&merged, entries, &set, count)      // 0xf26ba80, called @0xf26c81d
    free(set)
    return merged                                 // vector of {ChipCoreId, entry*} triples
```

> **QUIRK —** the id set is assembled as a little-endian qword `0x320000005B` first, so `91` and `50` are byte-packed (`0x5B`, `0x32`) into one 64-bit store, then `48` and `51` are written as separate dwords. A reimplementation can simply use the unordered set `{48, 50, 51, 91}`; the packing is a code-gen artifact, not semantics.

### Considerations

`GetMerged` (`0xf26ba80`) is the per-id-set merge that flattens the `TpuTraceEntryPerCoreMap` into a flat vector of `{ChipCoreId, TraceEntry*}` records (24-byte stride: the entry pointer is at `record+0x10`). The DMA producer iterates this vector once; each record is classified by its `trace_point_id` and routed to one of the four arms below.

---

## `dma_id` Extraction — `GetDmaId`

### Purpose

Every span is keyed by a 38-bit `dma_id` derived from the message's `TraceIdHeader`. `GetDmaId` is a large switch over the on-wire `trace_point_id`; for the four DMA ids it takes a "single-header" arm that loads the live message, reads its `TraceIdHeader` (field 1 at `+0x18`), and composes the id. The OCI *command* ids (22/23/26/54/55/96) take a different arm that calls `CmdDmaIdFromEntry<...>` — these are never reached by the DMA pass.

### Entry Point

```text
0xf26c6e0  ConvertTpuTraceToXPlane<pxc>::{lambda#3}     ── DMA producer
  └─ 0xf699ca0  GetDmaId(int)                           ── 38-bit dma_id, called @0xf26c8d9 with selector 0
       └─ 0xf69a444  single-header compose tail         ── the 38-bit pack
```

### Algorithm

```c
function GetDmaId(wrapper, selector):            // 0xf699ca0
    submsg = wrapper[2]                          // TraceEntry at +0x10
    hdr    = submsg[24] ?: TraceHeader_globals_   // TraceHeader, default prototype if null
    id     = hdr[6]                              // trace_point_id (uint32 @ TraceHeader+0x18)
    switch (id):
        // --- the four DMA single-header ids ---
        case 48:  if submsg.oneof == 29 goto live   else def = unk_2237B590  // globals+0x18
        case 50:  if submsg.oneof == 31 goto live   else def = unk_2237AF28
        case 51:  if submsg.oneof == 32 goto live   else def = unk_2237AEF0
        case 91:  if submsg.oneof == 48 goto live   else def = unk_2237B240
            // (oneof checked at submsg+0x28; "globals_" fallback when the
            //  live message is absent — see the per-id globals table below)
        // --- the OCI command ids take CmdDmaIdFromEntry, NOT this tail ---
        case 22/23/26/54/55/96: return CmdDmaIdFromEntry<OciCommon...>(...)
        default: return 0

  live:                                          // LABEL_170
    hdr2 = (*submsg[+0x20]) + 24                  // live message's TraceIdHeader ptr
  compose:                                       // LABEL_172 @0xf69a444
    txn  = hdr2[6]                               // transaction_id  (uint32 @ TraceIdHeader+0x18)
    core = hdr2[7]                               // core_id         (uint32 @ +0x1c)
    chip = hdr2[8]                               // chip_id         (uint32 @ +0x20)
    dma_id = (txn & 0x1FFF00)                     // transaction_id bits 8..21
           | (txn & 0xFF)                         // transaction_id bits 0..8
           | ((core & 7)      << 21)              // core_id bits 21..24
           | ((chip & 0x3FFF) << 24)              // chip_id bits 24..38
    presence = 1                                 // dl = 1 (the "this is a DMA event" flag)
    return dma_id
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `GetDmaId(int)` | `0xf699ca0` | per-id `TraceIdHeader` → 38-bit `dma_id`; single caller | CERTAIN |
| single-header compose tail | `0xf69a444` | the 38-bit bitfield pack | CERTAIN |
| `CmdDmaIdFromEntry<...>` | (per-class) | OCI command-band path, **unused** by the DMA pass | HIGH |

### Per-ID Globals Prototypes

The "globals_" fallback used when a live message is absent. Each is the zero-initialized message prototype; `GetDmaId` reads its `TraceIdHeader` slot at `+0x18` (the addresses below are the `globals_` base; the arm dereferences `base+0x18`).

| Band ID | `globals_` symbol | Address | Confidence |
|---|---|---|---|
| 48 | `IciPacketDataPacketQueuedForLocalIngress_globals_` | `0x2237b578` | CERTAIN |
| 50 | `OciMessageGeneratedInIcrEgressDma_globals_` | `0x2237af10` | CERTAIN |
| 51 | `OciMessageGeneratedInIcrIngressDma_globals_` | `0x2237aed8` | CERTAIN |
| 91 | `OciDescriptorCommonIssuedFromTcs_globals_` | `0x2237b228` | CERTAIN |
| — (null default) | `TraceIdHeader_globals_` (all-zero → `dma_id` 0) | `0x2237a308` | CERTAIN |

> **GOTCHA —** the presence bit (`dl`) is checked by the caller (`test dl,1; je drop` `@0xf26c8de`). The all-zero `TraceIdHeader_globals_` default still yields presence == 1 with `dma_id == 0`; a span keyed on 0 is technically built but, lacking a real begin/end pair, is dropped downstream. Do not assume `dma_id == 0` is impossible.

---

## Per-Band Payload Decode

The four arms share a skeleton: classify by `trace_point_id` (read at `TraceHeader+0x18`), load the live message pointer (`TraceEntry+0x20`, falling back to the class `globals_`), verify the proto oneof case at `submsg+0x28`, apply the band's gate, find-or-insert the `dma_id` slot in the band's map, and write the band's contribution. The dispatch order in the producer is:

```text
v23 = trace_point_id
if v23 <= 50:                  // → id 48 / id 50
    if v23 != 48: { if v23 == 50: <egress-message arm> }
    else:        <ici-packet arm>
elif v23 == 51:                <ingress-message arm>
elif v23 == 91:                <descriptor arm>
```

### Band 48 — `IciPacketDataPacketQueuedForLocalIngress` (ingress begin/end markers)

The ICI data-packet trace point carries the *begin* and *end* of an ingress DMA as two trailing bool flags. It writes into MAP_B (ingress) with kind tag 2.

| Field | Proto # | C++ off | Type | Role | Confidence |
|---|---|---|---|---|---|
| `trace_id_header` | f1 | `+0x18` | submessage | `dma_id` source | CERTAIN |
| `router_link_port_id` | f2 | `+0x20` | enum | (decoded, dropped) | HIGH |
| `virtual_channel` | f3 | `+0x24` | uint32 | (decoded, dropped) | HIGH |
| `link_targets` | f4 | `+0x28` | uint32 | (decoded, dropped) | HIGH |
| `local_ingress_target` | f5 | `+0x30` | bool | (decoded, dropped) | HIGH |
| `multicast` | f6 | `+0x31` | bool | (decoded, dropped) | HIGH |
| `dst_chip_id` | f7 | `+0x2c` | uint32 | (decoded, dropped) | HIGH |
| **`first_packet_in_dma`** | f8 | `+0x32` | bool | **begin marker** | CERTAIN |
| **`last_packet_in_dma`** | f9 | `+0x33` | bool | **end marker** | CERTAIN |

```c
function Band48(msg, slot):                      // arm @0xf26ca2f, stores @0xf26ce60..
    if msg[+0x32] == 1:                          // first_packet_in_dma
        slot.begin_gtc[+0x8]  = gtc
        slot.begin_present[+0x10] = 1
        slot.byte_count[+0x28] = 0               // begin zeroes the byte counter
        slot.kind_tag[+0x40]  = 2                // ingress
    else if msg[+0x33] == 1:                     // last_packet_in_dma
        slot.end_gtc[+0x18]   = gtc
        slot.end_present[+0x20] = 1
        slot.kind_tag[+0x40]  = 2
```

> **CORRECTION (ICR-1) —** an earlier reading held that a single id-48 event "can set BOTH first and last, producing a self-contained span." The decompiled store block (`@0xf26ce5c..` in `0xf26c6e0`) is an `if (first) {...} else { if (last) {...} }` chain: a single id-48 event writes **either** the begin marker **or** the end marker, never both. A self-contained span therefore requires two distinct id-48 events (one with `first_packet_in_dma`, one with `last_packet_in_dma`) sharing the same `dma_id`.

### Band 50 — `OciMessageGeneratedInIcrEgressDma` (egress end timestamp)

Egress messages contribute *only* the end timestamp of an egress span, and only when `done == 1`. They carry `msg_data` (the same field id 51 uses for bytes), but the producer routes id 50 to the end-timestamp store, not the byte-count store — the egress bytes come from the descriptor (id 91) instead.

| Field | Proto # | C++ off | Type | Role | Confidence |
|---|---|---|---|---|---|
| `trace_id_header` | f1 | `+0x18` | submessage | `dma_id` source | CERTAIN |
| `msg_data` | f2 | `+0x20` | uint32 | (present, **not** read by id 50) | CERTAIN |
| **`done`** | f3 | `+0x24` | bool | **gate** (`done == 1`) | CERTAIN |
| `msg_type` | f4 | `+0x28` | enum | (decoded, dropped) | HIGH |
| `opcode` | f5 | `+0x2c` | enum | (decoded, dropped) | HIGH |
| `node_type` | f7 | `+0x30` | enum | label, **not** a line key | HIGH |
| `addr` | f6 | `+0x34` | uint32 | (decoded, dropped) | HIGH |

```c
function Band50(msg, slot):                      // arm @0xf26c919, store @0xf26cf41
    if msg[+0x24] != 1: return                   // done gate
    slot.end_gtc[+0x18]    = gtc
    slot.end_present[+0x20] = 1                   // no bytes, no kind-tag write
```

> **NOTE —** id 50 does **not** write the kind tag. It touches a slot whose tag was set to 3 by the egress descriptor (id 91) begin. The span's lane is therefore fixed by its *map* (MAP_A → tag 3), not by id 50.

### Band 51 — `OciMessageGeneratedInIcrIngressDma` (ingress byte count)

Identical message layout to id 50 (SHAPE-A), but on the ingress side and used for its bytes. The ingress message contributes *only* the byte count; the ingress begin/end timestamps come from id 48's first/last-packet markers. Writes into MAP_B (ingress).

```c
function Band51(msg, slot):                       // arm @0xf26ca95, store @0xf26cedb
    slot.byte_count[+0x28] += (uint32)(msg[+0x20] << 9)   // msg_data * 512
```

The shift is fixed at 9: the OCI message length unit is a fixed 512-byte granule, and `OciMessageGeneratedInIcr*Dma` has no granule field. The `add` (not `mov`) means multiple ingress messages with the same `dma_id` sum their bytes into the span.

### Band 91 — `OciDescriptorCommonIssuedFromTcs` (egress begin + bytes)

The node-fabric descriptor is the richest of the four (17 fields), but the DMA pass reads only three: `dma_type` (gate), `length` (byte source), and `length_granule` (shift selector). It writes the egress span's begin timestamp **and** its byte count, with kind tag 3, into MAP_A (egress). The full src/dst endpoint and sync-flag fields are decoded into the proto but dropped by this pass.

| Field | Proto # | C++ off | Type | Role | Confidence |
|---|---|---|---|---|---|
| `trace_id_header` | f1 | `+0x18` | submessage | `dma_id` source | CERTAIN |
| **`dma_type`** | f2 | `+0x20` | enum | **gate** (`== REMOTEUNICAST`) | CERTAIN |
| `src_mem_mem_id` | f3 | `+0x24` | enum | (decoded, dropped) | HIGH |
| `src_mem_core_id` | f4 | `+0x28` | uint32 | (decoded, dropped) | HIGH |
| `src_opcode` | f5 | `+0x2c` | enum | (decoded, dropped) | HIGH |
| `dst_mem_mem_id` | f6 | `+0x30` | enum | (decoded, dropped) | HIGH |
| `dst_mem_core_id` | f7 | `+0x34` | uint32 | (decoded, dropped) | HIGH |
| `dst_opcode` | f8 | `+0x38` | enum | (decoded, dropped) | HIGH |
| `src_sync_flag_id` … `dst_sync_flag_1_core_id` | f9–f14 | `+0x3c`–`+0x50` | uint32 | (decoded, dropped) | HIGH |
| `program_counter` | f15 | `+0x54` | uint32 | (decoded, **not** the byte source) | HIGH |
| **`length`** | f16 | `+0x58` | uint32 | **byte-count source** | CERTAIN |
| **`length_granule`** | f17 | `+0x5c` | enum | **shift selector** | CERTAIN |

```c
function Band91(descr, slot):                     // arm @0xf26c9b2, store @0xf26c865..88b
    if descr[+0x20] != 2: return                  // dma_type == DMA_TYPE_REMOTEUNICAST
    slot.begin_gtc[+0x8]    = gtc
    slot.begin_present[+0x10] = 1
    shift = (descr[+0x5c] == 0) ? 9 : 2           // length_granule: 512B→<<9, 4B→<<2
    slot.byte_count[+0x28]  = descr[+0x58] << shift   // length, with `mov` (overwrite)
    slot.kind_tag[+0x40]    = 3                    // egress
```

> **CORRECTION (ICR-2) —** the byte-count source for id 91 is **`length`** (f16, `+0x58`), **not** `program_counter`. In the descriptor's in-memory layout `program_counter` (`+0x54`) immediately precedes `length` (`+0x58`); an earlier note read the `+0x58` source as `program_counter`. The producer's `mov eax,[r14+0x58]; shl rax,cl` (`@0xf26c880`) reads `length`, and the shift selector `cmp [r14+0x5c],0` reads `length_granule`. The `program_counter` field is decoded but never used by this pass.

---

## The Byte-Count Rule

`byte_count` lives at `DmaTransfer+0x28` (map-value form) / `+0x20` (merge/span form). There are two distinct accumulation rules; which one applies is fixed per band.

| Side | Band | Source field | Scale | Store op | Producer site | Confidence |
|---|---|---|---|---|---|---|
| OCI message | 51 | `msg_data` (f2, `+0x20`) | `<< 9` (×512, fixed) | `add` | `0xf26cedb` | CERTAIN |
| OCI message | 50 | — (end timestamp only) | — | — | — | CERTAIN |
| Descriptor | 91 | `length` (f16, `+0x58`) | `<< (granule==0 ? 9 : 2)` | `mov` | `0xf26c880` | CERTAIN |
| ICI packet | 48 | — (markers only) | — | — | — | CERTAIN |

The descriptor shift is chosen by the descriptor's own `length_granule` enum:

| `length_granule` | Value | Shift | Meaning |
|---|---|---|---|
| `LENGTH_GRANULE_512B` | 0 | `<< 9` | `length` counts 512-byte granules |
| `LENGTH_GRANULE_4B` | 1 | `<< 2` | `length` counts 4-byte words |

> **QUIRK —** id 91 **overwrites** (`mov`) the byte count while id 51 **adds** (`add`). This is correct by construction: id 91 is the descriptor *begin* — it zero-inits the slot and sets the bulk size once. id 51 is an ingress message arriving into a slot whose begin was set by an id-48 marker; multiple ingress messages for the same `dma_id` must sum. The two never mix in one span because id 91 lands in MAP_A (egress) and id 51 in MAP_B (ingress).

### The egress/ingress division of labour

Per direction, the span fields come from *different* trace points:

```text
EGRESS span (MAP_A, kind tag 3 → "To ICI Router"):
    begin_gtc, byte_count   ← id 91  (descriptor: length × granule)
    end_gtc                 ← id 50  (egress message, done==1)

INGRESS span (MAP_B, kind tag 2 → "From ICI Router"):
    begin_gtc, end_gtc      ← id 48  (first/last packet markers)
    byte_count              ← id 51  (ingress message: msg_data × 512)
```

> **NOTE —** the asymmetry — egress message (id 50) gives only a timestamp, ingress message (id 51) gives only bytes — is a property of the trace points themselves (where the size and the completion timestamp are emitted on each side), not a choice in libtpu. The static decode confirms the division of labour; the silicon rationale is not visible here.

---

## Span Pairing and the Two Maps

### Purpose

Begin and end events for one DMA arrive as separate trace entries; they are reunited by their shared 38-bit `dma_id`. Two independent `flat_hash_map<uint64,DmaTransfer>` hold the open spans — one for egress, one for ingress — so the two direction's spans never collide on `dma_id`.

### Algorithm

```c
function PairSpans(merged_entries):              // 0xf26c6e0 main loop
    map_egress  = {}                             // &v147 — id 50 + id 91
    map_ingress = {}                             // &v150 — id 48 + id 51
    vec_egress  = []                             // ptr   — flushed full egress spans
    vec_ingress = []                             // v145  — flushed full ingress spans

    for entry in merged_entries:
        gtc    = entry.TraceHeader.timestamp     // f3, uint64 @ TraceHeader+0x20
        dma_id = GetDmaId(entry, 0)              // 0xf699ca0
        if not dma_id.present: continue          // test dl,1; je drop @0xf26c8de
        id     = entry.trace_point_id            // @ TraceHeader+0x18
        map    = (id == 48 || id == 51) ? map_ingress : map_egress
        slot   = map.find_or_insert(dma_id)      // PrepareInsertSmallNonSoo / PrepareInsertLarge

        // If the slot is already complete (begin+end), flush it and reopen:
        if slot.begin_present && slot.end_present:
            (map == map_ingress ? vec_ingress : vec_egress).push_back(slot.span)  // 0xf2547e0
            slot.begin_present = slot.end_present = 0

        apply_band_contribution(id, entry.message, slot)   // see per-band arms

    for map,vec in {(egress,vec_egress),(ingress,vec_ingress)}:
        flush_all_complete(map, vec)
        MergeOverlappingTransfers(vec)           // 0xf26dae0
        ConvertDmaTransfersToXPlane(vec, plane)  // 0xf254bc0
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `flat_hash_map` policy | `0x21646fe8` | `FlatHashMapPolicy<unsigned long, DmaTransfer>` | CERTAIN |
| `PrepareInsertSmallNonSoo` | `0x21118e20` | small-table insert | HIGH |
| `PrepareInsertLarge` | `0x2111a600` | large-table insert | HIGH |
| `HashKey` | `0xe867c60` | `Hash<unsigned long>` over `dma_id` | HIGH |
| `vector<DmaTransfer>::push_back` | `0xf2547e0` | flush a complete span (88-byte copy) | CERTAIN |
| `MergeOverlappingTransfers` | `0xf26dae0` | coalesce overlapping spans per vector | HIGH |

> **GOTCHA —** the "flush-and-reopen" path (`slot.begin_present && slot.end_present` → `push_back`, clear flags) means a re-touched complete slot is *emitted and reused*, not overwritten in place. A reimplementation that simply overwrites a full slot will lose spans whenever one `dma_id` is reused across multiple transfers in a capture.

---

## The `DmaTransfer` Span Record

The map value type, 0x58 (88) bytes. The map stores the `dma_id` key at `+0x0` and the span body from `+0x8`; the merge/span form (what `push_back` copies and what the renderer reads) is the body alone.

| Field | Map-value off | Span/merge off | Type | Meaning | Confidence |
|---|---|---|---|---|---|
| `dma_id` | `+0x0` | (key) | uint64 | pairing key (38-bit) | CERTAIN |
| `begin_gtc` | `+0x8` | `+0x0` | uint64 | begin GTC tick (sort key) | CERTAIN |
| `begin_present` | `+0x10` | `+0x8` | bool | begin written | CERTAIN |
| `end_gtc` | `+0x18` | `+0x10` | uint64 | end GTC tick | CERTAIN |
| `end_present` | `+0x20` | `+0x18` | bool | end written | CERTAIN |
| `byte_count` | `+0x28` | `+0x20` | uint64 | accumulated bytes | CERTAIN |
| `kind_tag` | `+0x40` | `+0x38` | uint32 | 2 = ingress, 3 = egress | CERTAIN |

> **NOTE —** `gtc` is the event's `TraceHeader.timestamp` (proto field 3, uint64 at `TraceHeader+0x20`), loaded once per entry (`@0xf26c8cd`). It is a raw GTC tick; the GTC→picosecond conversion happens later, in the renderer's `AddEvent(GtcSpan)` path.

---

## Rendering to Lanes — `ConvertDmaTransfersToXPlane`

### Purpose

Each merged vector of `DmaTransfer` spans is turned into XEvents on the appropriate XPlane line. The renderer pre-creates four lines (63 MemcpyH2D, 64 MemcpyD2H, 54 From-ICI-Router, 55 To-ICI-Router) and four event-metadata names, then switches on each span's `kind_tag` to pick the line and event name. For the ICR band only tags 2 and 3 are produced; tags 6/7 are the host-DMA arms (unused on this trace family), and any other tag is skipped.

### Algorithm

```c
function ConvertDmaTransfersToXPlane(spans, plane):  // 0xf254bc0
    line_h2d  = plane.GetOrCreateLine(63)        // "MemcpyH2D"
    line_d2h  = plane.GetOrCreateLine(64)        // "MemcpyD2H"
    line_in   = plane.GetOrCreateLine(54)        // "From ICI Router"
    line_out  = plane.GetOrCreateLine(55)        // "To ICI Router"
    meta_in   = plane.GetOrCreateEventMetadata("ICI Ingress", 11)  // @0xf254eb8
    meta_out  = plane.GetOrCreateEventMetadata("ICI Egress",  10)  // @0xf254edf
    stat_bytes   = StatMetadata(GetStatTypeStr(78))   // byte-count stat
    stat_bw      = StatMetadata("bandwidth")
    // (also: details, queue=StatType 79 — not populated on this trace family)

    for span in spans:
        if not (span.byte_count && span.begin_present && span.end_present): continue
        switch span.kind_tag:                    // @0xf254... switch on [rbx-8]
            case 2: line = line_in;  meta = meta_in;  break    // ingress
            case 3: line = line_out; meta = meta_out; break    // egress
            case 6: line = line_h2d; meta = "MemcpyH2D"; break // host (unused here)
            case 7: line = line_d2h; meta = "MemcpyD2H"; break // host (unused here)
            default: continue                    // tags 0/4/5 dropped
        if span.end_gtc <= span.begin_gtc: continue   // non-positive duration
        ev = line.AddEvent(GtcSpan{begin_gtc, end_gtc})   // 0xf1df1e0
        ev.AddStat(stat_bytes, span.byte_count)
        if span.byte_count: ev.AddStat(stat_bw, bandwidth(span))
```

### Lane / Tag Map

| Kind tag | Source bands | Line (component) | XEvent name | Confidence |
|---|---|---|---|---|
| **2** | 48 (markers) + 51 (bytes) | 54 "From ICI Router" | "ICI Ingress" | CERTAIN |
| **3** | 91 (begin+bytes) + 50 (end) | 55 "To ICI Router" | "ICI Egress" | CERTAIN |
| 6 | (host H2D, unused) | 63 "MemcpyH2D" | "MemcpyH2D" | HIGH |
| 7 | (host D2H, unused) | 64 "MemcpyD2H" | "MemcpyD2H" | HIGH |
| 0 / 4 / 5 | — | (none) | dropped | HIGH |

The tag → direction mapping is byte-confirmed by the producer rather than only by the renderer's local-slot tracking: id 91 writes tag 3 into MAP_A (egress), id 48 writes tag 2 into MAP_B (ingress), and each map flushes to its own vector. The renderer's `case 2` pairs with the "ICI Ingress" metadata (`v120`) and `case 3` with "ICI Egress" (`v121`), and `TpuComponentName(54)` = "From ICI Router", `TpuComponentName(55)` = "To ICI Router" (`0x1c8ebb60`). The semantic direction is consistent end to end: data arriving from the router (ingress) → "From ICI Router"; data leaving to the router (egress) → "To ICI Router".

> **QUIRK —** the renderer also interns `queue` (StatType 79) and `details` stat metadata, but the `DmaTransfer` span on this trace family carries only `{begin, end, bytes, tag}`. The id-48 `router_link_port_id`/`virtual_channel`/`link_targets` and the id-91 src/dst endpoint and sync-flag fields are decoded into the proto and then dropped — they never reach an XStat here. Only the `dma_id`-derived pairing, the GTC span, and the byte count survive into the rendered event.

---

## Enums Referenced by the Band Decodes

| Enum | Values | Used by | Confidence |
|---|---|---|---|
| `DmaTypeValues` | 0=LOCAL, 1=CHIP2HOST, **2=REMOTEUNICAST**, 3=REMOTEMULTICAST | id 91 gate | CERTAIN |
| `LengthGranuleValues` | **0=LENGTH_GRANULE_512B**, 1=LENGTH_GRANULE_4B | id 91 shift | CERTAIN |
| `NodeTypeValues` | 0..6 = TCS/BC/CMQ/HBMQ/UHI/ICR/QNM | id 50/51 label (dropped) | HIGH |
| `RouterLinkPortIdValues` | 0..5 = LINK0..LINK5 | id 48 label (dropped) | HIGH |

> **NOTE —** only `REMOTEUNICAST` descriptors reach the egress timeline: id 91 is gated on `dma_type == 2`. Local, host, and multicast DMAs are filtered out before the span is built. A reimplementation that renders all `dma_type` values will over-populate the "To ICI Router" lane.

---

## What Is Not Resolved

- **Span cadence in a real capture.** The static decode proves the pairing logic but cannot prove that one egress DMA always emits exactly one id-91 begin and one id-50 end with the same `dma_id` (vs. split/coalesced). The flush-and-reopen path handles re-touched slots, but the temporal ordering (does the id-91 begin always precede the id-50 end?) is assumed from the trace-point semantics, not byte-proven.
- **The dropped descriptor/packet fields.** The 14 unused id-91 endpoint/sync-flag fields and the id-48 link/channel fields are decoded but not rendered by this pass. Whether a different XPlane pass or a downstream symbolizer reads them was not traced.
- **The GTC→picosecond and bandwidth string conversion.** `AddEvent(GtcSpan)` (`0xf1df1e0`) and the `"%.2fGB/s"`-style bandwidth `StrFormat` were not decoded here; the begin/end GTC ticks and byte count are exact, but the rendered duration and bandwidth depend on the GTC-tick divide, which this page does not pin.
- **Per-generation variants.** `GetDmaId(int)` is `pxc`-template-only. The `vfc`/`vlc`/`glc`/`gfc` generations fold the DMA band into a (likely inlined) per-gen producer over widened ids; whether they reuse the same four-id key set and byte-count rule was not located.

---

## Cross-References

- [UHI / OCI / ICI / General-DMA Payloads](payload-uhi-oci-ici-dma.md) — the sibling DMA and ICI band decodes; this page keeps the ICR node-fabric bands (48/50/51/91) distinct from those.
- [TracePoints Master Registry](tracepoints-master-registry.md) — the master band-ID registry; the home of all trace-point ids including these four.
- [Trace Entries Coder](trace-entries-coder.md) — the 16-byte packet, `TraceHeader`, and `TraceIdHeader` layout these payloads decode on top of.
- [DMA Endpoint Rendering](dma-endpoint-rendering.md) — the parallel DMA-rendering path; cross-link for the host-DMA (MemcpyH2D/D2H, tags 6/7) arms this band shares the renderer with.
- [Trace Entry to XEvent](trace-entry-to-xevent.md) — the general entry→XEvent conversion frame this DMA pass specializes.
