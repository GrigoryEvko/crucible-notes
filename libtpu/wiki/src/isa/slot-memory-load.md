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
| **Sub-opcode discriminator** | `…VectorLoad…Opcode::Matches` — PXC `TensorCoreVectorLoadVmemLoadOpcode` @ `0x1ee28100`; V5+ `TensorCoreVectorLoad0VectorLoadOpcode` @ VXC `0x1f006960`, GLC `0x1f3a2460`, GFC `0x1f9e97e0` |
| **Field accessor shape** | `<Field>::GetConcatenatedValue` = `(word@off >> shift) & mask` (the exact bit position) |
| **Dest field width** | 5-bit on JF/PF, **6-bit on VF/GL/GFC** (vector register file doubled at v5) |
| **Register files** | V0..V63 (vreg, 64 on V5+ / 32 on JF–PF), S0..S31 (32 sreg), CB0..CB15 (SparseCore), predicate file 15 entries (JF–PF) / 14 (V5+) — but the slot's Predication *field* is 5-bit, encoding the preg index plus the 15=always / 31=never sentinels |

---

## The Load Slot Is a Per-Gen VLIW Sub-Bundle

Each gen's TensorCore bundle is a struct of fixed-position slots; the slot order is the template-argument order of the per-gen `TensorCoreCodecBase<TensorCoreBundle, …Decoder, …Encoder, …>`. The number of *load* slots and whether CMEM gets a dedicated slot is the primary per-gen delta. CMEM is first-class only on Pufferfish (it has its own bundle slot); Viperfish/Ghostlite/6acc60406 have no `*Cmem*` ISA op family at all and reuse the freed bundle width for a 2nd/3rd VMEM-load slot.

| Gen | VMEM-load slots (TC) | CMEM-load slots | SMEM scalar-load slots |
|-----|---------------------:|----------------:|-----------------------:|
| Jellyfish | 1 (slot-mask bit `0x040`) | 0 | 2 (`scalar_0`/`scalar_1`) |
| Dragonfish | 1 (= Jellyfish codec) | 0 | 2 |
| Pufferfish | 1 (`VectorLoad`) | **1 (`CmemLoad`, dedicated)** | 2 (`Scalar0`/`Scalar1`) |
| Viperfish | **3 (`VectorLoad0/1/2`)** | 0 | 2 (`ScalarAlu0`/`ScalarAlu1`) |
| Ghostlite | 2 (`VectorLoad0/1`) | 0 | 2 (`ScalarAlu0`/`ScalarAlu1`) |
| 6acc60406 | 2 (`VectorLoad0/1`) | 0 | 2 (`ScalarAlu0`/`ScalarAlu1`) |

On Pufferfish the `VectorLoad` slot's control fields land at absolute bundle bits 119..140 (the `VectorLoadEncoder::Encode` body @ `0x1ee287e0` writes Predication via `BitCopy(dst, 136, …, 5)`, Dest `BitCopy(dst, 129, …, 5)`, Stride `…126,3`, Offset `…124,2`, BaseAddress `…122,2`, SublaneMask `…119,3`, Opcode `…134,2`); the `CmemLoad` control fields land at 103..118 (the `CmemLoadEncoder::Encode` body @ `0x1ecf89a0` writes Predication `BitCopy(dst,114,5)`, Opcode `…113,1`, SublaneMask `…110,3`, BaseAddress `…108,2`, Offset `…106,2`, Stride `…103,3`) — see [Pufferfish 51B Bundle](bundle-pf-51b.md). The two control regions are disjoint, so a CMEM load and a VMEM load can issue in the **same** bundle cycle — the only generation with this property (their shared `Vs0/Vs1/Vs2` and `Imm` fields land in higher disjoint bits, 241..256+). The three VMEM-load slots on Viperfish are confirmed by the three distinct per-slot encoder symbols `vxc::isa::TensorCoreVectorLoad{0,1,2}Encoder::Encode`.

> **NOTE —** the architectural reason CMEM needs its own slot on Pufferfish is precisely that a load slot's wire encoding carries no tier selector (see [The Slot-Selects-Tier Model](#the-slot-selects-tier-model)). To read CMEM and VMEM in the same cycle you need two physically distinct slots. When CMEM was dropped at v5, the slot was removed and the width went to extra VMEM-load slots.

---

## The VMEM-Load Encoder Algorithm (`VectorLoadEncoder::Encode`)

The Pufferfish VMEM-load slot is packed by `pxc::isa::TensorCoreVectorLoadEncoder::Encode(TensorCoreVectorLoad const&, Span<uint8>)` (`0x1ee287e0`), the read-side mirror of the [cmem_load `Encode`](slot-cmem-load-pf.md#on-wire-field-map). Like every Pufferfish slot it writes each field with one call to the shared bit-packing primitive `BitCopy(buf, abs_bit, &field, src_bit=0, nbits)` (`0x1fa0a900`), and **the `dst_bit` argument is the literal absolute bundle bit** — no shift arithmetic to invert (see [Pufferfish 51B Bundle §The Direct-BitCopy Model](bundle-pf-51b.md)). The structure is byte-exact from the disassembly: predication is staged from `proto[+0x1c]` and written first **unconditionally**, then a oneof discriminator at `proto[+0x50]` selects one of ten behaviours through a self-relative jump table (`lea -0x135e338d(%rip),%rcx` → table base `0xb8454a4`; `cmp $0x9,%rax; ja <out-of-range>`), and the `VmemLoad` issue arm replays the field sequence below, each field gated by a per-field has-bit on the `TensorCoreVectorLoad_VmemLoad_globals_` submessage (`0x22410b00`):

```c
// pxc::isa::TensorCoreVectorLoadEncoder::Encode(proto, buf)  @ 0x1ee287e0  (decoded byte-exactly)
pred = proto[+0x1c];                            // movslq 0x1c(%rsi)
BitCopy(buf, 136, &pred, 0, 5);                 // Predication @136/5 — written first, unconditionally
tag = proto[+0x50];                             // oneof discriminator; jump table over tags 0..9
if (tag > 9) abort();                           // cmp $0x9; ja  -> out-of-range trap
// dispatch via table @ 0xb8454a4 ; tag 5 (Noop) forces pred=31, tag 6 is the VmemLoad issue arm:
if (tag == NOOP) { pred = 31; BitCopy(buf, 136, &pred, 0, 5); return OK; }   // kNeverExecute
// ---- VmemLoad issue arm (tag 6): inner = the VmemLoad submessage (or _globals_ default if clear) ----
inner = proto[+0x48];                           // VmemLoad submessage
BitCopy(buf, 134, &Opcode, 0, 2);               // Opcode @134/2 — addr-mode discriminator
if (inner.has[0x10] & 0x01) BitCopy(buf, 129, &inner.Dest,        0, 5);  // Dest vreg @129/5
if (inner.has[0x10] & 0x02) BitCopy(buf, 126, &inner.Stride,      0, 3);  // Stride @126/3  (off +0x1c)
if (inner.has[0x10] & 0x04) BitCopy(buf, 124, &inner.Offset,      0, 2);  // Offset @124/2  (off +0x20)
if (inner.has[0x10] & 0x08) BitCopy(buf, 122, &inner.BaseAddress, 0, 2);  // BaseAddress @122/2 (off +0x24)
if (inner.has[0x10] & 0x10) BitCopy(buf, 119, &inner.SublaneMask, 0, 3);  // SublaneMask @119/3 (off +0x28)
// ----- shared operand pool (co-allocated across slots; abs 241/246/251 + 16-bit imms) -----
if (inner.has[0x10] & 0x20) BitCopy(buf, 251, &inner.Vs2, 0, 5);   // Vs2 register selector
if (inner.has[0x10] & 0x40) BitCopy(buf, 246, &inner.Vs1, 0, 5);   // Vs1
if (inner.has[0x10] & 0x80) BitCopy(buf, 241, &inner.Vs0, 0, 5);   // Vs0  (dest VREG / base)
if (inner.has[0x11] & 0x01) BitCopy(buf, 304, &inner.Imm, 0, 16);  // shared immediate word
if (inner.has[0x11] & 0x02) BitCopy(buf, 288, &inner.Imm, 0, 16);  // shared immediate word
if (inner.has[0x11] & 0x04) BitCopy(buf, 272, &inner.Imm, 0, 16);  // shared immediate word
if (inner.has[0x11] & 0x08) BitCopy(buf, 256, &inner.Imm, 0, 16);  // shared immediate word
```

The dst-bit constants are read straight off the `mov $imm,%esi` immediately before each `call 1fa0a900 <BitCopy>`: `0x88`=136 (pred), `0x86`=134 (opcode), `0x81`=129 (dest), `0x7e`=126 (stride), `0x7c`=124 (offset), `0x7a`=122 (base), `0x77`=119 (sublane), and the shared-pool `0xfb`=251 / `0xf6`=246 / `0xf1`=241 (Vs2/Vs1/Vs0, w5) and `0x130`=304 / `0x120`=288 / `0x110`=272 / `0x100`=256 (immediates, w16). They agree bit-for-bit with the dedicated-region map below and with the cmem-load slot's higher-bit shared pool (Vs at 241/246/251, immediates at 256/272/288/304 — see [cmem_load §The Shared Operand Pool](slot-cmem-load-pf.md#the-shared-operand-pool)), confirming the two memory-read slots draw from one physical Y-register/immediate pool.

> **NOTE — predication is written before the tag is even read.** The encoder stages `proto[+0x1c]` and emits the @136/5 predication `BitCopy` before the `proto[+0x50]` oneof discriminator load. An empty (tag-0) or Noop (tag-5) slot therefore still carries a 5-bit predication value — `31` (`kNeverExecute`) for the idle encoding — exactly as on the [cmem_load slot](slot-cmem-load-pf.md#the-oneof-tag-and-the-idle-encoding). The two memory-read slots share this idle-encoding convention; the zeroed bundle buffer is *not* the idle marker (predicate `0` is a live op).

> **QUIRK — the oneof spans ten arms, not three.** The `cmp $0x9,%rax; ja` bound on the discriminator means the VMEM-load oneof has up to ten tags (`0..9`), versus the cmem_load slot's three (`0/5/6`). The extra arms are the addressing-mode variants (`VmemLoad`, `VmemLoadShuffled`, `VmemLoadIndexedIar0/1`) plus the empty/Noop idle forms — each is a distinct jump-table arm that replays the same `BitCopy` field sequence with a different `Opcode` constant at @134. A reimplementer must size the discriminator at 10 arms and route every non-idle arm through the identical field map; only the 2-bit Opcode value at @134 differs between addressing modes.

---

## The Load Op List (Addressing-Mode Sub-Opcodes)

Every load slot is a discriminated union; the sub-opcode field selects the addressing-mode variant. The variants by family:

**PXC TensorCore `VectorLoad` (VMEM → vreg)** — 2-bit sub-opcode at byte `@0x18` bits 6-7 (mask `0xC0`). The discriminator is the literal `(byte@0x18 & 0xC0)` test in `TensorCoreVectorLoadVmemLoadOpcode::Matches` (@ `0x1ee28100`, body `(*((_BYTE*)this + 24) & 0xC0) == 0`):

| value | variant | meaning |
|-------|---------|---------|
| `00` | `VmemLoad` | base + immediate offset |
| `01` (`0x40`) | `VmemLoadShuffled` | base + offset, on-load sublane shuffle |
| `10` (`0x80`) | `VmemLoadIndexedIar0` | gather via index-address-register 0 |
| `11` (`0xC0`) | `VmemLoadIndexedIar1` | gather via index-address-register 1 |

**PXC TensorCore `CmemLoad` (CMEM → vreg)** — 1-bit sub-opcode at byte `@0x16` bit 1 (`TensorCoreCmemLoadCmemLoadOpcode::Matches` @ `0x1ecf8800`, body `(*((_BYTE*)this+22) & 2) >> 1`); the `Noop` (slot-idle) variant matches when bits `0x7c000000000000` of word `@0x10` are all set — its `Matches` (@ `0x1ecf87e0`) is `(~word@0x10 & 0x7c000000000000) == 0`.

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

| Field | word | shift | mask | width | meaning |
|-------|------|------:|------|------:|---------|
| Opcode | `@0x18` | 6 | `0x3` | 2 | addr-mode discriminator |
| Dest (vreg) | `@0x18` | 1 | `0x1f` | 5 | destination vreg |
| SublaneMask | `@0x10` (128-bit) | 62 | `0x7` | 3 | sublane-group select (straddles into `@0x18`) |
| BaseAddress | `@0x10` | 60 | `0x3` | 2 | base-address reg select |
| Offset | `@0x10` | 58 | `0x3` | 2 | immediate-offset slot index |
| Stride | `@0x10` | 55 | `0x7` | 3 | stride select |
| Vs0 / Vs1 / Vs2 | `@0x20` | 59 / 54 / 49 | 5-bit | 5 | vector source ports (gather index) |
| Imm2..Imm5 | `@0x2e`/`@0x2c`/`@0x2a`/`@0x28` | — | 16-bit | 16 | immediate displacement slots |
| Predication | (separate Predication slot) | — | — | 5 | 0..14 preg / 15 always / 31 never |

The `IndexedIar0`/`IndexedIar1`/`Shuffled` variants share these exact positions; only the 2-bit Opcode value changes (`VmemLoad`=0, `VmemLoadShuffled`=1, `VmemLoadIndexedIar0`=2, `VmemLoadIndexedIar1` via a dedicated branch). `Shuffled` adds a `ShuffleField` (sublane-shuffle selector); the Indexed variants use `Vs0/Vs1/Vs2` as the per-lane gather indices. Accessor anchors: `Opcode::Matches` @ `0x1ee28100`, `DestField` @ `0x1ee281a0`, `SublaneMaskField` @ `0x1ee281c0`, `BaseAddressField` @ `0x1ee281e0`, `OffsetField` @ `0x1ee28200`, `StrideField` @ `0x1ee28220`, `Vs0Field` @ `0x1ee28240`, `Vs1Field` @ `0x1ee28260`, `Vs2Field` @ `0x1ee28280`. The three source ports (`Vs0/Vs1/Vs2`) are the per-gen-stable count on Pufferfish — the load slot has not yet widened its gather-index port count at v4; the widening visible across v5+ is in the *number of load slots*, not the per-slot port count.

### Pufferfish (PXC) — TensorCore `CmemLoad` (CMEM → vreg)

CMEM load mirrors VMEM load field-for-field but lives in the separate `CmemLoad` slot/word: Opcode `@0x16` bit 1 (1-bit), Predication `@0x10>>50 &0x1f`, SublaneMask `@0x10>>46 &0x7`, BaseAddress `@0x10>>44 &0x3`, Offset `@0x10>>42 &0x3`, Stride `@0x10>>39 &0x7`, and — exactly like VMEM load — three gather index ports Vs0 `@0x20>>59`, Vs1 `@0x20>>54 &0x1f`, Vs2 `@0x20>>49 &0x1f`, plus the same Imm2..Imm5 16-bit slots. Anchors: `NoopOpcode::Matches` @ `0x1ecf87e0`, `CmemLoadOpcode` @ `0x1ecf8800`, `PredicationField` @ `0x1ecf8820`, `Vs0Field` @ `0x1ecf88c0`, `Vs1Field` @ `0x1ecf88e0`, `Vs2Field` @ `0x1ecf8900`.

### Viperfish (VXC) — TensorCore `VectorLoad0` (VMEM → vreg)

The discriminator is two tests, byte-exact in `VectorLoadOpcode::Matches` (@ `0x1f006960`): `(*((_BYTE*)this + 25) & 0xC) == 0` (byte `@0x19` bits 2-3, i.e. qword `@0x18` bits 10-11, mask `0xc00`) **and** `(~*((_QWORD*)this + 2) & 0x3800000000000000) != 0` (a high-word test of word `@0x10` that selects the Iar/Indexed family). `VectorLoadBaseOpcode` matches `0x400`; `VectorLoadShuffledOpcode` matches `0x800`.

| Field | word | shift | mask | width | meaning |
|-------|------|------:|------|------:|---------|
| Dest (vreg) | `@0x18` | 4 | `0x3f` | **6** | destination vreg (V0..V63) |
| SublaneMask | `@0x18` | 0 | `0xf` | 4 | sublane-group select |
| Predication | `@0x18` | 12 | `0xf` | 4 | predicate reg (0..15) |
| Stride | `@0x10` | 55 | `0xf` | 4 | stride select |
| Offset | `@0x10` | 59 | `0x7` | 3 | immediate-offset slot index |
| BaseAddress (Indexed) | `@0x10` | 62 | `0x3` | 2 | base-address reg select |

`DestVregField::GetConcatenatedValue` (@ `0x1f006b60`) reads `(*((_DWORD*)this + 6) >> 4) & 0x3F` — the byte-exact proof of the 6-bit dest. `ReadIar0/Iar1` carry only a `DestVreg` (the IAR value lands in a vreg). Anchors: `PredicationField` @ `0x1f006ac0`, `StrideField` @ `0x1f006b80`, `SublaneMaskField` @ `0x1f006ba0`, `OffsetField` @ `0x1f006bc0`.

### Ghostlite (GLC) — TensorCore `VectorLoad0` (VMEM → vreg)

GLC repacks the **same** logical fields to **different** positions than VXC. The discriminator is `(*((_QWORD*)this + 3) & 0x6000) == 0` (word `@0x18` bits 13-14) plus the high-word `(word@0x10 >> 62) & 7` test, byte-exact in `VectorLoadOpcode::Matches` (@ `0x1f3a2460`).

| Field | word | shift | mask | width |
|-------|------|------:|------|------:|
| Dest (vreg) | `@0x18` | 7 | `0x3f` | 6 |
| SublaneMask | `@0x18` | 3 | `0xf` | 4 |
| Predication | `@0x18` | 15 | `0xf` | 4 |
| Stride | `@0x10` | 58 | `0xf` | 4 |
| BaseAddress (Indexed) | `@0x18` | 1 | `0x3` | 2 |
| Offset | spans `@0x10`/`@0x18` (3-bit straddle) | — | — | 3 |

Versus VXC: Dest moves bit 4 → 7, SublaneMask bit 0 → 3, Predication bit 12 → 15, Stride bit 55 → 58, BaseAddress word `@0x10` bit 62 → word `@0x18` bit 1. This is a pure per-gen layout delta — same fields, different positions. Anchors: `DestVregField` @ `0x1f3a26a0`, `StrideField` @ `0x1f3a26c0`, `SublaneMaskField` @ `0x1f3a26e0`.

### 6acc60406 (GFC) — TensorCore `VectorLoad0` (VMEM → vreg)

GFC uses a **wider** opcode. The discriminator is `(*((_BYTE*)this + 25) & 0x18) == 0` (byte `@0x19` bits 3-4) plus a **3-bit** opcode in word `@0x10 & 0x7000000000000000` (bits 60-62), byte-exact in `VectorLoadOpcode::Matches` (@ `0x1f9e97e0`). The 3-bit opcode (vs GLC's 2-bit) is consistent with GFC adding ops.

| Field | word | shift | mask | width |
|-------|------|------:|------|------:|
| Dest (vreg) | `@0x18` | 5 | `0x3f` | 6 |
| SublaneMask | `@0x18` | 1 | `0xf` | 4 |
| Stride | byte `@0x17` | 0 | `0xf` | 4 |
| Offset | `@0x10` | 60 | `0x7` | 3 |

6acc60406 (GFC) moves Stride into byte `@0x17` — another per-gen layout delta. Anchors: `DestVregField` @ `0x1f9e99e0`, `StrideField` @ `0x1f9e9a00`, `SublaneMaskField` @ `0x1f9e9a20`, `OffsetField` @ `0x1f9e9a40`.

### PXC / VXC scalar-load (SMEM → sreg)

PXC `Scalar1` `ScalarLoadSmem`: Opcode `@0x30>>50 &0x3f` (`0x4`=Smem, `0x5`=SmemOffset), Address `@0x30>>39 &0x3f`, Dest (sreg) `@0x30>>34 &0x1f`, Imm0 `@0x30>>18 &0xffff`. VXC `ScalarAlu1` `ScalarLoadSmemY/XY` (byte-exact from the accessors): Opcode `@0x40 & 0xFC0000` (`0x40000`=SmemY → value 1, `0x80000`=SmemXY → value 2), Dest `@0x40>>2 &0x1f` (`0x1eedc280`), Y `@0x40>>7 &0x3f` (`0x1eedc2a0`), X `@0x40>>13 &0x1f` (`0x1eedc2e0`). GLC/GFC use the `gxc::glc`/`gxc::gfc` analogues; GFC additionally adds `SmemFetchAndAdd`.

---

## Addressing Modes

The sub-opcode selects one of six addressing modes:

1. **Base + immediate offset** (`VmemLoad` / `TileSpmemLoad`): `addr = base_reg + offset_imm`. `base_reg` is a 2-bit select of a base-address register; `offset` is a small (2-/3-bit) index into the bundle's shared immediate slots that hold the 16-bit displacement words (`Imm2..Imm5`).
2. **Base register only** (`VectorLoadBase`, VXC/GLC/GFC): no offset field.
3. **Strided**: a `StrideField` (3-/4-bit) selects a stride; the load reads N sublanes with a stride between them.
4. **Indexed / gather** (`VmemLoadIndexedIar0/1`, `VectorLoadIndexed0/1`, `TileSpmemIndexedLoad`): per-lane addresses come from an index-address register (IAR) or a Vs operand. `ReadIar0`/`ReadIar1` first stage the per-lane index into the IAR; the Indexed load then gathers `VMEM[base + IAR[lane]]`. PXC uses `Vs0/Vs1/Vs2` as the index ports; VF/GL/GF use IAR0/IAR1.
5. **Circular-buffer relative** (SparseCore only): a CB register (CB0..CB15) holds a rolling base; `TileSpmemLoadCircularBuffer` reads relative to it, optionally auto-updating the pointer.
6. **Sublane-shuffled** (`VmemLoadShuffled` / `VectorLoadShuffled`): the load applies a sublane permutation (`ShuffleField`) as part of the load, fusing load + sublane-shuffle into one slot.

The indexed IAR ports are shared with the store slot: an IAR set by a store's `SetIar*` sub-op can be consumed by a subsequent indexed load (see [Memory-Store Slot](slot-memory-store.md)). The per-gen IAR count is `Target::IarsPerTensorCore()`.

---

## The Slot-Selects-Tier Model

There is **no** tier-select bit inside the load slot. The tier is selected by which slot the op occupies:

| Source tier | Slot |
|-------------|------|
| VMEM | `VectorLoad` / `VectorLoad0/1/2` |
| CMEM | dedicated `CmemLoad` slot (Pufferfish only) |
| SMEM | `Scalar0/1` (PXC) or `ScalarAlu0/1` (VF/GL/GF), opcode `ScalarLoadSmem*` |
| SPMEM | SparseCore `VectorLoad` slot, opcode `TileSpmemLoad*` |

This is the canonical companion to the [MemorySpace Enum](memory-space-enum.md): the runtime `MemorySpace` carried by the LLO operand picks the tier at the IR level, and the per-gen lowering routes it to the matching slot; the slot's wire encoding then carries only the address (a tier-relative byte offset divided by the tier granule), the base-address register, and the offset/stride. Three bits in the bundle word cannot encode 17 MemorySpace values, which is exactly why the slot identity, not a tag, carries the tier.

---

## Load Granularity

The `SublaneMask` field controls load granularity. A vector register holds `lane_count × sublane_count` elements; the mask selects which sublane group(s) the load writes. PXC `SublaneMask` is 3-bit (8 selectable groups); VXC/GLC/GFC widen it to 4-bit (16 groups). SparseCore uses a `MaskField` (the `scVLD_MSK` family) plus `_NP` (no-predicate) and `_PASS` (passthrough on masked-out lanes) modifiers. There is no separate count field: granularity is the popcount of the `SublaneMask`, and the addressing mode (strided vs contiguous) determines how many memory words are touched. The default (all sublanes) is a full-vector load; the `Shuffled` variant additionally permutes sublanes on load.

---

## Destination Register File and the 5→6-Bit Widening

From `TPURegStrings`: vector V0..V63 (64 vregs; wide pairs/triples/quads `V60_V61`, `V60_V61_V62_V63` exist for multi-register loads), scalar S0..S31 (32 sregs), circular-buffer CB0..CB15 (16, SparseCore), predicate P0..P31 (32). The vector-load Dest field is **5-bit on Pufferfish** (byte-exact `(DWORD@0x18 >> 1) & 0x1f`, `0x1ee281a0`) and **6-bit on Viperfish/Ghostlite/6acc60406** (byte-exact `(DWORD@0x18 >> 4) & 0x3f`, `0x1f006b60`). The dest widening 5 → 6 bits at the v5 boundary is a primary per-gen delta tracking the doubled vector register file. The scalar-load Dest is 5-bit on every gen.

> **GOTCHA —** the 5-bit PXC dest addresses V0..V31 *directly* in the slot; the wider V32..V63 half and the wide/complement multi-vreg destinations (the `V60_V61_V62_V63` quads) are reached through a separate wide/complement mechanism, not by widening the slot field. The slot-encoding of a multi-register destination is not decoded here.

---

## Per-Gen Load Encoding Table (Consolidated)

| Dimension | Jellyfish/Dragonfish | Pufferfish (PXC) | Viperfish (VXC) | Ghostlite (GLC) | 6acc60406 (GFC) |
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

Jellyfish/Dragonfish vector-load uses the monolithic `VectorLoadInstruction` proto packed by `EncoderJf` into the 41-byte bundle plus the LLVM `InstBits` table; the slot presence (slot-mask bit `0x040`), dest-vreg semantics, and sublane-shuffle variant are established, but the exact JXC bit offsets live in `InstBits` (a binary record, all-zero on disk for the codec path) rather than in `Field` accessors and are not bit-enumerated here.

---

## What Is Not Yet Pinned

- **Jellyfish/Dragonfish exact VMEM-load bit positions.** Slot presence, dest semantics, and the sublane-shuffle variant are established; the per-field offsets are in `InstBits`, not `Field` accessors.
- **The Offset→immediate-slot mapping.** The 2-/3-bit Offset is a slot index; the 16-bit displacement lives in `Imm2..Imm5`; the per-opcode `Offset→Imm` mapping is not fully enumerated.
- **The IAR file size** (`ReadIar0/Iar1` imply 2 IARs per slot; the register-file count `IarsPerTensorCore()` numeric value is not recovered here).
- **The wide/complement multi-vreg load destination encoding** (`V60_V61_V62_V63` quads).
- **The literal byte-range of each load slot inside the bundle.** The field word offsets inside the decoded slot struct are pinned; the absolute Pufferfish load-slot region (abs 119..140) is on the [Pufferfish 51B Bundle](bundle-pf-51b.md) page, but the per-gen slot-to-byte map for V5+ comes from each codec's `Encode` dispatch.

---

## Cross-References

- [Memory-Store Slot](slot-memory-store.md) — the write-side mirror; shared addressing-mode taxonomy and `SetIar*`/IAR sharing, plus the load/store asymmetries.
- [MemorySpace Enum](memory-space-enum.md) — the 17-value runtime enum the slot tier-selects on, and the proto↔enum remap.
- [Bundle Model](bundle-model-overview.md) — the per-generation bundle widths (41/51/64) and slot taxonomy this slot plugs into.
- [Pufferfish 51B Bundle](bundle-pf-51b.md) — the absolute bundle bits of the `vector_load` (119..140) and `cmem_load` (103..118) slots.
- [Viperfish 64B Bundle](bundle-vf-64b.md) — the V5+ `EncodeBundle` + per-slot `Encoder::Encode` + `BitCopy` model the VXC/GLC/GFC load slots are written under.
- [MC-Emitter](mc-emitter.md) — the MC-layer `SLD*`/`scVLD*`/`bcVLD*` load mnemonics and the register encoding table.
- [Memory Subsystem Overview](../memory/overview.md) — the tier model (HBM/VMEM/SMEM/CMEM/SPMEM) the load slot reads from.
