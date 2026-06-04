# SCS Scalar Opcode Enumeration

> *Every opcode value, mask immediate, and bit position on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

The SCS (SparseCore Scalar) sequencer has three scalar slots per 32-byte bundle — `ScsScalarMisc`, `ScalarAlu1`, `ScalarAlu0` — each a 27-bit field carrying a 6-bit primary opcode at slot-relative bit `+16`, landing at absolute bundle bits 127, 154, and 181. This page is the opcode *roster* companion to the [SCS Engine](scs-engine.md) page, which owns the bundle byte layout and the 27-bit slot template; here we enumerate every operation each slot can encode, with its integer opcode value and the encoding form that carries it. The closest familiar analog is an ISA opcode table recovered not from a manual but from the decoder's match predicates: there is no opcode-name string table to read, so each value is reconstructed from the per-op compare immediate.

The roster is recoverable because libtpu emits **one C++ type per opcode per generation** — `asic_sw::deepsea::<pxc>::<gen>::isa::SparseCore<Slot><OpName>Opcode` — and each type carries a `Matches() const` predicate that masks the decoded opcode field out of the instruction struct and compares it against that op's own signature. The `cmp`/`movabs` immediate inside `Matches()` *is* the opcode value; the slot `Encoder::Encode` writes the same value back into the bundle via `BitCopy(dst, dst_bitoff, src, src_bitoff, nbits)` at the corresponding absolute bit. Because the decode predicate and the encode write agree, the `Matches()` immediate is the authoritative encoding. Every value below was cross-checked against a `Matches()` predicate in the decompiled gfc (6acc60406) namespace, with vfc (Viperfish) sampled for gen-invariance.

The opcode space is **two-level**. A 6-bit primary field selects either a concrete op (`IntegerAdd=0x0a`, `BitwiseAnd=0x0e`) or an op-*class*. When it names a class, the concrete op lives in a wider escape field that overlays the slot: control ops in an 11-bit field, register reads in a 17-bit field (`ReadRegister* = 0x280..0x28d`), config sets in a 16-bit field (`Set* = 0x4001..0x4005`), divide-push in a `0x16xxxx` field. The two ALU lanes share one opcode namespace and differ only in bundle bit position and a handful of lane-exclusive ops; `ScsScalarMisc` carries the sync-flag / atomic / barrier family (encoded as a 6-bit base + a 5-bit sub-opcode mode) plus an integer-ALU subset, with no FP and no branch. This page documents the three predicate shapes, the per-slot rosters with their integer values, the four class escapes, and the per-generation deltas.

For reimplementation, the contract is:

- **The three `Matches()` predicate shapes.** Form A (flat 6-bit, opcode straddles a word boundary), Form B (composite: 6-bit base + 5-bit sub-opcode mode at struct-relative bit 47 or extended-ALU class at bit 58), Form C (mask-compare against the slot word, used by the ALU lanes). Reproducing the decoder means reproducing these masks.
- **The flat 6-bit roster, shared across both ALU lanes.** `0x0a..0x31` integer/FP/bitwise/shift/compare ops, identical values on `ScalarAlu0` and `ScalarAlu1`; only the bundle bit (`@181` vs `@154`) and a few lane-exclusive ops differ.
- **The four class escapes and their value ranges.** Control `0x00..0x1d` (11-bit), register-read `0x280..0x28d` (17-bit), config-set `0x4001..0x4005` (16-bit), divide-push `0x160001..0x160002`. These values are slot-independent; only the field's bit base changes per lane.
- **The `ScsScalarMisc` composite encoding.** A 6-bit base names a class (Sync `0x01`, SyncWatch `0x02`, set-sync `0x05`, barrier `0x07`, Atomic `0x08`, extended-ALU `0x00`); the 5-bit sub-opcode at struct bit 47 (sync/atomic mode) or bit 58 (extended-ALU) picks the member.

| | |
|---|---|
| **Slots** | `ScsScalarMisc` (op `@127`) · `ScalarAlu1` (op `@154`) · `ScalarAlu0` (op `@181`) |
| **Primary opcode width** | 6 bits, slot-relative `@+16` |
| **Opcode→mnemonic source** | per-op `SparseCore<Slot><OpName>Opcode::Matches()` compare immediate |
| **Predicate shapes** | A flat-6 · B composite (base + 5-bit sub) · C mask-compare |
| **Class escapes** | control 11-bit (`0x00..0x1d`) · reg-read 17-bit (`0x280..0x28d`) · config 16-bit (`0x4001..0x4005`) · divide-push (`0x16xxxx`) |
| **gfc op-form counts** | `SparseCoreScalarMisc*` 82 · `ScsScalarMisc*` 81 · `ScalarAlu0*` 78 · `ScalarAlu1*` 82 |
| **Shared ALU namespace** | `ScalarAlu0` ≡ `ScalarAlu1` values; lane differs only by bit position + lane-exclusive ops |
| **Gen-invariance** | shared op values byte-identical VF/GL/GF (`IntegerAdd=0x0a` on both) |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

> **NOTE — this page enumerates the opcode roster; the bundle byte layout lives in [SCS Engine](scs-engine.md).** The 32-byte bundle, the absolute slot bases (111/138/165), the 27-bit slot template (x0 `@+0`, ScalarY `@+5`, x1 `@+11`, opcode `@+16`, predication header `@+22`), and the no-check-trailer rule are documented there and are not repeated here. The [M-Register Predicate Word](m-register-predicate.md) page owns the 3-bit/4-bit predication-header overlap that sits above each opcode field.

---

## The Opcode-Predicate Model

### Why there is no opcode string table

The SC ISA carries no opcode-name array to stringify. Instead, each opcode is a distinct empty C++ class `SparseCore<Slot><OpName>Opcode` in the `asic_sw::deepsea::<pxc>::<gen>::isa::` namespace, and the disassembler/validator asks each one "is this you?" through `Matches()`. The match predicate decodes the opcode field(s) from the instruction struct (a 128-bit-plus word array; the scalar slots read words at `this+0x10` and `this+0x18`) and compares against the op's hard-coded signature. The signature *is* the opcode. This makes the `Matches()` compare immediate the authoritative opcode→mnemonic source, the SC analog of the TensorCore LLO opcode stringify path.

Three predicate shapes appear across the scalar slots. The shapes matter to a reimplementer because they reveal where the opcode bits physically sit and how a class splits into a base plus a sub-opcode.

### Form A — flat 6-bit

The primary opcode straddles a 64-bit word boundary: bit 63 of word `this+0x10` is the low bit, bits 0..4 of word `this+0x18` are the high bits, masked to 6 bits and compared. `ScsScalarMisc IntegerAdd` (`0x1ebabf00`):

```c
// SparseCoreScalarMiscIntegerAddOpcode::Matches  (gfc 0x1ebabf00)
//   shld $1, word_0x10, word_0x18  →  (word_0x18<<1) | (word_0x10>>63)
return ((((word_0x18 << 1) | (word_0x10 >> 63)) & 0x3F) == 0x0A);   // opcode 0x0a, 6-bit straddle
```

(The decompiler renders the straddle as `(*((__int128*)this + 1) >> 63) & 0x3F`; the `__int128` view at `+1` is the word pair at `+0x10/+0x18`.)

### Form B — composite (base + sub-opcode)

A 6-bit base names a class, and a 5-bit sub-opcode picks the member. The base is reassembled as `(bit63 | low5<<1)`; the predicate XORs it against the class value, then ANDs out the sub-field and XORs against the member signature, ORs the two halves, and tests for zero. `ScsScalarMisc AtomicTileAdd` (`0x1ebabbe0`) — base 8 (Atomic), sub 1:

```c
// SparseCoreScalarMiscAtomicTileAddOpcode::Matches  (gfc 0x1ebabbe0)
//   base    = (word_0x10>>63) + 2*(word_0x18 & 0x1F)
//   sub mode field = word_0x10 & 0xF800000000000  (struct-rel bit 47, 5 bits)
return ( (((word_0x10>>63) + 2*(word_0x18 & 0x1F)) ^ 0x08)
       | ((word_0x10 & 0xF800000000000) ^ 0x800000000000) ) == 0;   // base 8, sub 1
```

The sync/atomic mode field is at struct-relative bit 47 (`0xF800000000000`). The extended-ALU class instead uses a 5-bit field at bit 58 (`0x7C00000000000000`); `CountLeadingZeros` (`base 0x00`, sub 14) confirms it: `(word_0x10 & 0x7C00000000000000) ^ 0x3800000000000000`, and `0x3800000000000000 >> 58 = 14`.

### Form C — mask-compare (the ALU lanes)

`ScalarAlu0`/`ScalarAlu1` use a single AND-mask + compare against the slot word. `ScalarAlu0 IntegerAdd` (`0x1eb67660`):

```c
// SparseCoreScalarAlu0IntegerAddOpcode::Matches  (gfc 0x1eb67660)
return (word_0x18 & 0x7E0000000000000) == 0x140000000000000;   // 0x140.. >> 53 = 0x0a
```

The opcode is `VAL >> tzcnt(MASK)`: `0x140000000000000 >> 53 = 0x0a`. `ScalarAlu1` reads the same opcode value from a *different* word — its predicates mask `*((_DWORD*)this + 6) & 0xFC000000` (a 32-bit word at `+0x18`, bits 26..31), so `AddCbreg` (`0x1eb7b5a0`) tests `== 0xCC000000` and `0xCC000000 >> 26 = 0x33`. Same opcode value, lane-specific bit position — which is exactly the `@181` (Alu0) vs `@154` (Alu1) bundle-bit difference.

> **QUIRK — the encode-side switch numbers ops differently from the `Matches()` immediates; trust `Matches()`.** Inside `ScalarAlu0Encoder::Encode` (`0x1eb693c0`) the dispatch `switch(*(a2+88))` uses *sequential ODS enum* case labels (case `0xA` → `AtomicTile…`, case `0x13` → `IntegerAdd`), then writes the *hardware* opcode via `BitCopy(dst, 181, …, 6)`. The case label is the high-level enum ordinal; the `BitCopy` value (and the `Matches()` immediate that reads it back) is the silicon opcode. A reimplementer who reads the switch case numbers as opcodes will mis-encode every op. The values on this page are the `Matches()`/`BitCopy` hardware values.

---

## Shared ALU Roster (ScalarAlu0 / ScalarAlu1)

### The flat 6-bit primary set

The two ALU lanes share one opcode namespace: `IntegerAdd=0x0a`, `BitwiseAnd=0x0e`, `CompareIntegerEq=0x1e`, the FP-compare block `0x2a..0x2f` decode to identical values on both lanes. They differ only in (a) the bundle bit the opcode lands at — `@181` for `ScalarAlu0` (decoded from word `this+0x18` bit 53), `@154` for `ScalarAlu1` (decoded from word `this+0x18` bits 26..31) — and (b) a small set of lane-exclusive ops listed below. Values are gen-invariant for shared ops (vfc `IntegerAdd` is `==0x0a`, byte-identical to gfc).

| Opcode | Mnemonic | Class | Lane |
|---:|---|---|---|
| `0x0a` | `IntegerAdd` | integer ALU | Alu0 + Alu1 |
| `0x0b` | `IntegerAddWithOverflowCheck` | integer ALU | both |
| `0x0c` / `0x0d` | `IntegerSubtractYX` / `…WithOverflowCheck` | integer ALU | both |
| `0x0e` / `0x0f` / `0x10` | `BitwiseAnd` / `BitwiseOr` / `BitwiseXor` | bitwise | both |
| `0x11` / `0x12` | `FloatingPointAdd` / `FloatingPointSubtractYX` | FP ALU | Alu1 |
| `0x13` | `FloatingPointMultiply` | FP ALU | Alu0 |
| `0x14` / `0x15` | `Multiply32BitIntegers` / `Multiply32BitUnsignedIntsReturningHighHalf` | integer mul | Alu0 |
| `0x16` | `DivideWithRemainderXY` | integer div | Alu0 |
| `0x17` / `0x18` / `0x19` | `LogicalShiftLeft` / `LogicalShiftRight` / `ArithmeticShiftRight` `XByYPlaces` | shift | both |
| `0x1a` / `0x1b` | `MaxOfTwoFloatingPointValues` / `MinOfTwoFloatingPointValues` | FP minmax | both |
| `0x1c` / `0x1d` | `MaxOfTwoUnsignedIntValues` / `MinOfTwoUnsignedIntValues` | int minmax | both |
| `0x1e`–`0x23` | `CompareIntegerEq/Ne`, `CompareSignedIntegerGt/Gte/Lt/Lte` | int compare | both |
| `0x24`–`0x27` | `CompareUnsignedIntegerGt/Gte/Lt/Lte` | uint compare | both |
| `0x28` | `CarryOutFromIntegerUnsigned` | carry | both |
| `0x29` | `PredicateOr` | predicate | both |
| `0x2a`–`0x2f` | `CompareFloatingPoint{Eq,Neq,Gt,Gte,Lt,Lte}` | FP compare | both |
| `0x30` | `IsInfOrNan` | FP classify | both |
| `0x31` | `ArithmeticShiftLeftXByYPlacesCheckOverflow` | shift | both |
| `0x3e` | `LogicalShiftLeftOnesXByYPlaces` | shift (GF-only) | Alu0 |

The compare blocks are dense: `0x1e..0x27` is the ten integer compares (Eq/Ne then signed Gt/Gte/Lt/Lte then unsigned Gt/Gte/Lt/Lte) and `0x2a..0x2f` is the six FP compares. A reimplementer can decode the whole compare region by linear opcode index.

### Lane-exclusive primary ALU ops

The lanes are not interchangeable. `ScalarAlu1` carries the SMEM load/store, circular-buffer ([CBREG](cbreg.md)), and task-request ops; `ScalarAlu0` carries the FP-multiply / integer-multiply / divide ops and (in the escape fields below) the branch/call/convert/divide-push ops.

| Opcode | Mnemonic | Lane |
|---:|---|---|
| `0x01` | `ScalarLoadSmemY` | Alu1 |
| `0x02` | `ScalarLoadSmemXY` | Alu1 |
| `0x03` | `ScalarStoreXToSmemY` | Alu1 |
| `0x09` | `DescriptorBasedDma` | Alu1 |
| `0x32` | `ScalarStoreXToSmemSumDestAndY` | Alu1 |
| `0x33` | `AddCbreg` | Alu1 |
| `0x34` | `TaskRequestClearIbuf` | Alu1 |
| `0x35` | `WriteCbreg` | Alu1 |
| `0x36` | `ReadCbreg` | Alu1 |
| `0x37` | `TaskRequest` | Alu1 |
| `0x3c` | `ScalarStoreCircularBuffer` | Alu1 |
| `0x3d` | `ScalarLoadCircularBuffer` | Alu1 |

`AddCbreg=0x33` is confirmed `(word6 & 0xFC000000) == 0xCC000000`, `0xCC000000 >> 26 = 0x33`; `TaskRequest=0x37` is `== 0xDC000000`, `0xDC000000 >> 26 = 0x37`. `FloatingPointAdd=0x11` exists only as `SparseCoreScalarAlu1FloatingPointAddOpcode` (`0x1eb7b4a0`, `== 0x44000000`, `0x44000000 >> 26 = 0x11`); there is no `ScalarAlu0FloatingPointAddOpcode` type in gfc — the lane asymmetry is structural, not a labeling artifact.

> **GOTCHA — `0x33`/`0x37` are NOT shared ops; they are Alu1-only.** A scheduler that treats the whole `0x00..0x3f` range as a flat lane-agnostic table will place `AddCbreg` or `TaskRequest` into lane 0, which has no encoder for them. The shared region is the arithmetic/compare block (`0x0a..0x31`); the memory/CBREG/task ops (`0x01..0x09`, `0x32..0x3d`) and the lane-exclusive FP/mul/div ops live on one lane only.

---

## The Four Class Escapes

When the primary 6-bit value names a class, the concrete op lives in a wider escape field that overlays the slot. The escape *values* are slot-independent (Alu0 and Alu1 use the same numbers); only the field's bit base differs per lane (`ScalarAlu0` higher in the struct, `ScalarAlu1` lower), mirroring the `@181`/`@154` opcode split.

| Escape | Field width | Value range | Decoded from |
|---|---|---|---|
| Control | 11-bit | `0x00..0x1d` | Alu0 word `+0x18` / Alu1 word `+6` |
| Register-read | 17-bit | `0x280..0x28d` | `(word3 & 0x7FFFC0000000000)` Alu0 |
| Config-set | 16-bit | `0x4001..0x4005` | `(word3 & 0x7FF03E000000000)` Alu0 |
| Divide-push | `0x16xxxx` | `0x160001..0x160002` | `(word3 & 0x7E003E000000000)` Alu0 |

### Control ops (11-bit escape)

These are the branch/call/fence/convert ops. The escape field sits above the 6-bit primary (Alu0 struct bit 48 / Alu1 bit 21). `Halt` (`0x1eb67500`) confirms the base: `(word15 & 0x7FF) == 0` → control `0x00`. `BranchAbsolute` (`0x1eb67d40`): `(word3 & 0x7FF000000000000) == 0x4000000000000`, `0x4000000000000 >> 48 = 0x04`.

| Opcode | Mnemonic | Opcode | Mnemonic |
|---:|---|---:|---|
| `0x00` | `Halt` | `0x0d` | `MoveY` |
| `0x02` | `PopDrf` | `0x0e` | `CountLeadingZeros` |
| `0x03` | `Delay` | `0x0f` | `Ceiling` |
| `0x04` | `BranchAbsolute` | `0x10` | `Floor` |
| `0x05` | `BranchRelative` | `0x18` | `BranchRelativeRotatingPreg` (GF, Alu0) |
| `0x06` | `CallAbsolute` | `0x1a` | `ScalarFenceSelect` |
| `0x07` | `CallRelative` | `0x1c` | `ScalarFenceStreamHbm` |
| `0x09` | `ScalarFence` | `0x1d` | `ScalarFenceStreamSpmem` |
| `0x0b` | `ConvertInt32ToFloat32` | | |
| `0x0c` | `ConvertFloat32ToInt32` | | |

`ScalarAlu1` adds three control ops to this set: `0x14 ReadDreg`, `0x15 WriteDreg`, `0x1b MoveCbreg`. `BranchSreg`/`CallSreg` appear as `ScalarAlu0` 6-bit-field forms (values 4/5) distinct from the absolute/relative branches above.

### Register-read ops (17-bit escape)

The chip hardware-register reads that gate SCS progress. `ReadRegisterLccLow` (`0x1eb67560`): `(word3 & 0x7FFFC0000000000) == 0xA000000000000`, `0xA000000000000 >> 42 = 0x280`.

| Opcode | Mnemonic | Opcode | Mnemonic |
|---:|---|---:|---|
| `0x280` | `ReadRegisterLccLow` | `0x288` | `ReadRegisterTracemark` |
| `0x281` | `ReadRegisterLccHigh` | `0x289` | `ReadRegisterTileid` |
| `0x282` | `ReadRegisterGtcLow` | `0x28a` | `ReadRegisterTaskBitmap` |
| `0x283` | `ReadRegisterGtcHigh` | `0x28b` | `ReadRegisterFenceStatus` |
| `0x286` | `ReadRegisterSparseCoreId` | `0x28c` | `ReadRegisterDifDepthRegister` |
| `0x287` | `ReadRegisterTag` | `0x28d` | `ReadRegisterDmaCreditRegister` |

### Config-set ops (16-bit escape)

The DMA-credit / tag / filter / throttle writes. `SetDmaCredit` (`0x1eb67ac0`): `(word3 & 0x7FF03E000000000) == 0x8006000000000`.

| Opcode | Mnemonic |
|---:|---|
| `0x4001` | `SetTag` |
| `0x4002` | `SetIndirectFilterValue` |
| `0x4003` | `SetDmaCredit` |
| `0x4004` | `SetDmaThrottleSflagRange` |
| `0x4005` | `SetRotatingPredicateRegister` (GF, Alu0) |

### Divide-push ops (Alu0 only)

The two-result divide that pushes quotient or remainder. `DivideWithRemainderXYPushQuotient` (`0x1eb67e60`): `(word3 & 0x7E003E000000000) == 0x2C0002000000000`; the primary part `0x2C0000000000000 >> 53 = 0x16` (the `DivideWithRemainderXY` base) and the sub part `0x2000000000 >> 37 = 1` give the `0x160001` form.

| Opcode | Mnemonic |
|---:|---|
| `0x160001` | `DivideWithRemainderXYPushQuotient` |
| `0x160002` | `DivideWithRemainderXYPushRemainder` |

> **QUIRK — register-read and config-set values cannot fit the 6-bit primary; they are wider escape fields.** A reimplementer reading "`ReadRegisterGtcLow = 0x282`" must place `0x282` in the 17-bit register-read field, not the 6-bit opcode field (which only spans `0x00..0x3f`). The primary opcode marks the *class* (register-read / config-set); the concrete `0x28x` / `0x400x` value lives in the overlaid escape field above it. The escape fields physically overlap the operand-selector bits of the slot, which is why a register-read op carries no x0/x1 operands.

---

## ScsScalarMisc — the Sync / Atomic Slot

### Composite encoding

`ScsScalarMisc` (op `@127`) is the sync/atomic engine. It carries no FP and no branch; it holds the sync-flag family that coordinates SC tiles with each other and with the TensorCore, plus an integer-ALU subset duplicated from the ALU lanes. Its opcode space is heavily composite (Form B): a 6-bit base names a class, and a 5-bit sub-opcode — the sync/atomic *mode* at struct-relative bit 47 (`0xF800000000000`), or an extended-ALU class at bit 58 (`0x7C00000000000000`) — picks the exact op.

| Base | Class | Sub field | Members (sub value) |
|---:|---|---|---|
| `0x00` | extended-ALU | bit 58 | `CoreInterrupt(0)`, `MoveY(13)`, `CountLeadingZeros(14)` |
| `0x01` | Sync compare-and-set | bit 47 | `SyncDone(0)`, `SyncEqual(1)`, `SyncNotEqual(2)`, `SyncGreater(3)`, `SyncGreaterOrEqual(4)`, `SyncLess(5)`, `SyncNotDone(6)`, `SyncEqualOrDone(7)`, `SyncNotEqualOrDone(8)`, `SyncGreaterOrDone(9)`, `SyncGreaterOrEqualOrDone(10)`, `SyncLessOrDone(11)` |
| `0x02` | SyncWatch | bit 47 | `SyncWatch{Done,Equal,NotEqual,Greater,GreaterOrEqual,Less,NotDone,EqualOrDone,NotEqualOrDone,GreaterOrDone,GreaterOrEqualOrDone,LessOrDone}` (modes 0..11) |
| `0x03` | SyncWatch escape | bit 58 | `SyncWatchWait(0)`, `SyncWatchWaitSelect(1)` |
| `0x04` | SyncWatch escape | bit 58 | `SyncWatchEnd(0)`, `SyncWatchEndSelect(1)` |
| `0x05` | set-sync | bit 47 | `SetSyncFlag(0)`, `SetSyncDone(1)`, `AddSyncFlag(2)` |
| `0x06` | read-sync | bit 58 | `ReadSyncFlag(0)`, `ReadSyncDone(1)`, `ReadSyncPublicAccess(2)` |
| `0x07` | barrier | bit 47 | `SyncBarrier(0)`, `SetPOrTState(4)` |
| `0x08` | Atomic | bit 47 | `AtomicTile{Write(0),Add(1),WriteSetDone(2),AddSetDone(3),WriteSetDoneInverted(4),AddSetDoneInverted(5)}`, `AtomicRemote{Write(6),Add(7),WriteSetDone(8),AddSetDone(9),WriteSetDoneInverted(10),AddSetDoneInverted(11)}` |

`SetSyncFlag` (base `0x05`) is confirmed by `((base) ^ 5 | (word & 0xF800000000000)) == 0` (sub 0); `AtomicTileAdd` by base `^8` / sub `^0x800000000000` → base 8, sub 1; `CoreInterrupt` is the all-zero opcode (base 0, extended-ALU sub 0); `CountLeadingZeros` is base 0, extended-ALU sub `0x3800000000000000 >> 58 = 14`.

The Atomic base `0x08` sub-field is a small product: `{Tile, Remote} × {Write, Add} × {plain, SetDone, SetDoneInverted}`, laid out so the low bit selects Add-vs-Write, the next pair selects the set-done modifier, and `+6` switches Tile→Remote.

### Flat 6-bit ops mirrored from the ALU set

`ScsScalarMisc` also carries flat Form-A 6-bit ops, an integer-ALU subset plus sync-state reads and trace ops:

| Opcode | Mnemonic | Opcode | Mnemonic |
|---:|---|---:|---|
| `0x0a` | `IntegerAdd` | `0x27` | `CompareUnsignedIntegerLte` |
| `0x0b` | `IntegerAddWithOverflowCheck` | `0x28` | `CarryOutFromIntegerUnsigned` |
| `0x0c` / `0x0d` | `IntegerSubtractYX` / `…WithOverflowCheck` | `0x29` | `PredicateOr` |
| `0x0e` / `0x0f` / `0x10` | `BitwiseAnd` / `BitwiseOr` / `BitwiseXor` | `0x2a` | `ReadSyncStateValue` |
| `0x17` / `0x18` / `0x19` | shift `XByYPlaces` (LSL/LSR/ASR) | `0x2b` | `ReadSyncStateDone` |
| `0x1c` / `0x1d` | `MaxOfTwoUnsignedIntValues` / `MinOf…` | `0x2d` | `SetTracemark` |
| `0x1e`–`0x23` | int compare Eq/Ne + signed Gt/Gte/Lt/Lte | `0x2e` | `Trace` |
| `0x24`–`0x26` | unsigned compare Gt/Gte/Lt | `0x2f` | `SetSyncFlagPublicAccess` |
| `0x31` | `ArithmeticShiftLeftXByYPlacesCheckOverflow` | `0x38` | `SmemFetchAndAdd` |

`IntegerAdd=0x0a` is confirmed `((word>>63) & 0x3F) == 0x0a`. The integer-ALU subset is exactly the lane-agnostic arithmetic/compare block — the Misc slot omits FP, multiply, divide, branch, and SMEM load/store, which is what makes it the dedicated sync/atomic + light-integer lane.

> **GOTCHA — `ScsScalarMisc` holds two parallel op-form families in gfc.** The binary emits both `SparseCoreScalarMisc<Op>Opcode` (82 forms) and `SparseCoreScsScalarMisc<Op>Opcode` (81 forms), with byte-identical `Matches()` predicates for the same op (e.g. `SyncEqual` decodes base 1 / mode 1 in both). They are the same hardware opcodes under two type spellings; the slot encoder `SparseCoreScsScalarMiscEncoder` (`0x1eb914a0`) takes a `SparseCoreScalarMisc` argument. A reimplementer needs one roster, not two — the duplicate type set does not double the encoding space.

---

## Per-Generation Deltas

The scalar ISA is gen-invariant for shared ops (vfc `IntegerAdd` decodes `==0x0a`, byte-identical to gfc); the deltas are small and concentrated in halt/yield and the rotating-predicate ring. The presence claims below are confirmed by the existence (or absence) of the corresponding `Matches()` type in each gen namespace: `vfc SparseCoreScalarAlu0HaltYieldOpcode` exists, the gfc one does not; `gfc SparseCoreScalarAlu0SetRotatingPredicateRegisterOpcode` exists, the vfc one does not.

| Aspect | Viperfish (vfc) | Ghostlite (glc) | 6acc60406 (gfc) |
|---|---|---|---|
| Primary opcode width | 6-bit | 6-bit | 6-bit |
| Opcode bundle bits | 127 / 154 / 181 | identical | identical |
| `ScsScalarMisc` op-forms | ~100 | (transitional) | 82 |
| `ScalarAlu0` op-forms | 79 | (transitional) | 78 |
| `ScalarAlu1` op-forms | 84 | (transitional) | 82 |
| `IntegerAdd` value | `0x0a` | `0x0a` | `0x0a` |
| VF-only ops | `HaltYield`, `HaltYieldConditional`, `ReadRegisterYieldRequest`, `ScalarFenceScmf` | — | — |
| GF-only ops | — | — | `BranchRelativeRotatingPreg`, `LogicalShiftLeftOnesXByYPlaces`, `SetRotatingPredicateRegister`, `MoveCbreg`, `ScalarStoreXToSmemSumDestAndY` |

> **NOTE — 6acc60406 simplified the sync model.** The VF/GL `ScsScalarMisc` carries a dual-channel sync family (`Set{Both,Other}Sync*`, `Add{Both,Other}SyncFlag`) and a `Yieldable*` sync set; the gfc roster drops both (down to 82 forms) and adds the single `SetPOrTState`. The interpretation is a non-yielding tile scheduler — fewer sync primitives, deterministic latency, driven by the rotating-predicate ring instead. A reimplementer targeting 6acc60406 must not emit the `Yieldable*` or `*Both*`/`*Other*` sync ops or the VF halt/yield ops; they have no encoder type in gfc.

---

## Function Map

All addresses are gfc (6acc60406) unless noted; the `Matches()` immediate is the authoritative opcode value.

| Symbol | Address | Opcode evidence |
|---|---|---|
| `SparseCoreScalarMiscIntegerAddOpcode::Matches` | `0x1ebabf00` | Form A `((w>>63)&0x3F)==0x0a` |
| `SparseCoreScalarMiscAtomicTileAddOpcode::Matches` | `0x1ebabbe0` | Form B base 8 / sub 1 |
| `SparseCoreScalarMiscSyncEqualOpcode::Matches` | `0x1ebab320` | Form B base 1 / mode 1 |
| `SparseCoreScalarMiscSetSyncFlagOpcode::Matches` | (gfc) | Form B base 5 / sub 0 |
| `SparseCoreScalarMiscCoreInterruptOpcode::Matches` | (gfc) | Form B base 0 / ext-ALU sub 0 |
| `SparseCoreScalarMiscCountLeadingZerosOpcode::Matches` | (gfc) | Form B base 0 / ext-ALU sub 14 |
| `SparseCoreScalarAlu0IntegerAddOpcode::Matches` | `0x1eb67660` | Form C `(w&0x7E0…)==0x140…` → 0x0a |
| `SparseCoreScalarAlu0BitwiseAndOpcode::Matches` | (gfc) | Form C → 0x0e |
| `SparseCoreScalarAlu0CompareIntegerEqOpcode::Matches` | (gfc) | Form C → 0x1e |
| `SparseCoreScalarAlu0HaltOpcode::Matches` | `0x1eb67500` | control escape `(w15&0x7FF)==0` → 0x00 |
| `SparseCoreScalarAlu0BranchAbsoluteOpcode::Matches` | `0x1eb67d40` | control escape → 0x04 |
| `SparseCoreScalarAlu0ReadRegisterLccLowOpcode::Matches` | `0x1eb67560` | 17-bit escape → 0x280 |
| `SparseCoreScalarAlu0SetDmaCreditOpcode::Matches` | `0x1eb67ac0` | 16-bit escape → 0x4003 |
| `SparseCoreScalarAlu0DivideWithRemainderXYPushQuotientOpcode::Matches` | `0x1eb67e60` | divide-push → 0x160001 |
| `SparseCoreScalarAlu0SetRotatingPredicateRegisterOpcode::Matches` | `0x1eb67b00` | config escape, GF-only |
| `SparseCoreScalarAlu1FloatingPointAddOpcode::Matches` | `0x1eb7b4a0` | Form C `==0x44000000` → 0x11, Alu1-only |
| `SparseCoreScalarAlu1AddCbregOpcode::Matches` | `0x1eb7b5a0` | Form C `(w6&0xFC000000)==0xCC000000` → 0x33 |
| `SparseCoreScalarAlu1TaskRequestOpcode::Matches` | `0x1eb7b620` | Form C `==0xDC000000` → 0x37 |
| `SparseCoreScalarAlu0Encoder::Encode` | `0x1eb693c0` | opcode `BitCopy(.,181,.,6)`; escapes @176/170/165 |
| `SparseCoreScsScalarMiscEncoder::Encode` | `0x1eb914a0` | opcode `BitCopy(.,127,.,6)`; pred @133/136/137 |
| `BitCopy` | `0x1fa0a900` | LE packer `(dst, dst_bitoff, src, src_bitoff, nbits)` |

Cross-gen anchors: vfc `SparseCoreScalarMiscIntegerAddOpcode::Matches` `0x1e8ff7c0` decodes `==0x0a` (gen-invariance); vfc `SparseCoreScalarAlu0HaltYieldOpcode::Matches` `0x1ee81460` exists (VF-only). The `TensorCore*` and `SparseCoreTec*` op-form types share the gfc `isa` namespace — match the `SparseCoreScalar`/`SparseCoreScsScalar` prefix exactly to avoid pulling TC or TEC predicates into the SCS scalar roster.

---

## Considerations

- **Opcode source of truth is the `Matches()` immediate, not the encode-side switch.** The `Encoder::Encode` dispatch uses sequential ODS enum ordinals as `switch` case labels; only the `BitCopy` value it writes (and the `Matches()` value that reads it back) is the silicon opcode. Decode and encode agree on the `Matches()` value; the switch case number is an internal ordinal.
- **No FP and no branch in the Misc slot.** `ScsScalarMisc` is sync/atomic + integer-ALU only. FP arithmetic, FP compare, branches, calls, multiply, divide, and SMEM load/store live in the ALU lanes. A scheduler must not place a sync op in an ALU lane or an FP/branch op in Misc.
- **Lane asymmetry is structural.** `ScalarAlu1` owns SMEM load/store, CBREG, Dreg, FP add/sub, and task-request; `ScalarAlu0` owns branch/call/convert/divide-push and FP/integer multiply. The shared arithmetic/compare block `0x0a..0x31` decodes identically on both, but the lane-exclusive ops have an encoder type on one lane only.
- **The composite sub-opcode absolute bundle bit is partly inferred (HIGH / LOW).** The 6-bit primary opcode bundle bit is confirmed (`@127`/`@154`/`@181`). The composite `ScsScalarMisc` sub-opcode field is recovered as a *struct-relative* offset (sync/atomic mode at bit 47, extended-ALU class at bit 58); its *absolute* bundle bit (slot base 111 + within-slot offset, per the [SCS Engine](scs-engine.md) 27-bit template) is not pinned for every one of the ~50 composite Misc ops individually. Decode each by primary opcode + the struct-relative sub field; the absolute-bit map for the sub field is a remaining gap.
- **`FloatingPointMultiply=0x13` and the FP minmax/compare values are HIGH, not CONFIRMED.** The integer/bitwise/shift/compare and the lane-exclusive `AddCbreg`/`TaskRequest`/`FloatingPointAdd` values were read from their `Matches()` immediates directly; the FP-multiply, FP-minmax (`0x1a/0x1b`), and FP-compare (`0x2a..0x2f`) values are taken from the op-form roster ordering and a sampled subset, not a full per-op immediate sweep.

---

## Related Components

| Name | Relationship |
|---|---|
| `SparseCoreScsScalarMiscEncoder::Encode` (`0x1eb914a0`) | writes the `ScsScalarMisc` opcode `@127` and predication header |
| `SparseCoreScalarAlu0Encoder::Encode` (`0x1eb693c0`) | writes the `ScalarAlu0` opcode `@181` and the escape fields |
| `BitCopy` (`0x1fa0a900`) | the LE packer every slot encoder uses to write the opcode bits |
| per-op `SparseCore<Slot><OpName>Opcode::Matches()` | the opcode→mnemonic source — one type per opcode per gen |

## Cross-References

- [SCS (Scalar) Engine](scs-engine.md) — the 32-byte bundle, the slot bases (111/138/165), and the 27-bit scalar slot template this roster's opcode field sits in.
- [Vector Opcode Enumeration](vector-opcode-enum.md) — the TEC vector-slot opcode roster (VectorAlu / Load / Store / Result / Extended); the vector-side analog of this page.
- [TAC Engine](tac-engine.md) — the tile-fetch DMA issuer (VF/GL) that reuses the SCS scalar-lane bundle layout for its Dma/Stream forms.
- [SparseCore Overview](overview.md) — the three engine classes, per-gen presence, and the `TpuSequencerType` codec-template enum.
- [M-Register Predicate Word](m-register-predicate.md) — the 3-bit/4-bit predication header that overlays each scalar slot above the opcode field.
- [CBREG Circular-Buffer Register](cbreg.md) — the circular-buffer registers driven by the Alu1 `AddCbreg`/`ReadCbreg`/`WriteCbreg`/`MoveCbreg` ops enumerated here.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore ISA — [back to index](../index.md)
