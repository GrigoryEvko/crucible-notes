# ICR Node-Fabric DMA Band

> *Addresses apply to `libtpu.so` from the libtpu-0.0.40-cp314 wheel. Other versions differ.*
> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset, base `0xe63c000`; `.data.rel.ro` VMA − `0x200000` = file offset). All addresses are VMA. Demangled names and offsets are cross-checked against the IDA decompile.

## Abstract

This page documents the **ICR (inter-chip-router) Node-Fabric DMA band**: the four device trace-point IDs — **48, 50, 51, 91** — that the deepsea DMA-timeline producer keys on to reconstruct a node-fabric DMA transfer, and how the producer routes each ID's payload into one of two `dma_id`-keyed maps that become the "ICI Ingress" / "ICI Egress" timeline spans.

The ICR sits at the edge of the on-chip network: it is the engine that takes a staged Node-Fabric DMA descriptor (issued from the Tensor Core Sequencer) and either pushes its bytes **out** onto the ICI router (egress) or accepts an ICI data packet arriving from the router and lands it **locally** (ingress). The hardware emits four distinct trace points across that path. The producer `ConvertTpuTraceToXPlane<pxc>` reads the on-wire ID, dispatches per ID, extracts a 38-bit `dma_id` from the message's `trace_id_header`, and accumulates `{begin_gtc, end_gtc, byte_count}` into a per-`dma_id` slot. The four IDs split cleanly: **91 + 50** form the egress span, **48 + 51** form the ingress span.

Three facts are the whole band, and a reimplementer must get all three right:

- **The band-ID → message-class → producer-role table.** Each of the four IDs carries a *different* protobuf message and plays a *different* role in the begin/end span (begin marker, end marker, byte-count source, or both).
- **The `byte_count` accumulation rule.** The OCI-message side (ID 51) adds `msg_data << 9` (a fixed 512-byte OCI message granule); the descriptor side (ID 91) adds `length << {9 or 2}`, where the shift is chosen by the descriptor's own `length_granule` enum. The descriptor `byte_count` source is `length`, **not** `program_counter`.
- **The two-map egress/ingress split.** Two independent `flat_hash_map<uint64, DmaTransfer>` keyed by `dma_id` keep the egress set (IDs 50 + 91 → lane 55 "To ICI Router") and the ingress set (IDs 48 + 51 → lane 54 "From ICI Router") apart; a span's kind tag (3 egress / 2 ingress) is fixed by which map it lands in.

> **SCOPE —** The on-wire `nf_descriptor` record layout (the staged descriptor the ICR consumes) is on [`nf-descriptor.md`](nf-descriptor.md). The XPlane-side decode of how these spans render to events/stats is in [`../profiling/icr-dma-timeline-band.md`](../profiling/icr-dma-timeline-band.md). This page owns the **ICR DMA band IDs, the 48/50/51/91 payload decode, and the ICR route path** through the producer.

| | |
|---|---|
| **Band IDs** | 48, 50, 51, 91 (the only four DMA-timeline source IDs) |
| **Producer** | `ConvertTpuTraceToXPlane<pxc::…::TraceEntry>{lambda}` @`0xf26c6e0` |
| **GetMerged ID set** | `{91, 50, 48, 51}` — packed `0x320000005B` @`0xf26c7bf`, grown `48`/`51` |
| **`dma_id` extractor** | `TraceEntryWrapper<pxc>::GetDmaId(int)` @`0xf699ca0` (single caller) |
| **`dma_id` width** | 38 bits = `txn_id[0:21] \| (core_id&7)<<21 \| (chip_id&0x3fff)<<24` |
| **Egress map → span** | IDs 50 + 91 → tag 3 → lane 55 "To ICI Router" / "ICI Egress" |
| **Ingress map → span** | IDs 48 + 51 → tag 2 → lane 54 "From ICI Router" / "ICI Ingress" |
| **Renderer** | `ConvertDmaTransfersToXPlane` @`0xf254bc0` |
| **Evidence grade** | Reimplementation-grade / **Confirmed** (byte-anchored) |

---

## 1. The four band IDs and their messages

The producer does not look at the OCI **command** band (IDs 22/26/96, the `OciCommon*` write/read commands) for the DMA timeline — that band is decodable by `GetDmaId` but no caller keys on it for this pass. The DMA timeline is built solely from the four IDs below, read from the trace-entry submessage's `trace_point_id` field (`ecx = [submsg + 0x18]` in the dispatch at `0xf26c8fb`).

Each ID maps to one protobuf message class in the `asic_sw::driver::deepsea::pxc::profiler` namespace, identified by a `oneof` case the producer verifies at `entry + 0x28` before trusting the live message pointer at `entry + 0x20` (otherwise it falls back to the `*_globals_` prototype). The `oneof` cases are confirmed in both `GetDmaId` and the producer.

| ID | Message class (`pxc::profiler::`) | `oneof` | Globals prototype | Producer role |
|----|-----------------------------------|---------|-------------------|---------------|
| 48 | `IciPacketDataPacketQueuedForLocalIngress` | 29 (`0x1d`) | `…_globals_` | **BEGIN + END markers** (ingress) |
| 50 | `OciMessageGeneratedInIcrEgressDma` | 31 (`0x1f`) | `…_globals_` | **END_GTC only** (egress, gated `done == 1`) |
| 51 | `OciMessageGeneratedInIcrIngressDma` | 32 (`0x20`) | `…_globals_` | **BYTE_COUNT** `msg_data << 9` (ingress) |
| 91 | `OciDescriptorCommonIssuedFromTcs` | 48 (`0x30`) | `…_globals_` | **BEGIN_GTC + BYTE_COUNT** `length << g` (egress) |

> The `oneof` cases are byte-exact in `GetDmaId` (`0xf699ca0`): `case 48 → [rdi+0x28]==0x1d`, `case 50 → 0x1f`, `case 51 → 0x20`, `case 91 → 0x30`. The producer re-checks the same cases at its per-ID arms (`*(_DWORD)(v16+40)` = the submessage `oneof` discriminator). All four messages place `trace_id_header` at cpp-offset `0x18` (field 1), so the `dma_id` extraction is uniform across the band.

The four IDs are assembled into the `GetMerged` request set in the producer's preamble:

```text
movabs rax, 0x320000005B   @0xf26c7bf   ; packs {91 = 0x5B, 50 = 0x32}
mov DWORD [r15+0x8], 0x30  @0xf26c7d9   ; 48
mov DWORD [r15+0xc], 0x33  @0xf26c7ff   ; 51
call GetMerged @0xf26ba80  @0xf26c81d   ; size-4 set {91, 50, 48, 51}
```

(In the decompile this is the single store `*v9 = 0x320000005B; v10[2] = 48; v10[3] = 51;` at lines 117/119/124 of the producer.)

---

## 2. The per-ID payload decode

Field offsets below are the C++ in-memory layout (`cpp_offset`, taken from each message's `TcParseTable` FieldEntry rows, stride `0x10`, `cpp_offset` at row+6) and the field names are confirmed from the carved descriptor pool. Only the bold fields are read by the DMA pass; the rest are decoded into the proto but **dropped** by this producer (it keeps only the `trace_id_header`-derived `dma_id`, the GTC timestamp, and the size).

### 2.1 ID 48 — `IciPacketDataPacketQueuedForLocalIngress` (9 fields)

The ICI data packet that has arrived from the router and is queued for local landing. It is the only ID that supplies **both** the begin and the end timestamp of an ingress span, via two trailing bool flags the C++ packs at `+0x32` / `+0x33`.

| Field | Name | cpp off | Role |
|-------|------|---------|------|
| f1 | `trace_id_header` | `0x18` | `dma_id` source |
| f2 | `router_link_port_id` | `0x20` | (dropped) |
| f3 | `virtual_channel` | `0x24` | (dropped) |
| f4 | `link_targets` | `0x28` | (dropped) |
| f5 | `local_ingress_target` (bool) | `0x30` | (dropped) |
| f6 | `multicast` (bool) | `0x31` | (dropped) |
| f7 | `dst_chip_id` | `0x2c` | (dropped) |
| f8 | **`first_packet_in_dma`** (bool) | `0x32` | **BEGIN marker** |
| f9 | **`last_packet_in_dma`** (bool) | `0x33` | **END marker** |

> **NOTE —** A single ID-48 event can set *both* markers (`first` and `last` true on the same packet) and thereby produce a self-contained ingress span. In the decompile this is `if (*((_BYTE*)v27 + 50) == 1) {begin…}` then `if (*((_BYTE*)v27 + 51) == 1) {end…}` (offsets `0x32` = decimal 50, `0x33` = 51).

### 2.2 IDs 50 / 51 — `OciMessageGeneratedInIcr{Egress,Ingress}Dma` (7 fields, identical layout)

The OCI message the ICR generates as it moves a DMA out (egress, ID 50) or in (ingress, ID 51). Same field layout for both; the producer treats them asymmetrically (see §4).

| Field | Name | cpp off | Role |
|-------|------|---------|------|
| f1 | `trace_id_header` | `0x18` | `dma_id` source |
| f2 | **`msg_data`** (uint32) | `0x20` | **BYTE_COUNT source** (ID 51) |
| f3 | **`done`** (bool) | `0x24` | **END gate** (ID 50, `done == 1`) |
| f4 | `msg_type` (enum) | `0x28` | `{PRIVATE, PUBLIC}` (dropped) |
| f5 | `opcode` (enum) | `0x2c` | `{WRITE_NO_DONE, WRITE_WITH_DONE, INC_NO_DONE, INC_WITH_DONE}` (dropped) |
| f7 | `node_type` (enum) | `0x30` | `0..6 = TCS/BC/CMQ/HBMQ/UHI/ICR/QNM` (payload label, **not** a line key; dropped) |
| f6 | `addr` (uint32) | `0x34` | (dropped) |

### 2.3 ID 91 — `OciDescriptorCommonIssuedFromTcs` (17 fields)

The staged Node-Fabric DMA descriptor as issued from the Tensor Core Sequencer — the egress send side. This is the on-chip analog of the [`nf_descriptor`](nf-descriptor.md) record; it carries the full src/dst endpoint and three sync-flag targets, but the DMA pass reads only the gate, the size, and the granule.

| Field | Name | cpp off | Role |
|-------|------|---------|------|
| f1 | `trace_id_header` | `0x18` | `dma_id` source |
| f2 | **`dma_type`** (enum) | `0x20` | **GATE** (`== DMA_TYPE_REMOTEUNICAST = 2`) |
| f3..f8 | `src_mem_*` / `dst_mem_*` / `*_opcode` | `0x24`..`0x38` | endpoint (dropped) |
| f9..f14 | `src_sync_flag_*` / `dst_sync_flag_{0,1}_*` | `0x3c`..`0x50` | sync targets (dropped) |
| f15 | `program_counter` | `0x54` | (dropped) |
| f16 | **`length`** (uint32) | `0x58` | **BYTE_COUNT source** |
| f17 | **`length_granule`** (enum) | `0x5c` | **SHIFT selector** |

Enums: `DmaTypeValues {LOCAL=0, CHIP2HOST=1, REMOTEUNICAST=2, REMOTEMULTICAST=3}`; `LengthGranuleValues {LENGTH_GRANULE_512B=0, LENGTH_GRANULE_4B=1}`.

> **CAUTION —** The C++ in-memory order is `program_counter`(`0x54`) then `length`(`0x58`) then `length_granule`(`0x5c`), which is the `_InternalParse` field order. The producer's `byte_count` reads `0x58` (`length`), **not** `0x54` (`program_counter`). Field indices in the decompile: `*((unsigned int*)v25 + 22)` = `0x58` = `length`; `*((_DWORD*)v25 + 23)` = `0x5c` = `length_granule`.

---

## 3. The ICR route path (the producer dispatch)

For each merged trace entry the producer first computes the `dma_id` key, then dispatches on the ID, applies that ID's gate, finds-or-inserts the `dma_id` slot in the appropriate map, and writes its contribution. The dispatch and the `dma_id` extraction run **before** the per-ID branch, so a non-DMA entry (no `dma_id`) is dropped early.

```text
ConvertTpuTraceToXPlane<pxc>{lambda} @0xf26c6e0
  │
  ├─ GetMerged({91,50,48,51})              @0xf26c81d
  │
  └─ for each merged entry:
        gtc   = TraceHeader.timestamp (f3, @hdr+0x20)        @0xf26c8cd  (rbx)
        dma_id = GetDmaId(entry, selector=0)                 @0xf26c8d9  → §3.1
        if (dma_id.present == 0) drop                        @0xf26c8de  (test dl,1)
        key   = dma_id                                       @0xf26c8e7  ([rbp-0x38])
        id    = trace_entry_submsg.trace_point_id (@+0x18)   @0xf26c8fb
        switch (id):
          91 → gate dma_type==2 → MAP_A.find_or_insert(key) → begin_gtc + byte_count(length<<g) + tag=3
          50 → gate done==1     → MAP_A.find_or_insert(key) → end_gtc
          48 →                    MAP_B.find_or_insert(key) → {begin_gtc if first, end_gtc if last} + tag=2
          51 →                    MAP_B.find_or_insert(key) → byte_count += msg_data<<9
        on a slot that already has begin_present && end_present → flush (push_back to the map's vector,
          clear present flags) then reopen
```

### 3.1 The 38-bit `dma_id` extraction

`GetDmaId(int)` (`0xf699ca0`) has **exactly one caller** (the producer at `0xf26c8d9`, called with `selector = 0`), proven by a full `.text` `e8`-rel32 scan. For the four band IDs each `switch` arm verifies the `oneof` (`[rdi+0x28]`), loads the live message's `trace_id_header` (each message's field 1 at `+0x18`), and converges on a common tail that composes the 38-bit ID:

```text
eax = trace_id_header.transaction_id   (@hdr+0x18)
edx = trace_id_header.core_id          (@hdr+0x1c)
ecx = trace_id_header.chip_id          (@hdr+0x20)

dma_id = (transaction_id & 0xFF)               // bits 0:8   (movzx al)
       | (transaction_id & 0x1FFF00)           // bits 8:21
       | ((core_id & 7)      << 21)            // bits 21:24
       | ((chip_id  & 0x3FFF) << 24)           // bits 24:38
present = 1
```

In the decompile (LABEL_172): `v4 = (transaction_id & 0x1FFF00) | ((core_id & 7) << 21) | ((chip_id & 0x3FFF) << 24); return v4 | (uint8)transaction_id;`. The `chip_id` mask is 14 bits; the result is 38 bits wide. A `oneof` mismatch falls back to the per-ID `TraceIdHeader` globals, and if that is null to the shared all-zero `TraceIdHeader_globals_` (`dma_id == 0`).

### 3.2 The per-ID store blocks (byte-exact)

> **ID 91 (egress, MAP_A) — begin + bytes:** at LABEL_9 (`0xf26c865`):
> `slot[+0x8] = gtc; slot[+0x10] = 1 (begin_present); shift = (length_granule == 0) ? 9 : 2; slot[+0x28] = length << shift; slot[+0x40] = 3 (tag)`.
> Decompile: `v15 = 2; if (!*((_DWORD*)v25 + 23)) v15 = 9; _R15[5] = (uint64)*((uint*)v25 + 22) << v15; *((_DWORD*)_R15 + 16) = 3;`. The `byte_count` is written with `mov` (overwrite) because the descriptor begin zero-inits the slot first (the `vpxor`/`vmovdqu ymm0` clears at `+0x8`/`+0x28`/`+0x40`).

> **ID 50 (egress, MAP_A) — end only:** gated `done == 1` (`*((_BYTE*)v24 + 36) == 1`, `0x24`). Stores `slot[+0x18] = gtc (end_gtc); slot[+0x20] = 1 (end_present)`. No begin, no bytes — only a *completed* egress message contributes the end timestamp.

> **ID 48 (ingress, MAP_B) — both markers:** `if (first_packet_in_dma == 1)` → `slot[+0x8] = gtc; slot[+0x10] = 1; slot[+0x28] = 0; slot[+0x40] = 2`. `if (last_packet_in_dma == 1)` → `slot[+0x18] = gtc; slot[+0x20] = 1; tag = 2`.

> **ID 51 (ingress, MAP_B) — bytes only:** `slot[+0x28] += msg_data << 9`. Decompile: `*(_QWORD*)(_RDX + 40) += (uint)(*((_DWORD*)v29 + 8) << 9);` (`*((_DWORD*)v29 + 8)` = `+0x20` = `msg_data`). Accumulating (`+=`), not overwriting, so multiple ingress messages on the same `dma_id` sum. No GTC store.

---

## 4. The `byte_count` accumulation rule

`byte_count` lives at the map-value slot `+0x28` (and at the merged-span form `+0x20`). It is **accumulated** across same-`dma_id` events — except the descriptor begin, which `mov`-writes into a freshly zero-inited slot. Two distinct scaling rules apply:

| Side | ID | Source field | Scale | Producer site | Granule meaning |
|------|----|--------------|-------|---------------|-----------------|
| OCI | 51 | `msg_data` (f2, `+0x20`) | `<< 9` (×512, fixed) | `0xf26cedb` | 512-B OCI message line |
| OCI | 50 | — (end_gtc only) | — | — | — |
| descr | 91 | `length` (f16, `+0x58`) | `<< (granule==0 ? 9 : 2)` | `0xf26c880` | see `length_granule` |
| ICI | 48 | — (markers only) | — | — | — |

**The OCI message granule (ID 51).** The OCI message length unit is a fixed 512 B (there is no granule field in the OCI message), so `byte_count += msg_data × 512`. ID 50 carries the *same* `msg_data` field but the producer routes it to the end-GTC store instead — the egress side's bytes come from the descriptor (ID 91), not from the egress message.

**The descriptor granule (ID 91).** The shift is chosen by the descriptor's own `length_granule`:

```text
length_granule == 0 (LENGTH_GRANULE_512B) → shift 9 → length × 512   (512-byte granules)
length_granule == 1 (LENGTH_GRANULE_4B)   → shift 2 → length × 4     (4-byte words)
```

> **GOTCHA —** the `+0x58` source is `length` (f16), **not** `program_counter`; `program_counter` (f15) sits at `+0x54` and is left unread on this path. The 512-B / 4-B granule meanings come from the `LengthGranuleValues` enum, not from the shift magnitude.

**Why ID 91 overwrites but ID 51 adds.** ID 91 is the descriptor *begin*: it zero-inits the slot then writes `byte_count = length << g`. ID 51 is an OCI-message *arrival* into a slot that may already hold a descriptor's begin (in the egress case) or a prior ingress message — so it `+=`'s. In practice the two never share a slot: ID 91/50 land in MAP_A, ID 48/51 in MAP_B.

---

## 5. The two-map egress/ingress split and the spans it renders

The producer keeps **two independent** `flat_hash_map<uint64, DmaTransfer>` keyed by `dma_id` (policy `FlatHashMapPolicy<unsigned long, DmaTransfer>`; insert via `PrepareInsertSmallNonSoo` / `PrepareInsertLarge`; key hashed by `HashKey<Hash<unsigned long>>`). The four IDs partition across them by direction:

| Map | Contents | Begin source | End source | Bytes | Flush → vector | Tag | Lane |
|-----|----------|--------------|------------|-------|----------------|-----|------|
| **MAP_A** (egress) | 91 descriptor + 50 egress msg | ID 91 (descriptor) | ID 50 (egress msg, `done==1`) | ID 91 `length×g` | vec_A | 3 | 55 "To ICI Router" |
| **MAP_B** (ingress) | 48 ICI packet + 51 ingress msg | ID 48 `first_packet` | ID 48 `last_packet` | ID 51 `msg_data×512` | vec_B | 2 | 54 "From ICI Router" |

When a slot is re-touched while it already holds **both** `begin_present` and `end_present`, the producer `push_back`s the completed `DmaTransfer` to the map's output vector and clears the present flags (flush-and-reopen), so a re-used `dma_id` opens a fresh span. The pairing key is always the 38-bit `dma_id`: the descriptor begin (MAP_A) pairs with the egress-message end sharing the `dma_id`; the ICI first-/last-packet markers (MAP_B) pair with the ingress-message bytes sharing the `dma_id`.

### 5.1 The render (lane + XEvent)

`ConvertDmaTransfersToXPlane` (`0xf254bc0`) consumes each vector and emits GTC spans onto a TPU component lane, switching on the kind tag:

```text
GetOrCreateLine(63) → MemcpyH2D   (host DMA, unused on pxc ICR band)
GetOrCreateLine(64) → MemcpyD2H   (host DMA, unused on pxc ICR band)
GetOrCreateLine(54) → TpuComponentName(54) = "From ICI Router"   ← ingress
GetOrCreateLine(55) → TpuComponentName(55) = "To ICI Router"     ← egress
EventMetadata("ICI Ingress", 11)   ← tag 2
EventMetadata("ICI Egress",  10)   ← tag 3
…
duration = end_gtc − begin_gtc;  if (end <= begin) skip;  AddEvent(line, begin, duration)
```

The kind tag selects the arm: tag 2 → lane 54 / "ICI Ingress", tag 3 → lane 55 / "ICI Egress" (tags 4/5 skip; 6/7 are the host-DMA Memcpy arms, unused for this band). A span with no begin (an end/byte-count event with no prior begin in its map) keeps the zero-init `tag == 0` and is dropped (`tag - 2; if (above) skip`).

> **DIRECTION CHECK —** Ingress (data arriving *from* the ICI router) = ID 48 `…QueuedForLocalIngress` + ID 51 `…GeneratedInIcrIngressDma` → lane 54 "From ICI Router". Egress (data leaving *to* the router) = ID 50 `…GeneratedInIcrEgressDma` + ID 91 descriptor `…IssuedFromTcs` (the send side) → lane 55 "To ICI Router". The lane names align with the on-wire direction of the four trace points.

---

## 6. Struct / table offsets (reference)

> **`DmaTransfer`** (map value, alloc `0x58` = 88 B): `+0x0` `dma_id` (key) · `+0x8` `begin_gtc` · `+0x10` `begin_present` (bool) · `+0x18` `end_gtc` · `+0x20` `end_present` (bool) · `+0x28` `byte_count` · `+0x40` kind tag (uint32, 2 | 3). The merged-span form is map-value `+0x8`: `+0x0` `begin_gtc` (sort key) · `+0x8` `begin_present` · `+0x10` `end_gtc` · `+0x18` `end_present` · `+0x20` `byte_count` · `+0x38` kind tag.
>
> **`TraceHeader`** (pxc): `trace_point_id` `+0x18` · `block_id` `+0x1c` · `timestamp` (uint64) `+0x20`.
> **`TraceIdHeader`** (pxc): `transaction_id` `+0x18` · `core_id` `+0x1c` · `chip_id` `+0x20`.
>
> **Key sites:** producer `0xf26c6e0` · GetMerged `0xf26ba80` (called `0xf26c81d`) · `GetDmaId` `0xf699ca0` (single caller `0xf26c8d9`) · id-91 begin `0xf26c865` · id-51 bytes `0xf26cedb` · id-50 end `0xf26cf41` · id-48 markers `0xf26ce60`/`0xf26ce85` · renderer `0xf254bc0` · `AddEvent(GtcSpan)` `0xf1df1e0`.

---

## 7. What the band does not carry into the span

> **NOTE —** This DMA pass drops every payload field except `trace_id_header` (→ `dma_id`), the GTC timestamp, and the size. The ID-48 `router_link_port_id` / `virtual_channel` / `link_targets` and the ID-91 src/dst mem-id, core-id, opcode, and three sync-flag targets are decoded into the proto but **not** copied into the `DmaTransfer` span on pxc — they would have to be rendered by a separate XPlane pass. The pxc span carries only `{begin, end, bytes, tag}`. The exact temporal ordering (does ID-91 begin always precede the matching ID-50 end for one `dma_id`?) is a runtime property the static decode cannot prove; the flush-and-reopen logic handles a re-touched full slot regardless.

The per-generation (vfc/vlc/glc/gfc) equivalent of this exact four-ID producer is out of scope here: `GetDmaId(int)` is pxc-template-only, and the newer generations fold the DMA band into a (likely inlined) per-family producer over widened IDs. Whether the `{48,50,51,91}` key set and the `msg_data×512` / `length×granule` byte-count rule are cross-generation invariant is unverified.

---

## Cross-References

- [`nf-descriptor.md`](nf-descriptor.md) — the on-wire Node-Fabric DMA descriptor record (the staged descriptor ID-91 traces).
- [`overview.md`](overview.md) — ICI routing section map; how a route table and net_router schedule feed the ICI DMA layer this band observes.
- [`net-router-pipeline.md`](net-router-pipeline.md) — the per-collective DMA program that drives the transfers this band times.
- [`route-table-generation.md`](route-table-generation.md) — the per-link routing table the ICR auto-route consults.
- [`../profiling/icr-dma-timeline-band.md`](../profiling/icr-dma-timeline-band.md) — the XPlane-side render of the 48/50/51/91 spans (lanes 54/55, "ICI Ingress" / "ICI Egress" events).
- [`../profiling/payload-uhi-oci-ici-dma.md`](../profiling/payload-uhi-oci-ici-dma.md) — the OCI/ICI DMA trace-point payload catalog these messages belong to.
- [`../dma/intra-chip-descriptor.md`](../dma/intra-chip-descriptor.md) — the intra-chip descriptor counterpart to this cross-chip node-fabric path.
