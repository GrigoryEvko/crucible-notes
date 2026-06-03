# nf_descriptor (27-Field Node-Fabric DMA Descriptor)

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). The binary carries full C++ symbols; `.text` VMA == file offset, base `0xe63c000`. Other versions will differ.*

## Abstract

`nf_descriptor_trace_entry` is the on-wire trace of one **staged Node-Fabric DMA descriptor**: the record the deepsea fabric stages to move a buffer from a source endpoint on one chip to a destination endpoint on another (or several others, for multicast). "Node-Fabric" (nf) is the inter-node DMA layer that sits below the routing schedule — the route table answers "which output link for this destination chip" and the net_router schedule answers "at step k, DMA this src→dst", but it is the nf_descriptor that names the *actual* `{src_offset, src_resource, src_node, src_chip}` → `{dst_offset, dst_resource, dst_node, dst_chip}` transfer, its length, its multicast/segmented flags, and the three independent SyncFlag completions it raises. This descriptor is the jxc analog of the deepsea OCI SHAPE-B `DmaDescriptor` and the on-the-wire form of the `jxc::DmaDescriptor` V1 record.

The record is a proto2 message `asic_sw::driver::deepsea::jxc::nf_descriptor_trace_entry`, case 3 of the `PerformanceTraceEntry` oneof. It has **27 fields** — all `uint32` except field 1 (`id`, a `TracePoint` enum) and field 4 (`descriptor_source`, a `descriptor_source_value` enum). The C++ in-memory layout is *not* in proto field order: fields 1–3 and 5–27 are laid out contiguously from offset `0x18` to `0x7c`, but field 4 (`descriptor_source`) is parked out of line at offset `0x80` with a non-zero in-memory default of `1` (`BARNA_CORE`). Both facts are re-derived twice below — once from the serializer's offset/wire-tag pairs and once from `::Clear`'s zeroing blocks.

This page documents the 27-field layout field-by-field with authoritative C++ offsets; the three SyncFlag-update channels (`Source` = "buffer free", `Destination` = "data arrived", `Ack` = "completion") and their shared 23-bit `SyncFlagTarget` pack; the HIB host-DMA channel (fields 26/27); the `GetDmaSize` / `GetDmaTransactionId` accessors; the related `bcs_internal` BarnaCore-sequencer band; and the **deepsea `GetDmaId` bit codec** that proves the cross-gen DMA-pairing key is the same logical `(tag, resource/core, node, chip)` tuple, widened on the modern silicon. The ICR DMA *bands* that carry these descriptors live on [ICR Node-Fabric DMA](icr-node-fabric-dma.md); the routing table that resolves the multi-hop link path lives on [Route-Table Generation](route-table-generation.md).

For reimplementation, the contract is:

- The **27-field record**: every proto number, name, type, wire tag, and C++ in-memory offset, including the out-of-line `descriptor_source` at `0x80` with its `BARNA_CORE` default.
- The **three SyncFlag channels** and their **23-bit target pack** `{sync_flag[0:10] | resource[10] | node[11] | chip[12:23]}`, with the explicit Destination pack and the `Source`/`Ack` OCI fold that produces the identical layout.
- The **HIB channel** (fields 26/27) that gates whether the descriptor also raises a host-interface flag/ack.
- The **deepsea `GetDmaId` 38-bit composite key** `transaction_id[0:21] | core_id[21:24] | chip_id[24:38]`, cross-aligned field-for-field to the jxc 27-bit key, proving the cross-gen pairing equivalence and the chip-field 11→14 widening.

| | |
|---|---|
| **Message** | `asic_sw::driver::deepsea::jxc::nf_descriptor_trace_entry` — `PerformanceTraceEntry` oneof case 3 |
| **Field count** | 27 (`uint32` × 25, `TracePoint` enum × 1, `descriptor_source_value` enum × 1) |
| **C++ body** | `0x18`–`0x7c` contiguous; `descriptor_source` out of line at `0x80` (default `1`) |
| **hasbits** | `0x10` (4 bytes) |
| **Serializer** | `_InternalSerialize` @`0x1cf0ed20`; **Clear** @`0x1cf0eca0`; `_table_` @`0x21c55ba0` |
| **SyncFlag channels** | `Source` @`0xf6982e0` · `Destination` @`0xf698340` · `Ack` @`0xf6983a0` (+ HIB fields 26/27) |
| **SyncFlagTarget pack** | 23 bits: `sync_flag[0:10] \| resource[10] \| node[11] \| chip[12:23]` |
| **Size / id accessors** | `GetDmaSize` @`0xf6982a0` (length≪10) · `GetDmaTransactionId` @`0xf698260` |
| **deepsea pairing key** | `GetDmaId(int)` @`0xf699ca0` — 38 bits `txid[0:21]\|core[21:24]\|chip[24:38]` (chip mask `0x3fff`) |

---

## 1. The 27-Field Record

### Purpose

The descriptor describes one inter-node fabric transfer end to end: both endpoints (offset + resource + node + chip), the byte length, two boolean shape flags (multicast / segmented), and three independent SyncFlag-raise channels. One trace event is emitted per *staged* descriptor; the per-engine execution of the descriptor (command issue, data-end) is traced by the nf band (case 6) on [ICR Node-Fabric DMA](icr-node-fabric-dma.md).

### Layout

The C++ offsets below are re-derived byte-exact from `_InternalSerialize` @`0x1cf0ed20`: each field appears as a `*((_DWORD *)this + N)` read paired with its proto wire tag stored into the output stream (`*a2 = TAG` for a one-byte tag, `*(_WORD *)a2 = TAG` for the two-byte tags of fields ≥16). The descriptor-pointer index `N` maps to byte offset `4*N`.

| proto# | Name | Type | C++ off | Wire tag | Confidence |
|---|---|---|---|---|---|
| 1 | `id` (`TracePoint`) | enum | `0x18` | `0x08` | CERTAIN |
| 2 | `tensor_node` | uint32 | `0x1c` | `0x10` | CERTAIN |
| 3 | `trace_id` | uint32 | `0x20` | `0x18` | CERTAIN |
| 4 | `descriptor_source` | enum | `0x80` | `0x20` | CERTAIN |
| 5 | `node_id` | uint32 | `0x24` | `0x28` | CERTAIN |
| 6 | `chip_id` | uint32 | `0x28` | `0x30` | CERTAIN |
| 7 | `program_counter` | uint32 | `0x2c` | `0x38` | CERTAIN |
| 8 | `source_offset` | uint32 | `0x30` | `0x40` | CERTAIN |
| 9 | `source_resource` | uint32 | `0x34` | `0x48` | CERTAIN |
| 10 | `destination_offset` | uint32 | `0x38` | `0x50` | CERTAIN |
| 11 | `destination_resource` | uint32 | `0x3c` | `0x58` | CERTAIN |
| 12 | `destination_node_id` | uint32 | `0x40` | `0x60` | CERTAIN |
| 13 | `destination_chip_id` | uint32 | `0x44` | `0x68` | CERTAIN |
| 14 | `length` | uint32 | `0x48` | `0x70` | CERTAIN |
| 15 | `destination_is_multicast` | uint32 | `0x4c` | `0x78` | CERTAIN |
| 16 | `destination_is_segmented` | uint32 | `0x50` | `0x80 01` | CERTAIN |
| 17 | `destination_update` | uint32 | `0x54` | `0x88 01` | CERTAIN |
| 18 | `destination_update_sync_flag` | uint32 | `0x58` | `0x90 01` | CERTAIN |
| 19 | `destination_update_resource` | uint32 | `0x5c` | `0x98 01` | CERTAIN |
| 20 | `source_update` | uint32 | `0x60` | `0xa0 01` | CERTAIN |
| 21 | `source_update_sync_flag` | uint32 | `0x64` | `0xa8 01` | CERTAIN |
| 22 | `source_update_resource` | uint32 | `0x68` | `0xb0 01` | CERTAIN |
| 23 | `ack_update` | uint32 | `0x6c` | `0xb8 01` | CERTAIN |
| 24 | `ack_update_sync_flag` | uint32 | `0x70` | `0xc0 01` | CERTAIN |
| 25 | `ack_update_resource` | uint32 | `0x74` | `0xc8 01` | CERTAIN |
| 26 | `hib_update` | uint32 | `0x78` | `0xd0 01` | CERTAIN |
| 27 | `hib_ack_update` | uint32 | `0x7c` | `0xd8 01` | CERTAIN |

> **QUIRK —** the C++ layout is *not* proto-field order. Fields 1–3 and 5–27 are packed contiguously from `0x18` to `0x7c`; field 4 (`descriptor_source`) is split out to `0x80`. A reimplementer who lays the struct out in declaration order — or who assumes proto field order equals memory order — will mis-read every field from `node_id` onward. The serializer reads field 4 as `*((int *)this + 32)` (i.e. `0x80`) while emitting wire tag `0x20`, between the field-3 (`0x18`) and field-5 (`0x28`) wire emissions; the proto *wire* order is correct, only the in-memory placement is reordered. This is a protobuf field-arena packing decision (the enum with a non-zero default is grouped with the trailing block), not an error in the table.

The two `_WORD` stores in the serializer (e.g. `*(_WORD *)a2 = 384` for field 16) are the little-endian two-byte varint tags `0x80 0x01`, `0x88 0x01`, … — the proto field numbers 16–27 whose tag-and-wire-type byte exceeds `0x7f`.

### Defaults and clearing

`::Clear` @`0x1cf0eca0` confirms the layout from the other direction. It zeroes three 32-byte (`ymm`) blocks and one trailing region, then sets one field to a non-zero default:

```c
function nf_descriptor_trace_entry::Clear(this):       // 0x1cf0eca0
    has = *(u32*)(this + 0x10)                          // hasbits
    if (has & 0xFF):      zero ymm [this + 0x18]        // fields 1,2,3 + below (0x18..0x37)
    if (has & 0xFF00):    zero ymm [this + 0x38]        // 0x38..0x57
    if (has & 0xFF0000):  zero ymm [this + 0x58]        // 0x58..0x77
    if (has & 0x7000000):                               // the trailing block + descriptor_source
        *(u64*)(this + 0x78) = 0                        // hib_update(0x78), hib_ack_update(0x7c)
        *(u32*)(this + 0x80) = 1                        // descriptor_source default = BARNA_CORE
    *(u32*)(this + 0x10) = 0                            // clear hasbits
```

The `*(u32*)(this + 0x80) = 1` store is the second, independent witness that `descriptor_source` lives at `0x80` and defaults to `1` (`BARNA_CORE`) — the serializer reads it from `0x80`, and `Clear` resets it there.

### Nested enums

```text
enum TracePoint:               TENSORCORE = 0,  BARNACORE = 1,  HIB = 2
enum descriptor_source_value:  TENSOR_CORE = 0, BARNA_CORE = 1, HIB = 2, HIB_HBM_QUEUE = 3
enum Constants:                NUM_LOG_LINES = 2
```

`descriptor_source` names which engine staged the descriptor; the in-memory default of `1` (`BARNA_CORE`) is what `Clear` installs. The `TracePoint` `id` (field 1) distinguishes the trace-event subtype within the band (`TENSORCORE` / `BARNACORE` / `HIB`).

---

## 2. The Three SyncFlag-Update Channels

### Purpose

A Node-Fabric DMA can raise up to three independent SyncFlags on completion — one per endpoint role. Each is the deepsea-descriptor analog of a completion semaphore: the source raises a **"buffer free"** flag when it no longer needs the source buffer, the destination raises a **"data arrived"** flag at the remote endpoint, and the ack channel raises a **"completion"** flag. Each channel is surfaced by a wrapper accessor that gates on `EntryDataCase == 3` (`cmpl $3, 0x30(rax)`, i.e. the oneof tag) and then on its own `*_update` enable field, and packs a 23-bit `SyncFlagTarget`.

### The 23-bit SyncFlagTarget pack

All three channels produce the same target layout:

```text
SyncFlagTarget (23 bits):
  bits  0..9  : sync_flag   (10 bits, mask 0x3ff)
  bit  10     : resource    (1 bit)
  bit  11     : node        (1 bit)
  bits 12..22 : chip        (11 bits, mask 0x7ff << 12 = 0x7ff000)
```

The clearest witness is `DestinationSyncFlagTarget` @`0xf698340`, which packs the fields explicitly (no SIMD fold). `v2` is the inner `nf_descriptor` message pointer at wrapper offset `+0x28` (`*(v1+40)`); the gate is `EntryDataCase == 3` and `destination_update != 0`:

```c
function DestinationSyncFlagTarget(wrapper):           // 0xf698340
    msg = wrapper.entry                                 // *(wrapper + 0x10)
    if (*(u32*)(msg + 0x30) != 3) return 0              // EntryDataCase != nf_descriptor
    nf  = *(ptr*)(msg + 0x28)                           // inner nf_descriptor message
    if (nf[21] == 0) return 0                           // destination_update (0x54) disabled
    return  (nf[17] << 12) & 0x7FF000                   // destination_chip_id   (0x44) -> chip[12:23]
          | ((nf[16] & 1)  << 11)                       // destination_node_id   (0x40) -> node[11]
          | ((nf[23] & 1)  << 10)                       // destination_update_resource (0x5c) -> resource[10]
          | (nf[22] & 0x3ff)                            // destination_update_sync_flag (0x58) -> sync_flag[0:10]
```

The four reads `nf[16]/nf[17]/nf[21]/nf[22]/nf[23]` are at C++ offsets `0x40`/`0x44`/`0x54`/`0x58`/`0x5c` — exactly the `destination_*` fields 12, 13, 17, 18, 19 from §1. This is byte-exact agreement with the table.

`SourceSyncFlagTarget` @`0xf6982e0` (gate `source_update`@`0x60`) and `AckSyncFlagTarget` @`0xf6983a0` (gate `ack_update`@`0x6c`) reach the same 23-bit layout through the shared OCI SyncFlag-target fold: a `vpunpcklqdq` interleave of `{node_id, *_update_sync_flag}` into a vector, a `vpmulld` by the lane multiplier `[1, 0x400, 0x800, 0x1000]` (fold table @`0xa2c2560`), a `vpand` by `[0x3ff, 0x400, 0x800, 0x7ff000]` (mask @`0xa2d5e00`), then a horizontal OR. The multiplier/mask pair maps lane 0 → `sync_flag[0:10]`, lane 1 → `resource[10]`, lane 2 → `node[11]`, lane 3 → `chip[12:23]` — the same 23-bit target the Destination channel packs explicitly.

> **NOTE —** the `Source`/`Ack` fold takes `{node_id, sync_flag}` as its input vector, so the bit-10 "resource" bit on those two channels is *derived from `node_id`'s low bit*, not from a separate resource field (the Source/Ack channels encode node-as-resource). The Destination channel reads a genuine `destination_update_resource` field. This is a minor semantic asymmetry between the channels; the *bit layout* is identical (HIGH confidence — the multiplier/mask are decoded byte-exact, but a symbolic proof that fold lane 1 equals Destination's `(resource & 1) << 10` was not constructed).

### The HIB channel

There is no fourth `SyncFlagTarget` accessor. Instead, fields 26/27 (`hib_update`@`0x78`, `hib_ack_update`@`0x7c`) form a host-interface (HIB) channel: the two bits select whether the descriptor also raises a HIB-side flag and/or ack. They are consumed by the jxc-only host-DMA aggregator (`DeriveHostDmaTransfers`'s `nf_descriptor_hib` path), which folds them into the host-DMA timeline. A reimplementer building only the device-side fabric path can treat them as opaque enable bits.

> **CORRECTION (P-3-431 follow-up) —** a fourth wrapper accessor, `UpdatedSyncFlagTarget` @`0xf698400`, exists alongside the three above, but it is **not** an `nf_descriptor` channel. It gates `EntryDataCase` `0xb` (`brn_sync_wait`) / `0x9` (`cs_external_sync_flag_update`) — it belongs to the TC/BarnaCore sync band, not the Node-Fabric descriptor. `nf_descriptor` carries exactly **three** SyncFlag channels plus the HIB channel. The "four accessors" reading is wrong.

---

## 3. The Size and Transaction-Id Accessors

Two more wrapper accessors read this record without packing a SyncFlag target:

- **`GetDmaSize()` @`0xf6982a0`** — for case 3 (`nf_descriptor`) returns `length(@0x48) << 10`, i.e. the byte length in **1 KiB units**. For case `0x13` (the BMEM nf arm) it instead returns `(@0x2c) << 10`. A reimplementer must left-shift by 10 to recover bytes; the descriptor stores KiB.

- **`GetDmaTransactionId()` @`0xf698260`** — returns 0 for the `nf_descriptor` case (the transaction id is absent there); it is only populated for case `0x13` (the BMEM arm), where it packs `{(@0x18 low byte) | (@0x18 bit-11) | ((@0x28) & 0x700) | ((@0x20) & ~0xfff)}` — the BMEM transaction id.

> **GOTCHA —** `length` is in KiB, not bytes. A DMA-timeline reimplementer that reads `length` directly as a byte count under-reports every transfer by a factor of 1024. The `<< 10` is in `GetDmaSize`, not in the descriptor.

---

## 4. The bcs_internal BarnaCore-Sequencer Band

### Purpose

`bcs_internal_trace_entry` (case 15 of the `PerformanceTraceEntry` oneof) is the BarnaCore-sequencer's internal observability band — the BarnaCore analog of the TC `cs_internal` band. It is not a DMA descriptor, but it shares the same proto family and the same `data_field` marker/operand sentinel scheme, and it is the band the BarnaCore raises around the descriptor staging it drives (trace markers, step boundaries, scalar fences, host interrupts, sync-trace start/stop). It is included here because its `data_field` carries the same cross-gen marker encoding the DMA bands use.

### Layout

The 7-field layout is re-derived byte-exact from `_InternalSerialize` @`0x1cf152a0` and `::Clear` @`0x1cf15260`:

| proto# | Name | Type | C++ off | Wire tag | Confidence |
|---|---|---|---|---|---|
| 1 | `id` (`TracePoint`) | enum | `0x30` | `0x08` | CERTAIN |
| 2 | `tensor_node` | uint32 | `0x18` | `0x10` | CERTAIN |
| 3 | `data_field` | uint32 | `0x1c` | `0x18` | CERTAIN |
| 4 | `sync_flag_number` | uint32 | `0x20` | `0x20` | CERTAIN |
| 5 | `program_counter` | uint32 | `0x24` | `0x28` | CERTAIN |
| 6 | `sync_sfence_end` | uint32 | `0x28` | `0x30` | CERTAIN |
| 7 | `sync_sfence_start` | uint32 | `0x2c` | `0x38` | CERTAIN |

hasbits @`0x10`; `_table_` @`0x21c56e80`. As with `nf_descriptor`, field 1 (`id`) is out of line: the serializer reads it as `*((int *)this + 12)` (`0x30`) while fields 2–7 are contiguous from `0x18`. `::Clear` zeroes an `xmm` block at `0x18` (16 bytes, `0x18`–`0x27`) plus a qword at `0x28`, then writes `*(u32*)(this + 0x30) = 122` — confirming `id` at `0x30` with **default `0x7a` = 122**.

### Per-id XLine map

The 6 `TracePoint` ids map to BarnaCore device lines via `Component()` @`0xf697f40` (case-15 arm → bcs sub-table @`0xab885fc`); names verified from `TpuComponentName` @`0x1c8ebb60`:

| id | TracePoint | TpuComponent | XLine name | Confidence |
|---|---|---|---|---|
| 122 | `BRN_TRACE_INSTRUCTION` | `0x3b` (59) | "Barna Core Trace Instruction" | HIGH |
| 123 | `BRN_SET_TRACEMARK` | `0x3c` (60) | "Barna Core Step" | HIGH |
| 124 | `BRN_SYNC_START_STOP_TRACE` | `0x16` (22) | "Barna Core Sync Flag" | HIGH |
| 125 | `BRN_HOST_INTERRUPT` | `0x3d` (61) | "Barna Core Host Interrupt" | HIGH |
| 126 | `BRN_FENCE_START` | `0x3e` (62) | "Barna Core Fence" | HIGH |
| 127 | `BRN_FENCE_END` | `0x3e` (62) | "Barna Core Fence" | HIGH |

`Constants: NUM_LOG_LINES = 1`.

### The data_field marker/operand sentinels

`bcs_internal` is consumed by the V1 path `ConvertTraceMarksAndInstructionsToXPlane<jxc>` @`0xf260960` — **not** by a registered CoreDispatcher subscriber (there is no "bcs" subscriber). `data_field` is the same cross-gen marker the `cs_internal` band carries, interpreted by three shared trackers:

```c
// step boundaries: id 123 (XLine 0x3c "Barna Core Step") via TraceMark() @0xf698140
//   StepTracker::ProcessTraceEntry @0xf2c4480, data_field at TraceMarkEntry+0x8:
data_field in {0x7ffffffc, 0x7ffffffd}            -> step-boundary sentinel
data_field & ~1 == 0x7ffffffe                     -> step START / END marker (begin/end)
data_field <= 1                                   -> dropped (no-op)
2 .. 0x7ffffff9                                   -> nested step-id push (the step name)

// per-instruction operand: id 122 (XLine 0x3b) via RunTracker::ProcessTraceOperand @0xf2c4460:
if (data_field >= 0xF0000000)  run_id = data_field & 0xfffffff   // run-boundary marker
else                            data_field is a per-instruction operand

// overlay open/close (TRACE_INSTRUCTION fan-out) via OverlayTracker::ProcessTraceOperand @0xf2c3e40:
operand kind 0x9  -> overlay close
operand kind 0xd  -> overlay open
```

Scalar fences (ids 126/127, XLine `0x3e`) use `ScalarFenceStart()` @`0xf698020` (gate `sync_sfence_start`@`0x2c`) / `ScalarFenceEnd()` @`0xf698080` (gate `sync_sfence_end`@`0x28`); the fence span lands on XLine `0x3e` via `GetOrCreateLine(0x3e)`. `BRN_SYNC_START_STOP_TRACE` (124, XLine `0x16`) uses `SyncFlagValue()` @`0xf6984a0` reading `data_field`. The begin/end-vs-point span model for ids 124/125 was not isolated from the V1 builder (LOW confidence — likely point events, not byte-confirmed).

---

## 5. The deepsea GetDmaId Bit Codec

### Purpose

DMA-timeline construction pairs a begin event with its matching end event by a synthetic **`dma_id`** composite key. The jxc `Dma` subscriber keys a `FlatHashMap<dma_id>`; the deepsea / modern gens use the same pairing via `GetDmaId`. Proving the two keys are the same logical tuple — and that the modern gens widen its components exactly as the trace header widened — closes the jxc↔deepsea DMA-timeline equivalence.

### The 38-bit deepsea key

`GetDmaId(int)` @`0xf699ca0` is a template on `pxc::profiler::TraceEntry` (the only instantiation in the unit; shared by vfc/vlc/glc/gfc through the common `profiler::TraceEntry` surface). It dispatches over the proto oneof field number (`0x96` arms), selects one `TraceIdHeader` (the int argument picks which of up to three command headers, defaulting to the global), and composes a 38-bit key with one bit layout. The composition tail @`0xf69a444` reads the `TraceIdHeader` fields `{transaction_id@0x18, core_id@0x1c, chip_id@0x20}`:

```c
// composition tail (0xf69a444); hdr is the selected TraceIdHeader
txid = *(u32*)(hdr + 0x18)                          // transaction_id
core = *(u32*)(hdr + 0x1c)                          // core_id
chip = *(u32*)(hdr + 0x20)                          // chip_id
dma_id =  (txid & 0xFF)                             //  -> transaction_id[0:8]
        | (txid & 0x1FFF00)                         //  -> transaction_id[8:21]
        | ((core & 7)   << 21)                      //  -> core_id[21:24]
        | ((u64)(chip & 0x3FFF) << 24)              //  -> chip_id[24:38]
// presence bit dl = 1
```

This is byte-exact in the decompile: the single expression
`v4 = (u32)v3 & 0x1FFF00 | ((v6[7] & 7) << 21) | ((u64)(v6[4] & 0x3FFF) << 24); return v4 | (u8)v3;`
where `v6` is the header pointer, `v6[6]`(=`+0x18`)=transaction_id, `v6[7]`(=`+0x1c`)=core_id, `v6[4*2]`(=`+0x20`)=chip_id.

```text
deepsea dma_id (38 bits):
  bits  0..20 : transaction_id   (21 bits — per-transfer tag)
  bits 21..23 : core_id          ( 3 bits — TC0/TC1/SC0..SC3 selector)
  bits 24..37 : chip_id          (14 bits, mask 0x3fff — the chip in the pod)
```

The 6 `CmdDmaIdFromEntry<…>` helpers @`0xf69a500`–`0xf69a6e0` (for `OciCommonReadCmdIssuedFromEngine` / `MemReadReqFromEngine` / `WriteCmdAcceptedAtMn` / `OciWriteCommand` / `OciReadCommand` / `CompletedInTcs`) compose the identical layout after using the int selector to pick one of up to three trace_id_headers (the OCI commands carry up to three). `GetDmaId` is called once — by `ConvertTpuTraceToXPlane<pxc>` @`0xf26c8d9` with selector `0` (the first/cmd0 header).

### Cross-gen correspondence to the jxc key

The jxc `GetDmaId()` @`0xf698180` (27-bit key) composes the same logical tuple from the jxc nf record:

```text
jxc dma_id (27 bits):     deepsea correspondence:
  trace_id[0:13]      <->  transaction_id[0:21]    (per-transfer tag; widened 13 -> 21)
  resource[13:15]     <->  core_id[21:24]          (jxc 2-bit resource slot -> deepsea 3-bit core_id)
  node[15]            <->  (folded into core_id)   (jxc 1-bit tensor-node selector)
  chip[16:27] (11b)   <->  chip_id[24:38] (14b)    (widened 11 -> 14)
```

> **QUIRK —** the deepsea / modern gens widen the `dma_id` chip field to **14 bits** (mask `0x3fff`), the same chip_id 11/12→14 widening the trace header undergoes across gens. The proto2 `chip_id` field is `uint32` in every gen; the 14-bit mask is the maximally-widened value — it over-allocates for pxc (12-bit chip in the header) but exactly fits the widened vfc/vlc/glc/gfc gens. So the DMA-pairing key tracks the same pod-address widening as the trace header. This proves the cross-gen pairing key is the **same logical `(tag, resource/core, node, chip)` tuple**, widened per generation.

> **CORRECTION (P-3-431 follow-up) —** the jxc `GetDmaId` simple arm @`0xf69824e` (the fall-through for VMEM-ICI / SMEM / IMEM / HIB engines) enters with `eax = 0`, `ecx = 0` and returns `dma_id = 0` with presence `dl = 0` — i.e. **no valid dma_id**, dropped by the subscriber's presence test. It does **not** return `id & 0xff`. Only HBM, VMEM_HBM, and BMEM arms produce a rich composite key.

> **NOTE —** no per-gen `GetDmaId` exists for vfc/glc/gfc. `GetDmaId(int)` is a pxc template only; the modern gens fold the DMA band into the ICI/intra-DMA band (see [Intra-Chip DMA Descriptor](../dma/intra-chip-descriptor.md)). The cross-gen *key* is proven the same tuple, but the modern-gen DMA-end consumer that recomputes it inline was not located (CONFIRMED-PARTIAL).

---

## 6. Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `nf_descriptor_trace_entry::_InternalSerialize` | `0x1cf0ed20` | 27-field serializer (offset/tag witness) | CERTAIN |
| `nf_descriptor_trace_entry::Clear` | `0x1cf0eca0` | zeroing + `descriptor_source=1` default | CERTAIN |
| `nf_descriptor_trace_entry::_table_` | `0x21c55ba0` | parse table | HIGH |
| `…SourceSyncFlagTarget` | `0xf6982e0` | "buffer free" flag (OCI fold) | HIGH |
| `…DestinationSyncFlagTarget` | `0xf698340` | "data arrived" flag (explicit pack) | CERTAIN |
| `…AckSyncFlagTarget` | `0xf6983a0` | "completion" flag (OCI fold) | HIGH |
| `…UpdatedSyncFlagTarget` | `0xf698400` | NOT nf — `brn_sync_wait`/`cs_external` band | CERTAIN |
| `…GetDmaSize` | `0xf6982a0` | `length << 10` (KiB→bytes) | CERTAIN |
| `…GetDmaTransactionId` | `0xf698260` | BMEM transaction id (0 for nf) | HIGH |
| OCI SyncFlag fold (mul / mask) | `0xa2c2560` / `0xa2d5e00` | `[1,0x400,0x800,0x1000]` / `[0x3ff,0x400,0x800,0x7ff000]` | HIGH |
| `bcs_internal_trace_entry::_InternalSerialize` | `0x1cf152a0` | 7-field serializer | CERTAIN |
| `bcs_internal_trace_entry::Clear` | `0x1cf15260` | zeroing + `id=122` default | CERTAIN |
| `Component` | `0xf697f40` | band→XLine (bcs sub-table @`0xab885fc`) | HIGH |
| `ConvertTraceMarksAndInstructionsToXPlane<jxc>` | `0xf260960` | V1 bcs consumer | HIGH |
| jxc `GetDmaId` | `0xf698180` | 27-bit key (jt @`0xab88674`) | HIGH |
| deepsea `GetDmaId(int)` | `0xf699ca0` | 38-bit key (jt @`0xab88c40`) | CERTAIN |
| `CmdDmaIdFromEntry<…>` × 6 | `0xf69a500`–`0xf69a6e0` | OCI command-header key helpers | HIGH |
| `ConvertTpuTraceToXPlane<pxc>` | `0xf26c8d9` | sole `GetDmaId(0)` caller | HIGH |

---

## 7. Considerations

- **Endianness of two-byte tags.** Fields ≥16 use two-byte varint wire tags stored little-endian (`*(_WORD *)a2 = 384` is bytes `0x80 0x01`). A reimplementer emitting these must write the low byte first.
- **The KiB length unit** (§3) is the single most common source of off-by-1024 errors in a DMA-timeline reconstruction.
- **`descriptor_source` placement** (§1) breaks naive declaration-order struct layouts; lay out `0x18`–`0x7c` first, then `descriptor_source` at `0x80`.
- **Multicast/segmented flags** (fields 15/16) change how the destination endpoint is interpreted (one chip vs. a fan-out set); the SyncFlag channels still raise a single target per descriptor.
- **The pxc `GetDmaId` is the only explicit key consumer**; modern-gen DMA timelines fold the pairing into the per-gen ICI/intra-DMA path (§5 NOTE), so a captured vfc/glc/gfc trace will not exercise this function directly.

---

## Cross-References

- [ICR Node-Fabric DMA](icr-node-fabric-dma.md) — the trace bands (case 6) that carry per-engine command/data-end execution of these descriptors
- [Route-Table Generation](route-table-generation.md) — the per-link table that resolves the descriptor's multi-hop route from `destination_chip_id`
- [net_router Pipeline](net-router-pipeline.md) — the per-step schedule that supplies the `(src, dst)` the descriptor stages
- [ICI Routing — Section Map](overview.md) — how the route table, schedule, and descriptor fit together
- [Intra-Chip DMA Descriptor](../dma/intra-chip-descriptor.md) — the on-chip DMA descriptor that the modern gens fold the DMA-pairing band into
