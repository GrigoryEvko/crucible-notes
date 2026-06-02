# V5+ EmitX Absolute Bit Positions

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (`libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so`, BuildID `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 B, not stripped, `.text` VA == file offset). Other builds will differ.*

## Abstract

Jellyfish (v3) and Pufferfish (v4) build a bundle with a single monolithic encoder — `EncoderJf::EncodeBundleInternal` (`0x1e86c7c0`) and `EncoderPfTensorCore::EncodeBundleInternal` (`0x1e8c5c40`) — that takes one `Bundle` object and packs every slot inline. Every V5+ generation in this build (Viperfish/`vxc`+`vfc`, Ghostlite/`glc`, 6acc60406/`gfc`) **abandons that model entirely**. There is no `EncodeBundleInternal` for any V5+ codec. Instead the bundle is produced by a two-stage chain: upstream, an `xla::tpu::sparse_core::isa_emitter::EmitX` proto-template populates a typed proto sub-message and sets a present bit; downstream, a per-slot `<Slot>Encoder::Encode` codec reads that proto and writes each field to a **fixed absolute bit position** in the flat bundle byte buffer via one universal bit-granular packer, `BitCopy(dst, dst_bit, src, src_bit, nbits)` (`0x1fa0a900`). This page consolidates those absolute bit positions for the sequencer, immediate, and predicate slots that the per-generation bundle pages defer to it.

The structural consequence is that the LLVM-MC `InstBits` table — which on a stock LLVM backend holds the fixed opcode bits of each instruction — is **all zero on disk for every V5+ generation**. There are no fixed instruction bits in a static table; the entire bundle layout is a flat list of *(absolute-bit, width)* triples distributed across roughly two hundred per-op `BitCopy` helpers and orchestrated by a single `EncodeBundle` dispatcher. The set of per-slot `BitCopy` offsets *is* the generation's effective `kIsaTable`. A reimplementer who looks for a static `InstBits` array, or for `EncoderVf::EncodeBundleInternal`, will find neither.

The page is organized by the two encode stages, then by slot. The opening `BitCopy` and `EmitImmediate` sections establish the primitive and its proto-field convention; the immediate, sequencer, and predicate sections give the absolute *(dst_bit, width)* maps per generation; the MXU/result/EUP section consolidates the compute-slot positions; the closing per-(slot, gen) table is the single reference the [Viperfish](bundle-vf-64b.md), [Ghostlite](bundle-gl.md), and [6acc60406](bundle-gf.md) bundle pages cite for the deferred fields.

For reimplementation, the contract is:

- The two-stage chain: `EmitX` proto population (Stage 1) → `<Slot>Encoder::Encode` `BitCopy` (Stage 2). No monolithic `EncodeBundleInternal`.
- The `BitCopy(dst, dst_bit, src, src_bit, nbits)` calling convention and bit-exact, LSB-first semantics.
- The `EmitImmediate` proto-slot map (which of 6 immediate slots a value lands in, and its present bit).
- The absolute *(dst_bit, width)* for the sequencer (branch/call/LCC) discriminator, dest/x-target sreg, and predication fields, per generation, for the 32-byte SCS bundle and the 64-byte TC bundle.
- The per-generation absolute-bit deltas: Viperfish baseline → Ghostlite +3-bit TC shift → 6acc60406 (its own layout, dedicated dual-predicate slot, 2-bit per-slot selector).

| | |
|---|---|
| **Universal packer** | `BitCopy(void* dst, int dst_bit, const void* src, int src_bit, int nbits)` @ `0x1fa0a900` (mangled `_Z7BitCopyPviPKvii`) |
| **Calling convention** | `dst`=`rdi`, `dst_bit`=`esi`, `src`=`rdx`, `src_bit`=`ecx`, `nbits`=`r8d` (System V); bit-granular, LSB-first |
| **Stage-1 emitter namespace** | `xla::tpu::sparse_core::isa_emitter::EmitX<…>` (`EmitImmediate`, `EmitBranchOp`, `EmitCallOp`, `EmitPredicationToSlot`) |
| **Stage-2 codec namespace** | `asic_sw::deepsea::{vxc,vxc::vfc,gxc::glc,gxc::gfc}::isa::<Slot>Encoder::Encode` |
| **Orchestrator** | `EncodeBundle` `0x1e838cc0` (6acc60406 codec; TC worker `0x1d371540`), `EncoderGlTensorCore::EncodeBundle` `0x1d331d00` (Ghostlite) |
| **SCS bundle** | 32 B / 256 bit; branch/call offset (imm slot 0) at bit 67, all V5+ gens |
| **TC bundle** | 64 B / 512 bit; branch/call offset (imm slot 0) at bit 430 (`vxc`) / 433 (`glc`) / 423 (`gfc`) |
| **`InstBits` table** | `0x3366d90` — all zero, no relocations, for every V5+ gen |

---

## The Two-Stage Encode Chain

### Purpose

Separate *what an instruction means* (a typed proto sub-message, generation-independent) from *where its bits go* (a per-generation `BitCopy` offset map). Stage 1 runs in the front-half emitter and produces a `*Bundle` proto with a populated sub-message per occupied slot. Stage 2 runs in the codec and serializes that proto into the wire bundle. The split is why the same LLO op encodes to three different bit layouts across Viperfish, Ghostlite, and 6acc60406 with no change to the front-end emitter logic — only the codec's offset literals differ.

### Entry Point

```text
LLO MCInst
  └─ isa_emitter::EmitX<Slot, OpKind>      ── Stage 1: populate proto sub-message
       │   sets present bit, writes operand fields
       ▼
  <gen> proto bundle (SparseCoreScsBundle / TensorCoreBundle / …)
       │
  EncodeBundle  0x1e838cc0  (dispatch on TpuSequencerType)
       ├─ case 0 → TensorCoreCodecBase<…>   → TC worker 0x1d371540
       ├─ case 3 → SparseCoreScsCodecBase<…> → EncoderBase::EncodeBundle
       └─ case 5 → SparseCoreTecCodecBase<…> → EncoderBase::EncodeBundle
                       │   walk per-slot Encoders in template-arg order
                       ▼
  <Slot>Encoder::Encode                    ── Stage 2: BitCopy fields → bundle bytes
       └─ BitCopy(buf, dst_bit, &field, 0, width)   0x1fa0a900
```

### Algorithm

Stage 1, immediate population, confirmed from the demangled `isa_emitter::EmitImmediate<glc::isa::SparseCoreImmediates>` body (`0x139f7060`):

```c
function EmitImmediate(slot_index, value, immediates_msg):    // 0x139f7060
    if value >= 0x100000:                                     // llvm::isUInt<20> RET_CHECK
        return Error("isa_emitter_base.h:587")                //   (signed 20-bit offsets are pre-masked)
    switch slot_index:                                        // jump on slot 0..5
        case 0: field = msg+0x18; present = 0x01; break       //   imm_0()
        case 1: field = msg+0x1c; present = 0x02; break       //   imm_1()
        case 2: field = msg+0x20; present = 0x04; break       //   imm_2()
        case 3: field = msg+0x24; present = 0x08; break       //   imm_3()
        case 4: field = msg+0x28; present = 0x10; break       //   imm_4()
        case 5: field = msg+0x2c; present = 0x20; break       //   imm_5()
        default: return Error("Invalid immediate: <n>")
    if (msg+0x10 & present) && *field != value:               // RET_CHECK: re-set must match
        return Error("imm_N() == value")
    *field = value
    msg+0x10 |= present                                       // set present bit at msg+0x10
    return Ok
```

Stage 2, the per-slot encoder, reads each populated sub-message and emits a sequence of `BitCopy` calls. The encoder body is a `switch` on the proto opcode at `proto+0x50`; each arm is one op family's field layout. The generic shape (from the immediates encoders) is:

```c
function <Slot>Encoder::Encode(this, proto, out_buf):
    scratch[0] = proto.imm_0                                  // stage value into 8-byte scratch
    BitCopy(out_buf, ABS_BIT_0, scratch, 0, WIDTH_0)          // place WIDTH_0 bits at ABS_BIT_0
    scratch[0] = proto.imm_1
    BitCopy(out_buf, ABS_BIT_1, scratch, 0, WIDTH_1)
    ...                                                       // one BitCopy per field
    return Ok
```

> **NOTE —** `EmitImmediate` does **not** know the absolute bit. It only chooses one of six proto fields and sets a present bit. The absolute bit is decided entirely in Stage 2 by the encoder's `BitCopy` literal. This is why the immediate-slot bit positions differ between SCS (bit 67) and TC (bit 430) for the *same* logical immediate slot 0 — same proto field, different encoder.

---

## BitCopy — The Universal Packer

### Purpose

One function packs every field of every V5+ bundle. There is no per-type serializer; each `<Slot>Encoder::Encode` is a flat list of `BitCopy` calls. Recovering the layout of any slot reduces to reading the `(dst_bit, nbits)` immediate pair preceding each `BitCopy` call in the named encoder.

### Calling Convention and Semantics

`BitCopy` is `_Z7BitCopyPviPKvii` — `BitCopy(void* dst, int dst_bit, const void* src, int src_bit, int nbits)`. Under System V the arguments land in `rdi`, `esi`, `rdx`, `ecx`, `r8d`. The decompiled body (`0x1fa0a900`) confirms bit-exact, LSB-first behavior:

```c
function BitCopy(dst, dst_bit, src, src_bit, nbits):          // 0x1fa0a900
    if nbits == 0: return                                     //   early-out, then vzeroupper
    dst_byte = dst_bit / 8                                    //   = dst_bit >> 3
    dst_off  = dst_bit & 7                                    //   bit-in-byte
    src_byte = src_bit / 8
    src_off  = src_bit & 7
    // copy nbits from src starting at (src_byte, src_off) into dst at (dst_byte, dst_off),
    // LSB-first, preserving the surrounding bits of any straddled dst byte
    // (vectorized inner loop when the run is >= 24 bits; scalar head/tail otherwise)
```

The body computes `dst_bit / 8` and `dst_bit & 7` exactly as written, masks the leading/trailing partial bytes so neighboring fields are untouched, and uses an AVX inner loop for runs of 24 bits or more. Bit 0 is the LSB of byte 0 of the bundle buffer.

> **QUIRK —** because every field is an independent `BitCopy` into a shared buffer, **field order does not matter for correctness** as long as windows do not overlap. The encoder writes the predication header first, then dispatches per-op, but a reimplementation may emit the calls in any order. Overlap *is* possible by design: on 6acc60406 SCS the 3-bit predicate selector and the 4-bit dual-predicate index both start at bit 187 (see [Predicate Slot](#predicate-slot-map)).

Every concrete `mov esi,<dst_bit>; mov r8d,<width>; call BitCopy` therefore reads directly as "place `width` bits at absolute bit `dst_bit`". All positions below were recovered from those immediates in the named encoders.

---

## Immediate Slot Map

### Purpose

The branch/call/sync target offset and all other immediates live in the immediate slots, not in any opcode field. A branch's 20-bit signed target offset is **immediate slot 0**; the opcode discriminator in the sequencer slot only distinguishes absolute/relative/call. This is the home that the per-generation bundle pages defer to this page.

### SCS Bundle (32 B / 256 bit)

`SparseCoreImmediatesEncoder::Encode` reads proto fields `a2[6]..a2[11]` (proto +0x18..+0x2c, i.e. `imm_0`..`imm_5`) and `BitCopy`s each at width 20. Confirmed byte-identical across all three generations:

| imm slot | proto field | dst_bit (hex) | dst_bit (dec) | width | Confidence |
|---|---|---|---|---|---|
| 0 (branch/call offset) | `+0x18` | `0x43` | 67 | 20 | CERTAIN |
| 1 | `+0x1c` | `0x2f` | 47 | 20 | CERTAIN |
| 2 | `+0x20` | `0x1b` | 27 | 20 | CERTAIN |
| 3 | `+0x24` | `0x07` | 7 | 20 | CERTAIN |
| 4 | `+0x28` | `0xd7` | 215 | 20 | HIGH |
| 5 | `+0x2c` | `0xc3` | 195 | 20 | HIGH |

Encoder addresses: `vfc` `0x1ee75ee0`, `glc` `0x1eb563c0`, `gfc` `0x1ecd1760`. Slots 4 and 5 (bits 215/195) appear in the full `SparseCoreImmediatesEncoder`; the SCS branch/call path uses only slots 0..3. A second class, `SparseCoreScalarImmediatesEncoder` (`gfc` `0x1eb5bd20`), packs only slots 0..3 at the same bits 67/47/27/7 — it is the encoder the `gfc` SCS codec template names (see [Correction](#corrections)).

### TensorCore Bundle (64 B / 512 bit)

`TensorCoreImmediatesEncoder::Encode` reads `imm_0`..`imm_5` (proto +0x18..+0x2c) and `BitCopy`s each at width 20. The per-generation bit positions, confirmed verbatim from the encoder `BitCopy` literals:

| imm slot | proto field | `vxc` (VF) | `glc` (GL) | `gfc` (GF) | width |
|---|---|---|---|---|---|
| 0 (branch/call offset) | `+0x18` | 430 (`0x1ae`) | 433 (`0x1b1`) | 423 (`0x1a7`) | 20 |
| 1 | `+0x1c` | 410 | 413 | 403 | 20 |
| 2 | `+0x20` | 390 | 393 | 383 | 20 |
| 3 | `+0x24` | 370 | 373 | 363 | 20 |
| 4 | `+0x28` | 350 | 353 | 343 | 20 |
| 5 | `+0x2c` | 330 | 333 | 323 | 20 |

Encoder addresses: `vxc` `0x1eebee40`, `glc` `0x1f20d520`, `gfc` `0x1f86de20`.

The per-generation delta is the central fact: **Ghostlite = Viperfish + 3 bits** (the TC scalar/sequencer/immediate region shifts +3 to make room for the 7→8-bit opcode widening), and **6acc60406 = Viperfish − 7 bits** for the immediate block (the immediate region moves *down* to make room for the wider scalar/predicate region above it, which on `gfc` includes the dedicated dual-predicate slot at bits 496..505).

> **GOTCHA —** the immediate slot index in `EmitImmediate` is *logical*, not a fixed bit. Slot 0 is bit 67 on SCS but bit 430/433/423 on TC. A reimplementation that hardcodes one bit for "immediate slot 0" will corrupt every cross-engine branch. Resolve the bit from the *(engine, generation)* pair, not the slot index alone.

---

## Sequencer Slot Map

### Purpose

The sequencer slot (`ScalarAlu0`) carries the branch/call discriminator, the call return-address (dest) sreg, the branch-by-register x-target sreg, and a predication header. The branch/call target offset is *not* here — it is in immediate slot 0. The sequencer slot is the position the per-gen bundle pages defer to this page; the TC sequencer in particular could not be bit-extracted from `InstBits` because `InstBits` is empty.

### Discriminator Model

Every `ScalarAlu0` op writes an opcode-HIGH field (width 6, family) and an opcode-LOW field (width 5, addressing discriminator). For all branch/call control ops opcode-HIGH = 0; the scalar-ALU compute ops carry their opcode in opcode-HIGH instead (e.g. `CompareIntegerEq` = 0x1e). The opcode-LOW discriminator values are uniform across SCS and TC, all V5+ generations:

| opcode-LOW | Op | Extra fields | Confidence |
|---|---|---|---|
| 4 | `BranchAbsolute` | offset → imm slot 0 | CERTAIN |
| 5 | `BranchRelative` | offset → imm slot 0 | CERTAIN |
| 6 | `CallAbsolute` | offset → imm slot 0; dest (link) sreg → dest field | CERTAIN |
| 7 | `CallRelative` | offset → imm slot 0; dest (link) sreg → dest field | CERTAIN |
| 4 (family field used) | `BranchSreg` | x-target sreg → x-target field | HIGH |
| 0x18 (24) | `BranchRelativeRotatingPreg` | rotating-preg index → dedicated field (`gfc` SCS only) | CERTAIN |

The branch offset range is signed 20-bit (`−0x80000..+0x7FFFF`) for absolute, relative, and call alike; the abs/rel distinction is purely the discriminator value. A return is not a dedicated op — it is a `BranchSreg` reading the link sreg.

### SCS Sequencer (`SparseCoreScalarAlu0Encoder::Encode`)

The encoder writes a common predication header, then dispatches `jmp *jt[proto+0x50]` (bound 0x56 = 86 entries) to a per-op helper. Confirmed from `glc` `0x1e9d2140` and the `BranchAbsolute` helper `0x1e9d67c0`; Viperfish (`vfc` `0x1ee873c0`) is byte-identical.

| field | dst_bit | hex | width | Source / written by | Confidence |
|---|---|---|---|---|---|
| predication reg index | 187 | `0xbb` | 4 | proto +0x20, main encoder | CERTAIN |
| predication inversion | 191 | `0xbf` | 1 | proto +0x18 (byte), main encoder | CERTAIN |
| opcode-HIGH / family | 181 | `0xb5` | 6 | per-op helper (=0 for branch/call) | CERTAIN |
| opcode-LOW / discriminator | 176 | `0xb0` | 5 | per-op helper (4/5/6/7/0x18) | CERTAIN |
| x-target / 2nd operand | 170 (`gfc`) / 176 (`vfc`/`glc`) | `0xaa` / `0xb0` | 6 / 5 | `BranchSreg`/aux | HIGH |
| call dest (return-addr) sreg | 165 | `0xa5` | 5 | `CallAbsolute`/`CallRelative` | CERTAIN |
| rotating-preg index (`gfc`) | 165 | `0xa5` | 4 | `BranchRelativeRotatingPreg` | CERTAIN |

On 6acc60406 (`gfc` encoder `0x1eb693c0`) the SCS predication narrows: a 3-bit selector at bit 187 (`0xbb`) + inversion at bit 190 (`0xbe`), overlaid with a 4-bit dual-predicate index also at bit 187 + inversion at bit 191. The `gfc` `BranchRelativeRotatingPreg` helper (`0x1eb6b9c0`) writes discriminator 24 at bit 176 (w5), opcode-HIGH 0 at bit 181 (w6), rotating-preg index at bit 165 (w4), and a 6-bit aux at bit 170.

### TC Sequencer (`TensorCoreScalarAlu0Encoder::Encode`)

The slot `InstBits` could not hold. Confirmed from `vxc` `0x1eecb900` (and helper `0x1eecf960`) and `gfc` `0x1f87b420`.

| field | `vxc` (VF) | `glc` (GL) | `gfc` (GF) | Confidence |
|---|---|---|---|---|
| predication reg index | bit 499 w4 | = `vxc` | — (2-bit selector) | CERTAIN |
| predication inversion | bit 503 w1 | = `vxc` | — | CERTAIN |
| predication 2-bit selector | — | — | bit 489 w2 | CERTAIN |
| opcode-HIGH / family | bit 493 w6 | = `vxc` | bit 483 w6 | CERTAIN |
| opcode-LOW / discriminator | bit 488 w5 | = `vxc` | bit 478 w5 | CERTAIN |
| x-target / 2nd operand | bit 482 w6 | = `vxc` | bit 472 w6 | CERTAIN |
| call dest (return-addr) sreg | bit 477 w5 | = `vxc` | bit 467 w5 | CERTAIN |

Ghostlite reuses the Viperfish TC sequencer offsets verbatim within the scalar slot (the +3 shift documented for Ghostlite applies to the immediate block, not these scalar-slot bits — the scalar fields already sit in the same window). 6acc60406's TC scalar slot is the widest: it adds the 6-bit operand at bit 472 and shrinks per-slot predication to a 2-bit selector at bit 489, with the actual 16-register predicate pool moved to the dedicated `TensorCorePredicates` slot.

> **CORRECTION (EMITX-1) —** the raw sequencer trace summarized the TC `vxc` x-target as "bit 488 w5". Decompilation of the `vxc` encoder body (`0x1eecb900`) shows that bit 488 (w5) is the opcode-LOW discriminator (shared with the `BranchSreg` x-target *value*), while the distinct secondary-operand / LCC-read aux field is a **6-bit** field at **bit 482** (`0x1da`). The 488-vs-482 distinction matters: a `BranchSreg` overwrites the discriminator window with the x() sreg, but an LCC read uses both the 5-bit discriminator at 488 and the 6-bit aux at 482.

---

## Predicate Slot Map

### Purpose

Pin the exact byte offset of the predicate field within each V5+ bundle slot — the position prior predicate-field analysis left open (it was "governed by `InstBits`", which is empty). The model differs between the Viperfish/Ghostlite per-slot scheme and the 6acc60406 dedicated dual-predicate slot.

### Viperfish / Ghostlite — Per-Slot 4+1 Field

Every populated functional slot carries its own predicate: a 4-bit register index plus a 1-bit inversion (the 2-bit extension of the `encodePredicateOperand` layout is the high end of the field, 0 in non-rotating code). At the top of the scalar slot:

| Slot | reg index | inversion | Encoder | Confidence |
|---|---|---|---|---|
| TC `ScalarAlu0` (`vxc`) | bit 499 w4 | bit 503 w1 | `0x1eecb900` | CERTAIN |
| SCS `ScalarAlu0` (`glc`/`vfc`) | bit 187 w4 | bit 191 w1 | `0x1e9d2140` / `0x1ee873c0` | CERTAIN |

### 6acc60406 — Dedicated Dual-Predicate Slot

`TensorCorePredicatesEncoder::Encode` (`gfc` `0x1f86e500`) writes two per-bundle predicates into the very top of the 64-byte TC bundle; each functional slot then carries only a 2-bit selector choosing among `{pred_0, pred_1, always, never}`:

| field | dst_bit | hex | width | proto src | Confidence |
|---|---|---|---|---|---|
| pred_0 reg | 501 | `0x1f5` | 4 | msg word | CERTAIN |
| pred_0 inversion | 505 | `0x1f9` | 1 | msg +0x20 (byte) | CERTAIN |
| pred_1 reg | 496 | `0x1f0` | 4 | msg word | CERTAIN |
| pred_1 inversion | 500 | `0x1f4` | 1 | msg +0x21 (byte) | CERTAIN |

The 16-register predicate pool (the `PredicationSlot` enum 0..15) is encoded into pred_0/pred_1; the per-slot 2-bit selector indexes the pool. The "predication overflow — both predicate slots already taken" condition is exactly the state where these two 4-bit `(reg, inversion)` entries at bits 496..505 are full. The exact value→`{pred_0,pred_1,always,never}` mapping of the 2-bit selector was not decoded (the selector's own jump table was not walked); the selector field offsets are confirmed (LOW confidence on the value semantics).

---

## Compute Slots — MXU, Result, EUP

### Purpose

The MXU matmul/push/latch (`VectorExtended`), the matres/EUP pop (`VectorResult`), and the transcendental push (VALU slot 3) come from the same `<Slot>Encoder::Encode` + `BitCopy` mechanism. They are documented in full on [MXU Slot](slot-mxu.md), [VPU Slot](slot-vpu.md), and [EUP/Transcendental Slot](slot-eup-transcendental.md); the absolute bit positions are consolidated here.

### MXU `VectorExtended` Slot

Two MXU slots (`VectorExtended0`, `VectorExtended1`) — one per physical matrix unit. The two slots **share the source-vreg (systolic-feed) region** but have opcode/control regions offset by a 25-bit slot stride on 6acc60406. The opcode field widens 7→8 bits across generations, mirroring the VALU slot. Confirmed from `gfc MatrixMultiplyBf16` `0x1f99a920`:

| field | `vxc` | `glc` | `gfc` VEx0 | `gfc` VEx1 | Confidence |
|---|---|---|---|---|---|
| MXU-id (unit) [proto +0x1c] | bit 64 w4 | bit 66 w4 | bit 70 w2 | bit 45 w2 | CERTAIN |
| opcode-HIGH | bit 57 w7 | bit 58 w8 | bit 62 w8 | bit 37 w8 | CERTAIN |
| data-format sub-disc | bit 51 w4 | bit 52 w4 | bit 57 w4 | bit 32 w4 | CERTAIN |
| done-gains/latch flag | bit 55 w2 | bit 56 w1 | bit 61 w1 | bit 36 w1 | CERTAIN |
| control (3-bit) | bit 48 w3 | bit 49 w3 | bit 54 w3 | bit 29 w3 | CERTAIN |
| primary operand | bit 180 w6 | bit 183 w6 | bit 47 w7 | bit 22 w7 | CERTAIN |
| src vregs (8 × 6-bit, `gfc`) | — | — | 156/276/287/243/254/210/221/177 | (same) | CERTAIN |
| opcode bound (#ops) | 0x66 (103) | 0x70 (113) | 0x54 (85) | 0x54 (85) | HIGH |

Encoder addresses: `vxc` `0x1efa0f60`, `glc` `0x1f32fd00`, `gfc` VEx0 `0x1f996940` / VEx1 `0x1f9d3800`. The weight latch is `LoadMatrixRegister{Gmr,Lmr}{Msra,Msrb}` (opcode-HIGH 0x37 on `gfc`, `0x1f9a04a0`); the moving-operand push is `PushMatrix{fmt}` (opcode-HIGH 0xe). The 8 source-vreg fields being byte-identical between VEx0 and VEx1 is the encoding statement that both MXUs draw the same vector read ports.

### `VectorResult` Slot (matres pop / EUP pop)

`VectorResult0Encoder::Encode` reads a result-type discriminator (proto +0x1c), dispatches `jmp *jt[proto+0x50]`, sets the per-result-type sub-message present tag, then a common tail `BitCopy`s the dest vreg. Confirmed from `gfc` `0x1fa01820`:

| field | `vxc` | `glc` | `gfc` | Confidence |
|---|---|---|---|---|
| result-type discriminator | bit 24 w4 | bit 24 w4 | bit 20 w2 | CERTAIN |
| dest vreg | bit 14 w6 | bit 14 w6 | bit 11 w6 | CERTAIN |
| PopMxu accum-mode/format | (per +0x1c) | (per +0x1c) | bit 323 w8 | CERTAIN |
| result-opcode bound | 0x8 (9) | 0x8 (9) | 0x7 (8) | HIGH |

The matres-pop opcode is 6 (`PopMxuResult`) and the EUP-pop opcode is 7 (`PopEupResult`) on all generations. Ghostlite adds `PopAddMxu01Result` (fused matres+accumulate, K>128 multi-pass); Viperfish adds `PopCcrfResult` (scalar/CRF pop). The result slot's own predication field accessor is `TensorCoreVectorResult1PredicationField::GetConcatenatedValue` (`gfc` `0x1fa02520`); its exact bit was not individually walked (the adjacent result-mode/format fields at bits 17..21 are decoded).

### EUP / Transcendental Push — VALU Slot 3

On all V5+ generations the transcendental push is a VALU slot-3 (`Alu3`) op, not a `VectorExtended` op — confirmed by the EUP helpers existing only in the `Alu3` set. The VALU opcode field selects the EUP-push family (value 0x0); a 5-bit EUP-function selector picks the transcendental. Confirmed from `gfc F32Tanh` `0x1f96ae40`:

| field | `vxc` | `glc` | `gfc` | Confidence |
|---|---|---|---|---|
| VALU opcode (EUP-push family = 0x0) | bit 197 w7 | bit 194 w8 | bit 194 w8 | CERTAIN |
| EUP-function selector | bit 186 w5 | bit 183 w5 | bit 183 w5 | CERTAIN |
| src vreg | (slot-3) | bit 188 w6 | bit 188 w6 | CERTAIN |

The `gfc` 5-bit function selector value map (VALU op = 0x0, selector @ bit 183), confirmed per-helper:

| function | F32 selector | Bf16 selector |
|---|---|---|
| `Erf` | 0x0e | 0x0f |
| `ReciprocalSqrt` | 0x10 | 0x0c |
| `PowTwo` (2^x) | 0x11 | 0x19 |
| `LogTwo` (log2) | 0x12 | 0x1a |
| `Tanh` | 0x13 | 0x1b |
| `ShiftedSigmoid` | 0x14 | 0x1c |
| `Reciprocal` | 0x15 | 0x1d |
| `Sinq` (sin) | 0x17 | 0x1e |
| `Cosq` (cos) | 0x18 | 0x1f |

The push-pop protocol is bit-exact: PUSH in bundle N is the VALU slot-3 op above; POP in bundle N+k is a `VectorResult` op with result-opcode 7 (`PopEupResult`), dest vreg at bit 11 (`gfc`). The XLU is single-issue, so only VALU slot 3 sources the EUP push.

---

## Per-(Slot, Generation) Absolute-Bit-Position Table

The consolidated reference cited by the per-generation bundle pages. All positions are `bit <n> w<width>`; bit 0 = LSB of byte 0.

### SCS Bundle (32 B / 256 bit)

| slot / field | `vfc` (VF) | `glc` (GL) | `gfc` (GF) |
|---|---|---|---|
| branch/call offset (imm 0) | 67 w20 | 67 w20 | 67 w20 |
| imm slot 1 | 47 w20 | 47 w20 | 47 w20 |
| imm slot 2 | 27 w20 | 27 w20 | 27 w20 |
| imm slot 3 | 7 w20 | 7 w20 | 7 w20 |
| seq opcode-HIGH | 181 w6 | 181 w6 | 181 w6 |
| seq opcode-LOW (discriminator) | 176 w5 | 176 w5 | 176 w5 |
| seq x-target / 2nd operand | 176 w5 | 176 w5 | 170 w6 |
| seq call dest sreg | 165 w5 | 165 w5 | 165 w5 |
| seq predicate reg | 187 w4 | 187 w4 | 187 w3 (selector) / w4 (dual) |
| seq predicate inversion | 191 w1 | 191 w1 | 190 w1 |
| rotating-preg index | — | — | 165 w4 |

### TensorCore Bundle (64 B / 512 bit)

| slot / field | `vxc` (VF) | `glc` (GL) | `gfc` (GF) |
|---|---|---|---|
| branch/call offset (imm 0) | 430 w20 | 433 w20 | 423 w20 |
| imm slots 1..5 | 410/390/370/350/330 | 413/393/373/353/333 | 403/383/363/343/323 |
| seq opcode-HIGH | 493 w6 | 493 w6 | 483 w6 |
| seq opcode-LOW (discriminator) | 488 w5 | 488 w5 | 478 w5 |
| seq x-target / 2nd operand (aux) | 482 w6 | 482 w6 | 472 w6 |
| seq call dest sreg | 477 w5 | 477 w5 | 467 w5 |
| seq per-slot predicate | reg 499 w4 + inv 503 w1 | = `vxc` | 2-bit selector @ 489 |
| dual predicate pred_0 | — | — | reg 501 w4 + inv 505 w1 |
| dual predicate pred_1 | — | — | reg 496 w4 + inv 500 w1 |
| MXU opcode-HIGH (VEx0) | 57 w7 | 58 w8 | 62 w8 |
| MXU data-format sub-disc | 51 w4 | 52 w4 | 57 w4 |
| MXU-id (unit) | 64 w4 | 66 w4 | 70 w2 |
| VectorResult discriminator | 24 w4 | 24 w4 | 20 w2 |
| VectorResult dest vreg | 14 w6 | 14 w6 | 11 w6 |
| EUP-push VALU opcode (Alu3) | 197 w7 | 194 w8 | 194 w8 |
| EUP-function selector | 186 w5 | 183 w5 | 183 w5 |

The generation deltas, in one line: **Ghostlite shifts the TC immediate block +3 bits** (bit 430 → 433) to absorb the 7→8-bit opcode widening; **6acc60406 shifts the TC immediate block −7 bits** (bit 430 → 423) and the scalar/sequencer region down to clear room for the dedicated dual-predicate slot at bits 496..505 and the wider scalar operand at bit 472.

---

## Delay-Slot and Loop Notes

Two fields a stock LLVM-MC mental model expects to be encoded are *not* in-bundle bit fields on V5+:

- **Delay-slot count.** No V5+ branch/call helper (Ghostlite, Viperfish, or 6acc60406) emits a `delay_slots` `BitCopy`. The branch/call helpers write only `{opcode-HIGH, opcode-LOW, offset (imm 0), dest}`. The delay-slot count is a bundle-packer pad count (empty bundles appended *after* the branch), gated by the LLVM-MC verifier bound (`delay_slots <= 5`) on the packer, not an encoded slot field.
- **Hardware-loop length.** V5+ has no hardware-loop setup slot bit field. A loop is the LCC hardware counter read (`ReadRegisterLccLow`/`High`, the sequencer-slot opcode at bit 181 + dst at bit 176) feeding a conditional `BranchRelative`. The "loop counter" is a register, not an encoded loop-length field.

> **GOTCHA —** a reimplementation that allocates a 3-bit delay-slot field inside the bundle (as a `BarnaCore`-style v4 layout would have) will desynchronize every subsequent field. On V5+, after the branch's dest field the next bits belong to the next slot, not to a delay count.

---

## Corrections

> **CORRECTION (EMITX-2) —** the raw immediates trace attributed the `gfc` SCS `SparseCoreImmediatesEncoder::Encode` to `0x1eb5bd20`. The function at `0x1eb5bd20` is `SparseCoreScalarImmediatesEncoder::Encode` (a distinct class that packs only slots 0..3, all at bits 67/47/27/7); the full `gfc SparseCoreImmediatesEncoder::Encode` is at `0x1ecd1760` (slots 0..5, including bits 215/195). The `gfc` SCS codec template (confirmed in the `EncodeBundle` `0x1e838cc0` `case 3` `SparseCoreScsCodecBase<…>` argument list) names `SparseCoreScalarImmediatesEncoder` — so `0x1eb5bd20` is the one actually invoked for the SCS branch path. The branch/call offset (imm slot 0 = bit 67) is unaffected; both encoders agree.

> **CORRECTION (EMITX-3) —** `EncodeBundle` `0x1e838cc0` is more precisely the **6acc60406** codec dispatcher: its `default` arm reports "EncodeBundle not implemented for sequencer type" from `learning/45eac/tpu/runtime/utility/tpu_codec_6acc60406.cc`, and its three live cases construct `gfc::isa::{TensorCore,SparseCoreScs,SparseCoreTec}CodecBase<…>`. Ghostlite uses its own `EncoderGlTensorCore::EncodeBundle` (`0x1d331d00`). The raw's "v5 codec" label is correct in spirit (the slot-walk mechanism is shared) but the symbol is generation-specific.

---

## Cross-References

- [IsaEmitter Registry](isa-emitter-registry.md) — the `isa_emitter::EmitX` template family and per-generation codec registration that produce the protos this page serializes
- [Viperfish 64B Bundle](bundle-vf-64b.md) — the `vxc`/`vfc` slot map; cites this page for the sequencer/immediate/predicate positions
- [Ghostlite Bundle](bundle-gl.md) — the `glc` slot map and the +3-bit TC shift
- [6acc60406 Bundle](bundle-gf.md) — the `gfc` slot map, dedicated dual-predicate slot, and 2-bit per-slot selector
- [MC-Emitter](mc-emitter.md) — the MCInst stream that feeds Stage 1 `EmitX`
- [Record Format](record-format.md) — the on-disk record framing around the encoded bundle bytes
- [Sequencer Slot](slot-sequencer.md) — branch/call/LCC op semantics for the discriminator values mapped here
- [Predicate Slot](slot-predicate.md) — the predication model whose absolute offsets this page pins
- [MXU Slot](slot-mxu.md) / [VPU Slot](slot-vpu.md) / [EUP/Transcendental Slot](slot-eup-transcendental.md) — compute-slot semantics for the consolidated MXU/result/EUP positions
- [InstBits Master DB](instbits-master-db.md) — the all-zero V5+ `InstBits` table that confirms the bits live in the emitter path
