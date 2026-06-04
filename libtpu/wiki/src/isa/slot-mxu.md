# MXU Slot

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped). Other versions differ.*

## Abstract

The MXU slot is the bundle field that drives the systolic matrix-multiply array — the single most important slot in the TensorCore ISA, because it is the only path to the TPU's flagship dense-linear-algebra throughput. It is not one opcode but a small instruction *family* threaded through one or two physical bundle slots: a **latch** (load the stationary weight matrix into the array), a **matpush** (stage the moving operand), a **matmul** (clock one systolic step), and a **matres / result-pop** (drain the accumulator). Across five hardware generations this family is encoded five different ways, but the underlying contract — weight-stationary systolic multiply, fed by a shared vector-register operand pool, drained through a separate result slot — is invariant.

The slot has two structurally distinct encoder lineages. On **Jellyfish (v2)** and **Dragonfish (v3)** the MXU is one `VectorExtended` slot whose 6-bit opcode is packed by `shl`/`and`/`or` arithmetic into qword 0 of a scratch struct and decoded by a two-level jump table. From **Pufferfish (v4)** onward the encoder becomes the generic `TensorCoreCodecBase` template: two `VectorExtended` slots (one control region per physical MXU), each field placed by a universal `BitCopy(dst, dst_bit, src, 0, width)` call, and decoded by a linear `Opcode::Matches` sweep. The two MXU control regions are a fixed **−N-bit twin** (−20 on PF/VF, −21 on Ghostlite, −25 on 6acc60406), packed over one shared 8×6-bit operand pool. This page covers the slot's *semantics* across all gens; the absolute bit positions live on the per-gen bundle pages.

If you have read the LLVM NVPTX backend, the closest analog is `wgmma`/`mma.sync`: a single machine op whose operand registers, accumulation half, and data format are all encoded fields, and whose result is drained separately. The TPU differs in being explicitly multi-instruction — the latch, the push, the multiply, and the pop are *distinct bundle slots* the compiler schedules, not one fused intrinsic — and in being weight-stationary, so the latch amortizes across many matmul steps.

For reimplementation, the contract is:

- The MXU op family and its mapping to physical bundle slots: latch + matprep + matmul share the `VectorExtended` slot; matres uses `VectorResult`.
- The per-gen field layout of the `VectorExtended` slot: opcode, data-format / dtype sub-discriminator, MXU-id, the done-gains / transpose / target control bits, and the 5-bit (v2/v3) / 4-bit (v5+) predicate.
- The opcode-encoding rules: how the JF emitter maps `LloOpcode × DoneWithGainsMode` to a 6-bit opcode, and how the v5+ codec splits a 7/8-bit opcode field into `{matmul, PushGains/latch, transpose}` families with per-dtype values.
- The −N-bit twin geometry and the shared-operand-pool model that lets two MXU control regions coexist in one bundle.

| | |
|---|---|
| **Op family (LLO)** | `vlatch`/`vlatchi` (latch), `vmatprep.subr`/`.mubr` (matpush), `vmatmul`(`.high`/`.low`/`.mubr`/`.msk`), `vmatres`(`.add`) |
| **JF LloOpcode values** | matprep.subr `0x97/0x98`, matprep.mubr `0x99/0x9a`, matmul `0x9b`, matmul.mubr `0x9c`/`0xa0`, matmul.high `0x9d`, matmul.low `0x9e`, matres `0x152` |
| **JF slot home** | latch/matprep/matmul → `VectorExtended` (slot_mask `0x080`, proto `+0x50`); matres → `VectorResult` (`0x100`, `+0x58`) |
| **JF VE field map** | opcode @ abs 29..34 (6b), mxu-id @ 27..28 (2b), predicate @ 35 (5b) — `EncodeVectorExtendedInstruction` @ `0x1e869f00` |
| **v5+ MXU slots** | two `VectorExtended` control regions (one per physical MXU), `−N`-bit twin over one shared operand pool |
| **Systolic array** | 128 × 128 (JF–VF) / 256 × 256 (Ghostlite, 6acc60406) weight-stationary; fed by 8×128 / 4×256 matpush tiles |
| **Empty-slot mark** | predicate `kNeverExecute = 31` (`0xB834CFC`) prefilled into the slot |

---

## The MXU Op Family

### Purpose

The MXU is a weight-stationary systolic array. A full dense matmul `C = A·B` is not one instruction but a *sequence* of LLO ops that the compiler emits and the bundle packer schedules: latch the stationary weight tile, push the moving-operand tile, clock the array forward, and drain the result FIFO. The op family is the vocabulary for that sequence.

### Op Roster

The Jellyfish op set was read directly from the per-op `LloOpcodeIsVector*` classifier functions, each a tiny `(opcode − base) < n` or mask test. The classifier values are CERTAIN — they are arithmetic constants in the binary, not inferred.

| LLO mnemonic | `LloOpcode` | Slot | Role | Confidence |
|---|---|---|---|---|
| `vmatprep.subr` (`+.msk`) | `0x97`/`0x98` | VectorExtended | push the **moving** operand, sub-row form | CERTAIN |
| `vmatprep.mubr` (`+.msk`) | `0x99`/`0x9a` | VectorExtended | push the **moving** operand, block-row form | CERTAIN |
| `vmatmul` | `0x9b` | VectorExtended | one systolic step | CERTAIN |
| `vmatmul.mubr` (`+.msk`) | `0x9c`/`0xa0` | VectorExtended | conv block-row matmul | CERTAIN |
| `vmatmul.high` | `0x9d` | VectorExtended | high-half accumulator step | CERTAIN |
| `vmatmul.low` | `0x9e` | VectorExtended | low-half accumulator step | CERTAIN |
| `vlatch` / `vlatchi` | (latch path) | VectorExtended | latch **stationary** weights into the array | CERTAIN |
| `vmatres` (`+.add`) | `0x152` | VectorResult | drain the result FIFO (`.add` = accumulate) | CERTAIN |

The classifier arithmetic, verified in the decompile:

```c
// platforms_deepsea::jellyfish — the per-op LloOpcode classifiers
LloOpcodeIsVectorMatprepSubr(op):  return (uint16)(op - 151) < 2;   // {0x97, 0x98}   @ 0x1d60c400
LloOpcodeIsVectorMatprepMubr(op):  return (uint16)(op - 153) < 2;   // {0x99, 0x9a}   @ 0x1d60c3e0
LloOpcodeIsVectorMatmulMubr(op):   return ((op - 156) & 0xFFFB)==0; // {0x9c, 0xa0}   @ 0x1d60c3c0
// matmul=0x9b, matmul.high=0x9d, matmul.low=0x9e read from the EmitVectorMatmul dispatch;
// matres=0x152 from EmitVectorMatres @ 0x140b9600 (cmp 0x152).
```

The same matrix ops register as MLIR LLO dialect operations — `VectorMatmulOp`, `VectorMatmulMubrOp`, `VectorMatprepSubrOp`, `VectorMatprepMubrOp`, `VectorMatresOp`, `VectorLatchOp`, `VectorLatchIOp`, `VectorDoneWithGainsOp`, `VectorMoveEvenAccLowOp` — confirming the dialect surface matches the encoded opcodes.

> **NOTE —** the contiguous `0x97..0xa0` block holds the whole matprep/matmul family, but `matres` lives far away at `0x152`. The two regions reflect the two physical slots: matprep/matmul/latch feed the array (`VectorExtended`); matres drains it (`VectorResult`). A reimplementation that assumes a single contiguous MXU opcode block will mis-classify the result-pop.

### Operand Roles — Stationary vs Moving

The array has two operands, fed by two different ops:

- **Stationary (weights / gains)** — loaded by the **latch** ops (`vlatch`/`vlatchi`, the v5+ `LoadMatrixRegister*` / `PushGains*` family). The stationary operand stays resident in the array across many matmul steps until re-latched. *How* it is loaded — transpose, packing, dtype staging — is the `GainLatchMode` enum (v2/v3) or the per-dtype `PushGains<fmt>` / `Pushmatrix<fmt>` opcode (v5+).
- **Moving (activations / multiplicand)** — staged by the **matprep** ops (`vmatprep.subr` = sub-row, `.mubr` = multiplicand-block-row). The moving operand is staged into a matrix-staging register, `MATPUSH_TARGET_MSRA` or `MATPUSH_TARGET_MSRB` (both strings present in `.rodata`), then clocked through the array.

The matpush tile granularity is fixed by the hardware perf-counter help string, byte-exact in the binary: *"This counts the number of 8x128 or 4x256 matrices that are pushed to MXUn by a vmatpush instruction."* So each push delivers an **8×128** (sublanes × lanes) or **4×256** tile; sixteen 8×128 tiles fill a 128×128 array (the JF–VF geometry — Ghostlite/6acc60406 widen the array to 256×256, see [Systolic-Array Geometry](#systolic-array-geometry)).

---

## Jellyfish (v2) — The Direct-Pack VectorExtended Slot

### Slot Assignment

Jellyfish has **one** MXU and one MXU-bearing slot per bundle. The MXU ops do not have a dedicated slot; they reuse the vector pipeline's `VectorExtended` and `VectorResult` slots. `JellyfishTarget::NumVexSlots()` (`0x1d4912c0`) returns **1** — exactly one `VectorExtended` slot per bundle; `NumVsSlots()` (`0x1d4912a0`) returns **3** (the three vector-source ports vs0/vs1/vs2 that feed it).

| MXU op | Bundle slot | `slot_mask` bit | proto offset |
|---|---|---|---|
| latch / matprep / matmul | `VectorExtended` | `0x080` | `proto+0x50` |
| matres | `VectorResult` | `0x100` | `proto+0x58` |

The emitter dispatch:

```text
JellyfishEmitter::EmitVectorMatmul   @ 0x140b92c0  ── matmul (all forms)
JellyfishEmitter::EmitVectorLatch    @ 0x140b8c20  ── latch + matprep
JellyfishEmitter::EmitVectorMatres   @ 0x140b9600  ── matres (op 0x152)
  ├─ EmitVectorExtendedInstruction   @ 0x140b4f80  ── builds the VE proto submessage
  ├─ EmitVectorResultInstruction     @ 0x140b53e0  ── builds the VR proto submessage
  ├─ AddMxuNumToVectorExtended       @ 0x140b8da0  ── stamps the 2-bit MXU id (proto +0x70)
  ├─ AddMxuNumToVectorResult         @ 0x140b9680
  └─ CheckMxuNum                     @ 0x140b9780  ── JF: mxu must == 0; Dragonfish: free
```

### VectorExtended Field Layout

`EncoderJf::EncodeVectorExtendedInstruction` (`0x1e869f00`) ORs the fields into the 8-byte little-endian word at struct byte `0x0C` (= absolute bundle bit 96, by the [12-byte-strip law](bundle-jf-41b.md)). Because struct byte `0x0C` is bit 96, the qword-0 shifts read directly as their absolute bundle bit positions. The field positions are verified byte-for-byte from the decompiled masks:

```c
// EncoderJf::EncodeVectorExtendedInstruction @ 0x1e869f00 (verified)
word  = struct[0x0C];
word  = (predicate & 0x1F) << 35 | word & 0xFFFFFF07FFFFFFFF;   // predicate @ abs 35
word  = (mxu_id    & 0x03) << 27 | word & 0xFFFFFFFFE7FFFFFF;   // mxu-id    @ abs 27..28
// opcode field cleared by 0xFFFFFFF81FFFFFFF (bits 29..34); each VEopcode value ORed/added in:
//   opcode 0 → word &= ~mask;            opcode 1 → | 0x20000000;   opcode 2 → + 0x40000000;
//   opcode 3 → + 0x60000000;             ... opcode 0x22 → + 0x540000000   (35-case jump table)
```

| Field | Source | abs bits | Width | Confidence |
|---|---|---|---|---|
| predicate | `EncodePredication & 0x1F` | 35 | 5 | CONFIRMED |
| opcode (`VectorExtendedOpcode`) | 35-case jump table | 29..34 | 6 | CONFIRMED |
| mxu-id (unit) | proto `+0x64 & 3` | 27..28 | 2 | CONFIRMED |
| operand vregs | proto `+0x6c` etc. | sub-mode dependent | 5 each | HIGH |

The opcode clear-mask `0xFFFFFFF81FFFFFFF` is the *same* mask the decoder (`DecoderJf::DecodeVectorExtendedSlot` @ `0x1e854000`) uses to extract the 6-bit opcode at abs 29..34, so encode and decode agree bit-for-bit — this raises the opcode / mxu-id / predicate fields to CERTAIN-grade cross-confirmation. See [Jellyfish 41B Bundle](bundle-jf-41b.md#vector-extended--mxu-struct-0x0c--encodevectorextendedinstruction--0x1e869f00) for the absolute positions in context.

The 6-bit `VectorExtendedOpcode` space partitions into three families (classifier ranges from `ProtoUtils::IsMatrixMultiply` @ `0x1e875b20`, `IsPushGains` @ `0x1e875b80`, `IsTranspose` @ `0x1e875b40`, `IsRpu` @ `0x1e875b60`):

| VEopcode range | Classifier | Family |
|---|---|---|
| `0..6` | `IsMatrixMultiply` (op < 7) | matmul (op 3 = staging-only; `VectorExtendedUsesData` @ `0x1e876160` returns op != 3) |
| `7..12` | `IsPushGains` (7..12) | weight-latch (the `GainLatchMode 0..5` range) |
| `17,18` | `IsTranspose` | matrix transpose |
| `17..34` | `IsRpu` | reduce / permute / transpose family |

### Matmul Opcode Encoding

`EmitVectorMatmul` (`0x140b92c0`) translates the matmul `LloOpcode` into a small `VectorExtendedOpcode`. The selector is the **`DoneWithGainsMode`** argument, *not* the data format — the dispatch tests `dwg_mode == kTransposed` (value 2). The decompiled `switch` is unambiguous:

```c
// JellyfishEmitter::EmitVectorMatmul @ 0x140b92c0 (a2 = LloOpcode, a5 = DoneWithGainsMode)
switch (a2):
  case 0x9b:  veopcode = 4 * (dwg != 2) + 0;  break;   // matmul        → 0 if transposed, else 4
  case 0x9e:  veopcode = 4 * (dwg != 2) + 1;  break;   // matmul.low    → 1 if transposed, else 5
  case 0x9d:  veopcode = 4 * (dwg != 2) + 2;  break;   // matmul.high   → 2 if transposed, else 6
  default:    NoteError("unhandled LLO opcode for matrix multiply: %s");
EmitVectorExtendedInstruction(veopcode, ...);
AddMxuNumToVectorExtended(mxu_num);
```

| `LloOpcode` | `dwg == kTransposed` (2) | else |
|---|---|---|
| `vmatmul` (`0x9b`) | VEopcode 0 | VEopcode 4 |
| `vmatmul.low` (`0x9e`) | VEopcode 1 | VEopcode 5 |
| `vmatmul.high` (`0x9d`) | VEopcode 2 | VEopcode 6 |

> **NOTE —** the `cmp $2` selector in `EmitVectorMatmul` tests the **`DoneWithGainsMode`** (param `a5`), where value 2 is `DoneWithGainsMode::kTransposed` — not `MatmulDataFormat`. The same function carries the assertion `dwg_mode != DoneWithGainsMode::kTransposed` guarded by "JF/DF have only one GSF" (`jellyfish_emitter.cc:1710`). `MatmulDataFormat` is a separate parameter and does not steer this opcode dispatch on Jellyfish.

### Latch Opcode Encoding

`EmitVectorLatch` (`0x140b8c20`) carries a `GainLatchMode` argument bounded `0..5` (`if (mode >= 6u)` is the fatal path) and indexes a 6-entry `.rodata` table at VA `0xaef42ac` to get the latch `VectorExtendedOpcode`. The table bytes were read directly from the ELF and are byte-exact `{7, 10, 9, 12, 8, 11}`:

```c
// JellyfishEmitter::EmitVectorLatch @ 0x140b8c20 (verified)
if (gain_latch_mode >= 6u) fatal(...);            // GainLatchMode must be 0..5
veopcode = dword_AEF42AC[gain_latch_mode];        // {7,10,9,12,8,11} read from ELF off 0xaef42ac
EmitVectorExtendedInstruction(veopcode, vreg, ...);
```

| `GainLatchMode` | VEopcode | | `GainLatchMode` | VEopcode |
|---|---|---|---|---|
| 0 | `0x07` | | 3 | `0x0c` |
| 1 | `0x0a` | | 4 | `0x08` |
| 2 | `0x09` | | 5 | `0x0b` |

These six VEopcodes land squarely in the `IsPushGains` range (7..12). The full cross-generation `GainLatchMode` enum is far richer (`NO_XPOSE_F32`/`HI_F32`/`LOW_F32`, `S4`/`S8`/`U4`/`U8`, the soft/nibble/packed/FP8 staging modes, plus `XPOSE_*` transposed counterparts); v3 uses only the small `{F32, HI_F32, LOW_F32, S8/U8, …}` subset the 6-entry table covers. The richer modes became named opcode families on later gens (see below).

### VectorResult (matres) Field Layout

`EncoderJf::EncodeVectorResultInstruction` (`0x1e865ae0`) shares the same word at struct `0x0C` — `VectorExtended` and `VectorResult` coexist in one bundle on disjoint bit ranges. Decoded from the encoder masks:

| Field | Source | abs bits | Width | Confidence |
|---|---|---|---|---|
| predicate | `& 0x1F` | 22..26 | 5 | CONFIRMED |
| result type / format | proto `+0x40` | 20..21 | 2 | HIGH |
| result mode | proto `+0x44` | 18..19 | 2 | HIGH |
| dest vreg | proto `+0x48` | mode-dependent | 5 | HIGH |

The 2-bit result-mode (proto `+0x44`, value 0/1/2) selects which MRF/MSR FIFO the result drains from and gates the destination-vreg shift; `matres.add` (a distinct LLO opcode) accumulates into the result accumulator rather than overwriting. `AddMxuNumToVectorResult` (`0x140b9680`) additionally stamps the source MXU id.

### The Single-MXU Constraint

`JellyfishEmitter::CheckMxuNum(int)` (`0x140b9780`) `CHECK`s `mxu == 0` on plain Jellyfish (device == `kJellyfishIdentifiers`, assertion *"0 != mxu"*). On **Dragonfish** (`kDragonfishIdentifiers`, the v3 multi-MXU package) the 2-bit MXU-id field is allowed to be non-zero and is stamped into the slot. So the 2-bit MXU-id field exists in the shared JF/DF (v2/v3) codec, is fixed to 0 on Jellyfish, and is live on Dragonfish — the encoders are otherwise identical (`EncoderDf::EncodeVectorExtendedInstruction` @ `0x1e85e520` shares the layout).

---

## Pufferfish (v4) — The Dual-MXU Codec Origin

### Slot Doubling and the −20 Twin

Pufferfish doubles Jellyfish's single `VectorExtended` slot into **two** independent MXU control slots and switches to the `TensorCoreCodecBase` template encoder (no scratch struct, no header strip — every field is placed by a `BitCopy(buf, dst_bit, &field, 0, width)` call whose `dst_bit` *is* the absolute bundle bit). MXU1 (abs 63..82) is a bit-for-bit twin of MXU0 (abs 83..102), offset exactly **−20 bits**.

| Field | MXU0 abs | MXU1 abs | Width | Confidence |
|---|---|---|---|---|
| sub-op | 83 | 63 | 3 | CONFIRMED |
| mode / mxu-num | 89 | 69 | 2 | CONFIRMED |
| opcode | 91 | 71 | 7 | CONFIRMED |
| predicate | 98 | 78 | 5 | CONFIRMED |

See [Pufferfish 51B Bundle](bundle-pf-51b.md#vector-extended--mxu-slots--0x1edb0900-mxu0-0x1ee08060-mxu1) for the slot map in context.

### Opcode Field — Matmul Widens, Carries the Physical MXU Number

The 7-bit opcode at abs 91 selects the op family; for **matmul** the field widens to **9 bits** (abs 89..97), and the low two bits @ 89..90 carry the *physical* MXU number 0..3. This is the v4 origin of the orthogonality between bundle slots and physical arrays — confirmed from the decode-side `Opcode::Matches` masks:

| Mnemonic | opcode value | abs bits | Notes |
|---|---|---|---|
| `Noop` | predication == 0 | 98..102 | empty |
| `MatmulLow Mxu0..3` | 4, 5, 6, 7 | 89..97 (9b) | op-hi 1; mxu-num @ 89..90 |
| `MatmulHi Mxu0..3` | 8, 9, 10, 11 | 89..97 | op-hi 2; mxu-num @ 89..90 |
| `PushGains{Rounded,Low,Hi,Packed,Byte}` | `0x20`..`0x24` | 91..97 (7b) | weight-latch |
| `PushGains{Low,Hi,Byte}Masked` | `0x31`, `0x32`, `0x34` | 91..97 | `= 0x2N + masked·0x10` |
| `DoneWithGains{Gsfn,Gsft}` | `0x18`, `0x19` | 91..97 | end-of-gains |
| `Transpose` / `PackedTranspose` | `0x40` / `0x48` | 91..97 | systolic transpose op |

So `PushGains opcode = 0x20 + {Rounded 0, Low 1, Hi 2, Packed 3, Byte 4} + masked·0x10`, and `matmul opcode = (op-hi << 2) | mxu-num`. The matmul-vs-PushGains distinction is the opcode-high value.

> **QUIRK — the second MXU is selected by an opcode field, not a bundle slot, even though there are two MXU *slots*.** Pufferfish has two MXU *control* slots (MXU0/MXU1, the −20 twin) and **four** physical MXU arrays (`mxu_count = 4`; the perf counters distinguish `MXU0`..`MXU3`). The two axes are orthogonal: the bundle slot picks the *control lane*, while the matmul opcode's low two bits @ 89..90 pick the *physical array*. A reimplementer must not conflate "which MXU slot" with "which of the four MXUs". `PufferfishTarget::MatrixStagingRegisterCount` (`0x1d4949e0`) returns **1**, so PF has a single MSR and therefore no `Target` field — unlike VF below.

---

## Viperfish (v5p) — Named Opcode Families and the Latch Control Bits

### MXU Control Region

Viperfish keeps the two-`VectorExtended`-slots-over-one-operand-pool model and the **−20** twin, widens to a 64-byte bundle, and replaces Jellyfish's `GainLatchMode` enum with *named opcode families*. The MXU control region, verified from the decompiled `EncodeTensorCoreVectorExtended0PushmatrixBf16` (`0x1efaf820`) and `MatrixMultiplyBf16` helpers:

| Field | MXU0 abs | Width | Op family | Value (Bf16) | Confidence |
|---|---|---|---|---|---|
| MXU-id (unit) | 64 | 4 | always (written first) | 0 (MXU 0) | CERTAIN |
| opcode-HIGH (matmul) | 57 | 7 | `MatrixMultiply<fmt>` | `0x1` | CERTAIN |
| opcode-HIGH (latch/push) | 59 | 5 | `Pushmatrix<fmt>` | `0xe` (14) | CERTAIN |
| data-format sub-disc | 51 | 4 | per-op | matmul Bf16 = 1 / push Bf16 = 3 | CERTAIN |
| control (proto `+0x18`) | 48 | 3 | per-op | — | CERTAIN |
| done-gains / latch flag | 55 | 2 | per-op | — | CERTAIN |
| **Transpose** | 57 | 1 | `Pushmatrix*` (proto `+0x20`) | — | CERTAIN |
| **Target** | 58 | 1 | `Pushmatrix*` (proto `+0x24`) | — | CERTAIN |

The latch (`Pushmatrix`) encode reads byte-for-byte from `0x1efaf820` as `BitCopy(buf, 59, …, 5)` (opcode = `0xe`), `BitCopy(buf, 51, …, 4)` (format = 3), `BitCopy(buf, 48, …, 3)` (control), `BitCopy(buf, 55, …, 2)` (done-gains/latch flag), `BitCopy(buf, 57, …, 1)` (Transpose, proto `+0x20`), `BitCopy(buf, 58, …, 1)` (Target, proto `+0x24`), plus the eight shared operand vregs at abs 157/282/293/248/259/214/225/180 (w6 each, proto order). The matmul helper `MatrixMultiplyBf16` @ `0x1efa2e40` writes the same control region with `BitCopy(buf, 57, …, 7)` (opcode = `0x1`), `BitCopy(buf, 51, …, 4)` (format = 1), `BitCopy(buf, 48, …, 3)` (control), `BitCopy(buf, 55, …, 2)` (done-gains) over the identical operand pool — so matmul and push share every field position, differing only in opcode width (7 vs 5) and the repurposed bits 57/58. All offsets **LSB-first**, **CONFIRMED**.

> **QUIRK — the opcode field changes position *and* width by op family at the same slot.** `MatrixMultiply<fmt>` writes a 7-bit opcode @ bit 57 (with a 4-bit `MatmulDataFormat` @ bit 51); `Pushmatrix<fmt>` writes a 5-bit opcode-HIGH @ bit 59 (with a 4-bit Pushmatrix format @ bit 51); `LoadMatrixRegister*` reuses the 7-bit @ 57 window with value `0x37`. On the push/latch path, bits 57 and 58 are *repurposed* as the 1-bit **Transpose** and **Target** fields — the same physical bits that on the matmul path are the two high bits of the 7-bit opcode. The decoder distinguishes them by the opcode-HIGH value (push/latch `0xe` vs matmul `0x1`), which frees the LSB region for latch control. Two distinct 4-bit `data_format` enums share abs 51: the **matmul** `MatmulDataFormat` (Bf16 = 1, U8 = 2, S8 = 3, U4 = 4, S4 = 5, Bf8 = 6) and the **latch** Pushmatrix format (Rounded = 0, PackedIf8Conv = 2, Bf16 = 3, Bf8 = 4, U8 = 5, S8 = 6, U4 = 7, S4 = 8). They are different ordinal spaces in the same field.

### The Transpose and Target Latch Fields

The two 1-bit latch fields trace through the full producer → encoder → decoder → cost-model chain:

- **Transpose (abs 57)** = the MLIR `tpu.matmul_push_rhs` `transpose` attribute — *latch the RHS weight matrix into the systolic array transposed* (matmul-with-transposed-weights at latch time). The attribute flows through the OpConversion to `llo.vector_latch_i`, into `ViperfishTensorCoreEmitter::EmitVectorLatchCommon` (`0x141ba820`), whose Pushmatrix lambda writes proto `+0x20`, then `BitCopy(buf, 57, &proto[0x20], 0, 1)`.
- **Target (abs 58)** = the `staging_register` attribute = the **MatrixStagingRegister (MSR)** bank select. `ViperfishTarget::MatrixStagingRegisterCount` (`0x1d49ace0`) returns **2**, so the 1-bit field picks MSR0 / MSR1. The producer writes the `MatpushTarget` arg to proto `+0x24`; `BitCopy(buf, 58, &proto[0x24], 0, 1)`.

`MatrixStagingRegisterCount` is **1** on Jellyfish (`0x1d490340`) and Pufferfish (`0x1d4949e0`), **2** on Viperfish (`0x1d49ace0`) and Ghostlite (`0x1d497ae0`) — so JF/PF carry *no* Target bit (one MSR, nothing to select), and the `Target` field appears only from v5p. The decoded Transpose / Target bits feed the cost model `SetReservations<MatpushModifier>` (`0x1c8abde0`, an `array<int,19>` keyed on `{dtype × transpose}`) and the transpose-load latency `LatencyTableViperfish::XposeXLUReservationLatency` (`0x1c8a4f00`).

> **GOTCHA — the latch `transpose` bit is *not* the standalone transpose op.** The latch Transpose @ abs 57 transposes the *weight* matrix as it is pushed into the array (a Pushmatrix-latch attribute). A separate `EmitVectorTranspose<viperfish>` (`0x141c3f00`) op family emits `TransposeStart`/`Continue`/`End`/`Packed`/`Segmented` opcodes that route data through the XLU/transpose unit (PF opcode `0x40`/`0x48`). They are two different transpose mechanisms; a reimplementer must not fold them together.

### Op-Family Roster (v5+)

The 94 `VectorExtended0` helpers per slot are not 94 distinct layouts — they are one `{opcode, sub-format, MXU-id, operand}` template specialized by the opcode immediate and the operand-present mask:

| Op family | Variants | Role |
|---|---|---|
| `MatrixMultiply<fmt>` | Bf16, Bf8, If8Bf16, S4, S8, U4, U8, F32Rounded | dense matmul step, one helper per data format |
| `MatrixMultiply<fmt>Lgmr{Msra,Msrb}[Masked]` | per fmt × {Msra, Msrb} × {plain, Masked} | latch-via-LMR fused matmul (multi-pass K-tiling) |
| `Pushmatrix<fmt>` | Bf16, Bf8, S4, S8, U4, U8, Rounded, PackedIf8Conv | moving-operand push (matprep) |
| `LoadMatrixRegister{Gmr,Lmr}{Msra,Msrb}` | 4 | weight-stationary latch (opcode-HIGH `0x37`) |
| `*Transpose*`, `Segmented*`, `Packed*` | ~10 | systolic-array transpose |

The `Masked` and `Lgmr{Msra,Msrb}` suffixes are the v5+ realization of the Jellyfish 6-entry `GainLatchMode → VEopcode` table: what was a small enum on v3 became a named opcode family on v5. `Msra`/`Msrb` select which of the two MSR banks the fused matmul accumulates into (the same bank `Target` selects on the latch path).

---

## GXC (v6e Ghostlite / v7 6acc60406) — Wider Opcodes, FP8, and Larger Twins

The GXC generations keep the v5+ codec shape — two `VectorExtended` slots over one operand pool, `BitCopy`-packed, linear `Opcode::Matches` decode — but widen the opcode field 7→8 bits, shift the control region, and re-map the dtype set.

### Ghostlite (v6e / `glc`)

The MXU opcode-HIGH widens to **8 bits @ bit 58** (vs Viperfish's 7-bit @ 57), MXU-id moves to bit 66, the done-gains / latch flag to bit 56, and the slot-encoder opcode bound grows to `0x70` (113 ops vs Viperfish's 103). The dtype set is the full **8-format** `{F32, If8, Bf16, Bf8}` (float) + `{U8, S8, U4, S4}` (int). See [Ghostlite Bundle](bundle-gl.md#transformation-2--opcode-fields-widen-78-bits) for the slot map.

The latch / matmul opcode is a *unified* 8-bit field @ abs 58 — confirmed on the encode side: `MatrixMultiplyBf16` @ `0x1f333ce0` writes `BitCopy(buf, 58, …, 8)` value `0x1`, and `LoadMatrixRegisterGmrMsra` @ `0x1f33f140` writes the *same* `BitCopy(buf, 58, …, 8)` window with the latch value `0x37` (55). The matmul opcode @ abs 58 (w8) = `0x2` (Msra) / `0x3` (Msrb), MSR-select = the opcode LSB; the decode-side `Opcode::Matches` groups the low two bits 58,59 == 3 as the `latch-class` discriminator and reads a 6-bit latch sub-opcode in the same window (14 = float / 15 = int) with the dtype-class @ abs 54 (w2) picking the sub-ordinal within the 4-element class. `GhostliteTarget::MatrixStagingRegisterCount` (`0x1d497ae0`) = 2. The MXU0↔MXU1 twin is **−21**: the matmul opcode-HIGH anchors at abs 58 on MXU0 (VEx0) and abs 37 on MXU1 (VEx1) — `MatrixMultiplyBf16` VEx0 @ `0x1f333ce0` (opcode @ 58, fmt @ 52, control @ 49, done-gains @ 56) vs VEx1 @ `0x1f388440` (opcode @ 37, fmt @ 31, control @ 28, done-gains @ 35), every field offset by exactly 21. All offsets in this paragraph are **LSB-first** and **CONFIRMED** from the encoder `BitCopy` immediates.

### 6acc60406 (v7 / `gfc`)

The newest generation is **float-only**: it drops the integer matmul group and supports four dtypes `{F32, E4m3, Bf16, E5m2}` — the two FP8 formats named explicitly (vs Ghostlite's `If8`/`Bf8`). From `MatrixMultiplyBf16` VEx0 @ `0x1f99a920` (verified `BitCopy` immediates): matmul opcode-HIGH 8-bit @ bit 62, data-format @ bit 57 (w4), control @ bit 54 (w3), done-gains / latch flag @ bit 61 (w1); MXU-id @ bit 70 (w2), written by the VEx0 dispatcher encoder `TensorCoreVectorExtended0Encoder::Encode` @ `0x1f996940` (`BitCopy(buf, 70, …, 2)`). The eight systolic source vregs land at bits 156 / 276 / 287 / 243 / 254 / 210 / 221 (w6) and 47 (w7) — the last operand widens to 7 bits. The latch valid-guard is a single `bt` of bit 62. See [6acc60406 Bundle](bundle-gf.md#the-mxu-format-remap-and-fp8-gf-vs-ghostlite). All offsets **LSB-first**, **CONFIRMED**.

The MXU0↔MXU1 twin is **−25**, not −21: 6acc60406's MXU0 control region drifted +4 bits higher than Ghostlite's (matmul opcode-HIGH 58 → 62), while MXU1 anchors at the *same* abs 37 in both gens, so the inter-MXU delta grows by 4. Encode-side `MatrixMultiplyBf16` VEx0 @ `0x1f99a920` vs VEx1 @ `0x1f9d77e0` shows every field offset by exactly 25: matmul opcode-HIGH 62 vs 37, format 57 vs 32, control 54 vs 29, done-gains 61 vs 36.

### Cross-Generation Field Summary

The MXU control region, synthesized across all five generations (matmul opcode-HIGH bit / latch opcode bit / inter-MXU twin):

| Gen | Codename | Bundle | VE slots | Physical MXUs | Matmul opcode bit | Latch opcode bit | Twin | MSR count | dtype set |
|---|---|---|---|---|---|---|---|---|---|
| v2 | jellyfish | 41 B | 1 | 1 | (6-bit VEopcode @ 29..34, jump table) | — | n/a | 1 | `GainLatchMode 0..5` subset |
| v3 | dragonfish | 41 B | 1 | 2 | (same codec; mxu-id live) | — | n/a | 1 | as JF |
| v4 | pufferfish | 51 B | 2 | 4 | 91 (9b w/ mxu-num @ 89..90) | 91 (PushGains `0x20`..`0x34`) | −20 | 1 | 8 (int + float) |
| v5p | viperfish | 64 B | 2 | 4 | 57 (7b) | 59 (Pushmatrix `0xe`) | −20 | 2 | 8 (int + float) |
| v6e | ghostlite (`glc`) | 64 B | 2 | 2 | 58 (8b) | 58 (unified 8b, low-2 == 3) | −21 | 2 | 8 (int + float) |
| v7 | 6acc60406 (`gfc`) | 64 B | 2 | 2 | 62 (8b) | 62 (unified 8b) | −25 | 2 | 4 (float only) |

The evolution is a clean progression: a single 6-bit jump-table opcode (v2/v3) → a dual-slot 7/9-bit opcode carrying the physical MXU in its low bits (v4) → named per-dtype opcode families with standalone Transpose/Target latch bits (v5p) → wider 8-bit unified opcodes with the latch discriminator folded into the opcode low bits (v6e) → a float-only FP8 remap (v7). The systolic *contract* — weight-stationary array, matpush 8×128/4×256 tiles, latch / push / matmul / matres sequence over a shared operand pool — never changes; only the encoding widens, the dtype set shifts, and the array dimension steps 128×128 (JF–VF) → 256×256 (Ghostlite, 6acc60406).

---

## Encode / Decode Path

The MXU slot is built as a proto `BundleSlot` first, then byte-serialized:

```text
STAGE 1 — emit (build proto submessage)
  Emit{VectorMatmul,VectorLatch,VectorMatres}
    └─ CurrentBundle → GetPopulatedSlots (check slot free)
    └─ EmitVector{Extended,Result}Instruction  (create the proto submessage)
    └─ AddMxuNumTo*                              (stamp 2-bit MXU id, set present bit)
  result: a Bundle proto with slot_mask bit 0x080 (VE) / 0x100 (VR) set

STAGE 2 — encode (proto → raw bundle bytes)
  v3:   EncoderJf::EncodeBundleInternal (0x1e86c7c0)
          → zero buffer; splat kNeverExecute (31) into every slot predicate
          → slot_mask dispatch → EncodeVectorExtendedInstruction / EncodeVectorResultInstruction
  v4+:  TensorCoreCodecBase::Encode → per-slot <Slot>Encoder::Encode → BitCopy(buf, dst_bit, …)
  decode (inverse):
    v3   = DecoderJf::DecodeVectorExtendedSlot (0x1e854000): two-level jump table on the 6-bit opcode
    v4+  = TensorCoreVectorExtended{0,1}Decoder::Decode: staged byte-copy + linear Opcode::Matches sweep
```

Both Jellyfish encoders prefill `kNeverExecute = 31` (`0xB834CFC`) into every slot's predicate field before any slot is written, so an MXU op absent from a bundle leaves a defined never-execute predicate rather than garbage. The 5-bit predicate semantics are shared by every slot: bits 0..3 select predicate register 0..14, value 15 = `kAlwaysExecute` (`0xB834CF8`), bit 4 (value 16) is the predicate-negate flag, value 31 = `kNeverExecute`. `kPredicateRegisterCount`, `kAlwaysExecute`, `kNeverExecute` read 15, 15, 31 directly from the ELF at `0xB834CF4`.

> **GOTCHA — empty is predicate-31, not all-zero.** The empty-slot mark is a *nonzero* stamp. A reimplementation that `memset`s the bundle to zero and fills only active slots leaves inactive MXU slots at predicate 0 — a valid predicate-register reference — turning empty slots into live garbage matmuls. See [NOP / Unused-Slot Canonical Encoding](nop-canonical.md).

---

## Systolic-Array Geometry

| Quantity | Value | Source | Confidence |
|---|---|---|---|
| Systolic array | 128 × 128 (JF/DF/PF/VF) / 256 × 256 (Ghostlite, 6acc60406) | base `Target` `LaneCount`; `GhostliteTarget::MxuContractingSize`/`MxuNoncontractingSize` @ `0x1d497840`/`0x1d497860` = 256 | CONFIRMED |
| MXUs per TensorCore | 1 (JF) / 2 (Dragonfish) / 4 (PF, VF) / 2 (Ghostlite, 6acc60406) | `NumVexSlots`, `VectorIsa.mxu_count` f5 | CONFIRMED |
| MXU-id field width | 2 bits (JF/PF: physical select) / 4 bits (VF MXU-id @ 64, glc @ 66) / 2 bits (gfc @ 70) | encoder | CONFIRMED |
| matpush / latch tile | 8 × 128 or 4 × 256 | vmatpush perf-counter string | CONFIRMED |
| vreg shape | 8 sublanes × 128 lanes | `Target::SublaneCount` @ `0x1d60f300` = 8, `LaneCount` @ `0x1d60f400` = 128 | CONFIRMED |

On the 128×128 generations the array is filled by sixteen 8×128 matpush tiles (or eight 4×256); the moving operand streams through, one `vmatmul` advances one systolic step, and the result drains via `vmatres` after the array latency. The per-format matmul latency tables are on the per-gen cost pages — they are *not* a single constant across generations.

> **QUIRK — the array is not 128×128 on every gen.** Ghostlite and 6acc60406 cut `mxu_count` from 4 to 2 but *double* the systolic dimension to **256×256** (the C++ overrides `GhostliteTarget::MxuContractingSize`/`MxuNoncontractingSize` return 256; the base `Target` returns 128). The 256 dimension is a C++ literal, not a proto field — the `VectorIsa` proto carries only `lane_count = 128` and `mxu_count`. A reimplementer who hard-codes a 128×128 array for v6e/v7 will mis-size the latch tile count and the per-step throughput. See [Per-Codename HW Constants](../targets/per-codename-hw-constants.md#mxu-count-vs-systolic-dimension).

---

## Related Components

| Component | Relationship |
|---|---|
| `EncoderJf::EncodeVectorExtendedInstruction` `0x1e869f00` | v3 MXU slot byte encoder (opcode @ 29..34) |
| `EncoderJf::EncodeVectorResultInstruction` `0x1e865ae0` | v3 matres byte encoder (predicate @ 22) |
| `JellyfishEmitter::EmitVectorMatmul` `0x140b92c0` | v3 matmul opcode dispatch (DoneWithGainsMode) |
| `JellyfishEmitter::EmitVectorLatch` `0x140b8c20` | v3 latch opcode dispatch (GainLatchMode table) |
| `TensorCoreCodecBase::Encode` `0x1d224300` | v4+ generic codec (BitCopy-packed slots) |
| `ViperfishTensorCoreEmitter::EmitVectorLatchCommon` `0x141ba820` | v5p Transpose/Target field producer |
| `SetReservations<MatpushModifier>` `0x1c8abde0` | cost-model consumer of the decoded transpose/target bits |

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the VLIW bundle, slot_mask dispatch, and kNeverExecute convention the MXU slot lives inside.
- [Jellyfish 41B Bundle](bundle-jf-41b.md) — the v3 VectorExtended/VectorResult absolute bit positions and the 12-byte-strip law.
- [Pufferfish 51B Bundle](bundle-pf-51b.md) — the v4 dual-MXU −20 twin and the 9-bit matmul opcode carrying the physical MXU number.
- [Viperfish 64B Bundle](bundle-vf-64b.md) — the v5p MXU control region, the Transpose/Target latch bits, and the named opcode families.
- [Ghostlite Bundle](bundle-gl.md) — the v6e 8-bit unified opcode and the −21 twin.
- [6acc60406 Bundle](bundle-gf.md) — the v7 float-only FP8 remap and the −25 twin.
- [Matprep / IAR / Latch](slot-matprep-iar-latch.md) — the matpush WORD tables and the IAR addressing the latch/push operands ride.
- [Decode-Side: JF / PF](decode-side-jf-pf.md) and [Decode-Side: VF / GXC](decode-side-vf-gxc.md) — the disassembler inverse that confirms every field position byte-for-byte.
- [InstBits Master DB](instbits-master-db.md) and [MC Emitter](mc-emitter.md) — the parallel LLVM-MC encoding path for the same vmatmul/vmatprep/vmatres MachineInstrs.
- [LLO Opcode Enum](llo-opcode-enum.md) — the LloOpcode numeric space the MXU mnemonics live in.
- [../cost/mxu-latency-overview.md](../cost/mxu-latency-overview.md) — the cost model that consumes the MXU-slot fields, and the per-gen matmul latency tables.
- [../compiler/dot-conv-mxu-lowering.md](../compiler/dot-conv-mxu-lowering.md) — the HLO kDot / kConvolution descent that fills the MXU slot with the `[matprep, latch, matmul…, matres]` sequence.
- [Per-Codename HW Constants](../targets/per-codename-hw-constants.md) — the `mxu_count` (1/2/4/4/2/2) and the 256×256 systolic-dimension override for Ghostlite/6acc60406 that the geometry table cites.
