# Memory-Load Slot

> *Every address, field offset, opcode value, and string on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). `.text`/`.rodata` are mapped VA == file offset. Other wheel versions differ.*

## Abstract

The memory-load slot is the bundle slot that moves on-chip memory **into** a compute register file inside a single VLIW issue word. It is the read-side mirror of the [Memory-Store Slot](slot-memory-store.md). It is distinct from intra-chip DMA: DMA moves blocks between memory *tiers* via descriptors and carries a sync flag and a done-count, whereas a load slot moves one register's worth of data from a tier into a vreg/sreg and carries a destination register plus a sublane mask. The boundary is clean in the binary and is the architectural reason the load slot has no tier-selector bit — the tier is selected by *which slot* the op occupies.

There are three load tiers and four physical landing points: VMEM → vreg (the vector load), SMEM → sreg (the scalar load), CMEM → vreg (a Pufferfish-only dedicated slot), and SPMEM → vreg (the SparseCore tile load). Each is a different bundle slot. A single TensorCore load slot is a discriminated union: a 1-/2-/3-bit sub-opcode field selects the addressing-mode variant (base+offset, base-only, strided, indexed/gather, circular-buffer-relative, sublane-shuffled). The per-field bit positions are recovered from the per-gen `<Field>::GetConcatenatedValue()` accessors, each of which is a one-instruction `mov <word>; shr <shift>; and <mask>` that pins the field's exact position inside the decoded slot struct.

The central per-generation story is **field repacking**: the same logical fields (opcode, dest, sublane-mask, base-address, offset, stride, predication) move to different bit positions on every generation, and the destination-register field widens from 5 bits to 6 bits at the v5 boundary (the vector register file doubled). The codename → slot-list comes from the per-gen `TensorCoreCodecBase<…Decoder, …Encoder, …>` template argument order; the number of load slots and whether CMEM gets its own slot is itself a per-gen delta.

For reimplementation, the contract is: the per-gen slot-list (1/2/3 VMEM-load slots; CMEM slot only on Pufferfish), the addressing-mode sub-opcode discriminator and its values per gen, the per-field word/shift/mask layout, the 5→6-bit dest widening, and the slot-selects-tier model.

| | |
|---|---|
| **Slot role** | move one register's worth of data from VMEM/SMEM/CMEM/SPMEM into a vreg/sreg |
| **Tier select** | by slot occupied (no tier bit) — VMEM=VectorLoad, SMEM=Scalar*, CMEM=CmemLoad (PF only), SPMEM=SparseCore VectorLoad |
| **PXC VMEM-load encoder** | `pxc::isa::TensorCoreVectorLoadEncoder::Encode` @ `0x1ee287e0` |
| **PXC CMEM-load encoder** | `pxc::isa::TensorCoreCmemLoadEncoder::Encode` @ `0x1ecf89a0` |
| **Sub-opcode discriminator** | `<gen> TensorCoreVectorLoad0VectorLoadOpcode::Matches` (PXC `0x1ee28100`, VXC `0x1f006960`, GLC `0x1f3a2460`, GFC `0x1f9e97e0`) |
| **Field accessor shape** | `<Field>::GetConcatenatedValue` = `(word@off >> shift) & mask` (the exact bit position) |
| **Dest field width** | 5-bit on JF/PF, **6-bit on VF/GL/GFC** (vector register file doubled at v5) |
| **Register files** | V0..V63 (vreg), S0..S31 (sreg), CB0..CB15 (SparseCore), P0..P31 (predicate) |
| **Confidence** | CONFIRMED (byte-anchored) unless a cell says otherwise |

---

## The Load Slot Is a Per-Gen VLIW Sub-Bundle

Each gen's TensorCore bundle is a struct of fixed-position slots; the slot order is the template-argument order of the per-gen `TensorCoreCodecBase<TensorCoreBundle, …Decoder, …Encoder, …>`. The number of *load* slots and whether CMEM gets a dedicated slot is the primary per-gen delta. CMEM is first-class only on Pufferfish (it has its own bundle slot); Viperfish/Ghostlite/Trillium have no `*Cmem*` ISA op family at all and reuse the freed bundle width for a 2nd/3rd VMEM-load slot.

| Gen | VMEM-load slots (TC) | CMEM-load slots | SMEM scalar-load slots | Confidence |
|-----|---------------------:|----------------:|-----------------------:|------------|
| Jellyfish | 1 (slot-mask bit `0x040`) | 0 | 2 (`scalar_0`/`scalar_1`) | CONFIRMED |
| Dragonfish | 1 (= Jellyfish codec) | 0 | 2 | CONFIRMED |
| Pufferfish | 1 (`VectorLoad`) | **1 (`CmemLoad`, dedicated)** | 2 (`Scalar0`/`Scalar1`) | CONFIRMED |
| Viperfish | **3 (`VectorLoad0/1/2`)** | 0 | 2 (`ScalarAlu0`/`ScalarAlu1`) | CONFIRMED |
| Ghostlite | 2 (`VectorLoad0/1`) | 0 | 2 (`ScalarAlu0`/`ScalarAlu1`) | CONFIRMED |
| Trillium | 2 (`VectorLoad0/1`) | 0 | 2 (`ScalarAlu0`/`ScalarAlu1`) | CONFIRMED |

On Pufferfish the `VectorLoad` slot encodes at absolute bundle bits 119..140 and the new `CmemLoad` slot at 103..118 (see [Pufferfish 51B Bundle](bundle-pf-51b.md)); the two are disjoint regions, so a CMEM load and a VMEM load can issue in the **same** bundle cycle — the only generation with this property. The Viperfish "ErrorEncodingVectorLoadSlot0/1/2" diagnostic strings are the binary's confirmation of the three VMEM-load slots on v5e.

> **NOTE —** the architectural reason CMEM needs its own slot on Pufferfish is precisely that a load slot's wire encoding carries no tier selector (see [The Slot-Selects-Tier Model](#the-slot-selects-tier-model)). To read CMEM and VMEM in the same cycle you need two physically distinct slots. When CMEM was dropped at v5, the slot was removed and the width went to extra VMEM-load slots.

---

## The Load Op List (Addressing-Mode Sub-Opcodes)

Every load slot is a discriminated union; the sub-opcode field selects the addressing-mode variant. The variants by family:

**PXC TensorCore `VectorLoad` (VMEM → vreg)** — 2-bit sub-opcode at byte `@0x18` bits 6-7 (mask `0xC0`). The discriminator is the literal `(byte@0x18 & 0xC0)` test in `TensorCoreVectorLoadVmemLoadOpcode::Matches` (@ `0x1ee28100`, body `(*((_BYTE*)this + 24) & 0xC0) == 0`):

| value | variant | meaning | Confidence |
|-------|---------|---------|------------|
| `00` | `VmemLoad` | base + immediate offset | CONFIRMED |
| `01` (`0x40`) | `VmemLoadShuffled` | base + offset, on-load sublane shuffle | CONFIRMED |
| `10` (`0x80`) | `VmemLoadIndexedIar0` | gather via index-address-register 0 | CONFIRMED |
| `11` (`0xC0`) | `VmemLoadIndexedIar1` | gather via index-address-register 1 | CONFIRMED |

**PXC TensorCore `CmemLoad` (CMEM → vreg)** — 1-bit sub-opcode at byte `@0x16` bit 1 (`TensorCoreCmemLoadCmemLoadOpcode::Matches` @ `0x1ecf8800`); the `Noop` (slot-idle) variant tests `0x7c000000000000 == 0`.

**PXC TensorCore `Scalar1` `ScalarLoadSmem` (SMEM → sreg)** — 6-bit opcode at word `@0x30` bits 50-55 (mask `0xfc000000000000`). `ScalarLoadSmemOpcode::Matches` (@ `0x1ed27c60`) tests `(word@0x30 & 0xfc000000000000) == 0x10000000000000` (value `0x4` → `ScalarLoadSmem`); value `0x5` → `ScalarLoadSmemOffset` (`Sreg ← SMEM[Sreg+imm]`).

**VXC/GLC/GFC TensorCore `VectorLoad0/1/2` (VMEM → vreg)** — multi-bit sub-opcode (positions below). Variants: `VectorLoad` (base+offset), `VectorLoadBase` (base reg only, no offset), `VectorLoadShuffled` / `VectorLoadShuffledBase` (on-load shuffle), `VectorLoadIndexed0/1` (gather via IAR0/IAR1), `ReadIar0` / `ReadIar1` (read an index-address-register into a vreg to stage a gather), and the `Compact_*` forms (`Compact_VectorLoad`, `Compact_ReadIar0/1`, `Compact_VectorLoadIndexed0/1`, `Compact_VectorLoadShuffled`) that pack a load into a narrower slot when the fields fit.

**VXC/GLC/GFC `ScalarAlu1` `ScalarLoadSmem`** — `ScalarLoadSmemY` (`Sreg ← SMEM[Y imm]`) and `ScalarLoadSmemXY` (`Sreg ← SMEM[X base-Sreg + Y imm]`).

**SparseCore (vfc / glc::sparsecore / gfc::sparsecore) `VectorLoad` (SPMEM → vreg)** — `TileSpmemLoad` (base+offset), `TileSpmemLoadCircularBuffer` (CB-register-relative), `TileSpmemIndexedLoad` (gather via Index), `TileSpmemIndexedLoadCircularBuffer` (indexed + CB). Rich predication (`NormalPredication`, `RotatePredication`, `IsRotatePredication`, `PredicationInversion`).

The MC-layer mnemonics for the scalar/SparseCore/BarnaCore loads (the TensorCore VMEM/CMEM loads go through the proto codec, not the MC tables) are documented on the [MC-Emitter](mc-emitter.md) page: `SLDi`/`SLDri`/`SLDrr` (TC scalar), the `scVLD*`/`scSLD*`/`scSLDCBREG*` family (SparseCore), and `bcVLDi`/`bcVLDr`/`bcVLD_aliaddr{i,r}` (BarnaCore).

---

## Bit-Field Layout (Decoded `GetConcatenatedValue` Accessors)

Each per-gen `Field` class exposes `GetConcatenatedValue()` whose body is literally `(word@off >> shift) & mask`, so the field's exact position is read off the disassembly with no inference. The slot struct holds the raw bundle bits as 64-bit words at fixed member offsets (`@0x10`, `@0x18`, `@0x20`, `@0x30`, `@0x40` depending on slot). Offsets below are `(member-word @byte, shift, mask → width)`. These are *slot-relative* member-word offsets inside the decoded slot struct; the bundle-absolute bit of the Pufferfish load slot (abs 119..140) is on the [Pufferfish 51B Bundle](bundle-pf-51b.md) page.

### Pufferfish (PXC) — TensorCore `VectorLoad` (VMEM → vreg)

`DestField::GetConcatenatedValue` (@ `0x1ee281a0`) is byte-exact: `(*((_DWORD*)this + 6) >> 1) & 0x1F` — the DWORD at member offset 24 (`@0x18`), shifted 1, masked to 5 bits.

| Field | word | shift | mask | width | meaning | Confidence |
|-------|------|------:|------|------:|---------|------------|
| Opcode | `@0x18` | 6 | `0x3` | 2 | addr-mode discriminator | CONFIRMED |
| Dest (vreg) | `@0x18` | 1 | `0x1f` | 5 | destination vreg | CONFIRMED |
| SublaneMask | `@0x10`/`@0x18` (`shld 2`) | — | `0x7` | 3 | sublane-group select | CONFIRMED |
| BaseAddress | `@0x10` | 60 | `0x3` | 2 | base-address reg select | CONFIRMED |
| Offset | `@0x10` | 58 | `0x3` | 2 | immediate-offset slot index | CONFIRMED |
| Stride | `@0x10` | 55 | `0x7` | 3 | stride select | CONFIRMED |
| Vs0 / Vs1 / Vs2 | `@0x20` | 59 / 54 / 49 | 5-bit | 5 | vector source ports (gather index) | CONFIRMED |
| Imm2..Imm5 | `@0x2e`/`@0x2c`/`@0x2a`/`@0x28` | — | 16-bit | 16 | immediate displacement slots | CONFIRMED |
| Predication | (separate Predication slot) | — | — | 5 | 0..14 preg / 15 always / 31 never | CONFIRMED |

The `IndexedIar0`/`IndexedIar1`/`Shuffled` variants share these exact positions; only the 2-bit Opcode value changes. `Shuffled` adds a `ShuffleField` (sublane-shuffle selector); the Indexed variants use `Vs0/Vs1/Vs2` as the per-lane gather indices. Accessor anchors: `Opcode::Matches` @ `0x1ee28100`, `BaseAddressField` @ `0x1ee281e0`, `OffsetField` @ `0x1ee28200`, `StrideField` @ `0x1ee28220`.

### Pufferfish (PXC) — TensorCore `CmemLoad` (CMEM → vreg)

CMEM load mirrors VMEM load field-for-field but lives in the separate `CmemLoad` slot/word: Opcode `@0x16` bit 1 (1-bit), Predication `@0x10>>50 &0x1f`, SublaneMask `@0x10>>46 &0x7`, BaseAddress `@0x10>>44 &0x3`, Offset `@0x10>>42 &0x3`, Stride `@0x10>>39 &0x7`, Vs0 `@0x20>>59`, plus the same Imm2..Imm5 16-bit slots. Anchors: `NoopOpcode::Matches` @ `0x1ecf87e0`, `CmemLoadOpcode` @ `0x1ecf8800`, `PredicationField` @ `0x1ecf8820`. All CONFIRMED.

### Viperfish (VXC) — TensorCore `VectorLoad0` (VMEM → vreg)

The discriminator is two tests, byte-exact in `VectorLoadOpcode::Matches` (@ `0x1f006960`): `(*((_BYTE*)this + 25) & 0xC) == 0` (byte `@0x19` bits 2-3, i.e. qword `@0x18` bits 10-11, mask `0xc00`) **and** `(~*((_QWORD*)this + 2) & 0x3800000000000000) != 0` (a high-word test of word `@0x10` that selects the Iar/Indexed family). `VectorLoadBaseOpcode` matches `0x400`; `VectorLoadShuffledOpcode` matches `0x800`.

| Field | word | shift | mask | width | meaning | Confidence |
|-------|------|------:|------|------:|---------|------------|
| Dest (vreg) | `@0x18` | 4 | `0x3f` | **6** | destination vreg (V0..V63) | CONFIRMED |
| SublaneMask | `@0x18` | 0 | `0xf` | 4 | sublane-group select | CONFIRMED |
| Predication | `@0x18` | 12 | `0xf` | 4 | predicate reg (0..15) | CONFIRMED |
| Stride | `@0x10` | 55 | `0xf` | 4 | stride select | CONFIRMED |
| Offset | `@0x10` | 59 | `0x7` | 3 | immediate-offset slot index | CONFIRMED |
| BaseAddress (Indexed) | `@0x10` | 62 | `0x3` | 2 | base-address reg select | CONFIRMED |

`DestVregField::GetConcatenatedValue` (@ `0x1f006b60`) reads `(*((_DWORD*)this + 6) >> 4) & 0x3F` — the byte-exact proof of the 6-bit dest. `ReadIar0/Iar1` carry only a `DestVreg` (the IAR value lands in a vreg). Anchors: `PredicationField` @ `0x1f006ac0`, `StrideField` @ `0x1f006b80`, `SublaneMaskField` @ `0x1f006ba0`, `OffsetField` @ `0x1f006bc0`.

### Ghostlite (GLC) — TensorCore `VectorLoad0` (VMEM → vreg)

GLC repacks the **same** logical fields to **different** positions than VXC. The discriminator is `(*((_QWORD*)this + 3) & 0x6000) == 0` (word `@0x18` bits 13-14) plus the high-word `(word@0x10 >> 62) & 7` test, byte-exact in `VectorLoadOpcode::Matches` (@ `0x1f3a2460`).

| Field | word | shift | mask | width | Confidence |
|-------|------|------:|------|------:|------------|
| Dest (vreg) | `@0x18` | 7 | `0x3f` | 6 | CONFIRMED |
| SublaneMask | `@0x18` | 3 | `0xf` | 4 | CONFIRMED |
| Predication | `@0x18` | 15 | `0xf` | 4 | CONFIRMED |
| Stride | `@0x10` | 58 | `0xf` | 4 | CONFIRMED |
| BaseAddress (Indexed) | `@0x18` | 1 | `0x3` | 2 | CONFIRMED |
| Offset | spans `@0x10`/`@0x18` (3-bit straddle) | — | — | 3 | CONFIRMED extent |

Versus VXC: Dest moves bit 4 → 7, SublaneMask bit 0 → 3, Predication bit 12 → 15, Stride bit 55 → 58, BaseAddress word `@0x10` bit 62 → word `@0x18` bit 1. This is a pure per-gen layout delta — same fields, different positions. Anchors: `DestVregField` @ `0x1f3a26a0`, `StrideField` @ `0x1f3a26c0`, `SublaneMaskField` @ `0x1f3a26e0`.

### Trillium (GFC) — TensorCore `VectorLoad0` (VMEM → vreg)

GFC uses a **wider** opcode. The discriminator is `(*((_BYTE*)this + 25) & 0x18) == 0` (byte `@0x19` bits 3-4) plus a **3-bit** opcode in word `@0x10 & 0x7000000000000000` (bits 60-62), byte-exact in `VectorLoadOpcode::Matches` (@ `0x1f9e97e0`). The 3-bit opcode (vs GLC's 2-bit) is consistent with GFC adding ops.

| Field | word | shift | mask | width | Confidence |
|-------|------|------:|------|------:|------------|
| Dest (vreg) | `@0x18` | 5 | `0x3f` | 6 | CONFIRMED |
| SublaneMask | `@0x18` | 1 | `0xf` | 4 | CONFIRMED |
| Stride | byte `@0x17` | 0 | `0xf` | 4 | CONFIRMED |
| Offset | `@0x10` | 60 | `0x7` | 3 | CONFIRMED |

Trillium moves Stride into byte `@0x17` — another per-gen layout delta. Anchors: `DestVregField` @ `0x1f9e99e0`, `StrideField` @ `0x1f9e9a00`, `SublaneMaskField` @ `0x1f9e9a20`, `OffsetField` @ `0x1f9e9a40`.

### PXC / VXC scalar-load (SMEM → sreg)

PXC `Scalar1` `ScalarLoadSmem`: Opcode `@0x30>>50 &0x3f` (`0x4`=Smem, `0x5`=SmemOffset), Address `@0x30>>39 &0x3f`, Dest (sreg) `@0x30>>34 &0x1f`, Imm0 `@0x30>>18 &0xffff`. VXC `ScalarAlu1` `ScalarLoadSmemY/XY` (byte-exact from the accessors): Opcode `@0x40 & 0xFC0000` (`0x40000`=SmemY → value 1, `0x80000`=SmemXY → value 2), Dest `@0x40>>2 &0x1f` (`0x1eedc280`), Y `@0x40>>7 &0x3f` (`0x1eedc2a0`), X `@0x40>>13 &0x1f` (`0x1eedc2e0`). All CONFIRMED. GLC/GFC use the `gxc::glc`/`gxc::gfc` analogues; GFC additionally adds `SmemFetchAndAdd`.

---

## Addressing Modes

The sub-opcode selects one of six addressing modes:

1. **Base + immediate offset** (`VmemLoad` / `TileSpmemLoad`): `addr = base_reg + offset_imm`. `base_reg` is a 2-bit select of a base-address register; `offset` is a small (2-/3-bit) index into the bundle's shared immediate slots that hold the 16-bit displacement words (`Imm2..Imm5`).
2. **Base register only** (`VectorLoadBase`, VXC/GLC/GFC): no offset field.
3. **Strided**: a `StrideField` (3-/4-bit) selects a stride; the load reads N sublanes with a stride between them.
4. **Indexed / gather** (`VmemLoadIndexedIar0/1`, `VectorLoadIndexed0/1`, `TileSpmemIndexedLoad`): per-lane addresses come from an index-address register (IAR) or a Vs operand. `ReadIar0`/`ReadIar1` first stage the per-lane index into the IAR; the Indexed load then gathers `VMEM[base + IAR[lane]]`. PXC uses `Vs0/Vs1/Vs2` as the index ports; VF/GL/GF use IAR0/IAR1.
5. **Circular-buffer relative** (SparseCore only): a CB register (CB0..CB15) holds a rolling base; `TileSpmemLoadCircularBuffer` reads relative to it, optionally auto-updating the pointer.
6. **Sublane-shuffled** (`VmemLoadShuffled` / `VectorLoadShuffled`): the load applies a sublane permutation (`ShuffleField`) as part of the load, fusing load + sublane-shuffle into one slot.

The indexed IAR ports are shared with the store slot: an IAR set by a store's `SetIar*` sub-op can be consumed by a subsequent indexed load (see [Memory-Store Slot](slot-memory-store.md)). The per-gen IAR count is `Target::IarsPerTensorCore()` (numeric value not yet pinned — LOW).

---

## The Slot-Selects-Tier Model

There is **no** tier-select bit inside the load slot. The tier is selected by which slot the op occupies:

| Source tier | Slot | Confidence |
|-------------|------|------------|
| VMEM | `VectorLoad` / `VectorLoad0/1/2` | CONFIRMED |
| CMEM | dedicated `CmemLoad` slot (Pufferfish only) | CONFIRMED |
| SMEM | `Scalar0/1` (PXC) or `ScalarAlu0/1` (VF/GL/GF), opcode `ScalarLoadSmem*` | CONFIRMED |
| SPMEM | SparseCore `VectorLoad` slot, opcode `TileSpmemLoad*` | CONFIRMED |

This is the canonical companion to the [MemorySpace Enum](memory-space-enum.md): the runtime `MemorySpace` carried by the LLO operand picks the tier at the IR level, and the per-gen lowering routes it to the matching slot; the slot's wire encoding then carries only the address (a tier-relative byte offset divided by the tier granule), the base-address register, and the offset/stride. Three bits in the bundle word cannot encode 17 MemorySpace values, which is exactly why the slot identity, not a tag, carries the tier.

---

## Load Granularity

The `SublaneMask` field controls load granularity. A vector register holds `lane_count × sublane_count` elements; the mask selects which sublane group(s) the load writes. PXC `SublaneMask` is 3-bit (8 selectable groups); VXC/GLC/GFC widen it to 4-bit (16 groups). SparseCore uses a `MaskField` (the `scVLD_MSK` family) plus `_NP` (no-predicate) and `_PASS` (passthrough on masked-out lanes) modifiers. There is no separate count field: granularity is the popcount of the `SublaneMask`, and the addressing mode (strided vs contiguous) determines how many memory words are touched. The default (all sublanes) is a full-vector load; the `Shuffled` variant additionally permutes sublanes on load.

---

## Destination Register File and the 5→6-Bit Widening

From `TPURegStrings`: vector V0..V63 (64 vregs; wide pairs/triples/quads `V60_V61`, `V60_V61_V62_V63` exist for multi-register loads), scalar S0..S31 (32 sregs), circular-buffer CB0..CB15 (16, SparseCore), predicate P0..P31 (32). The vector-load Dest field is **5-bit on Pufferfish** (byte-exact `(DWORD@0x18 >> 1) & 0x1f`, `0x1ee281a0`) and **6-bit on Viperfish/Ghostlite/Trillium** (byte-exact `(DWORD@0x18 >> 4) & 0x3f`, `0x1f006b60`). The dest widening 5 → 6 bits at the v5 boundary is a primary per-gen delta tracking the doubled vector register file. The scalar-load Dest is 5-bit on every gen.

> **GOTCHA —** the 5-bit PXC dest addresses V0..V31 *directly* in the slot; the wider V32..V63 half and the wide/complement multi-vreg destinations (the `V60_V61_V62_V63` quads) are reached through a separate wide/complement mechanism, not by widening the slot field. The slot-encoding of a multi-register destination is not yet decoded (LOW).

---

## Per-Gen Load Encoding Table (Consolidated)

| Dimension | Jellyfish/Dragonfish | Pufferfish (PXC) | Viperfish (VXC) | Ghostlite (GLC) | Trillium (GFC) |
|-----------|----------------------|------------------|-----------------|-----------------|----------------|
| Bundle width | 41 B | 51 B | 64 B | 64 B | 64 B |
| Codec namespace | `jellyfish::isa` | `pxc::isa` | `vxc::isa` | `gxc::glc::isa` | `gxc::gfc::isa` |
| VMEM-load slots | 1 (slot-mask `0x040`) | 1 (`VectorLoad`) | 3 (`VectorLoad0/1/2`) | 2 (`VectorLoad0/1`) | 2 (`VectorLoad0/1`) |
| CMEM-load slot | none | 1 (`CmemLoad`) | none | none | none |
| VMEM addr-mode opcode | (InstBits) | 2-bit @ `0x18` bit6-7 | bits 10-11 (`0xc00`)+hi-word | bits 13-14 (`0x6000`) | byte`0x19`&`0x18` + 3b@60-62 |
| VMEM Dest field | (InstBits) | 5-bit @ `0x18` bit1 | 6-bit @ `0x18` bit4 | 6-bit @ `0x18` bit7 | 6-bit @ `0x18` bit5 |
| VMEM SublaneMask | (InstBits) | 3-bit (straddle) | 4-bit @ `0x18` bit0 | 4-bit @ `0x18` bit3 | 4-bit @ `0x18` bit1 |
| VMEM Stride | (InstBits) | 3-bit @ `0x10` bit55 | 4-bit @ `0x10` bit55 | 4-bit @ `0x10` bit58 | 4-bit @ byte `0x17` |
| VMEM Offset | (InstBits) | 2-bit @ `0x10` bit58 | 3-bit @ `0x10` bit59 | 3-bit (straddle) | 3-bit @ `0x10` bit60 |
| VMEM BaseAddress | (InstBits) | 2-bit @ `0x10` bit60 | 2-bit @ `0x10` bit62 | 2-bit @ `0x18` bit1 | (3b opcode region) |
| VMEM Predication | 5-bit (15 preg) | 5-bit (sep. slot) | 4-bit @ `0x18` bit12 | 4-bit @ `0x18` bit15 | 4-bit |
| SMEM-load op | `EmitScalarLoad` | `ScalarLoadSmem(+Offset)` | `ScalarLoadSmemY/XY` | `ScalarLoadSmemY/XY` | `…Y/XY` (+FetchAndAdd) |
| Gather index source | n/a (no IAR) | Vs0/Vs1/Vs2 | IAR0/IAR1 (`ReadIar`) | IAR0/IAR1 | IAR0/IAR1 |
| SparseCore load | none | none (BarnaCore `bcVLD`) | `TileSpmem*` (vfc) | `TileSpmem*` (glc::sc) | `TileSpmem*` (gfc::sc) |

Jellyfish/Dragonfish vector-load uses the monolithic `VectorLoadInstruction` proto packed by `EncoderJf` into the 41-byte bundle plus the LLVM `InstBits` table; the slot presence (slot-mask bit `0x040`), dest-vreg semantics, and sublane-shuffle variant are CONFIRMED, but the exact JXC bit offsets live in `InstBits` (a binary record, all-zero on disk for the codec path) rather than in `Field` accessors — marked **LOW** / not bit-enumerated here.

---

## What Is Not Yet Pinned

- **Jellyfish/Dragonfish exact VMEM-load bit positions.** Slot presence, dest semantics, and the sublane-shuffle variant are CONFIRMED; the per-field offsets are in `InstBits`, not `Field` accessors. LOW.
- **The Offset→immediate-slot mapping.** The 2-/3-bit Offset is a slot index; the 16-bit displacement lives in `Imm2..Imm5`; the per-opcode `Offset→Imm` mapping is not fully enumerated.
- **The IAR file size** (`ReadIar0/Iar1` imply 2 IARs per slot; the register-file count `IarsPerTensorCore()` numeric value is not recovered). LOW.
- **The wide/complement multi-vreg load destination encoding** (`V60_V61_V62_V63` quads). LOW.
- **The literal byte-range of each load slot inside the bundle.** The field word offsets inside the decoded slot struct are CONFIRMED; the absolute Pufferfish load-slot region (abs 119..140) is on the [Pufferfish 51B Bundle](bundle-pf-51b.md) page, but the per-gen slot-to-byte map for V5+ comes from each codec's `Encode` dispatch.

---

## Cross-References

- [Memory-Store Slot](slot-memory-store.md) — the write-side mirror; shared addressing-mode taxonomy and `SetIar*`/IAR sharing, plus the load/store asymmetries.
- [MemorySpace Enum](memory-space-enum.md) — the 17-value runtime enum the slot tier-selects on, and the proto↔enum remap.
- [Bundle Model](bundle-model-overview.md) — the per-generation bundle widths (41/51/64) and slot taxonomy this slot plugs into.
- [Pufferfish 51B Bundle](bundle-pf-51b.md) — the absolute bundle bits of the `vector_load` (119..140) and `cmem_load` (103..118) slots.
- [Viperfish 64B Bundle](bundle-vf-64b.md) — the V5+ `EncodeBundle` + per-slot `Encoder::Encode` + `BitCopy` model the VXC/GLC/GFC load slots are written under.
- [MC-Emitter](mc-emitter.md) — the MC-layer `SLD*`/`scVLD*`/`bcVLD*` load mnemonics and the register encoding table.
- [Memory Subsystem Overview](../memory/overview.md) — the tier model (HBM/VMEM/SMEM/CMEM/SPMEM) the load slot reads from.
