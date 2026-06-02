# InstBits Master DB

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

`InstBits` is the LLVM TableGen per-opcode base-bits database that drives `TPUMCCodeEmitter::getBinaryCodeForInstr`. It is the TPU back end's instance of the array LLVM names `getBinaryCodeForInstr::InstBits` on every target: a dense, opcode-indexed table of fixed instruction bits that the MC emitter copies into a working record before the operand encoders overwrite the operand-shaped holes. On a conventional target this table is the *whole* encoding — the set bits are the opcode discriminator and constant sub-fields, the zero runs are where operands go. On TPU it is that for exactly one HwMode and a formality for every other.

The database is not one table but two, laid out back-to-back in `.lrodata`: the default `InstBits` (`0x3366d90`) and `InstBits_BarnaCorePxcHwMode` (`0x33931f0`). Both are `5667 × 32` bytes — one 4-word (`4 × uint64`) row per opcode, indexed by `opcode − 499`, interpreted as a [239-bit `APInt`](record-format.md). The emitter selects between them by a HwMode query on the `MCSubtargetInfo`. The counter-intuitive finding, byte-verified, is that the **default table is entirely zero on disk** and carries no relocations, while **only the BarnaCore variant is populated** (704 non-zero rows in the opcode range `2855..3991`). For every TensorCore and V5+ (Viperfish / Ghostlite / Trillium) instruction the base contributes nothing; their bytes come from the proto-bundle emitter path, not this table.

This page describes the database's axes — generation/HwMode × instruction-class × field — its in-binary representation as static `.lrodata` arrays plus the emitter-prologue accessor arithmetic, and how a slot's fields map to absolute bit positions in the 239-bit record. It shows the load-bearing rows (the BarnaCore vector-load, vector-store, loop, and predicate slot layouts) rather than dumping the 11,334 rows of the two tables.

For reimplementation, the contract is:

- The indexing arithmetic: `index = opcode − 499`, 5667 records, 32-byte stride, 239-bit `APInt` width.
- The two-table layout and the HwMode select that chooses default vs `BarnaCorePxcHwMode`.
- The row model: base bits set the discriminator, zero holes mark `(pos, width)` operand windows, and per-class `insertBits` fills them.
- The absolute slot-field bit positions for the populated (BarnaCore) classes, recovered from the case bodies' `insertBits(pos, width)` deposits.
- The all-zero / no-RELA property of the default table and what it implies for a reimplementer (the V5+ encoding lives elsewhere).

| | |
|---|---|
| **Default table** | `InstBits` @ `0x3366d90`, size `0x2c460` (181344 B = 5667 × 32) |
| **HwMode variant** | `InstBits_BarnaCorePxcHwMode` @ `0x33931f0`, size `0x2c460` (back-to-back) |
| **Row** | `4 × uint64` = 32 B, read as `APInt(BitWidth=0xEF=239, NumWords=4)` |
| **Index** | `opcode − 499`; first row = `0x1f3 = 499` (`ADDri`); 5667 rows |
| **Accessor** | `TPUMCCodeEmitter::getBinaryCodeForInstr` @ `0x13c74da0` (prologue arithmetic) |
| **HwMode select** | default `InstBits` (GOT `− 65189758`) vs BarnaCore (GOT `− 65167090`) |
| **Default on disk** | all-zero (`0 / 22668` non-zero words), no `.rela.dyn` relocation |
| **BarnaCore populated** | 704 non-zero rows, opcode range `2855..3991` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Database Axes

The InstBits database is best read as a three-axis cube. A reimplementer must reproduce all three axes; the per-row 32-byte payload is generated from the axes, not stored as free data.

| Axis | Values | Source | Confidence |
|---|---|---|---|
| HwMode / table | `default` (`InstBits`), `BarnaCorePxcHwMode` (`InstBits_BarnaCorePxcHwMode`) | two GOT-relative base computations in `getBinaryCodeForInstr` (`− 65189758` / `− 65167090`); HwMode query `*0x28(MCSubtargetInfo)` with feature index `3` | CONFIRMED |
| instruction class | 22 encoder case bodies (1 zero-base default + 21 BarnaCore classes) | jump table @ `0xaed7dac`, 5667 × int32 self-relative, dispatched `jmp *(base + table[index])` | CONFIRMED |
| field | per-class fixed `(pos, width)` windows (e.g. base-reg @ bit 35 w6, dst @ bit 88 w5, imm @ bit 207 w16) | the `insertBits(value, pos, width)` deposits inside each case body | CONFIRMED |

The cube is sparse. The HwMode axis has two values but only one (`BarnaCorePxcHwMode`) carries data. The instruction-class axis has 22 values but one of them — the zero-base default — absorbs `4956` of the `5667` opcodes. The field axis is dense only within the BarnaCore classes; the default class has no fields at all (it copies a zero row and returns). The whole encoding mass lives in the `2855..3991` opcode band, in one HwMode, across 21 case bodies.

> **QUIRK — the default table is not a placeholder waiting for the linker.** Reading all `181344` bytes of `InstBits` @ `0x3366d90` yields `0 / 22668` non-zero 8-byte words, and no `.rela.dyn` relocation targets the `[0x3366d90, 0x3366d90 + 0x2c460)` range (the full `~430k` `Elf64_Rela` entries at file offset `0x9170` were scanned, zero hits). The zero is the actual encoding, not an unrelocated stub. A reimplementation that expects load-time relocation to populate these base bits will encode every TensorCore and V5+ instruction as all-zero — and never see an error, because the proto-bundle path supplies the real bytes downstream. (For contrast, the same extraction against the AArch64 back end's `InstBits` in this binary finds it densely populated, confirming the extraction itself is correct.)

---

## In-Binary Representation

### The `.lrodata` TableGen region

The InstBits tables are part of one contiguous TableGen-emitted region in `.lrodata`. Because `.lrodata` has `VA == file offset` in this binary, every address below is directly seekable. The region is the LLVM `TPUGenMCCodeEmitter` / `TPUGenInstrInfo` / `TPUGenRegisterInfo` data, emitted adjacent by the TableGen backends:

```text
0x3360640  getMnemonic::OpInfo1            6166 × u32   InstPrinter mnemonic op-info
0x33666a0  getRegisterName::RegAsmOffset   ...          InstPrinter reg-name offsets
0x3366d90  InstBits                        5667 × 32B   <-- default base bits (all-zero)
0x33931f0  InstBits_BarnaCorePxcHwMode     5667 × 32B   <-- BarnaCore base bits (populated)
0x33bf650  TPUDescs                        6166 × 32B   per-opcode MCInstrDesc
0x33f2be0  TPUInstrNameData                274764 B     mnemonic string pool
0x3435d30  TPUInstrNameIndices             6166 × u32   opcode -> byte offset into NameData
0x343bd90  TPUStages                       0x7c8        pipeline stages
0x343cde0  TPURegStrings / 0x343e7b0 TPURegDesc          register names / descriptors
0x34469b0  TPURegEncodingTable             889 × u16    reg# -> HW encoding
0x344cb4c  LloOpcodeToProto                462 × u32    LloOpcode -> proto field id
```

The two InstBits tables are exactly `0x2c460` apart (back-to-back), which is the gap the emitter's two base computations encode (`65189758 − 65167090 = 22668` index units = `0x2c460` bytes). The descriptor, name, and register-encoding tables that the operand encoders read are documented on [Instr Name Data](instr-name-data.md); the per-opcode 239-bit record they pack into is [Record Format](record-format.md).

### The accessor arithmetic

There is no exported accessor function; the table is reached only inside the emitter prologue. The decompiled body fixes every constant on this page:

```c
// TPUMCCodeEmitter::getBinaryCodeForInstr @ 0x13c74da0  (prologue, decompiled)
uint32_t opc = *(uint32_t *)inst;          // MCInst opcode
if (opc <= 0x1F2u)                          // opcode <= 498: pseudo / target-independent
    reportUnsupportedInst(inst, inst);      //   trap (never reaches the table)

uint32_t index4 = (uint32_t)(4 * opc - 1996);  // 1996 = 0x7CC = 499*4 ; word-index = (opc-499)*4
if (index4 >= 0x588D)                        // 0x588D = 22669 -> 5667 rows
    __asm { ud1 };                           //   out of range: trap

// default path: InstBits row -> 239-bit APInt record
APInt(&record, /*BitWidth=*/239, &GLOBAL_OFFSET_TABLE_ + index4 - 65189758, /*NumWords=*/4);
// BarnaCore path (taken inside a populated case after the HwMode query):
APInt(&record, /*BitWidth=*/239, &GLOBAL_OFFSET_TABLE_ + index4 - 65167090, /*NumWords=*/4);
```

The arithmetic is exact. `index4 = 4 * opc − 1996` is the word index, i.e. `(opc − 499) × 4`; multiplied by 8 (word size) it is `(opc − 499) × 32` bytes, the 32-byte row stride. The bound `index4 < 0x588D` gives `0x588D / 4 = 0x1623 = 5667` rows, and `5667 × 32 = 0x2c460 = 181344` — the on-disk size of both symbols. The `APInt` width literal is `239` (`0xEF`), four words; the top 17 bits of the 256-bit storage are padding. The pseudo guard (`opc ≤ 498`) and the `ud1` out-of-range trap are the two bounds; see [MC-Emitter](mc-emitter.md) for the full pipeline.

> **NOTE —** the BarnaCore base read uses the *same* `index4`, only a different GOT displacement. The two tables are parallel arrays over the identical `opcode − 499` index space; an opcode's BarnaCore row and its (zero) default row sit at the same index in their respective tables. Selection is purely which displacement the case body uses, gated by the HwMode feature query.

---

## Default vs BarnaCore: The Two Tables

The two tables are structurally identical and semantically opposite. The default holds nothing; the variant holds everything the MC emitter encodes.

| Property | `InstBits` (default) | `InstBits_BarnaCorePxcHwMode` | Confidence |
|---|---|---|---|
| Address | `0x3366d90` | `0x33931f0` | CONFIRMED |
| Size | `0x2c460` (5667 × 32 B) | `0x2c460` (5667 × 32 B) | CONFIRMED |
| Non-zero rows | 0 | 704 | CONFIRMED |
| Non-zero 8-byte words | `0 / 22668` | `2144` | CONFIRMED |
| Populated opcode range | none | `2855..3991` | CONFIRMED |
| `.rela.dyn` relocations | none | none | CONFIRMED |
| Opcodes encoded through it | none (records returned all-zero) | Pufferfish BarnaCore lanes + native ops | CONFIRMED |
| Selected when | HwMode feature `BarnaCorePxcHwMode` inactive | feature active (query `*0x28(MCSubtargetInfo)`, idx 3) | CONFIRMED |

The populated rows of the BarnaCore table fall into a small set of instruction classes. The class taxonomy (recovered from the case-body grouping in the jump table) is the second axis of the database:

| Class | Rows | What it covers | Confidence |
|---|---:|---|---|
| `_V0` lane vector-ALU | 228 | BarnaCore vector lane 0 ops | CONFIRMED |
| `_V1` lane vector-ALU | 228 | BarnaCore vector lane 1 ops | CONFIRMED |
| `_V2` lane vector-ALU | 227 | BarnaCore vector lane 2 ops | CONFIRMED |
| `_VM` mask lane | 6 | BarnaCore mask-lane ops | CONFIRMED |
| `bc*` native | 11 | `bcHALT` / `bcLOOP_START` / `bcNOP` / `bcVLD*` / `bcVST*` / `bcVSHIFT` | CONFIRMED |
| other | 4 | misc | CONFIRMED |

The base bits of a populated row are predominantly 1-bits with structured zero **holes**. The set bits encode the fixed opcode discriminator plus default field values; the holes are exactly the `(pos, width)` windows the class's operand encoders write into. Every opcode in a given class shares one hole layout — the per-opcode difference is only the discriminator value in the base. A reimplementer recovers a class's field map either by reading the base zero-runs or by reading the case body's `insertBits` arguments; the two agree bit-for-bit. The 21 BarnaCore classes correspond one-to-one with the 21 non-default case bodies in the dispatch ([MC-Emitter §Per-Opcode Dispatch](mc-emitter.md)).

---

## Field Mapping: Slot to Absolute Bit Positions

A field's absolute position in the 239-bit record is fixed per instruction class, not per opcode. The positions below were recovered from the `insertBits(value, pos, width)` deposits in the populated case bodies (verified against the decompiled `getBinaryCodeForInstr` body: the deposit-position histogram contains exactly these constants — `0x23/6`, `0x58/5`, `0x8D/2`, `0xCF/0x10`, `0xDF/0x10`, the predicate `0/4` + `5/2`, etc.). These are the absolute positions the per-slot LLO reports defer to "the InstBits table" for — but only for the BarnaCore path. The TensorCore / V5+ positions are *not* here (see the all-zero finding).

### The predicate field (every populated slot)

The most reused field is the per-slot predicate, written by `encodePredicateOperand` (`0x13c77c40`). Its three deposits give a 7-bit field:

```text
 bits [0:3]  predicate register index   insertBits(reg_enc, pos=0, width=4)   reg# via TPURegEncodingTable
 bit  [4]    negate / inversion          word0 |= 0x10  (if operand flag bit 0 set)
 bits [5:6]  predication mode            insertBits((flags>>5)&3, pos=5, width=2)
```

The register index is 4 bits because the predicate block of `TPURegEncodingTable` holds values `1..15` (`P0..P14`). The same field repeats per sub-slot in multi-slot classes (e.g. the load slot replicates it at the start of each VLD sub-slot window). The decompiled `encodePredicateOperand` shows the deposit verbatim: `insertBits(dst, *(u16*)(table + 2*reg_index), 0, 4)`, then `dst |= 0x10` on the negate flag, then `insertBits(dst, (flags>>5)&3, 5, 2)`.

### `bcVLDi` / `bcVLDr` — BarnaCore vector-load slot

The load class case body (`0x13c767c0`) runs ~26 `insertBits` deposits over two sub-slots. The first sub-slot:

| Field | Position | Width | Encoder | Confidence |
|---|---|---:|---|---|
| base-address register | bit 35 (`0x23`) | 6 | `getMachineOpValue` | CONFIRMED |
| predicate (mode + reg) | bits 126/128 | 2 + 5 | `encodePredicateOperand` | CONFIRMED |
| addressing-mode sub-opcode | bit 133 (`0x85`) | 3 | `getMachineOpValue` | CONFIRMED |
| destination Vreg | bit 136 (`0x88`) | 5 | `getMachineOpValue` | CONFIRMED |
| load qualifier | bit 141 (`0x8D`) | 2 | constant | CONFIRMED |
| immediate displacement | bit 207 (`0xCF`) | 16 | `getMachineOpValue` | CONFIRMED |

The second VLD sub-slot repeats the same field shapes shifted up (`+21`), with its immediate displacement landing at bit 223 (`0xDF`, width 16) — the highest field in the database and the reason the record is sized at 239 bits. So a BarnaCore vector load is `{predicate(7b), base-reg(6b), addr-mode(3b), dst-Vreg(5b), qualifier(2b), imm16}`, twice.

### `bcVST*` — BarnaCore vector-store slot

The store classes (`bcVST_aliaddri/rr` @ `0x13c765a0`, `bcVSTi/r` @ `0x13c76628`) pack the source register and addressing into one wide window:

| Field | Position | Width | Confidence |
|---|---|---:|---|
| base-address register | bit 35 (`0x23`) | 6 | CONFIRMED |
| source-Vreg + addressing pack | bit 126 (`0x7E`) | 21 | CONFIRMED |
| immediate displacement | bit 207 (`0xCF`) | 16 | CONFIRMED |

### `bcLOOP_START` — BarnaCore loop slot

The loop class (`0x13c770f8`) is base-only: its row holds the discriminator and the trip/length field as a hole. The recovered hole map (opcode 3978):

| Field | Position | Width | Meaning | Confidence |
|---|---|---:|---|---|
| loop mode / type | bit 0 | 2 | loop kind | CONFIRMED |
| opcode discriminator | bit 2 | 1 | fixed (=1) | CONFIRMED |
| loop length / body offset | bit 3 | 9 | hardware-loop trip / length | CONFIRMED |
| bundle-common byte | bit 24 (`0x18`) | 8 | shared by all bc ops | CONFIRMED |
| bundle-common field | bit 58 (`0x3A`) | 2 | shared by all bc ops | CONFIRMED |

The 9-bit length at bit 3 is the BarnaCore hardware-loop trip count. The bundle-common fields at bits 24 and 58 are shared by every populated BarnaCore opcode (the `00` byte at bits `24:31` is a zero-hole in the otherwise 1-bit-dense base, e.g. low pattern `f3ffffff00fff004`).

> **GOTCHA — the sequencer branch/call/halt opcodes index this database but encode to zero.** `BRabs` (505), `BRind` (507), `BRrel` (508), `BRrelrot` (509), `CALLabs` (514), `CALLrel` (515), and `HALT` (571) are real MC opcodes with rows in `InstBits` — but those rows are all-zero (default table) and route to the zero-base default case. Their offsets, destination registers, and predication are written by the proto-bundle `EmitBranchOp` / `EmitCallOp` / `EmitImmediate` / `EmitPredicationToSlot` path, never through InstBits. A reimplementer who reads the InstBits row for `BRrel` to find the branch-offset field will find only zeros; the field is in the bundle emitter. The MC `BR`/`BRcond`/`BRcondrot`/`BRret` pseudos (opcodes 325/328/330/331) sit below 499 and are expanded before MC emission — they never reach the database at all.

---

## What This Database Does Not Hold

The InstBits database is the LLVM-MC slice of the TPU encoding stack, and it is honest about its bounds. It does **not** hold:

- **The TensorCore / V5+ absolute bit positions.** Proven by the all-zero, no-RELA default table. Those positions are produced by the proto-bundle `isa_emitter` `EmitX` templates and the per-gen `TensorCoreCodecBase` `BitCopy(dest, bit_offset, src, 0, width)` calls. The database proves *where they are not*, which closes the question the per-slot reports left open.
- **The full BarnaCore vector-ALU `_V0/_V1/_V2` field maps.** The VLD / VST / LOOP / predicate classes are decoded bit-exactly above; the two large vector-ALU classes (`0x13c74eb9`, `0x13c74f47`) and the 17 smaller vector classes (`0x13c75723 … 0x13c75d77`) were sampled, not exhausted (their X/Y operand, immediate, and sublane-mask windows). *(MEDIUM confidence on the unexhausted vector classes.)*
- **The register-class partition behind `TPURegEncodingTable`.** The predicate block (`1..15`) and the descending scalar/vector blocks are visible; a full reg# → (class, encoding) binding needs the `TPURegClassInfos` (`0x334ea60`) + `TPURegDesc` (`0x343e7b0`) cross-decode. See [Instr Name Data §Register Encoding](instr-name-data.md#register-encoding-tpuregencodingtable).

---

## Cross-References

- [Instr Name Data](instr-name-data.md) — `TPUInstrNameData` / `TPUDescs` / `TPURegEncodingTable`, the descriptor, mnemonic, and register-encoding tables the InstBits operand encoders read.
- [Record Format](record-format.md) — the 239-bit `APInt` the InstBits row is loaded into, and the base-bits / `insertBits`-holes model.
- [MXU Slot](slot-mxu.md) — a TensorCore slot whose fields are *not* in InstBits (zero base) and are emitted by the proto-bundle path instead.
- [kIsaTable Data](kisatable-data-sections.md) — the per-generation ISA-encoding split; InstBits is the LLVM-MC member of that split, complementing the per-gen codec metadata and NOP templates.
