# TAC Engine

> *Every address, bit offset, and per-generation presence claim on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`) — from the demangled C++ symbol table, the embedded proto-descriptor strings, and the decompiled per-slot `Encoder::Encode` bodies. Other versions differ.*

## Abstract

The **TAC** — *Tile Access Core*, `TpuSequencerType = 4` — is the third member of SparseCore's SCS / TAC / TEC engine trio and its narrowest in capability despite its wide bundle. Where the [SCS](scs-engine.md) is the scalar control sequencer and the [TEC](tec-engine.md) is the wide vector engine, the TAC is a **pure address-compute + tile-fetch DMA issuer**: it takes a stream of embedding indices (produced by SCS, or by another SC's stream slot) and emits the gather DMAs that pull embedding rows out of HBM/SPMEM into the per-tile working SRAM (TILE_SPMEM) the TEC then computes over. It has its own sequencer thread — branches, halt, delay, integer/address arithmetic, compares, predicate generation, SMEM load/store for address staging, sync/atomic coordination, and a stream slot — but **no FPU vector path, no vector ALU, no vector load/store, no scan/sort/uniquify**. In the embedding pipeline it is the stage between "which rows do I need" and "the rows are now in tile SRAM."

The single most consequential fact about the TAC is that it is *not present on every generation*. SparseCore first appears on Viperfish (TPU v5p) as a three-engine SCS+TAC+TEC split; Ghostlite (TPU v6e) keeps all three; the **`6acc60406` generation (TPU v7x) drops the TAC entirely**, folding its tile-fetch-issue role into the SCS+TEC pair (SCS computes the gather addresses, TEC's own stream slot issues the DMA). This is a direct binary readout, not an inference: the `6acc60406` (`gxc.gfc`) namespace contains **zero** `SparseCoreTac*` symbols and zero `SparseCoreTac*` decompiled functions, against ~930–950 for each of Viperfish (`vxc.vfc`) and Ghostlite (`gxc.glc`). A reimplementer who emits a TAC program for a `6acc60406` target produces something the codec cannot encode.

The TAC bundle is a 64-byte (512-bit) VLIW word, the same physical width as the TEC bundle, but it **reuses the SCS bundle's low-region slot layout** and leaves the upper 320 bits unwritten — it spends its width on many concurrent scalar-style address ops, not on vector compute. This page documents the codec/template identity of the engine, the 64-byte bundle and its slot-base byte/bit offsets (recovered from the `BitCopy` destination immediates inside each per-slot `Encoder::Encode`), the op roster it executes (which is the SCS scalar/stream set, gated to the address subset), and the per-generation presence with the decompile counts that confirm `6acc60406` = 0.

For reimplementation, the contract is:

- **TAC is `TpuSequencerType = 4` and a distinct codec.** It is encoded by `SparseCoreTacCodecBase<…, TpuSequencerType=4>` (the `LN3tpu16TpuSequencerTypeE4E` template literal), present only under `vxc.vfc` and `gxc.glc`. The dispatcher hands every per-slot encoder the *same* `absl::Span<uchar>` buffer, so each slot writes at *absolute* bundle-bit offsets.
- **The 64-byte bundle uses only bits 3..191** — the SCS low-region (4×20-bit immediates, the vector-scalar bridge, the scalar-misc slot, two scalar-ALU lanes, and the Dma/Stream oneof-of-lane), plus two header control bits the Stream slot writes (@3/3 and @6/1). The upper 320 bits of the 512-bit bundle carry nothing; TAC has no vector slots to fill them.
- **The op set is the SCS scalar + stream set, not a separate enum.** TAC reuses the `SparseCoreScalarAlu` / `SparseCoreScalarMisc` / `SparseCoreStream` proto-enum spaces; legality is gated inside the `SparseCoreTacScalarAlu{0,1}Encoder` / `SparseCoreTacStreamEncoder` classes during emit. There is no `SparseCoreTacScalarAlu` proto enum to enumerate.
- **Per-gen presence is part of the contract.** Viperfish (v5p) and Ghostlite (v6e) ship TAC; `6acc60406` (v7x) does not. Encode this as a hard codec-family test, not a flag.

| | |
|---|---|
| **Engine** | TAC — Tile Access Core (address-compute + tile-fetch DMA issuer) |
| **Sequencer enum** | `tpu::TpuSequencerType = 4` (`TPU_SEQUENCER_TYPE_SPARSE_CORE_TILE_ACCESS_CORE_SEQUENCER`) |
| **Codec root** | `SparseCoreTacCodecBase<…, TpuSequencerType=4>` (in `asic_sw::deepsea::{vxc.vfc, gxc.glc}::isa`) |
| **Bundle size** | 64 bytes / 512 bits (`BundleSizeBytes` → codec-metadata vtable slot 6) — no check trailer |
| **Active region** | bits 3..191 (SCS low-region; the Stream slot also writes header bits @3/3 and @6/1); bits 192..511 unwritten |
| **Slot roster** | 4×20-bit immediates · vector-scalar bridge · ScalarMisc · TacScalarAlu1 · TacScalarAlu0 · Dma / TacStream (oneof of lane) |
| **Op set** | SCS `SparseCoreScalarAlu` / `SparseCoreScalarMisc` / `SparseCoreStream` enums, address subset |
| **Compute path** | none — no vector ALU, no vector load/store, no scan/sort/uniquify |
| **Present on** | Viperfish / v5p (`vxc.vfc`) · Ghostlite / v6e (`gxc.glc`) — **absent on `6acc60406` / v7x (`gxc.gfc`)** |
| **Confidence** | CONFIRMED (decompile / `BitCopy`-immediate anchored) unless a row or callout says otherwise |

---

## Codec and Sequencer Identity

### Purpose

The TAC is selected, like SCS and TEC, by a `tpu::TpuSequencerType` value carried as a non-type template parameter on its codec. Nothing at the op level names the engine; the engine assignment is the `sc.sequencer` string attribute (`"access"`) stamped on the enclosing outlined function (see [Region → Sequencer Outliner](region-to-sequencer-outliner.md)), and the codec template enum (`4`) read back downstream selects this codec.

### Entry Point

```text
TpuSequencerType = 4  (TILE_ACCESS_CORE_SEQUENCER)
  └─ SparseCoreTacCodecBase<SparseCoreTacBundle, TacScalarSubBundle,
        SparseCoreTacScalarAlu0{Decoder,Encoder},
        SparseCoreTacScalarAlu1{Decoder,Encoder},
        SparseCoreScalarMisc{Decoder,Encoder},
        SparseCoreTacStream{Decoder,Encoder},
        SparseCoreDma{Decoder,Encoder},
        SparseCoreVectorScalar{Encoder,Decoder},
        SparseCoreScalarImmediates{Decoder,Encoder},
        …, SparseCoreTacProgram, TpuSequencerType=4>
       ├─ Encoder<…>::EncodeBundle   ── alloc 64 B, memset 0, dispatch each slot encoder
       │    └─ each <Slot>Encoder::Encode(this, Message, absl::Span<uchar> buf)  ── SAME buf to all
       │         └─ BitCopy(dst=buf, esi=dst_bitoff, src, src_bitoff, r8d=nbits)  ── LE bit packer
       └─ Encoder<…>::BundleSizeBytes ── codec_metadata.vtable[+0x30]()  → 64
```

### Codec Template Parameter List

The mangled `SparseCoreTacCodecBase` template name *is* the sub-bundle inventory — the codec enumerates exactly the slot {Encoder,Decoder} pairs it must drive. Recovered from the demangled symbol (the `…ELN3tpu16TpuSequencerTypeE4EE…` suffix pins the integer literal `4` = the TAC sequencer-type enum value):

| Slot {Enc,Dec} class | Role | Op-enum it consumes |
|---|---|---|
| `TacScalarSubBundle` | wrapper over the two scalar lanes | (structural) |
| `SparseCoreTacScalarAlu0` | scalar/address ALU lane 0 (TAC variant) | `SparseCoreScalarAlu` |
| `SparseCoreTacScalarAlu1` | scalar/address ALU lane 1 (TAC variant) | `SparseCoreScalarAlu` |
| `SparseCoreScalarMisc` | scalar misc / sync / atomic slot (shared with SCS) | `SparseCoreScalarMisc` |
| `SparseCoreTacStream` | stream slot — the tile-fetch DMA issuer (TAC variant) | `SparseCoreStream` |
| `SparseCoreDma` | DMA-initiation slot (shared with SCS) | `SparseCoreDma` |
| `SparseCoreVectorScalar` | scalar→vector value bridge slot (shared) | `SparseCoreVectorScalar` |
| `SparseCoreScalarImmediates` | 4× immediate-value slots (shared) | `SparseCoreImmediates` |

> **NOTE — TAC declares no vector slots.** Compare this list against the [TEC](tec-engine.md) codec, which additionally carries `SparseCoreTecVectorAlu0/1/2`, `…VectorLoad`, `…VectorStore`, `…VectorExtended`, and `…VectorResult`. The TAC template has *none* of these. The engine is structurally a second scalar sequencer with a stream slot — its only "wide" property is its 64-byte bundle, and that width holds many concurrent scalar address ops, not vector compute.

### Bundle Size — `BundleSizeBytes`

`Encoder<…>::BundleSizeBytes` reads slot 6 (`vtable[+0x30]`) of the codec metadata. For Viperfish (`EncoderVfSparseCoreTac::BundleSizeBytes` @ `0x1d2eebe0`) it tail-calls `codec_metadata.vtable[+48]()`; the metadata body proves the value:

```c
function ViperfishCodecMetadata_BundleSizeBytesForHbm(this, seq):   // 0x1ee71380
    result = 32                                  // seq == 3 (SCS)
    if seq != 3:
        result = 64                              // TAC or TEC
        if (seq & 0xFFFFFFFE) != 4:              // seq not in {4,5} → FATAL
            LOG(FATAL) << "Unhandled component"  // codec_metadata_viperfish.cc:31
    return result
```

The `(seq & 0xFFFFFFFE) != 4` mask is the byte-exact source: it admits exactly `seq ∈ {4, 5}` (TAC, TEC) for the 64-byte branch and `seq == 3` (SCS) for the 32-byte branch; any other value is fatal. So **TAC (seq 4) = 64 bytes**, like the TEC, and unlike the 32-byte SCS. As with all SC bundles, there is no `0x55` check trailer (an all-zero bundle means "all slots inactive").

> **QUIRK — TAC is the only SC engine that is 64 bytes wide yet carries no compute.** A reimplementer sizing buffers from "is it a 64-byte bundle?" will over-provision a tile-fetch program by assuming vector capacity that does not exist. The width is for *scalar address-op parallelism* — multiple independent index/stride/offset computations and a DMA-issue in one cycle — not for the vector slots a 64-byte TEC bundle uses.

---

## TAC Bundle Slot-Base Map

### Purpose

Every TAC slot encoder receives the *same* output buffer span from the dispatcher, so each `BitCopy(dst, esi, …)` writes at an **absolute bundle-bit offset**. The destination bit base and width of every field is therefore the `mov esi,IMM` / `mov r8d,IMM` pair before each `call BitCopy`. The map below is recovered from the Ghostlite (`gxc.glc`) per-slot encoders; Viperfish (`vxc.vfc`) is byte-identical. Bundle = 64 bytes / 512 bits; the active region is the SCS low-region (the scalar/immediate stack lives in bits 7..191, and the Stream slot additionally writes two header control bits @3/3 and @6/1) and **bits 192..511 are unwritten**.

### Slot Map

| Slot | base | end | width | opcode bit | internal template |
|---|---|---|---|---|---|
| (header) | 0 | 6 | 7 | — | bundle prefix — only the Stream slot writes here (@3/3, @6/1); other slots leave it zero |
| `ScalarImmediates` | 7 | 86 | 80 | — | 4 × 20-bit (@7, @27, @47, @67) |
| `VectorScalar` | 87 | 110 | 24 | — | scalar→vector bridge |
| `ScalarMisc` | 111 | 137 | 27 | 127 | 27-bit scalar template |
| `TacScalarAlu1` (lane 1) | 138 | 164 | 27 | 154 | 27-bit scalar template |
| `TacScalarAlu0` (lane 0) | 165 | 191 | 27 | 181 | 27-bit scalar template |
| `Dma` (oneof of lane) | 87 | 191 | — | 181 / 154 | scalar opcode + payload @87..142 |
| `TacStream` (oneof of lane) | 3 | 191 | — | 181 / 154 | scalar opcode + payload @99..162, plus header bits @3/3 and @6/1 |
| (reserved / pad) | 192 | 511 | 320 | — | unwritten — no vector path |

The three scalar slots (Misc @111, Alu1 @138, Alu0 @165) are stacked 27 bits apart above the vector-scalar bridge, exactly as in the [SCS bundle](bundle-slot-base-map.md). This is *the same low-region layout*: `TacScalarAlu0` opcode @181, `TacScalarAlu1` @154, `ScalarMisc` @127 — identical bit positions to SCS. The DMA and TacStream slots are **not** separate physical regions: they are *oneof alternatives* of a scalar lane (the lane carries an ALU op OR a Misc op OR a Dma op OR a Stream op), so a Dma/Stream op writes its opcode into the scalar-lane opcode field (@181 for lane 0, @154 for lane 1) and borrows the lower bundle payload for its multi-word descriptor.

### 27-bit Scalar Template

The internal layout of each scalar slot, slot-relative (bit-exact from `TacScalarAlu0` @165; identical −27 for `TacScalarAlu1` @138 and `ScalarMisc` @111):

| Field | offset | width | meaning |
|---|---|---|---|
| operand x0 | +0 | 5 | scalar-register selector |
| ScalarY | +5 | 6 | scalar-register-or-immediate selector |
| operand x1 | +11 | 5 | scalar-register selector |
| **OPCODE** | +16 | 6 | ≤ 64 scalar ops |
| normal_predication | +22 | 3 | `SparsecoreNormalPredication` |
| rotate_predication | +22 | 4 | overlaps when `is_rotate` (16-entry ring) |
| predication_inversion | +25 | 1 | |
| is_rotate_predication | +26 | 1 | |

### Decompile Cross-Check — the `BitCopy` Immediates

The `TacScalarAlu0Encoder::Encode` (Ghostlite @ `0x1ea17e40`) takes a `SparseCoreScalarAlu` message and the bundle span; its preamble and per-form `BitCopy` calls pin every offset:

```c
function TacScalarAlu0Encoder_Encode(this, msg /*SparseCoreScalarAlu*/, buf):  // 0x1ea17e40
    BitCopy(buf, 187, &msg[+32], 0, 4)        // rotate_predication header @187/4  (= slot-base 165 +22)
    BitCopy(buf, 191, &msg[+24], 0, 1)        // is_rotate / inversion bit  @191/1 (= +26)
    switch (msg.opcode /* [+80] = the SCS ScalarAlu proto-enum TAG, not the wire opcode */):
      case 0: return 1                         // NoInstruction — leave slot empty
      case 6:  BitCopy(buf, 181, 0,    6)      // 6-bit HW OPCODE @181 := 0 (tag 6 → wire op 0)
               BitCopy(buf, 176, 0,   5) …     // operand x1 / sub-selector region @176/5 (= +11)
      case 7:  BitCopy(buf, 181, 0,    6)      // 6-bit HW OPCODE @181 := 0 (tag 7 = Delay)
               BitCopy(buf, 176, 3,    5)       // sub-selector := 3
               BitCopy(buf, 165, …,  11) …     // operand x0 / ScalarY  @165 (= slot base)
      case 8:  BitCopy(buf, 181, 0,6); BitCopy(buf,176,8,5); BitCopy(buf,165,1,5)  // tag 8 = SetTag
               BitCopy(buf, 170, …,   6) …     // ScalarY @170/6  (= +5)
      … case 0x0A..0x5B: tail-call EncodeSparseCoreTacScalarAlu0<Op>(buf)  // ~70 named proto tags
      default: return MakeErrorImpl("Cannot find matching encoder for instruction: …")
```

`TacScalarAlu1Encoder::Encode` (@ `0x1ea2a7a0`) is the same body shifted down one lane: OPCODE @154/6, operand-x1 region @149/5, slot base @138, predication header @160/4 + @164/1. `TacStreamEncoder::Encode` (@ `0x1ea338e0`) writes OPCODE @181/6 and its descriptor across bits 99..162 (off-tile/indirect operand @105/5, @110/1, @104/1, @99/5; sync/list controls @154/1, @155/1, @156/1, @157/3, @160/1, @161/1; stream-opcode mirror @162/6), with the same @187/4 + @191/1 predication header — and, uniquely among the TAC slots, two **header control bits at @3/3 and @6/1** (verified `BitCopy(a3, 3, …, 0, 3)` and `BitCopy(a3, 6, …, 0, 1)` in the decompile). These header bits are the only TAC writes below bit 87; every other slot starts at bit 87 or above.

> **CONFIRMED — no TAC slot encoder writes a bit at or above 192.** Sweeping the `BitCopy` destination immediates across `TacScalarAlu0` (@1ea17e40), `TacScalarAlu1` (@1ea2a7a0), `TacStream` (@1ea338e0), and the shared `SparseCoreDmaEncoder` (@1ea09b40), the highest destination bit written by any TAC slot is **191** (the is-rotate bit of scalar lane 0), and the lowest is **3** (the Stream slot's header control field). The upper 320 bits of the 512-bit TAC bundle are pure padding — direct byte-level evidence that TAC has no vector path and reuses only the SCS low-region plus the two stream-header bits.

> **GOTCHA — the slot opcode is shared, the slot meaning is per-engine.** `TacScalarAlu0`'s opcode @181 occupies the *same* bit field as SCS `ScalarAlu0`'s opcode @181, and the two consume the *same* `SparseCoreScalarAlu` proto enum. A reimplementer cannot tell SCS-vs-TAC from the bundle bits alone — the engine is fixed by the `sc.sequencer` attribute on the enclosing function (`"scs"` vs `"access"`), read back to pick the codec. The bundle is engine-agnostic; the codec selection is not.

---

## Op Roster

### Purpose

The TAC executes the address-and-control subset of the SCS scalar instruction set plus the stream slot — its whole job is to compute gather addresses and issue tile-fetch DMAs. It does not carry a separate opcode enum; it reuses SCS's `SparseCoreScalarAlu`, `SparseCoreScalarMisc`, and `SparseCoreStream` proto-enum spaces and gates legality at emit time in the `SparseCoreTac*Encoder` classes.

### Op Categories

The categories below are read from the `TacScalarAlu0Encoder::Encode` switch (`SparseCoreScalarAlu` proto-enum tags 0x06..0x5B at `msg[+80]`) and the shared `SparseCoreScalarMisc` / `SparseCoreStream` / `SparseCoreDma` encoders. These are the SCS scalar/stream ops the TAC variant accepts. The parenthesised hex values below are the **proto-enum tag** (the switch discriminant), not the 6-bit wire opcode emitted to bundle bits @181 — the per-op `Encode<Op>` helper re-encodes each tag into the 6-bit OPCODE field plus a sub-selector at @176, so several proto tags can share a wire opcode of `0` (e.g. tags 6/7/8 above):

| Category | In TAC? | Representative ops (from the encoder switch) |
|---|:---:|---|
| Control flow & branches | Y | `BranchAbsolute` (0x4B), `BranchRelative` (0x4C), `BranchSreg` (0x4D), `BranchAbsoluteClearIbuf` (0x5B), `CallAbsolute/Relative/Sreg` (0x4E–0x50), `Delay` (0x07), `Halt` |
| Integer / address ALU (32-bit) | Y | `IntegerAdd` (0x12), `IntegerSubtractYX` (0x14), `Multiply32BitIntegers` (0x52), `DivideWithRemainder*` (0x54–0x56), `BitwiseAnd/Or/Xor` (0x16–0x18), shifts (0x19–0x1C), `CountLeadingZeros` (0x22) |
| Compare / condition | Y | `CompareInteger{Eq,Ne}` (0x2D–0x2E), `CompareSignedInteger*` (0x2F–0x32), `CompareUnsignedInteger*` (0x33–0x36), `CarryOutFromIntegerUnsigned` (0x37) |
| Predicate | Y | `PredicateOr` (0x38) |
| Register-file reads | Y | `ReadRegisterSparseCoreId` (0x41), `ReadRegisterTileid` (0x42), `ReadRegisterTaskBitmap` (0x43), `ReadRegister{Gtc,Lcc}*`, `ReadRegisterTag/Tracemark`, `ReadRegisterDifDepthRegister` (0x45) |
| SMEM load/store (address staging) | Y | `ScalarLoadSmem*` / `ScalarStore*Smem*` / `ScalarLoadCircularBuffer` (shared SCS set) |
| Programmable resources | Y | `SetPrefetchDepth` (0x47), `SetIndirectFilterValue` (0x48) |
| Stack / pop | Y | `PopDrf` (0x3A), `PopRcmf` (0x3B) |
| Fence / sync | Y | `ScalarFence` (0x3C), `ScalarFenceScmf` (0x3D), `ScalarFenceStream{Spmem,Hbm}` (0x3E–0x3F), `ScalarFenceSelect` (0x40) |
| Misc / sync / atomic (ScalarMisc slot) | Y | the sync-flag + atomic-remote/tile family (`SparseCoreScalarMisc` enum, opcode @127) |
| **Stream — the TAC's primary work** | Y | `TacStream`: `IndirectStream`, `IndirectVregStream`, `LinearStream`, `StridedStream` → tile-fetch DMA |
| DMA initiation | Y | `SparseCoreDma`: `SimpleDma`, `SingleStridedDma`, `GeneralDma` (opcode @181) |
| Floating-point scalar | partial | `MaxOfTwoFloatingPointValues` (0x1D), `MinOfTwoFloatingPointValues` (0x1E), `CompareFloatingPoint*` (0x25–0x2A), `FloatingPointMultiply` (0x51), `Convert{Int32ToFloat32,Float32ToInt32}` (0x10–0x11), `IsInfOrNan` (0x2B) appear in the switch |
| Vector ALU / load / store / scan / sort | **N** | no slot exists — the codec template carries no vector encoders |

> **CORRECTION (TAC-FP) —** earlier SC-ISA prose summarised TAC as having "NO FPU." The decompiled `TacScalarAlu0Encoder::Encode` switch *does* include scalar floating-point opcode cases — float min/max (0x1D/0x1E), float compares (0x25–0x2A), `FloatingPointMultiply` (0x51), and int↔float converts (0x10/0x11) — encoded the same way as the integer ALU ops. The accurate statement is narrower: TAC has **no *vector* FPU and no vector compute path** (no `VectorAlu`/`VectorLoad`/`VectorStore`/`VectorExtended` slots). Whether the hardware actually executes the scalar-float forms or merely accepts them in the encoder enum was not separately traced (MEDIUM); the absence of every *vector* slot is byte-exact (CONFIRMED).

### Tile-Fetch Role

The TacStream slot is the operational core of the engine. The forward embedding pipeline runs: SCS schedules a tile-fetch program and computes the gather descriptors; **TAC issues the stream-gather DMA** `HBM[table_base + index_i · row_stride] → TILE_SPMEM`; the TEC vector-loads the resident tiles and runs the per-sample reduction. The TAC sits as the dedicated address-handler + DMA-issuer between the SCS sequencer and the TEC vector core — architecturally analogous to what the Pufferfish BarnaCore address-handler was to BarnaCore (see [BarnaCore Overview](../barnacore/overview.md)). The `STREAM_OPCODE_*` set and the gather/scatter descriptor format are owned by [Stream Gather/Scatter](stream-gather-scatter.md); the scalar/misc opcode roster TAC shares with SCS is owned by [Scalar Opcode Enum](scalar-opcode-enum.md).

---

## Per-Generation Presence

### Purpose

The single most important fact a reimplementer encodes about the TAC is *when it exists*. SparseCore is a v5+ feature; among SC-bearing gens, the TAC ships on Viperfish (v5p) and Ghostlite (v6e) and is removed on `6acc60406` (v7x). The discriminator is the codec class family — the presence or absence of a `SparseCoreTacCodecBase` under a per-codename family namespace is a direct binary readout.

### Roster Table

| Gen | Codename | Family ns | TAC present | Tile-fetch issuer | Notes |
|---|---|:---:|:---:|---|---|
| TPU v5p | Viperfish | `vxc.vfc` | **Y** | TAC stream | first gen with the three-engine split |
| TPU v6e | Ghostlite | `gxc.glc` | **Y** | TAC stream | full SCS+TAC+TEC |
| TPU v7x | `6acc60406` | `gxc.gfc` | **–** | TEC stream | **TAC removed** — folded into SCS+TEC |
| earlier | Jellyfish / Dragonfish / Pufferfish | `jxc` / `pxc.pfc` | – | (no SparseCore) | BarnaCore era |

### Decompile Cross-Check — `6acc60406` TAC = 0

The roster was confirmed directly against the decompiled function set and symbol table. Two independent counts, both with the `6acc60406` column zero:

| Metric | Viperfish (`vxc.vfc`) | Ghostlite (`gxc.glc`) | `6acc60406` (`gxc.gfc`) |
|---|---:|---:|---:|
| Demangled `<ns>::isa::SparseCoreTac*` symbols (`nm -C`) | 838 | 860 | **0** |
| Decompiled functions named `SparseCoreTac*` (filename) | 932 | 952 | **0** |

The `gfc` (`6acc60406`) namespace has **zero** `SparseCoreTac*` decompiled functions and zero `SparseCoreTac*` symbols, against ~840–950 for each of Viperfish and Ghostlite (the two count methods differ because a single decompiled function file can carry the symbol several times and the `nm` count is per-symbol). There is no `gxc.gfc` `SparseCoreTacCodecBase` class, no `SparseCoreTacScalarAlu*Encoder`, no `SparseCoreTacStreamEncoder`, and no `SparseCoreTacProgram` proto. The corresponding `SparseCoreTacGFSchedModelSchedClasses` LLVM table is also absent — only `SparseCoreTacVF*`/`SparseCoreTacGL*` sched models exist, whereas SCS and TEC carry `VF`/`GL`/`GF` triples. TAC is entirely gone from `6acc60406` silicon.

> **CONFIRMED — `6acc60406` collapses the 3-engine pipeline to 2.** On Viperfish/Ghostlite the path is SCS → TAC (tile-fetch DMA) → TEC (compute). On `6acc60406` TAC is gone: the TEC's own stream slot (`IndirectStream` / `IndirectVregStream` / `LinearStream` / `StridedStream`) absorbs the address-generation + DMA-issue duties, SCS computes the gather addresses (consumed by TEC via `tile_wait_scs_smem`), and the new SCS ops `BranchRelativeRotatingPreg` / `SetRotatingPredicateRegister` plus the TEC's `TileSpmemLoadCircularBufferPostUpdate` enable the inner-loop tile-fetch dispatch that previously needed TAC's separate sequencer. The result is a single SCS↔TEC boundary, lower silicon area, and lower-latency tile-fetch — at the cost of heavier TEC bundles that now issue both compute and DMA.

### Outliner — Where the "access" Function Comes From

The engine assignment is a string attribute, not a numeric enum at the op. `TileTaskOutliningPass` walks each `sc_tpu.tile_task` op, outlines the body into a `func.func`, and stamps `sc.sequencer`. On `6acc60406` it stamps only `"execute"` (TEC) for the tile body and `"scs"` for the enclosing control program — there is no `"access"`. On Viperfish/Ghostlite the same Target-parameterized pass additionally produces the `"access"` (TAC) function. The `"access"` value string and the read-back predicates that map it to `TpuSequencerType=4` live with the outliner; the exact per-op Access-vs-Execute split rule on the TAC-bearing gens was not exhaustively bit-traced (the `GetTransferKind` `kStream`/`kDma` result plus op data-dependencies feed it). See [Region → Sequencer Outliner](region-to-sequencer-outliner.md) and [getSequencerType](getsequencertype.md).

> **QUIRK — codec-template enum vs geometry-descriptor enum.** This page and the SC-ISA pages use the codec-template / proto-enum convention `{3 = SCS, 4 = TAC, 5 = TEC}` (the `TpuSequencerType` literal carried in the codec template — `…E4E` = TAC, verified in the mangled `SparseCoreTacCodecBase` symbol). The hardware geometry descriptor [`SparseCoreTarget`](architecture.md#the-sparsecoretarget-map-target0x948) — backed by `tpu::TpuCoreParts`, an `EnumMap<TpuSequencerType, Sequencer, 6>` (6-slot array, `TpuCoreParts` ctor @ `0x20b29e40`) — indexes its core-parts by the *same* `TpuSequencerType` value when sizing tiles and lanes; the [architecture page](architecture.md) reads the per-chip tile count as `SequencerCount(seq-type 5 = TEC)`, consistent with the codec convention. A reimplementer should use the `TpuSequencerType` enum value (`4` = TAC) uniformly for both the codec selection and the `TpuCoreParts` lookup. *(The exact internal slot numbering of the geometry `EnumMap` was not independently bit-traced on this page; follow [SparseCore Hardware Architecture](architecture.md) for the descriptor's own indexing.)*

---

## Related Components

| Name | Relationship |
|---|---|
| `SparseCoreTacCodecBase<…, TpuSequencerType=4>` | the TAC codec — drives every TAC slot encoder (VF/GL only) |
| `TacScalarAlu0Encoder::Encode` (`0x1ea17e40` glc) | scalar lane-0 slot encoder; opcode @181, the `BitCopy`-immediate source |
| `TacStreamEncoder::Encode` (`0x1ea338e0` glc) | the tile-fetch DMA-issuer slot encoder; opcode @181, payload @99..162 |
| `ViperfishCodecMetadata::BundleSizeBytesForHbm` (`0x1ee71380`) | proves TAC (seq 4) = 64 bytes |
| `TileTaskOutliningPass` | stamps `sc.sequencer = "access"` (TAC) on VF/GL outlined functions |
| `SparseCoreScalarMiscEncoder` (`0x1ea46f00` glc) / `SparseCoreDmaEncoder` (`0x1ea09b40` glc) | slots TAC shares byte-identically with SCS |

## Cross-References

- [SparseCore Overview](overview.md) — the navigational entry for Part IX; engine names, per-gen presence, the data path.
- [SparseCore Hardware Architecture](architecture.md) — engine→core layout, the four-tier memory model, and the geometry-descriptor enum offset.
- [SCS (Scalar) Engine](scs-engine.md) — the scalar control sequencer whose low-region bundle layout TAC reuses.
- [TEC (Vector) Engine](tec-engine.md) — the wide vector engine that consumes the tiles TAC fetches, and absorbs TAC's role on `6acc60406`.
- [Per-Engine Bundle Slot-Base Map](bundle-slot-base-map.md) — the full SCS/TAC/TEC slot-base partition this page draws the TAC column from.
- [Region → Sequencer Outliner](region-to-sequencer-outliner.md) — the pass that stamps `sc.sequencer = "access"` to assign a function to the TAC.
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC engine-selection accessor and the `TpuSequencerType` enum.
- [Scalar Opcode Enum](scalar-opcode-enum.md) — the `SparseCoreScalarAlu` / `SparseCoreScalarMisc` roster TAC gates to its address subset.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the `STREAM_OPCODE_*` set and the gather/scatter descriptor TacStream emits.
- [SC Backend Pipeline](sc-backend-pipeline.md) — the SC-MLO offload pass pipeline that outlines and lowers the per-engine bundle streams.
- [SC EmitX Dispatcher](sc-emitx-dispatcher.md) — the seq3/seq4/seq5 → EmitX jump tables that route to the per-engine codec.
- [BarnaCore Overview](../barnacore/overview.md) — the retired embedding accelerator whose address-handler the TAC mirrors.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore engines — [back to index](../index.md)
