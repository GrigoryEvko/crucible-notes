# TEC Vector Opcode Enumeration

> *Every opcode value, mask immediate, field width, and per-generation count on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

The TEC ([Tile Execute Core](tec-engine.md)) is the SparseCore vector datapath, and `VectorAlu` is its compute slot — three concurrent lanes (`VectorAlu0/1/2`) in one 64-byte bundle, each carrying one element-wise / reduction / convert op. This page is the opcode *roster* companion to the [TEC Engine](tec-engine.md) page, which owns the 64-byte bundle, the per-lane slot bases (`@438/401/364`), and the 37-bit slot template; here we enumerate every `VectorAlu` operation, its integer opcode value, its emission template, and the per-generation form split. It is the vector-side analog of the [SCS scalar opcode roster](scalar-opcode-enum.md), and it recovers from the same evidence: there is no opcode-name string table, so each value comes from a per-op `Matches()` compare immediate and from the consumer jump table that maps `MCInst` opcode → proto field → emitter template.

The roster has **two independent recoveries that must not be confused**. First, the per-op match predicate: libtpu emits one C++ type per opcode per generation — `SparseCore<Slot><OpName>Opcode` — and each carries a `Matches() const` that masks the opcode field out of the decoded-instruction word and compares it against that op's signature. The union of these types across the three lanes is **148 ops on Viperfish, 229 on Ghostlite, 257 on 6acc60406 (gfc)** — the proto-enum op count that grows gen-over-gen. Second, the SC-MLO emitter's `ConsumeVectorAluInstruction` consumer: a jump table keyed on the `MCInst` opcode that dispatches each op to `_internal_mutable_<op>()` and a tail `Emit*` template. The Ghostlite consumer's table has **142 distinct ops** (135 single-op arms + 7 reached through one shared f32-compare/move oneof chain); the table is the reimplementer's authoritative opcode→op→emitter map. The 142 (jt arms) and 229 (Matches-type union) count the same ISA at two granularities: the jt folds the per-predicate / per-lane-width / dtype-merged forms into multi-opcode arms.

The opcode field is **8-bit on Ghostlite/6acc60406 (`VectorAlu0` op `@462`), 7-bit on Viperfish (`@456`)** — the width bump that carries the 148→229 op growth past the 7-bit (128) ceiling, and the reason the VF lane is 36 bits where GF/GL are 37. The opcode space is itself two-level: most values name a concrete op directly (`VectorAddS32=3`, `ByteNez=55`), but a handful are *group escapes* — `0/1/2` (unary EUP/convert), `27` (pack), `90/128` (vmask) — whose concrete member lives in a 6-bit sub-opcode field, and `VectorSelect`/`VectorSelectNot` live in a 4-bit select sub-field that on Viperfish was 32 explicit `VectorSelectVmsk0..15` opcodes. This page documents the opcode-recovery model, the per-lane slot opcode-field widths, the GF direct roster with integer values, the unary/pack/vmask group escapes, the eight emission templates, the FP-vs-int form split, and the per-generation deltas.

For reimplementation, the contract is:

- **The opcode→op map is the `ConsumeVectorAluInstruction` jump table (Ghostlite, 142 ops).** Index `opcode − 0xb26`, bound `0x5cf`; each arm calls `_internal_mutable_<op>()` and tail-jumps one `Emit*` template. The seven f32 compares + `VectorMove` share one oneof-dispatch chain. The 6acc60406 (gfc) per-op `Matches()` predicate is the silicon-bit cross-check.
- **The per-lane opcode-field widths and bit positions.** GF/GL: 8-bit, `VectorAlu0 @462`, `VectorAlu1 @425`, `VectorAlu2 @388` (= slot_base + 24). VF: 7-bit, lane 0 `@456`. The opcode *value* is lane-invariant (`ByteNez=55` on all three lanes); only the bundle bit differs.
- **The eight emission templates.** `EmitVectorBinop` (the ~92-op arithmetic/compare/bitwise/shift core), `EmitVectorVxUnop` (unary + all unpack), `EmitExtendedVectorVxUnop` (the 18 transcendentals), `EmitVectorVyUnop`, `EmitVectorModifyMask{Unop,Binop}` (vmsk), `EmitVectorSelect`, `EmitPackVectorBinop`, `EmitCrossLaneUnop`, `EmitVectorLaneLeftShiftInsert`. The template determines the operand-port encoding the scheduler must satisfy.
- **The FP-vs-int form split and the VF→GF rename.** GF names every op by dtype (`VectorAddS32` vs `VectorAddBf16` vs `VectorAddS16`); VF used dtype-merged generic names (`VectorFloatAdd`) plus 16 explicit `VectorSelectVmsk*` opcodes that GF folded into a 4-bit sub-field. GF added `cosq`/`erf`/`sinq` and the FP8/Exmy small-float pack family that VF lacks.

| | |
|---|---|
| **Slot** | `VectorAlu` ×3 lanes (`VectorAlu0` op `@462`, `VectorAlu1 @425`, `VectorAlu2 @388`, GF) |
| **Opcode width** | 8-bit (GF/GL) · 7-bit (VF) |
| **Opcode→mnemonic source** | per-op `SparseCore<Slot><OpName>Opcode::Matches()` immediate + `ConsumeVectorAluInstruction` jt |
| **Matches-type union (per gen)** | VF **148** · GL **229** · GF **257** |
| **Ghostlite consumer jt ops** | **142** (135 single-op arms + 7 shared f32-cmp/move chain) |
| **Consumer entry** | `ConsumeVectorAluInstruction<glc::SparseCoreTecBundle>` `0x13a0b580`; jt `0xae9d3dc`, base `0xb26`, bound `0x5cf` |
| **Group escapes** | unary `0/1/2` (6-bit sub) · pack `27` · vmsk `90/128` · `VectorSelect` (4-bit sub) |
| **Emission templates** | `EmitVectorBinop` · `…VxUnop` · `…ExtendedVxUnop` · `…VyUnop` · `…ModifyMask{U,B}` · `…Select` · `EmitPackVectorBinop` · `…CrossLaneUnop` · `…LaneLeftShiftInsert` |
| **Gen-invariance** | shared op values byte-identical VF/GL/GF (`ByteNez=55` on all) |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

> **NOTE — this page enumerates the `VectorAlu` opcode roster; the bundle byte layout lives in [TEC Engine](tec-engine.md).** The 64-byte bundle, the three lane slot bases (`@438/401/364`), the 37-bit slot template (four 6-bit VREG selectors, the 8-bit opcode `@+24`, the overlapped predication header `@+32`), and the no-check-trailer rule are documented there and not repeated. The `MCInst`→slot routing for the *scalar* slots is the [OneSlot Scalar Router](oneslot-router.md); the `VectorAlu` dispatch is the separate `ConsumeVectorAluInstruction` consumer documented here. The `VectorLoad`, `VectorStore`, `VectorResult`, and `VectorExtended` slots have their own opcode rosters on sibling pages.

---

## The Opcode-Recovery Model

### Why there are two op counts

The `VectorAlu` ISA is recoverable along two independent axes, and a reimplementer must keep them apart.

The first is the **per-op match predicate**, identical in shape to the [SCS scalar predicates](scalar-opcode-enum.md#the-opcode-predicate-model). Each opcode is a distinct empty C++ class `SparseCoreTecVectorAlu<N><OpName>Opcode` (one per lane `N ∈ {0,1,2}`, per gen) carrying a `Matches() const` that masks the opcode field from the decoded-instruction word and compares it against the op's signature. The signature is the compare immediate; the value is the silicon opcode. Counting the *distinct op names* across the three lanes gives the proto-enum op count — **148 (vfc) / 229 (glc) / 257 (gfc)** — confirmed by symbol enumeration.

The second is the **emitter consumer**, `ConsumeVectorAluInstruction`, the function the SC-MLO code generator calls to lower an `MCInst` into the bundle's `SparseCoreTecVectorAlu` proto. It is a jump table on the `MCInst` opcode: `index = opcode − 0xb26`, bound `0x5cf` (1488 entries on Ghostlite). Each non-default arm calls `SparseCoreTecVectorAlu::_internal_mutable_<op>()` to select the proto oneof field, then tail-jumps one `Emit*` template. Counting the distinct ops the Ghostlite table reaches gives **142** (135 single-op arms + 7 via a shared chain). The jt count is lower than the Matches-type count because the jt folds the per-predicate / per-lane-width / dtype-merged `MCInst` forms into multi-opcode arms.

```text
two recoveries of one ISA
  Matches() types          ConsumeVectorAluInstruction jt
  (one per op, per lane)   (one arm per reachable op)
  vfc 148 / glc 229 /      glc 142 arms = 135 single-op
  gfc 257  (proto enum)      + 7 shared f32-cmp/move
        │                          │
        └── silicon opcode ────────┘  agree: ByteNez=55, VectorAddS32=3, …
            (the BitCopy value the encoder writes @462 = the cmp value Matches() reads back)
```

> **QUIRK — the consumer jump table is Ghostlite-only; 6acc60406 (gfc) has no `ConsumeVectorAluInstruction`.** The `MCInst`→op jt-dispatch consumer exists for `viperfish` and `ghostlite` namespaces only. The gfc (6acc60406) emitter is not wired through `MakeTpuCoreProgram` in this wheel — only its `Matches()` predicate types and `<Slot>Encoder::Encode` bodies are present. So the **byte-exact opcode→op enumeration here is the Ghostlite 142-op table**; the **257-op gfc roster is the symbol-union count plus the GF-only ops named below** (the FP8/Exmy small-float family), recovered from `Matches()` types and the lane-0 encoder, not from a jt. A reimplementer targeting gfc (6acc60406) uses the Ghostlite jt as the structural template and adds the GF-only ops.

### The match predicate — form C against the slot word

The vector slots all use the mask-compare predicate (form C in the [scalar taxonomy](scalar-opcode-enum.md#form-c-mask-compare-the-alu-lanes)): a single AND-mask isolates the opcode field from the slot word, and the result is compared to `VAL`; the opcode is `VAL >> tzcnt(MASK)`. `ByteNez` (op 55) on the gfc lane-0 type (`0x1ec09e00`):

```c
// SparseCoreTecVectorAlu0ByteNezOpcode::Matches  (gfc 0x1ec09e00)
return (dword_0x40 & 0x3FC000) == 0xDC000;     // 0xDC000 >> 14 = 0x37 = 55 ; field bits 14..21 (8-bit)
```

The same op on Viperfish (`0x1e94fd00`) is a 7-bit field one bit lower:

```c
// vxc::vfc …VectorAlu0ByteNezOpcode::Matches  (0x1e94fd00)
return (dword_0x40 & 0x7F00) == 0x3700;        // 0x3700 >> 8 = 0x37 = 55 ; field bits 8..14 (7-bit)
```

And the same op on a *different lane* (gfc lane 1, `0x1ec4a6a0`) reads the same value from a different word position:

```c
// gfc …VectorAlu1ByteNezOpcode::Matches  (0x1ec4a6a0)
return (qword_0x38 & 0x1FE0000000000) == 0x6E0000000000;   // 0x6E0…>>41 = 55 ; field bits 41..48
```

Three facts fall out, and they are the spine of the whole roster: (a) the opcode **value** is lane-invariant — `ByteNez=55` on all three lanes and all three gens; (b) only the **bit position** changes per lane (lane 0 lower in the word, lanes 1/2 higher), matching the `@462`/`@425`/`@388` bundle-bit split; (c) the **field width** is 8 bits on GF/GL and 7 on VF.

> **QUIRK — the encoder's `switch` case labels are the proto-oneof tags, not the opcode values.** Inside the gfc `VectorAlu0Encoder::Encode` (`0x1ec11100`), the dispatch `switch(*(msg+88))` uses sequential proto-oneof case labels: `case 8 → BitCopy(.,462,.,8) = 3` (`VectorAddS32`), `case 9 → 87` (`VectorAddS16`), `case 10 → 4` (`VectorSubtractS32`), `case 11 → 88` (`VectorSubtractS16`). The case number is the oneof field tag; the value written into the bundle opcode field at bit 462 is the silicon opcode. A reimplementer who reads the `switch` case numbers as opcodes will mis-encode every op. The values on this page are the `BitCopy`/`Matches()` hardware values, exactly as on the [scalar page](scalar-opcode-enum.md).

### The slot opcode-field bit positions

Confirmed byte-exact against the gfc `VectorAlu0Encoder::Encode` (`0x1ec11100`), whose `BitCopy` calls write four 6-bit VREG selectors, the 8-bit opcode, and the predication header at fixed absolute bundle bits. The three lanes share one template (see [TEC Engine](tec-engine.md#the-37-bit-vector-alu-slot-template)); only the slot base shifts.

| Lane | Slot base (GF) | VREG selectors | Opcode bit | Opcode width | Pred header | Confidence |
|---|---:|---:|---:|---:|---:|---|
| `VectorAlu0` | 438 | 438 / 444 / 450 / 456 | **462** | 8 | 470 / 473 / 474 | CONFIRMED |
| `VectorAlu1` | 401 | 401 / 407 / 413 / 419 | **425** | 8 | 433 / 436 / 437 | CONFIRMED |
| `VectorAlu2` | 364 | 364 / 370 / 376 / 382 | **388** | 8 | 396 / 399 / 400 | CONFIRMED |
| `VectorAlu0` (VF) | 432 | 432 / 438 / 444 / 450 | **456** | 7 | 463 / 467 | HIGH |

The opcode always lands at `slot_base + 24`. The Viperfish lane is one bit narrower (7-bit opcode → 36-bit slot), so its lane-0 opcode sits at `@456` rather than `@462`; the VF predication header is the single-channel 4-bit form rather than GF's 3+4 overlap (see [TEC Engine](tec-engine.md#the-37-bit-vector-alu-slot-template)).

---

## The Ghostlite `VectorAlu` Consumer Jump Table

### Dispatch shape

`ConsumeVectorAluInstruction<glc::SparseCoreTecBundle>` (`0x13a0b580`) is the authoritative opcode→op→emitter map. It reads the `MCInst` opcode, indexes `opcode − 0xb26` against the jump table at `0xae9d3dc` (bound `0x5cf` = 1488 entries), and dispatches to one of 143 targets: 142 op arms + one default-error arm covering 1213 opcodes.

```c
function ConsumeVectorAluInstruction(printer, mcinst, vregports, ...):   // glc 0x13a0b580
    opcode = mcinst.opcode                       // DWORD[mcinst]
    idx = opcode - 0xb26                          // jt base 0xb26
    if (unsigned)idx > 0x5cf:                      // bound check
        return MakeError("Unsupported opcode for Vector Alu slot: $0 : $1")  // isa_emitter.cc
    switch (jt[idx]):                              // jt @0xae9d3dc, 1488×int32 rel offsets
        // 135 single-op arms, e.g.:
        case 0xb26:  proto.mutable_vector_add_bf16();  return EmitVectorBinop<…VectorAddBf16>(mcinst)
        case 0xc9f:  proto.mutable_cosq_f32();          return EmitExtendedVectorVxUnop<…CosqF32>(mcinst)
        case 0xf2e:  proto.mutable_tanh_f32();          return EmitExtendedVectorVxUnop<…TanhF32>(mcinst)
        case 0xe87:  GetOperandAndVsEncoding(…);
                     proto.mutable_pack_compressed_b16_to_b8();
                     return EmitPackVectorBinop<…PackCompressedB16ToB8>(mcinst)
        // 7 ops via ONE shared f32-cmp/move chain, selected by the proto oneof tag [proto+0x50]:
        case 0xcd2: case 0xd56: … : goto shared_cmp_move   // 0x28→EqF32 … 0x2d→LteF32 ; 0x1a→VectorMove
        default:    return MakeError(…)            // 1213 opcodes
```

The "`..`" in the opcode column of the roster below marks a multi-opcode arm: the same proto op and `Emit*` template is reached from 2 or 3 adjacent `MCInst` opcodes (the predicated / non-predicated / lane-width forms). The opcode→op mapping is byte-exact; the per-opcode operand-form decode within a multi-opcode arm is not (filled by `GetOperandAndVsEncoding` / `GetVregno`, not traced).

### The 142-op tally

| Family | Count | Notes |
|---|---:|---|
| Integer/float core VALU | ~92 | add/sub/mul/min/max/compare/bitwise/shift/clamp/relux/classify/select/convert/carry/total/broadcast/permute/popcount/clz/lane_id/byte_nez/create_mask/inf_or_nan + vmsk ALU + mask scan/reduce; **includes the 7 f32-cmp/move shared-chain ops** |
| Transcendental | 18 | 9 families × {f32, bf16}: `cosq` `erf` `log_two` `pow_two` `reciprocal` `reciprocal_sqrt` `shifted_sigmoid` `sinq` `tanh` |
| Quant pack/unpack | 32 | 6 `pack_*` + 24 `unpack_*` + 2 `vmsk_pack_{even,low}` |
| **Total** | **142** | 135 single-op jt arms + 7 shared-chain ops |

The tally is byte-confirmed: the consumer body references exactly **135 distinct `_internal_mutable_<op>()` accessors** (the single-op arms) plus the seven f32-compare/move ops reached through the oneof chain. The `Emit*` template distribution in the same body is **66 `EmitVectorBinop` · 34 `EmitVectorVxUnop` · 18 `EmitExtendedVectorVxUnop` · 6 `EmitPackVectorBinop` · 7 `EmitVectorModifyMask{U,B}` · 4 `EmitCrossLaneUnop` · 3 `EmitVectorVyUnop` · 2 `EmitVectorSelect` · 1 `EmitVectorLaneLeftShiftInsert`** — and the 18 `EmitExtendedVectorVxUnop` are exactly the 18 transcendentals.

> **GOTCHA — the seven f32 compares and `VectorMove` are NOT seven distinct jt arms; they share one oneof-dispatch chain.** The opcodes `{0xcd2,0xcd6,0xcda,0xd56,…}` and the `VectorMove` opcodes all land on one handler that switches on the proto oneof discriminator at `[proto+0x50]`: `0x28→VectorEqF32`, `0x29→VectorNeqF32`, `0x2a→VectorGtF32`, `0x2b→VectorGteF32`, `0x2c→VectorLtF32`, `0x2d→VectorLteF32` (all `EmitVectorBinop`), `0x1a→VectorMove` (`EmitVectorVyUnop`). A reimplementer counting jt arms gets 135; counting reachable ops gets 142. The 7 are real ops with real opcode values — they just don't have private arms.

---

## The GF Direct Opcode Roster

### Arithmetic, compare, bitwise, shift core (`EmitVectorBinop`)

The bulk of the ISA is direct binary ops whose opcode is the field value. Values are gen-invariant for the integer/bitwise ops (`VectorBitwiseAnd=6`, `ByteNez=55` byte-identical VF/GF). The dtype suffix is part of the opcode, not an operand — `VectorAddS32`, `VectorAddS16`, `VectorAddBf16`, `VectorAddF32` are four distinct opcodes.

| Opcode | Mnemonic | Opcode | Mnemonic | Confidence |
|---:|---|---:|---|---|
| 3 | `VectorAddS32` | 32 | `VectorMultiplyBf16` | CONFIRMED |
| 4 | `VectorSubtractS32` | 33 | `VectorMaxBf16` | CONFIRMED |
| 5 | `VectorMultiplyU32` | 34 | `VectorMinBf16` | CONFIRMED |
| 6 | `VectorBitwiseAnd` | 44 | `VectorCarryU32` | CONFIRMED |
| 7 | `VectorBitwiseOr` | 45 | `VectorBitwiseAndn` | CONFIRMED |
| 8 | `VectorBitwiseXor` | 55 | `ByteNez` | CONFIRMED |
| 9 | `VectorLogicalShiftLeft` | 84 | `VectorMaxU32` | CONFIRMED |
| 10 | `VectorLogicalShiftRight` | 85 | `VectorMinU32` | CONFIRMED |
| 11 | `VectorArithmeticShiftRight` | 86 | `VectorMultiplyReturningHighHalfU32` | CONFIRMED |
| 14 | `VectorMultiplyF32` | 87 | `VectorAddS16` | CONFIRMED |
| 15 | `VectorMaxF32` | 88 | `VectorSubtractS16` | CONFIRMED |
| 16 | `VectorMinF32` | 89 | `VectorMultiplyU16` | CONFIRMED |
| 17 | `VectorReluxF32` | 56 | `VectorMaxU16` | HIGH |
| 18 | `VectorClampF32` | 57 | `VectorMinU16` | HIGH |
| 22 | `VectorMove` | 75 | `VectorCarryU16` | HIGH |

`VectorAddS32=3`, `VectorAddS16=87`, `VectorSubtractS32=4`, `VectorSubtractS16=88` are confirmed against the gfc lane-0 encoder, whose proto-oneof switch writes those exact values at bit 462. `ByteNez=55` is confirmed against the `Matches()` immediate on all three lanes and gens.

### The compare blocks (per-dtype, dense)

The compares are laid out as dense per-dtype runs of `{Eq, Neq, Gt, Gte, Lt, Lte}`. The S32/S16/U32/U16/Bf16 blocks are direct opcodes; the **F32 block is the shared oneof chain** (opcodes `0x28..0x2d`), not direct values.

| Block | Opcodes | Ops | Confidence |
|---|---|---|---|
| S16 compare | 65 / 66 / 67 / 68 / 69 / 70 | `Eq Neq Gt Gte Lt Lte S16` | CONFIRMED |
| S32 compare | 38 / 39 / 40 / 41 / 42 / 43 | `Eq Neq Gt Gte Lt Lte S32` | CONFIRMED |
| U32 compare | 80 / 81 / 82 / 83 | `Gt Gte Lt Lte U32` | CONFIRMED |
| U16 compare | 71 / 72 / 73 / 74 | `Gt Gte Lt Lte U16` | CONFIRMED |
| Bf16 compare | 76 / 77 / 78 / 79 | `Eq Neq Gt Gte … Bf16` | CONFIRMED |
| F32 compare | `0x28..0x2d` (oneof) | `Eq Neq Gt Gte Lt Lte F32` | CONFIRMED |
| F32 total | 53 / 54 | `VectorTotalLtF32` / `VectorTotalLteF32` | CONFIRMED |
| Bf16 total | 26 / 36 | `VectorTotalLtBf16` / `VectorTotalLteBf16` | HIGH |

### Cross-lane, mask, and broadcast/permute

The high-opcode region is the cross-lane and mask group: broadcast, rotate, permute, lane-shift-insert, and the vmsk ALU. Several use `EmitCrossLaneUnop` or `EmitVectorModifyMask*` rather than `EmitVectorBinop`.

| Opcode | Mnemonic | Template | Confidence |
|---:|---|---|---|
| 52 | `CreateMask` | `EmitVectorVyUnop` | CONFIRMED |
| 91 / 92 / 93 | `VmskAnd` / `VmskOr` / `VmskXor` | `EmitVectorModifyMaskBinop` | CONFIRMED |
| 94 | `VmskPackLow` | `EmitVectorModifyMaskBinop` | CONFIRMED |
| 138 | `VmskPackEven` | `EmitVectorModifyMaskBinop` | HIGH |
| 129 / 130 | `VectorBroadcastB32` / `…B16` | `EmitVectorBinop` | CONFIRMED |
| 131 / 132 | `VectorRotateB32` / `…B16` | `EmitVectorBinop` | HIGH |
| 133 / 134 / 135 | `VectorPermuteB32` / `…B16` / `…B8` | `EmitVectorBinop` | CONFIRMED |
| 136 / 137 | `VectorLaneLeftShiftInsertB32` / `…B16` | `EmitVectorLaneLeftShiftInsert` | CONFIRMED |
| 139 / 140 / 141 | `VectorMaskPermuteB32` / `…B16` / `…B8` | `EmitCrossLaneUnop` | HIGH |

> **NOTE — the high opcodes (129..151) are GF/GL-only and several are gfc-only.** The broadcast/rotate/permute/lane-shift block at 129..141 and the FP8/Exmy pack family at 142..151 are 8-bit values (> 127) that cannot exist in the 7-bit Viperfish space. They are part of the 257-op gfc (6acc60406) union but above the Ghostlite jt's reachable arms for the FP8 family; the Ghostlite jt reaches `vector_permute_b32` (`0x10e8`), `vector_broadcast_b32` (`0x107b`), and the mask-permute / cross-lane ops, but the FP8 `PackCompressed*ToExmy` ops are gfc-only (see [Per-Generation Deltas](#per-generation-deltas)).

---

## The Group Escapes and Sub-Opcodes

A few primary opcodes do not name a concrete op; they name a *group*, and a 6-bit sub-opcode field (at struct-word `0x40` bit 2) picks the member. This is how the EUP transcendentals and the full pack/unpack matrix fit a finite primary-opcode space. The escape primaries are `0` (unary float), `1` (unpack-to-32), `2` (unpack-to-16 / round), `27` (pack), `90` (vmsk move), `128` (vmsk-count / E-format unpack); `VectorSelect`/`VectorSelectNot` use a separate 4-bit select sub-field at bit 18.

| Primary | Group | Sub field | Representative members (sub value) | Confidence |
|---:|---|---|---|---|
| 0 | unary float / convert | 6-bit `@bit2` | `VectorPopulationCount(1)`, `VectorCountLeadingZeros(2)`, `VectorCeilingF32(3)`, `VectorFloorF32(4)`, `VectorConvertS32ToF32(5)`, `VectorConvertF32ToS32(6)`, `ErfF32(14)`, `LogTwoF32(18)`, `TanhF32(19)`, `ReciprocalF32(21)`, `SinqF32(23)`, `CosqF32(24)` | HIGH |
| 1 | unpack-to-32 | 6-bit `@bit2` | `Unpack{Compressed,Interleaved}{Bf16,Hf16,S16,E5m2,S8,…}…ToF32/ToS32` (32 sub-ops) | HIGH |
| 2 | unpack-to-16 / round | 6-bit `@bit2` | `UnpackCompressed{S8,U8}…To{Bf16,S16,U16}`, `VectorRoundToIntegral{Even,Away}{F32,Bf16}` (32 sub-ops) | HIGH |
| 27 | pack | 6-bit `@bit2` | `Pack{Interleaved,Compressed}{F32→Bf16,B32→B16,B16→B8,…}`, `VectorTruncateFractional{F32,Bf16}` (32 sub-ops) | HIGH |
| 90 | vmsk move | 6-bit `@bit2` | `VmskMove(0)`, `VmskNegate(1)` | HIGH |
| 128 | vmsk-count / E-unpack | 6-bit `@bit2` | `VectorMaskPopulationCountB32(0)/B16(1)`, `VectorMaskPrefixSumB32(2)/B16(3)`, `VectorMaskCountTrailingZerosB32(4)/B16(5)`, `UnpackCompressed{E5m2,E4m3}…ToBf16` | HIGH |
| — | select | 4-bit `@bit18` | `VectorSelect(6)`, `VectorSelectNot(7)` | HIGH |

> **NOTE — the Ghostlite jt reaches the group members directly, so the page lists them under flat opcodes too.** The escape model above is the *encoding* (how the silicon opcode field stores a group + sub-opcode); the Ghostlite consumer jt indexes the full `MCInst` opcode space and reaches each member through its own arm (`cosq_f32 @0xc9f`, `pack_compressed_b16_to_b8 @0xe87`, the 24 `unpack_compressed_*` arms). A reimplementer encoding to silicon writes the group primary + the 6-bit sub; one driving off the `MCInst` jt uses the per-member arm. The two views are the same op set; the sub-opcode `@bit2` is the bridge.

> **GOTCHA — `VectorSelect`/`VectorSelectNot` are one opcode with a 4-bit sub-field; on Viperfish they were 32 explicit opcodes.** GF encodes the predicate-mask select as `VectorSelect`/`VectorSelectNot` plus a 4-bit select sub-field at struct bit 18 (16 mask sources). Viperfish instead had 16 explicit `VectorSelectVmsk0..15` (opcodes 96..111) + 16 `VectorSelectNotVmsk0..15` (opcodes 112..127), the full 96..127 range. Folding those 32 opcodes into a 4-bit sub-field is the saving that funded the GF transcendental and pack-family expansion within the 8-bit space. A reimplementer must not emit 32 distinct select opcodes on GF/GL, nor a 4-bit select sub-field on VF.

---

## The Eight Emission Templates

The consumer routes each op through one of eight `Emit*` templates. The template is not cosmetic — it fixes which read ports (Vx / Vy) the op consumes and therefore the `SparsecoreVregReadPort` conflict the bundle scheduler must satisfy across the three lanes (see [TEC Engine](tec-engine.md#immediate-slot-indexing)).

| Template | Ops it serves | Count (glc) | Confidence |
|---|---|---:|---|
| `EmitVectorBinop<…VregReadPort>` | the arithmetic/compare/bitwise/shift/clamp/relux/classify/carry/total/broadcast/permute core | 66 | CONFIRMED |
| `EmitVectorVxUnop` | ceiling/floor/convert/popcount/clz/lane_id/inf_or_nan + **all `unpack_*`** | 34 | CONFIRMED |
| `EmitExtendedVectorVxUnop` | the **18 transcendentals** (`cosq`/`erf`/`log_two`/`pow_two`/`reciprocal`/`reciprocal_sqrt`/`shifted_sigmoid`/`sinq`/`tanh` × {f32,bf16}) | 18 | CONFIRMED |
| `EmitVectorVyUnop` | `byte_nez`, `create_mask`, `VectorMove` (Vy read port) | 3 | CONFIRMED |
| `EmitVectorModifyMask{Unop,Binop}` | the `vmsk_*` ALU (`and/or/xor/negate/move/pack_even/pack_low`) | 7 | CONFIRMED |
| `EmitVectorSelect` | `vector_select`, `vector_select_not` | 2 | CONFIRMED |
| `EmitPackVectorBinop` | the 6 `pack_*` ops (each first calls `GetOperandAndVsEncoding`) | 6 | CONFIRMED |
| `EmitCrossLaneUnop` | `vector_mask_{count_trailing_zeros,population_count_b16/b32,prefix_sum}_b32` | 4 | CONFIRMED |
| `EmitVectorLaneLeftShiftInsert` | `vector_lane_left_shift_insert_b32` | 1 | CONFIRMED |

> **NOTE — `EmitExtendedVectorVxUnop` is the transcendental path and routes through the EUP pipeline.** The 18 ops served by this template are the SC's nonlinear-activation engine — they take a Vx read port, run on the extended (EUP) pipeline, and their results drain through the [VectorResult](tec-engine.md) slot, the same way the [VectorExtended](vectorextended-vex.md) scans do. A reimplementer must model these as multi-cycle EUP ops distinct from the single-cycle `EmitVectorBinop` arithmetic; the per-op operand→function→result binding is the largest remaining gap (the unary-group sub-opcode `@bit2` selects the function but the VREG selectors it consumes were not traced).

---

## The Sibling Vector Slots (op counts)

`VectorAlu` is one of five vector slots; the other four have their own opcode rosters on sibling pages. They are summarized here for completeness; the integer values live on the linked pages.

| Slot | Opcodes (GF) | Roster shape | Page |
|---|---:|---|---|
| `VectorAlu` ×3 | **257** (GF union) / 142 (GL jt) | this page | — |
| `VectorLoad` | 5 | `TileSpmemLoad{,CircularBuffer,CircularBufferPostUpdate,Indexed,IndexedCircularBuffer}` (3-bit field) | [VectorLoad Slot](vectorload-slot.md) |
| `VectorStore` | 33 | `TileSpmemStore` base + 32 typed `Add`/`CircularBuffer`/`Indexed` scatter variants (6-bit field) | [VectorStore Slot](vectorstore-slot.md) |
| `VectorResult` | 8 | `EupResult`, `PopXrfWriteAll`, `PopXrfWritePartial0..4`, `VresMove` (3-bit field) | [TEC Engine](tec-engine.md) |
| `VectorExtended` | 53 | `AddScan*`/`MinScan*`/`MaxScan*`/`*IndexScan*`/`Segmented*`/`Sort*`/`Uniquify*`/`DuplicateCount*` (6-bit field) | [VectorExtended (VEX)](vectorextended-vex.md) |

> **GOTCHA — `VectorStoreAdd*` is atomic scatter-add, encoded in the opcode, not an operand.** As on the [VectorStore](vectorstore-slot.md) page, the per-dtype `…StoreAdd{S32,F32,S16,Bf16}` variants accumulate into `TILE_SPMEM` — the embedding-gradient write path — and the dtype is part of the opcode. The same dtype-as-opcode rule governs `VectorAlu`: there is no element-type operand field; `VectorAddS32`/`VectorAddBf16`/`VectorAddS16`/`VectorAddF32` are four opcodes.

---

## Per-Generation Deltas

The `VectorAlu` ISA grows VF→GL→GF, and the growth is concentrated in three places: the opcode field width, the transcendental set, and the dtype/select form split. Shared op values are gen-invariant (`ByteNez=55`, `VectorAddS32=3` byte-identical across gens); the deltas are the *added* ops and the *renamed* forms.

| Aspect | Viperfish (vfc) | Ghostlite (glc) | 6acc60406 (gfc) |
|---|---|---|---|
| Opcode field width / lane-0 bit | 7-bit / `@456` | 8-bit / `@462` | 8-bit / `@462` |
| `Matches`-type union (3 lanes) | **148** | **229** | **257** |
| Consumer ops (distinct accessors) | 86 | **142** (135 single + 7 shared) | (no consumer in this wheel) |
| Max primary opcode | 127 (`VectorSelectNotVmsk15`, full 7-bit) | ~250 | 151 |
| Transcendental families | 6 (dtype-merged: `tanh`, `reciprocal`, `reciprocal_sqrt`, `log_two`, `pow_two`, `shifted_sigmoid`) | 9 × {f32,bf16} = 18 | 18 + |
| `cosq` / `erf` / `sinq` | **absent** (0 refs in body) | **present** (6 ops) | present |
| Op naming | dtype-merged generic (`VectorFloatAdd`, `tanh`) | dtype-suffixed split (`VectorAddF32`, `tanh_f32`) | dtype-suffixed split |
| Select forms | 32 explicit `VectorSelect[Not]Vmsk0..15` (op 96..127) | folded into 4-bit select sub-field | 4-bit select sub-field |
| FP8 / Exmy small-float | absent | partial (pack/unpack matrix) | **`VectorConvertStochasticF32ToE5m2`, `PackCompressedF32ToExmy`, `VectorConvertExmyToE4m3`, `UnpackCompressedExmyLanes07ToBf16`** (GF-only) |
| Inline-immediate path | oneof tag `0x13` → `SparseCoreImmediates` (2 shared arms) | folded into cmp/move/total datapath | (n/a) |

The presence claims are confirmed by the existence of the corresponding type / accessor in each gen namespace. The Viperfish consumer body (`0x1399ffe0`) has **zero references to `cosq`/`erf`/`sinq`** but does reference `tanh` (the dtype-merged single op) — the +3 transcendental families are a Ghostlite addition. Viperfish has 4 references to `SparseCoreImmediates` and 2 to `GetVectorYAndEmitImmediate`: its oneof-tag-`0x13` form routes an inline vector immediate through `GetVectorYAndEmitImmediate`, a path Ghostlite folded into the compare/move/total chain.

> **CORRECTION (VEC-DELTA) —** an earlier hypothesis held that Viperfish *lacks* a pack/unpack family and Ghostlite *adds* the whole pack suite. Decompilation overturns this: Viperfish has its own 27-op pack/unpack family under older sublane-indexed naming (`pack_bf16_compressed`, `unpack_bf8_sublanes_0_1`, `unpack_lower_bf16_interleaved`) and 6 dtype-merged transcendentals. The real delta is (a) the three *new* transcendental families `cosq`/`erf`/`sinq` (absent on VF); (b) the per-dtype / per-lane-range *split* of every merged op (each VF `tanh` → GL `tanh_f32` + `tanh_bf16`; each `unpack_bf8_sublanes_*` → `unpack_compressed_bf8_lanes_*_to_f32`), which drives most of the arm growth (Ghostlite reaches 142 ops vs the VF consumer's 86 distinct accessors); and (c) the fold of the VF inline-immediate path into the datapath. The gfc (6acc60406) 257-op union adds the FP8/Exmy small-float family on top.

---

## Function Map

All addresses are gfc (6acc60406) unless noted; the `Matches()` immediate and the `BitCopy` value are the authoritative opcode values.

| Symbol | Address | Opcode evidence | Confidence |
|---|---|---|---|
| `ConsumeVectorAluInstruction<glc::SparseCoreTecBundle>` | `0x13a0b580` | the opcode→op jt (base `0xb26`, bound `0x5cf`); 135 single-op arms + 7 shared | CONFIRMED |
| `ConsumeVectorAluInstruction<viperfish>` | `0x1399ffe0` | VF consumer (86 distinct `_internal_mutable_*` accessors; no cosq/erf/sinq; `SparseCoreImmediates` path) | CONFIRMED |
| `VectorAlu` jt (glc) | `0xae9d3dc` | 1488×int32 rel offsets; 143 targets (142 ops + default) | CONFIRMED |
| `SparseCoreTecVectorAlu0ByteNezOpcode::Matches` (gfc) | `0x1ec09e00` | `(dword[+0x40] & 0x3FC000) == 0xDC000` → 55, 8-bit `@bit14` | CONFIRMED |
| `SparseCoreTecVectorAlu0ByteNezOpcode::Matches` (vfc) | `0x1e94fd00` | `(dword[+0x40] & 0x7F00) == 0x3700` → 55, 7-bit `@bit8` | CONFIRMED |
| `SparseCoreTecVectorAlu1ByteNezOpcode::Matches` (gfc) | `0x1ec4a6a0` | `(qword[+0x38] & 0x1FE0…) == 0x6E0…` → 55, `@bit41` (lane 1) | CONFIRMED |
| `SparseCoreTecVectorAlu0Encoder::Encode` (gfc) | `0x1ec11100` | opcode `BitCopy(.,462,.,8)`; sel `@438/444/450/456`; pred `@470/473/474`; case8→3, case9→87, case10→4, case11→88 | CONFIRMED |
| `SparseCoreTecVectorAlu0Encoder::Encode` (vfc) | `0x1e954ae0` | opcode `BitCopy(.,456,.,7)` (7-bit VF form) | CONFIRMED |
| `EmitExtendedVectorVxUnop<…CosqF32>` (glc) | `0x13a1d800` | the transcendental emission template; tail-jump from `case 0xc9f` | CONFIRMED |
| `EmitVectorBinop<…VectorAddBf16>` (glc) | `0x13a1b000` | the arithmetic-core template; tail-jump from `case 0xb26` | CONFIRMED |
| `EmitPackVectorBinop<…PackCompressedB16ToB8>` (glc) | `0x13a1ff20` | the pack template; preceded by `GetOperandAndVsEncoding` | CONFIRMED |
| `BitCopy` | `0x1fa0a900` | LE packer `(dst, dst_bitoff, src, src_bitoff, nbits)` | CONFIRMED |

Cross-gen anchors: the per-op `Matches()` types exist in all three `isa` namespaces (`vxc::vfc`, `gxc::glc`, `gxc::gfc`); the 3-lane name union is **148 / 229 / 257** by symbol enumeration. The Ghostlite consumer error string is "`Unsupported opcode for Vector Alu slot: $0 : $1`" (`isa_emitter.cc`, an `absl::Substitute` template). Match the `SparseCoreTecVectorAlu[012]` prefix exactly to avoid pulling the scalar (`SparseCoreScalar*`) or TensorCore (`TensorCore*`) predicate types into the vector roster.

---

## Considerations

- **142 (jt arms) ≠ 229 (Matches-type union); both are real, at different granularities.** The Ghostlite jt reaches 142 ops because it folds dtype-merged / per-lane-width / predicated `MCInst` forms into multi-opcode arms and the 7-op shared chain. The 229 (glc) / 257 (gfc) figures count distinct `Matches()` op types across the three lanes. A reimplementer building an assembler uses the jt; one building a disassembler/validator uses the `Matches()` predicates.
- **The opcode value is lane-invariant; the bundle bit is not.** `VectorAlu0/1/2` decode the same opcode value from `@462/425/388`. The three lanes issue concurrently, so the scheduler must respect the `SparsecoreVregReadPort` per-bundle conflict (the read ports the chosen `Emit*` templates demand) — not fully traced; the six [immediate slots](tec-engine.md#immediate-slot-indexing) bound the distinct `VectorY` literals.
- **The dtype is in the opcode, never an operand.** `VectorAdd{S32,S16,Bf16,F32}` and `tanh_{f32,bf16}` are distinct opcodes. A reimplementer who encodes a single `VectorAdd` plus a dtype operand will mis-encode on GF/GL; the dtype-merged form existed only on Viperfish, where the element type lives in an operand/format field (inferred, not operand-decoded).
- **6acc60406 (gfc) has no jt in this wheel (LOW for arm-level facts).** The gfc opcode→op enumeration is the symbol-union count plus the lane-0 encoder and the `Matches()` predicates; the per-`MCInst`-opcode arm mapping is the Ghostlite jt. The GF-only FP8/Exmy ops (`PackCompressedF32ToExmy`, `VectorConvertStochasticF32ToE5m2`, `VectorConvertExmyToE4m3`, `UnpackCompressedExmyLanes07ToBf16`) are confirmed as `Matches()` types but their exact `MCInst` opcodes are not jt-anchored.
- **Sub-opcode operand binding is the remaining gap (HIGH/LOW).** The group escapes (`0/1/2/27/90/128`) and their 6-bit sub-field `@bit2` are recovered structurally; the per-sub-op VREG-selector→function→XRF-result binding (which Vx/Vy port each transcendental and unpack consumes, where its result lands) was not traced. Decode each by primary opcode + the sub-field; the operand-to-port map is open.

---

## Related Components

| Name | Relationship |
|---|---|
| `ConsumeVectorAluInstruction` (`0x13a0b580` glc) | the opcode→op→`Emit*` jt this page enumerates |
| `SparseCoreTecVectorAlu0Encoder::Encode` (`0x1ec11100` gfc) | writes the 8-bit opcode `@462` and the VREG selectors; the `BitCopy`-immediate source |
| per-op `SparseCoreTecVectorAlu<N><Op>Opcode::Matches()` | the opcode→mnemonic source — one type per op per lane per gen (148/229/257 union) |
| `EmitExtendedVectorVxUnop<…>` (`0x13a1d800` glc) | the transcendental emission template (the 18-op EUP path) |
| `BitCopy` (`0x1fa0a900`) | the LE packer every slot encoder uses to write the opcode bits |

## Cross-References

- [TEC (Vector) Engine](tec-engine.md) — the 64-byte bundle, the three `VectorAlu` lane slot bases (`@438/401/364`), and the 37-bit slot template this roster's opcode field sits in.
- [SCS Scalar Opcode Enumeration](scalar-opcode-enum.md) — the scalar-side opcode roster; the same `Matches()`-predicate / encode-switch-vs-silicon-value model.
- [OneSlot Scalar Router](oneslot-router.md) — the `ConsumeOneSlotInstruction` jt that routes the *scalar* slots; the `VectorAlu` dispatch is the separate consumer documented here.
- [VectorExtended (VEX)](vectorextended-vex.md) — the scan/sort/uniquify slot (53 ops); the transcendental `EmitExtendedVectorVxUnop` ops share its EUP pipeline.
- [VectorLoad Slot](vectorload-slot.md) — the 5-op tile vector-load roster.
- [VectorStore Slot](vectorstore-slot.md) — the 33-op tile vector-store + scatter-add roster.
- [SparseCore Overview](overview.md) — the three engine classes, per-gen presence, and the `TpuSequencerType` codec-template enum.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore ISA — [back to index](../index.md)
