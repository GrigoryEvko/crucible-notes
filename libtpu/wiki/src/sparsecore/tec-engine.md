# TEC (Vector) Engine

> *Every address, bit offset, opcode-width, and per-generation count on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`) — from the demangled C++ symbol table, the embedded proto-descriptor strings, and the decompiled per-slot `Encoder::Encode` bodies (the `BitCopy` destination-bit immediates). Other versions differ.*

## Abstract

The **TEC** — *Tile Execute Core*, codec-template `TpuSequencerType = 5` — is the wide vector datapath of the SparseCore and the engine every embedding lookup actually computes on. Where the [SCS](scs-engine.md) is the scalar control sequencer that runs the program counter and issues launches, and the [TAC](tac-engine.md) is the (VF/GL-only) tile-fetch DMA issuer, the TEC is the compute machine: it vector-loads embedding tiles out of per-tile SRAM (`TILE_SPMEM`), runs the per-sample reductions, scans, sorts, uniquifies, packs/unpacks the small-float formats, and scatter-adds gradients. The closest familiar analog is a VLIW SIMD core — three concurrent vector-ALU lanes plus a vector load, a vector store, a transcendental ("extended") unit, and a result-pop slot, all issued in one bundle — bolted onto a small scalar front end that re-uses the SCS scalar template verbatim. A TEC bundle is the body of the loop the SCS control program launches via `LaunchTileTaskOp`; the SCS program is the loop.

The TEC bundle is a **64-byte (512-bit) VLIW word**, the same physical width as the TAC bundle, but unlike TAC it fills its width with real compute. The low region (bits 7..191) is the *same* SCS layout — four 20-bit immediates, the vector-scalar bridge, the scalar-misc slot, and two scalar-ALU lanes — and above bit 191 sits a vector compute region (bits 195..474 on 6acc60406) that TAC and SCS leave empty: two more immediate slots, then `VectorResult`, `VectorExtended`, `VectorLoad`, `VectorStore`, and the three stacked vector-ALU lanes `Alu2/Alu1/Alu0`. Each slot encoder is handed the *same* output-buffer `Span` by the codec dispatcher, so every `BitCopy(dst, dst_bitoff, …)` writes at an *absolute* bundle-bit offset; the slot map below is recovered from those immediates.

The TEC is the engine that *grows* gen-over-gen, and it grows because it absorbs work the other engines shed. Its vector-ALU opcode set goes from 148 (Viperfish) to 229 (Ghostlite) to 257 (6acc60406); the opcode field widens 7→8 bits and the slot 36→37 bits to hold it; and on 6acc60406 it takes over the tile-fetch issuance that TAC owned, because the SC-MLO compiler emits no `"access"` (TAC) function on *any* generation — every tile_task body is outlined into a TEC `"execute"` function whose Stream slot issues the gathers. This page documents the codec identity, the 64-byte bundle and its slot-base byte/bit offsets, the vector op roster pointer, the VF-vs-Execute (access-region-vs-execute-region) split rule and immediate-slot indexing, and the decompile counts that evidence the growth.

For reimplementation, the contract is:

- **TEC is codec-template `TpuSequencerType = 5` and the only SC engine with a vector path.** It is encoded by `SparseCoreTecCodecBase<…, TpuSequencerType=5>` (the `LN3tpu16TpuSequencerTypeE5E` template literal), present on all three SC gens. The codec template enumerates its slot {Encoder,Decoder} pairs; the vector slots (`TecVectorAlu0/1/2`, `…VectorLoad`, `…VectorStore`, `…VectorExtended`, `…VectorResult`) are what distinguish it from SCS/TAC.
- **The 64-byte bundle reuses the SCS low region (bits 7..191) and adds a vector region above it.** Six 20-bit immediate slots (4 low @7/27/47/67 + 2 high @195/215), the three 27-bit scalar slots (Misc/Alu1/Alu0 @111/138/165, byte-identical to SCS), then the vector compute region (`VectorResult` @239, `VectorExtended` @261, `VectorLoad` @283, `VectorStore` @328, `VectorAlu2/1/0` @364/401/438 on 6acc60406). No `0x55` check trailer; an all-zero bundle is the NOP.
- **The 37-bit vector-ALU template (6acc60406): four 6-bit VREG selectors, an 8-bit OPCODE @+24, a dual-channel predication header.** Viperfish is the narrow 36-bit form (7-bit OPCODE) matching its 148-op set. The three lanes stack one slot-width apart; the per-gen width delta accumulates upward (VF Alu0 @432, GF Alu0 @438).
- **Engine assignment is a string attribute, and there is no MLIR-level Access/Execute split.** The outliner stamps a tile_task body `sc.sequencer="execute"` (the TEC) and the enclosing control program `"scs"`; it never stamps `"access"`. The TAC ("access") tile-fetch role is carried by the TEC's own Stream slot on *all* gens, which is why TEC grows and why a reimplementer never produces an `"access"` function.

| | |
|---|---|
| **Engine** | TEC — Tile Execute Core (the SparseCore vector datapath) |
| **Sequencer enum (codec-template)** | `TpuSequencerType = 5` (`TPU_SEQUENCER_TYPE_SPARSE_CORE_TILE_EXECUTE_CORE_SEQUENCER`) |
| **Codec root** | `SparseCoreTecCodecBase<…, TpuSequencerType=5>` (`vxc.vfc` / `gxc.glc` / `gxc.gfc`); `…E5E` template literal |
| **Bundle size** | **64 bytes / 512 bits**; no `0x55` check trailer (all-zero = NOP) |
| **Active region** | bits 7..474 (low SCS region 7..191 + vector region 195..474, GF) |
| **Vector slots** | `VectorResult` · `VectorExtended` · `VectorLoad` · `VectorStore` · `VectorAlu0/1/2` |
| **Vector-ALU opcode** | 8-bit (GF/GL) · 7-bit (VF); slot 37 bits (GF/GL) · 36 bits (VF) |
| **Vector-ALU op count** | VF **148** · GL **229** · GF **257** (grows gen-over-gen) |
| **`SparseCoreTec*` decompiled funcs** | vfc **5244** · glc **7803** · gfc **8636** (grows; vs TAC 932/952/**0**) |
| **Present on** | Viperfish · Ghostlite · 6acc60406 (and absorbs TAC's role on 6acc60406) |
| **Confidence** | CONFIRMED (decompile / `BitCopy`-immediate anchored) unless a row or callout says otherwise |

> **QUIRK — two enum numbering schemes; this page uses the C++/codec numbering (TEC = 5).** The C++ `tpu::TpuSequencerType` enum numbers `{3 = SCS, 4 = TAC, 5 = TEC}`, carried as the non-type template literal on the codec — verified byte-exact in the gfc `SparseCoreTecCodecBase` symbol's `LN3tpu16TpuSequencerTypeE5E` suffix (16 such gfc-namespace `SparseCoreTec*` symbols carry the literal; 32 across all three gen namespaces). This same C++ numbering is what `TpuSequencerTypeToString` renders and what the `SparseCoreTarget` geometry descriptor uses to index `TpuCoreParts` (tile-execute geometry read at C++ sequencer-type 5 = TEC). The *off-by-one* peer is the **protobuf** enum `TpuSequencerTypeProto`, which reserves `INVALID=0` and so numbers `{… SCS = 4, TAC = 5, TEC = 6}`; `TpuSequencerTypeFromProto` subtract-one-converts it to the C++ enum before any in-memory use. Use **5** for the TEC codec, engine name, *and* the `TpuCoreParts` index (matching [overview](overview.md), [scs-engine](scs-engine.md), [tac-engine](tac-engine.md)); only a raw `TpuSequencerTypeProto` field carries TEC = 6. Do not mix the two.

---

## Codec and Sequencer Identity

### Purpose

The TEC is selected, like SCS and TAC, by a `tpu::TpuSequencerType` value carried as a non-type template parameter on its codec. Nothing at the op level names the engine; engine assignment is the `sc.sequencer` string attribute (`"execute"`) stamped on the enclosing outlined function (see [The Access/Execute Split](#the-vf-accessexecute-split) below), and the codec template enum (`5`) read back downstream selects this codec.

### Entry Point

```text
TpuSequencerType = 5  (TILE_EXECUTE_CORE_SEQUENCER)
  └─ SparseCoreTecCodecBase<SparseCoreTecBundle, TecScalarSubBundle,
        SparseCoreTecScalarAlu0{Decoder,Encoder},     ── scalar lane 0 (TEC variant)
        SparseCoreTecScalarAlu1{Decoder,Encoder},     ── scalar lane 1 (TEC variant)
        SparseCoreScalarMisc{Decoder,Encoder},        ── misc/sync/atomic (shared)
        SparseCoreImmediates{Decoder,Encoder},        ── 6 × 20-bit immediates
        SparseCoreVectorScalar{Encoder,Decoder},      ── scalar→vector bridge
        SparseCoreTecDma{Decoder,Encoder},            ── DMA slot (TEC variant)
        SparseCoreTecStream{Decoder,Encoder},         ── stream slot (TEC variant)
        SparseCoreTecVectorAlu0{Decoder,Encoder},     ── vector lane 0  ┐
        SparseCoreTecVectorAlu1{Decoder,Encoder},     ── vector lane 1  ├ the vector
        SparseCoreTecVectorAlu2{Decoder,Encoder},     ── vector lane 2  │ path TAC/SCS
        SparseCoreTecVectorLoad{Decoder,Encoder},     ── tile vector load   lack
        SparseCoreTecVectorStore{Decoder,Encoder},    ── tile vector store
        SparseCoreTecVectorExtended{Decoder,Encoder}, ── scans/sort/uniquify
        SparseCoreTecVectorResult{Decoder,Encoder},   ── XRF pop / write
        …, SparseCoreTecProgram, TpuSequencerType=5>
       ├─ Encoder<…>::EncodeBundle   ── alloc 64 B, memset 0, dispatch each slot encoder
       │    └─ each <Slot>Encoder::Encode(this, Message, absl::Span<uchar> buf)  ── SAME buf to all
       │         └─ BitCopy(dst=buf, esi=dst_bitoff, src, src_bitoff, r8d=nbits)  ── LE bit packer (0x1fa0a900)
       └─ Encoder<…>::BundleSizeBytes ── codec_metadata.vtable[+0x30]()  → 64  (gfc 0x1e8359e0)
```

### Codec Template Parameter List

The mangled `SparseCoreTecCodecBase` template name *is* the slot inventory — the codec enumerates exactly the slot {Encoder,Decoder} pairs it must drive. The vector slots below are the structural difference from [TAC](tac-engine.md), whose codec template has *none* of them. Each slot consumes a proto-enum opcode space.

| Slot {Enc,Dec} class | Role | Op-enum it consumes |
|---|---|---|
| `TecScalarSubBundle` | wrapper over the two scalar lanes | (structural) |
| `SparseCoreTecScalarAlu0` / `…Alu1` | scalar/address ALU lanes (TEC variant) | `SparseCoreScalarAlu` |
| `SparseCoreScalarMisc` | scalar misc / sync / atomic slot (shared with SCS) | `SparseCoreScalarMisc` |
| `SparseCoreImmediates` | 6 × 20-bit immediate slots (note: not `…ScalarImmediates`) | `SparseCoreImmediates` |
| `SparseCoreVectorScalar` | scalar→vector value bridge (shared) | `SparseCoreVectorScalar` |
| `SparseCoreTecDma` / `SparseCoreTecStream` | DMA / stream slots (TEC variant; oneof of a scalar lane) | `SparseCoreDma` / `SparseCoreStream` |
| `SparseCoreTecVectorAlu0` / `…Alu1` / `…Alu2` | **three** concurrent vector ALU lanes (compute) | `SparseCoreTecVectorAlu` |
| `SparseCoreTecVectorLoad` | tile vector load (`TileSpmemLoad*`) | `SparseCoreTecVectorLoad` |
| `SparseCoreTecVectorStore` | tile vector store + scatter-add (`TileSpmemStore*[Add]`) | `SparseCoreTecVectorStore` |
| `SparseCoreTecVectorExtended` | scans / segmented scans / sort / uniquify (transcendental-like) | `SparseCoreTecVectorExtended` |
| `SparseCoreTecVectorResult` | pop the XRF (extended-result FIFO) into the VRF | `SparseCoreTecVectorResult` |

> **NOTE — the immediate slot is `SparseCoreImmediates`, not SCS's `SparseCoreScalarImmediates`, and there are six of them.** SCS carries four 20-bit immediate slots; the TEC carries six (four low @7/27/47/67 + two high @195/215). The extra two exist because vector ops reference literals through the `VectorY` 6-bit selector and need more slots than the scalar program — see [Immediate-Slot Indexing](#immediate-slot-indexing).

### Bundle Size — `BundleSizeBytes`

`EncoderBase<… gfc Tec …>::BundleSizeBytes` (gfc `0x1e8359e0`) does not return a literal; it dispatches the codec-metadata vtable — `return (*(codec_metadata->vtable[+0x30]))(codec_metadata)`, vtable slot 6 — which returns **64** for sequencer type 5. The Viperfish metadata body proves the mask that admits it:

```c
function ViperfishCodecMetadata_BundleSizeBytesForHbm(this, seq):   // 0x1ee71380
    result = 32                                  // seq == 3 (SCS)
    if seq != 3:
        result = 64                              // TAC or TEC
        if (seq & 0xFFFFFFFE) != 4:              // seq not in {4,5} → FATAL
            LOG(FATAL) << "Unhandled component"  // codec_metadata_viperfish.cc:31
    return result
```

The `(seq & 0xFFFFFFFE) != 4` mask admits exactly `seq ∈ {4, 5}` (TAC, TEC) for the 64-byte branch. So **TEC (seq 5) = 64 bytes**, like TAC and unlike the 32-byte SCS. As with all SC bundles there is no `0x55` check trailer; the `EncodeBundle` wrapper `memset`s the buffer to zero before dispatch, and an all-zero bundle is the canonical "all slots inactive" NOP. The *packed* size (the highest bit any slot writes, before the DMA pad to 64) is `GetBytesPerBundle` = `0x3c` = 60 on 6acc60406 (gfc `0x13923a80`) and `0x3b` = 59 on Viperfish (vfc `0x13933660`) — Viperfish packs one byte smaller because its vector lanes are 36 bits, not 37.

---

## The TEC Bundle (64 bytes)

### Layout

The bundle is 512 bits. The low region (bits 7..191) is the *same* slot stack as the [SCS bundle](bundle-slot-base-map.md) — four immediates, the vector-scalar bridge, and three 27-bit scalar slots — and is byte-identical across VF/GL/GF. Above bit 191 sits the vector compute region that SCS and TAC leave empty. Bit offsets are absolute (the dispatcher passes every slot encoder the same buffer `Span`); the map below is 6acc60406 (gfc), recovered from the `BitCopy` destination immediates inside each `SparseCoreTecVector*Encoder::Encode`.

```text
TEC bundle — 64 bytes / 512 bits (gfc / 6acc60406)
bit: 0    7              87      111  138  165   195      239    261        283     328    364   401   438       475      511
     ┌────┬──────────────┬───────┬────┬────┬────┬────────┬──────┬──────────┬───────┬──────┬─────┬─────┬─────────┬─────────┐
     │rsvd│ Immediates   │Vector │Sc  │Sc  │Sc  │Immed.  │Vector│ Vector   │Vector │Vector│Vec  │Vec  │ Vector  │ rsvd /  │
     │7b  │ (low) 4×20b  │Scalar │Misc│Alu1│Alu0│(high)  │Result│ Extended │Load   │Store │Alu2 │Alu1 │ Alu0    │ pad     │
     │hdr │ @7/27/47/67  │bridge │op  │op  │op  │2×20b   │op    │ op@261…  │op     │op    │op   │op   │ op@462  │ 37 bits │
     │    │              │24b    │@127│@154│@181│@195/215│@239  │ (scan/   │@283   │@353  │@388 │@425 │ (8-bit) │         │
     └────┴──────────────┴───────┴────┴────┴────┴────────┴──────┴ sort)────┴───────┴──────┴─────┴─────┴─────────┴─────────┘
     ◄──────────── SCS low region (bits 7..191, identical to SCS) ─────────►◄────────── TEC vector region (bits 195..474) ──────────►
     TecDma   (oneof of a scalar lane): scalar opcode @181, high payload @283/@322
     TecStream(oneof of a scalar lane): scalar opcode @181/@162, high payload @283/@322
```

| Slot | Base | End | Width | Opcode bit | Internal template | Confidence |
|---|---:|---:|---:|---:|---|---|
| (reserved header) | 0 | 6 | 7 | — | bundle prefix; meaning not decoded | MEDIUM |
| `Immediates` (low) | 7 | 86 | 80 | — | 4 × 20-bit (`@7,@27,@47,@67`) | CONFIRMED |
| `VectorScalar` | 87 | 110 | 24 | — | scalar→vector bridge | CONFIRMED |
| `ScalarMisc` | 111 | 137 | 27 | 127 | 27-bit scalar template | CONFIRMED |
| `ScalarAlu1` (lane 1) | 138 | 164 | 27 | 154 | 27-bit scalar template | CONFIRMED |
| `ScalarAlu0` (lane 0) | 165 | 191 | 27 | 181 | 27-bit scalar template | CONFIRMED |
| `Immediates` (high) | 195 | 234 | 40 | — | 2 × 20-bit (`@195,@215`) | CONFIRMED |
| `VectorResult` | 239 | 260 | 22 | 239 | XRF-pop (`EupResult`/`PopXrf…`) | CONFIRMED |
| `VectorExtended` | 261 | 461 | ~201 | 261 (EUP) | scan/sort/uniquify region; reuses VREG operands | HIGH |
| `VectorLoad` | 283 | 321 | 39 | 283 | `TileSpmemLoad*` | CONFIRMED |
| `VectorStore` | 328 | 363 | 36 | 353 | `TileSpmemStore*[Add]` | CONFIRMED |
| `VectorAlu2` (lane 2) | 364 | 400 | 37 | 388 | 37-bit vector template | CONFIRMED |
| `VectorAlu1` (lane 1) | 401 | 437 | 37 | 425 | 37-bit vector template | CONFIRMED |
| `VectorAlu0` (lane 0) | 438 | 474 | 37 | 462 | 37-bit vector template | CONFIRMED |
| (reserved / pad) | 475 | 511 | 37 | — | unwritten by any slot encoder | HIGH |
| `TecDma` (oneof of lane) | 87 | 327 | — | 181; 283/322 | scalar opcode + high payload | CONFIRMED |
| `TecStream` (oneof of lane) | 99 | 327 | — | 181/162; 283/322 | scalar opcode + high payload | CONFIRMED |

> **QUIRK — the immediate slots are split around the scalar lanes.** Four 20-bit slots sit *below* the scalar stack (bits 7..86) and two more sit *above* it (bits 195/215), separated by the 81-bit scalar-slot stack and the vector-scalar bridge. They are nonetheless a single 6-entry indexed array (slot index 0..5), packed in *descending* bundle-bit order (idx0→bit67 … idx3→bit7; idx4→215, idx5→195). A reimplementer must not treat the high pair as a separate resource — `EmitImmediate(slot_index, value)` indexes all six (see [Immediate-Slot Indexing](#immediate-slot-indexing)).

> **QUIRK — the Dma/Stream slots reach into the vector region; they are not in the low region only.** Unlike the SCS/TAC Dma/Stream (whose descriptors stay in bits 87..142), the *TEC* Dma/Stream slot is a oneof of a scalar lane (opcode @181 lane 0, @162 stream mirror) but spills its high descriptor fields up into bits 283/322 — overlapping the vector-load/store region. This is how a single TEC bundle issues a tile-fetch DMA *and* the vector load that consumes it: the Stream slot's indirect operands land at bundle bit 322 (the `IndirectVregStream` indirect-offset field). A reimplementer who confines a TEC Stream descriptor to the low region will double-book the vector slots.

### Encoder Dispatch and the Shared Buffer

The codec dispatcher (`SparseCoreTecCodecBase<…>::Encode`) is a thin loop that invokes each member slot encoder in turn and hands every one of them the *same* output buffer. The Viperfish dispatcher (`0x139328a0`, the vfc codec `Encode`) holds `rdx=buf.ptr` (in `%r14`) and `rcx=buf.len` (in `%rbx`) constant across every per-slot call; only the `rdi` member-encoder pointer differs. (The pseudocode below lists the gfc dispatch order and gfc encoder addresses — the gfc codec lists 14 encoder slots; the vfc codec carries a `VectorImmediates` slot in place of gfc's `Immediates`, so its member order differs.) Each encoder then packs its fields with the generic LE packer `BitCopy(dst, dst_bitoff, src, src_bitoff, nbits)` (`0x1fa0a900`); the `dst_bitoff` immediate is the absolute bundle bit.

```c
function SparseCoreTecCodecBase_Encode(bundle, buf):     // gfc dispatch order
    ImmediatesEncoder.Encode(   bundle, msg, buf.ptr, buf.len)   // @7/27/47/67/195/215 (0x1ecd1760)
    VectorScalarEncoder.Encode( bundle, msg, buf.ptr, buf.len)   // @87..110            (0x1ecd1e00)
    ScalarMiscEncoder.Encode(   bundle, msg, buf.ptr, buf.len)   // @111..137 op@127    (0x1ebad840)
    TecScalarAlu1Encoder.Encode(bundle, msg, buf.ptr, buf.len)   // @138..164 op@154    (0x1ebd8040)
    TecScalarAlu0Encoder.Encode(bundle, msg, buf.ptr, buf.len)   // @165..191 op@181    (0x1ebc54a0)
    VectorResultEncoder.Encode( bundle, msg, buf.ptr, buf.len)   // @239..260 op@239    (0x1ecbc9e0)
    VectorExtendedEncoder.Encode(bundle,msg, buf.ptr, buf.len)   // @261..461 op@261    (0x1ecab8a0)
    VectorLoadEncoder.Encode(   bundle, msg, buf.ptr, buf.len)   // @283..321 op@283    (0x1ecb9ee0)
    VectorStoreEncoder.Encode(  bundle, msg, buf.ptr, buf.len)   // @328..363 op@353    (0x1eccbe20)
    VectorAlu2Encoder.Encode(   bundle, msg, buf.ptr, buf.len)   // @364..400 op@388    (0x1ec85ae0)
    VectorAlu1Encoder.Encode(   bundle, msg, buf.ptr, buf.len)   // @401..437 op@425    (0x1ec51900)
    VectorAlu0Encoder.Encode(   bundle, msg, buf.ptr, buf.len)   // @438..474 op@462    (0x1ec11100)
    TecStreamEncoder.Encode(    bundle, msg, buf.ptr, buf.len)   // oneof: op@181/162, high@283/322 (0x1ebe33e0)
    TecDmaEncoder.Encode(       bundle, msg, buf.ptr, buf.len)   // oneof: op@181, high@283/322      (0x1ebb6960)
    // buf is zero-initialized by EncodeBundle; no check-byte epilogue written.
```

---

## The 37-Bit Vector-ALU Slot Template

### Layout

All three vector-ALU lanes share one internal template; only the slot base differs. Slot-relative offsets, confirmed byte-exact against the gfc `VectorAlu0` encoder (`0x1ec11100`), whose `BitCopy` calls write four 6-bit VREG selectors at `@438/444/450/456`, an 8-bit opcode at `@462`, and the predication header at `@470/3`, `@473/1`, `@470/4`, `@474/1`:

```text
37-bit vector-ALU slot (gfc; slot-relative; absolute = slot_base + offset)
  +0   w6   VREG operand selector 0
  +6   w6   VREG operand selector 1
  +12  w6   VREG operand selector 2
  +18  w6   VREG operand selector 3
  +24  w8   OPCODE              8-bit (≤ 256 — matches the 257-op gfc set)
  +32  w3   normal_predication  SparsecoreNormalPredication
  +32  w4   rotate_predication  overlaps normal when is_rotate (16-entry ring)
  +35  w1   predication_inversion
  +36  w1   is_rotate_predication
```

So the absolute opcode bits fall out as `base + 24`: `VectorAlu2` op `@388` (= 364+24), `VectorAlu1` `@425` (= 401+24), `VectorAlu0` `@462` (= 438+24). The three lanes stack 37 bits apart directly above the vector load/store region.

| Lane | Base (GF) | VREG sel | OPCODE `@+24/8` | pred header `@+32` | Confidence |
|---|---:|---:|---:|---|---|
| `VectorAlu2` | 364 | 364/370/376/382 | **388** | 396/399/400 | CONFIRMED |
| `VectorAlu1` | 401 | 401/407/413/419 | **425** | 433/436/437 | CONFIRMED |
| `VectorAlu0` | 438 | 438/444/450/456 | **462** | 470/473/474 | CONFIRMED |

The vector-ALU template is the scalar template's structural cousin: where the 27-bit [scalar slot](scs-engine.md#the-27-bit-scalar-slot-template) carries two/one 5-bit register operands and a 6-bit opcode, the vector slot carries *four* 6-bit VREG selectors and a wider opcode, with the same overlapped normal/rotate predication header at the top. The `VectorY` immediate-selector lives in the vector-scalar bridge / immediate-indexing path rather than inline (see [Immediate-Slot Indexing](#immediate-slot-indexing)).

> **NOTE — the predication header is a 3-bit/4-bit overlap, not two fields.** `normal_predication` (3 bits) and `rotate_predication` (4 bits) share the same starting bit `@+32`; the 1-bit `is_rotate_predication` `@+36` selects the interpretation. A reimplementer must allocate 4 bits with two meanings, not 3+4 distinct bits. Viperfish uses a *single-channel* form: only the 4-bit `rotate_predication` `@+31` plus a 1-bit inversion/is-rotate `@+35` — no separate 3-bit normal field — which is the +1-bit difference that makes the VF slot 36 bits.

---

## The Vector Op Roster

### Pointer to the Roster

The TEC's seven vector slots draw from seven distinct proto-enum spaces, each emitted as one C++ op-form type per opcode per gen (`SparseCore<Slot><OpName>Opcode`), whose `Matches()` predicate carries the opcode signature exactly as the scalar ops do (see [SCS roster](scs-engine.md#two-level-opcode-space) for the predicate shapes). The full per-slot, per-gen roster is owned by [Vector Opcode Enum](vector-opcode-enum.md); the categories and per-gen counts below frame it. The `VectorExtended` slot — scans, segmented scans, sort, uniquify — is the embedding-reduce heart of the engine and is detailed in [VectorExtended (VEX)](vectorextended-vex.md).

| Slot | VF | GL | GF | Representative ops (GF) |
|---|---:|---:|---:|---|
| `VectorAlu` (×3 lanes) | 148 | 229 | **257** | `VectorAdd{Bf16,F32,S16,S32}`, `VectorMultiply*`, `Tanh{Bf16,F32}`, `Reciprocal*`, FP8/FP4 `PackCompressed*`/`UnpackCompressed*`, `VmskAnd/Or/Xor`, lane permute/scan helpers |
| `VectorLoad` | 5 | 5 | 5 | `TileSpmemLoad`, `…LoadCircularBuffer[PostUpdate]`, `…LoadIndexed[CircularBuffer]` |
| `VectorStore` | 15 | 33 | 33 | `TileSpmemStore*` + per-dtype scatter-add `…StoreAdd{Bf16,F32,S16,S32}` variants |
| `VectorExtended` | 28 | 52 | 53 | `AddScan*`, `Segmented*Scan*`, `Max/MinScan`, `Max/MinIndexScan`, `Sort{Float,Integer}{Asc,Desc}`, `Uniquify*`, `DuplicateCount*` |
| `VectorResult` | 14 | 14 | 14 | `EupResult`, `VresMove`, `PopXrfWriteAll`, `PopXrfWritePartial0..4` |
| `TecStream` | 4 | 4 | 4 | `IndirectStream`, `IndirectVregStream`, `LinearStream`, `StridedStream` |
| `TecDma` | 3 | 3 | 3 | `SimpleDma`, `SingleStridedDma`, `GeneralDma` |

> **NOTE — the `VectorExtended` slot fires once per bundle and uses a separate pipeline stage.** Extended ops (scans/sorts/uniquify) are the "transcendentals" of the TEC: they take multiple cycles, write the XRF (extended-result FIFO), and their results must be drained by the `VectorResult` slot (`PopXrfWritePartialN` writes only the first N lanes — used when the reduction is narrower than the vector width). They run in a distinct `ProcResource` group from the three regular `VectorAlu` lanes.

> **GOTCHA — `VectorStoreAdd*` is atomic scatter-add into tile memory, not a plain store.** The per-dtype `…StoreAdd{Bf16,F32,S16,S32}` (and indexed/circular variants) accumulate the stored value into the existing `TILE_SPMEM` location. This is the building block of embedding-table gradient accumulation; the cross-tile / cross-HBM atomic equivalent is the Stream slot's `STREAM_OPCODE_SCATTER_FLOAT_ADD`. A reimplementer must encode the dtype suffix as part of the opcode, not as an operand — Ghostlite split the per-dtype forms that Viperfish folded.

---

## The VF (Access/Execute) Split

### The execute-region-only rule

It might seem natural for the outliner to split a tile_task into an `"access"` (TAC) function plus an `"execute"` (TEC) function on the TAC-bearing gens. Byte tracing shows **no such split exists in this wheel on any generation** — the SC-MLO compiler is a 2-sequencer SCS+TEC pipeline (VF, GL, *and* GF). This is the single most consequential fact about where the TEC sits in the pipeline.

```text
sc_tpu.tile_task region                         (the per-tile compute body)
   │  TileTaskOutliningPass::runOnOperation       0x13606220
   │    per-op outlining callback                  0x136066e0
   ▼
func.func( live-in memrefs )  sc.sequencer = "execute"   ← the TEC body (the ONLY value stamped on a body)
   ▲                               StringAttr "execute" @0x8681624 (7 chars)
   │  LaunchTileTaskOp::create     0x145dd0e0
enclosing func  sc.sequencer = "scs"                     ← the SCS control program
   │
   ▼  read back at lowering
LowerSequencerFunctionsPass::runOnOperation     0x13532120
   │   ScDialect::HasCoreSequencerTypeAttribute   0x14599ec0  (value=="scs",   len 3)
   │   ScDialect::HasExecuteSequencerTypeAttribute 0x1459a020 (value=="execute",len 7)
   │   (NO HasAccessSequencerTypeAttribute — no len-6 "access" predicate exists)
   ▼
per-engine codec selected by TpuSequencerType {3=SCS, 5=TEC}   ── no 4 (TAC) is ever produced
```

The per-op outlining callback (`0x136066e0`, used by the Target-parameterized pass for *all* gens) stamps the outlined func `sc.sequencer="execute"` *unconditionally* — `"execute"` (`@0x8681624`, 7 chars) is the only `sc.sequencer` value string it references, with no Target-conditional branch to a second value. The read-back predicate `HasExecuteSequencerTypeAttribute` (`0x1459a020`) confirms the value byte-exact: it accepts only a length-7 attribute whose bytes are `0x63657865` ("exec") + `0x65747563` ("cute"). There is no `HasAccessSequencerTypeAttribute` and no length-6 `"access"` comparison anywhere in the lowering chain. `MakeTpuCoreProgram` for *both* the Viperfish and Ghostlite emitters instantiates exactly two codecs — `SparseCoreScsCodecBase` + `SparseCoreTecCodecBase` — with zero `SparseCoreTacCodecBase` occurrences.

> **NOTE — the outliner never emits an `"access"` (TAC) function.** On every gen the outliner stamps only `"scs"` and `"execute"`. The standalone `"access"` strings in `.rodata` are libc math-function names (`abs`/`access`/`acos`/…) and the `sc.parallel_access` / `spirv.memory_access` attribute names — never an `sc.sequencer` value. The TAC ("access") engine survives only as a standalone codec (`SparseCoreTacCodecBase`, `TpuSequencerType=4`, glc) for the legacy `ProgramWrapper.tac` proto field; it is never reached from the MLIR tile-task pipeline. The access-region work folds into the TEC on *all* SC gens, not just 6acc60406.

### Where the tile-fetch goes

Because the compiler never produces an access function, the tile-fetch / gather work the TAC silicon could carry is emitted into the TEC `"execute"` function through the **TEC Stream slot**. `GetTransferKind` (`0x1351b140`) still classifies a transfer as `kStream` (gather/scatter) vs `kDma` (bulk) — routing it to the `TecStream` slot vs the `TecDma` slot — but it does *not* pick a sequencer: both kinds live in whichever SCS or TEC bundle their tile_task region was outlined into. `IndirectVregStream` is TEC-only (it reads its indirect offsets from a VREG, at bundle bit 322), which is the structural evidence that the indirect gather is a TEC-bundle op, not a separate engine.

> **GOTCHA — the access region is a region of the TEC bundle, not a separate engine.** "Access-vs-Execute" in this wheel is the split between the Stream/Dma descriptor fields (the access region, bits 283/322 of the TEC bundle) and the vector-compute slots (the execute region, bits 239..474) *within one TEC bundle*. A reimplementer modeling a separate TAC sequencer for VF/GL will produce a program the SC-MLO code generator never emits and the lowering chain has no predicate for.

---

## Why TEC Grows Gen-Over-Gen

### The growth, in counts

The TEC is the engine that accretes capability while the engine roster shrinks. Three independent decompile counts all rise monotonically across VF→GL→GF, against the TAC count collapsing to zero on 6acc60406:

| Metric | Viperfish (`vfc`) | Ghostlite (`glc`) | 6acc60406 (`gfc`) |
|---|---:|---:|---:|
| `SparseCoreTec*` decompiled functions | 5244 | 7803 | **8636** |
| `SparseCoreTecVectorAlu*` decompiled functions | 3215 | 4856 | **5466** |
| `VectorAlu` opcode count (proto enum) | 148 | 229 | **257** |
| `VectorStore` opcode count | 15 | 33 | 33 |
| `VectorExtended` opcode count | 28 | 52 | 53 |
| Vector-ALU opcode field width | 7-bit | 8-bit | 8-bit |
| Vector-ALU slot width | 36-bit | 37-bit | 37-bit |
| `SparseCoreTac*` decompiled functions | 932 | 952 | **0** |

The vector-ALU opcode set crosses the 7-bit ceiling (128) between Viperfish (148 ops, which only just exceeds 7 bits — the top ops fold into reserved encodings) and Ghostlite (229 ops), which is why the opcode field widens 7→8 bits and the slot 36→37 bits, shifting the GF vector lanes up relative to VF (VF Alu0 base 432, GF Alu0 base 438). Ghostlite split bf16/f32 instances of the transcendentals and conversions; 6acc60406 added the FP8 (E4m3/E5m2) and generic small-float (Exmy) pack/unpack family for embedding-optimizer weight quantization.

### Why it grows: it absorbs the other engines' roles

Two roles fold into the TEC across the generations:

- **The access/tile-fetch role (all gens, at the compiler level).** As [The VF Split](#the-vf-accessexecute-split) establishes, the SC-MLO compiler never emits an `"access"` function; the gather/scatter that TAC silicon could carry is emitted into the TEC Stream slot on every gen. The TEC bundle's high payload region (bits 283/322) holds the stream descriptor *in the same 64-byte word* as the vector load/store that consumes the fetched tile — so the engine that computes is also the engine that fetches.
- **The inner-loop dispatch role (6acc60406 silicon).** On 6acc60406 the TAC silicon is gone entirely (`SparseCoreTac*` decompiled count = 0, no `SparseCoreTacCodecBase`, no `SparseCoreTacGFSchedModelSchedClasses`). Its inner-loop tile-fetch dispatch is enabled by the SCS gaining the `BranchRelativeRotatingPreg` / `SetRotatingPredicateRegister` rotating-predicate ops — both present *only* in the `gfc` namespace (27 / 31 symbols on gfc; zero on vfc/glc) — which give the SCS a hardware-loop branch the inner tile-fetch can ride. The `TileSpmemLoadCircularBufferPostUpdate` TEC op (whose post-update auto-increments the load pointer so a TEC bundle can stream tile-fetches without a separate TAC stream-gather) is **not** a 6acc60406 addition — it ships on all three gens (vfc/glc/gfc each carry the op), so the load primitive predates the dispatch consolidation.

The result is the consolidation strategy the binary reflects: fewer engines, but the TEC alone carries more opcodes on 6acc60406 than all of Viperfish's SparseCore combined. The trade-off is heavier TEC bundles — each now issues compute *and* tile-fetch, so the LLVM scheduler must find more parallelism inside one bundle (6acc60406's TEC adds 2–3 `ProcResource` groups over Ghostlite to model the concurrent DMA issuance).

---

## Immediate-Slot Indexing

### The 6-entry indexed array

A TEC op that needs a literal operand does not carry it inline; it references one of the six 20-bit immediate slots through a 6-bit selector. The slot-index → bundle-bit map is recovered bit-exact from the `EmitImmediate` jump table (`@0xae91c68`; gfc `0x13a71920`, vfc `0x1398b7e0`) and the `SparseCoreImmediatesEncoder` (`0x1ecd1760`), whose `BitCopy` calls write all six 20-bit fields:

| Slot index | Struct field | Bundle bit | Width | Engines |
|---:|---|---:|---:|---|
| 0 | `+0x18` | 67 | 20 | SCS + TEC |
| 1 | `+0x1c` | 47 | 20 | SCS + TEC |
| 2 | `+0x20` | 27 | 20 | SCS + TEC |
| 3 | `+0x24` | 7 | 20 | SCS + TEC |
| 4 | `+0x28` | 215 | 20 | **TEC only** |
| 5 | `+0x2c` | 195 | 20 | **TEC only** |

`EmitImmediate(slot_index, value, msg)` bounds `slot_index ≤ 5` (`cmp $0x5`) and `value < 2^20` (`cmp $0x100000`), then a 6-way jump table writes the value into the struct field the encoder reads into the bundle. The low four slots are common to SCS and TEC; the high two exist only on the TEC. The VF and GF TEC immediate layout is byte-identical — the immediates do *not* shift between gens; only the vector compute region above bit 235 shifts.

### The operand → slot link

An operand references an immediate slot through the **`VectorY`** 6-bit selector (the vector-slot analog of the scalar slot's `ScalarY`). The selector enum names the slot directly:

- `VECTOR_Y_IMM0_ZERO` … `VECTOR_Y_IMM5_ZERO` — read a *single* 20-bit slot, zero-padded to the operand width.
- `VECTOR_Y_IMM1_IMM0` / `IMM3_IMM2` / `IMM5_IMM4` — read a *40-bit* literal spanning two adjacent slots (high 20 bits from the higher slot, low 20 from the lower).

So an op needing a ≤20-bit literal sets its `VectorY` selector to `IMMk_ZERO`, and the emitter calls `EmitImmediate(k, literal)` to write the literal into slot k. An op needing a ≤40-bit literal sets `IMM(k+1)_IMMk`, and the emitter calls `EmitImmediate` twice (slots k and k+1). The SCS `ScalarY` selector (`SCALAR_Y_IMM0_ZERO=40` … `IMM3_ZERO=43`, `IMM1_IMM0=44`, `IMM3_IMM2=45`, `ONES_IMM3=39`) works identically over the four scalar slots.

> **GOTCHA — there is no inline literal field in a TEC op slot.** Branch targets, DMA lengths, sync-flag ids/thresholds, and any other constant a TEC op consumes are referenced *only* through the `VectorY`/`ScalarY` immediate-slot indirection. A reimplementer who tries to pack a constant into the op slot's operand fields will find no room — the operand fields are register selectors (`VREG`/`SREG`), and a literal must be allocated into an immediate slot and named by the selector. The per-bundle limit is the six slots (and a `SparsecoreVregReadPort` conflict rule, not fully traced, that bounds how many distinct `VectorY` immediate operands the three vector lanes can reference at once).

---

## Function Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `SparseCoreTecCodecBase<…>::Encode` (vfc) | `0x139328a0` | bundle dispatcher; shared-Span call to each slot encoder | CONFIRMED |
| `BitCopy` | `0x1fa0a900` | little-endian bit packer (`dst, dst_bitoff, src, src_bitoff, nbits`) | CONFIRMED |
| `SparseCoreImmediatesEncoder::Encode` (gfc) | `0x1ecd1760` | 6 × 20-bit immediates `@7/27/47/67/195/215` | CONFIRMED |
| `SparseCoreVectorScalarEncoder::Encode` (gfc) | `0x1ecd1e00` | scalar→vector bridge `@87..110` | CONFIRMED |
| `SparseCoreScalarMiscEncoder::Encode` (gfc) | `0x1ebad840` | misc/sync/atomic slot, opcode `@127` | CONFIRMED |
| `SparseCoreTecScalarAlu1Encoder::Encode` (gfc) | `0x1ebd8040` | scalar lane 1, opcode `@154` | CONFIRMED |
| `SparseCoreTecScalarAlu0Encoder::Encode` (gfc) | `0x1ebc54a0` | scalar lane 0, opcode `@181` | CONFIRMED |
| `SparseCoreTecVectorResultEncoder::Encode` (gfc) | `0x1ecbc9e0` | XRF-pop slot, opcode `@239` | CONFIRMED |
| `SparseCoreTecVectorExtendedEncoder::Encode` (gfc) | `0x1ecab8a0` | scan/sort/uniquify region `@261..461` | HIGH |
| `SparseCoreTecVectorLoadEncoder::Encode` (gfc) | `0x1ecb9ee0` | tile vector load, opcode `@283` | CONFIRMED |
| `SparseCoreTecVectorStoreEncoder::Encode` (gfc) | `0x1eccbe20` | tile vector store + scatter-add, opcode `@353` | CONFIRMED |
| `SparseCoreTecVectorAlu2Encoder::Encode` (gfc) | `0x1ec85ae0` | vector lane 2, opcode `@388/8`; slot base 364 | CONFIRMED |
| `SparseCoreTecVectorAlu1Encoder::Encode` (gfc) | `0x1ec51900` | vector lane 1, opcode `@425/8`; slot base 401 | CONFIRMED |
| `SparseCoreTecVectorAlu0Encoder::Encode` (gfc) | `0x1ec11100` | vector lane 0, opcode `@462/8`; sel `@438/444/450/456`; pred `@470/473/474` | CONFIRMED |
| `SparseCoreTecStreamEncoder::Encode` (gfc) | `0x1ebe33e0` | Stream oneof-of-lane, opcode `@181/162`, high payload `@283/322` | CONFIRMED |
| `SparseCoreTecDmaEncoder::Encode` (gfc) | `0x1ebb6960` | Dma oneof-of-lane, opcode `@181`, high payload `@283/322` | CONFIRMED |
| `EncoderBase<…gfc Tec…>::BundleSizeBytes` | `0x1e8359e0` | dispatches codec-metadata `vtable[+0x30]` → 64 | CONFIRMED |
| `SparseCoreTecCodecBase GetBytesPerBundle` (gfc / vfc) | `0x13923a80` / `0x13933660` | packed bytes 60 (gfc) / 59 (vfc) | CONFIRMED |
| `TileTaskOutliningPass::runOnOperation` | `0x13606220` | outlines tile_task → TEC `"execute"` func | CONFIRMED |
| outliner per-op callback | `0x136066e0` | stamps `sc.sequencer="execute"` unconditionally | CONFIRMED |
| `LaunchTileTaskOp::create` | `0x145dd0e0` | replaces tile_task with a launch of the TEC func | CONFIRMED |
| `LowerSequencerFunctionsPass::runOnOperation` | `0x13532120` | reads `sc.sequencer`, lowers per-engine body | CONFIRMED |
| `ScDialect::HasExecuteSequencerTypeAttribute` | `0x1459a020` | predicate: `sc.sequencer == "execute"` (len-7 byte-exact) | CONFIRMED |
| `GetTransferKind` | `0x1351b140` | kStream/kDma classifier (routes within a sequencer, not between) | CONFIRMED |

Cross-gen anchors: vfc TEC `VectorAlu0` `0x1e954ae0` (opcode `@456/7`, **7-bit** narrow form, sel `@432/438/444/450`); glc TEC `VectorAlu0` `0x1eaa4880` (37-bit/8-bit, matching GF). The codec dispatcher passes the same buffer Span to every slot encoder on all gens; the low region (bits 7..191) and immediate layout are byte-identical VF/GL/GF — only the vector compute region above bit 235 shifts.

---

## Considerations

- **Three vector ALU lanes issue concurrently.** A legal TEC bundle can name three independent `VectorAlu` ops (lanes 0/1/2) plus a load, a store, an extended op, and a result-pop in one 64-byte word. A scheduler must satisfy the `SparsecoreVregReadPort` per-bundle conflict rule (not fully traced) that bounds how many distinct VREG read ports — and thus how many distinct `VectorY` immediate operands — the three lanes can use at once; the six immediate slots are the upper bound.
- **VF is the narrow form.** The Viperfish vector-ALU slot is 36 bits with a 7-bit opcode (single-channel predication), against Ghostlite/6acc60406's 37-bit/8-bit/dual form. A reimplementer targeting Viperfish must encode opcodes in 7 bits and use the single-channel predication header; the 148-op VF set just fits.
- **No separate TAC on any gen at the compiler level.** Do not emit an `"access"` function or a `SparseCoreTacCodecBase` program from the MLIR pipeline — neither the outliner nor the lowering chain has a path for it. The tile-fetch is a TEC Stream-slot op.
- **Unmapped regions (LOW/inferred).** The 7-bit bundle prefix (`@0..6`), the `475..511` padding, and the bit-exact field labels inside the `VectorExtended` region (`261..461`) and the `VectorScalar` bridge (`87..110`) are recovered as slot bases/extents but not field-by-field named (the per-extended-op operand-to-VREG-selector binding is the largest remaining gap). Whether the codec writes a version/valid nibble in an epilogue is undecoded (SC bundles carry no `0x55` trailer; one analysis notes a 6acc60406 NOP-bundle last byte of `0x50` that *might* be a 4-bit framing field — unconfirmed, LOW).

---

## Related Components

| Name | Relationship |
|---|---|
| `SparseCoreTecCodecBase<…, TpuSequencerType=5>` | the TEC codec this page documents (all three gens) |
| `SparseCoreTecVectorAlu0Encoder::Encode` (`0x1ec11100` gfc) | the vector lane-0 slot encoder; the `BitCopy`-immediate source for the 37-bit template |
| `TileTaskOutliningPass` (`0x13606220`) | stamps `sc.sequencer="execute"` to assign a tile_task body to the TEC |
| `LaunchTileTaskOp::create` (`0x145dd0e0`) | the launch the SCS control program issues into the TEC |
| `GetTransferKind` (`0x1351b140`) | routes a transfer to the TEC Stream vs Dma slot within the execute function |
| `SparseCoreScalarMiscEncoder` (`0x1ebad840` gfc) | the misc/sync/atomic slot the TEC shares byte-identically with SCS |

## Cross-References

- [SparseCore Overview](overview.md) — the three engine classes, per-gen presence, and the `TpuSequencerType` codec-template enum.
- [SparseCore Hardware Architecture](architecture.md) — the geometry the TEC targets and the `SparseCoreTarget`/`TpuCoreParts` sequencer indexing (the C++ `{3,4,5}` enum, with the proto off-by-one reconciled).
- [SCS (Scalar) Engine](scs-engine.md) — the control sequencer whose low-region bundle and 27-bit scalar template the TEC reuses, and that launches the TEC via `LaunchTileTaskOp`.
- [TAC Engine](tac-engine.md) — the VF/GL-only tile-fetch issuer whose access role the TEC absorbs (removed on 6acc60406).
- [Vector Opcode Enum](vector-opcode-enum.md) — the full per-slot, per-gen vector op roster the TEC executes.
- [VectorExtended (VEX)](vectorextended-vex.md) — the scan/sort/uniquify slot, the embedding-reduce heart of the TEC vector region.
- [Bundle Slot-Base Map](bundle-slot-base-map.md) — the per-engine absolute slot-bit partition for SCS / TAC / TEC.
- [SC Backend Pipeline](sc-backend-pipeline.md) — where the outliner and the sequencer-lowering passes sit in the SC-MLO pipeline.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore engines — [back to index](../index.md)
