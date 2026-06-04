# TPUInstrNameData / Descs / RegEncoding

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

Three opcode-indexed tables sit beside [InstBits](instbits-master-db.md) in the TPU back end's `.lrodata` and supply everything the bit-layout database does not: the mnemonic of each opcode, the descriptor metadata (`MCInstrDesc`) that says how many operands an opcode has and how each is encoded, and the register-encoding map that turns a virtual register number into the hardware bits an instruction field carries. They are the standard LLVM `TPUGenInstrInfo` / `TPUGenRegisterInfo` TableGen outputs, embedded verbatim:

- **`TPUInstrNameData`** (`0x33f2be0`, 274764 B) — a flat, null-terminated mnemonic string pool, indexed indirectly through **`TPUInstrNameIndices`** (`0x3435d30`, `6166 × u32` byte offsets). `mnemonic(op) = TPUInstrNameData + TPUInstrNameIndices[op]`.
- **`TPUDescs`** (`0x33bf650`, 210320 B = `0x33590`) — the per-opcode `MCInstrDesc` array: `6166` records at a `32`-byte stride (`6166 × 32 = 197312` B), holding `{NumOperands, NumDefs, Size, SchedClass, Flags, TSFlags}` and the operand-info index the operand encoders consult. The remaining `13008` B of the symbol (`210320 − 197312`) hold the trailing operand-info / implicit-operand arrays the descriptors index into; the symbol size is *not* `6166 × 32`.
- **`TPURegEncodingTable`** (`0x34469b0`, `889 × u16`) — register number → hardware encoding bits, the `movzwl (table, reg, 2)` lookup behind every register operand.

All three are wired into the MC layer by `createTPUMCInstrInfo` (`0x13c7a500`), which allocates a 64-byte `MCInstrInfo`, stores `NumOpcodes = 6166`, and points it at `TPUInstrNameIndices` and `TPUInstrNameData`. This page documents each table's record layout, its index space, and the accessor that reads it — the metadata side of the encoder, where [InstBits](instbits-master-db.md) is the bit side and the [239-bit record](record-format.md) is what they jointly produce.

For reimplementation, the contract is:

- The two-level mnemonic lookup: a `6166`-entry `u32` offset array into a flat string pool, not a fixed-stride name table.
- The opcode index space: `6166` opcodes total; opcodes `0..498` are pseudo / target-independent, `499..6165` are TPU MC opcodes (the same band InstBits indexes at `opcode − 499`).
- The `TPUDescs` `32`-byte `MCInstrDesc` record and the `getSpecialOpEncoding` consult that reads the per-operand encoding class out of it.
- The `TPURegEncodingTable` `u16` lookup and the register-number blocks it partitions (predicate `1..15`, scalar / vector descending).

| | |
|---|---|
| **Name pool** | `TPUInstrNameData` @ `0x33f2be0`, `0x4314c` (274764 B), null-terminated strings |
| **Name index** | `TPUInstrNameIndices` @ `0x3435d30`, `0x6058` (6166 × u32 byte offsets) |
| **Descriptors** | `TPUDescs` @ `0x33bf650`, `0x33590` (6166 × 32 B `MCInstrDesc`) |
| **Reg encoding** | `TPURegEncodingTable` @ `0x34469b0`, `0x6f2` (889 × u16) |
| **Wiring** | `createTPUMCInstrInfo` @ `0x13c7a500` (`new(0x40)`, `NumOpcodes = 6166`) |
| **Mnemonic accessor** | `TPUInstrNameData + TPUInstrNameIndices[op]` |
| **Descriptor consult** | `getSpecialOpEncoding(MCInstrDesc&, opno)` @ `0x13c63a80` |
| **Opcode count** | 6166 (`0x1816`); pseudo `0..498`, TPU MC `499..6165` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Index Space and Wiring

All three tables share one opcode index space, set up by `createTPUMCInstrInfo`. The decompiled body fixes the count and the pointers:

```c
// createTPUMCInstrInfo @ 0x13c7a500 (decompiled)
MCInstrInfo *info = operator new(0x40);          // 64-byte MCInstrInfo
info->vtable        = &TPUMCInstrInfo_vtable;     // result[0]
info->NameIndices   = TPUInstrNameIndices;        // result[+8]  (GOT-relative ptr)
info->NameData      = TPUInstrNameData;           // result[+0x10] (GOT-relative ptr)
info->Descs         = TPUDescs;                   // (zeroed-then-set region +0x18)
info->NumOpcodes    = 6166;                       // result[+0x28] = 0x1816
return info;
```

`NumOpcodes = 6166` is the literal `*(_DWORD *)(result + 40) = 6166` in the decompiled function — the canonical anchor for the size of every opcode-indexed table on this page. The name-table pointers land at `+8` (`TPUInstrNameIndices`) and `+0x10` (`TPUInstrNameData`); the IDA decompiler renders these GOT-relative loads as the unrelated strings they happen to point near (`"y?"`, `"G_FLOG10"`), which is a disassembly artifact, not the real value.

The index space splits at opcode `499`:

| Opcode band | Count | Role | Tables that cover it |
|---|---:|---|---|
| `0..498` (`≤ 0x1F2`) | 499 | pseudo / target-independent (`PHI`, `INLINEASM`, MC pseudo branches) | name + desc only; **not** InstBits |
| `499..6165` (`0x1F3..0x1815`) | 5667 | TPU MC opcodes | name + desc + InstBits (`index = opcode − 499`) |

The `5667` count in the second band is exactly the InstBits row count — the same opcodes, indexed at `opcode − 499` there and at `opcode` directly in the name and descriptor tables. So a reimplementer holds one opcode enum across all four tables, subtracting `499` only for the InstBits bit-layout lookup. See [InstBits DB](instbits-master-db.md#in-binary-representation).

---

## TPUInstrNameData / TPUInstrNameIndices

### Layout

The mnemonic database is two arrays. `TPUInstrNameIndices` is a flat `6166 × u32`; entry `op` is a **byte offset** into `TPUInstrNameData`, the null-terminated string pool. The accessor is one indirection:

```c
// mnemonic of opcode `op`
const char *mnemonic(uint32_t op) {
    return TPUInstrNameData + TPUInstrNameIndices[op];   // op in [0, 6166)
}
```

The two-level form (offset array + pooled strings) is the standard LLVM `getInstrName` representation; it deduplicates shared substrings and avoids a fixed name width. The index array is `6166 × 4 = 24664 = 0x6058` bytes (matches the symbol size); the pool is `274764 = 0x4314c` bytes. The largest observed index is `274754`, just inside the pool's `274764`-byte extent — confirming the index array addresses the whole pool and the pool size is exact.

### Verified opcode → mnemonic samples

These rows are the primary anchors that pin the index space; they are read directly from the two tables, not inferred:

| Opcode | Hex | Mnemonic | Note |
|---:|---|---|---|
| 0 | `0x000` | `PHI` | target-independent |
| 1 | `0x001` | `INLINEASM` | |
| 239 | `0x0ef` | `G_PTRMASK` | the `0xEF` generic opcode |
| 499 | `0x1f3` | `ADDri` | first TPU MC opcode (InstBits index 0) |
| 505 | `0x1f9` | `BRabs` | sequencer: absolute branch |
| 507 | `0x1fb` | `BRind` | sequencer: indirect branch |
| 508 | `0x1fc` | `BRrel` | sequencer: relative branch |
| 509 | `0x1fd` | `BRrelrot` | sequencer: rel branch + rotate |
| 514 | `0x202` | `CALLabs` | sequencer: absolute call |
| 515 | `0x203` | `CALLrel` | sequencer: relative call |
| 540 | `0x21c` | `EVENT` | special-cased in the predicate-index finder |
| 571 | `0x23b` | `HALT` | sequencer: halt |
| 3977 | `0xf89` | `bcHALT` | BarnaCore halt |
| 3978 | `0xf8a` | `bcLOOP_START` | BarnaCore loop slot |
| 3982 | `0xf8e` | `bcVLDi` | BarnaCore vector load (imm) |
| 3983 | `0xf8f` | `bcVLDr` | BarnaCore vector load (reg) |
| 3991 | `0xf97` | `bcVSTr` | BarnaCore vector store (reg) |

> **NOTE —** the sequencer `BR` (325), `BRcond` (328), `BRcondrot` (330), `BRret` (331) opcodes sit *below* 499 — they are MC pseudo branches expanded before MC emission and never reach the InstBits switch. The *concrete* forms (`BRabs`/`BRind`/`BRrel`/`BRrelrot`/`CALLabs`/`CALLrel`/`HALT`) are `≥ 499` and do reach the encoder, but route to the zero-base default and are encoded by the proto-bundle path. A reimplementer driving instruction selection off the mnemonic table must distinguish the pseudo band (`< 499`) from the MC band (`≥ 499`); only the latter has InstBits and descriptor encoding semantics. See [InstBits DB §Field Mapping](instbits-master-db.md#field-mapping-slot-to-absolute-bit-positions).

---

## TPUDescs — Per-Opcode `MCInstrDesc`

### Record layout

`TPUDescs` is the LLVM `MCInstrDesc` array: one descriptor per opcode, `6166` entries, decoding cleanly at a **32-byte stride** (`6166 × 32 = 197312` B of descriptor records; the leading `uint16` decrements `6165, 6164, …` and reaches `0` exactly at entry `6165`, the `6166`th record — a byte-anchored confirmation of the entry count). The `0x33590` (`210320`-B) symbol is larger than `197312` B; the trailing `13008` B are the operand-info / implicit-operand arrays the descriptors index into. Each descriptor record carries the standard `MCInstrDesc` payload — `{NumOperands, NumDefs, Size, SchedClass, Flags, TSFlags}` plus an operand-info index. The first three entries, viewed as `uint16` tuples:

```text
entry0:  (6165, 0, 7, 3, 117, 0, 1504, 0)
entry1:  (6164, 0, 5, 1, 116, 0, 1499, 0)
entry2:  (6163, 0, 7, 3, 115, 0, 1504, 0)
```

The leading `uint16` decrements (`6165, 6164, 6163, …`) — it is an operand-info / implicit-ops index, not the opcode itself. The descriptor is the source of the per-operand encoding decisions the bit emitter makes: `(pos, width)` of a deposit is fixed by the instruction class, but *which encoding* a given operand takes (register vs special-immediate vs expression) is read here.

> **GOTCHA —** the `TPUDescs` stride is **32 bytes**, not the historical LLVM `MCInstrDesc` `24`-byte size: the leading `uint16` decrements by one every `32` bytes (`6165, 6164, …`) and hits `0` at entry `6165`, the `6166`th record — matching `createTPUMCInstrInfo`'s `NumOpcodes`. The descriptor *array* is `6166 × 32 = 197312` B, not the full `0x33590` (`210320`-B) symbol — neither `0x33590 / 32` (`= 6572.5`) nor `0x33590 / 24` (`≈ 8763`) yields the entry count, because the symbol bundles trailing operand-info data after the descriptor records. The `32`-byte stride and `6166` count are CONFIRMED; the exact `uint16` field-offset binding for this struct version (which `uint16` is `NumOperands` vs the operand-info index) is MEDIUM confidence.

### The descriptor consult

The descriptor is read during operand lowering by `getSpecialOpEncoding(MCInstrDesc&, opno)` (`0x13c63a80`), called from `getMachineOpValue`. The decompiled body indexes a 32-byte-stride descriptor and binary-searches an encoding-compatibility table to return a per-operand encoding class:

```c
// getSpecialOpEncoding @ 0x13c63a80 (decompiled, condensed)
uint32_t opcode_field = *(uint32_t *)desc;
uint8_t  enc_class    = *((uint8_t *)desc + 32*opcode_field
                          + 6*desc->numImplicit + 6*opno + 35);  // 32B-stride desc record
if (enc_class >= 0x0D) {                                          // special-encoding class
    // binary search ImmediateCompatibilityTable (17 entries)
    // return matched (class | 0x100000000) or fall through
}
// binary search the 702-entry per-opcode encoding table (GOT - 65201892)
//   keyed on opcode_field, with a per-operand bittest gate
return matched ? (class | 0x100000000) : 0;
```

The `32 * opcode_field` term confirms the 32-byte descriptor stride from a second, independent site (the descriptor consult, distinct from `createTPUMCInstrInfo`). The function returns a `(found, class)` pair: the high bit (`| 0x100000000`) flags "this operand has a special encoding," and the low 32 bits are the class id the bit emitter uses to choose how to lower the operand. This is why the same `insertBits` site in a case body can deposit a raw immediate, a relocatable expression, or a label fixup depending on the operand — the position is class-fixed, the value is descriptor-driven. See [Record Format §Operand Value Sources](record-format.md#operand-value-sources).

---

## Register Encoding (`TPURegEncodingTable`)

### Layout and lookup

`TPURegEncodingTable` (`0x34469b0`, `889 × u16`, `0x6f2` bytes) maps an LLVM register number to the hardware encoding bits a register operand carries. The lookup is a single `uint16` load, rendered in the disassembly as `movzwl (table, reg, 2)` — i.e. `table[reg]` at a 2-byte stride:

```c
// register operand encoding, inside getMachineOpValue / encodePredicateOperand
uint16_t reg_enc = *(uint16_t *)(TPURegEncodingTable + 2 * reg_index);
```

In `encodePredicateOperand` this is the exact deposit `insertBits(dst, *(u16*)(table + 2*reg_index), 0, 4)` — the predicate field's 4-bit register index. The maximum value in the table is `128`, so every encoding fits in 8 bits; the field width is set by the instruction class, not the table.

### Register-number blocks

The table partitions the register-number space into blocks that align with the register classes. The visible structure:

| Block | Register numbers | Encoding values | Field width |
|---|---|---|---:|
| predicate | `P0..P14` | `1..15` | 4 bits (in the predicate field) |
| scalar / vector | descending blocks | `0..128` | per-class |

The predicate block holding `1..15` (`P0..P14`) is the byte-anchored reason the predicate field's register index is exactly 4 bits — the same `15` that appears as `kPredicateRegisterCount` / `kAlwaysExecute` in the per-gen hardware-bundle constants and as the `kNeverExecute = 31` skip encoding (a 5-bit field where `0..14` reference registers, `15` is always-execute, `31` is never-execute). The full reg# → (class, encoding) partition for the scalar and vector blocks needs the `TPURegClassInfos` (`0x334ea60`) and `TPURegDesc` (`0x343e7b0`) cross-decode and is left MEDIUM confidence here. See [ArchRegno Numbering](archregno-numbering.md) for the runtime register-numbering side.

> **GOTCHA — register number is not register encoding.** `TPURegEncodingTable[reg]` is a translation, not the identity. A reimplementation that deposits the LLVM register number directly into an instruction field (instead of `table[reg]`) will misencode every register operand, silently — the bit field is the right width and the value is wrong. The table is consulted on the value path of every register operand, in both `getMachineOpValue` and `encodePredicateOperand`.

---

## Cross-References

- [InstBits DB](instbits-master-db.md) — the per-opcode base-bits database these tables feed; the bit positions (the `(pos, width)` windows) `TPUDescs` and `TPURegEncodingTable` supply *values* for.
- [MC-Emitter](mc-emitter.md) — `getBinaryCodeForInstr` and the operand encoders (`getMachineOpValue`, `encodePredicateOperand`) that read all three tables on this page.
- [ArchRegno Numbering](archregno-numbering.md) — the runtime register-numbering (`ToArchRegno` / register-numbering init) that produces the register numbers `TPURegEncodingTable` translates.
