# Per-Engine Bundle Slot-Base Map

> *Every bundle-bit position, slot base, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`) — from the `BitCopy` destination-bit immediates inside each per-slot `Encoder::Encode`. Other versions differ.*

## Abstract

This page is the **consolidated slot-base index** for the three SparseCore sequencer-engine bundles. Each SparseCore VLIW bundle — SCS (32 bytes / 256 bits), TAC (64 bytes / 512 bits), TEC (64 bytes / 512 bits) — is a fixed stack of slots packed by a single little-endian bit packer. The per-engine pages ([SCS](scs-engine.md), [TAC](tac-engine.md), [TEC](tec-engine.md)) document each slot's internal field template and opcode roster in full; this page consolidates the one thing they share and that a reimplementer most needs in one place: **the absolute bundle-bit base of every slot, the slot ordering, and the per-generation deltas**. It is the SparseCore analog of the TensorCore LLO bundle slot maps — a single cross-engine partition table, not a re-derivation of each opcode.

The recovery method is uniform across all three engines and is what makes the bit positions *absolute*. Each engine's codec dispatcher (`SparseCore{Scs,Tac,Tec}CodecBase<…>::Encode`) calls every per-slot `<Slot>Encoder::Encode` in turn and hands every one of them the *same* output-buffer `absl::Span<uchar>` (the `rdx=buf.ptr`, `rcx=buf.len` pair is constant across all calls; only the `rdi` member-encoder pointer differs). Each slot encoder then writes its fields with the generic packer `BitCopy(dst, dst_bitoff, src, src_bitoff, nbits)` (`0x1fa0a900`), and the `dst_bitoff` immediate (`mov esi, IMM`) is therefore the *absolute* bundle bit, not a slot-relative one. A slot's bit extent is `[min dst_bitoff, max dst_bitoff + width)` over all its `BitCopy` calls; the opcode bit is the field at slot-relative `+16` (scalar) or `+24` (vector). The bundle byte size is read from `EncoderBase::BundleSizeBytes` (codec-metadata vtable slot 6, `vtable[+0x30]`): SCS 32, TAC 64, TEC 64.

The three maps share a **common low region**. SCS, TAC, and TEC all place the same slot stack in bits 7..191 — four 20-bit immediates, a 24-bit vector-scalar bridge, and three 27-bit scalar slots (`ScalarMisc`/`ScalarAlu1`/`ScalarAlu0` at bases 111/138/165, opcodes at 127/154/181). Above bit 191 the engines diverge: SCS pads to 256 bits, TAC pads its remaining 320 bits empty (no vector path), and TEC fills a vector compute region (bits 195..474 on gfc) that the other two leave blank. The Dma and Stream "slots" are not separate physical regions on any engine — they are `oneof` forms of a scalar lane (opcode `@181`/`@154`) that borrow lower payload bits (and, on TEC, reach up into bits 283/322). This page presents the unified partition; the field-level templates and opcode rosters live on the engine pages and the [scalar](scalar-opcode-enum.md)/[vector](vector-opcode-enum.md) opcode-enum pages.

This is a reference index page, not a reimplementable algorithm; the reimplementation contract for the bundles themselves lives on the engine pages. What this page guarantees:

- **The absolute slot-bit base of every slot on every engine**, in one cross-engine table, decompile-anchored.
- **The fixed slot ordering** (low region identical across all three engines; the TEC vector region above bit 191).
- **The per-generation deltas** (VF/GL/GF): which slots exist, the TEC vector-ALU width growth, and the engine roster (TAC presence).

| | |
|---|---|
| **Engines mapped** | SCS (seq 3, 32 B) · TAC (seq 4, 64 B, VF/GL only) · TEC (seq 5, 64 B) |
| **Bit packer** | `BitCopy(dst, dst_bitoff, src, src_bitoff, nbits)` `0x1fa0a900` (little-endian) |
| **Bit positions are** | **absolute** — dispatcher passes every slot encoder the same buffer `Span` |
| **Common low region** | bits 7..191 — 4×20-bit immediates + VectorScalar + Misc/Alu1/Alu0, identical on all three engines |
| **Scalar opcode bits** | `@127` (Misc) · `@154` (Alu1) · `@181` (Alu0) — base+16; identical on all three engines |
| **TEC vector opcode bits (GF)** | Result `@239` · Load `@283` · Store `@353` · Alu2/1/0 `@388/425/462` |
| **Bundle sizes** | SCS 32 · TAC 64 · TEC 64 (codec-metadata `vtable[+0x30]`) |
| **Check trailer** | none on any SC bundle (no `0x55`); all-zero bundle = NOP |
| **Confidence** | CONFIRMED (`BitCopy`-immediate anchored) unless a row or callout says otherwise |

---

## The Consolidated Slot-Base Table

### How to read it

The table below is the cross-engine partition: one row per slot, with the absolute bundle-bit base and end for each engine column (an em-dash means the slot does not exist on that engine). All three engines share the low region (bits 7..191) byte-for-byte; the columns diverge only above bit 191. The opcode bit (where a slot has one) is shown in the per-engine maps that follow; in this consolidated table the opcode bit is `base + 16` for the scalar slots and the value given for the TEC vector slots.

> **NOTE — the bit positions are absolute, not slot-relative.** Prior decode work reported a Stream/Dma opcode "at bit 181" and a scalar opcode "at bit 154/127" *relative to the slot*. Because the dispatcher hands every slot encoder the same buffer `Span`, those numbers are the *absolute* bundle bits: `@181` is the bundle-bit base of scalar lane 0's opcode field, full stop. A reimplementer composing a bundle writes each slot at its absolute base; there is no per-slot origin to add.

### The cross-engine map

| Slot | SCS (32 B) | TAC (64 B, VF/GL) | TEC (64 B) | Width | Confidence |
|---|---|---|---|---:|---|
| (reserved header) | 0..6 | 0..6 | 0..6 | 7 | MEDIUM |
| `Immediates` (low) | 7..86 | 7..86 | 7..86 | 80 | CONFIRMED |
| `VectorScalar` (bridge) | 87..110 | 87..110 | 87..110 | 24 | CONFIRMED |
| `ScalarMisc` (op `@127`) | 111..137 | 111..137 | 111..137 | 27 | CONFIRMED |
| `ScalarAlu1` (lane 1, op `@154`) | 138..164 | 138..164 | 138..164 | 27 | CONFIRMED |
| `ScalarAlu0` (lane 0, op `@181`) | 165..191 | 165..191 | 165..191 | 27 | CONFIRMED |
| `Immediates` (high, 2×20-bit) | — | — | 195..234 | 40 | CONFIRMED |
| `VectorResult` (op `@239`) | — | — | 239..260 | 22 | CONFIRMED |
| `VectorExtended` (op `@261`) | — | — | 261..461 | ~201 | HIGH |
| `VectorLoad` (op `@283`) | — | — | 283..321 | 39 | CONFIRMED |
| `VectorStore` (op `@353`) | — | — | 328..363 | 36 | CONFIRMED |
| `VectorAlu2` (lane 2, op `@388`) | — | — | 364..400 | 37 | CONFIRMED |
| `VectorAlu1` (lane 1, op `@425`) | — | — | 401..437 | 37 | CONFIRMED |
| `VectorAlu0` (lane 0, op `@462`) | — | — | 438..474 | 37 | CONFIRMED |
| (reserved / pad) | 192..255 | 192..511 | 475..511 | — | HIGH |
| `Dma` (oneof of lane) | 87..191 | 87..191 | 87..327 | — | CONFIRMED |
| `Stream` (oneof of lane) | 99..191 | 99..191 | 99..327 | — | CONFIRMED |

> **QUIRK — Dma and Stream are not separate slots; they are oneof forms of a scalar lane.** On every engine, a DMA or Stream instruction writes its opcode into a scalar lane's opcode field (`@181` lane 0, `@154` lane 1) and spills its multi-word descriptor into lower (and, on TEC, higher) payload bits. There is no physically separate "DMA region." A reimplementer who allocates one will double-book the lane and immediate bits. The SCS/TAC Dma descriptor stays in bits 87..142; the TEC Dma/Stream reaches up into bits 283/322 (overlapping the vector load/store region — see the [TEC map](#tec-bundle-map-64-bytes--512-bits-gf)).

> **GOTCHA — the slot bits are engine-agnostic; the engine is chosen by attribute, not by the bundle.** `ScalarAlu0`'s opcode `@181` occupies the identical bit field on SCS, TAC, and TEC, and consumes the identical `SparseCoreScalarAlu` proto enum. Nothing in the bundle bits names the engine. The engine is fixed by the `sc.sequencer` string attribute (`"scs"`/`"execute"`) on the enclosing outlined function, read back to select the per-engine codec (`TpuSequencerType` {3=SCS, 4=TAC, 5=TEC}). See [Region → Sequencer Outliner](region-to-sequencer-outliner.md).

---

## SCS Bundle Map (32 bytes / 256 bits)

The narrowest bundle and the only one byte-identical across all three SC generations (Viperfish/vfc, Ghostlite/glc, gfc). No slot encoder writes below bit 7 or above bit 191; bits 0..6 are a reserved/header prefix and 192..255 are padding. Documented in full on [SCS (Scalar) Engine](scs-engine.md); the slot bases:

```text
SCS bundle — 32 bytes / 256 bits (VF / GL / GF identical)
bit:  0    7              87       111      138      165      192        255
      ┌────┬──────────────┬────────┬────────┬────────┬────────┬──────────┐
      │rsvd│ Immediates   │ Vector │ Scalar │ Scalar │ Scalar │ reserved │
      │7b  │ 4×20-bit     │ Scalar │ Misc   │ Alu1   │ Alu0   │ / pad    │
      │hdr │ @7/27/47/67  │ bridge │ op@127 │ op@154 │ op@181 │ 64 bits  │
      └────┴──────────────┴────────┴────────┴────────┴────────┴──────────┘
                            24-bit   27-bit   27-bit   27-bit
      Dma   (oneof of lane): opcode @181/@154, descriptor payload @87..142
      Stream(oneof of lane): opcode @181/@154, descriptor payload @99..142
```

| Slot | Base | End | Width | Opcode bit | Confidence |
|---|---:|---:|---:|---:|---|
| (reserved header) | 0 | 6 | 7 | — | MEDIUM |
| `ScalarImmediates` | 7 | 86 | 80 | — | CONFIRMED |
| `VectorScalar` | 87 | 110 | 24 | — | CONFIRMED |
| `ScsScalarMisc` | 111 | 137 | 27 | 127 | CONFIRMED |
| `ScalarAlu1` | 138 | 164 | 27 | 154 | CONFIRMED |
| `ScalarAlu0` | 165 | 191 | 27 | 181 | CONFIRMED |
| (reserved / pad) | 192 | 255 | 64 | — | HIGH |

Decompile cross-check — gfc `SparseCoreScalarAlu0Encoder::Encode` (`0x1eb693c0`) writes its `BitCopy` destination bits at `165` (x0, w5), `170` (ScalarY, w6), `176` (x1, w5), `181` (opcode, w6), and the predication header at `187/190/191` — exactly the 27-bit scalar template at slot base 165. The encoder dispatcher is gfc `SparseCoreScsCodecBase::Encode` (`0x1391ef60`).

---

## TAC Bundle Map (64 bytes / 512 bits, VF/GL only)

A 64-byte bundle that **reuses the SCS low region (bits 7..191) and leaves the upper 320 bits empty** — TAC has no vector path, so its width buys concurrent scalar address-op parallelism, not vector compute. Present only on Viperfish (`vxc.vfc`) and Ghostlite (`gxc.glc`); **absent on gfc** (the TAC codec survives there only as a standalone legacy path — see [Per-Generation Deltas](#per-generation-deltas)). Documented in full on [TAC Engine](tac-engine.md); the slot bases:

| Slot | Base | End | Width | Opcode bit | Confidence |
|---|---:|---:|---:|---:|---|
| `ScalarImmediates` | 7 | 86 | 80 | — | CONFIRMED |
| `VectorScalar` | 87 | 110 | 24 | — | CONFIRMED |
| `ScalarMisc` | 111 | 137 | 27 | 127 | CONFIRMED |
| `TacScalarAlu1` | 138 | 164 | 27 | 154 | CONFIRMED |
| `TacScalarAlu0` | 165 | 191 | 27 | 181 | CONFIRMED |
| `Dma` (oneof of lane) | 87 | 191 | — | 181 / 154 | CONFIRMED |
| `TacStream` (oneof of lane) | 99 | 191 | — | 181 / 154 | CONFIRMED |
| (reserved / pad) | 192 | 511 | 320 | — | CONFIRMED |

Decompile cross-check — glc `TacScalarAlu0Encoder::Encode` (`0x1ea17e40`) writes its `BitCopy` destination bits at `165/170/176/181/187/191`, byte-identical to the SCS `ScalarAlu0` slot. Sweeping the `BitCopy` immediates across `TacScalarAlu0` (`0x1ea17e40`), `TacScalarAlu1` (`0x1ea2a7a0`), `TacStream` (`0x1ea338e0`), and the shared `SparseCoreDmaEncoder` (`0x1ea09b40`), the highest bit any TAC slot writes is **191** — the upper 320 bits of the 512-bit bundle are pure padding.

> **QUIRK — TAC is 64 bytes wide yet carries no compute.** A reimplementer sizing buffers from "is it a 64-byte bundle?" will over-provision a tile-fetch program by assuming vector capacity that does not exist. The width is for scalar address-op parallelism, not for the vector slots a 64-byte TEC bundle uses.

---

## TEC Bundle Map (64 bytes / 512 bits, GF)

The only SC engine with a vector path. The low region (bits 7..191) is the *same* slot stack as SCS; above bit 191 sits a vector compute region (bits 195..474 on gfc) that SCS and TAC leave empty: two more 20-bit immediate slots, then `VectorResult`, `VectorExtended`, `VectorLoad`, `VectorStore`, and the three stacked vector-ALU lanes. Documented in full on [TEC (Vector) Engine](tec-engine.md); the slot bases (gfc):

```text
TEC bundle — 64 bytes / 512 bits (gfc)
bit: 0   7          87   111 138 165  195    239   261       283    328   364  401  438     475   511
     ┌───┬──────────┬────┬───┬───┬───┬──────┬─────┬─────────┬──────┬─────┬────┬────┬───────┬──────┐
     │rsv│Immed.(low│Vec │Sc │Sc │Sc │Immed.│Vec  │ Vector  │Vector│Vec  │Vec │Vec │Vector │rsvd/ │
     │   │4×20b     │Scal│Mis│Al1│Al0│(high)│Resul│Extended │Load  │Store│Alu2│Alu1│Alu0   │pad   │
     │   │@7/27/47..│brdg│@12│@15│@18│@195..│@239 │ scan/   │@283  │@353 │@388│@425│@462   │      │
     └───┴──────────┴────┴───┴───┴───┴──────┴─────┴ sort────┴──────┴─────┴────┴────┴───────┴──────┘
     ◄──────── SCS low region (7..191, identical) ────────►◄──────── TEC vector region (195..474) ────────►
     TecDma   (oneof of lane): scalar opcode @181, high payload @283/@322
     TecStream(oneof of lane): scalar opcode @181/@162, high payload @283/@322
```

| Slot | Base | End | Width | Opcode bit | Confidence |
|---|---:|---:|---:|---:|---|
| (reserved header) | 0 | 6 | 7 | — | MEDIUM |
| `Immediates` (low) | 7 | 86 | 80 | — | CONFIRMED |
| `VectorScalar` | 87 | 110 | 24 | — | CONFIRMED |
| `ScalarMisc` | 111 | 137 | 27 | 127 | CONFIRMED |
| `ScalarAlu1` | 138 | 164 | 27 | 154 | CONFIRMED |
| `ScalarAlu0` | 165 | 191 | 27 | 181 | CONFIRMED |
| `Immediates` (high) | 195 | 234 | 40 | — | CONFIRMED |
| `VectorResult` | 239 | 260 | 22 | 239 | CONFIRMED |
| `VectorExtended` | 261 | 461 | ~201 | 261 | HIGH |
| `VectorLoad` | 283 | 321 | 39 | 283 | CONFIRMED |
| `VectorStore` | 328 | 363 | 36 | 353 | CONFIRMED |
| `VectorAlu2` | 364 | 400 | 37 | 388 | CONFIRMED |
| `VectorAlu1` | 401 | 437 | 37 | 425 | CONFIRMED |
| `VectorAlu0` | 438 | 474 | 37 | 462 | CONFIRMED |
| (reserved / pad) | 475 | 511 | 37 | — | HIGH |
| `TecDma` (oneof of lane) | 87 | 327 | — | 181; 283/322 | CONFIRMED |
| `TecStream` (oneof of lane) | 99 | 327 | — | 181/162; 283/322 | CONFIRMED |

Decompile cross-check — the gfc TEC slot encoders write their `BitCopy` destination bits exactly at the bases above:

```text
SparseCoreImmediatesEncoder        0x1ecd1760   →  7, 27, 47, 67, 195, 215      (6 × 20-bit)
SparseCoreTecVectorResultEncoder   0x1ecbc9e0   →  239 (op), 245, 251, 253, 256, 259, 260
SparseCoreTecVectorLoadEncoder     0x1ecb9ee0   →  283 (op) .. 321
SparseCoreTecVectorStoreEncoder    0x1eccbe20   →  328 .. 353 (op) .. 363
SparseCoreTecVectorAlu2Encoder     0x1ec85ae0   →  364/370/376/382 (sel), 388 (op), 396/399/400 (pred)
SparseCoreTecVectorAlu0Encoder     0x1ec11100   →  438/444/450/456 (sel), 462 (op), 470/473/474 (pred)
```

> **QUIRK — the six immediate slots are split around the scalar stack but form one indexed array.** Four 20-bit slots sit below the scalar lanes (bits 7..86) and two above (bits 195/215), separated by the 81-bit scalar-slot stack. They are a single 6-entry array (`EmitImmediate(slot_index 0..5, value)`), packed in *descending* bundle-bit order (idx0→@67 … idx3→@7; idx4→@215, idx5→@195). The low four exist on SCS too; the high two are TEC-only. See [TEC §Immediate-Slot Indexing](tec-engine.md#immediate-slot-indexing).

> **QUIRK — the TEC Dma/Stream slots reach into the vector region.** Unlike SCS/TAC (descriptors confined to bits 87..142), the TEC Dma/Stream slot spills its high descriptor fields up into bits 283/322, overlapping the vector load/store region — this is how a single TEC bundle issues a tile-fetch DMA *and* the vector load that consumes it. The `IndirectVregStream` indirect-offset field lands at bundle bit 322. A reimplementer confining a TEC Stream descriptor to the low region will double-book the vector slots.

---

## Slot Ordering and the Internal Templates

### Slot ordering

The slot stack grows upward from a 7-bit reserved prefix. The ordering is fixed and shared by all three engines through bit 191:

```text
bit 0      reserved/header prefix (7 bits; no slot writes here)
bit 7      Immediates  — 4 × 20-bit, packed descending (idx3@7, idx2@27, idx1@47, idx0@67)
bit 87     VectorScalar — 24-bit scalar→vector bridge
bit 111    ScalarMisc   — 27-bit scalar slot (opcode @127)
bit 138    ScalarAlu1   — 27-bit scalar slot (opcode @154)
bit 165    ScalarAlu0   — 27-bit scalar slot (opcode @181)
─── above bit 191: engine-specific ───
SCS:  192..255 padding (bundle ends at 256 bits)
TAC:  192..511 padding (no vector path; bundle ends at 512 bits)
TEC:  195   Immediates (high) — 2 × 20-bit (idx5@195, idx4@215)
      239   VectorResult   (opcode @239)
      261   VectorExtended (opcode @261; spans to ~461, overlapping Load/Store/Alu)
      283   VectorLoad     (opcode @283)
      328   VectorStore    (opcode @353)
      364   VectorAlu2     (opcode @388)
      401   VectorAlu1     (opcode @425)
      438   VectorAlu0     (opcode @462)
      475..511 padding
```

> **NOTE — `VectorExtended` (261..461) overlaps the load/store/ALU slots by design.** The extended slot's bit range subsumes `VectorLoad`, `VectorStore`, and the three vector-ALU lanes. This is a oneof-style sharing: a bundle issuing a scan/sort/uniquify op (the embedding-reduce primitives) uses the extended region *instead of* the regular vector lanes in that range. The field-level binding inside 261..461 is recovered as a slot extent but not exhaustively named (HIGH); see [VectorExtended (VEX)](vectorextended-vex.md).

### The 27-bit scalar slot template (all three engines)

All scalar slots — `ScalarMisc`, `ScalarAlu1`, `ScalarAlu0` on SCS/TAC/TEC, and the scalar-lane part of Dma/Stream — share one internal template; only the slot base differs. Slot-relative offsets (absolute = `slot_base + offset`):

```text
27-bit scalar slot
  +0   w5   operand x0          scalar-register selector
  +5   w6   ScalarY             scalar-register-or-immediate selector
  +11  w5   operand x1          scalar-register selector
  +16  w6   OPCODE              6-bit primary opcode (≤ 64 ops)
  +22  w3   normal_predication  (overlaps rotate_predication below)
  +22  w4   rotate_predication  4-bit when is_rotate (16-entry ring)
  +25  w1   predication_inversion
  +26  w1   is_rotate_predication
```

The opcode bits fall out as `base + 16`: `ScalarMisc` `@127`, `ScalarAlu1` `@154`, `ScalarAlu0` `@181`. Full roster on [Scalar Opcode Enum](scalar-opcode-enum.md); the predication header on [M-Register Predicate Word](m-register-predicate.md).

### The 37-bit TEC vector-ALU slot template (GF)

The three TEC vector-ALU lanes share one template; only the slot base differs. Slot-relative offsets (gfc):

```text
37-bit vector-ALU slot (gfc)
  +0   w6   VREG operand selector 0
  +6   w6   VREG operand selector 1
  +12  w6   VREG operand selector 2
  +18  w6   VREG operand selector 3
  +24  w8   OPCODE              8-bit (≤ 256 — matches the 257-op gfc set)
  +32  w3   normal_predication  (overlaps rotate_predication below)
  +32  w4   rotate_predication  4-bit when is_rotate
  +35  w1   predication_inversion
  +36  w1   is_rotate_predication
```

The opcode bits fall out as `base + 24`: `VectorAlu2` `@388`, `VectorAlu1` `@425`, `VectorAlu0` `@462`. Full roster on [Vector Opcode Enum](vector-opcode-enum.md).

> **NOTE — the predication header is a 3-bit/4-bit overlap, not two distinct fields.** On both templates, `normal_predication` (3 bits) and `rotate_predication` (4 bits) share the same start bit; the 1-bit `is_rotate_predication` selects the interpretation. Allocate 4 bits with two meanings, not 3+4 distinct bits.

---

## Per-Generation Deltas

The low region (bits 7..191) and the scalar template are byte-identical across all three generations. The deltas concentrate in the engine roster (TAC presence) and the TEC vector region's width.

| Mechanism | VF (vfc) | GL (glc) | GF (gfc) |
|---|---|---|---|
| SCS bundle size / layout | 32 B / fixed | 32 B / fixed | 32 B / fixed |
| TAC bundle | 64 B (low region only) | 64 B (low region only) | **— (no TAC)** |
| TEC bundle size | 64 B | 64 B | 64 B |
| Scalar slot width / opcode width | 27 b / 6 | 27 b / 6 | 27 b / 6 |
| Scalar lane bases (Misc/Alu1/Alu0) | 111/138/165 | 111/138/165 | 111/138/165 |
| Stream/Dma opcode @ bundle bit | 181/154 | 181/154 | 181/154 |
| TEC immediate slots | 6 (4+2) | 6 (4+2) | 6 (4+2) |
| TEC VectorAlu slot width / opcode width | 36 b / 7 | 37 b / 8 | 37 b / 8 |
| TEC VectorAlu lane bases (v2/v1/v0) | (≈) 358/395/432 | 364/401/438 | 364/401/438 |
| TEC VectorAlu opcode count | 148 | 229 | 257 |
| TEC vector predication header | single (rotate + inversion) | dual | dual |

> **QUIRK — the TEC vector-ALU width crosses the 7-bit ceiling between Viperfish and Ghostlite.** Viperfish's 148-op vector-ALU set only just exceeds 128 (the top ops fold into reserved encodings), so its opcode field is 7 bits and its slot 36 bits. Ghostlite's 229-op set forces the field to 8 bits and the slot to 37 bits, shifting the GF vector lanes up ~6 bits (VF Alu0 base 432 → GF Alu0 base 438). Decompile cross-check: vfc `VectorAlu0` (`0x1e954ae0`) writes its VREG selectors at `432/438/444/450` and its opcode at `456` (= 432+24, 7-bit), with the single-channel predication header at `463/467` — the narrow 36-bit form. gfc `VectorAlu0` (`0x1ec11100`) writes selectors at `438/444/450/456`, opcode at `462` (8-bit), header at `470/473/474` — the wide 37-bit form.

> **Note — the TAC bundle layout is produced by the legacy codec path, not the outliner.** The region→sequencer outliner does *not* emit an `"access"` (TAC) function on any gen: its per-op callback (`0x136066e0`) stamps `sc.sequencer="execute"` unconditionally, and there is no `HasAccessSequencerTypeAttribute` / length-6 `"access"` predicate anywhere in the lowering chain. The TAC codec (`SparseCoreTacCodecBase`, `TpuSequencerType=4`, glc) survives only as a standalone encoder for the legacy `ProgramWrapper.tac` proto field; it is never reached from the MLIR tile-task pipeline. The TAC bundle layout above is the real glc/vfc TAC slot map, but it is produced by that legacy codec path. See the [TEC engine](tec-engine.md) for the full byte-level account.

---

## What Is Not Mapped

- **The 7-bit reserved prefix (bits 0..6) of every SC bundle** is unwritten by any slot encoder. Whether the codec sets a version/valid nibble in an epilogue is undecoded (SC bundles carry no `0x55` check trailer, unlike TensorCore bundles). MEDIUM/LOW. One analysis notes a gfc NOP-bundle last byte of `0x50` that *might* be a 4-bit framing field — unconfirmed, LOW.
- **The trailing padding** (SCS 192..255; TAC 192..511; TEC 475..511) is confirmed unwritten by any slot encoder; whether a codec epilogue touches it is unconfirmed (HIGH that it is unwritten by slots).
- **The bit-exact field labels inside `VectorScalar` (87..110) and `VectorExtended` (261..461)** — the slot bases and extents are recovered, but the per-op operand-to-selector binding inside these regions is not exhaustively named (HIGH).
- **The full VF TEC vector-region slot map** — only `VectorAlu0` was bit-confirmed on VF (`0x1e954ae0`, base 432, pinning the 36-bit / 7-bit-opcode delta); the VF `VectorLoad`/`Store`/`Extended`/`Result` bases are inferred as the −6-bit shift of GF (LOW for the exact VF bases of those four slots).
- **The decode-side struct→bundle inverse** (the `DecoderBase` path) was not re-extracted; the encode-side absolute bit map here is authoritative and the prior decode-struct shifts are consistent with it.

---

## Function Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `BitCopy` | `0x1fa0a900` | little-endian bit packer (`dst, dst_bitoff, src, src_bitoff, nbits`) | CONFIRMED |
| `SparseCoreScsCodecBase::Encode` (gfc) | `0x1391ef60` | SCS dispatcher; shared-Span call to each slot encoder | CONFIRMED |
| `SparseCoreScalarAlu0Encoder::Encode` (gfc) | `0x1eb693c0` | SCS lane 0; `BitCopy` bits 165/170/176/181/187/190/191 | CONFIRMED |
| `SparseCoreScsScalarMiscEncoder::Encode` (gfc) | `0x1eb914a0` | SCS misc/sync, opcode `@127` | CONFIRMED |
| `TacScalarAlu0Encoder::Encode` (glc) | `0x1ea17e40` | TAC lane 0; `BitCopy` bits 165/170/176/181/187/191 (= SCS) | CONFIRMED |
| `SparseCoreDmaEncoder::Encode` (glc) | `0x1ea09b40` | TAC/SCS shared Dma oneof-of-lane | CONFIRMED |
| `SparseCoreTecCodecBase::Encode` (vfc) | `0x139328a0` | TEC dispatcher; shared-Span call to all 14 slot encoders | CONFIRMED |
| `SparseCoreImmediatesEncoder::Encode` (gfc) | `0x1ecd1760` | 6 × 20-bit immediates `@7/27/47/67/195/215` | CONFIRMED |
| `SparseCoreTecVectorResultEncoder::Encode` (gfc) | `0x1ecbc9e0` | XRF-pop slot, opcode `@239` | CONFIRMED |
| `SparseCoreTecVectorLoadEncoder::Encode` (gfc) | `0x1ecb9ee0` | tile vector load, opcode `@283` .. 321 | CONFIRMED |
| `SparseCoreTecVectorStoreEncoder::Encode` (gfc) | `0x1eccbe20` | tile vector store, base 328, opcode `@353` | CONFIRMED |
| `SparseCoreTecVectorAlu2Encoder::Encode` (gfc) | `0x1ec85ae0` | vector lane 2, base 364, opcode `@388/8` | CONFIRMED |
| `SparseCoreTecVectorAlu0Encoder::Encode` (gfc) | `0x1ec11100` | vector lane 0, base 438, opcode `@462/8`; sel `@438/444/450/456` | CONFIRMED |
| `SparseCoreTecVectorAlu0Encoder::Encode` (vfc) | `0x1e954ae0` | narrow VF lane 0, base 432, opcode `@456/7` | CONFIRMED |
| `EncoderBase<…Scs…>::BundleSizeBytes` (gfc) | `0x1e835260` | codec-metadata `vtable[+0x30]` → 32 | CONFIRMED |
| `EncoderBase<…Tec…>::BundleSizeBytes` (gfc) | `0x1e8359e0` | codec-metadata `vtable[+0x30]` → 64 | CONFIRMED |
| `EncoderBase<…Tac…>::BundleSizeBytes` (glc) | `0x1e832100` | codec-metadata `vtable[+0x30]` → 64 | CONFIRMED |

---

## Cross-References

- [SCS (Scalar) Engine](scs-engine.md) — the 32-byte bundle, the scalar opcode roster, and the launch-by-attribute issue model the SCS column draws from.
- [TAC Engine](tac-engine.md) — the VF/GL-only 64-byte bundle that reuses the SCS low region; per-gen presence and the gfc-drops-TAC evidence.
- [TEC (Vector) Engine](tec-engine.md) — the 64-byte vector bundle, the 37-bit vector-ALU template, immediate-slot indexing, and the access/execute split.
- [Scalar Opcode Enum](scalar-opcode-enum.md) — the `SparseCoreScalarAlu` / `SparseCoreScalarMisc` roster carried in the 6-bit opcode field at `@127/154/181`.
- [Vector Opcode Enum](vector-opcode-enum.md) — the per-slot, per-gen TEC vector op roster carried in the 8-bit opcode field at `@239/283/353/388/425/462`.
- [Region → Sequencer Outliner](region-to-sequencer-outliner.md) — the `TileTaskOutliningPass` that stamps `sc.sequencer` and thereby selects which engine bundle (and slot map) encodes a function.
- [SparseCore Overview](overview.md) — the three engine classes, per-gen presence, and the `TpuSequencerType` codec-template enum {3,4,5}.
- [VectorExtended (VEX)](vectorextended-vex.md) — the field-level detail of the wide `VectorExtended` region (bits 261..461) this page leaves at slot-extent grade.
- [M-Register Predicate Word](m-register-predicate.md) — the predication header that overlays the top of every scalar and vector slot.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore engines — [back to index](../index.md)
