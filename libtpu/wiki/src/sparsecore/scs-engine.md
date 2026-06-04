# SCS (Scalar) Engine

> *Every address, offset, opcode value, and bundle-bit position on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

**SCS — the SparseCore Scalar sequencer** — is the control CPU of a SparseCore. Where the [TEC](tec-engine.md) is the wide vector engine that reduces embedding tiles and the [TAC](tac-engine.md) is the tile-fetch DMA issuer, SCS is the scalar machine that runs the program counter, computes the gather/scatter addresses, manages the circular buffers, reads the chip's hardware registers (GTC clocks, tile id, sparse-core id, DMA credits), and issues the sync-flag and atomic operations that coordinate the SC tiles with each other and with the [TensorCore](../isa/overview.md). It is the narrowest of the three SparseCore engines — a **32-byte (256-bit) VLIW bundle** against TAC/TEC's 64 — and the only one whose bundle layout is *byte-identical across all three SparseCore generations* (Viperfish, Ghostlite, 6acc60406). The closest familiar analog is a small in-order scalar core driving a co-processor: SCS is the host loop, and `TileTaskOp`/`LaunchTileTaskOp` launches into the TEC are its "kernel launches."

The SCS bundle is a fixed stack of slots packed by a generic little-endian bit packer. Each slot is encoded by a dedicated `<Slot>Encoder::Encode` that the codec dispatcher (`SparseCoreScsCodecBase::Encode`, gfc `0x1391ef60`) calls in turn, handing every slot encoder the *same* output buffer `Span` — so every slot writes at an absolute bundle-relative bit position. The bundle carries **no check trailer** (unlike the TensorCore's `0x55` byte): an all-zero bundle means "all slots inactive." Above a 7-bit reserved prefix sit four 20-bit immediate slots, a 24-bit scalar→vector bridge, and three 27-bit scalar slots — `ScsScalarMisc`, `ScalarAlu1`, `ScalarAlu0` — whose opcode fields land at bundle bits 127, 154, and 181. The DMA and Stream "slots" are not separate physical regions; they are `oneof` alternatives of a scalar lane that reuse the same opcode field and borrow the lower payload region for their descriptors.

The SCS instruction set is a two-level opcode space. A 6-bit primary opcode selects either a concrete op or an op-*class*; op-classes (Sync, SyncWatch, Atomic, control, register-read, config-set) then carry a secondary sub-opcode field that picks the exact operation. The two ALU lanes (`ScalarAlu0`/`ScalarAlu1`) share one opcode namespace and differ only in bundle bit position and a handful of lane-exclusive ops; `ScsScalarMisc` carries the atomic + sync-flag family plus an integer-ALU subset (no FP, no branch). This page documents the bundle byte layout, the per-slot bit bases, the 27-bit scalar slot template, the scalar opcode roster (primary + the four escape encodings), and how SCS issues work into the TAC/TEC engines through `LaunchTileTaskOp` and the shared sync-flag pool.

For reimplementation, the contract is:

- **A 32-byte bundle with absolute per-slot bit bases, no check trailer.** Pack four 20-bit immediates (`@7/27/47/67`), a 24-bit VectorScalar bridge (`@87`), then the three 27-bit scalar slots `Misc/Alu1/Alu0` at bases `111/138/165`. Bits `0..6` and `192..255` are unwritten by any slot encoder. This layout is fixed across VF/GL/GF.
- **The 27-bit scalar slot template.** Slot-relative: x0 `@+0/5`, ScalarY `@+5/6`, x1 `@+11/5`, **OPCODE `@+16/6`**, predication header `@+22` (3-bit normal / 4-bit rotate overlap + 1-bit inversion + 1-bit is-rotate). Identical for all three scalar slots and for the scalar-lane part of Dma/Stream.
- **A two-level opcode space.** A 6-bit primary opcode is the concrete op (e.g. `IntegerAdd=0x0a`) *or* a class escape: control ops via an 11-bit field, register reads via a 17-bit field (`ReadRegister* = 0x280..0x28d`), config sets via a 16-bit field (`Set* = 0x4001..0x4005`), divide-push via a 0x16xxxx field. Alu0 and Alu1 share these values; only the bundle bit position differs.
- **The issue model is launch-by-attribute, not by op.** SCS does not carry a numeric engine selector per instruction. The compiler outlines the TEC body into a `func.func` stamped `sc.sequencer="execute"`, leaves the enclosing SCS control program stamped `"scs"`, and the per-engine codec template (`TpuSequencerType` = 3 SCS / 4 TAC / 5 TEC) is chosen from that string at lowering time.

| | |
|---|---|
| **Engine** | SparseCore Scalar sequencer (control / addressing / sync) |
| **Sequencer enum** | `TpuSequencerType` = **3** (`TPU_SEQUENCER_TYPE_SPARSE_CORE_SEQUENCER`) |
| **Bundle size** | **32 bytes / 256 bits**; no `0x55` check trailer |
| **Codec root** | `SparseCoreScsCodecBase` (per-gen `vxc.vfc` / `gxc.glc` / `gxc.gfc`); gfc `Encode` `0x1391ef60` |
| **Bit packer** | `BitCopy(dst, dst_bitoff, src, src_bitoff, nbits)` `0x1fa0a900` (little-endian) |
| **Scalar slots** | `ScsScalarMisc` (base 111, op `@127`) · `ScalarAlu1` (base 138, op `@154`) · `ScalarAlu0` (base 165, op `@181`) |
| **Scalar slot width** | 27 bits; 6-bit primary opcode `@+16` |
| **Opcode counts (gfc)** | ScalarAlu 95 (union; Alu0 78 / Alu1 82) · ScsScalarMisc 82 · Dma 3 forms · Stream 3 forms · Immediates 1 · VectorScalar 1 |
| **Cross-gen layout** | Bundle + slot bases byte-identical on VF / GL / GF |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

---

## Role and Position in SparseCore

### Purpose

SCS is the SparseCore's scalar control plane. In the embedding datapath ([Architecture](architecture.md)) it is the engine that turns a stream of lookup indices into the addresses, sync events, and tile-task launches that drive the rest of the SparseCore. Concretely it: runs the program counter and branches/calls; computes `HBM[table_base + index_i × row_stride]` gather addresses with its integer ALU; stages address/descriptor buffers in SMEM; manages the circular-buffer registers (CBREG) that ring through tile windows; reads the hardware registers that gate progress (GTC clocks, DMA-credit register, fence status, tile/sparse-core id); and issues the sync-flag and atomic operations that handshake the SC tiles with each other and with the TensorCore. It owns *no* vector path and *no* tile load/store — that is the TEC's job. SCS is the loop; the TEC is the body.

> **NOTE — SCS is the persistent "outer" program; the TEC body is outlined.** The compiler partitions an SC computation by outlining each `sc_tpu.tile_task` region into its own `func.func` (stamped `sc.sequencer="execute"`, the TEC) and replacing it with a `LaunchTileTaskOp` in the enclosing function — which stays `sc.sequencer="scs"`. So the SCS program is the control loop that *issues launches*; the per-tile compute lives in the launched TEC functions. See [Region → Sequencer Outliner](region-to-sequencer-outliner.md).

### Engine roster

SCS exists on every generation that ships SparseCore. TAC is the engine that varies: present on Viperfish and Ghostlite, dropped on 6acc60406 (which folds tile-fetch issuance into the SCS+TEC pair). SCS itself is constant. Marketing-name / codename / family-namespace mapping follows the sibling [SparseCore Overview](overview.md).

| Marketing | Codename | Family ns | SCS | bundle | Notes |
|---|---|:---:|:---:|---|---|
| TPU v5p | Viperfish | `vxc.vfc` | Y | 32 B | first three-engine split SCS+TAC+TEC |
| TPU v6e | Ghostlite | `gxc.glc` | Y | 32 B | full SCS+TAC+TEC; widened TEC |
| TPU7x | 6acc60406 | `gxc.gfc` | Y | 32 B | no TAC; SCS gains rotating-preg ops |

> **CORRECTION (SCS-ENUM) —** two enum spaces number the SparseCore sequencers and they are off by one, but the split is **proto-vs-C++**, not codec-vs-anything. The C++ `tpu::TpuSequencerType` enum numbers SCS=3 / TAC=4 / TEC=5 — confirmed in the demangled `EncoderBase<… SparseCoreScsCodecBase …, LN3tpu16TpuSequencerTypeE3E>` symbol (the `3E` suffix is the literal 3), and the same `{3,4,5}` numbering is what `TpuSequencerTypeToString` renders (`off_22010DE0[3]="SparseCoreSequencer"`), what the codec-metadata tables take, and what `TpuCoreParts::SequencerParts` is indexed with (see [Architecture](architecture.md) — geometry is read at codec-enum TEC=5, so SCS=3, not 4). Every C++-side use shares this one numbering. The off-by-one peer is the **protobuf** enum `TpuSequencerTypeProto`, which reserves `INVALID=0` and so numbers SCS=4 / TAC=5 / TEC=6; `TpuSequencerTypeFromProto` subtracts one when a proto value crosses into the C++ enum. Use `{3,4,5}` for every encoder, codec, and core-parts index; only a raw `TpuSequencerTypeProto` field carries `{4,5,6}`. Do not mix the two.

---

## The SCS Bundle (32 bytes)

### Layout

The bundle is 256 bits. No slot encoder writes below bit 7 or above bit 191; bits `0..6` are a reserved/header prefix and `192..255` are padding. The byte layout was recovered from the `BitCopy` destination-bit immediates inside each per-slot encoder (the dispatcher passes every encoder the same buffer `Span`, so the bit offsets are absolute), and is byte-identical on Viperfish, Ghostlite, and 6acc60406.

```text
SCS bundle — 32 bytes / 256 bits (VF / GL / GF identical)
bit:  0      7                 87        111       138       165       192        255
      ┌──────┬──────────────────┬─────────┬─────────┬─────────┬─────────┬──────────┐
      │ rsvd │ ScalarImmediates │ Vector  │ ScsScal │ ScalarA │ ScalarA │ reserved │
      │ 7b   │ 4 × 20-bit       │ Scalar  │ arMisc  │ lu1     │ lu0     │ / pad    │
      │ hdr  │ @7,@27,@47,@67   │ bridge  │ op@127  │ op@154  │ op@181  │ 64 bits  │
      └──────┴──────────────────┴─────────┴─────────┴─────────┴─────────┴──────────┘
                                  24-bit    27-bit    27-bit    27-bit
      Dma   (oneof of a scalar lane): opcode @181/@154, descriptor payload @87..142
      Stream(oneof of a scalar lane): opcode @181/@154, descriptor payload @99..142
```

| Slot | Base | End | Width | Opcode bit | Internal template | Confidence |
|---|---:|---:|---:|---:|---|---|
| (reserved header) | 0 | 6 | 7 | — | bundle prefix; meaning not decoded | MEDIUM |
| `ScalarImmediates` | 7 | 86 | 80 | — | 4 × 20-bit (`@7,@27,@47,@67`) | CONFIRMED |
| `VectorScalar` | 87 | 110 | 24 | — | scalar→vector bridge (4 × {5-bit,1-bit} pairs) | CONFIRMED |
| `ScsScalarMisc` | 111 | 137 | 27 | 127 | 27-bit scalar template | CONFIRMED |
| `ScalarAlu1` (lane 1) | 138 | 164 | 27 | 154 | 27-bit scalar template | CONFIRMED |
| `ScalarAlu0` (lane 0) | 165 | 191 | 27 | 181 | 27-bit scalar template | CONFIRMED |
| (reserved / pad) | 192 | 255 | 64 | — | unwritten by any slot encoder | HIGH |
| `Dma` (oneof of lane) | 87 | 191 | — | 181 / 154 | scalar opcode + payload `@87..142` | CONFIRMED |
| `Stream` (oneof of lane) | 99 | 191 | — | 181 / 154 | scalar opcode + payload `@99..142` | CONFIRMED |

> **QUIRK — there is no separate "DMA slot" or "Stream slot."** A SparseCore DMA or Stream instruction is a `oneof` *form* of a scalar lane: it writes its opcode into the lane's 6-bit opcode field (`@181` for lane 0, `@154` for lane 1) and spills its multi-word descriptor into the lower payload region (the immediate / VectorScalar / Misc area, bits `87..142`). This is why prior decode work reported the Stream opcode "at bit 181" — that is the *absolute* scalar-lane-0 opcode base, not a Stream-specific position. A reimplementer who allocates a physically separate DMA/Stream region will mis-size the bundle and double-book bits `87..142`.

### Encoder dispatch and the shared buffer

The codec dispatcher is a thin loop that invokes each member slot encoder in turn and hands every one of them the *same* output buffer. The decompiled gfc `SparseCoreScsCodecBase::Encode` (`0x1391ef60`) calls each `<Slot>::Encode((char*)bundle + member_off, msg, a3 /*buf.ptr*/, a4 /*buf.len*/)` — the `a3,a4` pair (the destination buffer span) is constant across every call, only the `rdi` member-encoder pointer differs. Each encoder then packs its fields with the generic LE packer `BitCopy(dst, dst_bitoff, src, src_bitoff, nbits)` (`0x1fa0a900`). The `dst_bitoff` immediate is therefore the absolute bundle bit.

```c
function SparseCoreScsCodecBase_Encode(bundle, buf):     // gfc 0x1391ef60
    ImmediatesEncoder.Encode(  bundle, msg, buf.ptr, buf.len)   // @7..86   (0x1eb5bd20)
    VectorScalarEncoder.Encode(bundle, msg, buf.ptr, buf.len)   // @87..110 (0x1ecd1e00)
    ScsScalarMiscEncoder.Encode(bundle,msg, buf.ptr, buf.len)   // @111..137 op@127 (0x1eb914a0)
    ScalarAlu1Encoder.Encode(  bundle, msg, buf.ptr, buf.len)   // @138..164 op@154 (0x1eb7cd00)
    ScalarAlu0Encoder.Encode(  bundle, msg, buf.ptr, buf.len)   // @165..191 op@181 (0x1eb693c0)
    StreamEncoder.Encode(      bundle, msg, buf.ptr, buf.len)   // oneof: op@181/154, payload@99 (0x1eb9b4c0)
    DmaEncoder.Encode(         bundle, msg, buf.ptr, buf.len)   // oneof: op@181/154, payload@87 (0x1eb5a3a0)
    // buf is zero-initialized by the EncodeBundle wrapper; no check-byte epilogue written.
```

> **GOTCHA — the bundle has no check trailer; you must zero it.** The `EncodeBundle` wrapper allocates and `memset`s the 32-byte buffer before dispatch, because an inactive slot writes nothing and an all-zero bundle is the canonical "all slots inactive" NOP. There is no trailing `0x55` validity byte to append (the TensorCore bundles have one; SC bundles do not). Skipping the zero-init leaves stale bits in unwritten slots, which the decoder will interpret as live ops.

### Bundle size accessor

`EncoderBase<… SparseCoreScsCodecBase …>::BundleSizeBytes` (gfc `0x1e835260`) does not return a literal; it dispatches the codec-metadata vtable: `return (*(codec_metadata->vtable[+0x30]))(codec_metadata)` — vtable slot 6, which returns 32 for sequencer type 3 in the per-gen `CodecMetadata`. The same indirection returns 64 for TAC (seq 4) and TEC (seq 5).

---

## The 27-Bit Scalar Slot Template

### Layout

All three scalar slots share one internal template; only the slot base differs. Slot-relative offsets, confirmed byte-exact against the `ScalarAlu0` encoder (`0x1eb693c0`), whose `BitCopy` calls write `@165/5` (x0), `@170/6` (ScalarY), `@176/5` (x1), `@181/6` (opcode), and the predication header at `@187/3`, `@190/1`, `@187/4`, `@191/1`:

```text
27-bit scalar slot (slot-relative; absolute = slot_base + offset)
  +0   w5   operand x0          scalar-register selector
  +5   w6   ScalarY             scalar-register-or-immediate selector
  +11  w5   operand x1          scalar-register selector
  +16  w6   OPCODE              6-bit primary opcode (≤ 64 ops)
  +22  w3   normal_predication  SparsecoreNormalPredication
  +22  w4   rotate_predication  overlaps normal when is_rotate (16-entry ring)
  +25  w1   predication_inversion
  +26  w1   is_rotate_predication
```

So the absolute opcode bits fall out as `base + 16`: `ScsScalarMisc` opcode `@127` (= 111+16), `ScalarAlu1` `@154` (= 138+16), `ScalarAlu0` `@181` (= 165+16). The three slots stack 27 bits apart directly above the 24-bit VectorScalar bridge.

| Slot | Base | x0 `@+0/5` | ScalarY `@+5/6` | x1 `@+11/5` | OPCODE `@+16/6` | pred header `@+22` |
|---|---:|---:|---:|---:|---:|---|
| `ScsScalarMisc` | 111 | 111 | 116 | 122 | **127** | 133/136/137 |
| `ScalarAlu1` | 138 | 138 | 143 | 149 | **154** | 160/163/164 |
| `ScalarAlu0` | 165 | 165 | 170 | 176 | **181** | 187/190/191 |

> **NOTE — the predication header is a 3-bit/4-bit overlap, not two fields.** `normal_predication` (3 bits) and `rotate_predication` (4 bits) share the same starting bit `@+22`; the 1-bit `is_rotate_predication` `@+26` selects which interpretation applies. When rotating predication is active the 4-bit field indexes a 16-entry rotating-predicate ring (6acc60406 adds `SetRotatingPredicateRegister` and `BranchRelativeRotatingPreg` to drive it). A reimplementer must not allocate 3+4 distinct bits — it is 4 bits with two meanings.

> **GOTCHA — the two 5-bit operand fields are operand selectors, not a result selector (HIGH confidence).** The opcode and predication-header positions are bit-exact; the precise operand-vs-result role of x0/x1 was inferred from the scalar-ALU operand model (`ScalarY @+5` is the operand-or-immediate selector documented in the ScalarY field work). Treat x0/x1 as register-source selectors; the destination encoding was not independently bit-traced here.

---

## The SCS Scalar Opcode Roster

### Two-level opcode space

Each SC opcode is recoverable byte-exact from its op-form's `Opcode::Matches()` predicate — libtpu emits one C++ type per opcode per gen (`SparseCore<Slot><OpName>Opcode`), each with a `Matches()` that masks the decoded opcode field and compares against the op's own signature. The `cmp`/mask immediate *is* the opcode. Three predicate shapes appear:

```c
// Form A — flat 6-bit (e.g. ScsScalarMisc IntegerAdd, 0x1ebabf00):
return ((word_0x10 >> 63 | word_0x18 << 1) & 0x3F) == 0x0A;     // opcode straddles word boundary

// Form B — composite: 6-bit base + 5-bit sub-class (e.g. AtomicTileAdd, 0x1ebabbe0):
//   base = (bit63 | low5<<1);  ^base==0 AND (word & 0xF800000000000) ^ sub_sig == 0
return ((bit63 + 2*low5) ^ 0x08 | (word_0x10 & 0xF800000000000) ^ 0x800000000000) == 0;  // base 8, sub 1

// Form C — mask-compare (the ALU lanes, e.g. ScalarAlu0 IntegerAdd, 0x1eb67660):
return (word_0x18 & 0x7E0000000000000) == 0x140000000000000;    // (0x140.. >> 53) = 0x0A
```

A **6-bit primary opcode** selects a concrete op or an op-*class*. Op-classes (Sync, SyncWatch, Atomic, control, register-read, config-set) carry a secondary sub-opcode field. The primary values `0..63` are the canonical scalar set; the wider encodings (`0x280+`, `0x4001+`, `0x16xxxx`) are class escapes living in dedicated bit fields.

### Shared ALU namespace (ScalarAlu0 / ScalarAlu1)

The two ALU lanes share one opcode namespace — `IntegerAdd=0x0a`, `BitwiseAnd=0x0e`, `CompareIntegerEq=0x1e`, the FP-compare block `0x2a..0x2f` are identical on both — and differ only in (a) bundle bit position (`@181` vs `@154`) and (b) a few lane-exclusive ops. Values are gen-invariant for shared ops (vfc `IntegerAdd` is `0x0a`, byte-identical to gfc).

| Opcode | Mnemonic | Class | Lane | Confidence |
|---:|---|---|---|---|
| `0x0a` | `IntegerAdd` | integer ALU | Alu0+Alu1 | CONFIRMED |
| `0x0b` | `IntegerAddWithOverflowCheck` | integer ALU | both | CONFIRMED |
| `0x0c`/`0x0d` | `IntegerSubtractYX` / `…WithOverflowCheck` | integer ALU | both | CONFIRMED |
| `0x0e`/`0x0f`/`0x10` | `BitwiseAnd` / `BitwiseOr` / `BitwiseXor` | bitwise | both | CONFIRMED |
| `0x11`/`0x12` | `FloatingPointAdd` / `FloatingPointSubtractYX` | FP ALU | Alu1 | HIGH |
| `0x13` | `FloatingPointMultiply` | FP ALU | Alu0 | HIGH |
| `0x14`/`0x15` | `Multiply32BitIntegers` / `…UnsignedReturningHighHalf` | integer mul | Alu0 | HIGH |
| `0x16` | `DivideWithRemainderXY` | integer div | Alu0 | HIGH |
| `0x17`/`0x18`/`0x19` | `LogicalShiftLeft/Right`, `ArithmeticShiftRight` `XByYPlaces` | shift | both | CONFIRMED |
| `0x1a`/`0x1b` | `MaxOfTwoFloatingPointValues` / `MinOf…` | FP minmax | both | HIGH |
| `0x1c`/`0x1d` | `MaxOfTwoUnsignedIntValues` / `MinOf…` | int minmax | both | CONFIRMED |
| `0x1e`–`0x27` | `CompareInteger{Eq,Ne}` + signed/unsigned `Gt/Gte/Lt/Lte` | compare | both | CONFIRMED |
| `0x28` | `CarryOutFromIntegerUnsigned` | carry | both | CONFIRMED |
| `0x29` | `PredicateOr` | predicate | both | CONFIRMED |
| `0x2a`–`0x2f` | `CompareFloatingPoint{Eq,Neq,Gt,Gte,Lt,Lte}` | FP compare | both | HIGH |
| `0x30` | `IsInfOrNan` | FP classify | both | HIGH |
| `0x31` | `ArithmeticShiftLeftXByYPlacesCheckOverflow` | shift | both | CONFIRMED |
| `0x3e` | `LogicalShiftLeftOnesXByYPlaces` | shift (GF-only) | Alu0 | HIGH |

Lane-exclusive primary ALU ops (Alu1-only): `0x01 ScalarLoadSmemY`, `0x02 ScalarLoadSmemXY`, `0x03 ScalarStoreXToSmemY`, `0x09 DescriptorBasedDma`, `0x32 ScalarStoreXToSmemSumDestAndY`, `0x33 AddCbreg`, `0x34 TaskRequestClearIbuf`, `0x35 WriteCbreg`, `0x36 ReadCbreg`, `0x37 TaskRequest`, `0x3c ScalarStoreCircularBuffer`, `0x3d ScalarLoadCircularBuffer`. (`AddCbreg=0x33` confirmed: `(word6 & 0xFC000000) == 0xCC000000`, `0xCC000000 >> 26 = 0x33`.)

### The four class-escape encodings

When the primary value names a *class*, the concrete op lives in a wider escape field. Bit positions are lane-specific (Alu0 lives higher in the struct than Alu1); the *values* are slot-independent.

| Escape | Field width | Value range | Examples | Confidence |
|---|---|---|---|---|
| Control | 11-bit | `0x00..0x1d` | `Halt=0x00`, `BranchAbsolute=0x04`, `BranchRelative=0x05`, `CallAbsolute=0x06`, `CallRelative=0x07`, `ScalarFence=0x09`, `ConvertInt32ToFloat32=0x0b`, `Delay=0x03`, `BranchRelativeRotatingPreg=0x18` (GF) | CONFIRMED |
| Register-read | 17-bit | `0x280..0x28d` | `ReadRegisterLccLow=0x280`, `…GtcLow=0x282`, `…SparseCoreId=0x286`, `…Tileid=0x289`, `…TaskBitmap=0x28a`, `…DmaCreditRegister=0x28d` | CONFIRMED |
| Config-set | 16-bit | `0x4001..0x4005` | `SetTag=0x4001`, `SetIndirectFilterValue=0x4002`, `SetDmaCredit=0x4003`, `SetDmaThrottleSflagRange=0x4004`, `SetRotatingPredicateRegister=0x4005` (GF) | HIGH |
| Divide-push | 11-bit (Alu0) | `0x160001..0x160002` | `DivideWithRemainderXYPushQuotient`, `…PushRemainder` | HIGH |

Verified anchors: `Halt` `(word15 & 0x7FF) == 0` → control 0x00; `BranchAbsolute` `(word3 & 0x7FF…) == 0x4000…` → `0x4000…>>50 = 0x04`; `ReadRegisterLccLow` `(word3 & 0x7FFFC…) == 0xA000…` → `0xA000…>>42 = 0x280`.

> **QUIRK — register reads and config sets are not opcode `0x28x`/`0x400x` in a 6-bit field.** They cannot be; the primary opcode is only 6 bits. The `0x280+` / `0x4001+` values live in a *wider escape field* (17-bit / 16-bit) that overlays the slot when the primary opcode marks the register-read / config-set class. A reimplementer reading "ReadRegisterGtcLow = 0x282" must place 0x282 in the 17-bit field, not the 6-bit opcode field.

### ScsScalarMisc — the atomic + sync slot

`ScsScalarMisc` (base 111, opcode `@127`) is the sync/atomic engine. It carries no FP and no branch; it holds the sync-flag family that coordinates SC tiles with each other and with the TensorCore, plus an integer-ALU subset duplicated from the ALU lanes. The opcode space is heavily composite: a 6-bit base names a class, and a 5-bit sub-opcode (the sync/atomic *mode*, at struct-relative bit 47, or an extended-ALU class at bit 58) picks the op.

| Base | Class | Sub-opcode field | Members | Confidence |
|---:|---|---|---|---|
| `0x00` | extended-ALU | sub `@bit58` | `CoreInterrupt(0)`, `CountLeadingZeros(14)`, `MoveY(13)` | HIGH |
| `0x01` | Sync compare-and-set | mode `@bit47` | `SyncDone/Equal/NotEqual/Greater/GreaterOrEqual/Less/NotDone/…/…OrDone` (12 modes) | CONFIRMED |
| `0x02` | SyncWatch | mode `@bit47` | `SyncWatch{Done,Equal,…,LessOrDone}` (12 modes) | CONFIRMED |
| `0x03` | SyncWatch escape | sub `@bit58` | `SyncWatchWait(0)`, `SyncWatchWaitSelect(1)` | HIGH |
| `0x04` | SyncWatch escape | sub `@bit58` | `SyncWatchEnd(0)`, `SyncWatchEndSelect(1)` | HIGH |
| `0x05` | set-sync | mode `@bit47` | `SetSyncFlag(0)`, `SetSyncDone(1)`, `AddSyncFlag(2)` | CONFIRMED |
| `0x06` | read-sync | sub `@bit58` | `ReadSyncFlag(0)`, `ReadSyncDone(1)`, `ReadSyncPublicAccess(2)` | HIGH |
| `0x07` | barrier | mode `@bit47` | `SyncBarrier(0)`, `SetPOrTState(4)` | HIGH |
| `0x08` | Atomic | op×set-done `@bit47` | `AtomicTile{Write,Add}[SetDone][Inverted]`, `AtomicRemote{Write,Add}…` (12 forms) | CONFIRMED |

`ScsScalarMisc` also carries flat 6-bit ops mirrored from the ALU set: `IntegerAdd=0x0a`, `BitwiseAnd=0x0e`, the integer compare block `0x1e..0x27`, plus `ReadSyncStateValue=0x2a`, `ReadSyncStateDone=0x2b`, `SetTracemark=0x2d`, `Trace=0x2e`, `SetSyncFlagPublicAccess=0x2f`, `SmemFetchAndAdd=0x38`. (`AtomicTileAdd` confirmed base 8 / sub 1; `IntegerAdd` confirmed `==0x0a`.)

> **NOTE — 6acc60406 simplified the sync model.** Ghostlite's `ScalarMisc` carries 100 ops including the dual-channel sync family (`Set{Both,Other}Sync*`, `Add{Both,Other}SyncFlag`) and the `Yieldable*` sync ops; 6acc60406 drops both families (down to 82 ops) and adds the single `SetPOrTState`. Confirmed by symbol count: glc carries 272 `Yieldable*` and 126 `Set/Add{Both,Other}*` SparseCore symbols against **zero** in gfc, while `SetPOrTState` appears only in gfc. The interpretation is a non-yielding tile scheduler: fewer sync primitives, deterministic latency. A reimplementer targeting 6acc60406 must not emit the `Yieldable*` or `*Both*`/`*Other*` sync ops — they have no encoder in gfc.

### Per-generation deltas

The scalar ISA is gen-invariant for shared ops; the deltas are small and concentrated in halt/yield and the rotating-predicate ring.

| Aspect | Viperfish | Ghostlite | 6acc60406 |
|---|---|---|---|
| Bundle / slot layout | 32 B, bases 111/138/165 | identical | identical |
| Primary opcode width | 6-bit | 6-bit | 6-bit |
| `ScalarAlu` op count (union) | 96 | 95 | 95 |
| `ScsScalarMisc` op count | 100 | 100 | 82 |
| Halt/yield ops | `HaltYield`, `HaltYieldConditional`, `ReadRegisterYieldRequest`, `ScalarFenceScmf` | same family present | (dropped) |
| Rotating-preg ops | — | — | `BranchRelativeRotatingPreg`, `LogicalShiftLeftOnesXByYPlaces`, `SetRotatingPredicateRegister`, `MoveCbreg`, `ScalarStoreXToSmemSumDestAndY` |
| `IntegerAdd` value | `0x0a` | `0x0a` | `0x0a` |

---

## How SCS Issues into TAC / TEC

### The launch-by-attribute model

SCS does not embed a numeric engine selector in each instruction. Engine assignment is a *string attribute on the enclosing function*, produced by the region→sequencer outliner and read back at lowering. The flow:

```text
sc_tpu.tile_task region                         (the per-tile compute body)
   │  TileTaskOutliningPass::runOnOperation       0x13606220
   │    per-op callback                            0x136066e0
   ▼
func.func(  live-in memrefs )  sc.sequencer = "execute"   ← the TEC body
   ▲                                  qmemcpy(…, "execute", 7) in the outliner callback 0x136066e0
   │  LaunchTileTaskOp::create        0x145dd0e0
enclosing func  sc.sequencer = "scs"                       ← the SCS control program
   │
   ▼  read back at lowering
LowerSequencerFunctionsPass::runOnOperation     0x13532120
   │   ScDialect::HasCoreSequencerTypeAttribute   0x14599ec0  (reads "sc.sequencer", 12; "scs" path)
   │   ScDialect::HasExecuteSequencerTypeAttribute 0x1459a020 ("execute" path)
   ▼
per-engine codec selected by TpuSequencerType {3=SCS, 4=TAC, 5=TEC}
```

The outliner (`TileTaskOutliningPass`, `0x13606220`) walks each `sc_tpu.tile_task`, collects the region's live-in `mlir::Value`s as the outlined func's arguments (`getUsedValuesDefinedAbove`), builds a `FunctionType` from the live-in memref shapes, clones the region into a new `func.func` (`Region::cloneInto`), wires the entry with a `cf.BranchOp`, replaces the `tile_task` with a `LaunchTileTaskOp`, erases the original, and stamps the new func `sc.sequencer="execute"`. The enclosing function — the SCS control program that issues the launches — carries `sc.sequencer="scs"`. The downstream `LowerSequencerFunctionsPass` reads the string back through the `ScDialect` predicates and selects the SCS/TAC/TEC encoder by the codec-template `TpuSequencerType` ({3,4,5}).

> **QUIRK — the only `sc.sequencer` string values the binary carries are `"scs"` and `"execute"`.** Both string literals are present (`"execute"` at 5 sites, `"scs"` at 1; verified by `qmemcpy(…, "execute", 7)` in the outliner callback and a `"scs", 3` literal elsewhere). There is **no `"access"` string** anywhere in the binary — the SCS↔TEC partition is the only one the attribute model expresses, and TileTaskOp carries the `ParentFuncHasCoreSequencerTypeAttribute` trait rather than a per-engine string. TAC's presence on Viperfish/Ghostlite is a codec-namespace fact (`vxc.vfc`/`gxc.glc` ship `SparseCoreTacCodecBase`), not an outliner-string fact; on 6acc60406 the `gfc` namespace ships zero `SparseCoreTac*` symbols, so tile-fetch folds into the `"execute"` (TEC) function. UNVERIFIED: that any pass ever stamps a distinct `"access"` attribute for TAC.

### The control / sync primitives SCS uses to issue

The SCS-side mechanics of a launch and handoff are built from the scalar roster above:

- **Address setup** — integer ALU (`IntegerAdd`, `Multiply32BitIntegers`, the shift/compare block) computes `table_base + index × stride`; results are staged in SMEM via `ScalarStoreXToSmemY` / circular-buffer stores and via the CBREG ops (`AddCbreg`, `WriteCbreg`, `ReadCbreg`, `MoveCbreg`). See [CBREG Circular-Buffer Register](cbreg.md).
- **Register-gated progress** — `ReadRegisterDmaCreditRegister=0x28d`, `ReadRegisterFenceStatus=0x28b`, `ReadRegisterGtcLow/High=0x282/0x283`, `ReadRegisterTileid=0x289`, `ReadRegisterSparseCoreId=0x286` read the chip registers that throttle issuance; `SetDmaCredit=0x4003` and `SetDmaThrottleSflagRange=0x4004` write the DMA-credit/throttle state.
- **The launch** — `TaskRequest=0x37` / `TaskRequestClearIbuf=0x34` request a tile task; the lowered `LaunchTileTaskOp` carries the outlined TEC func. `BranchRelativeRotatingPreg` (GF) drives the inner-loop tile-fetch dispatch that previously needed TAC's separate sequencer.
- **The handshake** — `ScsScalarMisc` issues `SetSyncFlag`/`AddSyncFlag` (base 0x05) and the `Sync*` compare-and-set family (base 0x01) so the TEC and the TensorCore wait on / signal completion through the shared sync-flag pool; the `Atomic*` family (base 0x08) does tile/remote atomic write/add with optional set-done.
- **The fence** — `ScalarFence=0x09`, `ScalarFenceStreamHbm=0x1c`, `ScalarFenceStreamSpmem=0x1d` order the outstanding stream/DMA traffic before the launch returns.

> **NOTE — SCS issues the *control* of a gather, not the gather itself.** The actual indirect HBM traffic is a Stream/DMA op (a `oneof` form of an SCS scalar lane, opcode `@181/154`) consumed by the TAC stream engine on VF/GL or the TEC stream slot on 6acc60406. SCS computes the indices/addresses and issues the descriptor; the bytes move on TAC/TEC. The `STREAM_OPCODE_SCATTER_FLOAT_ADD` atomic-into-HBM primitive that justifies SparseCore is one of these forms. See [Stream Gather/Scatter](stream-gather-scatter.md).

---

## Function Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `SparseCoreScsCodecBase::Encode` (gfc) | `0x1391ef60` | bundle dispatcher; shared-Span call to each slot encoder | CONFIRMED |
| `BitCopy` | `0x1fa0a900` | little-endian bit packer (`dst, dst_bitoff, src, src_bitoff, nbits`) | CONFIRMED |
| `SparseCoreScalarImmediatesEncoder::Encode` | `0x1eb5bd20` | 4 × 20-bit immediates `@7/27/47/67` | CONFIRMED |
| `SparseCoreVectorScalarEncoder::Encode` | `0x1ecd1e00` | scalar→vector bridge `@87..110` | CONFIRMED |
| `SparseCoreScsScalarMiscEncoder::Encode` | `0x1eb914a0` | misc/sync/atomic slot, opcode `@127` | CONFIRMED |
| `SparseCoreScalarAlu1Encoder::Encode` | `0x1eb7cd00` | scalar lane 1, opcode `@154` | CONFIRMED |
| `SparseCoreScalarAlu0Encoder::Encode` | `0x1eb693c0` | scalar lane 0, opcode `@181`; template bit-exact | CONFIRMED |
| `SparseCoreStreamEncoder::Encode` | `0x1eb9b4c0` | Stream oneof-of-lane, opcode `@181/154`, payload `@99` | CONFIRMED |
| `SparseCoreDmaEncoder::Encode` | `0x1eb5a3a0` | Dma oneof-of-lane, opcode `@181/154`, payload `@87` | CONFIRMED |
| `EncoderBase<…gfc Scs…>::BundleSizeBytes` | `0x1e835260` | dispatches codec-metadata `vtable[+0x30]` → 32 | CONFIRMED |
| `SparseCoreScalarMiscIntegerAddOpcode::Matches` | `0x1ebabf00` | flat-6 predicate, `==0x0a` (form A) | CONFIRMED |
| `SparseCoreScalarMiscAtomicTileAddOpcode::Matches` | `0x1ebabbe0` | composite predicate, base 8 / sub 1 (form B) | CONFIRMED |
| `SparseCoreScalarAlu0IntegerAddOpcode::Matches` | `0x1eb67660` | mask-compare, `(w&0x7E0…)==0x140…` → 0x0a (form C) | CONFIRMED |
| `SparseCoreScalarAlu0HaltOpcode::Matches` | `0x1eb67500` | control escape, `(w15&0x7FF)==0` → 0x00 | CONFIRMED |
| `SparseCoreScalarAlu0BranchAbsoluteOpcode::Matches` | `0x1eb67d40` | control escape → 0x04 | CONFIRMED |
| `SparseCoreScalarAlu0ReadRegisterLccLowOpcode::Matches` | `0x1eb67560` | 17-bit escape → 0x280 | CONFIRMED |
| `SparseCoreScalarAlu1AddCbregOpcode::Matches` | `0x1eb7b5a0` | `(w6&0xFC000000)==0xCC000000` → 0x33 | CONFIRMED |
| `TileTaskOutliningPass::runOnOperation` | `0x13606220` | region→sequencer outliner driver | CONFIRMED |
| `LowerSequencerFunctionsPass::runOnOperation` | `0x13532120` | reads `sc.sequencer`, lowers per-engine body | CONFIRMED |
| `ScDialect::HasCoreSequencerTypeAttribute` | `0x14599ec0` | predicate: `sc.sequencer == "scs"` | CONFIRMED |

Cross-gen anchors: vfc SCS `ScalarAlu0` `0x1ee82ce0` (op `@181`), `ScsScalarMisc` `0x1eeac160` (op `@127`); glc SCS `ScalarAlu0` `0x1e9d2140` (op `@181`) — all byte-identical to gfc. The `Encode` dispatcher and slot bases do not change across VF/GL/GF.

---

## Considerations

- **No FP and no branch in the Misc slot.** `ScsScalarMisc` is sync/atomic + integer-ALU only; FP arithmetic, FP compare, branches, calls, and SMEM load/store live in the ALU lanes. A scheduler must not place a sync op into an ALU lane or an FP op into Misc.
- **Lane asymmetry.** `ScalarAlu1` holds the SMEM load/store, CBREG, and task-request ops (`ScalarLoadSmemY`, `AddCbreg`/`WriteCbreg`/`ReadCbreg`, `TaskRequest`, `DescriptorBasedDma` — each present only in the Alu1 namespace, Alu0=0); `ScalarAlu0` holds the branch/call and divide-push ops (`BranchAbsolute`, `CallAbsolute`, `DivideWithRemainderXYPushQuotient` — Alu0-only). Many ops (`IntegerAdd`, `Halt`, `ConvertInt32ToFloat32`) appear in *both* lanes. The two lanes are not interchangeable despite sharing the opcode namespace.
- **6acc60406's non-yielding scheduler.** The dropped `Yieldable*` and dual-channel sync families mean a 6acc60406 SCS program cannot express cooperative yield-on-sync; it relies on the rotating-predicate ring and deterministic latency instead.
- **Unmapped regions (LOW/inferred).** The 7-bit bundle prefix (`@0..6`) and the `192..255` padding are unwritten by any slot encoder; whether the codec sets a version/valid nibble in an epilogue is not decoded (the bundle carries no `0x55` trailer). The absolute bundle bit of each composite `ScsScalarMisc` sub-opcode field (the sync/atomic mode at struct-relative bit 47/58) is recovered as a within-struct offset but was not re-derived to its absolute bundle bit for all 50 composite Misc ops.

---

## Related Components

| Name | Relationship |
|---|---|
| `SparseCoreScsCodecBase::Encode` (`0x1391ef60`) | the SCS bundle encoder this page documents |
| `TileTaskOutliningPass` (`0x13606220`) | stamps `sc.sequencer` to assign the SCS parent vs TEC body |
| `LowerSequencerFunctionsPass` (`0x13532120`) | reads `sc.sequencer` back and selects the per-engine codec |
| `getSequencerType` (`0x13507760`) | the accessor that maps the string attribute to `TpuSequencerType` |

## Cross-References

- [SparseCore Overview](overview.md) — the three engine classes, per-gen presence, and the `TpuSequencerType` codec-template enum.
- [SparseCore Hardware Architecture](architecture.md) — the geometry SCS targets and the `SparseCoreTarget`/`TpuCoreParts` sequencer indexing (the C++ `{3,4,5}` enum, with the proto off-by-one reconciled).
- [TAC Engine](tac-engine.md) — the tile-fetch DMA issuer that reuses the SCS low-region bundle layout (VF/GL only).
- [TEC (Vector) Engine](tec-engine.md) — the wide vector engine the SCS program launches via `LaunchTileTaskOp`.
- [Scalar Opcode Enum](scalar-opcode-enum.md) — the full SCS / TAC scalar ALU and scalar-misc opcode roster.
- [Bundle Slot-Base Map](bundle-slot-base-map.md) — the per-engine absolute slot-bit partition for SCS / TAC / TEC.
- [Region → Sequencer Outliner](region-to-sequencer-outliner.md) — the `TileTaskOutliningPass` that assigns each region its engine.
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC engine-selection accessor.
- [CBREG Circular-Buffer Register](cbreg.md) — the circular-buffer registers SCS manages with `AddCbreg`/`ReadCbreg`/`WriteCbreg`/`MoveCbreg`.
- [M-Register Predicate Word](m-register-predicate.md) — the predication header that overlays the 27-bit scalar slot.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the Stream/DMA oneof-of-lane descriptor and the `STREAM_OPCODE_*` set.
- [SC Backend Pipeline](sc-backend-pipeline.md) — where the outliner and the sequencer-lowering passes sit in the SC-MLO pipeline.
- [SC EmitX Dispatcher](sc-emitx-dispatcher.md) — the seq3/seq4/seq5 → EmitX jump tables that drive per-engine emission.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore engines — [back to index](../index.md)
