# ICI Cross-Chip DMA Descriptor

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`). Other versions will differ. All field positions were recovered by objdump disassembly + IDA decompile of the per-gen `*DmaDescriptorState` builders, the `jxc::DmaDescriptor` register class, and the per-gen `EncodeRemoteSyncFlagAddress*` encoders.*

## Abstract

When a TensorCore sequencer issues a DMA whose far endpoint is on **another chip**, it does not fill the intra-chip `OciDescriptorCommonIssuedFromTcs` record documented on [Intra-Chip DMA Descriptor](../dma/intra-chip-descriptor.md). It instead stages an **ICI wire descriptor**: a word array built one 32-bit word at a time in SMEM by a per-generation `*DmaDescriptorState` builder, then handed to an enqueue instruction that the on-chip DMA engine and the NodeFabric Ingress Unit (NIU) consume to push the bytes across the Inter-Chip Interconnect. The descriptor carries three things the intra-chip record never has: a **remote chip id** (or X/Y core coordinate) naming *which* chip, a **remote VMEM/HBM destination address** with a memory-space resource tag at bit 40, and a **remote destination sync-flag address** that the receiving chip's NIU auto-increments when the last byte lands — the cross-chip completion signal.

There are exactly two physical descriptor layouts in the binary, joined by a `std::variant` inside `asic_sw::driver::deepsea::DmaCommand`: **V1** (`jxc::DmaDescriptor`, Jellyfish/Dragonfish) is 8 × 32-bit words = 32 bytes with one stride level and one destination sync flag; **V2** (`DmaDescriptorV2`, Pufferfish/Viperfish/Ghostlite) is ≥96 bytes with native 4-level scatter/gather and two destination sync flags. Both are *staged in SMEM* — the compiler writes words through `WriteDescriptorWord`, merging per-DMA runtime fields over a static control-bit template via `UpdateDescriptorWord(index, mask, value)`.

This page owns the **ICI cross-chip descriptor field layout**, the **remote-address composition** (data address + chip-id endpoint + sync-flag address), the **intra-vs-inter-chip difference**, and the **SerDes-level framing** the descriptor delegates to. It does **not** re-document: the intra-chip descriptor field set ([Intra-Chip DMA Descriptor](../dma/intra-chip-descriptor.md)); the `trace_id_header` DMA-id pairing key ([OCI Command DMA-ID](../dma/oci-command-dma-id.md)); or the routing-engine NodeFabric descriptor ([NodeFabric Routing Descriptor](../routing/nf-descriptor.md)). For reimplementation the contract is:

- **The staging model** — `WriteDescriptorWord(index, val)` stores to `SmemAddrScaled(base@+0x18, index)`; `UpdateDescriptorWord(index, mask, value)` merges runtime bits over the static template default.
- **The V1 word map** — word 6 = size in granules (low 10 bits, `≤1024` byte cap), word 7 = packed `(dst_sflag<<0xa)|src_sflag` (low 12 bits, sflag number `≤59`), address words carrying remote core X/Y at bits 0x10/0x13 and the resource tag at bit 40.
- **The remote-address composition** — three independent encodings: the data address (`EncodeDmaAddressForGranule`, HBM bit-31 marker, resource id `<<0x28`), the chip endpoint (`(phys_chip_id & 0xfff) << 0xe | LocalEndpoint`), and the destination sync-flag address (per-gen `EncodeRemoteSyncFlagAddress*`).
- **The per-gen deltas** — V1 vs V2 word count, 12- vs 14-bit sflag fields, single vs 4-level stride, single vs dual destination sync flag.

| | |
|---|---|
| **V1 builder (JfDf)** | `xla::jellyfish::JellyfishDmaDescriptorState` ctor @ `0x1d4ca0a0` |
| **V2 builder (PF/VF/GL)** | `xla::pufferfish::PufferfishDmaDescriptorState`; `CreateForViperfish` @ `0x1d5ad860` |
| **Base staging** | `DmaDescriptorState::WriteDescriptorWord(long,LloValue*,b)` @ `0x1d4c73a0`; `ReadDescriptorWord` @ `0x1d4c72e0` |
| **V1 hardware class** | `asic_sw::driver::deepsea::jxc::DmaDescriptor` — 8 words / 32 B (`GetWord(int)` @ `0x1d62d760`, idx `0..7`) |
| **V2 hardware class** | `asic_fw::deepsea::registers::DmaDescriptorV2` — ≥96 B, 4-level stride (`set_*_stride` @ `0x1febaf20`/`0x1febb060`) |
| **Remote-sflag encoders** | JfDf @ `0x1d5aa620`, Pufferfish @ `0x1d5ae1a0`, Viperfish @ `0x1d5af9c0`, Ghostlite @ `0x1d5affc0` |
| **Data-address encode** | `LloRegionBuilder::EncodeDmaAddressForGranule` @ `0x1d5402c0` (HBM bit-31 marker) |
| **Endpoint render** | `xla::jellyfish::MemorySpaceToDriverResource(MemorySpace)` @ `0x1d6223e0` |
| **Evidence grade** | Reimplementation-grade for V1 word map + per-gen sflag encoders; V2 partial (≥96 B, stride/sflag setter offsets) |

---

## 1. Intra-Chip vs Cross-Chip: What Changes at the Chip Boundary

### Purpose

The single most common reimplementation error is to assume one DMA descriptor format. There are two unrelated record families, selected by whether the far endpoint is on the same chip. The reader who has [Intra-Chip DMA Descriptor](../dma/intra-chip-descriptor.md) in hand needs only the *delta*; this section is that delta.

### The two record families

The intra-chip descriptor (`OciDescriptorCommonIssuedFromTcs`) is a **17-field profiler/wire record** whose two endpoints each resolve to a *local tier* via a `(mem_id, core_id)` pair. It never names another chip. The ICI descriptor is a **staged SMEM word array** built by a `*DmaDescriptorState` and lowered to a hardware register class (`jxc::DmaDescriptor` or `DmaDescriptorV2`); its destination endpoint resolves to a *remote chip*.

| Aspect | Intra-chip (`OciDescriptorCommonIssuedFromTcs`) | ICI cross-chip (`jxc::DmaDescriptor` / V2) |
|---|---|---|
| Named by | `(mem_id, core_id)` local-tier pair | remote chip id (12-bit) or core X/Y |
| Destination | local HBM/VMEM/SMEM/… tier | remote chip VMEM/HBM + sflag address |
| Build site | LLO `Dma*StartOp` fills proto fields | `*DmaDescriptorState` stages SMEM words |
| Completion | local `dst_sync_flag_*` bump | receive-side NIU auto-increment of encoded remote sflag |
| `dma_type` value | profiler `DMA_TYPE_LOCAL=0` | `DMA_TYPE_REMOTE_*` (unicast/write-unicast/multicast) |
| Stride levels | (rolled/strided body emitters) | V1: 1 level (+ unroll); V2: 4 levels |
| Sync flags | `dst_sync_flag_{0,1}` in proto | V1: 1 dst (word 7); V2: 2 dst (mem-offset setters) |

> **NOTE —** the two families *share the same data-address encoder*. `MemorySpaceToDriverResource` @ `0x1d6223e0` (the resource-id map documented in detail on the [Intra-Chip DMA Descriptor](../dma/intra-chip-descriptor.md) page, §3) and `EncodeDmaAddressForGranule` @ `0x1d5402c0` (resource tag `<<0x28`, HBM external-address marker `0x80000000` at bit 31) serve *both* the intra-chip endpoint render and the ICI source/local-dest address word. What the ICI descriptor adds on top is the **chip-id endpoint** and the **remote sync-flag address** — the two encodings unique to cross-chip transfer, covered in [§4](#4-remote-address-composition).

### The selector

Whether the builder writes a remote endpoint at all is gated on `DmaDescriptorState::IsRemote()` @ `0x1d4c72c0`, which reads `*(byte*)(this+0xe)`. The (src MS, dst MS, remoteness, requested `DmaType`) tuple is resolved by the per-`TpuVersion` `BuildDmaOverrides` registry @ `0x1d546780`, which returns the control-word override and selects which per-gen encoder fills the descriptor. `DMA_TYPE_LOCAL` is explicitly **not** an ICI descriptor — it is the intra-chip case.

---

## 2. The Staging Model: A Word Array Built in SMEM

### Purpose

Unlike the intra-chip proto, the compiler does *not* hand a packed struct to a register. It stages the descriptor as a word array in scratch memory (SMEM), one 32-bit word at a time, then issues a DMA-enqueue instruction that reads the staged words. Every per-gen builder is a thin layer over two base primitives.

### The two primitives

```c
// xla::jellyfish::DmaDescriptorState::WriteDescriptorWord(long index, LloValue* val, b)
//   sub_1D4C73A0
function WriteDescriptorWord(index, val, b):
    base = *(LloValue**)(this + 0x18)              // SMEM base of staged word-array
    addr = b.SmemAddrScaled(base, b.SimmU32(index), target.SmemWordSizeBytes())
    b.Sst(addr, val)                               // scalar store of one word

// ReadDescriptorWord(long index, b)   sub_1D4C72E0 — Sld from the same SmemAddrScaled(base@+0x18, index)
```

The read-modify-write helper is how invariant control bits (set by the hardware-class ctor) coexist with per-DMA runtime fields:

```c
// xla::jellyfish::JellyfishDmaDescriptorState::UpdateDescriptorWord(long index, u32 mask, LloValue* value, b)
//   sub_1D4CA880
function UpdateDescriptorWord(index, mask, value, b):
    static_word = jxc::DmaDescriptor::GetWord(index)    // template default for this word
    merged      = b.Sor(b.Simm(static_word & mask), value)
    WriteDescriptorWord(index, merged)                  // through vtable+0x60
```

`mask` selects which bits of the **static template default** survive; `value` is OR'd into the rest. The `jxc::DmaDescriptor` ctor @ `0x1d62bc20` zero-fills the 32 bytes (`vmovups [rdi],ymm0`) then stamps four 16-bit default sub-fields (each `= 1`) at *bit offsets* 0x40, 0x50, 0xa0, 0xb0 via `BitCopy` — so the V1 descriptor is a 256-bit packed bit-field exposed through an 8-word `GetWord` API.

> **NOTE —** every field writer ultimately calls `WriteDescriptorWord(index, …)` through the field-group write virtual (`vtable+0x60`, group selector in `esi`). A writer either updates a sub-field via `UpdateDescriptorWord(index, mask, value)` or writes a whole field group via the virtual; the staging-region bound is CHECK'd (`descriptor_state_word_offset < region.word_count()`).

---

## 3. The V1 Descriptor Word Map (Jellyfish / Dragonfish)

### Purpose

V1 is the only descriptor whose word-by-word layout is fully recovered. It is 8 words = 32 bytes (`GetWord(int)` @ `0x1d62d760` bounds-checks `(unsigned)idx > 7 → fatal`, confirmed in the decompile), staged in SMEM and merged over the template default.

### Layout

| Field | Word / bits | Writer (addr) |
|---|---|---|
| (template control defaults) | bit-fields @ bit 0x40 / 0x50 / 0xa0 / 0xb0 `= 1` | `jxc::DmaDescriptor` ctor @ `0x1d62bc20` |
| Destination address | field-group 0 (`vtable+0x60`, `esi=0`) | `WriteDestinationAddress` @ `0x1d4caa20` |
| Remote dest core X/Y | field-group 1, `(x<<0x13)\|(y<<0x10)`, low 16 kept | `WriteRemoteDestinationCoreLocation` @ `0x1d4cae80` |
| Source address | field-group 3 (`vtable+0x60`, `esi=3`) | `WriteSourceAddress` @ `0x1d4ca960` |
| Size in granules | **word 6**, mask `0xfffffc00` (low 10 bits) | `WriteSize` @ `0x1d4ca7c0` |
| Sync flags (src+dst) | **word 7**, mask `0xfffff000`, `(dst<<0xa)\|src` (low 12 bits) | `WriteSyncFlags` @ `0x1d4cb040` |
| Stride (single level) | per-level | `WriteStrideForLevel` @ `0x1d4caae0` |
| Outfeed queue / multicast | CHECK-fail on JF (V2-only) | `WriteOutfeedQueueId` @ `0x213c6da0` |

The IDA decompile of `WriteSyncFlags` confirms the packing byte-for-byte:

```c
// JellyfishDmaDescriptorState::WriteSyncFlags(src_sflag, dst_sflag, …, b)   sub_1D4CB040
// MS gates first: if this+0xa==1 -> src sflag MS must be kBarnaCoreSflag else kSflag;
//                 if this+0xf==1 -> dst sflag MS must be kBarnaCoreSflag else kSflag
v11 = SshllU32(dst_sflag, 0xa)              // dst << 10
v12 = SorU32(src_sflag, v11)                // (dst<<0xa) | src
UpdateDescriptorWord(this, 7, 0xFFFFF000, v12, …)   // word 7, low 12 bits
```

### The size and sync-flag bounds

`WriteSize` @ `0x1d4ca7c0` CHECKs the size operand's mem-unit equals `target.GranuleMemUnit()` and packs into word 6's low 10 bits — the universal `granule_bytes <= 1024` cap (a 10-bit field at one byte per granule unit; the byte count is `size_in_granules × granule_bytes`, the granule being target-dependent: 32 B on Jellyfish, 64 B on newer gens). `WriteSyncFlags`'s 12-bit field bounds the remote-DMA sync-flag number to `jxc::MaxSyncFlagNumberForRemoteDma = 59` (`0x1d62da80`).

> **GOTCHA —** the **size word holds granules, not bytes**. A reimplementer that writes a byte count into word 6 overflows the 10-bit field for any transfer over 1 KB. The conversion is enforced by the `GranuleMemUnit` CHECK in `WriteSize`; the byte count never appears in the V1 descriptor. The same word-6 low-10-bit cap is why strided remote DMAs that exceed it are *unrolled* into multiple flat descriptors (`UnrollStridedRemoteDma` @ `0x1d4c7b20`, gated by `ShouldUnrollStridedRemoteDma` @ `0x1d4c7ac0`) rather than expressed in one word.

### Remote core location

`WriteRemoteDestinationCoreLocation` @ `0x1d4cae80` is emitted only when `IsRemote()`. It requires the X and Y operands to be U32 register-produced values, composes `(coreX << 0x13) | (coreY << 0x10)`, then merges into the destination address word keeping the low 16 bits (`AND 0xffff`). This is the V1 way of naming the neighbour core when coordinate addressing (not chip-id) is used.

---

## 4. Remote-Address Composition

### Purpose

A cross-chip transfer needs three independent addresses, each from a different encoder. Conflating them is the central reimplementation hazard, so they are enumerated separately.

### (1) The data address — resource tag + HBM marker

The source and local-destination data addresses are produced by `LloRegionBuilder::EncodeDmaAddressForGranule` @ `0x1d5402c0`. For an HBM / external-resource operand (`mem_space == 1`) it OR's the address with **`0x80000000` (bit 31)** as the external-address marker, then scales by the granule. The memory-space *resource tag* is placed at **bit 40** by the builder ctor:

```c
SetSourceAddress(MemorySpaceToDriverResource(ms) << 0x28)   // resource id at bit 40
```

`MemorySpaceToDriverResource` @ `0x1d6223e0` is the same 17-arm switch documented in detail on [Intra-Chip DMA Descriptor §3](../dma/intra-chip-descriptor.md) — the resource ids are *not* the identity of the `MemorySpace` enum (`hbm→2`, `hib→3`, `vmem→4`, `smem→6`, `sflag→0`, `imem→5`; `cmem` is a hard FATAL). A reimplementer must use the explicit table.

### (2) The chip endpoint — who receives

On V2 the destination *chip* is named by `PufferfishDmaDescriptorState::WriteRemoteEndpoints` @ `0x1d5abbe0`:

```c
// WriteRemoteEndpoints — composes the remote-endpoint word
physChipId = MapLogicalToPhysicalChipId(ChipId)
word = ((physChipId & 0xfff) << 0xe)        // 12-bit physical chip id at bit 14
     | LocalEndpoint(ms@this+0x130, 16, 18, b)  // mem-space + within-chip offset sub-field
     | <sub-fields at bit 0x18 (24) and 0x1a (26)>
```

The decompile confirms `LocalEndpoint(*(this+304), 16, 18, …)` and a `SimmU32(0x18, …)` sub-field. The chip-id *delta* (dest vs self, `SeqS32`) is computed by `WriteEndpoints` @ `0x1d5ac0a0`, which `Sselect`s between two encoding modes (direct vs routed) before calling `WriteRemoteEndpoints`. The neighbour's remote VMEM address is therefore `{phys_chip_id (12b), local_endpoint = (mem_space, within-chip word offset)}`; the routing engine resolves the *path* — the descriptor names only the destination chip, not the hops ([NodeFabric Routing Descriptor](../routing/nf-descriptor.md)).

### (3) The remote sync-flag address — completion target

This is the encoding unique to cross-chip DMA and the one that varies most per generation. The destination sync flag is named by a **VMEM address on the remote chip**, computed by a per-`TpuVersion` encoder pulled from `GetRemoteSyncFlagEncoderRegistry`. The dispatcher `LloRegionBuilder::EncodeRemoteSyncFlagAddress` @ `0x1d54da40` validates the operand is in the sflag memory space (`"remote_sync_flag->memory_space() == MemorySpace::kSflag"`), maps logical→physical chip id, and calls the registered encoder.

**(A) Jellyfish / Dragonfish** — `EncodeRemoteSyncFlagAddressJfDf` @ `0x1d5aa620`, coordinate-based. The decompile confirms the OR-composition exactly:

```c
addr = sflag_value
     | (chipX << 0x14)                  // bit 20  — X coordinate   (SimmU32 0x14, SshllU32)
     | (chipY << 0x15)                  // bit 21  — Y coordinate   (SimmU32 0x15, SshllU32)
     | 0x40000                          // bit 18  — fixed remote marker
     | (DefaultSyncFlagSegmentId() << 12)  // = 0x40 << 12 — segment id at bits 12..17
     | 0x80000                          // bit 19  — set-done / atomic-target marker (conditional)
// annotated "remote sync flag address"; DefaultSyncFlagSegmentId() = 0x40 @ 0x1d62da60
```

**(B) Pufferfish** — `pufferfish::dma_utils::EncodeRemoteSyncFlagAddress` @ `0x1d5ae1a0`, core-relative: masks the sflag field `& 0xfff` (12-bit), shifts `<< 0x12` (bit 18), OR's `0x20000` (bit 17 remote marker) and `CoreIndex() << 0x10` (bit 16), with a `Sshrl(…,2)` segment fold (shift-right-by-2) applied to the core value before the final OR.

**(C) Viperfish** — `viperfish::dma_utils::EncodeRemoteSyncFlagAddress` @ `0x1d5af9c0`, same shape as Pufferfish but a **wider 14-bit** sflag field. The decompile confirms `sflag & 0x3fff` (`SimmU32 0x3FFF`, `SandU32`) `<< 0x11` (bit 17) | `0x20000` (bit 17 marker), with the low 2 bits kept (`& 3`) and `CoreIndex << 0x10`. Ghostlite reuses this V2 path; `ghostlite::…EncodeRemoteSyncFlagAddressGhostlite` @ `0x1d5b01e0` is an 11-byte delegator.

| Gen | sflag field width | sflag shift | remote marker | core encoding |
|---|---:|---:|---|---|
| Jellyfish / Dragonfish | (coordinate) | X`<<0x14`, Y`<<0x15` | `0x40000` (b18) + `0x80000` (b19) | core X/Y coordinate |
| Pufferfish | 12-bit | `<< 0x12` (b18) | `0x20000` (b17) | `CoreIndex() << 0x10` |
| Viperfish | **14-bit** (`0x3fff`) | `<< 0x11` (b17) | `0x20000` (b17) | `CoreIndex() << 0x10` |
| Ghostlite / 6acc60406 | 14-bit | (delegates to Viperfish path) | `0x20000` | `CoreIndex` |

> **GOTCHA —** the remote sync-flag address is **not** the destination data address and **not** the chip-id endpoint. It is a *separately encoded* VMEM address the receiving NIU dereferences to find the flag to bump. The Jellyfish encoder is coordinate-based (X/Y in bits 20/21); the V2 encoders are `CoreIndex`-relative with the chip already resolved by `WriteRemoteEndpoints`. A reimplementer who reuses the data-address encoder for the sflag produces a valid-looking but wrong completion target — the transfer lands and the wait never releases.

---

## 5. SerDes-Level Framing

### Purpose

The descriptor names the destination chip; it does **not** carry a per-hop path or any link-level framing. Everything below the descriptor — flit framing, virtual-channel arbitration, credit flow control, per-link routing — is delegated to the NodeFabric / NIU hardware and the routing table installed at link bring-up.

### What the descriptor delegates

- **Routing / path.** The descriptor names only the destination chip (chip-id endpoint, [§4.2](#4-remote-address-composition), or core X/Y, [§3](#3-the-v1-descriptor-word-map-jellyfish--dragonfish)). The on-chip routing engine resolves 1..N hops via the per-link routing table installed at bring-up ([ICI Link Bring-Up](link-bringup.md), [Topology Discovery](topology-discovery.md)); the fast path stamps a precomputed routing-table index, the slow path emits per-hop descriptors via `net_router`. The NodeFabric-side descriptor that crosses the routing engine is [NodeFabric Routing Descriptor](../routing/nf-descriptor.md).
- **Credit / flow control — NONE in the descriptor.** There is no credit count, window, or back-pressure field. Per-flit credit handling lives entirely in the NIU credit FSM and the on-chip switch arbiter; the compiler budgets chunk size against per-core VMEM, never against link credit. Host visibility is only through the per-link telemetry counter set.
- **Ordering.** The strict-ordering bit is a per-DMA control default in the template; the per-gen `SupportsRemoteDmaRelaxedOrdering` capability (`JellyfishTarget` @ `0x1d491360`, `ViperfishTarget` @ `0x1d49bc40`, `GhostliteTarget` @ `0x1d4988a0`) gates whether relaxed ordering is even permitted (`GhostliteTarget::RelaxedOrderingDmaOnly = false` @ `0x1d4988e0`).

### The completion handshake on the wire

Cross-chip completion is the descriptor's one wire-level interaction with the receiving chip. Two complementary mechanisms:

1. **Piggyback completion.** Every `DMA_TYPE_REMOTE_WRITE_UNICAST` carries a destination sync-flag address (the [§4.3](#4-remote-address-composition) per-gen encoding). When the last byte lands in remote VMEM, the receiving NIU **auto-increments** that sflag — the `atomic_remote_write_set_done` / `atomic_remote_add_set_done` hardware path (an `_inverted` variant supports poll-on-zero). The V2 descriptor supports **two** destination sync flags via `set_dst_sync_flag_mem_offset(idx 0..1, u32 ≤0x80000)` @ `0x1febae20` — dual-channel completion.
2. **Explicit set-done.** `net_util::BumpRemoteSyncFlag` @ `0x1c696de0` issues a zero-payload remote write (HBM-immediate-null source) whose only effect is to bump the remote sflag — used for barrier release and async send-completion ordering.

> **NOTE —** the descriptor sync-flag *number* (V1 word 7) and the sync-flag *address* ([§4.3](#4-remote-address-composition)) are distinct. Word 7 packs the local src + dst sflag numbers `(dst<<0xa)|src`; the per-gen encoder produces the remote VMEM *address* that the receiving NIU auto-increments. The Pufferfish `WriteSyncFlags` @ `0x1d5acb80` additionally OR's a `0x2000` (bit 13) valid marker into each sflag value and writes src via field-group 1, dst via field-group 2 (checking `this+0x131==2` for a second dst sflag).

---

## 6. Per-Generation Summary

The descriptor family splits into V1 (one hardware class) and V2 (one hardware class, three gens), unified by the `DmaCommand` variant.

| Gen / family | HW class | Words / size | Sflag field | Remote-addr encoder | Stride | Dual dst sflag |
|---|---|---|---|---|---|---|
| Jellyfish / Dragonfish | `jxc::DmaDescriptor` (V1) | 8 × 32-bit = 32 B | word 7 low 12 bits (`≤59`) | JfDf @ `0x1d5aa620` (coord) | 1 level (+ unroll) | no |
| Pufferfish | `DmaDescriptorV2` | ≥96 B (~24 words) | 12-bit, valid bit 13 | dma_utils @ `0x1d5ae1a0` (`CoreIndex`) | 4 levels | yes |
| Viperfish | `DmaDescriptorV2` | ≥96 B | **14-bit** sflag | dma_utils @ `0x1d5af9c0` | 4 levels | yes |
| Ghostlite / 6acc60406 | `DmaDescriptorV2` | ≥96 B (header/fields slot split) | 14-bit | Ghostlite @ `0x1d5b01e0` (delegator) | 4 levels | yes |

The two hardware classes are joined by `std::variant<std::monostate, DmaDescriptor, DmaDescriptorV2>` inside `asic_sw::driver::deepsea::DmaCommand` (3-arm `__variant_detail::__dispatcher<0|1|2>`). `DmaDescriptorV2`'s 4-level scatter/gather is `set_src_stride(level 0..3, u32)` @ `0x1febaf20` / `set_dst_stride` @ `0x1febb060` / `set_steps_per_stride` @ `0x1febb1a0` — each level index range-checked against 3, each stride stored `<< 6` (granule-shifted). Pufferfish is reused for Viperfish via `CreateForViperfish` @ `0x1d5ad860`, differing only in the per-gen encoders pulled from the version-keyed registries.

> **GOTCHA (V2 PARTIAL) —** the exact V2 byte size is `≥96 B`, inferred from `set_dst_stride` touching offsets `+0x30`/`+0x40`/`+0x48`/`+0x58` and the `kDefaultDescriptor` static's `2× vmovaps ymm0` initializers at `+0x08`/`+0x28`. The precise word-by-word V2 field map (chip-id word index, the 4 stride-level word group, the 2 dst-sflag words, the multicast/outfeed word) is **not** fully recovered — only V1's 8-word map is byte-complete. A reimplementer targeting V2 has the setters' byte offsets and the structural shape but must re-decode the remaining inlined `DmaDescriptorV2` BitCopy setters for the full layout.

---

## 7. The `DMA_TYPE` Transfer-Class Codes

The descriptor's transfer class is selected by `BuildDmaOverrides(srcMS, dstMS, isRemote, DmaType, …)` @ `0x1d546780`, a per-`TpuVersion` registry. The recovered `.rodata` names:

| `DMA_TYPE` string | Meaning | ICI? |
|---|---|---|
| `DMA_TYPE_LOCAL` | intra-chip (VMEM↔VMEM/HBM) | no — see [Intra-Chip DMA Descriptor](../dma/intra-chip-descriptor.md) |
| `DMA_TYPE_LOCAL_OR_HOST` | local or chip↔host | no |
| `DMA_TYPE_CHIP_TO_HOST` | chip → host DRAM (infeed/outfeed) | no — host path |
| `DMA_TYPE_REMOTE_UNICAST` | cross-chip point-to-point | **yes** |
| `DMA_TYPE_REMOTE_WRITE_UNICAST` | cross-chip write (AR reduce-scatter / all-gather) | **yes** |
| `DMA_TYPE_REMOTE_MULTICAST` | cross-chip fan-out to a multicast group | **yes** |

> **QUIRK —** the *runtime/LLO* `xla::jellyfish::DmaType` enum (3 values, starting `DMA_TYPE_CHIP_TO_HOST=0`) and the *profiler descriptor* `DmaTypeValues` enum (`DMA_TYPE_LOCAL=0`, …) are **two unrelated enumerations** — see [Intra-Chip DMA Descriptor §6](../dma/intra-chip-descriptor.md). For the ICI descriptor this page owns, the relevant runtime type is `DMA_TYPE_REMOTE_WRITE_UNICAST`: an all-reduce always emits one per shard (the reduction itself is a local VPU op, never on-wire). The descriptor's own profiler `dma_type` field then reads back as `DMA_TYPE_REMOTEUNICAST`.

---

## Cross-References

- [Intra-Chip DMA Descriptor](../dma/intra-chip-descriptor.md) — the *local* counterpart: `OciDescriptorCommonIssuedFromTcs`, the `(mem_id, core_id)` tier resolution, `MemorySpaceToDriverResource`, and the two `DmaType` enums (shared with this page)
- [OCI Command DMA-ID](../dma/oci-command-dma-id.md) — the `trace_id_header` DMA-id pairing key carried by every descriptor (begin/end trace pairing)
- [NodeFabric Routing Descriptor](../routing/nf-descriptor.md) — the on-chip routing-engine descriptor that resolves the destination-chip path the ICI descriptor names
- [ICI Fabric Overview](overview.md) — where the cross-chip descriptor sits between the collective emitter and the NIU
- [ICI Link Bring-Up](link-bringup.md) — the routing-table install that lets the routing engine resolve the descriptor's destination chip to a SerDes port
- [Topology Discovery](topology-discovery.md) — how logical chip ids map to physical coordinates the remote-address encoders consume (`MapLogicalToPhysicalChipId`)
- [ICI All-Reduce Primitive](all-reduce-primitive.md) — the collective whose per-shard `DMA_TYPE_REMOTE_WRITE_UNICAST` writes *use* this descriptor
