# 239-Bit Record Format

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

The TPU MC layer encodes one LLO machine instruction into a fixed-width `llvm::APInt` of **239 bits** — a 4-word (`4 × uint64`, 32-byte) value, of which the top 17 bits are unused padding. This is the intermediate record the LLVM-MC emitter (`TPUMCCodeEmitter::getBinaryCodeForInstr`, `0x13c74da0`) fills before the bytes are sliced into a per-generation VLIW bundle. The width is not a free choice: the emitter constructs the `APInt` with `BitWidth = 0xEF = 239` and `NumWords = 4` directly from a per-opcode base-bits table (`InstBits`), then overwrites operand-shaped *holes* in that base with `APInt::insertBits(value, pos, width)`. The 239-bit window is sized to the widest single BarnaCore VLIW slot: the highest field deposits in `getBinaryCodeForInstr` are a 16-bit field at `insertBits(…, /*pos=*/0xDF=223, /*width=*/16)` and a 64-bit field at `insertBits(…, /*pos=*/0xAF=175, /*width=*/64)`, both of which end at bit **238** — the highest bit the emitter ever writes, which is exactly one below the 239-bit width. The window is the same width regardless of opcode, generation, or HwMode.

Three facts drive the page, and a reimplementer needs all three:

- **The record is a wide-instruction `APInt`, not a `uint64`.** `getBinaryCodeForInstr` carries *two* trailing `APInt&` outputs — LLVM's wide-instruction emitter form for instructions wider than 64 bits. The first (`a3`) is the assembled 239-bit record; the second (`a4`) is a per-operand scratch `APInt` the emitter zeroes and reuses between fields. A reimplementation that returns a single 64-bit code word cannot represent a TPU MC instruction.
- **The record is base-bits plus insertBits holes.** The 239-bit value starts as a copy of `InstBits[opcode−499]` (a 32-byte row). The set bits are the fixed opcode discriminator and default field values; the *zero holes* are exactly the `(pos, width)` windows the operand encoders fill. Every populated field is written by an `insertBits(value, pos, width)` call whose `(pos, width)` is fixed per encoding class.
- **The default base is all-zero for TensorCore and V5+.** On disk the default `InstBits` table (`0x3366d90`, `0x2c460` B) is entirely zero — verified byte-for-byte, zero non-zero bytes and no relocations. Only the `InstBits_BarnaCorePxcHwMode` variant (`0x33931f0`) carries real base bits. For every TensorCore and Viperfish/Ghostlite/`6acc60406` opcode the record arrives at the emitter all-zero, no `insertBits` runs, and the actual bundle bytes are produced by the separate proto-bundle `EmitX` → `<Slot>Encoder::Encode` path. See [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md).

| | |
|---|---|
| **Record type** | `llvm::APInt`, `BitWidth = 0xEF = 239`, `NumWords = 4` (32 B) |
| **Filled by** | `TPUMCCodeEmitter::getBinaryCodeForInstr` @ `0x13c74da0` |
| **Base-bits table** | `InstBits` @ `0x3366d90` (default, all-zero) / `InstBits_BarnaCorePxcHwMode` @ `0x33931f0` (populated) |
| **Records per table** | 5667 rows (`0x2c460` table size `/ 32` B stride); index `opcode − 499`; the bound test is `4·index < 0x588D` |
| **First real opcode** | `0x1f3 = 499` (`ADDri`); opcodes `≤ 498` are pseudo/target-independent |
| **Operand primitive** | `APInt::insertBits(value, pos, width)` |
| **Padding** | bits `[239:255]` (top 17 of the 256-bit storage) unused |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The 239-Bit Window

A TPU MC instruction record is one row of a per-opcode TableGen array. The row is 32 bytes — four little-endian `uint64` words — and the emitter interprets it as a 239-bit `APInt`:

```text
 word0[63:0]   word1[127:64]   word2[191:128]   word3[255:192]
 |--------------- 239-bit instruction value ----------------|·············|
 bit 0 = LSB of word0                          bit 238   bits 239..255 = padding
```

The width is read straight out of the emitter prologue. The disassembly resolves to `APInt(/*BitWidth=*/239, ArrayRef<uint64>(record, /*NumWords=*/4))`, and the decompiled body confirms the literal:

```c
// TPUMCCodeEmitter::getBinaryCodeForInstr @ 0x13c74da0 (prologue, decompiled)
v5 = *(uint32_t *)a2;                  // v5 = MCInst opcode
if (*(uint32_t *)a2 <= 0x1F2u)         // opcode <= 498 -> not an MC opcode
    reportUnsupportedInst(a2, a2);

v8 = (uint32_t)(4 * v5 - 1996);        // index*4 ; 1996 = 0x7CC = 499*4
if ((uint32_t)v8 >= 0x588D)            // bound: index*4 < 22669 -> 5667 records
    __asm { ud1 };                     // out of range -> trap

// record = InstBits + (opcode-499)*32  (the GOT-relative base resolves to 0x3366d90)
APInt(&record, /*BitWidth=*/239, &GLOBAL_OFFSET_TABLE_ + v8 - 65189758, /*NumWords=*/4);
```

Two derived constants are worth pinning. The record stride is **32 bytes** (`4 × uint64`), so `(opcode − 499) × 4` words advance one row. The on-disk `InstBits` symbol is `0x2c460 = 181344` bytes = `5667 × 32`, so the table holds **5667 rows** (valid indices `0 … 5666`). The bound test `index·4 < 0x588D` (= `22669`) admits `index·4 ≤ 22668`, i.e. `index ≤ 5667` — one slot wider than the 5667-row table; index 5667 would read 32 bytes past the symbol. The trap protects against opcodes far above the valid range, not against this single boundary index. The first row (index 0) belongs to opcode `0x1f3 = 499` (`ADDri`); the named-mnemonic database (`TPUInstrNameIndices` @ `0x3435d30` / `TPUInstrNameData` @ `0x33f2be0`) confirms, byte-exactly, `[499] = ADDri`, `[505] = BRabs`, `[571] = HALT`, `[3978] = bcLOOP_START` (with `[0] = PHI` among the pseudo opcodes below 499). See [InstBits DB](instbits-master-db.md).

> **NOTE —** the 256-bit storage minus the 239-bit logical width leaves bits `[239:255]` as dead padding. A reimplementation must mask field writes to the 239-bit window; an `insertBits` that runs off the high end is a silent encoding bug because the `APInt` storage tolerates the spill.

---

## Base Bits and Operand Holes

The record is never built field-by-field from scratch. It begins as a verbatim copy of the per-opcode base row, and the operand encoders then overwrite the holes:

```c
// conceptual fill order, per populated (BarnaCore) opcode
APInt record(239, InstBits_BarnaCorePxcHwMode[opcode - 499], /*NumWords=*/4); // base
for (operand i in this encoding class) {
    uint64_t v = encode_operand(i);          // reg# / imm / predicate / expr
    record.insertBits(v, pos_i, width_i);    // (pos_i, width_i) fixed per class
}
```

The set bits in the base encode the fixed opcode discriminator and any default field values; the **zero holes** mark exactly the `(pos, width)` windows the loop writes into. The two facts are coupled: the base value of a BarnaCore opcode and its class's `insertBits` sequence are complementary, and the hole map can be recovered either by reading the base zero-runs or by reading the case body's `insertBits(pos, width)` arguments — they agree bit-for-bit.

For the **default** (TensorCore / V5+) opcodes the base row is all-zero and the encoding class is the no-op default case: the record is constructed, no `insertBits` runs, and the emitter returns the zero record. The instruction's real field bits are written elsewhere — by the proto-bundle `EmitX` populators and the per-slot `<Slot>Encoder::Encode` `BitCopy` calls — never through this 239-bit record. This is why the default `InstBits` table is genuinely zero on disk and carries no relocations: there is nothing to relocate because nothing reads it.

| Opcode group | Base row | insertBits in `getBinaryCodeForInstr` | Where the bytes come from | Confidence |
|---|---|---|---|---|
| BarnaCore-Pxc (`_V0/_V1/_V2/_VM`, `bc*`) | populated (`InstBits_BarnaCorePxcHwMode`) | yes — per encoding class | the 239-bit record | CONFIRMED |
| TensorCore / Viperfish / Ghostlite / `6acc60406` | all-zero (`InstBits` default) | none (default case) | proto-bundle `EmitX` + `<Slot>Encoder::Encode` | CONFIRMED |
| pseudo / target-independent (`opcode ≤ 498`) | n/a | n/a — `reportUnsupportedInst` | expanded before MC emission | CONFIRMED |

---

## The insertBits Primitive

`APInt::insertBits(value, pos, width)` is the only field-write primitive in the record path. It deposits the low `width` bits of `value` at bit offset `pos` of the destination `APInt`, LSB-first, leaving every other bit untouched. The emitter uses it in two shapes, both visible in the decompiled body.

The first shape is a direct deposit of a small constant or a register-encoding lookup. The predicate operand is the cleanest example; its three writes give the canonical 7-bit predicate field at bits `[0:6]` of the field window:

```c
// TPUMCCodeEmitter::encodePredicateOperand @ 0x13c77c40 (decompiled)
// a4 = destination APInt ; reg-encoding table at a1 ; operand record at a2
insertBits(a4, RegEncodingTable[ a2->preg_index ], /*pos=*/0, /*width=*/4); // reg index
if (a2->flags & 1)                                                          // negate bit
    a4.word0 |= 0x10;                                                       //   -> bit 4
insertBits(a4, (a2->flags >> 5) & 3, /*pos=*/5, /*width=*/2);               // mode, bits 5:6
```

The second shape is a *stage-then-extract-then-deposit* idiom used for general operands. The emitter zeroes the scratch `APInt` (`a4`), calls `getMachineOpValue` to lower the operand into it, reads the low bits back with `extractBitsAsZExtValue`, and deposits them into the real record (`a3` / `v260`) at the class-fixed position. Concretely, from a load-slot class body:

```c
// excerpt: a BarnaCore load-slot operand fill (getBinaryCodeForInstr, decompiled)
*a4 = 0;                                              // clear the per-operand scratch
getMachineOpValue(self, inst, &inst->op[1], a4);     // lower operand -> a4
v = extractBitsAsZExtValue(a4, /*width=*/5, /*lo=*/0);// read its low 5 bits
insertBits(record, v, /*pos=*/0x58, /*width=*/5);    // deposit at bit 88 of the record
```

The scratch indirection exists because `getMachineOpValue` may itself emit fixups, set multiple sub-fields, or widen the value; staging it in a private `APInt` keeps those side effects out of the record until the final, position-pinned `insertBits`. A reimplementation can collapse the scratch round-trip into a direct masked deposit *only* if it reproduces `getMachineOpValue`'s exact low-bit value — the staged path is observable through the fixup list, not just the final bits.

> **QUIRK — the second `APInt&` output is scratch, not a second result.** LLVM's wide-instruction emitter signature passes two `APInt&`s. On most targets the second is a high-word continuation. On TPU it is reused as a per-operand staging buffer: it is `memset`-zeroed before each operand, written by `encodePredicateOperand` / `getMachineOpValue`, read back by `extractBitsAsZExtValue`, and then discarded. Only `a3` survives as the instruction record. A reimplementation that treats `a4` as a high-order word produces garbage above bit 64.

---

## Operand Value Sources

`getMachineOpValue` (`0x13c777e0`) is the generic operand lowering that feeds the staged `insertBits`. It resolves three operand kinds, selected by consulting the per-opcode descriptor:

- **Register operands** → a `uint16` lookup in `TPURegEncodingTable` (`0x34469b0`, 889 entries, reg# → hardware encoding). The decompiler renders this as `*(uint16_t *)(table + 2*reg_index)`. The predicate-register block of this table holds values `1..15` (`P0..P14`), which is why the predicate field's register index is 4 bits.
- **Immediate / expression operands** → `getSpecialOpEncoding(MCInstrDesc&, opno)` (`0x13c63a80`), which reads the per-operand encoding class from the opcode's descriptor (`TPUDescs`, consulted via `getMachineOpValue`). This is how the same `insertBits` site can encode a raw immediate, a relocatable expression, or a label fixup depending on the operand's descriptor class.
- **Label / symbol operands** → emitted as an `MCFixup` appended to the fixup list (the first non-`APInt` argument of the emitter), with the bit window in the record left for the linker/assembler to patch.

The descriptor consult ties the record format to the descriptor table: a reimplementation that hard-codes "operand 1 is a register" misencodes any opcode whose descriptor marks that operand as a special-encoding immediate. The `(pos, width)` of the deposit is fixed per class, but *what value goes there* is a descriptor-driven decision per operand. See [InstBits DB](instbits-master-db.md) for the descriptor and register-encoding tables.

---

## Relationship to the Bundle Byte-Widths

The 239-bit record is wider than any single per-generation bundle slot but narrower than a whole bundle. It is a *per-instruction* intermediate, not a *per-bundle* one: one LLO machine instruction → one 239-bit record → (via the bundle packer) one slot's worth of bytes in the gen-specific bundle word. The bundle widths it feeds are fixed per generation:

| Codename (ordinal) | Public name | Bundle bytes | Bundle bits | MC record path | Confidence |
|---|---|---|---|---|---|
| Jellyfish (0) | TPU v2 | 41 | 328 | proto-direct (no `insertBits` record) | CONFIRMED |
| Dragonfish (1) | TPU v3 | 41 | 328 | shares the Jellyfish codec path | CONFIRMED |
| Pufferfish (2, BarnaCore) | TPU v4 | 51 | 408 | **239-bit record + insertBits** (`InstBits_BarnaCorePxcHwMode`) | CONFIRMED |
| Viperfish (3) | TPU v5p (+v5e lite) | 64 | 512 | zero base → proto-bundle `EmitX` | CONFIRMED |
| Ghostlite (4) | TPU v6e | 64 | 512 | zero base → proto-bundle `EmitX` | CONFIRMED |
| `6acc60406` (5) | TPU v7 | 64 | 512 | zero base → proto-bundle `EmitX` | CONFIRMED |

The single generation whose bundle bytes actually flow through this 239-bit record is the **Pufferfish BarnaCore** HwMode: its vector-ALU lanes (`_V0/_V1/_V2/_VM`) and native ops (`bc*`) are the **704 populated rows** of `InstBits_BarnaCorePxcHwMode` (verified by counting non-zero 32-byte rows; the first is opcode `2855`), and their field positions are recoverable from the record. The highest-positioned deposits seen in `getBinaryCodeForInstr` are a 16-bit field at `pos = 0xCF = 207` (bits `[207:222]`), a 16-bit field at `pos = 0xDF = 223` (bits `[223:238]`), and a 64-bit field at `pos = 0xAF = 175` (bits `[175:238]`) — all ending at **bit 238**, the highest bit the emitter writes. That bit-238 ceiling is why the 239-bit window is sized as it is: it must hold the largest single BarnaCore slot with room for the opcode discriminator below it. For the V5+ generations the record is a formality: it is built, found all-zero, and bypassed in favour of the bundle byte buffer the proto-bundle encoders write directly. See [Bundle Model](bundle-model-overview.md) for the per-generation bundle word layout and slot map.

> **NOTE —** the bundle width is *not* the record width. The 239-bit record is the MC-emitter's working unit for one instruction; the bundle packer is what places (a slice of) that record into the 41/51/64-byte bundle word at a slot-relative offset. Conflating the two — assuming a 64-byte V5+ bundle is "two and a half 239-bit records" — does not hold, because on V5+ the record contributes zero bits and the bundle is assembled entirely by `BitCopy`.

---

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the per-generation 41/51/64-byte VLIW bundle word and slot map the record feeds.
- [MC-Emitter](mc-emitter.md) — `getBinaryCodeForInstr`, the per-opcode dispatch, and the full emit pipeline that fills this record.
- [InstBits DB](instbits-master-db.md) — the `InstBits` / `InstBits_BarnaCorePxcHwMode` base-bits tables, `TPUDescs`, `TPUInstrNameData`, and `TPURegEncodingTable` that the record is built from.
- [Kisatable Data Sections](kisatable-data-sections.md) — the on-disk addresses and byte sizes of `TPUDescs`, `TPUStages`, `TPUInstrNameData`, and `TPUInstrNameIndices` this record's tables sit beside.
- [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md) — the proto-bundle `EmitX` → `<Slot>Encoder::Encode` path that produces the real bytes when this record is all-zero (every TensorCore / V5+ opcode).
