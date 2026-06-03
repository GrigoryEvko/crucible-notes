# Predicate-Register File and Per-Slot Predication

> *Every offset, address, bit position, and constant on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped). Other versions differ.*

## Abstract

A TPU VLIW core predicates execution at two granularities, and the two are backed by two physically distinct register files. The **scalar predicate file** (`Preg` / LLVM `PPRRegClass`) holds single-bit booleans that gate a whole bundle slot and the conditional branch; the **vector mask file** (`Vmreg` / LLVM `MPRRegClass`) holds per-lane booleans that gate individual lanes inside one vector op. This page documents the scalar predicate file: its per-generation register count, the per-slot predicate field encoding, the per-slot predication model, and the predicate-set / predicate-combine op families. The vector mask file is its own page; here it is the contrast that makes the scalar file legible.

The model is the familiar predicated-VLIW one — every functional slot carries a guard, and the empty-slot encoding is "predicate = never" rather than a separate valid bit — but with two TPU specifics a reimplementer must reproduce exactly. First, the field grows across silicon: Jellyfish/Dragonfish use a **5-bit** per-slot field (4-bit register index + 1 negate bit), while Viperfish onward use a **7-bit** field (4-bit index + 1 negate + 2-bit mode/extension), decoded byte-exactly from `TPUMCCodeEmitter::encodePredicateOperand`. Second, `6acc60406` (gen 5, external name `TPU7x`) breaks the per-slot model: instead of each slot carrying a full register index, the bundle carries a dedicated `TensorCorePredicates` slot holding a two-entry predicate pool (`pred_0`, `pred_1`) at bits 496..505, each with its own inversion bit, and per-slot fields hold a 2-bit selector (e.g. the sequencer slot's selector @ bit 489) that indexes into that pool — a third distinct predicate in one bundle is an encode-time error.

For reimplementation, the contract is:

- The per-gen predicate-register **count**: 15 named registers (P0..P14) on JF/DF/PF-TensorCore via the `HardwareBundleBits` constants; a 16-value encodable index namespace (`getNumPredicateRegisters()` returns `16`) on the BarnaCore subtarget and on all of V5+, of which the `chip_parts` proto reports `PREG = 14` as the allocatable hardware count (see [Per-Codename Constants](../targets/per-codename-hw-constants.md)).
- The two sentinel values: `kAlwaysExecute = 15` (slot runs unconditionally) and `kNeverExecute = 31` (slot is dropped / empty).
- The per-slot predicate **field layout**: legacy 5-bit (index@0:3, negate@4) vs V5+ 7-bit (index@0:3, negate@4, mode@5:6).
- The per-slot predication model: every populated functional slot carries its own predicate field (except `6acc60406`, whose per-slot field is a 2-bit selector into a two-entry bundle-level pool).
- The op families: 16 scalar compare→predicate ops (`Compare*` with a `PdstField`), and the four combine ops (`PredicateOr` / `PredicateNegate` / `PredicateMove` / `PredicateImmediate`) — with **no native predicate-AND** on any generation.

| | |
|---|---|
| **Scalar predicate file** | `Preg` / `deepsea::mnemonics::PregNumber` / LLVM `TPU::PPRRegClass` @ `0x2192f380` |
| **Vector mask file** | `Vmreg` / `VmregNumber` / LLVM `TPU::MPRRegClass` @ `0x2192f0c0` (separate page) |
| **JF/DF/PF-TC count** | 15 named (P0..P14) — `HardwareBundleBits::kPredicateRegisterCount` @ `0xb834cf4` = `15` |
| **BC + V5+ count** | 16 — `*Subtarget::getNumPredicateRegisters()` returns `16` (Bc/Vfc/Glc/Gfc) |
| **Always-execute sentinel** | `kAlwaysExecute = 15` @ `0xb834cf8` (LLVM reg #6 in the printer) |
| **Never-execute sentinel** | `kNeverExecute = 31` @ `0xb834cfc` (empty-slot mark) |
| **Legacy field encode/decode** | `DecoderJf::GetPredicateRegister` @ `0x1e84eae0` (`val & 15`), `GetPredicateValue` @ `0x1e84eb00` (`(val & 16)==0`) |
| **V5+ field encoder** | `TPUMCCodeEmitter::encodePredicateOperand` @ `0x13c77c40` (4+1+2 = 7-bit) |
| **Rotating predicates** | Trillium-only — `TPUGfcSubtarget::hasRotatingPredicates()` @ `0x13c62c20` = `1`; all others `0` |
| **Native predicate-AND** | none — `*Subtarget::hasPredicateAnd()` = `0` on every subtarget |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Two Distinct Boolean Register Files

The hardware has two boolean register files, not one file viewed two ways, and conflating them is the first reimplementation trap. The scalar predicate file gates *slots and branches*; the vector mask file gates *lanes inside a vector op*. They compose: a vector store slot can be both scalar-predicated (does the slot run at all?) and lane-masked (which lanes write?).

| | Scalar predicate (`Preg`) | Vector mask (`Vmreg`) |
|---|---|---|
| IR strong-int type | `deepsea::mnemonics::PregNumber` | `deepsea::mnemonics::VmregNumber` |
| LLVM register class | `TPU::PPRRegClass` @ `0x2192f380` | `TPU::MPRRegClass` @ `0x2192f0c0` (`MPR_3Tuple`/`MPR_4Tuple` spans) |
| Register name root | `P0`..`P14` (per-core `_0`/`_10` suffixes) | `M0`, `M8_M9_M10_M11` (tuple) |
| Width | **1 bit** | per-(lane × sublane-group) — a tuple of mask words |
| Gates | bundle slot, scalar ALU commit, conditional branch | masked vector store/load, masked vector ALU lane commit, select/blend |
| Debug accessor | `Read/WritePredicateRegister(core[, seq], int)` | `Read/WriteVectorMaskRegister(core[, seq], sublane_group, lane)` |

The 3-argument vector-mask debug accessor (`core`, `sublane_group`, `lane`) is the proof that a `Vmreg` is addressed per-(lane × sublane-group), whereas a `Preg` is a single bit. Both files exist on every generation: the `Read*Register` debug methods are present on the JXC, PXC, and VXC drivers alike. JF/DF additionally expose a *separate* BarnaCore predicate sub-file (`Read/WriteBarnaCorePredicateRegister`), whose register range was not separately decoded (the PF BarnaCore subtarget reports 16).

> **GOTCHA — predicate ≠ mask.** A reimplementation that allocates one boolean file and routes both slot guards and lane masks through it will mis-encode every masked vector op: the slot's scalar predicate and the op's lane mask are independent operands drawn from independent register namespaces. The `Preg`/`Vmreg` split is the central architectural fact.

The vector mask file (production via `VectorAlu*CreateMask`/`CreateLaneMask`/`CreateSublaneMask`, combine via `mlir::llo::VectorMaskAnd/Or/NegateOp`) lives on [vcreate_mask / M-Register](slot-vcreate-mask-mregister.md). The remainder of this page is the scalar predicate file.

---

## Predicate-Register Count Per Generation

The count is exposed two ways, and the two reconcile to the same 4-bit index plus special encodings. The legacy proto path uses the `HardwareBundleBits` constants in `.rodata`; the LLVM path uses per-subtarget `getNumPredicateRegisters()` overrides.

The three Jellyfish constants are byte-verified at `0xb834cf4` as three little-endian `uint32`: `0f000000 0f000000 1f000000`.

```c
// platforms_deepsea::jellyfish::isa::HardwareBundleBits  (.rodata)
kPredicateRegisterCount = 15;   // @ 0xb834cf4  — count of named usable Pregs P0..P14
kAlwaysExecute          = 15;   // @ 0xb834cf8  — the always-true sentinel (slot runs)
kNeverExecute           = 31;   // @ 0xb834cfc  — slot is a NOP / not executed
```

The V5+ subtargets each override `getNumPredicateRegisters()` to a literal `16` (each decompiles to a single `return 16;`):

```c
llvm::TPUBcSubtarget ::getNumPredicateRegisters()  @ 0x13c59780  -> 16
llvm::TPUVfcSubtarget::getNumPredicateRegisters()  @ 0x13c5f6e0  -> 16
llvm::TPUGlcSubtarget::getNumPredicateRegisters()  @ 0x13c615c0  -> 16
llvm::TPUGfcSubtarget::getNumPredicateRegisters()  @ 0x13c630e0  -> 16
```

The base `llvm::TPUSubtarget` provides no override — JF/DF/PF-TensorCore inherit the 15-register `HardwareBundleBits` model. The `15` vs `16` is not a contradiction: `15` is the count of **allocatable** named registers (`ValidatePredicateRegister` accepts only index `< 15`, `cmp $0xf; jb`), while `16` is the size of the **encodable** 4-bit index namespace `0..15`, where index 15 maps to the always-execute sentinel as a constant-true register.

| Codename | TpuVer | Family | `getNumPredicateRegisters` | Named usable regs | Notes | Confidence |
|---|---|---|---|---|---|---|
| Jellyfish | 0 | JXC | 15 (`HardwareBundleBits`) | P0..P14 | + always(15), never(31) | CONFIRMED |
| Dragonfish | 1 | JXC | 15 (inherits JF) | P0..P14 | alias of Jellyfish model | HIGH |
| Pufferfish | 2 | PXC | TC 15 (base) / BC 16 (`TPUBcSubtarget`) | P0..P14 (TC) / P0..P15 (BC) | BC subtarget bumps to 16-index | CONFIRMED |
| Viperfish | 3 | VXC | 16 (`TPUVfcSubtarget`) | P0..P15 | full 16-index namespace | CONFIRMED |
| Ghostlite | 4 | GXC/GLC | 16 (`TPUGlcSubtarget`) | P0..P15 | | CONFIRMED |
| Trillium | 5 | GXC/GFC | 16 (`TPUGfcSubtarget`) | P0..P15 | + rotating predicates, + dual-slot pool | CONFIRMED |

> **NOTE —** the always-execute register appears as index `15` in the proto/`HardwareBundleBits` view but as LLVM register `#6` in the MC printer (`printPredicateOperandAux` @ `0x13c73c80` tests `reg == 6` and emits no `@Pn`). The two numbering schemes are different register-ID spaces for the same architectural "unpredicated" register; a decoder must not assume the LLVM register number equals the hardware index.

---

## Per-Slot Predicate Field Encoding

There are two encoding views, mapping to the same hardware bits, distinguished by generation.

### Legacy 5-bit field (Jellyfish / Dragonfish)

`DecoderJf::GetPredicateRegister` (`0x1e84eae0`) and `GetPredicateValue` (`0x1e84eb00`) decompile to single expressions that pin the layout:

```c
// DecoderJf::GetPredicateRegister(uint8 v) @ 0x1e84eae0
return v & kPredicateRegisterCount;          // v & 15  -> 4-bit reg index (0..14; 15 = always)

// DecoderJf::GetPredicateValue(uint8 v) @ 0x1e84eb00
return (v & (kPredicateRegisterCount + 1)) == 0;   // (v & 16) == 0  -> bit 4 is the negate/value bit
```

So the Jellyfish per-slot predicate field is **5 bits**:

| Bits | Field | Meaning |
|---|---|---|
| `[0:3]` | register index | `0..14` → P0..P14; `15` → always-execute (`kAlwaysExecute`) |
| `[4]` | polarity / negate | the "value" bit; cleared = take predicate as-is |
| all 5 set | `0x1F = kNeverExecute` | slot does not run (empty-slot fill) |

The BarnaCore vector-load decode path additionally extracts the full 5-bit field (`shld $0x2; and $0x1f`) and compares against `kNeverExecute = 31` to skip a slot.

### V5+ 7-bit field (Viperfish / Ghostlite / Trillium)

`TPUMCCodeEmitter::encodePredicateOperand` (`0x13c77c40`) builds the field with three deposits — confirming the `4 + 1 + 2` layout that [MC-Emitter](mc-emitter.md) documents:

```c
// encodePredicateOperand @ 0x13c77c40 (decompiled, exact)
//   a1 = reg-encoding table, a2 = MCInst operands, a3 = per-slot operand index, a4 = dst APInt
flags = ops[a3].flags;                                  // operand flags word
APInt::insertBits(dst, regEncTable[ops[a3].preg], /*pos=*/0, /*width=*/4);  // [3:0] reg index
if (flags & 1)
    dst.word0 |= 0x10;                                  // [4] negate / inversion
return APInt::insertBits(dst, (flags >> 5) & 3, /*pos=*/5, /*width=*/2);    // [6:5] mode/extension
```

| Bits | Field | Meaning |
|---|---|---|
| `[0:3]` | register index | 4-bit, `0..15` (permuted — see below) |
| `[4]` | negate / inversion | set via `\|= 0x10` |
| `[5:6]` | 2-bit mode / extension | predicate-bank / rotating-stage selector; `0` in non-rotating code, drives the rotating-predicate stage on Trillium |

The four `[5:6]` modes correspond to the `PREDICATION_ALWAYS` / `NEVER` / `OR_NEVER` / `OR_INVERTED_NEVER` enum the [Sequencer Slot](slot-sequencer.md) documents. The printer (`printPredicateOperandAux` @ `0x13c73c80`) confirms the read side: `reg == 6` (LLVM always) emits no `@Pn`; `flags & 1` emits the `!` negation prefix; `flags & 0x7E` (bits 1-6 set) routes to the BarnaCore predicate printer.

> **QUIRK — the hardware register index is a permutation, not a raw 0..15.** `pxc::mnemonics::PredicationIsPositive` (`0x1d2e98c0`) masks positive values with `0x7FFF` (bits 0-14) and negated values with `0x7FFF0000` (bits 16-30): the `Predication` enum is `0..14 = +P0..P14`, `16..30 = ~P0..~P14`. `GetPregNumber` (`pxc` @ `0x1d2e9920`, `vxc` @ `0x1d32ad40`, `glc` @ `0x1d36b100`, `gfc` @ `0x1d3ab9a0`) is a 16-way jump table that maps the permuted hardware enum value back to the canonical Preg index. A reimplementation that emits a raw register index into the field will select the wrong predicate; the permutation table is the encode/decode mapping. The full 16-entry permutation per gen was not dumped (only the dispatch shape and the Pufferfish head `0→none, 14, 11, 4, 12, 9, 2, 3, 7, 1, 5, …` — MEDIUM).

The wide predicate-source operand used by predicate-OR and V5+ slot predication is `PredicationOr`, a dense enum `[0, 31]` (32 values = 16 regs × {positive, negated}), confirmed via the `NameOfDenseEnum<…PredicationOr_descriptor, 0, 31>` instantiations for `pxc`/`vxc`/`gxc::glc`/`gxc::gfc`.

---

## Per-Slot Predication Model

Every executable functional slot in a bundle carries its **own** predicate field — predication is per-slot, not per-bundle. A bundle can therefore run different slots under different predicates in the same cycle (per-slot predicated VLIW issue). The evidence is the per-slot `SetPredicate` / `EmitPredicationToSlot` template instantiations, one per slot type:

```text
Pufferfish (PXC) — xla::pufferfish::(anon)::SetPredicate<Slot> for each of:
  TensorCoreScalar0/1, TensorCoreVectorAlu0/1, TensorCoreVectorExtended0/1,
  TensorCoreVectorLoad, TensorCoreVectorStore, TensorCoreVectorResult0/1, TensorCoreMisc
  (+ BarnaCoreSequencerScalar0/1, BarnaCoreChannelVectorAlu1, … for BC)

V5+ (VXC/GXC) — isa_emitter::EmitPredicationToSlot<PredicateDest, Predication, Slot> for each of:
  SparseCoreScalarAlu, SparseCoreScalarMisc, SparseCoreStream, SparseCoreDma, SparseCoreTecDma,
  SparseCoreTecVectorAlu, SparseCoreTecVectorExtended, SparseCoreTecVectorLoad,
  SparseCoreTecVectorResult, SparseCoreTecVectorStore  (TensorCore side mirrors these)
```

Each populated slot independently selects a guarding predicate register plus polarity; the encoder stamps each slot's predicate field, and an empty slot is filled with `kNeverExecute = 31` (the NOP fill — see [Bundle Model §empty-slot convention](bundle-model-overview.md#empty-slot-and-nop-convention)). The def-use of predicates in the LLO IR is tracked by `ScalarInstruction::add_consumes_predicate_register` (`0x1e851be0`) and `add_produces_predicate_register` (`0x1e851c60`), feeding the bundle packer's hazard analysis; `ValidatePacking` re-checks every slot's field is in range `{0..14, 15, 31}`.

### Trillium dual-predicate pool

Trillium (gfc) is the only generation with a dedicated **bundle-level** predicate-encoder slot. Instead of each functional slot carrying a full register index, the bundle carries a `TensorCorePredicates` container holding two references — `pred_0` and `pred_1`, each a `PredicationSlot` enum value (`0..15` register index) plus its own `inversion` bit — and the per-functional-slot fields index into that two-entry pool. The container's opcode descriptor is `NoInstructionsH` (a pure container, not a callable op).

The pool is a shared resource: a bundle can reference at most two distinct `(register, inversion)` pairs across all of its sub-slots. A third distinct predicate triggers an encode-time error, with the binary embedding the verbatim strings:

```text
"Field pred_0 of TensorCorePredicates PredicationSlot is an enum and value 0x%x does not match any encodings."
"Field pred_1 of TensorCorePredicates PredicationSlot ..."
"bundle predication overflow; both predicate slots are already taken
 [pred_0: %s, inversion: %d], [pred_1: %s, inversion: %d];
 attempted to place another predicate: [%s, inversion: %d] in the same bundle."
```

`PredicationSlot` is a dense enum `[0, 15]` (16 register indices); the inversion is carried separately per pool entry. This packing is how Trillium fits a wider compute fabric into the same 64-byte bundle — see [Bundle GF §dual-predicate slot](bundle-gf.md#the-dedicated-dual-predicate-slot-gf-vs-ghostlite).

---

## Predicate-Set Ops (Scalar Compare → Predicate)

The scalar ALU produces a predicate via a compare op. Each compare op carries an explicit `PdstField` (the destination `Preg`) plus `XField` / `YField` (the two scalar operands). The complete family is **16 opcodes**, split by type and operator:

| Group | Ops | Count |
|---|---|---|
| Floating-point compare | `CompareFloatingPoint{Eq, Neq, Gt, Gte, Lt, Lte}` | 6 |
| Integer eq/ne (untyped) | `CompareInteger{Eq, Ne}` | 2 |
| Signed integer order | `CompareSignedInteger{Gt, Gte, Lt, Lte}` | 4 |
| Unsigned integer order | `CompareUnsignedInteger{Gt, Gte, Lt, Lte}` | 4 |

Integer equality needs no signedness; only the ordering comparisons split into signed vs unsigned, which is why the family is `6 + 2 + 4 + 4 = 16`. Each emits four fields: `<Op>Opcode`, `<Op>PdstField` (the destination Preg), `<Op>XField`, `<Op>YField`. These exist for both scalar lanes (Alu0 and Alu1) on every V5+ engine (`TensorCoreScalarAlu0/1`, `SparseCoreScalarAlu0/1`, `SparseCoreTecScalarAlu0/1`, `SparseCoreScalarMisc`) — either scalar lane can produce a predicate. Confirmed encoders include `EncodeTensorCoreScalarAlu0CompareIntegerEq` (`0x1f87e6c0`) and `EncodeSparseCoreScalarAlu0CompareFloatingPointEq` (`0x1eb6d3a0`). The vector side instead produces a `Vmreg` mask, not a predicate.

---

## Predicate-Combine Ops (Scalar Predicate Logic)

The scalar predicate file is combined via four dedicated emitter ops, decoded from the Pufferfish TensorCore emitter (other gens analogous):

| Op | Address | Semantics | Confidence |
|---|---|---|---|
| `EmitPredicateOr(dst, a, b)` | `0x14105300` | `dst = a OR b`; the only logical-combine primitive the hardware exposes | CONFIRMED |
| `EmitPredicateNegate(src, dst)` | `0x141052e0` | `dst = NOT src`; implemented as `dst \| 0x100000000` dispatched via vtable `+0xe0` (the high dword sets the negate flag) | CONFIRMED |
| `EmitPredicateMove(src, dst)` | `0x141052c0` | `dst = src` (predicate copy) | CONFIRMED |
| `EmitPredicateImmediate(dst, value)` | `0x141033e0` | `dst = const` (true/false materialization) | CONFIRMED |

The `EmitPredicateNegate` body confirms the negate mechanism byte-exactly:

```c
// PufferfishTensorCoreEmitter::EmitPredicateNegate @ 0x141052e0
return (*(vtable + 0xe0))(this, dst, src | 0x100000000LL);   // high dword = negate flag
```

The ISA-level `PredicateOr` opcode (what `EmitPredicateOr` lowers to) is present as `Encode/Decode<engine>PredicateOr` for the TensorCore, SparseCore, SparseCoreTec, and Misc scalar lanes; its operand is a `PredicationOr` (32-value dense enum). At the sequencer level it appears as `ScalarPredicateOrH` / `PredicateOrH`.

> **QUIRK — there is no native predicate-AND.** `hasPredicateAnd()` returns `0` on every subtarget (`TPUBcSubtarget` @ `0x13c59260`, `TPUVfcSubtarget` @ `0x13c5ef60`, `TPUGlcSubtarget` @ `0x13c60e40`, `TPUGfcSubtarget` @ `0x13c629c0`, all `xor eax`/`return 0`). The compiler synthesizes `a AND b` via De Morgan: `NOT(NOT a OR NOT b)`, using `PredicateOr` plus the negate bit on the `Predication` operand (which makes the inversion free). A reimplementation that expects a hardware AND opcode will find none.

---

## Rotating Predicates (Trillium-Only)

`hasRotatingPredicates()` is `1` on Trillium and `0` everywhere else:

```c
TPUBcSubtarget ::hasRotatingPredicates() @ 0x13c59400 -> 0
TPUVfcSubtarget::hasRotatingPredicates() @ 0x13c5f1e0 -> 0
TPUGlcSubtarget::hasRotatingPredicates() @ 0x13c610c0 -> 0
TPUGfcSubtarget::hasRotatingPredicates() @ 0x13c62c20 -> 1   // Trillium only
```

Rotating predicates provide a ring of predicate registers the hardware advances one position per loop iteration, used to enable/disable software-pipeline stages (prolog / steady-state / epilog) without explicit guard code. The machinery is the `TPURotatingPredicateModuloExpander` (`expand`, `peelPrologs`, `initializeRotatingPredicatesAndPredicatePrologs`, `propagateRotatingPredicate`, `generateBranchRotate`); on generations *without* the hardware feature (Vfc/Glc), the `TPURotatingPredicateEmuModuloExpander` lowers a rotating-predicate loop into explicit predicate-register moves when `EnableRotatingPredicateEmulation` (`0x224e1148`) is set. The 2-bit `[5:6]` extension in `encodePredicateOperand` is the natural place the rotating-stage offset is encoded; the Trillium-only sequencer ops `SetRotatingPredicateRegister` and `BranchRelativeRotatingPreg` drive the ring (see [Hardware Loop-Counter](slot-loop.md)). The ring depth is computed per software-pipelined loop from the initiation interval and stage count, not a static constant.

---

## Cross-References

- [SPU / Scalar Slot](slot-spu-scalar.md) — the scalar-ALU lanes that produce predicates via the `Compare*` family and host the `PredicateOr`/`Negate`/`Move`/`Immediate` ops.
- [vcreate_mask / M-Register](slot-vcreate-mask-mregister.md) — the vector mask file (`Vmreg`/`MPR`) this page contrasts against: per-lane masks, `CreateMask`/`CreateLaneMask`/`CreateSublaneMask`, `VectorMaskAnd/Or/Negate`.
- [MC-Emitter](mc-emitter.md) — `encodePredicateOperand` (`0x13c77c40`), the byte-exact 4+1+2 deposit sequence for the V5+ predicate field.
- [Bundle Model](bundle-model-overview.md) — the VLIW bundle and the `kNeverExecute` empty-slot convention into which each slot's predicate field is stamped.
