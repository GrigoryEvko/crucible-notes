# Immediate Slot

> *Every width, bit position, proto offset, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). All addresses are virtual addresses; `.text` / `.rodata` / `.lrodata` are mapped 1:1 (VA == file offset). Other wheel versions differ.*

## Abstract

A TPU VLIW bundle has no inline immediate operands. Every literal an instruction needs — a branch/call target, a sync-flag id or threshold, a vector-ALU constant, a DMA segment size — is written into one of a small, fixed pool of **immediate slots** that sit in a dedicated region of the bundle word and are *shared* across every slot in the bundle. A slot encoder never embeds a constant in its own bit window; it routes the value to an immediate-slot index, and a single per-generation encoder lays that index down at a generation-fixed absolute bit. This page documents the immediate-slot pool as a reimplementation target: the per-gen slot count and width, the byte-exact bit ladders, how a value is chosen and split across slots, how the sequencer's 20-bit branch target reaches immediate-slot 0, and how the overlay-fetch DMA descriptor consumes an immediate sync-flag.

The immediate region is the single piece of the bundle most often misread, because it is *not* inside any functional slot. Three facts make it tractable. (1) The slot **width** is a subtarget virtual, `TPUSubtarget::getImmediateSizeInBits()` at vtable slot `+0x340`: 16 bits on the BarnaCore (v4) path, 20 bits on every V5+ generation (Viperfish / Ghostlite / 6acc60406). (2) The slot **bit positions** are a flat list of `(absolute-bit, width)` pairs written by `<gen>ImmediatesEncoder::Encode` through the universal packer `BitCopy(dst, dst_bit, src, src_bit, nbits)` (`0x1fa0a900`), so the position of immediate slot *i* is the literal `dst_bit` argument of the *i*-th `BitCopy` call. (3) Which immediate slot a value lands in is decided upstream by a runtime `ResourceSolver` slot-pool walk (`getFullImmediate` @ `0x13be79a0`), not by a static per-encoding table — a value wider than one slot is split lo/hi across two slots.

For reimplementation, the contract is:

- The **per-gen immediate-slot width** is `getImmediateSizeInBits()` (vtable `+0x340`): BarnaCore = 16, Viperfish/Ghostlite/6acc60406 = 20. The same width governs both the value-fit test in `getPackedImm` and the lo/hi split in `getFullImmediate`.
- The **TensorCore (64-byte V5+) bundle has 6 immediate slots** written by `TensorCoreImmediatesEncoder::Encode` from proto fields `+0x18 + 4*i` (i = 0..5). The base ladder is Viperfish `430/410/390/370/350/330` (stride −20); Ghostlite is that ladder `+3`, 6acc60406 is that ladder `−7`.
- The **SparseCore (32-byte) bundle has 4 immediate slots** at `67/47/27/7` on all V5+ gens; Ghostlite's SCS encoder adds two more (`215/195`) for a 6-slot wide variant.
- The **Jellyfish (41-byte) and Pufferfish (51-byte) bundles each carry 6 immediate slots at 16 bits**; Pufferfish places them at `256/272/288/304/320/338` in the shared operand pool, Jellyfish carries them as proto fields `+0x68..+0x7C`. These pre-V5 codecs are *not* subtarget-virtual.
- A **value ≤ width occupies one slot; a wider value splits** into a low half and a high half across two slots, recorded as a `SmallVector<pair<slot_id, ImmediateSlot>, 4>` by `getFullImmediate`.
- The **sequencer's signed 20-bit branch/call target lands in immediate slot 0** — the same slot the barrier/sync ops reuse for the sflag id/threshold.
- The **overlay-fetch DMA descriptor** is `{hbm-src, local/timem-dst, size, sflag}`; its sflag is an immediate built from `Target::GetOverlayReservedSyncFlagNumber()` (`Target+0x534`), emitted by `EncodeOverlaysForDma` → `EmitContinuationTailcall`.

| | |
|---|---|
| **Per-gen width fn** | `TPUSubtarget::getImmediateSizeInBits()` @ vtable `+0x340` (reloc-walked `R_X86_64_RELATIVE`) |
| **Width values** | BarnaCore = 16 (`0x13c596a0`); Viperfish / Ghostlite / 6acc60406 = 20 (`0x13c5f5a0` / `0x13c61480` / `0x13c62fa0`) |
| **Universal packer** | `BitCopy(dst, dst_bit, src, src_bit, nbits)` @ `0x1fa0a900` — LSB-first; `dst_bit` == absolute bundle bit |
| **TC imm encoder** | `TensorCoreImmediatesEncoder::Encode` — VXC `0x1eebee40`, GLC `0x1f20d520`, GFC `0x1f86de20` |
| **SCS imm encoder** | `SparseCoreImmediatesEncoder::Encode` — VFC `0x1ee75ee0`, GLC `0x1eb563c0`, GFC `0x1eb5bd20` |
| **TC imm ladder** | VXC `430/410/390/370/350/330` (slot 0..5, stride −20, each w20); GLC = VXC+3 (`433..333`); GFC = VXC−7 (`423..323`) |
| **SCS imm ladder** | `67/47/27/7` (all V5+); GLC adds `215/195` (wide 6-slot variant) |
| **Slot allocator** | `ResourceSolver::getFullImmediate` @ `0x13be79a0` (pool walk: `+0xd8` records, `+0x118` candidate list, `+0x120` count) |
| **Value chooser** | `getPackedImm` @ `0x13bec4e0` (`call [vtable+0x340]` ×4 = value-fit test) |
| **Integer-imm family** | encoding-id `0x2c` = SyImm32 (`getFirstSyImm32Encoding`); `0x1a` = VyImm32 (`getFirstVyImm32Encoding`) |
| **PF / JF imm** | 6 × 16-bit — PF @ `256/272/288/304/320/338`; JF proto `+0x68..+0x7C` (not subtarget-virtual) |
| **Branch target home** | immediate slot 0 (signed 20-bit; `EmitImmediate<…Immediates>(slot=0, …)`) |
| **Overlay DMA sflag** | `Target::GetOverlayReservedSyncFlagNumber()` = `Target+0x534`; `EncodeOverlaysForDma` @ `0x14095f40` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Immediate-Slot Width Is a Subtarget Virtual

The width of a single immediate slot is not a constant baked into the encoder — it is read through the subtarget vtable, slot `+0x340`, which resolves per generation to `TPUSubtarget::getImmediateSizeInBits()`. The four concrete overrides are one-instruction functions, and the return constants are unambiguous:

```c
// TPUBcSubtarget::getImmediateSizeInBits   @ 0x13c596a0
return 16;   // BarnaCore (v4 path) — 16-bit immediate slot
// TPUVfcSubtarget::getImmediateSizeInBits  @ 0x13c5f5a0
return 20;   // Viperfish (v5e)
// TPUGlcSubtarget::getImmediateSizeInBits  @ 0x13c61480
return 20;   // Ghostlite (v6e)
// TPUGfcSubtarget::getImmediateSizeInBits  @ 0x13c62fa0
return 20;   // 6acc60406 (v7x / TPU7x)
```

The abstract base `TPUSubtarget`'s `+0x340` slot is `__cxa_pure_virtual`; there is no `TPUPfSubtarget` or `TPUJfSubtarget` class, so the pre-V5 generations do not expose their immediate width through this virtual at all (see [Pre-V5 Generations](#pre-v5-generations-pufferfish-and-jellyfish)).

This width is exactly the `BitCopy` field width emitted by the V5+ immediate encoders — `getImmediateSizeInBits` *is* the per-gen immediate-slot width, and the encoder writes `w20` cells because the subtarget says 20. Two consumers read it:

- `getPackedImm` (`0x13bec4e0`) tests how many high bits of a candidate value are non-zero by `call [TPUSubtarget vtable+0x340]` (four times, for the four integer-immediate encoding classes) and shifting by the returned width — the value-fit test that decides whether a constant fits in one slot.
- `getFullImmediate` (`0x13be79a0`) uses the same width to split a too-wide value into a 16-bit low half and a 16-bit high half (the proto immediate field is 32-bit; the slot is 20 or 16, so a full-range value occupies two slots).

> **NOTE —** the V5+ slot is 20 bits but the lo/hi split in `getFullImmediate` records 16-bit halves of the source proto field. A reimplementation must not conflate the *slot width* (20, the room in the bundle) with the *split granularity* (the 16-bit halves of the 32-bit proto immediate). A single 20-bit slot still only carries one half of a split value.

---

## TensorCore Immediate Ladder (V5+, 64-byte bundle)

The TensorCore bundle ([Viperfish 64B](bundle-vf-64b.md)) carries **6 immediate slots**, written by `TensorCoreImmediatesEncoder::Encode`. Each generation's encoder reads the same six proto fields (`TensorCoreImmediates` message offsets `+0x18, +0x1c, +0x20, +0x24, +0x28, +0x2c` = words `a2[6..11]`, i.e. proto field = `+0x18 + 4*i`) and `BitCopy`s each to a generation-fixed absolute bit, width 20. The Viperfish encoder reads literally:

```c
// asic_sw::deepsea::vxc::isa::TensorCoreImmediatesEncoder::Encode  @ 0x1eebee40
v17[0] = a2[6];  BitCopy(buf, 430, v17, 0, 20);   // imm slot 0
v17[0] = a2[7];  BitCopy(buf, 410, v17, 0, 20);   // imm slot 1
v17[0] = a2[8];  BitCopy(buf, 390, v17, 0, 20);   // imm slot 2
v17[0] = a2[9];  BitCopy(buf, 370, v17, 0, 20);   // imm slot 3
v17[0] = a2[10]; BitCopy(buf, 350, v17, 0, 20);   // imm slot 4
v17[0] = a2[11]; BitCopy(buf, 330, v17, 0, 20);   // imm slot 5
```

The ladder is a uniform stride of −20 bits, slot 0 highest. Ghostlite (`0x1f20d520`) and 6acc60406 (`0x1f86de20`) are field-identical structurally — same six proto reads, same width 20 — but shifted by a constant per generation:

| imm slot | proto field | Viperfish (VXC) bit | Ghostlite (GLC) bit | 6acc60406 (GFC) bit | width | Confidence |
|---|---|---|---|---|---|---|
| 0 | `+0x18` | 430 | 433 | 423 | 20 | CONFIRMED |
| 1 | `+0x1c` | 410 | 413 | 403 | 20 | CONFIRMED |
| 2 | `+0x20` | 390 | 393 | 383 | 20 | CONFIRMED |
| 3 | `+0x24` | 370 | 373 | 363 | 20 | CONFIRMED |
| 4 | `+0x28` | 350 | 353 | 343 | 20 | CONFIRMED |
| 5 | `+0x2c` | 330 | 333 | 323 | 20 | CONFIRMED |

The per-generation delta is exact: **Ghostlite = Viperfish + 3**, **6acc60406 = Viperfish − 7**, applied uniformly to every slot. The shift reflects a different layout of the slots *above* the immediate region (the scalar/sequencer slots at the top of the 512-bit word differ per gen); the immediate ladder itself keeps its −20 internal stride. Slot 0 is the home of the sequencer branch/call target — on 6acc60406 it is bit 423, matching the [Sequencer Slot](slot-sequencer.md) page's branch-target observation.

> **NOTE —** the proto-field-to-slot binding is rigid: immediate slot *i* is always `TensorCoreImmediates` field `+0x18 + 4*i`, identical across all three V5+ generations. The only thing that changes per gen is the absolute bit the value is laid down at. A decoder built for one V5+ gen ports to the others by applying the `+3` / `−7` offset.

---

## SparseCore Immediate Ladder (V5+, 32-byte bundle)

The SparseCore scalar (SCS) bundle is 32 bytes and carries **4 immediate slots** on Viperfish and 6acc60406, written by `SparseCoreImmediatesEncoder::Encode` (VFC `0x1ee75ee0`; the GFC variant is named `SparseCoreScalarImmediatesEncoder`, `0x1eb5bd20`):

```c
// asic_sw::deepsea::vxc::vfc::isa::SparseCoreImmediatesEncoder::Encode  @ 0x1ee75ee0
v13[0] = a2[6]; BitCopy(buf, 67, v13, 0, 20);   // imm slot 0
v13[0] = a2[7]; BitCopy(buf, 47, v13, 0, 20);   // imm slot 1
v13[0] = a2[8]; BitCopy(buf, 27, v13, 0, 20);   // imm slot 2
v13[0] = a2[9]; BitCopy(buf,  7, v13, 0, 20);   // imm slot 3
```

Ghostlite's SCS encoder (`0x1eb563c0`) is a **6-slot wide variant**: the same four base slots plus two more reading proto fields `+0x28`/`+0x2c` (`a2[10]`/`a2[11]`):

```c
// asic_sw::deepsea::gxc::glc::isa::SparseCoreImmediatesEncoder::Encode  @ 0x1eb563c0
...same 67/47/27/7 as above...
v17[0] = a2[10]; BitCopy(buf, 215, v17, 0, 20);  // imm slot 4 (GLC only)
v17[0] = a2[11]; BitCopy(buf, 195, v17, 0, 20);  // imm slot 5 (GLC only)
```

| imm slot | proto field | VFC bit | GLC bit | GFC bit | width | Confidence |
|---|---|---|---|---|---|---|
| 0 | `+0x18` | 67 | 67 | 67 | 20 | CONFIRMED |
| 1 | `+0x1c` | 47 | 47 | 47 | 20 | CONFIRMED |
| 2 | `+0x20` | 27 | 27 | 27 | 20 | CONFIRMED |
| 3 | `+0x24` | 7 | 7 | 7 | 20 | CONFIRMED |
| 4 | `+0x28` | — | 215 | — | 20 | CONFIRMED (GLC only) |
| 5 | `+0x2c` | — | 195 | — | 20 | CONFIRMED (GLC only) |

The base 4-slot ladder (`67/47/27/7`) is byte-identical across all three V5+ gens — unlike the TensorCore ladder, the SCS ladder has *no* per-gen offset. Only Ghostlite adds the high pair (`215/195`), in a separate higher region of the 32-byte word, for ops needing more than four constants.

---

## How a Value Is Chosen and Placed — the ResourceSolver Walk

The bit ladders above say *where* each immediate slot lives; they do not say *which* slot a given constant goes in. That decision is a runtime allocation over a per-program slot pool, not a static per-encoding table. Two functions own it.

`getPackedImm` (`0x13bec4e0`) classifies the value. The integer-immediate family is encoding-id `0x2c` (`SyImm32`) for scalar immediates and `0x1a` (`VyImm32`) for vector immediates; the chooser writes OpEnc class `5` and selects via `getFirstSyImm32Encoding` / `getFirstVyImm32Encoding`. It then runs the value-fit test by `call [TPUSubtarget vtable+0x340]` (= `getImmediateSizeInBits`) to learn how many bits a slot holds.

`ResourceSolver::getFullImmediate` (`0x13be79a0`) walks the slot pool. The `ResourceSolver` `this` carries the pool, not the subtarget:

```c
// ResourceSolver::getFullImmediate  @ 0x13be79a0  (offsets relative to this == a1)
*(_DWORD *)out      = 5;                                   // OpEnc class 5 (integer-imm)
*(_BYTE *)(out + 5) = getFirstSyImm32Encoding(0, ...);     // encoding-id 0x2c (or 0x1a for VyImm32)
count   = *(uint *)(a1 + 0x120);    // +0x120: candidate-slot list count
list    = *(uint **)(a1 + 0x118);   // +0x118: candidate-slot id pairs (walked lo-half then hi-half)
records =  *(_QWORD *)(a1 + 0xd8);  // +0xd8 : ImmediateSlot[] records, 12 B each
                                    //         +0x0 allowed-value/constraint, +0x8 present flag
for (entry in list) {
    slot = list[entry];
    if (!_bittest64(&slotmask, slot)) continue;            // slot legal for this op?
    if (records[slot].present != 1)  continue;             // slot usable in this bundle?
    // match value.lo16 / value.hi16 against records[slot] allowed-value, record (half, ImmediateSlot)
    push_back(out_vec, pair(slot, ImmediateSlot{slot}));   // SmallVector<pair<uint,ImmediateSlot>,4>
}
```

The pool is built per program in the `ResourceSolver` ctor (`0x13beb0a0`) from a per-gen `MCBundleInfo` (`MCBundleInfoFactory::create` @ `0x13c799e0`, dispatched on the CPU string), whose slot-list-provider virtuals (`[+0x360]`/`[+0x368]`) populate the `+0x118` candidate list. The `MaxImms` (`0x224e2b00`) / `MinImmSlot` (`0x224e2bb8`) BSS globals bound the slot range. So the "immediate-slot resource table" is the per-gen SparseCore SCS/TEC sequencer slot inventory materialised into the runtime pool — the width each slot carries is `getImmediateSizeInBits`, and the bit it lands at is the encoder ladder above.

The chain is therefore: **operand-type → OpEnc class → integer-imm encoding-id `0x2c` → `ResourceSolver` picks a free slot index → proto field `+0x18 + 4*idx` → `<gen>ImmediatesEncoder::Encode` lays it at the per-gen bit.** A value that fits the slot width occupies one slot; a wider value splits into low and high halves across two slots (the `value.lo16` / `value.hi16` walk above), and the two halves take two distinct candidate slots from the pool.

The operand → OpEnc class mapping is the `ImmediateCompatibilityTable` (`0xaed36d0`, 17 × 12 B, `{OperandType, compat_mask, OpEnc_class}`), looked up by `getOperandTypeRecord` (`0x13c63b80`, binary search). `OperandType 4` → class `5` (the integer-immediate chooser), compat `0x0f` (ZeroExt/OneExt/Shl/Imm32 all permitted). `canAddImmInternal` (`0x13bebce0`) reads the operand-type byte at the `MCInstrDesc` operand record `+0x23` and feeds it here before calling `getPackedImm` / `getFullImmediate`.

---

## The Sequencer Branch Target Lives in Immediate Slot 0

The single most important consumer of an immediate slot is the sequencer. A branch or call target is **not** stored in the sequencer slot's own bytes — it is a signed 20-bit value placed in **immediate slot 0** of the bundle, and only an opcode discriminator in the sequencer slot decides whether that value is a PC-relative delta or an absolute bundle index (see [Sequencer Slot](slot-sequencer.md)). The V5+ SCS branch/call emitters write it through the same immediate path:

```c
// isa_emitter::EmitBranchOp<…BranchRelative>  @ 0x13a5d3e0  (abs/rel are field-identical)
target = MCInst.getOperand(0).Imm;
if ((uint64_t)(target + 0x80000) >= 0x100000) return RetCheckFail();  // signed-20-bit range
EmitImmediate<SparseCoreImmediates>(/*slot=*/0, target);              // → immediate slot 0
```

The range check `(value + 0x80000) < 0x100000` is precisely `−524288 .. +524287` — a signed 20-bit field, the full width of one V5+ immediate slot (`getImmediateSizeInBits = 20`). On Trillium that slot 0 is TC bit 423; on Viperfish, 430. Because the branch target fits one slot, it never triggers the lo/hi split. The barrier/sync ops (`EmitBarrierSync<…ScalarMisc>`) reuse the *same* immediate slot 0 for the sflag id/threshold, which is why a decoder must read the bundle's immediate region — not the sequencer slot bytes — to recover a branch offset.

> **GOTCHA —** the LLVM-MC code emitter contributes *zero* bits to a branch's encoding; the MC `InstBits` record for `BRrel`/`BRabs`/`CALLrel`/`CALLabs` is all zero. The real 20-bit offset is written by the proto-bundle `EmitImmediate` path into immediate slot 0. A reimplementation that looks for the offset in the MC-emitted scalar bytes finds nothing. See [Sequencer Slot](slot-sequencer.md).

---

## Pre-V5 Generations: Pufferfish and Jellyfish

The pre-V5 generations carry **6 immediate slots, each 16 bits**, but through a *different codec* — there is no subtarget-virtual width function (`getImmediateSizeInBits` is pure-virtual in the base, and no `TPUPfSubtarget`/`TPUJfSubtarget` exists). The BarnaCore (`TPUBcSubtarget`) override returns 16, consistent with the pre-V5 16-bit slot, but the BarnaCore/Pufferfish slot *positions* are not laid down by a subtarget-driven encoder.

- **Pufferfish (51-byte bundle, [PF 51B](bundle-pf-51b.md))** packs the 6 immediates as a `SlotMap<ImmValue, 6>` in the shared operand pool at absolute bits **`256 / 272 / 288 / 304 / 320 / 338`** (16 bits each), bound at proto-build time by `SetImmOrDie` / `VisitImmediateSlots<0..5>` rather than by a `getImmediateSizeInBits`-driven `BitCopy` ladder. The slot allocation is a SlotMap over one shared pool — Pufferfish's analog of the `ResourceSolver` walk — and a bundle can name at most six distinct immediates across all of its present slots combined.
- **Jellyfish (41-byte bundle, [JF 41B](bundle-jf-41b.md))** carries the six 16-bit immediates as `Bundle` proto fields `+0x68 .. +0x7C`, validated `< 0x10000` by `ValidateImmediate` (`0x1e86da20`), keyed off the `proto+0x10` slot mask. Slot assignment is `JellyfishEmitter::FindFreeSlot`.

| gen | bundle | imm slots | width | positions / proto | codec | Confidence |
|---|---|---|---|---|---|---|
| Jellyfish (v3) | 41 B | 6 | 16 | proto `+0x68..+0x7C` | `Bundle` proto + `FindFreeSlot` | CONFIRMED (see [JF 41B](bundle-jf-41b.md)) |
| Pufferfish (v4) | 51 B | 6 | 16 | `256/272/288/304/320/338` | shared SlotMap + `SetImmOrDie` | CONFIRMED (see [PF 51B](bundle-pf-51b.md)) |
| BarnaCore (v4 BC) | — | — | 16 (`getImmediateSizeInBits`) | `BarnaCore*MinImm` fields | subtarget width only | LOW (positions not walked) |
| Viperfish (v5e) | 64 B TC / 32 B SCS | 6 / 4 | 20 | TC `430/410/390/370/350/330`; SCS `67/47/27/7` | `TensorCoreImmediatesEncoder` / `SparseCoreImmediatesEncoder` | CONFIRMED |
| Ghostlite (v5p) | 64 B TC / 32 B SCS | 6 / 4(+2) | 20 | TC `433/413/393/373/353/333`; SCS `67/47/27/7(+215/195)` | same (GLC) | CONFIRMED |
| Trillium (v6e) | 64 B TC / 32 B SCS | 6 / 4 | 20 | TC `423/403/383/363/343/323`; SCS `67/47/27/7` | same (GFC) | CONFIRMED |

> **NOTE — the BarnaCore bit positions are not recovered.** `getImmediateSizeInBits` confirms the BarnaCore immediate slot is 16 bits wide, but the bit positions live in the `BarnaCoreSequencerScalar*ScalarFloatMinImm0..3` / `BarnaCoreChannelVectorAlu*VectorFloatMinImm0..5` accessors (a different `GetConcatenatedValue` codec, not a `BitCopy` ladder) and were not walked. This is the only gap in the per-gen ladder. Marked LOW. The clean V5+ table (VF/GL/GF, w20) and the PF/JF 16-bit pool are complete.

---

## EncodeOverlaysForDma's Use of an Immediate Slot

The immediate-slot mechanism extends past the per-instruction operand pool into the overlay-fetch DMA path. When a program is too large for local memory, the backend divides it into overlay segments and emits a DMA descriptor per segment to stream each one from HBM. `EncodeOverlaysForDma` (`0x14095f40`, dispatched as the `XLA_EncodeOverlaysForDma` fiber task) is the per-overlay parallel encoder: it iterates the overlay array (`BuildContext+0x98`, count `+0xa0`) and, per overlay, fans each out onto a `thread::Bundle` (or runs inline below a threshold), then `JoinAll`s.

The per-overlay lambda (`0x14096060`) reads the `IsaProgram` (`IsaProgramsCase` → `GetIsaProgramUtil`), indexes the overlay, reads its segment-start word (`overlay+0xf8`) and set-value (`overlay+0x100`), adds the program base, and stores the result `LloAddress` into the `OverlayMetadata_Overlay` proto via an `IsaProgramUtil` virtual (the `[util+0x28]` setter; the lambda also calls `[util+0x178]` proto getter and `[util+0x160]` `FindOverlaySegment`). The per-segment descriptor record is `{overlay_number, overlay_begin, overlay_address / overlay_address_byte_offset, overlay_size, overlay_order}`.

The on-chip realisation of that descriptor is a `{hbm-src, local/timem-dst, size, sflag}` DMA, and its **sflag is an immediate** built from the overlay-reserved sync-flag number. The TensorCore continuation fetch is `EmitContinuationTailcall` (`0x12718ca0`), whose overlay-fetch sequence reads byte-exactly:

```c
// EmitContinuationTailcall  @ 0x12718ca0  (overlay-fetch segment, ~line 599)
sflag_no = Target::GetOverlayReservedSyncFlagNumber(target);          // = Target+0x534
sflag    = LloRegionBuilder::SflagImmPtr(rb, sflag_no, "overlay reserved sync flag", 26);
src      = LloRegionBuilder::CastAddr(rb, value, MemorySpace);        // overlay HBM source
size     = LloRegionBuilder::LloMemUnitFromGranules(rb, seg_size);    // segment size in granules
LloRegionBuilder::EnqueueDmaLocalInGranules(rb, src, dst, size, _, sflag, 0, 0, 0);
LloRegionBuilder::DmaDoneInGranules(rb, ..., sflag);                  // "continuation-tail-wait"
```

So the DMA descriptor's sflag operand is an `SflagImmPtr` immediate — the same immediate-operand machinery, here carrying a reserved sync-flag id rather than a branch target. The next-segment source and size come from program-descriptor words held in Target-local SMEM, all `mov rax,[rdi+N]; ret` accessors:

| descriptor field | source (compile time) | on-chip realisation | Confidence |
|---|---|---|---|
| hbm-src address | `OverlayMetadata_Overlay.overlay_address(_byte_offset)` | `Target::OverlayAddressWordOffset()` = `Target+0x810`; `CastAddr` | CONFIRMED |
| local/timem dst | overlay workspace | `Target::OverlayWorkspaceWordOffset/SizeWords()` = `Target+0x738`/`+0x740` | CONFIRMED |
| size | `OverlayMetadata_Overlay.overlay_size` | `LloMemUnitFromGranules` (granule count) | CONFIRMED |
| **sflag (immediate)** | overlay-reserved sync flag | `Target::GetOverlayReservedSyncFlagNumber()` = `Target+0x534`; `SflagImmPtr` | CONFIRMED |
| segment-begin | `OverlayMetadata_Overlay.overlay_begin` | first bundle index of segment | CONFIRMED |
| order | `OverlayMetadata_Overlay.overlay_order` | "natural" pack/fetch order | CONFIRMED |

The SparseCore tile-overlay path (`overlayer::OverlayProgram` @ `0x1395bba0`) hand-builds an `scDMA_HBM_TO_TIMEM_SIMPLErrrii` (opcode `0xfd7`) whose sflag operand is the tile-overlay sflag (`SCTarget+0x1e8`) wrapped as a **`SyImm32` (encoding-id `0x2c`)** via `getFirstSyImm32Encoding` — the same integer-immediate family the `ResourceSolver` allocates into a slot. This is the direct link between the immediate-slot codec and the overlay DMA: the overlay sflag is a `SyImm32` immediate (see [TPUMCImm / SyImm32](tpumcimm-syimm32.md)).

The runtime loader reads each segment as a `{byte_offset, block.word_count}` pair (the `BcsProgram` program header), bounded by the invariant `overlay_address_byte_offset + overlay_address_block.word_count * bytes_per_word <= read_start + dma_alignment`. This is the concrete HBM fetch payload the descriptor above resolves to.

---

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the VLIW issue-word contract and the `(TpuVersion, TpuSequencerType)` codec-metadata table.
- [Viperfish 64B Bundle](bundle-vf-64b.md) — the TensorCore bundle that hosts the 6 × 20-bit immediate ladder.
- [Pufferfish 51B Bundle](bundle-pf-51b.md) — the 6 × 16-bit immediate SlotMap at `256/272/288/304/320/338`.
- [Jellyfish 41B Bundle](bundle-jf-41b.md) — the 6 × 16-bit immediate proto fields `+0x68..+0x7C`.
- [Sequencer Slot](slot-sequencer.md) — the signed 20-bit branch/call target that lands in immediate slot 0.
- [TPUMCImm / SyImm32](tpumcimm-syimm32.md) — the integer-immediate operand (`encoding-id 0x2c`) the allocator places into a slot.
