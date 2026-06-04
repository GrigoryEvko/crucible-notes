# Merged-ALU Bit Layout

> *All addresses, offsets, and bit positions on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`). The ELF is **not** stripped; full C++ symbols are present. `.text` VMA equals file offset (`0xe63c000`); all addresses are analysis VMAs. Other versions will differ.*

## Abstract

The JF/DF generation of BarnaCore (Jellyfish/Dragonfish, the v2–v3 embedding accelerator that predates the Pufferfish [32-byte BCS bundle](bcs-32byte-bundle.md)) drives its embedding-address datapath with a **23-byte / 184-bit address-handler bundle**. Two of that bundle's slots are vector-ALU lanes — `Alu0` and `Alu1` — and the way they are encoded is the single most counter-intuitive part of the whole format: the address-handler encoder **does not encode the ALU itself**. `EncoderJf::EncodeBarnaCoreAddressHandlerVectorAlu` (`@0x1e86f5c0`) builds a throwaway full `isa::Bundle`, copies the `VectorAluInstruction` proto into that bundle's `vector_alu` lane, runs the *standard* 41-byte JF bundle encoder (`EncoderJf::EncodeBundleInternal` `@0x1e86c7c0`), and then **harvests a 7-byte window** out of the 41-byte output and shift-merges it into the 23-byte address-handler bundle. The address-handler ALU is therefore not a distinct ISA — it is the main JF vector-ALU ISA, *relocated*.

This has a clean consequence for a reimplementer. The opcode space (`VectorAluOpcode`, a 56-value 6-bit enum) and the operand selectors (`VectorRegister`, `VectorAluYEncoding`) are exactly the main JF vector-ALU encoding; only the **5-bit lane predication** and the **lane select** are address-handler-specific. The merge maps a 56-bit JF window onto two contiguous **31-bit lane slots** (5-bit pred + 26-bit body), `Alu0` at absolute bits 48..78 and `Alu1` at 79..109, filling the previously-unmapped gap between the predication base (bit 48) and the Store slot base (bit 110) documented in the parent JF/DF bundle map.

This page documents three artifacts. (1) The **merged vector-ALU bit layout** — the harvest mechanism, the 12-byte header strip, and the byte-exact per-lane merge map, plus the `VectorAluOpcode` (56v) and `VectorAluYEncoding` (32v) enums. (2) The two **shared operand-value enums** the VectorSlot Store/Load/Result fields reference — `VectorResultDestination` (the EUP-result routing target) and `BaseAddressEncoding` (the 2-bit Store/Load base-address mode) — plus the `Result.which_destination` routing and per-lane EUP-result placement. (3) The **program-level BCS assembly** — how an address-handler program is a flat `N × 23`-byte bundle stream with no header, no separator, and no terminator byte, terminated by a per-bundle `prog_end` bit.

For reimplementation, the contract is:

- **The harvest-and-merge model**: the merged ALU body is the standard JF `VectorAluInstruction` encoding, produced by the full 41-byte JF bundle encoder and relocated by a per-lane shift/mask merge. Re-encode the ALU by re-running the JF encoder, not by packing fields directly.
- **The 12-byte header strip + the per-lane merge map**: `Alu0` slot abs 48..78, `Alu1` slot abs 79..109, each a 5-bit predication + a 26-bit opcode/operand body, with the exact shift/mask arithmetic.
- **The shared operand enums**: `VectorResultDestination` (V0/V1/VLD, 3 values) and `BaseAddressEncoding` (ZERO/VS0/VS1/VS2, 4 values), byte-pinned from their `EnumDescriptorProto`s.
- **The program structure**: `BarnaCoreAddressHandlerProgram` = a single repeated `bundles` field; the DMA buffer is `23·N` bytes packed back-to-back, 32-byte-aligned; termination is `prog_end` (ScalarSlot field 5) at abs bit 44 of the final bundle, not a separate terminator byte.

| | |
|---|---|
| **Merged-ALU encoder** | `EncoderJf::EncodeBarnaCoreAddressHandlerVectorAlu(int lane, …)` `@0x1e86f5c0` |
| **Harvested JF encoder** | `EncoderJf::EncodeBundleInternal` `@0x1e86c7c0` (41-byte / `0x29` JF bundle) |
| **JF ALU field writer** | `EncoderJf::EncodeVectorAluInstruction` `@0x1e864f00` (struct bytes `0x16/0x1a/0x1c/0x1d`) |
| **Per-lane slot** | `Alu0` abs 48..78, `Alu1` abs 79..109 — each 5-bit pred + 26-bit body |
| **Opcode enum** | `VectorAluOpcode` — 56 values, 6-bit (`EnumDescriptorProto @0xc01e7d3`) |
| **Y-operand enum** | `VectorAluYEncoding` — 32 values, 5-bit (`EnumDescriptorProto @0xc01dc38`) |
| **Result-dest enum** | `VectorResultDestination` — V0/V1/VLD, 3 values (`@0xc01f14e`) |
| **Base-address enum** | `BaseAddressEncoding` — ZERO/VS0/VS1/VS2, 4 values, 2-bit (`@0xc01f977`) |
| **Program encoder** | `tpu::EncodeBarnaCoreAddressHandler<EncoderJf,…>` `@0x1e841640` / `<EncoderDf,…>` `@0x1e836ea0` |
| **DMA buffer** | `23·N` bytes, `posix_memalign(buf, 0x20, 23·N)` — no header / separator / terminator |
| **Bundle dispatcher** | `EncoderJf::EncodeBarnaCoreAddressHandlerBundle` `@0x1e86fd80` |

---

## 1. The Harvest-and-Merge ALU Encoder

### Purpose

`EncodeBarnaCoreAddressHandlerVectorAlu` is the slot encoder for both ALU lanes of the 23-byte bundle. Its job is to place a `VectorAluInstruction` (opcode + operands) into the lane's 31-bit region. It does this indirectly: rather than encoding the ALU fields against the address-handler bundle, it re-uses the full JF bundle encoder as a scratch packer and harvests the bytes it produces. The merged ALU body is, byte-for-byte, the standard JF vector-ALU encoding — only relocated.

### Entry Point

```text
EncoderJf::EncodeBarnaCoreAddressHandlerBundle (0x1e86fd80)        ── per-bundle dispatcher
  ├─ EncodeBarnaCoreAddressHandlerVectorAlu(lane=0, …) (0x1e86f5c0) @+0x1b7
  └─ EncodeBarnaCoreAddressHandlerVectorAlu(lane=1, …) (0x1e86f5c0) @+0x1d5
       └─ EncoderJf::EncodeBundleInternal(temp_bundle, true) (0x1e86c7c0)   ── full 41-byte JF encoder
            └─ EncoderJf::EncodeVectorAluInstruction(…, lane, …) (0x1e864f00)
                 └─ EncoderJf::EncodeVectorAluYEncoding(…) (0x1e864be0)     ── y_encoding → imm copy
```

### Algorithm

```c
// Models EncoderJf::EncodeBarnaCoreAddressHandlerVectorAlu @0x1e86f5c0.
// lane = 0 (Alu0) or 1 (Alu1); bundle = the address-handler bundle (proto);
// bits = the 23-byte output struct (qword0 @+0, qword1 @+8, dword @+16, word @+20, byte @+22).
function EncodeAddrHandlerVectorAlu(lane, bundle, bits):
    alu = bundle.vector_slot[lane].alu.instruction   // the VectorAluInstruction proto

    // (1) BUILD A SCRATCH FULL BUNDLE and copy the ALU into the matching JF lane.
    temp = isa::Bundle()                              // stack-local, zero-initialized
    temp.vector_alu[lane].CopyFrom(alu)               // JF lane N == addr-handler lane N
                                                      //   has-bit 0x8 (lane0) / 0x10 (lane1)

    // (1a) If y_encoding names an immediate, propagate the Common.imm metadata so the
    //      JF encoder can pack it. Special-case checks at proto+0x54:
    if alu.y_encoding in {0x1d, 0x1e, 0x1f}           // cmp [alu+0x54],0x1d/0x1e/0x1f
       or BIT(0x249, alu.y_encoding)                  // bt 0x249  (the IMMx_IMMy paired forms)
       or BIT(0x4208200, alu.y_encoding):             // bt 0x4208200
        temp.common.imm_* = bundle.common.imm_*       // copy the addressed immediate slot(s)

    // (2) RUN THE STANDARD 41-BYTE JF BUNDLE ENCODER as a scratch packer.
    out = EncodeBundleInternal(temp, /*final=*/true)  // 0x1e86c7c0 → StatusOr<vector<u8>>, size 0x29=41
    // out is the JF bundle-bits struct with its 12-byte header STRIPPED (see §1.1):
    //   out[0xa..0x11] == the JF VectorAlu region (struct bytes 0x16..0x1d).

    // (3) HARVEST + MERGE the 7-byte window into the address-handler bundle.
    if lane != 0:                                     // Alu1 → qword1, mask 0xffffc000000fffff (clear bits 20..49)
        MergeAlu1(bits.qword1, out)                   // §3 Table A1
    else:                                             // Alu0 → qword0 bits53..63 + qword1 bits0..14
        MergeAlu0(bits.qword0, bits.qword1, out)      // §3 Table A0
    // The 5-bit lane predication and the lane select are NOT harvested; they are written
    // separately (Alu0 pred → abs 48, Alu1 pred → abs 79) by the predication path.
```

> **QUIRK —** the ALU encoder runs the *entire* 41-byte JF bundle encoder just to harvest 7 bytes of it. A reimplementer who tries to encode the address-handler ALU body field-by-field against the 23-byte bundle will diverge subtly, because the operand sub-field boundaries inside the harvested window follow the *JF lane-N* layout (which differs slightly between lane 0 and lane 1 — see §3), not a clean address-handler layout. The correct, byte-identical reproduction is to re-run the JF encoder and relocate its output.

### Function Map

| Function | Address | Role |
|---|---|---|
| `EncodeBarnaCoreAddressHandlerVectorAlu` | `0x1e86f5c0` | Per-lane ALU slot encoder; harvest + merge |
| `EncodeBundleInternal` | `0x1e86c7c0` | Full 41-byte JF bundle encoder (the scratch packer) |
| `EncodeVectorAluInstruction` | `0x1e864f00` | JF ALU field writer; struct bytes `0x16/0x1a/0x1c/0x1d` |
| `EncodeVectorAluYEncoding` | `0x1e864be0` | Y-encoding jump table `@0xb83450c`; copies `Common.imm` slots |
| `ValidateVectorAluInstruction` | `0x1e8632e0` | Proto-offset → field binding (opcode `@+0x50`) |
| `ProtoUtils::IsEupOpcode` | `0x1e875900` | `add edi,-0x30; cmp 5; setb` → opcodes `0x30..0x34` |
| `EncodeBarnaCoreAddressHandlerBundle` | `0x1e86fd80` | Per-bundle dispatcher; calls ALU lane 0 then 1 |

> **NOTE —** the decompile confirms the call chain directly: `EncodeBundleInternal` is invoked at line 249 of the merged-ALU encoder, `IsEupOpcode` is called on the opcode (proto offset `+0x50`, i.e. `v40+0x14` as a `uint`) at line 324, and the merge writes follow immediately after. The harvest window reads `out[0xa]` (dword), `out[0xe]` (word), `out[0x10]` (byte) — confirmed as `*(v37+10)`, `*(v37+14)`, `*(v37+16)` in the decompiled merge expressions.

---

## 1.1 The 12-Byte Header Strip

`EncodeBundleInternal`'s 41-byte output is **not** the start of its internal bundle-bits struct. On the success path the encoder does `add r15, 0xc` (`@0x1e86d766`) before `new(0x29)` and the 41-byte copy, then records the StatusOr length `[rbx+0x10] = 0x29`. So:

```text
output byte N  ==  internal-struct byte (0xc + N)
```

The internal struct's first 12 bytes hold the JF scalar / predication / immediate header — content the 23-byte address-handler bundle re-implements in its own layout and does not borrow. The JF `VectorAlu` region (struct bytes `0x16..0x1d`, where `EncodeVectorAluInstruction` writes) therefore lands at **output bytes `0xa..0x11`** — exactly the window the merge reads:

| Output byte | JF struct byte | Holds (JF VectorAlu region) |
|---|---|---|
| `0xa` (dword) | `0x16` | Alu1 opcode + Vx + Y-region low bits |
| `0xe` (word) | `0x1a` | Alu1 Y-region high / Alu0 operand |
| `0x10` (byte) | `0x1c` | overflow byte (bit 48 of the 56-bit window) |
| `0x11` (word) | `0x1d` | Alu0 opcode + low bits |

> **GOTCHA —** the 41-byte length is the *post-strip* size: the internal struct is at least `0xc + 0x29 = 53` bytes, but only the 41 bytes from offset `0xc` are emitted. Treating the 41-byte buffer as a complete JF bundle (rather than a 12-byte-stripped tail) will mis-locate every field by 12 bytes. The strip is the byte-level bridge between the 41-byte JF bundle and the 23-byte address-handler bundle.

---

## 2. The Per-Lane Slot Layout

Each ALU lane occupies a **31-bit slot**: a 5-bit predication followed by a 26-bit opcode/operand body. `Alu0` is based at abs bit 48, `Alu1` at abs bit 79. The two slots pack contiguously and fill the exact gap between the Alu0-predication base (48) and the Store slot base (110) in the parent JF/DF bundle map — a full byte-level account of the previously-unmapped 48..110 region.

```text
23-byte address-handler bundle (184 bits): the 48..110 ALU region

bit: 48      53                          78  79      84                          109  110
    +-------+-----------------------------+   +-------+-----------------------------+----...
    | Alu0  |        Alu0 body            |   | Alu1  |        Alu1 body            | Store
    | pred  | opcode 6b | operand 20b     |   | pred  | opcode 6b | operand 20b     | slot
    | 5b@48 | @53..58   | @59..78         |   | 5b@79 | @84..89   | @90..109        | @110
    +-------+-----------------------------+   +-------+-----------------------------+----...
      (written by predication path)             (written by predication path)
```

### Slot fields

| Slot field | Abs bits | Width | Source | Role |
|---|---|---|---|---|
| `Alu0` predication | 48 .. 52 | 5 | predication path | 5-bit BCS predication (lane 0) |
| `Alu0` OPCODE | 53 .. 58 | 6 | `VectorAluInstruction.opcode` | `VectorAluOpcode` (§4) |
| `Alu0` operand body | 59 .. 78 | 20 | `Vx`/`y_encoding`/`y_reg`/`dest` | JF VectorAlu operands (lane-0 layout) |
| `Alu1` predication | 79 .. 83 | 5 | predication path | 5-bit BCS predication (lane 1) |
| `Alu1` OPCODE | 84 .. 89 | 6 | `VectorAluInstruction.opcode` | `VectorAluOpcode` (§4) |
| `Alu1` `Vx` | 90 .. 94 | 5 | `consumes_vector_register`/`x_reg` | `VectorRegister` (VREG 0..31) |
| `Alu1` Y-region | 95 ..104 | 10 | `y_reg` + `y_encoding`-driven | `VectorRegister` + `VectorAluYEncoding` (§5) |
| `Alu1` Dest | 105 ..109 | 5 | `produces_register`/`destination` | `VectorRegister` |

The opcode/operand body is the standard JF VectorAlu encoding, relocated. Only the predication and the lane select are address-handler-specific. The predication bits are written by the predication path (decompile: `(pred & 0x1F) << 48` into qword0 for `Alu0`; `(pred & 0x1F) << 15` into qword1 for `Alu1`, i.e. abs bit 79), not by the harvest.

> **NOTE —** the within-window operand split is the JF ISA layout, not isolated bit-by-bit here (**INFERRED**, see §7). The merge harvests the Alu1 Y-region as a contiguous 10-bit window (the JF encoder writes `{y_reg 5b, a 5b}` into it) and the Alu0 operand as a 20-bit window. The *window extents* are byte-exact (confirmed in decompile); the internal field boundaries follow `EncodeVectorAluInstruction`'s per-field shifts. The exact `{y_reg / y_encoding-driven}` and `{Vx / Y / dest}` sub-splits inside the harvested windows were not isolated.

---

## 3. The Per-Lane Merge Arithmetic

The merge maps the 56-bit JF VectorAlu window `S` onto the address-handler qwords. Both paths are decoded byte-exact from the decompile; the two lanes use different windows because the JF encoder writes lane 0 to struct byte `0x1d` and lane 1 to struct bytes `0x16..0x1c`.

### Lane 1 (Alu1) — merge into qword1

Window: `S = out[0xa] (dword) | out[0xe] (word) << 32 | out[0x10] (byte) << 48` (56-bit; `S` bit `b` == JF struct bit `176+b`). Clear-mask `0xffffc000000fffff` clears qword1 bits 20..49. Decompile (`@0x1e86f993`, line 302):

```text
step (decompiled)                                   -> address-handler qword1
--------------------------------------------------  ----------------------------------------
(S >> 10) & 0x3F00000                                S30..35 (OPCODE)  -> qw1 bits20..25  (ABS 84..89)
2 * (S & 0x3E000000)                                 S25..29 (Vx)      -> qw1 bits26..30  (ABS 90..94)   [<<1]
(S & 0x1FF8000) << 16                                S15..24 (Y 10b)   -> qw1 bits31..40  (ABS 95..104)
((S >> 10) & 0x1F) << 41                             S10..14 (Dest)    -> qw1 bits41..45  (ABS 105..109)
```

### Lane 0 (Alu0) — merge into qword0 + qword1

Reads `out[0x11]` (word, = JF struct `0x1d`) for opcode/low, and the `out[0xe]|out[0x10]` word for the operand spill. Decompile (`@0x1e86faa3`, line 313):

```text
step (decompiled)                                   -> address-handler bundle
--------------------------------------------------  ----------------------------------------
((out[0x11] >> 5) & 0x3F) << 53                      OPCODE 6b  -> qword0 bits53..58  (ABS 53..58)
out[0x11] << 59                                      low 5b     -> qword0 bits59..63  (ABS 59..63)
((W >> 14) & 0xFFFFFFE0) | ((W >> 14) & 0x1F)        operand    -> qword1 bits0..14   (ABS 64..78)
2 * (out[0xe] & 0x3E00)                               Dest 5b    -> qword1 bits10..14  (ABS 74..78)  [<<1]
   (W = out[0xe] | out[0x10]<<16 ; clear 0xffffffffffff8000)
```

qword0 is cleared with `0x1FFFFFFFFFFFFF` (preserving bits 0..52, writing 53..63); qword1 is cleared with `0xFFFFFFFFFFFF8000` (writing bits 0..14). The trailing `2 * (out[0xe] & 0x3E00)` term (a `<<1` of word bits 9..13) places the Alu0 5-bit dest sub-field at qword1 bits 10..14 (ABS 74..78) — the same sub-field the Result-routing path's `<<10` (line 342, §6) targets for an EUP V0 result.

> **QUIRK —** the `2 *` multiply in the Alu1 path (and the `lea [reg+reg*2]` it compiles from) is a 1-bit left shift folded into the merge: the 5-bit `Vx` window sits one bit above its source position, so the encoder shifts it left by 1 while masking. A naive reimplementation that masks without the `<<1` will place `Vx` at the wrong bit. The shift is part of the field placement, not an optimization.

---

## 4. `VectorAluOpcode` — the 56-Value Opcode Enum

The ALU opcode is a 6-bit field carrying one of 56 `VectorAluOpcode` values, decoded byte-exact from its `EnumDescriptorProto @0xc01e7d3` (the `0a <len> name (12 <l> 0a <nl> NAME 10 <num>)*` serialized pattern). The symbol roster is confirmed present in the symbol table. The same op set is the [BCS Channel](bcs-32byte-bundle.md) `VectorAlu` roster and the [Scalar0/Scalar1 ISA](bcs-scalar-isa.md) shared ALU tail, here numbered at the enum-value level rather than the hardware-opcode or proto-oneof level.

| Opcode | Name | Opcode | Name |
|---|---|---|---|
| `0x00` | `VECTOR_INT_ADD` | `0x1d` | `VECTOR_SUBLANE_CIRCULAR_ROTATE_DOWN` |
| `0x01` | `VECTOR_INT_SUB` | `0x1e` | `VECTOR_RELUX` |
| `0x02` | `VECTOR_AND` | `0x1f` | `VECTOR_MOVE` |
| `0x03` | `VECTOR_OR` | `0x20` | `VECTOR_INT_EQUAL` |
| `0x04` | `VECTOR_XOR` | `0x21` | `VECTOR_INT_NOT_EQUAL` |
| `0x05` | `VECTOR_FLOAT_ADD` | `0x22` | `VECTOR_INT_GREATER` |
| `0x06` | `VECTOR_FLOAT_SUB` | `0x23` | `VECTOR_INT_GREATER_EQUAL` |
| `0x07` | `VECTOR_FLOAT_MUL` | `0x24` | `VECTOR_INT_LESS` |
| `0x08` | `VECTOR_FLOAT_MAX` | `0x25` | `VECTOR_INT_LESS_EQUAL` |
| `0x09` | `VECTOR_FLOAT_MIN` | `0x26` | `VECTOR_INT_ADD_CARRY_OUT` |
| `0x0a` | `VECTOR_LOGICAL_SHIFT_LEFT` | `0x28` | `VECTOR_FLOAT_EQUAL` |
| `0x0b` | `VECTOR_LOGICAL_SHIFT_RIGHT` | `0x29` | `VECTOR_FLOAT_NOT_EQUAL` |
| `0x0c` | `VECTOR_ARITHMETIC_SHIFT_RIGHT` | `0x2a` | `VECTOR_FLOAT_GREATER` |
| `0x0d` | `VECTOR_ROUNDING_ARITHMETIC_SHIFT_RIGHT` | `0x2b` | `VECTOR_FLOAT_GREATER_EQUAL` |
| `0x0e` | `VECTOR_CONVERT_INT_TO_FLOAT` | `0x2c` | `VECTOR_FLOAT_LESS` |
| `0x0f` | `VECTOR_CONVERT_FLOAT_TO_INT` | `0x2d` | `VECTOR_FLOAT_LESS_EQUAL` |
| `0x10`..`0x17` | `VECTOR_SELECT_VMSK0`..`VMSK7` | `0x2e` | `VECTOR_FLOAT_IS_INF_OR_NAN` |
| `0x18` | `VECTOR_LANE_ID` | `0x30` | `VECTOR_RECIPROCAL_SQUARE_ROOT` (EUP) |
| `0x19` | `VECTOR_EXTRACT_EXPONENT` | `0x31` | `VECTOR_POW_2` (EUP) |
| `0x1a` | `VECTOR_EXTRACT_SIGNIFICAND` | `0x32` | `VECTOR_LOG_2` (EUP) |
| `0x1b` | `VECTOR_COMPOSE_FLOAT` | `0x33` | `VECTOR_TANH` (EUP) |
| `0x1c` | `VECTOR_PACK_AS_HALF_FLOATS` | `0x34` | `VECTOR_RECIPROCAL` (EUP) |
| `0x3a` | `VECTOR_POP_COUNT` | `0x3c` | `VECTOR_SET_RNG_SEED` |
| `0x3b` | `VECTOR_COUNT_LEADING_ZEROS` | `0x3d` | `VECTOR_GET_RNG_SEED` |
| | | `0x3e` | `VECTOR_RNG` |

> **QUIRK —** the five EUP (extended/transcendental unit) opcodes are **contiguous** at `0x30..0x34` (rsqrt → pow2 → log2 → tanh → recip), which is exactly the range `IsEupOpcode` (`@0x1e875900`, `add edi,-0x30; cmp 5; setb`) returns true for. This is the encode-side trigger for the EUP-result drain (§6): only these five opcodes produce a deferred result needing a Result slot; every other op writes its result inline via the per-lane dest field. The same contiguous `0x30..0x34` block appears in the [BCS Channel hardware-opcode space](bcs-32byte-bundle.md), independently confirming the within-block EUP order.

> **NOTE —** opcodes `0x27`, `0x2f`, and `0x35..0x39` are unused in this enum. `VECTOR_FLOAT_ADD`/`SUB` (`0x05`/`0x06`) and the four shifts (`0x0a`..`0x0d`) are the lane-locked `Alu1`-only ops in the Pufferfish-generation lane-capability table (their absence from the `MigrateInstruction` set is documented on the [BCS bundle page](bcs-32byte-bundle.md)); the address-handler bundle inherits the same lane asymmetry through the JF lane-N harvest.

---

## 5. `VectorAluYEncoding` — the 32-Value Y-Operand Selector

The ALU's Y operand is not a raw register index but a 5-bit **selector** naming one of: a vector register, a baked hardware constant, an address-handler immediate slot, or a scalar register. Decoded byte-exact from `EnumDescriptorProto @0xc01dc38` (symbol confirmed in the symbol table). The selector is read at `VectorAluInstruction` proto offset `+0x54`; `EncodeVectorAluYEncoding` (`@0x1e864be0`) jump-tables on it (`@0xb83450c`) and copies the selected `Common.imm_*` into the bundle's immediate region.

| Value | Name | Group |
|---|---|---|
| `0` | `VECTOR_ALU_Y_VREG` | a vector register (`y_reg` field) |
| `1` | `VECTOR_ALU_Y_INTEGER_ONE` | HW constant (int +1) |
| `2` | `VECTOR_ALU_Y_INTEGER_NEGATIVE_ONE` | HW constant (int −1) |
| `3` | `VECTOR_ALU_Y_ZERO` | HW constant (zero) |
| `4`..`7` | `VECTOR_ALU_Y_FLOAT_ONE` / `_NEGATIVE_ONE` / `_TWO` / `_ZERO_POINT_FIVE` | HW constants (float ±1, +2, +0.5) |
| `8`..`13` | `VECTOR_ALU_Y_ZERO_IMM0`..`IMM5` | IMM0..5 zero-extended |
| `14`..`19` | `VECTOR_ALU_Y_ONES_IMM0`..`IMM5` | IMM0..5 ones-extended |
| `20`..`25` | `VECTOR_ALU_Y_IMM0_ZERO`..`IMM5_ZERO` | IMM0..5 in the high half |
| `26`..`28` | `VECTOR_ALU_Y_IMM1_IMM0` / `IMM3_IMM2` / `IMM5_IMM4` | paired 32-bit immediates |
| `29`..`31` | `VECTOR_ALU_Y_VS0` / `VS1` / `VS2` | scalar registers vs0/vs1/vs2 |

> **GOTCHA —** the Y selector is what makes the merge propagate immediate metadata (§1, step 1a). When `y_encoding` names an immediate (the `0x1d/0x1e/0x1f` direct checks plus the `bt 0x249` / `bt 0x4208200` bit tests covering the `IMMx_IMMy` paired forms), the merge copies the addressed `Common.imm_*` into the scratch bundle *before* running the JF encoder, so the immediate is packed into the harvested window. A reimplementation that ignores the Y selector will produce ALU ops whose Y operand reads garbage for every immediate-addressing form.

---

## 6. The `VectorResultDestination` Routing

The five EUP opcodes (`0x30..0x34`) produce a deferred result that does not land in the issuing lane's dest field — it is drained by a separate Result slot. `EncodeBarnaCoreAddressHandlerVectorResult` (`@0x1e86eb40`) encodes that slot. The 2-bit `Result.which_destination` field (proto field 2, a `VectorResultDestination`) selects where the EUP result is written back.

### Routing

```c
// Models EncodeBarnaCoreAddressHandlerVectorResult @0x1e86eb40.
function EncodeVectorResult(result, bits):
    // which_destination at [result+0x20], 2-bit value placed at dword@16 bit19..20 (ABS 147..148):
    bits.dword16 |= (result.which_destination & 3) << 0x13       // ABS 147..148
    bits.byte18  |= 0x4                                          // result-valid bit, ABS 146
    // result predication → ABS 141..145 (dword@16 << 0xd)
    switch result.which_destination:                            // cmp [result+0x20], 2/1/0 @0x1e86ec30
        case V0_DEST (0):  bits.qword1 |= result.dest << 0xa     // → Alu0 dest, ABS 74..78
        case V1_DEST (1):  bits.qword1 |= result.dest << 0x29    // → Alu1 dest, ABS 105..109
        case VLD_DEST (2): route to the vector-load destination slot
```

`ApplyEupResultTargetWorkaround` (`@0x1e8478a0`) re-targets the 2-bit field at ABS 147..148 on Jellyfish silicon — the same bits this encoder writes.

### `VectorResultDestination` enum (`EnumDescriptorProto @0xc01f14e`)

| Value | Name | Role |
|---|---|---|
| `0` | `V0_DEST` | EUP result → vector ALU lane 0 destination register (ABS 74..78) |
| `1` | `V1_DEST` | EUP result → vector ALU lane 1 destination register (ABS 105..109) |
| `2` | `VLD_DEST` | EUP result → vector-load destination (the loaded-row register) |

> **NOTE —** `VectorResultDestination` surfaces only as its `EnumDescriptorProto` name in `.rodata` (no mangled C++ symbol of its own), but the three value names `V0_DEST`/`V1_DEST`/`VLD_DEST` and the `cmp 2/1/0` routing are byte-pinned from the descriptor and the encoder's branch ladder. The result-target placement (`<<0xa` for V0, `<<0x29` for V1, confirmed at decompile lines 334/337/342) re-uses the same Alu0/Alu1 dest sub-fields the per-lane body would otherwise write — the EUP path simply writes them from the Result slot instead.

---

## 7. `BaseAddressEncoding` — the Store/Load Base-Address Mode

The Store and Load slots each carry a 2-bit base-address mode: the embedding-row address is either zero or one of three scalar base pointers. `BaseAddressEncoding` is byte-pinned from `EnumDescriptorProto @0xc01f977` (descriptor name confirmed in `.rodata`). It is the `<4`-checked value at `Store.base` (ABS 121..122) and `Load.base` (ABS 137..138) in the parent JF/DF bundle map.

| Value | Name | Role |
|---|---|---|
| `0` | `BASE_ADDRESS_ZERO` | base = 0 (absolute / no scalar base) |
| `1` | `BASE_ADDRESS_VS0` | base = vs0 scalar register |
| `2` | `BASE_ADDRESS_VS1` | base = vs1 scalar register |
| `3` | `BASE_ADDRESS_VS2` | base = vs2 scalar register |

The vs0/vs1/vs2 scalar registers are the same physical `BarnaCoreAddressHandlerScalarRegister` selectors named elsewhere (the BarnaCore-id / gradient / weight / arguments VMEM-address registers). Two adjacent shared operand enums decode the same way and are listed here as context for the wider JF vector ISA:

| Enum | Descriptor | Values |
|---|---|---|
| `OffsetEncoding` | `@0xc01f9e7` | `OFFSET_IMM_2`..`OFFSET_IMM_5` = 0..3 |
| `ShuffleEncoding` | `@0xc01fa42` | `SHUFFLE_VS0`/`VS1`/`VS2` = 1/2/3; `SHUFFLE_IMM1_IMM0`/`IMM3_IMM2`/`IMM5_IMM4` = 4/5/6 |
| `VectorRegister` | `@0xc01e0cf` | `VREG_0`..`VREG_31` = 0..31; `VREG_COUNT` = 32; `VREG_INVALID` = −1 (5-bit field) |

> **NOTE —** these three context enums share the IMM0..5 / vs0..vs2 operand-source vocabulary of `VectorAluYEncoding` (§5) — the whole JF vector ISA addresses operands through the same small set of register / immediate-slot / scalar-register selectors. A Store with `BASE_ADDRESS_VS1` and a Load with `BASE_ADDRESS_VS2` reference the same `vs1`/`vs2` the ALU's `VECTOR_ALU_Y_VS1`/`VS2` selectors do.

---

## 8. The Program-Level BCS Assembly

### Purpose

A BarnaCore address-handler program is the highest-level artifact: the byte stream that the DMA engine pushes to the BarnaCore sequencer. It is built from individual bundles with no framing whatsoever — the program is a pure concatenation of fixed-width bundles, and termination is encoded *inside* the last bundle rather than as a trailing marker.

### Program proto + DMA buffer

`BarnaCoreAddressHandlerProgram` (`DescriptorProto @0xc0188f6`) has **exactly one field**: `bundles` (field 1, repeated message `BarnaCoreAddressHandlerBundle`). There is no header field, no count field, no version field. The program length is implicit: `N = buffer_size / 23`.

The top encoder `tpu::EncodeBarnaCoreAddressHandler<EncoderJf,…>` (`@0x1e841640`; `<EncoderDf,…>` `@0x1e836ea0` is identical) allocates and fills the DMA buffer:

```c
// Models tpu::EncodeBarnaCoreAddressHandler<EncoderJf,…> @0x1e841640 (DF @0x1e836ea0 identical).
function EncodeProgram(program):
    N = program.bundles_count                       // @program+0x20
    size = 23 * N                                   // decompile: `v2 = 23LL * *(int*)(a2+32)`
                                                    //   compiled as (N*3)<<3 - N = 23N
    posix_memalign(&buf, 0x20, size)                // 32-byte aligned
    out = buf
    for bundle in program.bundles:                  // repeated-ptr walk, stride 8
        bits = EncodeBarnaCoreAddressHandlerBundle(bundle)   // 0x1e86fd80 → 23-byte struct
        // copy the 23-byte struct image back-to-back:
        memcpy16(out,        bits.qword0_qword1)    // 16 bytes (qword0 @0..7, qword1 @8..15)
        memcpy4 (out + 0x10, bits.dword16)          // dword @16..19
        memcpy2 (out + 0x14, bits.word20)           // word  @20..21
        memcpy1 (out + 0x16, bits.byte22)           // byte  @22
        out += 0x17                                 // 23; decompile: `_R14 += 23`
    return buf                                       // N×23 bytes, no header/separator/terminator
```

```text
DMA buffer layout (N×23 bytes, 32-byte aligned):

  +------------------+------------------+-----+--------------------+
  | bundle[0] (23 B) | bundle[1] (23 B) | ... | bundle[N-1] (23 B) |
  +------------------+------------------+-----+--------------------+
   ^ no program header   ^ no separator        ^ no trailing terminator/check byte
```

> **QUIRK —** unlike the Pufferfish PxC sequencer (which frames bundles with a `BundleCheckByte`), the JF/DF address-handler program has **zero** framing bytes. No header, no inter-bundle separator, no trailing check byte — the byte stream is the raw concatenation of `N` fixed 23-byte bundles. A reimplementer must not insert any alignment padding *between* bundles (the only alignment is the 32-byte alignment of the whole buffer base); each bundle is exactly 23 bytes and the next begins immediately.

### Tier-2 assembly: one instruction → one bundle

`barna_core::EncodeProgramAsProto` (`@0x141697c0`) builds the program proto. It walks the high-level `BarnaCoreAddressHandlerInstruction` span (each instruction is `0x148` bytes) and appends exactly one `BarnaCoreAddressHandlerBundle` per instruction via `EncodeBundleAsProto` (`@0x14165f60`) and `RepeatedPtrField::Add` — 1 instruction → 1 bundle, in order, with no scheduling or merging at this layer. The VLIW slot packing (folding a Load/Alu/Store/Result tuple into one instruction) happens upstream in the `MakeInstruction<Slot...>` constructor family (not traced here — see §9). `BarnaCoreAddressHandlerEmitter::AddBundles` (`@0x141604c0`) pre-allocates `N` empty bundles into the program.

### `prog_end` — the halt marker

Program termination is a single per-bundle bit, not a separate terminator. `prog_end` is `BarnaCoreAddressHandlerScalarSlot` field 5 (a bool; the ScalarSlot proto is at `@0xc017d35`, with fields `loop`/`shift_mask`/`push`/`branch`/`prog_end`). `EncoderDf::EncodeBarnaCoreAddressHandlerScalarSlot` (`@0x1e85e8a0`) writes it at **abs bit 44**:

```text
prog_end encode (decompile @0x1e85e8a0, line 55):
  *a3 = ((u64)*((u8*)scalar_slot + 0x38) << 44) | (*a3 & 0xFFFFEFFFFFFFFFFF)
        ^ ScalarSlot proto+0x38 (the prog_end bool)        ^ clear bit 44
  gated by ScalarSlot has-bit 0x10.
```

The compiler sets `prog_end = 1` in the **final** bundle's scalar control slot; the hardware sequencer halts after executing that bundle. Intra-program control flow lives in the same DF scalar slot's Branch fields — predication (ABS 30..34), branch_type (ABS 36), branch_target_pc (ABS 37..43, `&0x7f`); `prog_end` (ABS 44) is the unconditional halt.

| Field | Abs bits | Width | Encoder |
|---|---|---|---|
| Branch predication | 30 .. 34 | 5 | `EncodeBarnaCoreAddressHandlerScalarSlot` (DF) |
| Branch type | 36 | 1 | same |
| Branch target PC | 37 .. 43 | 7 | same (`&0x7f`) |
| `prog_end` | 44 | 1 | same (`<<0x2c`, mask `0xFFFFEFFFFFFFFFFF`) |

> **QUIRK —** only the **DF** address-handler scalar surface exposes Branch and `prog_end`. The JF standalone scalar-slot helper (`EncodeBarnaCoreAddressHandlerScalarSlotHelper` `@0x1e86f2e0`) does not. A reimplementer targeting the JF generation must source program termination and intra-program control flow from the DF scalar-slot encoding, consistent with the asymmetry noted in the parent JF/DF bundle work.

---

## 9. Not Traced

- **The internal sub-splits inside the harvested windows.** The Alu1 10-bit Y-region (ABS 95..104) and the Alu0 20-bit operand body (ABS 59..78) are harvested as contiguous windows; the merge does not isolate the `{y_reg / y_encoding-driven}` and `{Vx / Y / dest}` sub-fields bit-by-bit. The window extents are byte-exact; the within-window boundaries follow the JF `EncodeVectorAluInstruction` per-field shifts but were not individually pinned (**INFERRED**).
- **The decode side.** No `BarnaCoreAddressHandler` ALU decoder reading the 23-byte bundle back into a `VectorAluInstruction` exists in this build — the address-handler path is encode-only. The encode-side merge map is authoritative but not cross-validated by an independent reader.
- **The `MakeInstruction<Slot...>` VLIW-packing constructors.** Which tuples of `{ScalarSlot, VectorLoad, VectorStore, VectorAluSlot, EupResultRead}` are legal in one instruction (the `address_handler_program_constructors::MakeInstruction<…>` family, 9 template overloads `@0xfa96040..0xfa96680`) and their slot-conflict rules — the scheduling layer above this byte map — were not traced.
- **`consumes_scalar_register` (proto field 11) in the merged body.** `x_reg` / `consumes_vector_register` / `produces_register` map to the Vx/dest body fields; the scalar-register operand path was not isolated in the merge (it may ride the Common vs0/vs1/vs2 scalar selectors rather than the ALU body) (**INFERRED**).
- **Whether the HW decoder reads all 26 body bits per lane.** The encoder writes the full standard-JF body; the silicon field widths are **INFERRED** to match (no decode-side reader; the harvested window is 26 bits and fits the 31-bit slot).

---

## Related Components

| Name | Relationship |
|---|---|
| `EncodeBarnaCoreAddressHandlerVectorAlu` (`@0x1e86f5c0`) | The harvest-and-merge ALU slot encoder this page decodes |
| `EncodeBundleInternal` (`@0x1e86c7c0`) | The full 41-byte JF bundle encoder the ALU harvests |
| `tpu::EncodeBarnaCoreAddressHandler<EncoderJf/Df,…>` | The program encoders; `23·N`-byte DMA buffer, no framing |
| `barna_core::EncodeProgramAsProto` (`@0x141697c0`) | Tier-2 assembly; 1 instruction → 1 bundle |
| `EncoderDf::EncodeBarnaCoreAddressHandlerScalarSlot` (`@0x1e85e8a0`) | Writes `prog_end` (ABS 44) + Branch control flow |

## Cross-References

- [Overview](overview.md) — BarnaCore, the legacy embedding accelerator: where the address-handler datapath sits in the pipeline
- [BCS 32-Byte Bundle](bcs-32byte-bundle.md) — the Pufferfish-generation Channel/Sequencer bundle; the same `VectorAluOpcode` op set, hardware-opcode-numbered, and the `MigrateInstruction` lane-capability table this ALU inherits
- [BCS Scalar0/Scalar1 ISA](bcs-scalar-isa.md) — the per-op control+memory ISA whose shared ALU tail overlaps the `VectorAluOpcode` enum
- [Per-Generation Perf Grids](per-gen-perf-grids.md) — the priced primitive grids that place the JF/DF vs PxC vector-ALU ops in cost terms
- [Retirement](retirement.md) — the BarnaCore↔SparseCore retirement matrix and the BCAH=2 address-handler personality byte this bundle belongs to
- [Index](../index.md) — Part IX — SparseCore & BarnaCore / BarnaCore (legacy v2–v4)
