# InstructionType

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, `neuronxcc/starfish/lib/libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`; cp310/11/12 are ABI-equivalent). For `.text`/`.rodata`, virtual address equals file offset. Other wheels differ — treat every address as version-pinned.*

## Abstract

`bir::InstructionType` is the single flat enum that names every operation the BIR can hold. It is a plain `enum` of **110 members, ordinals 0–109**, dense and gap-free. Every `bir::Instruction` object carries its `InstructionType` in one 32-bit field; that field is the discriminator for LLVM-style RTTI (`isa<>`/`dyn_cast<>`), for the printer, for the JSON serializer, and for every pass that needs to know "what kind of op is this". Unlike an MLIR dialect — where each op is a distinct C++ class registered with a `TypeID` — BIR uses one closed enum plus a parallel class hierarchy, and the two are kept in lock-step by hand: each `InstXxx` subclass hard-codes the ordinal it expects.

The enum is recovered intact from `bir::InstructionType2string` (`0x2d5bf0`), a 110-case switch that maps each ordinal to its canonical spelling and falls through to an `"Unknown opcode"` `assert` keyed to `walrus/ir/lib/IR/Instruction.cpp:79`. That assert path is the strongest possible anchor: it is the compiler's own statement that the legal ordinal range is exactly the cases it enumerates. This page reproduces the full table, the opcode field offset, and the two distinct "is this op in family X" mechanisms BIR actually uses — there is no single bitmask register; instead there is a per-opcode **`sameInst`** virtual comparator (structural equality, used by CSE) and a battery of free **`isXxx(const Instruction*)`** family predicates that test the opcode field against hard-coded sets, ranges, shifted 64-bit masks, and byte lookup tables.

For reimplementation, the contract is:

- The 110-entry ordinal→name table, verbatim, in declaration order (it is the wire/printer contract).
- The opcode field location: `*(uint32_t*)(instr + 0x58)` (the 22nd dword), read by every classifier on this page.
- `sameInst`: the per-opcode structural-equality dispatch — what makes two instructions "the same" for de-duplication.
- The family-predicate idioms: equality, half-open ranges, `(MASK >> (opc - base)) & 1` shifted masks, and a `byte_786480[opc - 19]` lookup table — plus the **sub-type discriminator** fields that split one opcode (e.g. `DMACopy`) into several logical families.

| | |
|---|---|
| **Enum** | `bir::InstructionType` — 110 members, ordinals 0–109, `uint32_t` underlying |
| **Name table** | `bir::InstructionType2string` @ `0x2d5bf0` (110-case switch → `assert` @ `Instruction.cpp:79`) |
| **Opcode field** | `*(uint32_t*)(instr + 0x58)` — the 22nd dword of `bir::Instruction` |
| **Structural compare** | `bir::Instruction::sameInst(Instruction*)` @ `0x2db7b0` (8119 B; 110-case dispatch) + 16 per-class overrides |
| **Family predicates** | free `bir::isXxx(const Instruction*)` in `0x30e9a0–0x315710` |
| **Owning translation unit** | `neuronxcc/walrus/ir/lib/IR/Instruction.cpp` (path baked into the assert string) |
| **Cross-refs** | [7.1 Instruction base](instruction-base.md) · [7.9 enum crosswalk](enum-crosswalk.md) · [7.14 op-family JSON schema](op-family-schema.md) |

---

## The 110-opcode enum

The table below is read directly off the `bir::InstructionType2string` switch at `0x2d5bf0`: case `N` emits string `S`, so ordinal `N` is member `S`. The ordering is the C++ declaration order — it is not alphabetical, not grouped, and (critically) **not stable across enum edits**, because adding a member in the middle renumbers everything after it. Persisted BIR therefore stores names, not ordinals; the ordinal is an in-memory detail. The "family" column is this page's editorial grouping (drawn from the `sameInst` ranges and `isXxx` predicates below), not a field in the binary.

| # | Member | Family | # | Member | Family |
|---|---|---|---|---|---|
| 0 | `Generic` | generic | 55 | `NKIKernel` | kernel |
| 1 | `GenericCopy` | generic | 56 | `NKIKLIRKernel` | kernel |
| 2 | `GenericRelu` | generic | 57 | `DevicePrint` | misc |
| 3 | `AbstractCopy` | generic | 58 | `GetRandState` | rng |
| 4 | `Activation` | compute | 59 | `SetRandState` | rng |
| 5 | `ReadActivationAccumulator` | compute | 60 | `Rand` | rng |
| 6 | `LoadActFuncSet` | compute | 61 | `Iota` | compute |
| 7 | `MatmultBase` | matmul | 62 | `TensorScalarAffineSelect` | tensorscalar |
| 8 | `Matmult` | matmul | 63 | `RangeSelect` | compute |
| 9 | `MatmultSparse` | matmul | 64 | `GetSequenceBounds` | misc |
| 10 | `Memset` | memory/rng | 65 | `Dropout` | rng |
| 11 | `GetGlobalRankId` | collective | 66 | `GetCurProcessingRankID` | collective |
| 12 | `NoOp` | control | 67 | `DMATrigger` | dma |
| 13 | `EventSemaphore` | sync | 68 | `DMADescriptor` | dma |
| 14 | `GroupResetSemaphores` | sync | 69 | `DMADescriptorCopy` | dma |
| 15 | `AllEngineBarrier` | sync | 70 | `DMADescriptorCCE` | dma |
| 16 | `Drain` | sync | 71 | `DMADescriptorTranspose` | dma |
| 17 | `Halt` | control | 72 | `DMADescriptorReplicate` | dma |
| 18 | `DMA` | dma | 73 | `RegisterAlu` | register |
| 19 | `Load` | memory/dma | 74 | `RegisterMove` | register |
| 20 | `Pool` | compute | 75 | `TensorLoad` | memory |
| 21 | `Reciprocal` | compute | 76 | `TensorSave` | memory |
| 22 | `Save` | memory/dma | 77 | `Terminator` | control |
| 23 | `TensorCopy` | copy | 78 | `CompareAndBranch` | control |
| 24 | `TensorCopyDynamicSrc` | copy | 79 | `UnconditionalBranch` | control |
| 25 | `TensorCopyDynamicDst` | copy | 80 | `BranchHint` | control |
| 26 | `IndirectCopy` | copy | 81 | `Return` | control |
| 27 | `TensorReduce` | compute | 82 | `Exit` | control |
| 28 | `TensorScalar` | tensorscalar | 83 | `Break` | control |
| 29 | `TensorScalarPtr` | tensorscalar | 84 | `Call` | control |
| 30 | `TensorScalarCache` | tensorscalar | 85 | `SwitchQueueInstance` | queue |
| 31 | `TensorTensor` | compute | 86 | `ResetQueueInstance` | queue |
| 32 | `DMACopy` | dma | 87 | `CoreBarrier` | sync |
| 33 | `GPSIMDSB2SB` | gpsimd | 88 | `Max` | reduce |
| 34 | `BNStats` | batchnorm | 89 | `MaxIndex` | reduce |
| 35 | `BNStatsAggregate` | batchnorm | 90 | `MatchReplace` | reduce |
| 36 | `BNGradients` | batchnorm | 91 | `MaxIndexAndMatchReplace` | reduce |
| 37 | `BNBackprop` | batchnorm | 92 | `Gather` | memory |
| 38 | `BNBackprop2` | batchnorm | 93 | `InlineASMBytes` | escape |
| 39 | `StreamShuffle` | shuffle | 94 | `TensorCompletion` | misc |
| 40 | `StreamTranspose` | shuffle | 95 | `MatmultMx` | matmul |
| 41 | `ReadVarAddr` | register | 96 | `QuantizeMx` | compute |
| 42 | `GenericIndirectLoad` | indirect | 97 | `Rand2` | rng |
| 43 | `IndirectLoad` | indirect | 98 | `RandSetState` | rng |
| 44 | `GenericIndirectSave` | indirect | 99 | `RandGetState` | rng |
| 45 | `IndirectSave` | indirect | 100 | `Rng` | rng |
| 46 | `IndirectSaveAccumulate` | indirect | 101 | `ActivationReadAccumulator` | compute |
| 47 | `Collective` | collective | 102 | `DveReadAccumulator` | compute |
| 48 | `CollectiveCompute` | collective | 103 | `Exponential` | compute |
| 49 | `CollectiveSend` | collective | 104 | `NonzeroWithCount` | compute |
| 50 | `CollectiveRecv` | collective | 105 | `Loop` | region |
| 51 | `Select` | compute | 106 | `DynamicForLoop` | region |
| 52 | `CopyPredicated` | copy | 107 | `DMABlock` | region |
| 53 | `CustomOp` | custom | 108 | `DoWhile` | region |
| 54 | `BIRKernel` | kernel | 109 | `TongaReduceMacroSymbolic` | reduce |

> **NOTE —** the `default` arm of `InstructionType2string` does not return a fallback string; it calls `sub_679F20("Unknown opcode", "…/Instruction.cpp", 79)` — an `assert`/`report_fatal`-style abort. The compiler treats an out-of-range ordinal as a corrupt-IR bug, not a soft error. The legal range is therefore *closed* at `[0, 109]`; there is no reserved tail.

> **QUIRK —** several spellings in this enum never reach the Python `birpy` reflection layer. The Cython `birpy/Opcodes` module exposes only ~64 of these names as an `enum.Enum`; the internal control/sync/region opcodes (`NoOp`, `Drain`, `Halt`, `Terminator`, `CompareAndBranch`, `Loop`, `DoWhile`, `DMADescriptor*`, …) exist only on the C++ side. Reading the ordinal table off the Python module alone *undercounts* the enum by ~46 members — this is the trap that makes "how many opcodes are there?" easy to get wrong. The C++ `InstructionType2string` switch is the only complete source.

### The opcode field

Every classifier on this page begins by reading the same field. In `bir::InstructionType2string`'s callers, in `sameInst`, and in every `isXxx`, the opcode is `*((_DWORD *)this + 22)` — the 22nd 32-bit word, i.e. byte offset **`0x58`** in the `bir::Instruction` object. It is a `uint32_t` holding the raw `InstructionType` ordinal.

```c
// The discriminator read shared by every classifier in libBIR.
static inline InstructionType getOpcode(const Instruction *I) {
    return (InstructionType)(*(const uint32_t *)((const char *)I + 0x58));
}
```

> **GOTCHA —** the LLVM-RTTI `isa<bir::InstXxx>(I)` machinery is built on this one field, so a stale or mis-set opcode silently corrupts every downstream `dyn_cast`. The decompiled predicates show the cost: each one opens with an `assert(Val && "isa<> used on a null pointer")` (from `llvm/Support/Casting.h`) before touching `+0x58`, because the whole classification scheme trusts that field absolutely.

---

## `sameInst` — structural equality and the family ranges

`bir::Instruction::sameInst(Instruction* other)` (`0x2db7b0`, 8119 bytes) answers "is `other` operationally identical to `this`?" — the predicate a CSE/de-duplication pass needs. It is **not** pointer or address equality; it is a deep, per-opcode field comparison. The function is one giant `switch (getOpcode(this))` whose 110 arms each (a) gate on `getOpcode(other)` and (b) compare the operands and attributes that matter for *that* opcode. Sixteen opcodes whose comparison is too large to inline are delegated to a virtual override `bir::InstXxx::sameInst`.

### The dispatch shape

```c
// bir::Instruction::sameInst @ 0x2db7b0 — abridged dispatch skeleton.
bool Instruction::sameInst(Instruction *other) {
  switch (getOpcode(this)) {

    case 0: // Generic
      // family gate: ANY of {Generic, GenericCopy, GenericRelu, AbstractCopy}
      return getOpcode(other) <= 3;          // ordinals 0..3 == the "generic" family

    case 1: // GenericCopy
      if (getOpcode(other) != 1) return false;
      // (variant-access guards elided) compare the 1-byte mode at +240
      return byte_at(other, 240) == byte_at(this, 240);

    case 2: // GenericRelu
      return getOpcode(other) == 2;          // opcode-only; no payload to compare

    case 3: // AbstractCopy
      return getOpcode(other) == 3 &&
             dword_at(this, 0xF0) == dword_at(other, 0xF0);   // field[+60]

    case 4:  return InstActivation::sameInst(this, other);    // delegated override
    case 8:  // fallthrough into MatmultBase override family
    case 7:  return InstMatmultBase::sameInst(this, other);
    /* … 110 arms total … */

    default: /* unreachable; opcode is in [0,109] */;
  }
}
```

The `case 0` arm is the clearest "family mask" in the whole file: a `Generic` instruction is `sameInst` to *any* instruction whose opcode is `≤ 3`. That half-open range `{0,1,2,3}` **is** the generic family, encoded as a single comparison rather than a bitmask. Other arms use exact-opcode gates plus payload comparison.

### The 16 delegated overrides

When the per-opcode comparison is large, the base switch tail-calls a class method. These exist at the following sites; each one re-checks the opcode gate itself (so they are safe to call directly):

```text
InstActivation::sameInst              @ 0x2f3ff0   (opcode 4)
InstTensorScalar::sameInst            @ 0x2f41a0   (opcode 28)
InstTensorScalarPtr::sameInst         @ 0x2f42b0   (opcode 29)
InstTensorScalarCache::sameInst       @ 0x2f4480   (opcode 30)
InstTensorScalarAffineSelect::sameInst             (opcode 62)
InstDMACopy::sameInst                 @ 0x2f4570   (opcode 32)
InstMatmultBase::sameInst                          (opcodes 7/8/9/95)
InstRangeSelect::sameInst                          (opcode 63)
InstCoreBarrier::sameInst                          (opcode 87)
InstRand::sameInst                                 (opcode 60)
InstCustomOp::sameInst                             (opcode 53)
InstCollectiveCompute::sameInst                    (opcode 48)
InstGetCurProcessingRankID::sameInst               (opcode 66)
InstIota::sameInst                                 (opcode 61)
InstStreamShuffle::sameInst                        (opcode 39)
InstBIRKernel::sameInst                            (opcode 54)
```

### A worked override — `InstActivation::sameInst`

`InstActivation::sameInst` (`0x2f3ff0`) is the densest override and shows exactly what "same activation" means. After the opcode gate, it compares the activation function id, the two scale/bias floats, and a chain of optional attributes — bailing the moment any differs. The `std::__throw_bad_variant_access` calls are the guards on `std::variant`-typed operands (an unengaged variant is a hard error, not a mismatch).

```c
// bir::InstActivation::sameInst @ 0x2f3ff0 — what makes two Activations identical.
bool InstActivation::sameInst(Instruction *other) {
  if (getOpcode(other) != 4)                      // Activation
    return false;
  // activation func parameters: two floats at +0xF0 / +0xF4
  if (fp_at(this, 0xF0) != fp_at(other, 0xF0)) return false;
  if (fp_at(this, 0xF4) != fp_at(other, 0xF4)) return false;
  // bias-enable byte at +248 (variant operand at +280 must be engaged)
  if (byte_at(this, 248) != byte_at(other, 248)) return false;
  // scale operands: two dwords at +0x120 / +0x124
  if (dword_at(this, 0x120) != dword_at(other, 0x120)) return false;
  if (dword_at(this, 0x124) != dword_at(other, 0x124)) return false;
  // … further optional-attr chains at +296/+0x150, +344, +384/+0x1A8 …
  return byte_at(this, 384) == byte_at(other, 384)
      && dword_at(this, 0x1A8) == dword_at(other, 0x1A8);
}
```

> **NOTE —** `sameInst` is asymmetric in form but symmetric in intent. `case 0` (`Generic`) accepts a wider set than the reverse direction would, because `Generic` is the lowering-time placeholder that any of the three concrete generic ops can satisfy. A reimplementation that makes `sameInst` strictly symmetric will mis-handle the generic family; preserve the directional `≤ 3` gate.

---

## Family predicates — the real "masks"

There is no single per-opcode bit-vector "family register" in BIR. Instead, the families a pass actually queries are computed on demand by free functions `bir::isXxx(const Instruction*)`. They are the closest thing to the "sameInst family masks", and they use four distinct encodings. All read `getOpcode` at `+0x58` first.

### Encoding 1 — exact opcode, or opcode + sub-type field

The simplest predicates test one ordinal, or test a base opcode plus a discriminator sub-field. `DMACopy` (opcode 32) is the canonical multiplexed opcode: a single ordinal that fans out into transpose/replicate/copy by reading a sub-type dword at offset **`+0x128`** (the 74th dword).

```c
// bir::isTransposeDMA @ 0x30f0f0
bool isTransposeDMA(const Instruction *I) {
  int opc = getOpcode(I);
  if (opc == 32)  return dword_at(I, 0x128) == 1;   // DMACopy, subtype==1
  return opc == 71;                                  // or DMADescriptorTranspose
}

// bir::isReplicateDMA @ 0x30f190
bool isReplicateDMA(const Instruction *I) {
  int opc = getOpcode(I);
  if (opc == 32)  return dword_at(I, 0x128) == 3;   // DMACopy, subtype==3
  return opc == 72;                                  // or DMADescriptorReplicate
}

// bir::isCopyOrCastDMA @ 0x30f090
bool isCopyOrCastDMA(const Instruction *I) {
  int opc = getOpcode(I);
  if (opc == 22 || opc == 19) return true;          // Save or Load
  if (opc == 32) return dword_at(I, 0x128) == 0;    // DMACopy, subtype==0
  return false;
}
```

The `DMACopy` sub-type field thus encodes a 3-way family split (`0 = copy/cast`, `1 = transpose`, `3 = replicate`) under one opcode — a reimplementer must carry that field, not just the opcode, to classify DMAs.

`Collective` (opcode 48) is split the same way by a *granularity* dword at `+0xF8` (62nd dword): `> 6` is fine-grain, `≤ 6` is coarse-grain.

```c
// bir::isFineGrainCCOp  @ 0x30f7e0  /  isCoarseGrainCCOp @ 0x30f800
bool isFineGrainCCOp  (const Instruction *I){ return getOpcode(I)==48 && dword_at(I,0xF8) >  6; }
bool isCoarseGrainCCOp(const Instruction *I){ return getOpcode(I)==48 && dword_at(I,0xF8) <= 6; }
```

### Encoding 2 — shifted 64-bit inclusion mask

When a family is a sparse set of ordinals within a contiguous window, BIR uses a single 64-bit immediate shifted by `(opcode − base)`. `isRNGInstruction` (`0x30ebc0`) is the textbook case: window base 58, mask `0x38000000085`.

```c
// bir::isRNGInstruction @ 0x30ebc0
bool isRNGInstruction(const Instruction *I) {
  int opc = getOpcode(I);
  if (opc == 10) return dword_at(I, 0xF0) == 1;     // Memset variant flagged as RNG
  if (opc == 59) return true;                       // SetRandState (always)
  unsigned d = opc - 58;
  if (d > 0x29) return false;                        // outside [58, 99]
  return (0x38000000085ULL >> d) & 1;
}
```

Decoding `0x38000000085 >> (opc-58)` yields bits set at offsets `{0,2,7,39,40,41}`, i.e. opcodes **`{58 GetRandState, 60 Rand, 65 Dropout, 97 Rand2, 98 RandSetState, 99 RandGetState}`** — plus the two special cases `10 Memset(rng)` and `59 SetRandState`. So the full RNG family is `{10*, 58, 59, 60, 65, 97, 98, 99}`. (`*` = conditional on a sub-field.)

### Encoding 3 — shifted 64-bit *exclusion* mask

`isSRDMA` (`0x315620`) inverts the idiom: it accepts a window `[19, 19+0x35]` *except* the ordinals whose mask bit is set. The immediate `0xFFC1FFFFFFFFDFF6` is an exclusion mask.

```c
// bir::isSRDMA @ 0x315620 — accept opcode in window MINUS the masked-out bits,
// then refine on the source/dest tensor-kind discriminator.
bool isSRDMA(const Instruction *I) {
  unsigned d = getOpcode(I) - 19;
  if (d > 0x35) return false;
  if ((0xFFC1FFFFFFFFDFF6ULL >> d) & 1) return false;  // excluded ordinal
  // … then check source/dest TensorKind via getSrc()/getDst() at +56/+224 …
}
```

The bits that are *clear* select opcodes **`{19 Load, 22 Save, 32 DMACopy, 68 DMADescriptor, 69 DMADescriptorCopy, 70 DMADescriptorCCE, 71 DMADescriptorTranspose, 72 DMADescriptorReplicate}`** — the DMA-capable memory ops. `isSRDMA` further narrows by the SBUF-resident tensor kinds, and the leaf predicates compose:

```c
bool isSRCopyDMA(const Instruction *I){ return isSRDMA(I) && isCopyDMA(I); }   // @ 0x3156e0
bool isSRCastDMA(const Instruction *I){ return isSRDMA(I) && isCastDMA(I); }   // @ 0x315710
```

where `isCopyDMA`/`isCastDMA` (`0x315480`/`0x315550`) reuse `isCopyOrCastDMA` and then split on whether source and destination *dtypes* are equal (`copy`) or not (`cast`) — the dtype dword is at `+48` of the access pattern. `isIOCopyDMA`/`isIOCastDMA` (`0x3154c0`/`0x315590`) add a `TensorKind` input/output test on top.

### Encoding 4 — byte lookup table

When the membership set is dense but irregular, BIR uses a `.rodata` byte table indexed by `(opcode − base)`. `isAxiInstruction` (`0x30f1e0`) reads `byte_786480[opcode − 19]`:

```c
// bir::isAxiInstruction @ 0x30f1e0
bool isAxiInstruction(const Instruction *I) {
  int opc = getOpcode(I);
  if (opc == 18) return true;                                  // DMA
  unsigned d = opc - 19;
  if (d <= 0x30 && byte_786480[d]) return true;                // table lookup, window [19,67]
  // tail ranges: TensorLoad/TensorSave (75..76) and DMADescriptor* (68..72)
  return (unsigned)(opc - 75) <= 1 || (unsigned)(opc - 68) <= 4;
}
```

This mixes all three index idioms in one function: an exact test (`== 18`), a table lookup over `[19, 67]`, and two half-open tail ranges. It is the most general classifier in the file.

> **CORRECTION —** an earlier reading of this subsystem looked for a literal symbol named `sameInst` *family-mask function* and a single opcode→family bit register. Neither exists. `sameInst` is per-opcode structural equality (Encoding-free, field-by-field); the *families* are computed by the `isXxx` predicates above using four different encodings, and one opcode can belong to several families depending on a sub-type field (`DMACopy` is at once copy, transpose, or replicate). Any reimplementation that flattens families into a static opcode→family map will mis-classify the multiplexed opcodes.

---

## Reimplementation checklist

1. **Declare the enum verbatim**, ordinals 0–109, in the table order above. Treat any out-of-range value as a fatal IR-corruption assert, exactly as `InstructionType2string`'s default arm does.
2. **Serialize by name, never by ordinal.** Ordinals renumber on every enum edit; the printer and JSON path key on the spelling. (See [7.9 enum crosswalk](enum-crosswalk.md) for the name↔ordinal pinning for this build.)
3. **Store the opcode at a fixed offset** (`+0x58` here) and build all RTTI on it.
4. **Implement `sameInst` as a per-opcode switch** with optional class overrides; preserve the directional `Generic ≤ 3` family gate.
5. **Compute families on demand**, carrying the multiplexer sub-fields (`DMACopy.subtype` at `+0x128`, `Collective.granularity` at `+0xF8`) so one opcode can answer several family queries.

---

## Verification ceiling

The 110-count and the name table are **CONFIRMED at the strongest level**: they are read directly off the live switch body in `libBIR.so` (`0x2d5bf0`), the case count was independently re-counted (110 string-emit calls), and the out-of-range path names the source file and line. The opcode field offset (`+0x58`) is **CONFIRMED** — it is the identical read in `InstructionType2string`'s callers, `sameInst`, and every `isXxx`.

The `sameInst` dispatch shape and the family-predicate encodings are **CONFIRMED** from the decompiled bodies; the *exact field offsets* inside the larger overrides (e.g. the deep attribute chain in `InstActivation::sameInst`) are **STRONG** rather than certain, because Hex-Rays' struct recovery for `bir::Instruction` is partial and some offsets are reported as raw `+N` rather than named members — the relative comparison logic is exact, the absolute offsets should be re-confirmed against [7.1 Instruction base](instruction-base.md) before relying on a specific byte.

The "family" column in the opcode table is **editorial/INFERRED** — it is this page's grouping synthesized from the `sameInst` ranges and `isXxx` sets, not a field the binary stores. The decoded mask memberships (RNG `{58,59,60,65,97,98,99,10*}`, SRDMA `{19,22,32,68–72}`, generic `{0–3}`) are **CONFIRMED** by arithmetic on the literal immediates. The `byte_786480` AXI table contents were *not* dumped byte-for-byte here; `isAxiInstruction`'s window and tail ranges are confirmed, but the precise set of opcodes the table admits within `[19, 67]` is **INFERRED** pending a rodata dump and should be treated as such.
