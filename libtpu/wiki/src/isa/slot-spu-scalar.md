# SPU / Scalar Slot

> *Every offset, address, and bit position on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

The **SPU** (scalar-processing unit, `ScalarAlu` in the V5+ ISA namespaces) is the TensorCore's per-core control CPU lane. It owns the 32-entry scalar register file (SREGs), computes loop bounds, memory addresses, strides, and predicate values, runs branch/call/halt control flow, and bridges the scalar and vector engines through the V2S FIFO. In the VLIW bundle the SPU appears as **two scalar lanes** — `scalar_0`/`scalar_1` on Jellyfish and Pufferfish, `ScalarAlu0`/`ScalarAlu1` on Viperfish/Ghostlite/6acc60406, `SparseCoreScalarAlu0/1` on the SparseCore sequencer. Lane 0 carries the full op set including branch/call; lane 1 is an ALU+halt mirror. This is ARM-style predicated, statically-scheduled execution: there is no scoreboard, and the compiler proves bundle legality (see [Bundle Model](bundle-model-overview.md)).

Two encoder families produce the slot bytes. **Jellyfish/Dragonfish (JXC)** is a direct-pack encoder: `EncoderJf::EncodeScalarInstruction` (`0x1e862060`) ORs each field into a 64-bit slot word held at the encoder's struct byte `+0x2D` via literal `shl`/`and`/`or` arithmetic, then the [41-byte bundle](bundle-jf-41b.md) is sliced out. **Pufferfish/Viperfish/Ghostlite/6acc60406 (PXC/VXC/GXC)** route every field through the universal little-endian bit packer `BitCopy(dst, dst_bit, src, src_bit, nbits)` (`0x1fa0a900`), called once per field from a leaf `*ScalarAlu0Encoder::Encode` method whose body is a `switch` on the proto opcode at `[instr+0x50]`. The field positions on this page were read from the `BitCopy(buf, <bitoff>, …, <nbits>)` call triples in those leaf encoders, which is why each row is byte-anchored to a concrete bit offset.

This page documents the SPU to reimplementation grade across all six generations: the two-lane slot model and lane-0/lane-1 ownership split, the per-gen bit-field grid (opcode class, sub-opcode, predicate, X/Y/dst register operands), the unified 6-bit `ScalarYEncoding` operand selector and its 14 hardwired constants, the scalar register file bound, the immediate-slot geometry, the scalar address-computation ops, the V2S bridge, and the JF→GF field-layout evolution.

For reimplementation, the contract is:

- The **two-lane model**: `scalar_0`/`ScalarAlu0` (full op set, branch/call legal) vs `scalar_1`/`ScalarAlu1` (ALU+halt mirror, no branch/call), with `kMaxScalarSlotsPerBundle = 2` enforced at encode time.
- The **per-gen field grid**: opcode-class + sub-opcode + predicate-bit + X/Y/dst register fields, each at the absolute bit offset its `BitCopy` call names; and the JF direct-pack equivalent at struct word `+0x2D`.
- The **`ScalarYEncoding` 6-bit operand model**: `0..0x1f` = SREG, `0x20..0x25` = immediate-slot reference, `0x2e..0x3b` = hardwired constant.
- The **opcode dispatch**: a leaf `switch`/jump-table indexed by the proto opcode whose case count *is* the per-gen scalar ISA size (JF 0..0x3E, PF ≤0x33, VF ≤0x4C / lane1 ≤0x52, VF-SCS ≤0x57, GL/GF ≤0x49).
- The **JF→GF shrink**: opcode-class width 6→5→(4+6)→(4+6)→(2+6), the predicate model from in-slot 5-bit field to in-slot 1-bit selector to the dedicated dual-predicate slot on 6acc60406 (GF), and the 16-bit→20-bit immediate-slot widening.

| | |
|---|---|
| **Lanes / bundle** | 2 (`scalar_0`/`scalar_1`, lane 0 = full + branch/call, lane 1 = ALU+halt mirror) |
| **JF encoder** | `EncoderJf::EncodeScalarInstruction(ScalarInstruction const&, int lane, Bundle const&)` @ `0x1e862060` |
| **V4+ packer** | `BitCopy(dst, dst_bit, src, src_bit, nbits)` @ `0x1fa0a900` (`_Z7BitCopyPviPKvii`) |
| **VF lane-0 encoder** | `vxc::isa::TensorCoreScalarAlu0Encoder::Encode` @ `0x1eecb900` |
| **Scalar registers** | **32** SREGs, **5-bit** field; bound by `EncodingToScalarRegister` @ `0x1e871e40` (`idx > 0x1F` → error) |
| **Y operand** | 6-bit `ScalarYEncoding`: SREG / imm-slot / hardwired constant |
| **Immediate slots** | 6 per bundle; **16-bit** on JF, **20-bit** on V5+ (`BitCopy … nbits=20`) |
| **Opcode source** | proto field at `[instr+0x50]`, dispatched by a per-gen leaf `switch` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Two-Lane Slot Model

Every generation issues up to two scalar ops per bundle. The lanes are *not* symmetric: lane 0 owns the full opcode space (arithmetic, compare, convert, memory, control flow, FIFO bridge), while lane 1 is a restricted mirror that can run the ALU/halt subset but **not** branches or calls. The asymmetry is enforced in the encoder, not merely by convention.

On Jellyfish, `EncodeScalarInstruction` takes an explicit lane argument `a3` and opens with a hard check:

```c
// EncoderJf::EncodeScalarInstruction(instr, lane, bundle)  @ 0x1e862060 (lines 216-223)
if (lane > 1)                                          // kMaxScalarSlotsPerBundle == 2
    CHECK_FAIL("slot < kMaxScalarSlotsPerBundle");     // encoder_jf.cc:1261
// branch (opcode 0xA) and call (0xC..0xF) reject lane != 0:
case 0xA: if (lane != 0) CHECK_FAIL("slot == 0");      // BranchRelative  (line 433)
case 0xE: if (lane != 0) CHECK_FAIL("slot == 0");      // Call            (line 476)
// scalar load/store/DMA-issue reject lane != 1:
case 4,5: if (lane != 1) CHECK_FAIL("slot == 1");      // ScalarLoad      (line 337)
case 6:   if (lane != 1) CHECK_FAIL("slot == 1");      // ScalarStore     (line 369)
```

So on Jellyfish the *control-flow* ops bind to lane 0 and the *memory* ops bind to lane 1 — a deliberate static partition of the two lanes by function, the JF analog of the V5+ "branch/call legal only in lane 0" rule. The V5+ encoders express the same split structurally: there are separate `TensorCoreScalarAlu0Encoder` and `TensorCoreScalarAlu1Encoder` leaf methods, and only the lane-0 jump table contains `BranchAbsolute`/`CallSreg` cases (lane 1's table runs to a larger case count but only halt+ALU cases are legal — see [Sequencer Slot](slot-sequencer.md)).

> **QUIRK — lane 1 is not "scalar slot 2", it is a constrained twin.** A reimplementation that treats the two scalar lanes as interchangeable will happily place a branch in lane 1 and produce a bundle the hardware sequencer cannot execute. The legality predicate must reject control flow and FIFO-return ops in lane 1; the JXC encoder does this with `CHECK_FAIL("slot == 0")`, the VXC/GXC encoders by simply not emitting a lane-1 control-flow case.

The two lanes are stacked adjacently at the **high end** of the bundle (control state lives where the sequencer reads it first), with lane 0 above lane 1. On Viperfish, lane 0 occupies roughly bits 477–503 and lane 1 roughly bits 454–476 of the 512-bit bundle — the lane-1 fields are exactly the lane-0 fields shifted down by 27 bits (lane-0 class @499 → lane-1 class @472; sub @493 → @466; X @488 → @461).

---

## The `BitCopy` Field Packer

All V4+ scalar fields are placed by one function:

```c
// _Z7BitCopyPviPKvii  @ 0x1fa0a900
void BitCopy(void*       dst,        // bundle byte buffer (the Span<uint8_t>)
             int         dst_bitoff, // absolute bit offset into the bundle
             const void* src,        // pointer to the field value word
             int         src_bitoff, // bit offset into the source word (always 0 for scalar fields)
             int         nbits);     // field width in bits
```

It is a generic, byte-spanning, little-endian unaligned bit-field copy (`if (!nbits) return; word = dst_bitoff / 8; …`), with an AVX2-vectorized fast path for `nbits >= 24` that is irrelevant to the ≤6-bit scalar fields. Each leaf scalar encoder loads the field value into a stack slot and calls `BitCopy` once per field. A representative prologue, the Viperfish lane-0 header:

```c
// vxc::isa::TensorCoreScalarAlu0Encoder::Encode(this, instr, span)  @ 0x1eecb900 (lines 26-30)
v22[0] = *(int*)(instr + 32);   BitCopy(span, 499, v22, 0, 4);   // opcode-class  @bit 499, 4b
v22[0] = *(uint8*)(instr + 24); BitCopy(span, 503, v22, 0, 1);   // predicate bit @bit 503, 1b
switch (*(uint32_t*)(instr + 80)) {                              // opcode = [instr+0x50]
    case 6:  BitCopy(span, 493, &zero, 0, 6);                    // sub-opcode    @bit 493, 6b
             BitCopy(span, 488, &Xreg, 0, 5); break;             // X register    @bit 488, 5b
    case 8:  …  BitCopy(span, 482, &Yenc, 0, 6);                 // Y encoding    @bit 482, 6b
             BitCopy(span, 477, &dst,  0, 5); break;             // dst register  @bit 477, 5b
    …
}
```

The opcode-class is read from `[instr+0x20]` (proto field at offset 0x20, exposed to the decompiler as `instr+32`), the predicate from `[instr+0x18]`, and the dispatch opcode from `[instr+0x50]`. Every bit offset quoted below is the literal first argument of one of these `BitCopy` calls.

---

## Per-Generation Field Layout

The five generations share one logical field set — opcode class, sub-opcode, predicate, and the X/Y/dst register operands — but each places it at different bits because the bundle width and the predicate model change. The opcode is a small **class** field at the top of the slot plus a 6-bit **sub-opcode** below it; the class+sub pair jointly index the leaf `switch`. The table below is the consolidated, byte-verified grid (all offsets absolute bits, LSB-first):

| Gen (lane 0) | bundle | opcode class | sub-opcode | predicate | X reg | Y enc | dst reg | jump table | source |
|---|---:|---|---|---|---|---|---|---|---|
| **Jellyfish** | 41 B | opcode 6b @abs **311** (`<<47` in word `+0x2D`) | — (single 6-bit opcode) | 5b @abs **317** (`<<53`) | 5b | 6b ScalarY | 5b | 0..0x3E | `0x1e862060` |
| **Pufferfish** | 51 B | opcode 5b @bit **403** | 6b @bit **397** | opcode value `0x1F`=NeverExecute | 6b @bit **386** | 6b ScalarY | — | ≤0x33 | `0x1ed16dc0` |
| **Viperfish** | 64 B | 4b @bit **499** | 6b @bit **493** | 1b @bit **503** | 5b @bit **488** | 6b @bit **482** | 5b @bit **477** | ≤0x4C | `0x1eecb900` |
| **Ghostlite** | 64 B | 4b @bit **502** | 6b @bit **496** | 1b @bit **506** | 5b @bit **491** | 6b @bit **485** | 5b @bit **480** | ≤0x49 | `0x1f219b40` |
| **6acc60406** | 64 B | 2b @bit **489** | 6b @bit **483** | (dual-pred slot) | 5b @bit **478** | — | 5b @bit **467** | ≤0x49 | `0x1f87b420` |
| **VF SparseCore** | 32 B | 4b @bit **187** | 6b @bit **181** | 1b @bit **191** | 5b @bit **176** | 6b | — | ≤0x57 | `0x1ee82ce0` |

The cleanest reading of the table is the **Ghostlite = Viperfish + 3** relation: every GL field is exactly 3 bits higher than its VF counterpart (499→502, 503→506, 493→496, 488→491, 482→485, 477→480), the uniform `+3` shift that [Ghostlite Bundle](bundle-gl.md) attributes to widening the MXU/VALU opcodes by one bit each. 6acc60406 (GF) then *removes* the in-slot predicate entirely (no `BitCopy` to a predicate bit appears in `0x1f87b420`) and shrinks the class field to 2 bits, dropping the slot header down ~13 bits to free room for the dedicated [`TensorCorePredicates`](slot-predicate.md) dual-predicate slot.

### Jellyfish — direct-pack at struct word `+0x2D`

Jellyfish does not use `BitCopy`. `EncodeScalarInstruction` maintains a 64-bit slot word at the encoder's `this+0x2D` and ORs fields in with shift/mask, branching on the lane argument:

```c
// EncoderJf::EncodeScalarInstruction  @ 0x1e862060 (lines 239-253)
pred = EncodePredication(instr) & 0x1F;
word = *(uint64*)(this + 0x2D);
if (lane == 1) {                                          // scalar_1
    word = (pred << 26)         | (word & 0xFFFF'FFFF'83FF'FFFF); // predicate @ word-bit 26
    word = ((opcode & 0x3F)<<20)| (word & 0xFFFF'FFFF'FC0F'FFFF); // opcode    @ word-bit 20
} else {                                                  // scalar_0 (sequencer lane)
    word = (pred << 53)         | (word & 0xFC1F'FFFF'FFFF'FFFF); // predicate @ word-bit 53
    word = ((opcode & 0x3F)<<47)| (word & 0xFFE0'7FFF'FFFF'FFFF); // opcode    @ word-bit 47
}
*(uint64*)(this + 0x2D) = word;
// operands then OR'd from ScalarBinaryOperands (X, Y, ScalarY at lane-dependent shifts)
```

Because the [41-byte wire bundle is `struct[0x0C..0x34]`](bundle-jf-41b.md), a field at word-bit `S` in struct byte `0x2D` lands at absolute bundle bit `0x2D*8 + S − 96`: lane-0 opcode `0x2D*8 + 47 − 96 = 311`, lane-1 opcode `0x2D*8 + 20 − 96 = 284`. The opcode is a single 6-bit field (`opcode & 0x3F`) with no separate class; the `switch (opcode)` covers cases 0..0x3E with a sparse legal set and rejects `opcode > 0x3E` (`"Not implemented yet."`, encoder_jf.cc:1439).

> **CORRECTION (SPU-1) —** an earlier reading placed the Jellyfish `scalar_0` opcode at "bits 20-25" and `scalar_1` higher in the word. The decompiled lane branch shows the opposite: the `lane==0` (sequencer) branch shifts the opcode by 47 (abs 311) and the predicate by 53 (abs 317), while `lane==1` shifts the opcode by 20 (abs 284). "Bits 20-25" is the **lane-1** opcode placement, not lane 0. This page follows the decompile and is consistent with the [Jellyfish Bundle](bundle-jf-41b.md) scalar-slot table.

### Pufferfish — 5-bit opcode with NeverExecute overload

Pufferfish places a single 5-bit opcode at bit 403 (the last byte of the 51-byte bundle), with a 6-bit sub-opcode at 397 and a 6-bit operand at 386:

```c
// pxc::isa::TensorCoreScalar0Encoder::Encode  @ 0x1ed16dc0 (lines 25-44)
v21[0] = *(int*)(instr + 32);  BitCopy(span, 403, v21, 0, 5);   // opcode @bit 403, 5b
switch (*(uint32_t*)(instr + 80)) {
    case 7: BitCopy(span, 397, v21, 0, 6);                       // sub-opcode @bit 397, 6b
            BitCopy(span, 386, v21, 0, 6); …                     // operand    @bit 386, 6b
}
```

> **NOTE —** Pufferfish has no separate 1-bit predicate field in the scalar slot. The 5-bit opcode field doubles as the slot-skip marker: writing the value `0x1F` (`NeverExecute`) is how an unused Pufferfish scalar slot is stamped, the same convention the [NOP encoding](nop-canonical.md) uses bundle-wide. The full PXC immediate geometry (routed through the `TensorCoreMiscEncoder` `ScalarY` 5-bit fields) is not byte-pinned here — see *Limits*.

---

## The `ScalarYEncoding` Operand Model

Every scalar op's "Y" operand is a single 6-bit selector decoded by `ProtoUtils::EncodingToScalarRegister` (`0x1e871e40`) and the `ScalarY` constants map. The value space is a three-way union:

| `ScalarYEncoding` value | Meaning |
|---|---|
| `0x00 .. 0x1f` (0–31) | SREG number — the Y source register |
| `0x20 .. 0x25` (32–37) | bundle immediate-slot reference imm0..imm5 |
| `0x2e .. 0x3b` (46–59) | hardwired constant (table below) |

`EncodingToScalarRegister` is the register-decode half, and its bound *is* the proof that the file is 32 deep:

```c
// ProtoUtils::EncodingToScalarRegister(ScalarYEncoding)  @ 0x1e871e40 (lines 13-30)
if (encoding > 0x1F) {                                  // > 31
    return Error("Input is not a valid register encoding. "
                 "Input must be in the range [%d, %d]", 0, 31);  // proto_utils.cc:917
}
out->reg = encoding;  out->status = OK;
```

The hardwired-constant half lets the SPU reference common scalar values without consuming any of the six immediate slots, fed from a rodata key/value pair into `ProtoUtils::GetConstants<ScalarYEncoding>` (`0x1e870700`):

| Enc | Raw | Value | Enc | Raw | Value |
|---|---|---|---|---|---|
| `0x2e` | `0x00000001` | int 1 | `0x35` | `0xc0000000` | −2.0f |
| `0x2f` | `0xffffffff` | int −1 | `0x36` | `0x3f000000` | 0.5f |
| `0x30` | `0x00000000` | 0 / 0.0f | `0x37` | `0xbf000000` | −0.5f |
| `0x31` | `0x80000000` | −0.0f | `0x38` | `0x40490fdb` | π |
| `0x32` | `0x3f800000` | 1.0f | `0x39` | `0xc0490fdb` | −π |
| `0x33` | `0xbf800000` | −1.0f | `0x3a` | `0x402df854` | e |
| `0x34` | `0x40000000` | 2.0f | `0x3b` | `0xc02df854` | −e |

> **QUIRK — the constant table is why loop strides rarely cost an immediate slot.** Strides of ±1, comparison thresholds of 0, and reciprocal-Newton seeds (±0.5, ±2.0) are dominated by these 14 values. A `ScalarYEncoding` in `0x2e..0x3b` resolves the Y operand entirely inside the slot, leaving all six 20-bit immediate slots free for genuinely arbitrary constants. A reimplementer who routes every constant through an immediate slot will spuriously run out of slots.

---

## Scalar Register File

| Gen | SREGs | field width | spill backing | scalar-load latency |
|---|---:|---|---|---:|
| Jellyfish / Dragonfish | 32 | 5-bit | SMEM (2 banks) | 2 cyc |
| Pufferfish | 32 | 5-bit | SMEM (8 banks) | 4 cyc |
| Viperfish / Ghostlite / 6acc60406 | 32 | 5-bit | SMEM (8 banks) | 6 cyc |

The 32-entry / 5-bit count is bound three independent ways in the binary: `EncodingToScalarRegister` rejects `> 0x1F`; every per-gen register operand `BitCopy` is `nbits=5` (VF X@488, dst@477; GL X@491, dst@480; GF X@478, dst@467); and a debug flag `FLAGS_ForceTargetNumScalarRegs` (`0x22542da8`) can only clamp the count *below* the hardware 32. SMEM is the spill backing store — there is no SMEM register window. When LSRA-v2 cannot keep a SREG live across a region, it emits a `ScalarStore*ToSmem*` to the reserved spill region and a `ScalarLoadSmem*` at the next use; the spill region size is set by `FLAGS_xla_jf_lsra_v2_reserved_smem` (`0x223afaa8`).

---

## The Scalar Op Set

The authoritative per-gen op roster is the leaf encoder `switch` itself: each `case N` returns an `Encode…<OpName>` thunk, so the case set is the ISA. The Viperfish lane-0 `switch` (`0x1eecb900`, cases 6–0x4C, 77 ops) groups as follows; Ghostlite and 6acc60406 are this set minus their deltas.

```text
Integer ALU    IntegerAdd, IntegerAddWithOverflowCheck, IntegerSubtractYX(+OverflowCheck),
               Multiply32BitIntegers, Multiply32BitUnsignedIntsReturningHighHalf,
               CarryOutFromIntegerUnsigned,
               DivideWithRemainderXY{,PushQuotient,PushRemainder}
Float ALU      FloatingPointMultiply, Max/MinOfTwoFloatingPointValues,
               Max/MinOfTwoUnsignedIntValues
Bitwise        BitwiseAnd, BitwiseOr, BitwiseXor
Shift          LogicalShiftLeftXByYPlaces, LogicalShiftRightXByYPlaces,
               ArithmeticShiftLeftXByYPlacesCheckOverflow, ArithmeticShiftRightXByYPlaces
Compare        CompareFloatingPoint{Eq,Neq,Gt,Gte,Lt,Lte}, CompareInteger{Eq,Ne},
               CompareSignedInteger{Gt,Gte,Lt,Lte}, CompareUnsignedInteger{Gt,Gte,Lt,Lte}
Convert        ConvertInt32ToFloat32, ConvertFloat32ToInt32, Ceiling, Floor,
               CountLeadingZeros, IsInfOrNan
Move           MoveY
Control        BranchAbsolute/Relative/Sreg, CallAbsolute/Relative/Sreg,
               HaltYield, HaltYieldConditional, Delay, ScalarFence, PredicateOr, SetTag
FIFO bridge    PopV2s, PopDrf, PopSfrf, PopSccf, PushSccf
Register reads ReadRegisterLccLow/High (loop counter), ReadRegisterGtcLow/High (time counter),
               ReadRegisterTag, ReadRegisterTcid, ReadRegisterTracemark, ReadRegisterYieldRequest
```

Pufferfish uses `Scalar`-prefixed mnemonics (`ScalarIntAdd`, `ScalarFloatMul`, `ScalarBranchAbsolute`, `ScalarPopV2s`, `ScalarLoadSmem`, `ScalarLoadSmemOffset`, `ScalarStoreSmemAbsolute`, the `ScalarDma*` family, `ScalarSetRegister`, `ScalarReadRegisters` — 52 lane-0 cases, ≤0x33). It has **no** `HaltYield*` and **no** `ReadRegisterLcc*` (no hardware loop counter on the PF TensorCore). The opcode→mnemonic case sets are byte-recovered from the encoder switches (VF inline at `0x1eecb900`; PF at `0x1ed16dc0`; GL 64 distinct cases at `0x1f219b40`; GF 65 at `0x1f87b420`).

### Address-computation ops

The SPU computes all scalar addresses. The encoding evolved across two families:

```text
JF / PF (JXC/PXC)   ScalarLoadSmem        SREG <- SMEM[abs imm]
                    ScalarLoadSmemOffset  SREG <- SMEM[baseSREG + imm]
                    ScalarStoreSmemAbsolute  SMEM[abs imm] <- SREG
V5+ (VXC/GXC)       ScalarLoadSmemY       SREG <- SMEM[imm]          (Y = 20-bit imm)
                    ScalarLoadSmemXY      SREG <- SMEM[Xreg + imm]   (base+displacement)
                    ScalarStoreXToSmemY   SMEM[imm] <- Xreg
                    ScalarStoreXToSmemSumDestAndY  SMEM[imm] += Xreg (6acc60406 scatter-add)
```

The `XY` form (X = base SREG, Y = `ScalarYEncoding` reg/imm/const) is the canonical V5+ base+displacement address generator; an `indicesToOffset` chain `Σ indices[d]·strides[d]` lowers to a sequence of scalar `IntegerAdd`/`Multiply` ops packed into SPU slots. See [Memory-Load](slot-memory-load.md) and [Memory-Store](slot-memory-store.md).

---

## Immediate Slots

Six immediate slots ride at the **low** end of every bundle (opposite the scalar slot's high end). They are placed by a per-gen `TensorCoreImmediatesEncoder`, each slot a single `BitCopy` call. The widths and positions are byte-exact:

| Gen | slots | width | imm0..imm5 bit offsets | encoder |
|---|---:|---:|---|---|
| Jellyfish | 6 | 16 b | `Bundle` proto words `+0x70/+0x74/+0x78/+0x7c` (mask bits `0x1000`..`0x8000`) | `Place16BitScalarImmediate` @ `0x1e8721e0` |
| Viperfish | 6 | 20 b | 330, 350, 370, 390, 410, 430 | `0x1eebee40` |
| Ghostlite | 6 | 20 b | 333, 353, 373, 393, 413, 433 | `0x1f20d520` |
| 6acc60406 | 6 | 20 b | 323, 343, 363, 383, 403, 423 | `0x1f86de20` |

The V5+ slots are a perfect stride-20 ladder (`BitCopy(span, off, …, 20)`), and the GL ladder is again VF+3, the GF ladder VF−7 — the same uniform per-gen shift the scalar slot shows. A 32-bit immediate consumes an adjacent **pair** of 20-bit slots (40 ≥ 32); anything wider becomes an SMEM constant load issued by the SPU.

> **CORRECTION (SPU-2) —** an earlier note placed the Viperfish immediate slots at bits `0x4a, 0x5e, 0x72, …` (74, 94, …). The decompiled `TensorCoreImmediatesEncoder::Encode` (`0x1eebee40`) packs them at **330, 350, 370, 390, 410, 430** (stride 20), with Ghostlite at 333…433 and 6acc60406 at 323…423. The earlier offsets are not what the encoder emits; this page uses the `BitCopy` arguments directly.

Jellyfish's `Place16BitScalarImmediate` (`0x1e8721e0`) walks the slot-presence mask testing `0x8000/0x4000/0x2000/0x1000` and returns a `ScalarYEncoding` in `0x20..0x23` referencing the slot it filled, folding the sign bit as `(val >> 13) & 4`. A 32-bit value splits into an adjacent slot pair via `Place32BitScalarImmediate` (`0x1e8724c0`). See [Immediate Slot](slot-immediate.md) for the full per-gen encoding-id → slot-position map.

---

## Scalar↔Vector Bridge

The SPU and vector engine exchange values through FIFOs, never a direct register port — bundle-internal writes do not cross-feed (see [Bundle Model](bundle-model-overview.md)). Vector→scalar values cross the **V2S FIFO**: the vector engine pushes (debug accessor `WriteV2SFifo` @ `0x0e758400`), and the SPU pops with `PopV2s` (debug `ReadV2SFifo` @ `0x0e758060`). The canonical use is reduction: a vector reduce writes its scalar result into V2S, and the SPU pops it for control flow. V5+ adds `PopDrf` (data-result FIFO, MXU/EUP results), `PopSfrf` (sync-flag-result FIFO), and `PopSccf`/`PushSccf` (Viperfish SCCF) — all present as dedicated cases (`0x3A`..`0x3E`) in the Viperfish lane-0 switch. The scalar→vector direction is the separate `TensorCoreVectorScalar` slot (encoder `0x1f01a3e0`), which broadcasts an SREG value into a vreg lane. The FIFO also serves as the implicit return-address stack for `CallSreg` (return = `BranchSreg` reading the popped value); there is no dedicated return opcode in any generation.

---

## Per-Generation Deltas

| Feature | JF v2 | PF v4 | VF v5p | GL v6e | GF TPU7x |
|---|---|---|---|---|---|
| Scalar lanes | 2 | 2 | 2 | 2 | 2 (+ dual-pred slot) |
| Opcode field | 6-bit single | 5-bit + 6-sub | 4-class + 6-sub | 4 + 6 | 2 + 6 |
| In-slot predicate | 5-bit field | opcode `0x1F`=NeverExec | 1-bit @503 | 1-bit @506 | none (dual-pred slot) |
| Jump-table size (lane 0) | 0..0x3E | ≤0x33 | ≤0x4C | ≤0x49 | ≤0x49 |
| SREGs | 32 | 32 | 32 | 32 | 32 |
| Immediate slots | 6 × 16 b | 6 (Misc/ScalarY) | 6 × 20 b | 6 × 20 b | 6 × 20 b |
| HW call/return | inlined (none) | yes (lane 0) | yes | yes | yes |
| HW loop counter (`ReadRegLcc`) | no | no | yes | yes | yes |
| `HaltYield` | no | no | yes + Conditional | Conditional only | none |
| `ScalarStoreXToSmemSumDestAndY` (lane 1) | no | no | no | no | yes (scatter-add) |
| `LogicalShiftLeftOnesXByYPlaces` (lane 0) | no | no | no | no | yes |
| FIFO bridge | V2S, Hmf | V2S | V2S,Drf,Sfrf,Sccf | V2S,Drf,Sfrf | V2S,Drf,Sfrf |

The 6acc60406 (GF) TensorCore scalar slot differs from Ghostlite by two binary-confirmed deltas: it **adds** `LogicalShiftLeftOnesXByYPlaces` to the lane-0 op set (the GF lane-0 switch `0x1f87b420` has 65 distinct `Encode…` mnemonics vs GL's 64, the single new case at `0x18`) and adds `ScalarStoreXToSmemSumDestAndY` to the **lane-1** op set (a scatter-add store; present in the GF `TensorCoreScalarAlu1` family — e.g. `0x1f64dc60` — but absent from the VF/GL TensorCore lane-1 store set, which stops at `ScalarStoreXToSmemY`). The lane-0 65-vs-64 count therefore reflects `LogicalShiftLeftOnesXByYPlaces` alone; the scatter-add store is a separate lane-1-only addition and does not appear in the lane-0 switch. 6acc60406 (GF) also **drops** the in-slot predicate (no predicate `BitCopy` in `0x1f87b420`; the opcode-class is read from `[instr+0x1c]` as 2 bits, not `[instr+0x20]` as 4). The dropped predicate routes to the dedicated dual-`pred_0`/`pred_1` slot; the exact wiring from the scalar slot into that slot was not traced (see *Limits*). (This GF generation is the externally-documented "Ironwood"/TPU7x; do not confuse it with Trillium, which is the prior Ghostlite/v6e generation.)

> **GOTCHA — 6acc60406 (GF) reads the opcode class from a different proto offset.** The VF/GL/SCS encoders read the class from `[instr+0x20]` (4 bits); GF reads it from `[instr+0x1c]` (2 bits). A decoder that hardcodes the `+0x20` offset and a 4-bit class will misread every GF scalar slot. The slot also starts ~13 bits lower in the bundle (sub-opcode @483 vs GL @496) precisely because the predicate bit no longer lives in the slot.

### Branch/call discriminators (lane 0)

The control-flow ops share the sub-opcode/operand grid but disambiguate through a value written into the **opcode-LOW** field — the same 5-bit field the table calls "sub-opcode discriminator", at the per-gen position below. On 6acc60406 (GF) (`0x1f87b420`) the immediate branch/call family pins the 6-bit `sub-opcode` (opcode-HIGH) at **483** to `0` and selects the op via the 5-bit field at **478**:

| Op | opcode value (`[instr+0x50]`) | sub-opcode @483 (w6) | discriminator @478 (w5) | leaf encoder | Confidence |
|---|---:|---:|---:|---|---|
| `BranchAbsolute` | 62 | 0 | **4** | `0x1f87f5c0` | CONFIRMED |
| `BranchRelative` | 63 | 0 | **5** | `0x1f87f660` | CONFIRMED |
| `CallAbsolute` | 65 | 0 | **6** | `0x1f87f7e0` | CONFIRMED |
| `CallRelative` | 66 | 0 | **7** | `0x1f87f8e0` | CONFIRMED |

The register-indirect forms (`BranchSreg`/`CallSreg`) instead carry their opcode in the opcode-HIGH (`@483`) field, and a `CallSreg` writes its return-address SREG into the 5-bit field at **467** and the link/target SREG into the 6-bit field at **472**. This is the same discriminator grid the [Sequencer Slot](slot-sequencer.md) documents from the same encoders; the SPU page's "sub-opcode" is that page's "opcode-HIGH", and the @478 field is its "opcode-LOW". On Viperfish (`0x1eecb900`) the equivalent fields sit at the VF positions (sub @493, dst @477), on Ghostlite at VF+3 (sub @496, dst @480).

---

## SparseCore Scalar Slot

The SparseCore sequencer carries its own scalar lanes (`SparseCoreScalarAlu0/1`) in the 32-byte SCS bundle. The Viperfish SCS encoder (`vfc::isa::SparseCoreScalarAlu0Encoder::Encode` @ `0x1ee82ce0`) is structurally identical to the TensorCore lane-0 encoder — same `[instr+0x20]` class read, same `[instr+0x18]` predicate, same `[instr+0x50]` dispatch — but scaled to the 32-byte bundle:

```c
// vfc::isa::SparseCoreScalarAlu0Encoder::Encode  @ 0x1ee82ce0 (lines 27-46)
v22[0] = *(int*)(instr + 32);  BitCopy(span, 187, v22, 0, 4);    // opcode-class @bit 187, 4b
v22[0] = *(uint8*)(instr + 24); BitCopy(span, 191, v22, 0, 1);   // predicate    @bit 191, 1b
case 6: BitCopy(span, 181, &zero, 0, 6);                         // sub-opcode   @bit 181, 6b
        BitCopy(span, 176, &Xreg, 0, 5);                         // X register   @bit 176, 5b
```

The SCS scalar slot sits near the top of the 256-bit bundle (bytes 22–23). Its jump table is *larger* than the TensorCore's — cases run to 0x57 (88 ops) — because the SparseCore scalar path carries gather/scatter address ops the TensorCore lacks. Ghostlite and 6acc60406 SCS share the 32-byte bundle width, so the byte positions are expected to match; this was inferred from the shared bundle width, not byte-verified for GL/GF SCS (MEDIUM).

---

## Limits and Open Items

- **Per-opcode operand sets (Partial).** The binary-ALU template (`IntegerAdd`-class: dst@477, X@488, Y@482 on VF), the load/store template, and the branch template were decoded. The remaining ~70 VF / ~64 GL / ~65 GF ops reuse the same field grid, but each op's exact present/absent operand set was not enumerated one leaf encoder at a time. (LOW for the non-template ops' operand presence.)
- **V5+ predicate-register count (Partial).** Only Jellyfish's 15-predicate file is decoded; the `getNumPredicateRegisters` overrides for the VF/GL/GF subtargets were not — the in-slot 1-bit field is a selector into a per-gen predicate file whose size lives in the HAL factory. See [Predicate Register File](slot-predicate.md).
- **Pufferfish immediate geometry (Partial).** PXC routes immediates through the `TensorCoreMiscEncoder` `ScalarY` 5-bit fields rather than a clean 16-bit slot ladder; the exact PXC slot count/width was not pinned (only the 5-bit `ScalarY` field). The table lists the JF/VF/GL/GF immediate ladders, which are byte-exact.
- **6acc60406 (GF) dual-predicate routing (Not traced).** The slot exists; the wiring from `ScalarAlu0`'s removed predicate bit into `TensorCorePredicates` was not followed.
- **GL/GF SparseCore slot positions (Inferred).** Only the VF SCS layout (`0x1ee82ce0`) is byte-verified; GL/GF SCS positions are inferred from the shared 32-byte bundle width.
- **Decode side (Located, not field-decoded).** The symmetric `*ScalarAlu0Decoder` methods were located but not decoded field-by-field; they are the inverse `BitCopy` extraction at the same offsets. See [Decode-Side: VF / GXC](decode-side-vf-gxc.md) and [Decode-Side: JF / PF](decode-side-jf-pf.md).

---

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the per-generation bundle widths, the no-scoreboard VLIW contract, and the slot taxonomy this page instantiates for the scalar lanes.
- [Sequencer Slot](slot-sequencer.md) — the branch/call/halt control-flow ops that bind to scalar lane 0, and the proto-bundle emitter fold.
- [Immediate Slot](slot-immediate.md) — the per-gen encoding-id → immediate-slot bit-position map the `ScalarYEncoding` `0x20..0x25` references resolve into.
- [Predicate Register File](slot-predicate.md) — the (smaller) predicate file the in-slot 1-bit predicate selector indexes, and the JF 5-bit predicate field.
- [Jellyfish Bundle](bundle-jf-41b.md) — the 41-byte direct-pack layout and the scalar-slot lane-0/lane-1 field arithmetic this page summarizes.
- [Ghostlite Bundle](bundle-gl.md) — the source of the uniform +3-bit scalar/immediate shift versus Viperfish.
- [6acc60406 Bundle](bundle-gf.md) — the GF generation that moves the scalar predicate to the dual-predicate slot.
- [MC-Emitter](mc-emitter.md) — `getBinaryCodeForInstr` / `InstBits`, the LLVM-MC path the V5+ proto encoders bypass (all-zero `InstBits`).
- [LLO Opcode Enum](llo-opcode-enum.md) — the 462-opcode LLO enum the scalar-ALU opcodes map into via the per-gen jump tables.
