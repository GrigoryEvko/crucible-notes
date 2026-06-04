# Jellyfish 41-Byte Bundle

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

The Jellyfish TensorCore VLIW bundle is a **41-byte (328-bit)** issue word. It is the oldest TensorCore bundle libtpu still encodes, and the per-generation bundle pages for Pufferfish, Viperfish, Ghostlite, and the gen-5 `6acc60406` codec are best read as deltas against it. Unlike the V5+ generations — which build an all-zero MC record and assemble the bundle entirely through `BitCopy` into a shared span (see [Record Format](record-format.md)) — Jellyfish is a *direct-pack* encoder: `EncoderJf::EncodeBundleInternal` (`0x1e86c7c0`) builds the bundle as read-modify-write `shl`/`and`/`or` arithmetic on the qwords of a 53-byte internal scratch struct, then strips the first 12 bytes and copies out the 41-byte tail. There is no per-instruction record and no `BitCopy`; every field's position is the literal shift constant in its slot encoder.

Three properties make the bundle decodable from the binary alone. First, the **12-byte-strip law**: the encoder works in a 53-byte struct, and on success copies `struct[0x0C .. 0x34]` as the wire bundle, so **output byte N == internal-struct byte (0x0C + N)** and the absolute bundle bit of any field is `struct_byte*8 + shift − 96`. Second, the **slot-mask dispatch**: the `Bundle` proto carries a 32-bit `slot_mask` at `proto+0x10`, one bit per slot, and `EncodeBundleInternal` tests each bit to call the matching per-slot `Encode*` writer. Third, the **`kNeverExecute` prefill**: before any slot is filled, the encoder stamps the predicate value 31 into every slot's predicate field, so an absent slot is a defined no-op rather than garbage.

This page documents the complete slot map at absolute bit precision, the `EncodeBundleInternal` packing order, the per-slot field arithmetic, the empty-slot prefill, and the `MakeInstruction` / `FindFreeSlot` VLIW-construction model that folds individual LLO ops into bundle slots. For reimplementation, the contract is:

- The **53-byte scratch / 41-byte wire** relationship and the `output_byte N == struct_byte 0x0C+N` law that converts any per-slot shift into an absolute bundle bit.
- The **slot-mask → per-slot-writer dispatch**: nine dispatchable slots, six 16-bit immediate slots, and the TTU path, all keyed on `proto+0x10`.
- The **five physical word-regions** of the bundle and which slots co-exist by partitioning a 64-bit word disjointly.
- The **`kNeverExecute=31` prefill** convention and the 5-bit predicate field every slot carries.
- The **`FindFreeSlot` fold**: how the emitter assigns an LLO op to a free, legal slot and marks the `slot_mask` bit.

| | |
|---|---|
| **Generation** | `TpuVersion 0` = `kJellyfish`; external display name **TPU v2**; internal codename `jellyfish` (no `Trillium` string exists in the binary) — see [Codename Matrix](../targets/tpu-version-codename-matrix.md) |
| **Encode entry** | `EncoderJf::EncodeBundleInternal(Bundle const&, bool)` @ `0x1e86c7c0` |
| **Encoder factory** | `tpu::internal::CreateEncoderJfDf` @ `0x1e835b80` (builds both the JF and DF encoders) |
| **Wire width** | **41 bytes / 328 bits** (`JellyfishCodecMetadata::BundleSizeBytes` @ `0x1ecf7460` returns `41`; the buffer is hard-pinned by `operator new(0x29)` in `EncodeBundleInternal`) |
| **Internal scratch** | 53 bytes; wire = `struct[0x0C..0x34]`; `output_byte N == struct_byte 0x0C+N` |
| **Slot mask** | 32-bit word at `Bundle` proto `+0x10`; one bit per slot |
| **Dispatchable slots** | 9 (scalar ×2, vector-ALU ×2, vstore, vload, vextended/MXU, vresult, misc) + 6 imm + TTU |
| **Empty-slot mark** | `HardwareBundleBits::kNeverExecute = 31` (`0xB834CFC`) in every slot's 5-bit predicate field |
| **HBM stored width** | 42 bytes (`BundleSizeBytesForHbm` @ `0x1ecf74c0`); 3 bundles per 128-byte DMA chunk |
| **Shared by** | Dragonfish (`EncoderDf` inherits this `EncodeBundleInternal`) — see [Dragonfish Bundle](bundle-df.md) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The 12-Byte Strip and the Absolute-Bit Law

`EncodeBundleInternal` does not build the bundle in a 41-byte buffer. It builds it in a larger internal scratch struct (the encoder's working object, addressed through `r15`/`this`), writes every slot at struct-relative byte offsets, and on the success path copies a 41-byte window out of the struct as the wire bundle.

The success path is the proof. After all present slots are encoded, the encoder advances the struct pointer by `0x0C` and copies 41 (`0x29`) bytes:

```c
// EncoderJf::EncodeBundleInternal success path  @ 0x1e86c7c0 (lines 849-857)
buf = operator new(0x29);                 // 41-byte heap buffer
src = (char*)struct + 12;                 // strip the 12-byte header
// vmovups ymm0=[src]      ; ymm1=[src+9]   -> [buf]/[buf+9]   (overlapping 32+9 = 41 B)
memcpy(buf, src, 41);
result.data = buf;  result.size = 0x29;   // StatusOr<vector<uint8>> of size 41
```

The two `vmovups` copy bytes `[src .. src+32)` and `[src+9 .. src+41)`; together they cover all 41 bytes (`9 + 32 = 41`). The wire bundle is therefore exactly `struct[0x0C .. 0x34]`.

> **NOTE — bit-numbering convention.** Every absolute bit position on this page is **LSB-first**, matching [Bundle Model](bundle-model-overview.md#what-a-bundle-is): bit 0 is the least-significant bit of output byte 0, and a field shifted left by `S` (`(value & mask) << S`) occupies bit `S` and up. The Jellyfish direct-pack encoder uses this convention via its `shl`/`or` shift constants; there is no MSB-first / big-endian ordering anywhere in the encode path. A field written by `(value & mask) << S` into the struct qword at byte offset `B` therefore lands at **absolute bundle bit `B*8 + S − 96`** (because `0x0C * 8 = 96`). When `B == 0x0C` the `−96` cancels and the absolute bit equals the shift directly — which is why the qword-0 prefill constants read as their bundle bit positions verbatim.

The 41 bytes partition into five qwords by absolute bit:

```text
 qword0 = abs   0 .. 63    (struct 0x0C..0x13)   output bytes 0x00..0x07
 qword1 = abs  64 .. 127   (struct 0x14..0x1B)   output bytes 0x08..0x0F
 qword2 = abs 128 .. 191   (struct 0x1C..0x23)   output bytes 0x10..0x17
 qword3 = abs 192 .. 255   (struct 0x24..0x2B)   output bytes 0x18..0x1F
 qword4 = abs 256 .. 327   (struct 0x2C..0x34)   output bytes 0x20..0x28 (partial, 9 B)
```

---

## The Slot-Mask Dispatch

The compiler-side `Bundle` is a proto with a 32-bit `slot_mask` at `proto+0x10` and a typed sub-message pointer per slot. `EncodeBundleInternal` reads the mask once into a register, tests each bit, and on a set bit calls that slot's per-slot writer with the sub-message pointer. An unset bit leaves the slot at its prefilled `kNeverExecute` value.

```c
// EncodeBundleInternal slot dispatch  @ 0x1e86c855.. (decompiled, lines 110-270)
mask = bundle->slot_mask;                 // *(uint32_t*)(proto + 0x10)
if (mask & 0x001) EncodeScalarInstruction(scalar0 = proto+0x18, /*lane=*/0);  // vtable +120
if (mask & 0x002) EncodeScalarInstruction(scalar1 = proto+0x20, /*lane=*/1);  // vtable +120
if (mask & 0x008) EncodeVectorAluInstruction(valu0 = proto+0x30, /*lane=*/0); // 0x1e864f00
if (mask & 0x010) EncodeVectorAluInstruction(valu1 = proto+0x38, /*lane=*/1); // 0x1e864f00
if (mask & 0x020) EncodeVectorStoreInstruction(vstore = proto+0x40);          // 0x1e868c40
if (mask & 0x040) EncodeVectorLoadInstruction(vload  = proto+0x48);           // 0x1e867340
if (mask & 0x080) EncodeVectorExtendedInstruction(vext = proto+0x50);         // vtable +136
if (mask & 0x100) EncodeVectorResultInstruction(vresult = proto+0x58);        // vtable +128
if (mask & 0x200) EncodeMiscInstruction(misc = proto+0x60);                   // vtable +144
// then the six 16-bit immediate slots, validated < 0x10000:
for bit in {0x8000,0x4000,0x2000,0x1000,0x800,0x400}:
    if (mask & bit) ValidateImmediate(proto[+0x7C..+0x68]);                   // 0x1e86da20
```

The bit-to-slot map is byte-exact from the decompiled `if ((mask & N) != 0)` ladder; each writer reads its sub-message pointer at the listed `proto+` offset (a null pointer falls back to the per-type `_globals_` default instance). The same `proto+0x10` word is read by `ProtoUtils::GetPopulatedSlots` (`0x1e875be0`) with the identical bit tests — it is the canonical bundle-occupancy bitfield.

| `slot_mask` bit | `proto+` | Slot (role) | Per-slot writer | Struct word | Confidence |
|---|---|---|---|---|---|
| `0x0001` | `+0x18` | scalar_0 (SPU lane0 / sequencer lane) | `EncodeScalarInstruction(lane=0)` @ `0x1e862060` | `0x2D` (qword4) | CONFIRMED |
| `0x0002` | `+0x20` | scalar_1 (SPU lane1) | `EncodeScalarInstruction(lane=1)` @ `0x1e862060` | `0x2D` (qword4) | CONFIRMED |
| `0x0008` | `+0x30` | vector_alu_0 (VPU lane0) | `EncodeVectorAluInstruction(lane=0)` @ `0x1e864f00` | `0x1D` (qword2) | CONFIRMED |
| `0x0010` | `+0x38` | vector_alu_1 (VPU lane1) | `EncodeVectorAluInstruction(lane=1)` @ `0x1e864f00` | `0x16` window | CONFIRMED |
| `0x0020` | `+0x40` | vector_store (mem-store) | `EncodeVectorStoreInstruction` @ `0x1e868c40` | `0x14` + `0x16` | CONFIRMED |
| `0x0040` | `+0x48` | vector_load (mem-load) | `EncodeVectorLoadInstruction` @ `0x1e867340` | `0x0C` + `0x1F` | CONFIRMED |
| `0x0080` | `+0x50` | vector_extended (MXU / EUP) | `EncodeVectorExtendedInstruction` @ `0x1e869f00` | `0x0C` + `0x16` | CONFIRMED |
| `0x0100` | `+0x58` | vector_result (matres / EUP-pop) | `EncodeVectorResultInstruction` @ `0x1e865ae0` | `0x0C` | CONFIRMED |
| `0x0200` | `+0x60` | misc (mask / rotate / imm-set) | `EncodeMiscInstruction` @ `0x1e86be80` | `0x0C` | CONFIRMED |
| `0x0400`..`0x8000` | `+0x68`..`+0x7C` | immediate slots imm0..imm5 (16-bit each) | `ValidateImmediate` @ `0x1e86da20` | imm region `0x1F`/`0x27` | HIGH |
| `(0x0004)` | — | TTU operands | `EncodeTtuOperands` @ `0x1e863280` | `0x2D` + `0x2B` (borrows scalar0) | HIGH |

> **QUIRK — the scalar slot is *lane-aware* and the lanes live at different bit bases in the same qword.** `EncodeScalarInstruction` takes a lane argument (0 or 1) and writes scalar_0 at the *high* bits of qword4 (opcode @abs311) and scalar_1 at the *lower* bits (opcode @abs284). A reimplementation that assumes "slot 0 is the low bits, slot 1 the high bits" inverts the two scalar lanes. The same lane split applies to the two vector-ALU lanes, which live in *two different struct words* (lane0 in word `0x1D`, lane1 in the 56-bit window at `0x16`), not adjacent fields of one word.

---

## The Five Physical Word-Regions

The 41-byte bundle is not nine contiguous slot fields. Several slots share a 64-bit word and co-exist by occupying disjoint bit sub-ranges of it; others are spread across a multi-word operand window. The five regions:

```text
 struct 0x0C  -> qword0 (abs 0..63)    Misc | VectorResult | VectorExtended(MXU) | VectorLoad
                                        (four slots packed disjointly into one qword)
 struct 0x14  -> qword1 low (abs 64..)  VectorStore source/control
 struct 0x16  -> 56-bit window (abs 90..127)  VALU lane1 + VStore/VExtended operand cross-fields
 struct 0x1D  -> abs 136..167          VALU lane0
 struct 0x1F/0x27 -> abs ~152..223     immediate-value bytes
 struct 0x2D  -> qword4 (abs 264..327) scalar_0 / scalar_1 (SPU) + TTU operands
```

The four-slots-in-qword0 packing is the most important structural fact: **Misc** (abs 5..17), **VectorResult** (abs 18..26), **VectorExtended/MXU** (abs 27..39), and **VectorLoad** (abs 40..62) each own a disjoint sub-range of the first 64 bits, so a bundle can carry all four simultaneously. The 56-bit window at struct `0x16` is a genuine cross-word field: it is assembled in the encoder as `dword[0x16] | (word[0x1A] << 32) | (byte[0x1C] << 48)` and holds VALU lane1 plus the operand spillover of VectorStore and VectorExtended — these three are mutually constrained by the bundle packer, not independent.

> **GOTCHA — qword0 co-existence is real but the `0x16` window is shared.** Misc / VResult / VExtended / VLoad genuinely co-exist (disjoint bit ranges in qword0), but VALU-lane1, VectorStore, and VectorExtended *operands* all reach into the same 56-bit window at struct `0x16`. A packer that treats every slot as fully independent will let a VALU-lane1 op and a wide VectorStore both claim window bits and silently corrupt each other. The slot-legality predicate in [`FindFreeSlot`](#the-vliw-packing-model) is what prevents this.

---

## Per-Slot Field Arithmetic

Each per-slot writer ORs its fields into one struct qword via `(field & mask) << shift`. The shifts below are struct-word-relative; add `struct_byte*8 − 96` to get the absolute bundle bit (for qword0 / struct `0x0C` the shift *is* the absolute bit). Every position is read directly from the `shl`/`and`/`or` triples in the decompiled encoders.

### Scalar (struct `0x2D`, qword4) — `EncodeScalarInstruction` @ `0x1e862060`

The lane-0 (sequencer) lane sits at the high bits, lane-1 at the low bits. Decompiled (lines 240-253): lane1 takes the `(... & 0x3F) << 20` / `pred << 26` branch (clear-masks `0x...FC0FFFFF` / `0x...83FFFFFF`); lane0 takes the `<< 47` / `pred << 53` branch (clear-masks `0xFFE07F...` / `0xFC1F...`).

| Field | proto src | lane0 shift → abs | lane1 shift → abs | Width | Confidence |
|---|---|---|---|---|---|
| predicate | `EncodePredication & 0x1F` | `<<53` → 317 | `<<26` → 290 | 5 | CONFIRMED |
| opcode | `[instr+0x50] & 0x3F` | `<<47` → 311 | `<<20` → 284 | 6 | CONFIRMED |
| X reg | `ScalarBinaryOperands+0x20 & 0x1F` | `<<31` → 295 | `<<4` → 268 | 5 | CONFIRMED |
| Y reg | `+0x1C & 0x1F` | `<<42` → 306 | `<<15` → 279 | 5 | CONFIRMED |
| ScalarY | `+0x18 & 0x3F` | `<<36` → 300 | `<<9` → 273 | 6 | CONFIRMED |

The opcode jump table covers `ScalarOpcode` 0..0x37; an immediate Y constant routes through `EncodeImmediateValueForScalarYEncoding` (`0x1e85f3a0`) into the immediate region. This refines an earlier reading that placed the scalar0 opcode at bits 20-25 — that was the *lane-1* placement; the sequencer lane (lane0) opcode is at abs 311.

### Vector-ALU lanes — `EncodeVectorAluInstruction` @ `0x1e864f00`

Lane0 packs into the 16-bit word at struct `0x1D`; lane1 packs into the 56-bit window at struct `0x16`. Decompiled (lines 80-128): lane0 `pred << 11` (clear `& 0x7FF`) and `opcode << 5` (clear `0xF81F`); lane1 `pred << 36`, `Vx << 25` (clear `0xC1FFFFFF`), `y << 10` (clear `0xFFFF83FF`).

| Field | proto src | Lane0 (word `0x1D`) abs | Lane1 (window `0x16`) abs | Width | Confidence |
|---|---|---|---|---|---|
| Vx | `[instr+0x58] & 0x1F` | `<<0` → 136 | `<<25` → 105 | 5 | CONFIRMED |
| opcode | `[instr+0x50] & 0x3F` | `<<5` → 141 | `<<30` → 110 | 6 | CONFIRMED |
| predicate | `& 0x1F` | `<<11` → 147 | `<<36` → 116 | 5 | CONFIRMED |
| y | `[instr+0x60] & 0x1F` | (spills to `0x16`) | `<<10` → 90 | 5 | CONFIRMED |
| dest | `[instr+0x60] & 0x1F` | `<<41`/`<<51` (spill) | `<<41` → 121 | 5 | HIGH |

The opcode is 6-bit (`VectorAluOpcode` 0..62). Opcode `0x18` (LANE_ID) and the EUP run `0x30..0x34` take dedicated branches; `IsEupOpcode` (`0x1e875900`) gates whether the EUP/XLU reservation is needed before the bundle commits.

> **NOTE —** the lane-0 *dest* width is graded HIGH (not CONFIRMED) because it was recovered from the shared `y`/`dest` branch (struct `0x16` `shl 0x29`/`shl 0x33`) rather than a dedicated named-field write. The lane-1 window is fully isolated (`y@90, Vx@105, opcode@110, pred@116, dest@121`) and the decode-side independently confirms the lane-1 layout — see [Decode-Side: JF / PF](decode-side-jf-pf.md).

### Vector-Extended / MXU (struct `0x0C`) — `EncodeVectorExtendedInstruction` @ `0x1e869f00`

Decompiled (lines 49-76): `pred << 35` (clear `0xFFFFFF07FFFFFFFF`), `mxu_id << 27` (clear `0xFFFFFFFFE7FFFFFF`), has-bit `| 0x20000000` (bit 29), and the opcode field cleared by `0xFFFFFFF81FFFFFFF` (bits 29..34).

| Field | proto src | abs bit | Width | Confidence |
|---|---|---|---|---|
| predicate | `EncodePredication & 0x1F` | 35 | 5 | CONFIRMED |
| opcode | (6-bit `VectorExtendedOpcode`) | 29..34 | 6 | CONFIRMED |
| mxu-id | `[instr+0x64] & 3` | 27..28 | 2 | CONFIRMED |
| has-bit | `\| 0x20000000` | 29 | 1 | CONFIRMED |
| operands | `[instr+0x6C]` | `<<46`/`<<15` into the `0x16` window; byte `0x14` `<<11` | — | HIGH |

The opcode clear-mask `0xFFFFFFF81FFFFFFF` here is the *same* mask the decoder uses to extract the 6-bit opcode at abs 29..34, so the encode and decode sides agree bit-for-bit; this gives the MXU opcode/mxu-id/predicate fields CERTAIN-grade cross-confirmation. The opcode space is `{matmul 0..6, latch/PushGains 7..12, transpose/RPU 13..34}`; see [MXU Slot](slot-mxu.md) and [Decode-Side: JF / PF](decode-side-jf-pf.md) for the full roster and the two-level jump-table decode.

### Vector-Result / matres (struct `0x0C`) — `EncodeVectorResultInstruction` @ `0x1e865ae0`

| Field | proto src | abs bit | Width | Confidence |
|---|---|---|---|---|
| predicate | `& 0x1F` `<<22` | 22 | 5 | CONFIRMED |
| result-format | `[instr+0x40] & 3` `<<20` | 20 | 2 | CONFIRMED |
| result-mode | `[instr+0x44] & 3` `<<18` | 18 | 2 | CONFIRMED |
| dest-Vreg | `[instr+0x48] & 0x1F` | mode-dependent: `<<10` / `<<41` / `<<51` | 5 | HIGH |

The destination register's bit position is selected by the `which_destination` sub-form (which EUP/result target the value drains to). The result-valid routing is covered on the [ResultFifo](resultfifo-archregister.md) page.

### Vector-Load (struct `0x0C` + Vs ports `0x1F`) — `EncodeVectorLoadInstruction` @ `0x1e867340`

| Field | proto src | abs bit | Width | Confidence |
|---|---|---|---|---|
| predicate | `& 0x1F` `<<58` | 58 | 5 | CONFIRMED |
| opcode (addr-mode) | `[instr+0x50] & 3` `<<56` | 56 | 2 | CONFIRMED |
| has-bit | `[instr+0x60]!=0` → `\| (1<<40)` | 40 | 1 | CONFIRMED |
| destVreg | `[instr+0x64] & 0x1F` `<<51` | 51 | 5 | CONFIRMED |
| stride | `[instr+0x54] & 7` `<<48` | 48 | 3 | CONFIRMED |
| offset | `[instr+0x58] & 3` `<<46` | 46 | 2 | CONFIRMED |
| base | `[instr+0x5C] & 3` `<<44` | 44 | 2 | CONFIRMED |
| vs2 / vs1 | `[target+0x88] & 0x1F` `<<10`, `[target+0x84] & 0x1F` `<<5` | word `0x1F` | 5 each | HIGH |

The 2-bit opcode at abs 56 selects `{VmemLoad, VmemLoadShuffled, VmemLoadIndexedIar0, VmemLoadIndexedIar1}`. The Vs base/index ports and the sublane-mask sub-fields route through `EncodeVectorSublaneMaskEncoding` (`0x1e867840`). These positions close a long-standing gap: the VectorLoad/Store field layout is written byte-exact by the proto-path encoders here, not hidden in an `InstBits` row. See [Memory-Load](slot-memory-load.md).

### Vector-Store (struct `0x14` + `0x16`) — `EncodeVectorStoreInstruction` @ `0x1e868c40`

Writes the source vreg (11-bit packed) into word `0x14` at abs 75, predication into the `0x16` cross-word at abs 85, sets the presence bit `or [struct+0x13], 0x80`, and writes base/offset into word `0x2D`. The sublane/stride sub-fields land via `<<8`/`<<6`/`<<4` into word `0x14`. The source (abs 75) and predication (abs 85) and presence bit are CONFIRMED; the sublane/stride sub-fields are located but not each pinned to an absolute width (HIGH). See [Memory-Store](slot-memory-store.md).

### Misc (struct `0x0C`) — `EncodeMiscInstruction` @ `0x1e86be80`

| Field | proto src | abs bit | Width | Confidence |
|---|---|---|---|---|
| predication | `& 0x1F` `<<13` | 13 | 5 | CONFIRMED |
| operand | `[instr+0x40]` `<<8` | 8 | — | HIGH |
| sub-op | `[instr+0x18]/[+0x1C]` `<<5` (per `MiscOperandEncoding`) | 5 | — | HIGH |

---

## The kNeverExecute Prefill

Before any slot is encoded, `EncodeBundleInternal` zeroes the slot region with two `ymm` stores and then stamps the predicate value `kNeverExecute = 31` (`0xB834CFC`) into every slot's 5-bit predicate field. The relevant prologue (decompiled, lines 98-109):

```c
// EncodeBundleInternal prologue  @ 0x1e86c7e0 (decompiled)
vmovups [struct+0x20] = 0;  vmovups [struct+0x14] = 0;     // clear the slot region
nx = kNeverExecute & 0x1F;                                  // = 31
*(uint64*)(struct+0x2D) = (nx << 53) | (nx << 26);          // scalar_0 @317, scalar_1 @290
*(uint16*)(struct+0x1D) = nx << 11;                         // VALU lane0 @147
*(uint32*)(struct+0x16) = nx << 5;                          // VALU lane1 opcode-region init
*(uint16*)(struct+0x1A) = 0x1F0;                            // = nx << 4  -> VALU lane1 pred @116
*(uint8 *)(struct+0x1C) = 0;                                // window high byte clear
*(uint64*)(struct+0x0C) = (0x400000800000000 * nx)          // bits 35 (MXU pred) + 58 (VLoad pred)
                        | (nx << 22)                        // VResult pred @22
                        | (nx << 13);                       // Misc predication @13
```

The constant `0x400000800000000` has exactly two set bits — bit 35 and bit 58 — so multiplying it by 31 places `kNeverExecute` at the MXU predicate (abs 35) and the VectorLoad predicate (abs 58). The struct `0x2D` store places it at the two scalar predicates (abs 317, abs 290), and the struct `0x1D`/`0x1A` stores at the two VALU-lane predicates (abs 147, abs 116). Because struct byte `0x0C` is bit 96, the qword-0 shifts read as their absolute bit positions directly (35, 58, 22, 13), which is the cleanest demonstration of the 12-byte-strip law.

| Prefill store | Sets predicate (abs bit) to 31 | Confidence |
|---|---|---|
| `[struct+0x2D] = (31<<53)\|(31<<26)` | scalar_0 @317 ; scalar_1 @290 | CONFIRMED |
| `[struct+0x1D] = 31<<11` | VALU lane0 @147 | CONFIRMED |
| `[struct+0x1A] = 0x1F0` | VALU lane1 @116 | CONFIRMED |
| `[struct+0x0C] = (31·0x400000800000000)\|(31<<22)\|(31<<13)` | VLoad @58 ; MXU @35 ; VResult @22 ; Misc @13 | CONFIRMED |

A present slot's writer overwrites its predicate with the real 5-bit `EncodePredication` value (0..14 predicate register, 15 `kAlwaysExecute`, +16 negate, 31 `kNeverExecute`); the constants `kPredicateRegisterCount=15` / `kAlwaysExecute=15` / `kNeverExecute=31` live at `0xB834CF4`..`0xB834CFC`. An absent slot keeps predicate 31 and the hardware skips it. See [NOP / Unused-Slot Canonical Encoding](nop-canonical.md) and [Predicate Slot](slot-predicate.md).

> **GOTCHA — empty is not all-zero.** The prefill is a *nonzero* stamp (31 replicated across several predicate fields). A reimplementation that `memset`s the bundle to zero and then fills only active slots leaves inactive slots at predicate 0, which is a valid predicate-register reference, turning empty slots into live garbage ops.

---

## The VLIW Packing Model

Above the byte encoder sits the model that decides *which* slot an LLO op occupies. The `Bundle` proto is the VLIW packing record: `slot_mask` at `proto+0x10`, per-slot sub-message pointers at `proto+0x18..+0x60`, and six 16-bit immediate fields at `proto+0x68..+0x7C`. An LLO op is folded into a slot by `JellyfishEmitter::FindFreeSlot<Inst, Opcode>`.

```c
// JellyfishEmitter::FindFreeVectorAluSlot(opcode)  @ 0x140b0f00
//   -> FindFreeSlot<VectorAluInstruction, VectorAluOpcode>  @ 0x140c09e0 (ecx = 3 candidate slots)
// 5 std::function callbacks drive the fold:
function FindFreeSlot(opcode, candidate_count, {legal, free, get_or_create, mark_used, err}):
    for slot in candidate_slots(candidate_count):     // 2 VALU lanes for the ALU case
        if !legal(opcode, slot):       continue       // (i)  op allowed in this BundleSlot?
        if !free(bundle, slot):        continue       // (ii) slot's has-bit clear in the Bundle?
        sub = get_or_create(bundle, slot)             // (iii) Arena::DefaultConstruct the submessage
        mark_used(bundle, slot)                        // (iv)  set the slot_mask bit @proto+0x10
        return sub                                     //       first free + legal slot
    return error(err(opcode))                          // (v)   "no free VALU slot" diagnostic
```

The five callbacks are passed as `std::function` thunks (`0x140c3680`/`.../0x140c38a0`). Once `FindFreeSlot` returns a sub-message pointer, the per-op `Emit*` populator (`EmitVectorBinop`, `EmitVectorMatmul`, `EmitScalarLoad`, …) fills the proto fields (opcode `@+0x50`, operands `@+0x58`/`+0x60`, predication), and `FinalizeCurrentBundle` (`0x140afc60`) commits the bundle for `EncodeBundleInternal` to serialize. The scalar path uses `FindFreeScalarSlot` (`0x140b2f40`) → `FindFreeSlot<ScalarInstruction, ScalarOpcode>` (`0x140c0180`), whose legality predicate enforces the branch/call-in-slot-0-only rule (see [Sequencer Slot](slot-sequencer.md)).

> **NOTE —** the BarnaCore address-handler personality uses a *static* analog of this fold: `MakeInstruction<Slot...>` (`0xfa961c0`..`0xfa96680`) = `EmptyInstruction` (`0x1416aa60`) followed by `InsertOperation(instr, slot)` (`0x1416a240`) once per element of a compile-time variadic tuple, each `InsertOperation` returning a `Status` (`FailedPrecondition` on a slot conflict). The 16 template instantiations enumerate the *legal slot-combos* at compile time, where the TensorCore path searches lanes at run time. The address-handler bundle is a separate 23-byte frame, not this 41-byte one.

---

## HBM Chunk Layout

The 41-byte width is the *issue* width. Stored in HBM, each bundle occupies a slightly larger slot and bundles are grouped into 128-byte DMA chunks. `JellyfishCodecMetadata::BundleSizeBytesForHbm` (`0x1ecf74c0`) returns **42** for the TensorCore — one byte over the 41-byte issue bundle. `EncoderJf::GetBundleByteOffset` (`0x1e86db80`) computes a bundle's offset inside a chunk:

```c
// EncoderJf::GetBundleByteOffset(int n)  @ 0x1e86db80 (decompiled)
if (n < 0 || n >= bundles_per_chunk)
    return error("Requested bundle number N exceeds limit of "
                 "bundles-per-chunk (...)");        // encoder_jf.cc:3202
offset = (n / 3) * 128                              // 3 bundles per 128-byte chunk
       + (n % 3) * (BundleSizeBytes() + 2);         // 41 + 2 = 43-byte in-chunk stride
```

| Quantity | Value | Source | Confidence |
|---|---|---|---|
| Issue bundle | 41 B | `BundleSizeBytes` @ `0x1ecf7460` | CONFIRMED |
| HBM stored bundle | 42 B | `BundleSizeBytesForHbm` @ `0x1ecf74c0` | CONFIRMED |
| In-chunk stride | 43 B (`BundleSizeBytes()+2`) | `GetBundleByteOffset` @ `0x1e86db80` | CONFIRMED |
| Bundles per chunk | 3 | `(n/3)*128` grouping | CONFIRMED |
| Chunk granularity | 128 B | `(n/3)*128` | CONFIRMED |

> **GOTCHA —** the in-chunk stride is `BundleSizeBytes()+2 = 43`, *not* the HBM bundle width 42. The two differ by one byte: `BundleSizeBytesForHbm` reports the stored bundle as 42 (a single check/pad byte over the 41-byte issue width), while `GetBundleByteOffset` advances by 43 between consecutive bundles in a chunk. The extra byte is consistent with a check byte plus one pad byte, or a 2-byte trailer; the exact per-bundle framing bytes (the check byte `0x55` and any pad) are written by the program-level `EncodeProgramForHbmInternal` and are not pinned here.

---

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the per-generation bundle widths and the slot taxonomy this page instantiates for Jellyfish.
- [MXU Slot](slot-mxu.md) — the matmul/matpush/latch fields of the vector-extended slot and the per-gen MXU twin.
- [Record Format](record-format.md) — the 239-bit MC `APInt`; why the Jellyfish path is proto-direct and bypasses it.
- [MC-Emitter](mc-emitter.md) — `getBinaryCodeForInstr` and the MC pipeline that Jellyfish does *not* route bundle bytes through.
- [Dragonfish Bundle](bundle-df.md) — the gen-1 codec that inherits this exact `EncodeBundleInternal` and 41-byte layout.
- [MXU Latency: JF / DF](../cost/mxu-latency-jf-df.md) — the cost model that consumes the MXU-slot fields decoded here.
