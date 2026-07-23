# Codegen of DVE-Search, RNG, Fill, Control, Barrier, and Debug Ops

> *All symbols, addresses, and struct offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The codegen bodies live in `neuronxcc/starfish/lib/libwalrus.so` (`.text`/`.rodata` `VA == file offset`, base `0x62d660` / `0x1c72000`); the `bir::Inst*` templates and `InstBuilder` overloads they call resolve into `libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`), and the runtime numeric models into `libBIRSimulator.so`. The `nki_klr_sim` sibling links the identical `KlirToBirCodegen` statically (stripped). The cp311/cp312 wheels share the ABI but drift every VA. Provenance: static binary analysis, cross-checked against reports D-I11/I12/I13/I14/I17/I18, D-D01, D-D02, D-D08, D-E14, D-F08/F10/F12, D-H35.*

## Abstract

This page documents the long tail of `neuronxcc::backend::KlirToBirCodegen` — the per-op KLR→BIR leaves that do **not** lower to a tensor compute op (matmul, activation, reduce). It covers five families that share a frontend (the NKI `nisa.*` / `nl.*` intrinsics) and a dispatch core ([the master routing switch](klir-codegen-dispatch.md), 7.21) but emit very different BIR: the **DVE search** primitives behind TopK / argmax / MoE-routing (`Max8`, `FindIndex8`, `MatchReplace8`, `NonzeroWithCount`); the **RNG** family (`Rng`, `Rand2`, `SetRngSeed`, `RandGetState`, `RandSetState`); the **fill / init / regularization** ops (`MemSet`, `Iota`, `Dropout`); the **scalar-control / predication** ops (`CopyPredicated`, `CmpBranch`, `RegisterAluOp`, `RegisterMove`); the **SPMD** primitives (`CoreBarrier`, `RankId`, `CurrentProcessingRankId`); and the **debug / driver** path (`DevicePrint`, `SequenceBounds`, `LncKernel`, `Immediate`).

The unifying frame is the one a reimplementer already owns from the compute leaves: each leaf resolves its `klr::TensorRef` operands through `codegenTensorRef` into `{MemoryLocation*, SmallVector<APPair,4>, base-idx, optional<Dtype>}` bundles, mints a `bir::Instruction` (either via an `InstBuilder::addXxx` convenience overload or via `insertElement<Inst>`), binds operands to the instruction's input/output lists, stamps a small number of op-specific scalar fields, and registers the instruction with `sub_F20EC0` (the codegen book-keeping hook). What makes this family worth its own page is the set of *non-obvious lowerings* — KLR node names that do **not** map 1:1 to the BIR opcode they emit. The headline is the **Rng→Memset remap**: `klr::Rng` lowers to `bir::InstMemset` (IT 10) with `MemsetMode = Random`, never to an `InstRng`. There are four more: `klr::SetRngSeed`→`InstSetRandState`; `klr::MatchReplace8`→ either `InstMatchReplace` **or** the fused `InstMaxIndexAndMatchReplace` selected by an operand-presence flag; the register/control ops stamped on `EngineType::ALL(7)` (not `SP`); and `klr::LncKernel`, which is not an operator at all but the per-core kernel-body **driver**.

For reimplementation, the contract is:

- **The non-1:1 opcode map.** Five KLR-name→BIR-opcode remaps a name-driven reimplementation will get wrong, each anchored to the `insertElement<Inst>` / `InstBuilder::add*` call site.
- **The two operand models.** Tensor leaves use `MemoryLocation + AccessPattern` ([the AP encoder](codegen-accesspattern.md), 7.22); the scalar-control leaves use the [`bir::Register` / `RegisterAccess` / `ImmediateValue` value model](value-model.md) (7.4) — input list at `Instruction+0xA0`, output list at `+0xC0`, `Dtype int32 = 15`.
- **What codegen sets vs. what later passes number.** The CoreBarrier index and HW barrier-register id are *not* set here; the dropout `1/(1−p)` scale is *not* applied here. The leaf binds structure; downstream passes (`renumberCoreBarriers`, `lower_branch`, the simulator) finish the semantics.

| | |
|---|---|
| **Codegen class** | `neuronxcc::backend::KlirToBirCodegen` (TU `klir_to_bir_codegen.cpp`) |
| **Dispatch in** | `codegenStmt` → `codegenStmtOperWrapper` → `codegenOperator<Op>Wrapper` → leaf (7.21) |
| **Operand resolver (tensor)** | `codegenTensorRef` `0xf18b30` → `{MemLoc*, SmallVector<APPair,4>, base-idx, opt<Dtype>}` |
| **Operand resolver (scalar)** | `codegenImmediate` `0xf16980` / `addOperandToInst` `0xf1be80` (7.4) |
| **Engine remap** | `codegenEngine` `0xf140f0` → table `dword_1DE9740 = {2,4,5,3,1,6}` |
| **Book-keeping hook** | `sub_F20EC0` (append inst to codegen list; `this+0xE0`/`this+0xDB?` rooted) |
| **IR level** | BIR `bir::Instruction` (post-KLR, pre-engine-split / pre-SASS) |

---

## The non-1:1 opcode map

Before the per-family detail, the single most decisive fact for a reimplementer: **KLR op name does not predict BIR opcode.** A code generator built by switching on the KLR tag and emitting a same-named BIR instruction will be wrong in five places. All five are anchored to the leaf's `insertElement<Inst>` / `InstBuilder::add*` call site (`@plt` symbol read verbatim from the disassembly).

| KLR node | Leaf @ VA | BIR opcode emitted | IT | Why it surprises |
|---|---|---|---|---|
| `klr::Rng` | `codegenRng` `0xf245c0` | **`bir::InstMemset`** | 10 | a "random fill" *is* a Memset variant (`MemsetMode=Random`), there is no `InstRng` on this path |
| `klr::SetRngSeed` | `codegenSetRngSeed` `0xf24860` | **`bir::InstSetRandState`** | 59 | the gen-1 seed-install op, not a `SetRngSeed` opcode |
| `klr::MatchReplace8` | `codegenMatchReplace8` `0xf29e10` | `InstMatchReplace` **or** `InstMaxIndexAndMatchReplace` | 90 / 91 | one KLR kind → two BIR opcodes, selected by an operand-presence byte |
| `klr::RegisterAluOp` / `klr::RegisterMove` | `0xf25f10` / `0xf26370` | `InstRegisterAlu` / `InstRegisterMove` | 73 / 74 | stamped on `EngineType::ALL(7)`, **not** `SP(6)` |
| `klr::LncKernel` | `codegenLncKernel` `0xf34140` | *(no single Inst — a driver)* | — | not a `klr::Operator`; it is the per-core stmt-list walk driver |

> **QUIRK — the KLR RNG path never emits `InstRand(60)` or `InstRng(100)`.** Those two opcodes (the ones carrying `RandomAlgorithmKind` / `RandomDistributionKind` / params) are produced by a *different* frontend (direct BIR-JSON / HLO). A full scan of the codegen roster (`nm -DC`) finds no `insertElement<InstRand>` and no `insertElement<InstRng>`. So the algorithm/distribution enums are **irrelevant** to KLR-originated randomness: KLR rides `Memset(Random)` + `Rand2` (XORWOW) and the host MT19937-64 instead. A reimplementer wiring the gen-1 `InstRand` machinery into the NKI path is implementing dead code.

The remaining leaves *are* 1:1 (`Max8`→`InstMax`, `FindIndex8`→`InstMaxIndex`, `NonzeroWithCount`→`InstNonzeroWithCount`, `MemSet`→`InstMemset`, `Iota`→`InstIota`, `Dropout`→`InstDropout`, `CopyPredicated`→`InstCopyPredicated`, `CmpBranch`→`InstCompareAndBranch`/`InstUnconditionalBranch`, `CoreBarrier`→`InstCoreBarrier`, `RankId`→`InstGetGlobalRankId`, `CurrentProcessingRankId`→`InstGetCurProcessingRankID`, `DevicePrint`→`InstDevicePrint`, `SequenceBounds`→`InstGetSequenceBounds`). All IT values are CERTAIN from [the InstructionType enum](instruction-type.md) (7.3) and re-confirmed at the producer side here.

---

## DVE Search Family — Max8 / FindIndex8 / MatchReplace8 / NonzeroWithCount

### Purpose

These four leaves lower the DVE (Vector engine, `EngineType=5`) reduction/index primitives that implement TopK, argmax, and MoE-router compaction. They are the BIR producers for the NKI surface ops `nisa.max8`, `nc_find_index8`, `nc_match_replace8` / `nc_match_replace_indices8`, and `find_nonzero_indices_with_count`. The wire-level DVE microcode (`Max-8` / `Find-Index-8` / `Match-Value-Load`) and the bit-fields are owned by [DVE Search & Datamove Encoding](../isa/dve-search-encoding.md) (2.16); this section is the *producer*. The simulator's numeric model is the beta3 counterpart in [BirCodeGenLoop Compute Codegens](../nki/bircodegen-compute.md) (6.5.11) / [Scan / Reduce / Top-K Primitives](../nki/scan-reduce-topk.md).

### Entry Point

```text
codegenStmt → codegenStmtOperWrapper → codegenOperator<Op>Wrapper (unwrap+refcount) → leaf
  codegenOperatorMax8Wrapper             0xf1cc10 ── codegenMax8              0xf1c970
  codegenOperatorFindIndex8Wrapper       0xf1d050 ── codegenFindIndex8        0xf1cc80
  codegenOperatorMatchReplace8Wrapper    0xf2a520 ── codegenMatchReplace8     0xf29e10
  codegenOperatorNonzeroWithCntWrapper   0xf2cd70 ── codegenNonzeroWithCount  0xf2c8f0
```

### Algorithm

`Max8` and `FindIndex8` are the simple shape: resolve operands, call the `InstBuilder` convenience overload, return. The reduce-axis and partition count come *entirely* from the operand APs — the "8" is the silicon DVE lane width, not an attribute.

```c
// codegenMax8  @0xf1c970   klr::Max8 → bir::InstMax (IT88), DVE
// klr::Max8 = { TensorRef data, TensorRef maxVals(out), opt<Dtype> }  (Max8_ser tag 173, 3 fields)
bir::Instruction* codegenMax8(this, shared_ptr<klr::Max8> node) {
  InstArg dataAP   = codegenTensorRef(node->op0);          // 0xf1c9cb  the row to reduce
  InstArg maxValAP = codegenTensorRef(node->op1);          // 0xf1ca21  8 fp32 out per partition
  // each AP: dynId != 0 → sub_F12B40 dynamic-AP splice (0xf1cbc0/…dd); else static AP
  return InstBuilder::addMax(dataAP, maxValAP, src_loc);   // 0xf1cb1f  IT88, 2 operands
}

// codegenFindIndex8  @0xf1cc80   klr::FindIndex8 → bir::InstMaxIndex (IT89), DVE
// klr::FindIndex8 = { TensorRef maxVals, TensorRef data, TensorRef indices(out), opt<Dtype> }
bir::Instruction* codegenFindIndex8(this, shared_ptr<klr::FindIndex8> node) {
  InstArg maxValAP = codegenTensorRef(node->op0);          // 0xf1ccdd  the 8 values to locate
  InstArg dataAP   = codegenTensorRef(node->op1);          // 0xf1cd33  full input row
  InstArg idxAP    = codegenTensorRef(node->op2);          // 0xf1cd89  8 × uint32 argmax out
  return InstBuilder::addMaxIndex(maxValAP, dataAP, idxAP, src_loc);  // 0xf1cee4  IT89, 3 operands
}
```

`MatchReplace8` is the one place in this whole family where a single KLR kind collapses onto two BIR opcodes — the same idiom seen in the TensorScalar family ([the enum crosswalk](codegen-enum-crosswalk.md), 7.20). The selector is the engaged-byte of an `Option<TensorRef>` index output, read at `klr+0x50`.

```c
// codegenMatchReplace8  @0xf29e10   klr::MatchReplace8 → IT90 OR IT91, DVE
// klr::MatchReplace8 = { TensorRef maxVals, TensorRef data, TensorRef replaced(out),
//                        Immediate replace, Option<TensorRef> indexOut@+64, opt<Dtype> }
//                        (MatchReplace8_ser tag 172, 6 fields)
bir::Instruction* codegenMatchReplace8(this, shared_ptr<klr::MatchReplace8> node) {
  InstArg maxValAP   = codegenTensorRef(node->op0);        // 0xf29e77
  InstArg dataAP     = codegenTensorRef(node->op1);        // 0xf29ecd
  InstArg replacedAP = codegenTensorRef(node->op2);        // 0xf29f23
  variant<float,int> replace = codegenImmediate(node->op3);// 0xf29f72
  assert(holds_alternative<float>(replace) &&              // klir_to_bir_codegen.cpp:1454
         "The replace value for MatchReplace8 must be a float");
  float sentinel = get<float>(replace);

  if (*(uint8_t*)(node + 0x50) == 0) {                     // 0xf29fcb  cmpb $0,0x50(%rsi)
    // index output ABSENT → plain strike-out
    return InstBuilder::addMatchReplace(maxValAP, dataAP,  // 0xf2a0dc  IT90 InstMatchReplace
               replacedAP, sentinel, src_loc);            //           sentinel bound inside builder
  } else {                                                 // 0xf29fd7  jnz → IT91 path
    InstArg idxAP = codegenTensorRef(node->indexOut);      // 0xf2a1f5  the optional 4th operand
    name = returnAndIncrementId() + "MaxIndexAndMatchReplace-";        // 0xf2a370
    inst = InstBuilder::addInstruction<bir::InstMaxIndexAndMatchReplace>(  // 0xf2a3ad  IT91
               name,
               /*inputs */ {maxValAP, dataAP},             //   count 2
               /*imms   */ {sentinel},                     //   count 1
               /*outputs*/ {replacedAP, idxAP});           //   count 2  (fused strike + argmax)
    *(float*)(inst + 0xF0) = sentinel;                     // 0xf2a46b  movss %xmm7,0xf0(%r12)
    return inst;                                           //   inst+0xF0 = the "imm_value" json key
  }
}
```

`NonzeroWithCount` is the lone member with header-immediate operands and the only one that uses the lower-level `addArgument`/`addOutput`/`ImmediateValue` idiom directly:

```c
// codegenNonzeroWithCount  @0xf2c8f0   klr::NonzeroWithCount → bir::InstNonzeroWithCount (IT104), Pool gen3+
// klr = { TensorRef out, TensorRef data, Immediate indexOffset(int32), Immediate paddingVal(int32) }
bir::Instruction* codegenNonzeroWithCount(this, shared_ptr<klr::NonzeroWithCount> node) {
  InstArg outAP  = codegenTensorRef(node->op0);            // 0xf2c950  OUTPUT (compaction buffer)
  InstArg dataAP = codegenTensorRef(node->op1);            // 0xf2c9a3  INPUT data
  variant<float,int> indexOffset = codegenImmediate(node->op2);  // 0xf2c9eb
  variant<float,int> paddingVal  = codegenImmediate(node->op3);  // 0xf2ca43
  inst = insertElement<bir::InstNonzeroWithCount>("NonzeroWithCount-" + id);  // 0xf2ca96
  inst->addArgument<PhysicalAccessPattern>(dataAP);        // 0xf2cb19  input data
  if (indexOffset.tag != 1) throw runtime_error("indexOffset must be int32");
  ImmediateValue(inst, get<int>(indexOffset));             // 0xf2cb57  int32 imm → input list
  if (paddingVal.tag != 1)  throw runtime_error("paddingVal must be int32");
  ImmediateValue(inst, get<int>(paddingVal));              // 0xf2cbbb  int32 imm (= -1 pad)
  inst->addOutput<PhysicalAccessPattern>(outAP);           // 0xf2cc24  compaction output
  sub_F20EC0(this, inst);  return inst;
}
```

### Function Map

| Function | @ VA | Role | Confidence |
|---|---|---|---|
| `codegenMax8` | `0xf1c970` | `klr::Max8` → `InstMax(88)` via `addMax` (2 op) | CERTAIN |
| `codegenFindIndex8` | `0xf1cc80` | `klr::FindIndex8` → `InstMaxIndex(89)` via `addMaxIndex` (3 op) | CERTAIN |
| `codegenMatchReplace8` | `0xf29e10` | `klr::MatchReplace8` → `InstMatchReplace(90)` / `InstMaxIndexAndMatchReplace(91)` | CERTAIN |
| `codegenNonzeroWithCount` | `0xf2c8f0` | `klr::NonzeroWithCount` → `InstNonzeroWithCount(104)` (2 int32 imms) | CERTAIN |
| `InstBuilder::addInstruction<InstMaxIndexAndMatchReplace>` | `0xf43150` | the IT91 allocator (defined-local; 104-B InstArg stride, 480-B AP) | CERTAIN |

### Considerations

The "8" is silicon, not an attribute: `InstMax` is header-only (zero op-keys); the simulator hard-codes 8 outputs per partition and the verifier asserts `numOutPerPartition == 8`, `8 ≤ numInPerPartition ≤ 16384`. The descending sort is a strict-`>` insertion (stable earliest-index tie-break); `FindIndex8` presets each index slot to `0xFFFFFFFF` so a maxVal absent in the data (or NaN) yields index `-1`. The TopK recipe is iterative: `Max8` → `FindIndex8` → `MatchReplace8(sentinel=-inf)` strikes the top-8 so the next `Max8` round yields ranks 9-16; the fused IT91 collapses the index-extract and strike-out into one DVE pass (`nc_match_replace_indices8`), and is the per-round workhorse.

> **GOTCHA — `NonzeroWithCount` is compiler-lowered but simulator-stubbed.** `birverifier::visitInstNonzeroWithCount` (`0xfb2140`) dispatches to a *real* Pool-engine ISA emitter (`CoreV3GenImpl::runISACheckInstNonzeroWithCount` `0x13463c0`, `CoreV4` `0x14325c0`), gen3+ only (`checkArchLevel(0x1E)`). The BIR *simulator* lacks a kernel for it (stubbed at base `visitInstruction`). Reading "the sim has no kernel" as "the op is unsupported" is wrong — the compiler emits hardware for it; only numerical replay is missing.

---

## RNG Family — Rng / Rand2 / SetRngSeed / RandGetState / RandSetState

### Purpose

The five KLR RNG ops install, advance, and checkpoint the per-partition PRNG state. The decisive fact is the remap table in [the non-1:1 opcode map](#the-non-11-opcode-map): the *names* do not match the *opcodes*. The wire-level RNG bit-fields are owned by [RNG-Family Encoding](../isa/rng-encoding.md) (2.18); the simulator's MT19937-64 / XORWOW kernels by [BirCodeGenLoop Compute Codegens](../nki/bircodegen-compute.md) (6.5.11).

### Entry Point

```text
codegenOperatorRngWrapper          0xf247f0 ── codegenRng           0xf245c0 → InstMemset(10) "Rng-"
codegenOperatorRand2Wrapper        0xf252b0 ── codegenRand2         0xf24ff0 → InstRand2(97) "Rand2-"
codegenOperatorSetRngSeedWrapper   0xf24a60 ── codegenSetRngSeed    0xf24860 → InstSetRandState(59) "SetRandState-"
codegenOperatorRandGetStateWrapper 0xf24f80 ── codegenRandGetState  0xf24d60 → InstRandGetState(99) "RandGetState-"
codegenOperatorRandSetStateWrapper 0xf24cf0 ── codegenRandSetState  0xf24ad0 → InstRandSetState(98) "RandSetState-"
```

### Algorithm

```c
// codegenRng  @0xf245c0   klr::Rng{dst,engine} → bir::InstMemset(IT10), MemsetMode=Random
bir::Instruction* codegenRng(this, shared_ptr<klr::Rng> node) {
  name = returnAndIncrementId() + "Rng-";                 // 0xf245fd
  inst = insertElement<bir::InstMemset>(curBlock, name);  // 0xf24650  ★ emits a Memset, not an Rng
  sub_F20EC0(this, inst);                                  // 0xf24696  book-keeping
  InstArg dstAP = codegenTensorRef(node->dst);            // 0xf246d0  bind dst as OUTPUT
  inst->addOutput<PhysicalAccessPattern>(dstAP);          // 0xf2471e
  *(uint32_t*)(inst + 0xF0) = 1;                           // 0xf24731  movl $0x1,0xf0(%r13) → MemsetMode=Random
  *(uint32_t*)(inst + 0x90) = codegenEngine(node->engine);// 0xf2474a  mov %eax,0x90(%r13) → EngineType
  return inst;
}
```

The other four are structurally simple binds. `Rand2` carries two scalar operands (min, max) and a dst output, **no** engine/mode field — the bounded XORWOW uniform `[min,max)`. `SetRngSeed` binds its seed as an `addArgument` *input* and writes **no** engine (`EngineType` stays Unassigned). The gen-2 checkpoint pair is the asymmetry that defines the contract:

```c
// codegenRandGetState  @0xf24d60   → InstRandGetState(99)   state OUT  (snapshot/save)
inst = insertElement<bir::InstRandGetState>("RandGetState-" + id);
inst->addOutput<PhysicalAccessPattern>(codegenTensorRef(node->dst));   // state → dst tensor
*(uint32_t*)(inst + 0x90) = codegenEngine(node->engine);               // +0x90 = EngineType

// codegenRandSetState  @0xf24ad0   → InstRandSetState(98)   state IN   (restore/replay)
inst = insertElement<bir::InstRandSetState>("RandSetState-" + id);
inst->addArgument<PhysicalAccessPattern>(codegenTensorRef(node->src)); // state ← src tensor
*(uint32_t*)(inst + 0x90) = codegenEngine(node->engine);
```

### Function Map

| Function | @ VA | Emits (IT) | Engine field | Confidence |
|---|---|---|---|---|
| `codegenRng` | `0xf245c0` | `InstMemset(10)`, `MemsetMode=Random` | `codegenEngine` → `+0x90` | CERTAIN |
| `codegenRand2` | `0xf24ff0` | `InstRand2(97)` {min,max,dst} | *(none — N/A)* | CERTAIN |
| `codegenSetRngSeed` | `0xf24860` | `InstSetRandState(59)`, seed=`addArgument` | *(none — Unassigned)* | CERTAIN |
| `codegenRandGetState` | `0xf24d60` | `InstRandGetState(99)`, state=`addOutput` | `+0x90` | CERTAIN |
| `codegenRandSetState` | `0xf24ad0` | `InstRandSetState(98)`, state=`addArgument` | `+0x90` | CERTAIN |
| `codegenEngine` | `0xf140f0` | `klr::Engine` → `EngineType` via `{2,4,5,3,1,6}` | — | CERTAIN |

### Considerations

`codegenEngine(e)` is a 6-entry table lookup `i = e-2; return i<=5 ? dword_1DE9740[i] : 0`, with `dword_1DE9740 = {2,4,5,3,1,6}` (byte-verified at `0x1de9740`). The `klr::Engine` enum `{act=2, dma=3, dve=4, pe=5, pool=6, sp=7}` maps to `bir::EngineType {Activation=2, DMA=4, DVE=5, PE=3, Pool=1, SP=6}`; out-of-range / unassigned → `0`. This is the *same* table the compute leaves use.

> **NOTE — the engine field is the simulator's generator discriminator.** `visitInstMemset` (`0x1d5060`) reads `mode = inst+0xF0`, `eng = inst+0x90`. For a `Random` Memset (`mode==1`): `eng==1`(Pool) → GEN-2 XORWOW step; else → host MT19937-64 (`((x>>43)^x)+w`, the 312-word `mersenne_twister_engine<u64,…,0xB5026F5AA96619E9,…>`). So the `codegenEngine` value `codegenRng` stamps decides which RNG the "random fill" uses. Same seed ⇒ same stream ⇒ deterministic replay — the compiler enforces seed→state→advance by emitting `SetRngSeed` (install) before `Rng`/`Rand2` (advance), with `RandGetState`/`RandSetState` for checkpoint/replay.

The state tensor of all three of `SetRngSeed`/`RandGetState`/`RandSetState` lives in `MemoryType RNGSTATE = 0x40` (uint32; 6 u32/partition gen-1, 24 u32/partition gen-2 XORWOW). That `MemoryType` is set by the tensor's storage class **upstream**, not stamped numerically by these leaves — they carry only the AP + optional `Dtype`. The `RNGSTATE` identification comes from the sim-side assertions; it is not byte-stamped in these leaves.

---

## Fill / Init / Regularization — MemSet / Iota / Dropout

### Purpose

The fill and initialization leaves. `MemSet` is the constant-fill (and the const-prop folding target); `Iota` materializes a per-element ramp; `Dropout` applies a Bernoulli keep-mask. `Dropout` is the linkage point to the RNG family — its mask shares the per-partition PRNG state seeded by `SetRngSeed`. (These bodies were transcribed from the `nki_klr_sim` build, where the same TU is statically linked; the VAs below are the `nki_klr_sim` frame and differ from the `libwalrus` RNG-family VAs above — see the [two-VA-frame note](codegen-sub-encoders.md).)

### Entry Point

```text
codegenOperatorMemSetWrapper  sub_4F80C0 ── codegenMemSet   sub_4F7D50 → InstMemset(10) "memset-"
                              (self-unwrap) ── codegenIota    sub_4FB0B0 → InstIota(61)   (cpp:1474)
codegenOperatorDropoutWrapper sub_5060B0 ── codegenDropout  sub_505D20 → InstDropout(65) "Dropout-"
```

### Algorithm

```c
// codegenMemSet  sub_4F7D50   klr::MemSet{dest,fill,_,dtype} → bir::InstMemset(IT10), mode=Const
bir::Instruction* codegenMemSet(bb, klr::MemSet* node) {
  MemLoc dest = toBirMemoryLocation(node->dest);          // sub_4F6B20  slot 0
  variant<float,int> fill = codegenImmediate(node->fill); // sub_4F4970  slot 1 (kind 4=float / 3=int)
  inst = InstBuilder::addMemset(name, &dest, destAP,      // thunk 0x44e320 → libBIR
             numElem, fillBits, src_loc);                 //   MemsetMode defaults to 0=Const inside
  *(uint32_t*)(inst + 144) = dtypeRemap(node->dtype);     // sub_4F20E0; remap via dword_8AE4E0
  return inst;                                            //   mode@+0xF0=Const, fill@+0xF8, dtype@+144
}

// codegenIota  sub_4FB0B0   klr::Iota{dest, AP-pattern, start} → bir::InstIota(IT61)
bir::Instruction* codegenIota(bb, klr::Iota* node) {
  MemLoc dest = toBirMemoryLocation(node->dest);          // slot 0
  SmallVector<APPair,4> ramp = buildAPPairs(node->pattern);// slot+16: (step,num) per dim, axis [W,Z,Y,X]
  uint32_t start = *(uint32_t*)node->pattern;             // first ramp value
  inst = InstBuilder::addIota(&dest, ramp, start, STEP,   // thunk 0x44c640 → libBIR
             PATTERN, secondaryAP, src_loc);              //   value = start + step·index
  return inst;                                            //   base@+0xF0(240), AP-array@+280[len+288]
}
```

```c
// codegenDropout  sub_505D20   klr::Dropout{dst,in,mode,prob,seed} → bir::InstDropout(IT65)
bir::Instruction* codegenDropout(bb, klr::Dropout* node) {
  MemLoc out = toBirMemoryLocation(node->dst);            // slot 0
  MemLoc in  = toBirMemoryLocation(node->in);             // slot 1 (activations being dropped)
  inst = insertElement<bir::InstDropout>(bb, "Dropout-" + id);   // 0x520980
  bool keep = (*(uint32_t*)(node + 8) == 2);              // KLR mode dword == 2
  setVariantBoolFlag(inst + 240, keep);                   // variant<bool,QuasiAffineExpr>@+240 (train/eval)
  attachOutputAP(inst, out.ap, RATE_double);             // sub_515C40 → out list @+192 (p threaded as double)
  attachInputAP (inst, in.ap);                            // sub_515D60 → in  list @+160
  codegenOperand(bb, node->seed, inst);                   // sub_4F9E70  slot 5 = RNG state / seed
  //   seed variant: tag0 → ImmediateValue setType(16) uint seed; tag1 → setType(15) int;
  //                 tag2 → MemoryLocation tile (the RNG-state handle) attached as input
  registerInst(bb, inst);  return inst;
}
```

### Function Map

| Function | @ VA (`nki_klr_sim`) | Emits (IT) | Key fields | Confidence |
|---|---|---|---|---|
| `codegenMemSet` | `sub_4F7D50` | `InstMemset(10)`, `mode=Const` | mode`+0xF0`, fill`+0xF8`, dtype`+144` | CERTAIN |
| `codegenIota` | `sub_4FB0B0` | `InstIota(61)` (cpp:1474) | base`+0xF0`, AP-array`+280` | CERTAIN |
| `codegenDropout` | `sub_505D20` | `InstDropout(65)` | keep-flag`+240`, out`+192`/in`+160` | CERTAIN |
| `dtype remap` | `dword_8AE4E0` | `klr[k-2]` → `{2,4,5,3,1,6}` | written to `InstMemset+144` | CERTAIN |

### Considerations

`codegenMemSet` always emits `MemsetMode=Const` (the default inside `addMemset`); the `Random` mode is reached only via `codegenRng` above. `Const` is the sole mode that seeds the constant-propagation lattice — `const_propagate_for_memset0` (`0xbd5fe0`) and `replace_with_memset` (`0xbd3be0`) synthesize an `InstMemset(Const)` to fold an all-equal tensor. For `Iota`, the ramp axis is *implicit* in the stepped AP dim (the entry whose `step != 0`); there is no `AxisListType` field on `InstIota`. The sim and const-prop bounds pass both materialize `value(index) = base + Σ step_d · index_d`.

> **QUIRK — `codegenDropout` binds the rate, not the `1/(1−p)` scale.** The leaf threads the drop probability `p` (as a `double` into the output-AP attach) and the keep/training flag, and binds the RNG seed/state via slot 5. It emits **no** reciprocal or divide. The inverted-dropout `1/(1−p)` scale is applied by the *simulator* / downstream legalization at IT-65 visit time, not in this lowering. *(**[INFERRED]** — there is no divide in `sub_505D20`, and the sim applying `keep·(1/(1−p))` is consistent with that.)* The Bernoulli mask itself is drawn by the `InstDropout` op at sim time (MT19937-64 temper), sharing the per-partition PRNG state with `Rand`/`Rand2`/`Memset(Random)` — so a dropout and a sibling `SetRngSeed`/`Rand2` reference the *same* deterministic stream, advanced in emission order.

---

## Scalar-Control / Predication — CopyPredicated / CmpBranch / RegisterAluOp / RegisterMove

### Purpose

The sequencer-control and predication primitives. Three of them (`CmpBranch`, `RegisterAluOp`, `RegisterMove`) operate the sequencer **register file** via the [`bir::Register` / `RegisterAccess` / `ImmediateValue` value model](value-model.md) (7.4) — *not* `AccessPattern + MemoryLocation`. `CopyPredicated` is the one data-path member (a masked tensor copy). All four reach their leaves through `codegenOperator<Op>Wrapper` from the master switch as KLR kinds 10 / 48 / 46 / 47. The branch/compare-op wire encoding is owned by [SP Sync / Branch / Control Encoding](../isa/sp-sync-encoding.md) (2.20); `codegenRegister` itself (the register-mint sub-encoder) is documented in [Codegen Sub-Encoders](codegen-sub-encoders.md) (7.24).

### Entry Point

```text
kind 10 → codegenOperatorCopyPredicatedWrapper 0xf1ad60 ── codegenCopyPredicated 0xf1a9c0 → InstCopyPredicated(52)
kind 46 → codegenOperatorRegisterMoveWrapper   0xf26650 ── codegenRegisterMove   0xf26370 → InstRegisterMove(74)
kind 47 → codegenOperatorCmpBranchWrapper      0xf340d0 ── codegenCmpBranch      0xf33a70 → InstCompareAndBranch(78)/UnconditionalBranch(79)
kind 48 → codegenOperatorRegisterAluOpWrapper  0xf26300 ── codegenRegisterAluOp  0xf25f10 → InstRegisterAlu(73)
```

### Algorithm

`CopyPredicated` binds three tensors (dst, src, **mask**) and calls one static `InstBuilder` overload; the mask is the per-lane predicate.

```c
// codegenCopyPredicated  @0xf1a9c0   klr::CopyPredicated{dst,src,MASK,opt<Dtype>,bool} → InstCopyPredicated(52), DVE
bir::Instruction* codegenCopyPredicated(this, shared_ptr<klr::CopyPredicated> node) {
  auto dst  = codegenTensorRef(node->op0);                // 0xf1aa1f  +0
  auto src  = codegenTensorRef(node->op1);                // 0xf1aa71  +16
  auto mask = codegenTensorRef(node->op2);                // 0xf1aacd  +32  ◄ per-lane predicate
  return InstBuilder::addCopyPredicated(                  // 0xf1ac09  imported libBIR; mints IT52 internally
      dst.loc, src.loc, mask.loc, dst.ap, src.ap, mask.ap,
      dst.n, src.n, mask.n, src_loc);                     //   out ← preds ? src : out  (TRUE lanes only)
  //   useFalseFill (+0x118) is NOT set here — that is LowerSelect's job (the OTHER IT52 producer)
}
```

`CmpBranch` is the conditional branch. The `always` predicate (`klr::BrCmpOp == 1`) diverts to the unconditional path (which may *inline* a no-reorder target instead of branching); otherwise it builds an `InstCompareAndBranch`.

```c
// codegenCmpBranch  @0xf33a70   klr::CmpBranch → InstCompareAndBranch(78) / InstUnconditionalBranch(79)
// klr = { String lhs@0, String rhs@0x20, Int rhsImm@0x40, BrCmpOp@0x44, String trueBB@0x48, falseBB@0x68 }
bir::Instruction* codegenCmpBranch(this, shared_ptr<klr::CmpBranch> node) {
  if (*(int*)(node + 0x44) == 1) goto unconditional;      // 0xf33a87  "always" → IT79
  inst = insertElement<bir::InstCompareAndBranch>("CmpAndBranch-" + id);   // 0xf33ad0  IT78
  sub_F20EC0(this, inst);
  reg = Function::getRegisterByName(node->lhs);           // 0xf33b34  LHS always a register
  new RegisterAccess(inst, +0xA0) → setLocation(reg) → setType(15);        // int32 input
  if (!isBrRegRegOp(node->op)) {                          // 0xf14180  IMM (klr 2..7)
    new ImmediateValue(inst, kind 6, +0xA0); value = sext_i32(node->rhsImm);// rhs literal
  } else {                                                //          REG (klr 8..13)
    reg2 = Function::getRegisterByName(node->rhs);        // 0xf33d08  rhs register
    new RegisterAccess(inst, +0xA0) → setLocation(reg2) → setType(15);
  }
  trueBB  = Function::getBasicBlockByName(node->trueBB);  // 0xf33c68 → inst+0xF8 (on_true)
  falseBB = Function::getBasicBlockByName(node->falseBB); // 0xf33c85 → inst+0x100 (on_false)
  sub_F20CB0(this+0x1B8, node->trueBB); sub_F20CB0(this+0x1B8, node->falseBB);  // CFG successor edges
  *(int*)(inst + 0xF0) = codegenBrCmpOp(node->op);        // 0xf33ce4  = klr::BrCmpOp - 2 (BranchCompareOp)
  return inst;                                            //   NO engine write — lower_branch re-homes to SP(6)
unconditional:
  // probe no-reorder block map; if inlinable → inlineNoReorderBlock(node->target);
  // else insertElement<InstUnconditionalBranch>("UnconditionalBranch-"+id); target → inst+0xF0
}
```

`RegisterAluOp` and `RegisterMove` operate scalar registers. Both mint-or-reuse named `bir::Register`s via `codegenRegister(name, EngineType)` and — the corrected fact — stamp the instruction's `EngineType` field to **ALL(7)**.

```c
// codegenRegisterAluOp  @0xf25f10   klr::RegisterAluOp{dst,src,imm,AluOp} → InstRegisterAlu(73)
bir::Instruction* codegenRegisterAluOp(this, shared_ptr<klr::RegisterAluOp> node) {
  dstReg = getRegisterByName(node->dst) ?: codegenRegister(node->dst, /*EngineType*/7);  // mint on ALL(7)
  srcReg = getRegisterByName(node->src) ?: codegenRegister(node->src, 7);
  inst = insertElement<bir::InstRegisterAlu>("RegisterAlu-" + id);  // 0xf25fce
  *(int*)(inst + 0x90) = 7;                               // 0xf2602c  movl $0x7,0x90(%r12)  ★ EngineType=ALL
  *(int*)(inst + 0xF0) = codegenAluOp(node->op);          // 0xf26038  = (AluOp-1) → dword_1DE9760
  new RegisterAccess(inst, +0xA0) → setLocation(srcReg, isOut=0) → setType(15);  // in0 = src
  new ImmediateValue(inst, kind 6, +0xA0); value = sext_i32(node->imm);          // in1 = imm
  new RegisterAccess(inst, +0xC0) → setLocation(dstReg, isOut=1) → setType(15);  // out = dst
  return inst;                                            //   dst = src AluOp imm  (int32, sequencer)
}

// codegenRegisterMove  @0xf26370   klr::RegisterMove{dst,imm} → InstRegisterMove(74)
//   dstReg ?: codegenRegister(dst, 7);  inst+0x90 = 7 (ALL);  in = imm@+0xA0;  out = dstReg@+0xC0
//   imm→reg ONLY at codegen — reg→reg chains arise later from copy-prop and are folded by seq_inst_opt.
```

### Function Map

| Function | @ VA | Emits (IT) | Engine | Operand model | Confidence |
|---|---|---|---|---|---|
| `codegenCopyPredicated` | `0xf1a9c0` | `InstCopyPredicated(52)` | DVE *(in builder)* | tensor (3 AP) | CERTAIN |
| `codegenCmpBranch` | `0xf33a70` | `InstCompareAndBranch(78)` / `InstUnconditionalBranch(79)` | Unassigned *(lower_branch→SP6)* | register/imm | CERTAIN |
| `codegenRegisterAluOp` | `0xf25f10` | `InstRegisterAlu(73)` | **ALL(7)** | register/imm | CERTAIN |
| `codegenRegisterMove` | `0xf26370` | `InstRegisterMove(74)` | **ALL(7)** | register/imm | CERTAIN |
| `codegenBrCmpOp` | `0xf141d0` | `klr::BrCmpOp - 2` → `BranchCompareOp` | — | — | CERTAIN |
| `isBrRegRegOp` | `0xf14180` | IMM(klr 2..7→0) / REG(klr 8..13→1) | — | — | CERTAIN |
| `codegenAluOp` | `0xf140c0` | `(AluOp-1)` → `dword_1DE9760` (29-entry) | — | — | CERTAIN |

### Considerations

> **GOTCHA — the register/control ops are stamped `EngineType::ALL(7)`, not `SP(6)`.** Calling them "SP register file" ops is right semantically — they are the sequencer/control registers — but the codegen-time engine tag is `ALL(7)`, witnessed twice in `codegenRegisterAluOp` (`movl $0x7,0x90(%r12)` @ `0xf2602c`, plus `codegenRegister(name, 7)`) and again in `codegenRegisterMove`. `lower_branch` is the pass that later re-homes the control/branch flow onto `SP(6)`. `CmpBranch` writes no engine at all — it stays Unassigned until `lower_branch`.

The `BranchCompareOp` IMM/REG split is byte-exact: `codegenBrCmpOp = klr::BrCmpOp - 2`, with `klr 2..7` = IMM (rhs `ImmediateValue` from `klr+0x40`) → bir `0..5`, and `klr 8..13` = REG (rhs `RegisterAccess` from `klr+0x20`) → bir `6..11`; `always(1)` becomes the `UnconditionalBranch` handled before codegen. The compare op lands at `InstCompareAndBranch+0xF0` — the *same* field `lower_branch`'s `addCompareAndBranch` overloads write and the simulator's `visitInstCompareAndBranch` (`0x1bffb0`) reads (`op@+0xF0`, `on_true@+0xF8`, `on_false@+0x100`).

`CopyPredicated` has a second producer: the `LowerSelect` peephole lowers `Select(51)` → `GenericCopy(1)` + `CopyPredicated(52)`, and *that* path sets `useFalseFill (+0x118)`. `codegenCopyPredicated` never sets `useFalseFill` — it binds only the explicit 3-tensor form. Both feed the same DVE encoder and sim kernel. `RegisterMove` is imm→reg only at codegen; the `seq_inst_opt` pass folds reg→reg copy-chains keying on `(slot, EngineType) → Register*` — exactly the `+0xA0` input / `+0xC0` output split these leaves build, which is why the `EngineType` codegen stamps here matters downstream.

---

## SPMD Primitives — CoreBarrier / RankId / CurrentProcessingRankId

### Purpose

The distributed-execution primitives. `RankId` / `CurrentProcessingRankId` materialize a runtime rank into a scalar `bir::Register` (control-plane); `CoreBarrier` is a cross-core rendezvous over an SBUF semaphore tensor. Together they give SPMD: rank-conditioned branch divergence + semaphore rendezvous. The collective lowering that builds on these is the beta3 [BirCodeGenLoop Collective Codegens](../nki/bircodegen-collective.md) (6.5.13).

### Entry Point

```text
codegenCoreBarrier              0xf23c10 → InstCoreBarrier(IT87)            "CoreBarrier-"
codegenRankId                   0xf26e00 → InstGetGlobalRankId(IT11)        "GetGlobalRankId-"
codegenCurrentProcessingRankId  0xf211b0 → InstGetCurProcessingRankID(IT66) "GetCurProcessingRankID-"
```

### Algorithm

`CoreBarrier` binds the *same* semaphore AccessPattern as both an argument and an output (a read-modify-write of the semaphore), copies the active-core id list into `inst+0x320`, and takes its engine from the KLR node:

```c
// codegenCoreBarrier  @0xf23c10   klr::CoreBarrier{data,cores,engine} → bir::InstCoreBarrier(IT87)
bir::Instruction* codegenCoreBarrier(this, shared_ptr<klr::CoreBarrier> node) {
  auto ap = codegenTensorRef(node->data);                 // the SBUF SEMAPHORE tensor
  inst = insertElement<bir::InstCoreBarrier>("CoreBarrier-" + id);   // 0xf23caa
  sub_F20EC0(this, inst);
  inst->addArgument<PhysicalAccessPattern>(ap);           // 0xf23d40  semaphore as INPUT  (the R of RMW)
  inst->addOutput  <PhysicalAccessPattern>(ap);           // 0xf23d92  semaphore as OUTPUT (the W of RMW)
  uint32_t* ids = tc_new(4 * node->cores.size);
  for (c : node->cores) *ids++ = c;                       // active-core id list
  store(inst + 0x320, ids);                               // ★ +0x320 = active-core list
  *(int*)(inst + 0x90) = codegenEngine(node->engine);     // +0x90 = EngineType (from klr)
  return inst;                                            //   NO barrier index — set by renumberCoreBarriers
}
```

`RankId` and `CurrentProcessingRankId` resolve-or-mint the destination register, write `EngineType=ALL(7)` by hand, and bind a `RegisterAccess` *output* carrying `Dtype uint32 (0xE)`:

```c
// codegenRankId  @0xf26e00   klr::RankId{dst} → bir::InstGetGlobalRankId(IT11), opcode 220
bir::Instruction* codegenRankId(this, shared_ptr<klr::RankId> node) {
  reg = getRegisterByName(node->dst) ?: codegenRegister(node->dst, /*EngineType*/7);  // 0xf26fb0 edx=7
  inst = insertElement<bir::InstGetGlobalRankId>("GetGlobalRankId-" + id);  // 0xf26ea0
  *(int*)(inst + 0x90) = 7;                               // 0xf26edc  EngineType=ALL
  sub_F20EC0(this, inst);
  new RegisterAccess(inst, +0xC0) → setLocation(reg, isOut=1);   // rank → dst register (OUTPUT)
  inst->out.setType(0x0E);                                // uint32
  return inst;
}
// codegenCurrentProcessingRankId  @0xf211b0   → InstGetCurProcessingRankID(IT66)
//   same register-output shape, PLUS group geometry: iterationId→inst+0xF0, channelId→inst+0x118,
//   replicaGroup (list<list<int>>) → vector<vector<uint>> @ inst+0x168.
```

### Function Map

| Function | @ VA | Emits (IT, opcode) | Materialization | Confidence |
|---|---|---|---|---|
| `codegenCoreBarrier` | `0xf23c10` | `InstCoreBarrier(87, op 216)` | semaphore RMW + active-core list `+0x320` | CERTAIN |
| `codegenRankId` | `0xf26e00` | `InstGetGlobalRankId(11, op 220)` | uint32 → named `Register` (ALL 7) | CERTAIN |
| `codegenCurrentProcessingRankId` | `0xf211b0` | `InstGetCurProcessingRankID(66)` | uint32 → `Register` + group geometry | CERTAIN |

### Considerations

`GetGlobalRankId` reads the worker's invariant global rank (`= core / numCoresPerLNC`, sim `NeuronCoresManager::getRankIdforCore` `0x272380`); `GetCurProcessingRankID` resolves the rank *within* the current collective/processing group (the TP rank) from `(iterationId, channelId, numChannels)` over the `replicaGroup` partition. Both write `uint32` into a named `bir::Register` on `ALL(7)` (the verifier `visitInstGetGlobalRankId` `0xfa5a90` enforces `checkValidEngines({7})` and `checkArchLevel(0x14)` = arch ≥ sunda(20)). `CoreBarrier` requires gen3 (`checkArchLevel(0x1E)` = 30; LNC is a Trn2 feature).

> **GOTCHA — codegen mints the barrier, but never numbers it.** `codegenCoreBarrier` sets the data-AP arg+out, the active-core list, and the engine — but **not** the barrier index. The contiguous module-wide index is assigned post-codegen by `renumberCoreBarriers` (`0x1089250`, multi-core only; written to the QuasiAffineExpr-variant immediate at `+240` with discriminator `+272`), and the *hardware* barrier-register id is allocated even later by `CoreV3GenImpl::getNextCoreBarrierId` (`0x1346150`) at SASS emit. `lower_control` *also* mints its own epilogue-fence `InstCoreBarrier`s (active-core list `= [0..lnc-1]`) using the same `+0x320` layout, so there are two origins of IT87 — explicit (from `klr.cores`) and implicit control-flow fences — both renumbered together. A reimplementer who tries to assign the index in the codegen leaf will collide with the renumber pass.

---

## Debug / Driver / Immediate — DevicePrint / SequenceBounds / LncKernel / Immediate

### Purpose

The debug-print, sequence-bound query, kernel-body driver, and host-immediate path. `DevicePrint` is the on-device `printf`; `SequenceBounds` queries variable-length sequence boundaries; `LncKernel` is the per-LNC-core kernel-body driver (not an operator); `codegenImmediate` is the value sub-encoder. IT values are byte-confirmed two ways (the libBIR ctor's `mov ecx, IT` and the [`sameInst` guards](instruction-type.md), 7.3).

### Entry Point

```text
codegenDevicePrint     0xf2cde0 (kind 74) → InstDevicePrint(57, libBIR ctor mov ecx,0x39)        "DevicePrint-"
codegenSequenceBounds  0xf276c0 (kind 41) → InstGetSequenceBounds(64, libBIR ctor mov ecx,0x40)  "SequenceBounds-"
codegenLncKernel       0xf34140 (driver) → (no single Inst — walks the stmt list)
codegenImmediate       0xf16980 (value)  → variant<float,int> (bir::ImmediateValue minted by addOperandToInst)
```

### Algorithm

```c
// codegenDevicePrint  @0xf2cde0   klr::DevicePrint{src,printPrefix,buffer} → InstDevicePrint(57), cpp:1172
bir::Instruction* codegenDevicePrint(this, shared_ptr<klr::DevicePrint> node) {
  InstArg ref = codegenTensorRef(node->src);              // the printed tensor (single input)
  inst = InstBuilder::addInstruction<bir::InstDevicePrint>(  // 0xf2cf7b  the ONLY addInstruction<…> site
             builder, "DevicePrint-" + id, /*args*/{ref}, /*imms*/{}, /*outputs*/{}, src_loc);
  inst->prefix.assign(node->printPrefix);                 // → inst+0xF0 (json key "prefix")
  *(int*)(inst + 0x110) = (node->buffer.firstDword == 2); // → inst+272 (json key "output_buffer")
  return inst;
}

// codegenSequenceBounds  @0xf276c0   klr::SequenceBounds{dst,segmentIds,opt<Dtype>} → InstGetSequenceBounds(64)
bir::Instruction* codegenSequenceBounds(this, shared_ptr<klr::SequenceBounds> node) {
  auto apDst = codegenTensorRef(node->dst);               // OUTPUT
  auto apSeg = codegenTensorRef(node->segmentIds);        // INPUT (per-token segment ids)
  if (!apDst.opt_has || !apSeg.opt_has) throw bad_optional_access;  // both required
  return InstBuilder::addInstruction<bir::InstGetSequenceBounds>(   // 0xf27a7b  cpp:1708
             builder, "SequenceBounds-" + id, /*args*/{apSeg}, /*imms*/{}, /*outputs*/{apDst}, src_loc);
  //   per-segment bounds written into the dst SBUF TENSOR (getDst→Inst+0xC8), not a scalar register
}
```

`codegenImmediate` is the value decoder — it returns a **host** `variant<float,int>`; the `bir::ImmediateValue` is built one level up in `addOperandToInst`:

```c
// codegenImmediate  @0xf16980   klr::Immediate{tag@0, payload@4} → variant<float,int>
variant<float,int> codegenImmediate(this, shared_ptr<klr::Immediate> node) {
  switch (*(int*)node) {                                  // tag @ +0
    case 4: return {float, codegenImmediateFloatWrapper(node)};  // 0xf14080  payload @ node+4; variant byte 0
    case 3: return {int,   codegenImmediateIntWrapper(node)};    // 0xf14090  payload @ node+4; variant byte 1
    default: __assert_fail("Unsupported immediate type encountered",   // klir_to_bir_codegen.cpp:425
                           "klir_to_bir_codegen.cpp", 425, ...);
  }
}
// addOperandToInst @0xf1be80 then materializes: float→ImmediateValue setType(16)=float32;
//   int→setType(15)=int32; value union @ ImmediateValue+0x48; threaded onto Inst+0xA0 input list.
```

### Function Map

| Function | @ VA | Emits (IT) | Role | Confidence |
|---|---|---|---|---|
| `codegenDevicePrint` | `0xf2cde0` | `InstDevicePrint(57)` | `src`→args; `printPrefix`→`+240`; `(buffer==2)`→`+272` | CERTAIN |
| `codegenSequenceBounds` | `0xf276c0` | `InstGetSequenceBounds(64)` | `segmentIds`→in `+0xA8`; `dst`→out `+0xC8` (SBUF tensor) | CERTAIN |
| `codegenLncKernel` | `0xf34140` | *(driver)* | per-core stmt-list walk; LNC shard; CFG legalize | CERTAIN |
| `codegenImmediate` | `0xf16980` | *(value)* | `variant<float,int>` (tag 4=float / 3=int) | CERTAIN |
| `addOperandToInst` | `0xf1be80` | `bir::ImmediateValue` | float→float32(16) / int→int32(15), value union `+0x48` | CERTAIN |

### Considerations

> **NOTE — `DevicePrint` has two producers, same opcode.** The user-facing `nki.device_print` lowers through `codegenDevicePrint` via `InstBuilder::addInstruction<InstDevicePrint>` (the *only* such call site). A second, *auto-injected* `COREID DevicePrint` is emitted in `codegenStmtOperWrapper` (`0xf325a0`) via `insertElement<InstDevicePrint>` (`0xf32caf`), once per **non-fp32** output in device-dump builds (the gate tests the AP MemoryLocation's element **bit-width != 32**, not the `Dtype` enum), named `"COMPILER-GENERATED-COREID-<core>-DevicePrint-"`. Both mint IT57; they differ only in builder API and provenance.

`SequenceBounds` is *not* a loop trip-count query — it computes the start/end boundaries of variable-length sequences packed in a tensor (ragged / packed attention), by scanning `segmentIds`; the per-segment bounds land in the `dst` **SBUF tensor**, not a scalar register (downstream `RegisterMove`/`RegisterAluOp` can pull them into sequencer registers if needed).

`codegenLncKernel` is **not** a `klr::Operator` — there is no `OperatorLncKernelWrapper` in the dynsym. `klr::LncKernel` is a top-level statement-list container that the driving pass (`TranslateNKIASTToBIR`) walks once per logical neuron core. The driver does far more than "iterate → `codegenStmt`": (a) splices the shared-constant file list; (b) binds named I/O tensors; (c) **LNC sharding** — `core = getNeuronCoreId(); if (core unset && LNC_count > 1) throw; if (core >= LNC_count) skip-lowering (this core gets no body)`; (d) `no_reorder`-block structural validation (last stmt must be `cmpBranch`/`always`, asserts at cpp 2780-2786); (e) the per-statement walk wrapped in try/catch into a deferred error sink; (f) **CFG legalization** — empty terminating blocks get a `"<kernel>-RESUME-Block-compiler-inserted"` BB with an `InstUnconditionalBranch(79)`; then (h) one `codegenDependencyEdges` and two late fixups (`legalizeActBiasTensorAddress`, `legalizeIdentityMatrices`).

> **QUIRK — `klr::Immediate` is always an int32 or float32 literal, never a register.** `codegenImmediate` handles only tag 3 (int) and tag 4 (float); anything else asserts at `klir_to_bir_codegen.cpp:425`. Symbolic/array immediates (`SymbolicImmediateValue` kind 7, `ImmediateArray` kind 8) come from the JSON wire path, not from this codegen. And a constant operand becomes an inline `ImmediateValue` argument — there is **no** `RegisterMove` materialization on the immediate path. `klr::RegisterMove` (op kind 46, IT74) is a *separate* operator that moves a value into a sequencer register; it does not appear on the immediate-operand lowering.

---

## Shared Infrastructure Functions

| Function | @ VA | Role | Confidence |
|---|---|---|---|
| `codegenTensorRef` | `0xf18b30` | `klr::TensorRef` → `{MemLoc*, SmallVector<APPair,4>, base-idx, opt<Dtype>}`; `dynId!=0` → `sub_F12B40` dynamic-AP splice | CERTAIN |
| `codegenImmediate` | `0xf16980` | `klr::Immediate` → host `variant<float,int>` (tag 4/3; else assert cpp:425) | CERTAIN |
| `codegenEngine` | `0xf140f0` | `klr::Engine` → `EngineType` via `dword_1DE9740 = {2,4,5,3,1,6}` | CERTAIN |
| `codegenRegister` | `0xf141e0` | mint-or-reuse a named `bir::Register` on a chosen `EngineType` (= `Function::addRegister`) | CERTAIN |
| `returnAndIncrementId` | — | mint the per-inst name suffix `"<id>"`, bump counter at `KlirToBirCodegen+0xF0` | CERTAIN |
| `insertElement<InstT>` | per-T | `NamedObjectContainer<BasicBlock,Instruction>` — alloc typed Inst (stamps IT at `+0x58`), link to BB | CERTAIN |
| `sub_F20EC0` | `0xf20ec0` | append the new inst to the codegen book-keeping list (rooted `this+0xE0`) | MEDIUM (role inferred; not symbol-named) |
| `sub_F20CB0` | `0xf20cb0` | hash a BB name into the per-function CFG successor-edge set at `this+0x1B8` | CERTAIN |
| `sub_F12B40` | `0xf12b40` | copy/grow a `SmallVector<APPair,4>` (16-byte `APPair`) — the dynamic-AP splice | CERTAIN |

---

## Related Components

| Name | Relationship |
|---|---|
| `renumberCoreBarriers` (`0x1089250`) | assigns the module-wide CoreBarrier index codegen leaves leave unset |
| `lower_branch` (`LowerBranch::run` `0x11c02d0`) | re-homes branch/compare flow onto `SP(6)`; consumes the `BranchCompareOp@+0xF0` this leaf writes |
| `seq_inst_opt` (`SeqInstOpt::run` `0x155e0e0`) | folds reg→reg move chains keying on `(slot, EngineType) → Register*` |
| `LowerSelect` (`SplitSelectVisitor::visitInstSelect` `0xbf1b30`) | the second `InstCopyPredicated(52)` producer; sets `useFalseFill` |
| `const_propagate_for_memset0` (`0xbd5fe0`) | synthesizes `InstMemset(Const)` to fold all-equal tensors |
| `visitInstMemset` (sim `0x1d5060`) | the random-fill numeric model (XORWOW / MT19937-64) for `codegenRng`'s output |

## Cross-References

- [KlirToBirCodegen Dispatch Core & the Master Routing Table](klir-codegen-dispatch.md) — the `codegenStmt`→`codegenOperator<Op>Wrapper` switch that reaches every leaf here (7.21)
- [Codegen Sub-Encoders — Operand / Immediate / Register / Dependency-Edge](codegen-sub-encoders.md) — `codegenRegister`, `addOperandToInst`, and the shared value-binding helpers (7.24)
- [codegen AccessPattern Primitive](codegen-accesspattern.md) — `codegenTensorRef` and the `PhysicalAccessPattern` every tensor leaf builds (7.22)
- [InstructionType: the Opcode Enum & sameInst Family Masks](instruction-type.md) — IT 10/52/57/59/61/64/65/73/74/78/79/87/88/89/90/91/97/98/99/104/11/66 source of truth (7.3)
- [Argument / AccessPattern / Immediate / Register Value Model](value-model.md) — `RegisterAccess`/`ImmediateValue` the scalar-control leaves use (7.4)
- [DVE Search & Datamove Encoding — Max8 / FindIndex8 / MatchReplace / Nonzero](../isa/dve-search-encoding.md) — the wire encoding of the DVE-search ops this page produces (2.16)
- [RNG-Family Encoding](../isa/rng-encoding.md) — the wire encoding of the RNG ops, and the gen-1/gen-2 state layout (2.18)
- [SP Sync / Branch / Control Encoding](../isa/sp-sync-encoding.md) — the wire encoding of `CmpBranch`/`CoreBarrier`/register ops (2.20)
- [BirCodeGenLoop Compute Codegens](../nki/bircodegen-compute.md) — the beta3 counterpart (DVE-search / RNG numeric models) (6.5.11)
- [BirCodeGenLoop Collective Codegens](../nki/bircodegen-collective.md) — the beta3 counterpart for the SPMD/rank primitives (6.5.13)
