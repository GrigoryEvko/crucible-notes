# DMA Endpoint Rendering

> *All addresses on this page apply to `libtpu.so` from the wheel `libtpu-0.0.40` (build-id `89edbbe81c5b328a958fe628a9f2207d` — the unambiguous anchor; the runtime-reported `0.103` is not statically verifiable in the binary). Other builds will differ. All offsets are VMA; `.text` and `.rodata` are mapped VMA == file offset; `.data.rel.ro` is VMA − 0x200000.*

## Abstract

A DMA descriptor on a TPU carries a full pair of endpoints — a *source* memory space, a *destination* memory space, an opcode at each end, and a set of sync-flag targets. Each endpoint is a `(mem_id, core_id)` tuple drawn from the on-chip address map: HBM, the TensorCore VMEM/SMEM/IMEM banks, the BarnaCore / SparseCore local memories, CMEM, and so on. The hardware names these compactly: a 2-bit `mem_id` selects a memory *class*, and a companion 3-bit `core_id` disambiguates *which* core's instance of that class. The profiler descriptor message `OciDescriptorCommonIssuedFromTcs` (node-fabric trace id 91) decodes all of this into a fully-typed proto with eleven nested enums.

This page owns those endpoint enums and the question of whether they reach a trace label. The answer is the central, counter-intuitive fact: **on the pxc (deepsea) generation they do not.** The DMA-timeline rendering pass `ConvertDmaTransfersToXPlane` at `0xf254bc0` builds exactly eight `XStat`s per span — none of which is a source/destination memory space or address. The descriptor's `src_mem_*`, `dst_mem_*`, `src_opcode`, `dst_opcode`, and three sync-flag fields are parsed into the proto by the `TcParseTable` but are never read by the producer that fills the `DmaTransfer` span, and never symbolized into a display string. A whole-unit symbol scan finds no `mem_id → name`, `node_type → component`, or DMA-endpoint symbolizer linked into `libtpu.so`. The endpoint enums are therefore a *decode contract* the binary honours, not a *render contract* it executes — the renderer reduces every endpoint to a byte count, a bandwidth string, and a flow arrow.

So this page does two things. First, it establishes the full value→name tables for every endpoint enum on the id-91 descriptor and the companion id-50/51 OCI messages and id-48 ICI packet, byte-verified against the carved `FileDescriptorProto` pool, including the polymorphic memory-class rename across the five silicon generations. Second, it documents the renderer end-to-end: the eight per-span `XStat`s, the kind-tag → lane/event-name dispatch, the GTC-tick → picosecond timebase in `TpuXLineBuilder::AddEvent` (`0xf1df1e0`), and the five-rung `%.2f{B,KB,MB,GB,TB}/s` bandwidth ladder — so that what *is* rendered, and what is provably dropped, are both reimplementable. The raw band bit-layout and the four-id pairing logic live on the sibling [ICR Node-Fabric DMA Timeline Band](icr-dma-timeline-band.md); this page picks up at the merged `DmaTransfer` span and the enums behind its endpoints.

For reimplementation, the contract is:

- **The endpoint enum tables** — `DmaType`, `SrcMem/DstMem MemId`, `SrcMem/DstMem CoreId`, `Src/Dst Opcode`, the three `SyncFlagCoreId` sets, and `LengthGranule` on the id-91 descriptor; `MsgType`/`Opcode`/`NodeType` on the id-50/51 OCI messages; `RouterLinkPortId` on id-48 — value→name, byte-anchored.
- **The memory-class composite rule** — each `mem_id` value name is a `_`-joined composite (`HBM_TCVMEM_BCBMEM`); the displayed space is the segment picked by the companion `core_id` (NONCORE → 1st, TC → 2nd, BC/SC → 3rd). Inferred from the name structure; no selector executes it in `libtpu.so`.
- **The eight rendered `XStat`s** and their byte sources on the 0x58-byte `DmaTransfer` span.
- **The drop** — which descriptor/message fields are decoded into the proto but never copied into the span.
- **The timebase and bandwidth math** — `ps = round((gtc & ~0xf) · 1e9 / (clk·16))` and the threshold ladder.

| | |
|---|---|
| **Lane renderer** | `0xf254bc0` — `xprof::tpu::(anon)::ConvertDmaTransfersToXPlane(absl::Span<const DmaTransfer>, TpuXPlaneBuilder*)` |
| **Event adder / timebase** | `0xf1df1e0` — `xprof::TpuXLineBuilder::AddEvent(tpu::GtcSpan, const XEventMetadata&)` |
| **Span producer (id-91 store)** | `0xf26c6e0` — `ConvertTpuTraceToXPlane<pxc>::{lambda#1}::{lambda#3}` |
| **id-91 descriptor proto** | `OciDescriptorCommonIssuedFromTcs`, 17 fields, 11 nested enums; FDP body @ `0xbef9426` (len 3655) |
| **id-50/51 OCI messages** | `OciMessageGeneratedInIcrEgressDma` @ `0xbef7b3b`; `…IngressDma` @ `0xbef7e5f` |
| **id-48 ICI packet** | `IciPacketDataPacketQueuedForLocalIngress` @ `0xbef6b17` |
| **Span type** | `DmaTransfer`, 0x58 (88) bytes; `push_back` @ `0xf2547e0` |
| **Endpoint renderer** | *none* — no `mem_id`/`node_type` symbolizer is linked into the unit |

---

## At a Glance — What Survives the Render

The descriptor carries seventeen fields; the rendered XEvent carries eight stats. The table below is the whole story of the data flow: every column on the left is *decoded*, only the bottom three rows survive into a *label*.

| Descriptor / message field | Decoded into proto | Reaches an XStat |
|---|---|---|
| `src_mem_mem_id` / `src_mem_core_id` / `src_opcode` (f3–f5) | yes | **no** |
| `dst_mem_mem_id` / `dst_mem_core_id` / `dst_opcode` (f6–f8) | yes | **no** |
| `src_sync_flag_*` / `dst_sync_flag_0_*` / `dst_sync_flag_1_*` (f9–f14) | yes | **no** |
| `program_counter` (f15) | yes | **no** |
| id-50/51 `addr` / `node_type` / `msg_type` / `opcode` | yes | **no** |
| id-48 `router_link_port_id` / `virtual_channel` / `dst_chip_id` | yes | **no** |
| `length` (f16) × `length_granule` (f17) → byte count | yes | **`bytes_transferred` (78)** |
| derived: bytes ÷ duration | — | **`bandwidth` (string)** |
| derived: GTC span | — | **`offset_ps` / `duration_ps` + `flow` (56) + `_a` (42)** |

> **GOTCHA —** the page title is descriptive of the *enums*, not of an executed renderer. A reimplementer who expects `ConvertDmaTransfersToXPlane` to emit a `"HBM → VMEM"`-style endpoint label will find no such code. The `queue` (StatType 79) and `details` string XStats *are* attached to every span, but their source strings live at `DmaTransfer+0x28` and `+0x40` and are **zero-initialized and never written** by the pxc producer — so they render as empty strings. The endpoint enums below describe a wire format whose symbolizer is not in this translation unit (see [Considerations](#considerations--where-the-endpoints-go)).

---

## The id-91 Descriptor Endpoint Enums

### Purpose

`OciDescriptorCommonIssuedFromTcs` is the proto the profiler decodes for trace-point id 91 — the descriptor a TensorCore sequencer issues to start a node-fabric DMA. It is the richest of the four node-fabric messages: it names both endpoints, both opcodes, three sync-flag targets, the transfer length, and the `dma_type` that gates the egress lane. Its eleven nested enums are the authoritative source/destination memory-space vocabulary. They are byte-verified by walking the `EnumDescriptorProto` blocks inside the carved `FileDescriptorProto` body at `0xbef9426`; the value strings sit consecutively in `.rodata` from `0xbef9a0f` onward, and every value's protobuf `number` is decoded from the FDP `EnumValueDescriptorProto.number` field, not assumed from order.

### Field Layout

The producer and parser address the message at fixed C++ offsets. The seventeen fields, in declaration (and tag) order:

| Field | Tag | C++ off | Type | Role |
|---|---|---|---|---|
| `trace_id_header` | 1 | +0x18 | submessage | yields the 38-bit `dma_id` pairing key |
| `dma_type` | 2 | +0x20 | `DmaTypeValues` | egress-lane gate (`== REMOTEUNICAST`) |
| `src_mem_mem_id` | 3 | +0x24 | `SrcMemMemIdValues` | source memory class |
| `src_mem_core_id` | 4 | +0x28 | `SrcMemCoreIdValues` | source core selector |
| `src_opcode` | 5 | +0x2c | `SrcOpcodeValues` | source-side opcode |
| `dst_mem_mem_id` | 6 | +0x30 | `DstMemMemIdValues` | destination memory class |
| `dst_mem_core_id` | 7 | +0x34 | `DstMemCoreIdValues` | destination core selector |
| `dst_opcode` | 8 | +0x38 | `DstOpcodeValues` | destination-side opcode |
| `src_sync_flag_id` | 9 | +0x3c | uint32 | source sync-flag index |
| `src_sync_flag_core_id` | 10 | +0x40 | `SrcSyncFlagCoreIdValues` | source sync-flag core |
| `dst_sync_flag_0_id` | 11 | +0x44 | uint32 | dst sync-flag 0 index |
| `dst_sync_flag_0_core_id` | 12 | +0x48 | `DstSyncFlag0CoreIdValues` | dst sync-flag 0 core |
| `dst_sync_flag_1_id` | 13 | +0x4c | uint32 | dst sync-flag 1 index |
| `dst_sync_flag_1_core_id` | 14 | +0x50 | `DstSyncFlag1CoreIdValues` | dst sync-flag 1 core |
| `program_counter` | 15 | +0x54 | uint32 | issuing PC |
| `length` | 16 | +0x58 | uint32 | transfer length (granule units) |
| `length_granule` | 17 | +0x5c | `LengthGranuleValues` | granule selector for `length` |

> **NOTE —** fields f3–f15 (every endpoint, opcode, sync-flag, and the PC) are the fields the rendering pass drops. Only f1 (for the `dma_id`), f16, and f17 are read by the producer. The C++ offsets are recovered from the producer store at `0xf26c6e0` (`length` = descriptor dword 22 = +0x58; `length_granule` = dword 23 = +0x5c) and the message's `TcParseTable` field order.

### `DmaType` — the egress gate (f2)

The transfer class; the timeline producer keeps a span only when this is `REMOTEUNICAST` (value 2), the inter-chip-router direction. Byte-verified value numbers:

| Value | Name (pxc) | Meaning |
|---|---|---|
| 0 | `DMA_TYPE_LOCAL` | within-chip DMA |
| 1 | `DMA_TYPE_CHIP2HOST` | chip → host |
| 2 | `DMA_TYPE_REMOTEUNICAST` | chip → one remote chip (the ICI band) |
| 3 | `DMA_TYPE_REMOTEMULTICAST` | chip → many remote chips |

### `SrcMemMemId` / `DstMemMemId` — the memory class (f3 / f6)

A 2-bit class. Each value *name* is a composite of one memory-space name per core class, joined by `_`. The companion `core_id` (next section) picks which segment applies. The `Src` and `Dst` tables are identical apart from the prefix; pxc values:

| Value | `SRC_MEM_MEM_ID_…` / `DST_MEM_MEM_ID_…` (pxc) | NONCORE seg | TC seg | BC seg |
|---|---|---|---|---|
| 0 | `HBM_TCVMEM_BCBMEM` | HBM | TC VMEM | BC BMEM |
| 1 | `RSVD_TCSMEM_BCSMEM` | (reserved) | TC SMEM | BC SMEM |
| 2 | `CMEM_TCIMEM_BCBIMEM` | CMEM | TC IMEM | BC BIMEM |
| 3 | `RSVD_RSVD_BCVIMEM` | (reserved) | (reserved) | BC VIMEM |

### `SrcMemCoreId` / `DstMemCoreId` — the core selector (f4 / f7)

A 3-bit selector that disambiguates the composite memory-class name. Identical `Src`/`Dst` tables; pxc values:

| Value | `…_MEM_CORE_ID_…` | Selects MemId segment |
|---|---|---|
| 0 | `RESERVED` | — |
| 1 | `NONCORE` | 1st (HBM / CMEM / reserved) |
| 2 | `TC0` | 2nd (TCVMEM / TCSMEM / TCIMEM) |
| 3 | `TC1` | 2nd |
| 4 | `BC0` | 3rd (BCBMEM / BCSMEM / BCBIMEM / BCVIMEM) |
| 5 | `BC1` | 3rd |
| 6 | `BC2` | 3rd |
| 7 | `BC3` | 3rd |

> **QUIRK —** the full endpoint space is *the cross product*, not either table alone. A 2-bit `mem_id` × the 3-segment `core_id` class encodes the entire HBM / VMEM / SMEM / IMEM / BMEM / VIMEM / CMEM address map in five bits. `src_mem_mem_id = 0, src_mem_core_id = 2` reads as "TC0 VMEM"; the *same* `mem_id = 0` with `core_id = 1` reads as "HBM". A reimplementation that symbolizes the `mem_id` alone — ignoring the `core_id` segment pick — produces a wrong endpoint for every non-NONCORE transfer.

### `SrcOpcode` / `DstOpcode` — per-end opcode (f5 / f8)

The source and destination tables differ. pxc values:

| Value | `SRC_OPCODE_…` | `DST_OPCODE_…` |
|---|---|---|
| 0 | `READ` | `WRITE` |
| 1 | `RESERVED` | `RESERVED` |
| 2 | `INSTRUCTIONMEMSET` | `WRITESPECIAL0` |
| 3 | `DATAMEMSET` | `WRITESPECIAL1` |

### Sync-flag core enums (f10 / f12 / f14)

The three sync-flag *core* fields each carry their own 8-value enum — `SrcSyncFlagCoreIdValues`, `DstSyncFlag0CoreIdValues`, `DstSyncFlag1CoreIdValues`. All three share the value sequence `RESERVED, NONCORE, TC0, TC1, BC0, BC1, BC2, BC3` (numbers 0–7), the same shape as the mem-core enum.

> **NOTE —** the three sync-flag core enums are three *distinct* `EnumDescriptorProto`s, not one shared type with `SrcMemCoreId`. Each has its own prefixed value names (`SRC_SYNC_FLAG_CORE_ID_TC0`, `DST_SYNC_FLAG_0_CORE_ID_TC0`, `DST_SYNC_FLAG_1_CORE_ID_TC0`), nested under the descriptor message at `0xbef9eb5`, `0xbef9fd2`, `0xbefa100`. The *value→number binding* is identical across all three; the *enum types* are not. A reimplementation that interns them as one type loses three message-distinct names.

### `LengthGranule` — the byte-count shift (f17)

The selector that turns `length` into a byte count. Byte-verified:

| Value | Name | `length` shift | Byte count |
|---|---|---|---|
| 0 | `LENGTH_GRANULE_512B` | `<< 9` | `length × 512` |
| 1 | `LENGTH_GRANULE_4B` | `<< 2` | `length × 4` |

This is the *only* descriptor enum the renderer consumes: the producer reads `length_granule` to pick the shift and `length` to compute `bytes_transferred`. See [The Producer Store](#the-producer-store--what-it-reads).

---

## The id-50/51 and id-48 Endpoint Enums

### Purpose

The other three node-fabric trace points carry their own endpoint vocabulary. id-50/51 are the OCI messages generated in the ICR (`OciMessageGeneratedInIcrEgressDma` / `…IngressDma`) — both byte-identical enum sets. id-48 is the ICI data packet. As with the descriptor, the producer reads only the timing/byte-count fields; the endpoint enums here are decoded but dropped.

### OCI message enums (id-50 / id-51)

Byte-verified from the egress message FDP body at `0xbef7b3b` (the ingress body at `0xbef7e5f` is identical):

| Enum (C++ off) | Values |
|---|---|
| `MsgType` (+0x28) | 0 `MSG_TYPE_PRIVATE`, 1 `MSG_TYPE_PUBLIC` |
| `Opcode` (+0x2c) | 0 `WRITE_NO_DONE`, 1 `WRITE_WITH_DONE`, 2 `INC_NO_DONE`, 3 `INC_WITH_DONE` |
| `NodeType` (+0x30) | 0 `TCS`, 1 `BC`, 2 `CMQ`, 3 `HBMQ`, 4 `UHI`, 5 `ICR`, 6 `QNM` |

> **NOTE —** `NodeType` is the node-fabric component vocabulary (the same one the OCI command bands use). It is the closest thing in these messages to a routing endpoint, and it is the field a hypothetical `node_type → TpuComponent` lane symbolizer would consume. No such symbolizer is linked; `NodeType` is parsed and dropped on every id-50/51 span. The id-51 (ingress) producer reads only `msg_data` (+0x20, for the `<< 9` byte-count accumulate); id-50 (egress) reads only `done` (+0x24).

### ICI packet enum (id-48)

Byte-verified from `IciPacketDataPacketQueuedForLocalIngress` at `0xbef6b17`:

| Enum (C++ off) | Values |
|---|---|
| `RouterLinkPortId` (+0x20) | 0 `LINK0` … 5 `LINK5` (`ROUTER_LINK_PORT_ID_LINK0..5`) |

The producer reads only `first_packet_in_dma` (+0x32) and `last_packet_in_dma` (+0x33) — the begin/end markers — and drops `router_link_port_id`, `virtual_channel`, the link-target mask, and `dst_chip_id`.

---

## Cross-Generation Memory-ID Rename

### Purpose

`OciDescriptorCommonIssuedFromTcs` exists in all five generations the unit ships (`pxc`, `vfc`, `vlc`, `glc`, `gfc`). The mem-id composite is *polymorphic*: its third segment tracks the gen's third core class. pxc has BarnaCore (BC); the vfc/glc/gfc family has SparseCore (SC); vlc has no third core at all and ships a 2-segment name. The value *numbers* stay 0–3; only the names change. Recovered by enumerating the `SRC_MEM_MEM_ID_*` / `DST_MEM_MEM_ID_*` strings across the per-gen FDP pools.

### The rename, by generation

| `mem_id` | pxc (BarnaCore) | vfc / glc / gfc (SparseCore) | vlc (no SC) |
|---|---|---|---|
| 0 | `HBM_TCVMEM_BCBMEM` | `HBM_TCVMEM_SCSPMEM` | `HBM_TCVMEM` |
| 1 | `RSVD_TCSMEM_BCSMEM` | `HOST_TCSMEM_SCSMEM` | `HOST_TCSMEM` |
| 2 | `CMEM_TCIMEM_BCBIMEM` | `VMEMALL_TCIMEM_SCSIMEM` | `NONCORERESERVEDMEM0_TCIMEM` |
| 3 | `RSVD_RSVD_BCVIMEM` | `NONCORERESERVEDMEM0_TCRESERVEDMEM_SCTIMEM` | `NONCORERESERVEDMEM0_TCRESERVEDMEM` |

The `core_id` enum gains `SC0..SC3` (replacing `BC0..BC3`) on the SparseCore gens; the SparseCore strings `SRC_MEM_CORE_ID_SC0..SC3` are present in the unit. The `dma_type` enum also collapses on the newer gens: pxc has the four-value `{LOCAL, CHIP2HOST, REMOTEUNICAST, REMOTEMULTICAST}`, while the SparseCore family's descriptor `DmaTypeValues` collapses to a two-value `{LOCALORHOST = 0, REMOTEUNICAST = 1}` (strings `DMA_TYPE_LOCALORHOST` @ `0xbf07208`, `DMA_TYPE_REMOTEUNICAST` @ `0xbf07222` present in the `protodesc_cold` pool).

> **QUIRK —** the rename is *string-only*; the proto field numbers, C++ offsets, and the 2-bit/3-bit widths are stable across all five gens. A single decoder handles every gen by value number; only the *display* name table is per-gen. This is why a reimplementer should key the symbolizer on `(gen, mem_id, core_id_segment)`, never on the value-name string.

### The segment-selection rule (inferred)

The displayed memory space is the segment of the `mem_id` composite picked by the companion `core_id`:

```text
core_id == NONCORE          -> 1st segment   (HBM / HOST / CMEM / VMEMALL / reserved)
core_id in {TC0, TC1}       -> 2nd segment   (TCVMEM / TCSMEM / TCIMEM / TCRESERVEDMEM)
core_id in {BC0..BC3}       -> 3rd segment   (pxc:  BCBMEM / BCSMEM / BCBIMEM / BCVIMEM)
core_id in {SC0..SC3}       -> 3rd segment   (SC:   SCSPMEM / SCSMEM / SCSIMEM / SCTIMEM)
```

> **GOTCHA —** this rule is **inferred from the name structure, not byte-proven from a selector.** No code in `libtpu.so` splits a `mem_id` composite by `core_id` — the pxc pass drops both fields before any such split could happen. The rule is structurally consistent across all five gens (the segment count always matches the gen's core classes), but the string-splitting selector lives in the downstream xprof/TensorBoard UI, outside this unit, and is not verifiable here. Treat it as the most likely interpretation, not a confirmed algorithm.

---

## The Renderer

### Purpose

`ConvertDmaTransfersToXPlane` (`0xf254bc0`) turns a span of merged `DmaTransfer` records into device-XPlane XEvents. It is the *only* DMA-transfer XPlane pass for the deepsea gens (the jxc legacy gen has a separate `ConvertDmaEndsToXPlane` at `0xf25c500`). It sets up four timeline lanes and four event-metadata names up front, then walks the span, and for each valid record emits one XEvent on the lane its kind tag selects.

### Setup — Lanes, Event Names, Stat Names

Before the per-span loop, the function interns its metadata in a fixed order:

```c
function ConvertDmaTransfersToXPlane(transfers, plane):     // 0xf254bc0
    // Four timeline lanes, each named via TpuComponentName().
    line63 = GetOrCreateLine(plane, 63)    // "MemcpyH2D"      (host -> device)
    line64 = GetOrCreateLine(plane, 64)    // "MemcpyD2H"      (device -> host)
    line54 = GetOrCreateLine(plane, 54)    // "From ICI Router"  (ingress)
    line55 = GetOrCreateLine(plane, 55)    // "To ICI Router"    (egress)

    // Four XEvent names (the title shown on the span).
    evtH2D  = GetOrCreateEventMetadata(plane, "MemcpyH2D")
    evtD2H  = GetOrCreateEventMetadata(plane, "MemcpyD2H")
    evtIn   = GetOrCreateEventMetadata(plane, "ICI Ingress")
    evtEg   = GetOrCreateEventMetadata(plane, "ICI Egress")

    // Six explicit XStat metadata handles (StatType id or literal name).
    stBytes = GetOrCreateStatMetadata(plane, GetStatTypeStr(78))   // bytes_transferred
    st_a    = GetOrCreateStatMetadata(plane, GetStatTypeStr(42))   // group_id ("_a")
    stFlow  = GetOrCreateStatMetadata(plane, GetStatTypeStr(56))   // flow
    stQueue = GetOrCreateStatMetadata(plane, GetStatTypeStr(79))   // queue
    stDet   = GetOrCreateStatMetadata(plane, "details")            // literal
    stBw    = GetOrCreateStatMetadata(plane, "bandwidth")          // literal
```

The lane numbers (54/55/63/64) and their names resolve through `xprof::TpuComponentName`; the lane-name catalog is owned by the device-plane line reference. The sibling [ICR Node-Fabric DMA Timeline Band](icr-dma-timeline-band.md) covers lanes 54/55 in detail.

### The Per-Span Loop and Kind Dispatch

Each `DmaTransfer` is 0x58 bytes. The loop addresses the record via `RBX` at `record + 0x40`, so a field at struct offset `k` reads as `[RBX - 0x40 + k]`. A span is rendered only when its byte count is non-zero and both begin and end markers are present:

```c
for record in transfers:                                 // 0xf25502e
    if byte_count[+0x20] == 0:  continue                 // [RBX-0x20]
    if begin_present[+0x08] != 1: continue               // [RBX-0x38]
    if end_present[+0x18]   != 1: continue               // [RBX-0x28]

    switch (kind_tag[+0x38]):                            // [RBX-0x08], jt @ 0xab589bc
        case 2: event = evtIn,  lane = line64; break     // "ICI Ingress"
        case 3: event = evtEg,  lane = line54; break     // "ICI Egress"
        case 6: event = evtH2D, lane = line63; break     // "MemcpyH2D"  (default arm)
        case 7: event = evtD2H, lane = line63; break     // "MemcpyD2H"
        default: continue                                // tags 4,5 skipped

    begin = begin_gtc[+0x00]                             // [RBX-0x40]
    end   = end_gtc[+0x10]                               // [RBX-0x30]
    if end <= begin:  continue                           // zero/negative span dropped
    xevent = AddEvent(lane, GtcSpan{begin, end-begin}, event)   // 0xf1df1e0
    AttachStats(xevent, record)                          // the six explicit stats
```

The kind tags pair with the sibling page's node-fabric directions: tag 2 = ingress ("ICI Ingress"), tag 3 = egress ("ICI Egress"); tags 6/7 are the host-DMA memcpy arms. The pxc producer only ever writes tags 2 and 3 — the memcpy arms exist for the shared `Convert` but are not exercised on a captured pxc XSpace.

> **NOTE —** the `switch` ordinal is `kind_tag − 2` indexed through a jump table at `0xab589bc`; tags 0/1 are out of range and tags 4/5 fall through to the no-op default. Each arm also guards on its event-metadata handle being non-null, so a lane whose metadata failed to intern is silently skipped rather than crashing.

### The Eight XStats

`AddEvent` stamps two universal int64 stats (the ps timestamps); the loop body adds six more. The complete set, with the byte source on the span:

| XStat | StatType | value case | Source | Site |
|---|---|---|---|---|
| `offset_ps` | (device offset) | 3 int64 | `round(begin_gtc & ~0xf · 1e9 / (clk·16))` | `AddEvent` |
| `duration_ps` | (device duration) | 3 int64 | `round((end−begin masked) · 1e9 / (clk·16))` | `AddEvent` |
| `bytes_transferred` | 78 | 3 int64 | `byte_count[+0x20]` | `0xf2550ab` |
| `queue` | 79 | 1 string | string @ `+0x28` / len `+0x30` — **empty on pxc** | `0xf255121` |
| `details` | — | 1 string | `std::string` @ `+0x40` (SSO flag `+0x57`) — **empty on pxc** | `0xf255181` |
| `_a` (`group_id`) | 42 | 4 uint64 | constant `1` (per-DMA aggregate marker) | `0xf2551f4` |
| `flow` | 56 | 3 int64 | `(XFlow::next_flow_id_++  & 0xFF…FF) · 4 + 3` | `0xf255203` |
| `bandwidth` | — | 5 string | `StrFormat(unit, bytes / (duration_ps/1e12))` | `0xf255411` |

> **QUIRK —** `queue` (79) and `details` are *always attached* but *always empty* on pxc. The producer's id-91 store (`0xf26c865`) zero-fills the span (`vpxor` + three `vmovdqu` of a zero ymm over `+0x08..+0x60`) and then writes only `begin_gtc`, the present-flags, `byte_count`, and the kind tag — it never writes a string into `+0x28` or `+0x40`. A reimplementer must still emit the two stats (a downstream consumer keys on their presence) but with empty values. The `flow` id is masked to 56 bits then `<<2 | 3` to tag it as a flow-kind 3 (begin↔end DMA arrow); `_a = 1` marks the span for per-DMA aggregation.

### The Producer Store — What It Reads

The span producer (`ConvertTpuTraceToXPlane<pxc>`, third nested lambda at `0xf26c6e0`) is where the endpoint drop is provable. For an id-91 descriptor it executes, in decompiled form:

```c
// id-91 store, @ ~0xf26c865 inside the producer lambda
zero_fill(span + 0x08, 0x58)               // vpxor + 3× vmovdqu ymm
span.begin_gtc[+0x00] = begin_present_ts
span.begin_present[+0x08] = 1
shift = (descr.length_granule[+0x5c] == 0) ? 9 : 2     // dword 23
span.byte_count[+0x20] = (u64)descr.length[+0x58] << shift   // dword 22
span.kind_tag[+0x38] = 3                    // egress
// NO read of descr+0x24..0x50 — every src/dst endpoint, opcode,
// sync-flag, and PC field is skipped.
```

A grep of the entire producer for `src_mem`, `dst_mem`, `mem_id`, `mem_core`, `opcode`, and `sync_flag` returns zero references. The fields are parsed by the message's `TcParseTable` into the proto, then abandoned. This is the byte-level proof behind the [At a Glance](#at-a-glance--what-survives-the-render) "no" column.

---

## The Timebase and Bandwidth Math

### GTC → picoseconds (`AddEvent`, `0xf1df1e0`)

Every device XEvent timestamp is a GTC (global time counter) tick converted to picoseconds. The raw tick counts in units of `clk/16`, so the conversion masks off the low four fractional bits, scales by 1e9, and divides by `clk·16` with round-to-nearest. The math, byte-exact:

```c
function AddEvent(line, GtcSpan{begin, dur}, meta):    // 0xf1df1e0
    clk  = *(*(meta.converter[+0x10]))          // CycleConverter tick rate
    div  = clk << 4                             // clk · 16  (the /16 fractional convention)
    half = div >> 1                             // round-to-nearest addend

    // offset: mask the begin tick's 4 fractional bits, scale, divide.
    offset_ps   = (( (begin & ~0xf) * 1e9 ) + half) / div        // 128-bit udiv (_udivti3 @0x21213680)

    // duration: end = begin + dur; subtract the *masked* begin, re-mask to bits[4:44].
    end_masked  = ((begin + dur) - (begin & 0x1FFFFFFFFFF0)) & 0x1FFFFFFFFFF0
    duration_ps = (( end_masked * 1e9 ) + half) / div            // same div / half

    // stored both as native XEvent fields and as two int64 XStats (value case 3)
    xevent.offset_ps[+0x38]   = offset_ps
    xevent.duration_ps[+0x30] = duration_ps
```

> **NOTE —** the begin-offset and duration paths use **two distinct masks**. The begin-offset numerator masks with `~0xf` (`0xFFFFFFFFFFFFFFF0`, clearing only the low 4 bits) at the offset multiply, while the duration path masks the end with `0x1FFFFFFFFFF0` (bits 4–44, a 41-bit window). For in-range GTC values they coincide, but a reimplementation must use both masks to be byte-faithful: `begin & 0xFFFFFFFFFFFFFFF0`, `end & 0x1FFFFFFFFFF0`.

`1e9` appears as the immediate `0x3B9ACA00`; the round-half (`+ div/2`) is folded into the 128-bit numerator before the unsigned 128-bit divide. The two stats are stamped through the converter's pre-interned metadata (`meta+0x38` offset, `meta+0x40` duration). The `clk` provenance — the per-silicon nominal GTC frequency that fills `CycleConverter+0x10` — is a separate decode (the `TpuVersion → GTC-Hz` table) not on this page.

### The bandwidth ladder (`0xf25527b..0xf255411`)

After `AddEvent` returns the XEvent, the renderer computes a bandwidth string. Both the byte count and the duration are converted `u64 → f64` via the standard `vunpcklps` / `vsubpd` magic-constant trick, then divided:

```c
bytes_d = (double) byte_count[+0x20]
dur_s   = (double) duration_ps[+0x30] / 1e12     // ps -> seconds
bw_Bps  = bytes_d / dur_s                         // bytes per second

// 5-rung threshold ladder (vucomisd / jae): pick the largest unit <= bw,
// then divide bw by that unit's threshold.
if      bw_Bps >= 1e12:  s = StrFormat("%.2fTB/s", bw_Bps / 1e12)   // @0xa2dfd18
else if bw_Bps >= 1e9 :  s = StrFormat("%.2fGB/s", bw_Bps / 1e9 )   // @0xa2de620
else if bw_Bps >= 1e6 :  s = StrFormat("%.2fMB/s", bw_Bps / 1e6 )   // @0xa2e0208
else if bw_Bps >= 1e3 :  s = StrFormat("%.2fKB/s", bw_Bps / 1e3 )   // @0xa2e0430
else                  :  s = StrFormat("%.2fB/s",  bw_Bps      )
set_stat(xevent, stBw, s)                          // value case 5 (string)
```

The four `double` thresholds are verified at `0xa2dfd18` (1e12), `0xa2de620` (1e9), `0xa2e0208` (1e6), `0xa2e0430` (1e3); the five format strings `%.2f{TB,GB,MB,KB,B}/s` are interned in `.rodata` at `0x857a5f4 / 0x857a60f / 0x857a5fd / 0x857a606 / 0x857a5ec`. This is the single human-readable summary of a DMA span — and, notably, the one place the endpoint *bandwidth* surfaces without the endpoint *names*.

---

## Considerations — Where the Endpoints Go

The endpoint enums are a complete, byte-faithful wire contract — and on the pxc DMA-timeline pass they have no consumer. Three facts bound what a reimplementer can conclude:

1. **The pxc pass drops them.** Proven at the producer store (`0xf26c6e0`): it reads `length`, `length_granule`, and the `dma_id`, and nothing in `descr+0x24..0x50`. The merged span has no field for a memory space, opcode, or address — its layout is `{begin, present, end, present, byte_count, queue-ptr/len, kind_tag, details-string}`, all 0x58 bytes accounted for.

2. **No symbolizer is linked.** A whole-unit symbol scan finds no `mem_id → name`, `node_type → TpuComponent`, or DMA-endpoint stringifier. The only DMA-XPlane passes in the unit are this `ConvertDmaTransfersToXPlane` (pxc, endpoints dropped) and the jxc `DeriveHostDmaTransfers` / `ConvertDmaEndsToXPlane` pair (`0xf25c500`), which builds its timeline from a different `SyncFlagUpdate` structure.

3. **The downstream is out of scope.** Whether the xprof / TensorBoard UI that *consumes* the emitted `XSpace` re-reads the original proto fields to build a routing display is not decidable from `libtpu.so` — that code is not in this binary. The endpoint enum tables on this page are exactly what such a downstream consumer would need; this page provides them, and proves that `libtpu.so` itself does not use them.

The honest summary: the rendered ICI DMA span shows offset/duration in ps, a byte count, a bandwidth string, a flow arrow, and an aggregate marker — never a source or destination memory space. The endpoint enums are decoded but unrendered, and the `queue`/`details` stats that *could* carry an endpoint label are empty on pxc.

---

## Cross-References

- [ICR Node-Fabric DMA Timeline Band](icr-dma-timeline-band.md) — the four-id `{48,50,51,91}` pairing, the `dma_id` extractor (`GetDmaId` `0xf699ca0`), and the band bit-layout that feeds the merged `DmaTransfer` this page renders
- [Trace Payload: UHI / OCI / ICI / DMA Bands](payload-uhi-oci-ici-dma.md) — the raw on-wire field decode of the OCI / ICI / DMA payload bands these endpoints come from
- [TraceEntry to XEvent/XStat](trace-entry-to-xevent.md) — the `CoreDispatcher::Dispatch` path that routes a decoded trace entry to the producer lambdas
- [XStat Metadata IDs](xstat-metadata-ids.md) — the StatType id → name table behind `GetStatTypeStr(78/42/56/79)`
- [XEvent Metadata IDs](xevent-metadata-ids.md) — the event-name interning behind `GetOrCreateEventMetadata`
- [Profiling and Telemetry](overview.md) — the profiler subsystem overview and the device-XPlane lane catalog
