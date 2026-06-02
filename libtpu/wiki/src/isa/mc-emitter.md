# MC-Emitter (`getBinaryCodeForInstr`)

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

`TPUMCCodeEmitter::getBinaryCodeForInstr` (`0x13c74da0`, `0x29c6` bytes) is the LLVM-MC function that lowers one LLO machine instruction (`MCInst`) into its packed bit pattern. It is the standard TableGen-generated wide-instruction emitter: read the opcode, bounds-check it, copy the per-opcode base bits out of the `InstBits` table into a [239-bit `APInt` record](record-format.md), then run a per-opcode `switch` whose body is a sequence of operand encoders that overwrite the record's holes with `APInt::insertBits(value, pos, width)`. The structure matches the generic LLVM `MCCodeEmitter` template, with the TPU specifics being a wide (239-bit, 4-word) record, two-table HwMode selection between the default and BarnaCore base-bits arrays, and a per-operand staging `APInt` that the emitter reuses across fields.

For a reimplementer the C++ skeleton is not the contract. The contract is the combination of:

- The opcode bounds and indexing arithmetic (`opcode − 499`, 5667 records, 32-byte stride).
- The HwMode select between `InstBits` (default, all-zero) and `InstBits_BarnaCorePxcHwMode` (populated).
- The 22-body `switch` and which opcode classes route to the zero-base default versus a real `insertBits` sequence.
- The per-operand encoders — `encodePredicateOperand`, `getMachineOpValue`, and the `getSpecialOpEncoding` descriptor consult — and the `(pos, width)` of each deposit.
- The `reportUnsupportedInst` trap on both the pseudo guard and the out-of-range / default arms.

| | |
|---|---|
| **Function** | `llvm::(anon)::TPUMCCodeEmitter::getBinaryCodeForInstr` @ `0x13c74da0` (`0x29c6` B) |
| **Signature** | `(MCInst&, SmallVectorImpl<MCFixup>&, APInt& record, APInt& scratch, MCSubtargetInfo&) const` |
| **Record** | `APInt`, 239 bits, 4 words — see [Record Format](record-format.md) |
| **Base tables** | `InstBits` @ `0x3366d90` / `InstBits_BarnaCorePxcHwMode` @ `0x33931f0` (HwMode-selected) |
| **Dispatch** | jump table @ `0xaed7dac` (5667 × int32 self-relative); 22 distinct case bodies |
| **Default arm** | `0x13c74e9d` — zero base, no `insertBits`, return (4956 opcodes) |
| **Operand encoders** | `encodePredicateOperand` @ `0x13c77c40`, `getMachineOpValue` @ `0x13c777e0` |
| **Trap** | `MCCodeEmitter::reportUnsupportedInst` @ `0x13c77750` (pseudo guard + default) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Emit Pipeline

The emit path has a fixed five-step shape, all visible in the decompiled body. The function takes the `MCInst` (`a2`), a fixup list (the `SmallVectorImpl<MCFixup>&`), two `APInt&` outputs (`a3` = the record, here aliased `v260`; `a4` = the per-operand scratch), and the `MCSubtargetInfo` (`a5`).

```c
// TPUMCCodeEmitter::getBinaryCodeForInstr @ 0x13c74da0  (annotated pseudocode)
uint64_t emit(MCInst *inst, MCFixup_vec *fixups,
              APInt *record /*a3/v260*/, APInt *scratch /*a4*/,
              MCSubtargetInfo *sti) {

    uint32_t opc = inst->opcode;                       // step 1: read opcode
    if (opc <= 0x1F2)                                  //   opcode <= 498
        reportUnsupportedInst();                       //   -> pseudo/target-indep: trap

    // step 2: ensure the scratch APInt is 239 bits wide (zext if not)
    if (scratch->BitWidth != 239)
        scratch->zext(239);

    uint32_t index = 4 * opc - 1996;                   // step 3: (opcode-499)*4 words
    if (index >= 0x588D)                               //   5667-record bound
        trap();                                        //   ud1

    // step 4: copy the per-opcode base row into the record (HwMode-selected table)
    *record = APInt(239, &InstBits[index*8], /*NumWords=*/4);

    // step 5: per-opcode operand fill
    switch (opc) {
        // ... 21 real BarnaCore encoding cases (insertBits sequences) ...
        default:                                       // 4956 opcodes: TC + V5+
            return record_normalize(record);           //   zero base, no insertBits
    }
}
```

Steps 1 and 3 are the two bounds checks. Opcodes `≤ 498` are MC pseudo / target-independent forms (`PHI`, `INLINEASM`, the pre-expanded `BR`/`BRcond`/`BRret` pseudo branches) that never reach a real encoding and trap on the pseudo guard. Opcodes `≥ 499 + 5667 = 6166` are out of table range and trap on the `ud1`. Everything in between indexes the base-bits table at `(opcode − 499) × 32` bytes.

Step 2 is a width guard on the scratch `APInt`: if the caller handed in a scratch that is not already 239 bits, the emitter `zext`s it (freeing any heap storage from a wider prior use). After this point both `APInt`s are 239 bits wide.

Step 4 builds the record from the base row. Step 5 is the per-opcode `switch`, expanded by the decompiler into 5667 individual `case` labels that share 22 bodies. The overwhelming majority (the TensorCore and V5+ opcodes) fall to the `default` body, which does nothing — the record is already correct because its base is zero and those instructions are encoded by the proto-bundle path, not here.

---

## HwMode Table Select

The emitter chooses between two base-bits tables. The default `InstBits` (`0x3366d90`) is read in the prologue; the BarnaCore variant `InstBits_BarnaCorePxcHwMode` (`0x33931f0`, immediately following it) is read inside the populated BarnaCore case bodies. The decompiler shows the select as two distinct GOT-relative base computations feeding the same `APInt(239, base, 4)` construction:

```c
// prologue (default path): InstBits  (GOT - 65189758 -> 0x3366d90)
APInt(&record, 239, &GLOBAL_OFFSET_TABLE_ + index4 - 65189758, 4);

// inside a BarnaCore case body: InstBits_BarnaCorePxcHwMode  (GOT - 65167090 -> 0x33931f0)
APInt(&record, 239, &GLOBAL_OFFSET_TABLE_ + index4 - 65167090, 4);
```

The two offsets differ by `65189758 − 65167090 = 22668` table-index units, which is the `0x2c460`-byte gap between the two back-to-back arrays. The case body that re-reads the base from the BarnaCore table does so after querying the subtarget's HwMode (the `BarnaCorePxcHwMode` feature, a virtual feature query on the `MCSubtargetInfo`): when that mode is active, the BarnaCore base supplies real opcode-discriminator bits; otherwise the default zero base stands. Because the default table is all-zero, the practical effect is binary — either the instruction is a BarnaCore-Pxc op (real base bits, real `insertBits` sequence) or it is a TensorCore / V5+ op (zero base, no `insertBits`).

| HwMode | Base table | On disk | Opcodes encoded here | Confidence |
|---|---|---|---|---|
| default | `InstBits` @ `0x3366d90` | all-zero, no RELA | none (records returned all-zero) | CONFIRMED |
| `BarnaCorePxcHwMode` | `InstBits_BarnaCorePxcHwMode` @ `0x33931f0` | 704 populated rows (range `2855..3991`) | Pufferfish BarnaCore lanes + native ops | CONFIRMED |

> **NOTE —** the all-zero default table is not a load-time placeholder. No `.rela.dyn` relocation targets either `InstBits` range, so the zero is the actual encoding. A reimplementation that expects the linker to fill these base bits at load time will encode every TensorCore and V5+ instruction as all-zero and never notice, because the proto-bundle path supplies the real bytes downstream.

---

## Per-Opcode Dispatch

The `switch` is implemented as a jump table at `0xaed7dac`: 5667 signed `int32` self-relative offsets indexed by `(opcode − 499)`, dispatched via `jmp *(table_base + table[index])`. The decompiler renders it as 5667 `case` labels collapsing onto **22 distinct case bodies**. The histogram below is the structural contract — which opcode classes carry a real `insertBits` sequence and which return the zero base unchanged.

| Case body | #opcodes | Role | Confidence |
|---|---:|---|---|
| `0x13c74e9d` | 4956 | DEFAULT — zero base, no `insertBits`, return (all TensorCore + V5+) | CONFIRMED |
| `0x13c74eb9` | 384 | BarnaCore `_V1`/`_V2` vector-ALU lanes | CONFIRMED |
| `0x13c74f47` | 195 | BarnaCore `_V0` vector-ALU lane (predicate + operand `insertBits`) | CONFIRMED |
| `0x13c754d1` | 34 | BarnaCore compose/sub `_V1`/`_V2` | CONFIRMED |
| `0x13c75559` | 18 | BarnaCore `VIMM*`/`VMOV*` `_V1`/`_V2`/`_VM` | CONFIRMED |
| `0x13c75723` … `0x13c75d77` | 8+12+10+9+6+6+5 | seven smaller BarnaCore vector classes | CONFIRMED |
| `0x13c765a0` / `0x13c76628` / `0x13c766b0` / `0x13c76738` | 2 each | BarnaCore store-slot classes (`bcVST*`) | CONFIRMED |
| `0x13c767c0` | 2 | `bcVLDi`/`bcVLDr` load slot (26 `insertBits`) | CONFIRMED |
| `0x13c770f8` | 1 | `bcLOOP_START` loop slot | CONFIRMED |
| `0x13c77180` / `0x13c77208` / `0x13c77290` / `0x13c77318` | 1 each | BarnaCore singletons (ops 3789/3787/3786/3588) | CONFIRMED |
| `default` (`reportUnsupportedInst`) | — | out-of-range opcode fell through the jump table | CONFIRMED |

The asymmetry is the point. 4956 of the 5667 opcodes — every concrete branch/call/halt, every load/store, and every compute op on the TensorCore and V5+ side — route to the zero-base default. Their bytes are *not* emitted here; they are produced by the proto-bundle `EmitX` populators and the per-slot `<Slot>Encoder::Encode` `BitCopy` calls. Only 711 opcodes (all in the BarnaCore range `2855..3991`) carry a real `insertBits` sequence in this function. A reimplementation that treats the default arm as an error path would mis-diagnose 4956 legal opcodes; a reimplementation that tries to encode V5+ instructions through this function would emit all-zero bundles.

> **QUIRK — the sequencer branch/call opcodes reach the switch but encode to zero.** `BRabs` (505), `BRind` (507), `BRrel` (508), `BRrelrot` (509), `CALLabs` (514), `CALLrel` (515), and `HALT` (571) are real MC opcodes that index the jump table — but they route to the zero-base default. Their 20-bit offsets, destination/x registers, and predication are written by the proto-bundle `EmitBranchOp` / `EmitCallOp` / `EmitImmediate` / `EmitPredicationToSlot` path, not by `getBinaryCodeForInstr`. The MC emitter contributes literally nothing to a V5+ branch's bits. See [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md).

---

## The Operand Encoders

Inside a real case body the operand fill is a sequence of `insertBits` deposits, fed by two helpers and the per-operand stage-then-deposit idiom.

`encodePredicateOperand` (`0x13c77c40`) writes the slot-relative 7-bit predicate field. It is called with a per-slot index argument (the third parameter, the values `1`/`2`/`3`/`4` seen across the 17 call sites select which slot's predicate is being encoded) and deposits a 4-bit register index at bit 0, a negate bit at bit 4, and a 2-bit mode at bits 5:6:

```c
// encodePredicateOperand @ 0x13c77c40 (decompiled, exact)
__int64 encodePredicateOperand(RegEncTable *tbl, MCInst_ops *ops, uint32_t slot, APInt *dst) {
    uint64_t flags = ops[slot].flags;                       // operand flags word
    insertBits(dst, tbl[ ops[slot].preg ], /*pos=*/0, /*width=*/4);  // reg index (P0..P14)
    if (flags & 1)
        dst->word0 |= 0x10;                                 // bit 4: negate / inversion
    return insertBits(dst, (flags >> 5) & 3, /*pos=*/5, /*width=*/2);// bits 5:6: mode
}
```

`getMachineOpValue` (`0x13c777e0`) is the generic operand lowering. It locates the operand's index within the `MCInst`, calls `getSpecialOpEncoding(MCInstrDesc&, opno)` (`0x13c63a80`) to read the operand's encoding class out of the per-opcode descriptor (`TPUDescs`), and produces a register encoding (`uint16` lookup in `TPURegEncodingTable` @ `0x34469b0`), an immediate, an expression value, or an `MCFixup`. The result is staged in the scratch `APInt`, and the case body reads back the relevant low bits and deposits them at the class-fixed position.

The general per-operand pattern in a case body is therefore: zero the scratch, lower the operand into it, extract the needed bits, deposit them into the record. The store-slot tail shows three such deposits from a single lowered operand (a 16-bit displacement at bit 207, a 2-bit qualifier at bit 141, a 6-bit field at bit 35):

```c
// excerpt: bcVST store-slot operand fill (getBinaryCodeForInstr, decompiled)
*scratch = 0;
getMachineOpValue(self, inst, &inst->op[2], scratch);
insertBits(record, extractBitsAsZExtValue(scratch, /*w=*/0x10, /*lo=*/8),  /*pos=*/0xCF, 0x10); // imm16 @ bit 207
insertBits(record, extractBitsAsZExtValue(scratch, /*w=*/2,    /*lo=*/0),  /*pos=*/0x8D, 2);    // qualifier @ bit 141
insertBits(record, extractBitsAsZExtValue(scratch, /*w=*/6,    /*lo=*/2),  /*pos=*/0x23, 6);    // base-reg @ bit 35
```

The `(pos, width)` constants are fixed per encoding class — bit 35 (base register), bit 88 (load destination), bit 207 (immediate displacement), and so on for the BarnaCore slots. The values are descriptor-driven through `getSpecialOpEncoding`. This split is the heart of the encoder: positions come from the class, values come from the descriptor and the register-encoding table. See [InstBits DB](instbits-master-db.md) for the descriptor, name, and register-encoding tables these helpers read.

---

## Return and Trap Paths

On a normal exit the emitter normalizes the record `APInt` (an `assignSlowCase` when the value is wider than one inline word) and returns it. Two paths trap instead of returning:

- The **pseudo guard** (`opcode ≤ 498`) calls `reportUnsupportedInst` before any table access — these opcodes are expanded before MC emission and should never reach the emitter.
- The **default `switch` arm** also calls `reportUnsupportedInst`. This is reached only when the jump-table index lands outside the dense BarnaCore case set in a way the table cannot dispatch; the common V5+ default is the separate zero-base body at `0x13c74e9d`, not this trap.

Both traps lower to `reportUnsupportedInst` (`0x13c77750` / `0x13c7775d`) followed by `ud1`/`int3`. A reimplementation must distinguish the two "default"-looking paths: the `0x13c74e9d` body is the legitimate zero-base return for 4956 opcodes; the `switch` `default` is the genuine error trap. Conflating them turns every TensorCore and V5+ instruction into an unsupported-instruction crash.

---

## Cross-References

- [Record Format](record-format.md) — the 239-bit `APInt` this emitter fills, and the base-bits / `insertBits`-holes model.
- [InstBits DB](instbits-master-db.md) — the `InstBits` / `InstBits_BarnaCorePxcHwMode` base-bits tables, `TPUDescs`, `TPUInstrNameData`, and `TPURegEncodingTable` the operand encoders read.
- [IsaEmitter Registry](isa-emitter-registry.md) — the proto-bundle `EmitX` / `<Slot>Encoder::Encode` path that encodes every opcode this emitter returns all-zero for.
- [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md) — the absolute bundle bit positions for the TensorCore and V5+ slots `getBinaryCodeForInstr` does not encode.
