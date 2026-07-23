# Master Opcode Reference

> *All symbols, ordinals, and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The L1 enum is defined in `neuronxcc/starfish/lib/libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`); the L2/L3 encoders and `setupHeader` are in `neuronxcc/starfish/lib/libwalrus.so` (`.text`/`.rodata` base `0x62d660` / `0x1c72000`, VA == file offset). cp311/cp312 share the `.text` logic — the opcode integers and the L1/L2/L3 arithmetic are ABI-stable across cp3xx, only the VAs drift. Other wheels differ — treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This appendix is the single consolidated lookup table for the neuronx-cc backend opcode space: **all 110 `bir::InstructionType` arms × the wire opcode word per generation `{V2, V3, V4}` × the owning engine × the access/bundle family.** It unifies what the three normative pages established separately — the [110-opcode `InstructionType` enum and its `sameInst` masks](../bir/instruction-type.md), the [`setupHeader` L3 wire-word path and per-op opcode master](../walrus/opcode-master.md), and the per-engine encoding pages (PE matmul, Pool, Activation, DVE, DMA) — into one row-per-opcode grid a reimplementer can scan by either the IR ordinal or the wire byte.

The reimplementation model is the **three-layer opcode contract** ([§0](#0-the-three-layer-opcode-contract)): a BIR op carries up to three *distinct* integer identities in three *separate* namespaces. **L1** is the `bir::InstructionType` ordinal (`0..109`, the IR class id). **L2** is the `NEURON_ISA_TPB_OPCODE` enum value the `COLLECT_OPCODES` arm records (the engine-relative silicon micro-op id). **L3** is the 16-bit wire header word `setupHeader` stamps at `bundle[0:2]`, equal to `(0x10 << 8) | L2byte`. The cardinal invariant is **`L1 ≠ L2` for every op** — they are different enums, equal for no opcode — so an opcode↔IT mapping is *never* one-to-one, and the only cross-layer-stable key is the shared **name** (the BIR-JSON `"type"` string), not any integer.

This page is a reference catalog, not an algorithm walkthrough; its value is **completeness and correctness**. The table in [§1](#1-master-110-row-opcode-table) is grounded directly in `InstructionType2string` (`0x2d5bf0`), the `sameInst` switch (`0x2db7b0`), the three `setupHeader` bodies, and the per-op `visitInst*` COLLECT/GENERATE immediates. Every per-gen cell carries a confidence tag, and the five rows most prone to transcription error (the per-arch `Activation` divergence, the MX family, the collective/custom-op/DMA arms) carry their explicit byte witnesses in [§4](#4-byte-witnesses--five-highest-risk-rows).

| | |
|---|---|
| **L1 enum** | `bir::InstructionType` — 110 members, ordinals `0..109`, `uint32_t`, at `bir::Instruction + 0x58` |
| **L1 name table** | `bir::InstructionType2string` @ `0x2d5bf0` (110-case switch → `assert "Unknown opcode"` @ `Instruction.cpp:79`) |
| **L2 enum** | `NEURON_ISA_TPB_OPCODE` — COLLECT-set int; decoded by `core_v4::enum_variant_string_opcode` @ `0x143fd80` (161 named) |
| **L3 word rule (INV-1)** | `L3word = (0x10 << 8) \| L2byte`; `QuantizeMx` sets `byte[1] \|= 1` on `SATURATE` |
| **Header writer** | `CoreV{2,3,4}GenImpl::setupHeader` @ `0x1172120` / `0x1369280` / `0x143f440` — byte-identical, 16-byte body |
| **`inst_word_len`** | `byte[1] = 0x10` = 16 dwords = 64-byte bundle, hardcoded, no non-16 length exists |
| **Cardinal invariant (INV-3)** | `L1 ≠ L2` numerically for **every** op; the shared **name** is the only cross-layer key |
| **Concrete-opcode ITs** | ~85 of 110 reach a real L2/L3 opcode; ~25 are lowered / abstract / kernel-carrier (no opcode) |

---

## 0. The three-layer opcode contract

A BIR op has up to three integer identities. Conflating them is the cross-layer error the normative opcode page ([§0](../walrus/opcode-master.md)) was written to kill, and it recurs in any map that prints "opcode 4 = Activation" without saying *which* opcode.

```text
L1  bir::InstructionType ordinal (0..109)
      = the C++ enum tag at bir::Instruction+0x58 (the 22nd dword)
      = the IR class id — what the node *is*
      defined in libBIR  (2string @0x2d5bf0, sameInst @0x2db7b0)

L2  NEURON_ISA_TPB_OPCODE value
      = the int the COLLECT_OPCODES arm inserts into std::set<u32>
      = the byte GENERATE writes to bundle[0]
      = the engine-bitmap bit position in the wire validator
      = the silicon micro-op id — engine-relative
      decoded by core_v4::enum_variant_string_opcode @0x143fd80

L3  the 16-bit wire HEADER WORD setupHeader stamps at bundle[0:2]
      bundle[0]   = opcode byte           (= L2 low byte)
      bundle[1]   = 0x10 = inst_word_len  (16 dwords = 64-byte bundle, HARDCODED)
      bundle[2:4] = 0x0000
      ⇒ L3word = (0x10 << 8) | L2byte
```

The three invariants a reimplementation must honor:

- **INV-1** — `L3word = (0x10 << 8) | L2byte`. The high byte `0x10` is the fixed length nibble (16 dwords); the low byte is the L2 opcode. `QuantizeMx` is the *only* op that perturbs the high byte: it OR-s bit 0 (`0x10 → 0x11`) when the `SATURATE` flag is set.
- **INV-2** — `L2` is one value, consistent across its uses: COLLECT-set int == GENERATE `bundle[0]` byte == opcode-count-map key == engine-bitmap bit.
- **INV-3** — **`L1 ≠ L2` numerically for every op.** The encoder is the non-injective projection `L1 → {L2}`. The only cross-layer-stable key is the L2/L1 *name* (BIR-JSON `"type"`), never any integer.

`setupHeader` itself is the same six instructions in all three generations (`0f b6 02 c6 46 01 10 88 06 31 c0 66 89 46 02 c3`): load the opcode byte, store `0x10` at `bundle[1]`, store the opcode at `bundle[0]`, zero `bundle[2:4]`, return. It is reached as a virtual through the `Generator` vtable slot `+0x48` (slot 9). All per-arch and per-op variation lives in the *opcode byte* fed to it, never in the header layout — which is exactly why this table can be one grid: only one column-family (the L3 byte) ever moves.

> **NOTE —** the engine column is **metadata, not a wire field.** The physical engine rides the BIR `Instruction` at `+0x90` (`EngineType`); the encoder only *reads* it as a census-map key. `{PE=0, ACT=1, POOL=2, DVE=3, SP=4}` on the wire. It is listed below because a reimplementer must route the op to the right engine, but it is never encoded in the 64-byte bundle.

---

## 1. Master 110-row opcode table

One row per `bir::InstructionType` ordinal. Words are shown as `0x10NN` (`NN` = the L2 low byte). `=` in a per-gen column means *inherits the prior generation's word verbatim* (no override). `—` means *not encoded on that generation* (gen-gated). `(lowered)` / `(no enc)` / `(base)` mean the IT reaches no `setupHeader` at all (see the **Kind** column).

Columns: **IT** = L1 ordinal · **Name** = `InstructionType2string` name (= the cross-layer key) · **L2** = `NEURON_ISA_TPB_OPCODE` low byte (hex) · **V2/V3/V4** = the L3 16-bit word per generation · **#B** = 64-byte bundles this one op emits · **Eng** = physical engine (`+0x90` metadata) · **Kind/flags** = reconciliation tags (legend below the table) · **Conf** = confidence of the opcode value.

Legend for **Kind/flags**: `[base]` abstract base IT (leaves carry the opcode) · `[low]` lowered-away / no encoder · `[split]` one IT forks into several L2 opcodes by a runtime field · `[mb:n]` multi-bundle (emits *n* wire words) · `[col]` L2 collision (this opcode is shared by another IT) · `[ΔV4]`/`[ΔV3]` genuine per-gen opcode-byte renumber · `[newV3]`/`[newV4]` gen-introduced (no earlier encoder) · `[+1=cast]` opcode low bit = dtype-cast flag.

| IT | Name | L2 | V2 | V3 | V4 | #B | Eng | Kind / flags | Conf |
|---|---|---|---|---|---|---|---|---|---|
| 0 | `Generic` | — | (lowered) | = | = | lo | — | `[base][low]` accepts IT≤3 in `sameInst` | CERTAIN |
| 1 | `GenericCopy` | — | (lowered) | = | = | lo | — | `[low]` no `visitInstGenericCopy` body | CERTAIN |
| 2 | `GenericRelu` | — | (lowered) | = | = | lo | — | `[low]` → Activation | MEDIUM |
| 3 | `AbstractCopy` | — | (lowered) | = | = | lo | — | `[low]` LowerAC pass | CERTAIN |
| 4 | `Activation` | `0x21`/`0x25` | `0x1021` | = | `0x1025` | 1 | ACT | `[ΔV4]` ★ V4 byte `0x25` ≠ V2/V3 `0x21` | CERTAIN |
| 5 | `ReadActivationAccumulator` | `0x24` | `0x1024` | = | = | 1 | ACT | `[col]` shares `0x24` w/ IT101 | CERTAIN |
| 6 | `LoadActFuncSet` | `0x23` | `0x1023` | = | = | 1 | ACT | — | CERTAIN |
| 7 | `MatmultBase` | — | (base) | (base) | (base) | — | PE | `[base]` of {7,8,9,95} | n/a |
| 8 | `Matmult` | `0x01`+`0x02` | `0x1001`+`0x1002` | = | = | 1–4 | PE | `[split][mb:2]` fp32→4 bundles; cache may skip LdW | CERTAIN |
| 9 | `MatmultSparse` | `0x06`+`0x07`(+`0x01`) | — | `0x1006`+`0x1007` | = | 2–5 | PE | `[split][mb:2+][newV3]` +LdWeights `0x01`; fp32→5 | CERTAIN |
| 10 | `Memset` | `0x49`/`0x4D` | `0x1049`/`0x104D` | = | = | 1 | DVE | `[split][col]` `0x49` Const / `0x4D` Random(==Rng IT100) | CERTAIN |
| 11 | `GetGlobalRankId` | `0xDC` | `0x10DC` | = | = | 1 | CC | — | CERTAIN |
| 12 | `NoOp` | `0xA4` | `0x10A4` | = | = | 1 | SP | `Nop` | CERTAIN |
| 13 | `EventSemaphore` | `0xA0` | `0x10A0` | = | = | 1 | SP | — | CERTAIN |
| 14 | `GroupResetSemaphores` | `0xB0` | `0x10B0` | = | = | 1 | SP | `EventSemaphoreRangeClear` (≠ IT13) | CERTAIN |
| 15 | `AllEngineBarrier` | `0xD5` | `0x10D5` | = | = | 1 | SP | `PseudoSyncBarrier` | CERTAIN |
| 16 | `Drain` | `0xA2` | `0x10A2` | = | = | 1 | SP | — | CERTAIN |
| 17 | `Halt` | `0xA1` | `0x10A1` | = | = | 1 | SP | — | CERTAIN |
| 18 | `DMA` | — | (base) | = | = | — | DMA | `[base]` accepts {18,19,22,32,41-46,67} | n/a |
| 19 | `Load` | `0xD4` | `0x10D4` | = | = | 1 | DMA | `[col]` → `generateDynamicDMA` (==Save,DMACopy) | CERTAIN |
| 20 | `Pool` | `0x45` | `0x1045` | = | = | 1 | POOL | — | CERTAIN |
| 21 | `Reciprocal` | `0x48` | `0x1048` | = | = | 1 | DVE | — | CERTAIN |
| 22 | `Save` | `0xD4` | `0x10D4` | = | = | 1 | DMA | `[col]` → `generateDynamicDMA` | CERTAIN |
| 23 | `TensorCopy` | `0x46`/`0x47` | `0x1046`/`0x1047` | = | = | 1 | DVE | `[split][+1=cast]` `0x47` = dtype change | CERTAIN |
| 24 | `TensorCopyDynamicSrc` | — | (lowered) | = | = | lo | — | `[low]` lowered before CoreV2 | MEDIUM |
| 25 | `TensorCopyDynamicDst` | — | (lowered) | = | = | lo | — | `[low]` lowered before CoreV2 | MEDIUM |
| 26 | `IndirectCopy` | `0xE7` | `0x10E7` | = | (copy) | 1 | POOL | TRN1/V2-only; lowered to COPY on V3+ | CERTAIN |
| 27 | `TensorReduce` | var | `0x107C`/`0x107D`/`0x1042`/`0x1052`/`0x1083`/`0x1084` | = | = | 1 | Pool/DVE | `[split]` TR 124/125 + CR 66/82/131/132 (6 opcodes) | CERTAIN |
| 28 | `TensorScalar` | var | `0x1043`/`0x1093`/`0x1053`/`0x109A` | (ext) | (ext) | 1 | DVE | `[split]` no own enc; shares IT29 dispatcher | CERTAIN |
| 29 | `TensorScalarPtr` | var | `0x1043`/`93`/`53`/`9A`/`9C`/`9E`/`E5`/`74` | (ext) | (ext) | 1 | DVE | `[split]` dispatcher: OrPtr+STT+Addr | CERTAIN |
| 30 | `TensorScalarCache` | `0x9A`/`0xE6` | `0x109A`/`0x10E6` | (ext) | (ext) | 1 | DVE | `[split]` `0x9A` Reduce / `0xE6` Cumul+Scan by TSCMode | CERTAIN |
| 31 | `TensorTensor` | `0x41`/`0x51` | `0x1041`/`0x1051` | `0x1041`/`0x1051` | = | 1 | DVE | `[split]` +GPSIMD int32 re-stamp `0x8A`; V3 = field ext only | CERTAIN |
| 32 | `DMACopy` | `0xD4`(/`0xDA`) | `0x10D4`(/`0x10DA`) | = | = | 1 | DMA | `[col]` → `generateDynamicDMA`; `IT==32` test re-distinguishes | CERTAIN |
| 33 | `GPSIMDSB2SB` | `0xBF` | — | `0x10BF` | = | 1 | GPSIMD | `[newV3]` `Sb2sbCollective` | CERTAIN |
| 34 | `BNStats` | `0x61`/`0x82` | `0x1061`/`0x1082` | = | = | 1 | POOL | `[split]` `0x61`/`0x82` by `apply_transpose` | CERTAIN |
| 35 | `BNStatsAggregate` | `0x62` | `0x1062` | = | = | 1 | POOL | `BatchNormAggregate` | CERTAIN |
| 36 | `BNGradients` | `0x63`/`0x94` | `0x1063`/`0x1094` | = | = | 1 | POOL | `[split]` `0x63` AP-coeff / `0x94` register-coeff | CERTAIN |
| 37 | `BNBackprop` | `0x64`(+`0x65`) | `0x1064`(+`0x1065`) | = | = | 1–2 | POOL | `[mb:2]` BACK_PROP `0x64` + PARAM_LOAD `0x65` | CERTAIN |
| 38 | `BNBackprop2` | `0x64`(+`0x8E`) | `0x1064`(+`0x108E`) | = | = | 1–2 | POOL | `[mb:2]` shares `0x64`; V2 visit stamps `0x8E` param | CERTAIN |
| 39 | `StreamShuffle` | `0x69`+`0x6A` | `0x1069`+`0x106A` | = | = | 2 | DVE | `[mb:2][col]` mask `0x69` (==StreamTranspose) | CERTAIN |
| 40 | `StreamTranspose` | `0x69`+`0x6B` | `0x1069`+`0x106B` | = | = | 2 | DVE | `[mb:2][col]` mask `0x69` (==StreamShuffle) | CERTAIN |
| 41 | `ReadVarAddr` | `0xB2` | `0x10B2` | = | = | 1 | DMA | `MoveShape` (`generateMoveShape`) | HIGH |
| 42 | `GenericIndirectLoad` | — | (lowered→43) | = | = | lo | — | `[low]` → IndirectLoad | MEDIUM |
| 43 | `IndirectLoad` | `0xD6`/`0xC4` | `0x10D6`/`0x10C4` | = | = | 1 | DMA | `[split]` `generateIndirectLoadSave(isLoad)` picks by direction | CERTAIN |
| 44 | `GenericIndirectSave` | — | (lowered→45/46) | = | = | lo | — | `[low]` → IndirectSave(Accum) | MEDIUM |
| 45 | `IndirectSave` | `0xD6`/`0xC4` | `0x10D6`/`0x10C4` | = | = | 1 | DMA | `[split]` `generateIndirectLoadSave(isLoad)` picks by direction | CERTAIN |
| 46 | `IndirectSaveAccumulate` | `0xCA` | `0x10CA` | = | = | 1 | CCE/Pool/Act | `PseudoEmbeddingUpdate` (scatter-add) | CERTAIN |
| 47 | `Collective` | — | (base) | = | = | — | POOL | `[base]` of {47,48,49,50} | n/a |
| 48 | `CollectiveCompute` | `0xD9`/`0xDA` | `0x10D9`/`0x10DA` | = | = | 1 | POOL | `[split]` fine `0xD9` / coarse `0xDA` | CERTAIN |
| 49 | `CollectiveSend` | `0xCB` | `0x10CB` | = | = | 1 | POOL | `[col]` shares `0xCB` w/ Recv; direction = field | CERTAIN |
| 50 | `CollectiveRecv` | `0xCB` | `0x10CB` | = | = | 1 | POOL | `[col]` shares `0xCB` w/ Send | CERTAIN |
| 51 | `Select` | — | (lowered) | = | = | lo | DVE | `[low]` → GenericCopy + CopyPredicated(`0xEA`) | CERTAIN |
| 52 | `CopyPredicated` | `0xEA` | `0x10EA` | = | = | 1 | DVE | the masked-copy / SELECT emitter | CERTAIN |
| 53 | `CustomOp` | `0x85`+`0x86` | `0x1085`+`0x1086` | = | = | ≥2 | any | `[mb:≥2]` header `0x85` + `0x86`×(1+num_args) | CERTAIN |
| 54 | `BIRKernel` | — | (no enc) | = | = | none | — | `[low]` kernel container; no 64-B bundle | MEDIUM |
| 55 | `NKIKernel` | — | (no enc) | = | = | none | — | `[low]` "Not Implemented" (sameInst too) | CERTAIN |
| 56 | `NKIKLIRKernel` | — | (no enc) | = | = | none | — | `[low]` "Not Implemented" | CERTAIN |
| 57 | `DevicePrint` | — | (debug-meta) | = | = | none | — | `[low]` `instruction_debug_info` protobuf, NO `setupHeader` | CERTAIN |
| 58 | `GetRandState` | `0x77` | `0x1077` | `0x1077` | = | 1 | DVE/Pool | `[col]` shares `0x77` w/ RandGetState IT99; V3 ovr = same op | CERTAIN |
| 59 | `SetRandState` | `0xD0`(/`0x78`) | `0x10D0` | `0x1078`/`0x10D0` | = | 1 | DVE/Pool | `[split]` V2=DVE `0xD0`; V3 Pool-arm `0x78` / DVE-arm `0xD0` | CERTAIN |
| 60 | `Rand` | — | (no enc) | (no enc) | see Rand2 | — | DVE | `[low]` no CoreV* encoder; Rand2(IT97) is the gen form | MEDIUM |
| 61 | `Iota` | `0x7E` | `0x107E` | = | = | 1 | DVE | — | CERTAIN |
| 62 | `TensorScalarAffineSelect` | `0x92` | `0x1092` | (ext) | (ext) | 1 | DVE | fixed `0x92`; 4-D mask + 2-bit compare | CERTAIN |
| 63 | `RangeSelect` | `0xBC` | — | `0x10BC` | = | 1 | DVE | `[newV3]` +`0x9B` DveReadAcc drain tail | CERTAIN |
| 64 | `GetSequenceBounds` | `0xBE` | — | `0x10BE` | = | 1 | GPSIMD | `[newV3]` | CERTAIN |
| 65 | `Dropout` | `0x7F` | `0x107F` | = | = | 1 | DVE | — | CERTAIN |
| 66 | `GetCurProcessingRankID` | `0xDB` | `0x10DB` | = | = | 1 | CC | pairs w/ GetGlobalRankId | CERTAIN |
| 67 | `DMATrigger` | `0xC1` | `0x10C1` | = | = | 1 | DMA | `PseudoDMATrigger` | CERTAIN |
| 68 | `DMADescriptor` | — | (no enc) | = | = | none | DMA | `[base][low]` of {68..72}; emitted via DMA path | MEDIUM |
| 69 | `DMADescriptorCopy` | — | (no enc) | = | = | none | DMA | `[low]` | MEDIUM |
| 70 | `DMADescriptorCCE` | — | (no enc) | = | = | none | DMA | `[low]` | MEDIUM |
| 71 | `DMADescriptorTranspose` | — | (no enc) | = | = | none | DMA | `[low]` | MEDIUM |
| 72 | `DMADescriptorReplicate` | — | (no enc) | = | = | none | DMA | `[low]` | MEDIUM |
| 73 | `RegisterAlu` | `0xA8` | `0x10A8` | = | = | 1 | SP | `AluOp` | CERTAIN |
| 74 | `RegisterMove` | `0xA7` | `0x10A7` | = | = | 1 | SP | `Move` | CERTAIN |
| 75 | `TensorLoad` | `0xCE`/`0xAA` | `0x10CE`/`0x10AA` | = | = | 1 | scalar | `[split]` `0xCE` imm-addr / `0xAA` reg-mode | CERTAIN |
| 76 | `TensorSave` | `0xAB`/`0xCD` | `0x10AB`/`0x10CD` | = | = | 1 | scalar | `[split]` `0xAB` imm-addr / `0xCD` reg-mode | CERTAIN |
| 77 | `Terminator` | — | (base) | = | = | — | SP | `[base]` of {77,78,79,81,82}; **rejects 80** | n/a |
| 78 | `CompareAndBranch` | `0xA9` | `0x10A9` | = | = | 1 | SP | `[col]` shares `0xA9` w/ UncondBranch | CERTAIN |
| 79 | `UnconditionalBranch` | `0xA9` | `0x10A9` | = | = | 1 | SP | `[col]` shares `0xA9` w/ CmpBranch | CERTAIN |
| 80 | `BranchHint` | `0xDD` | `0x10DD` | = | = | 1 | SP | `PseudoBranchPrefetchHint`; excluded from Terminator-class eq | CERTAIN |
| 81 | `Return` | `0xD2` | `0x10D2` | = | = | 1×N | SP | `[mb:N]` broadcast 1 bundle per active engine | CERTAIN |
| 82 | `Exit` | `0xDF` | `0x10DF` | = | = | 1 | SP | `PseudoInst` | CERTAIN |
| 83 | `Break` | — | (control) | = | = | none | SP | `[low]` no standard ISA bundle | MEDIUM |
| 84 | `Call` | `0xD3` | `0x10D3` | = | = | 1×N | SP | `[mb:N]` broadcast per engine; no `sameInst` | CERTAIN |
| 85 | `SwitchQueueInstance` | `0xCF` | `0x10CF` | = | = | 1 | DMA | `PseudoDMASwapQueueSet` | CERTAIN |
| 86 | `ResetQueueInstance` | `0xC2` | `0x10C2` | = | = | 1 | DMA | `PseudoDMARearm` | CERTAIN |
| 87 | `CoreBarrier` | `0xD8` | — | `0x10D8` | = | 1 | SP | `[newV3]` `PseudoCoreBarrier` | CERTAIN |
| 88 | `Max` | `0x6C` | `0x106C` | = | = | 1 | DVE | `Max8` | CERTAIN |
| 89 | `MaxIndex` | `0x6D`+`0x6E` | `0x106D`+`0x106E` | = | = | 2 | DVE | `[mb:2][col]` shares `0x6D`+`0x6E` w/ IT91 | CERTAIN |
| 90 | `MatchReplace` | `0x6F` | `0x106F` | = | = | 1 | DVE | `MatchReplace8`; sameInst accepts {90,91} | CERTAIN |
| 91 | `MaxIndexAndMatchReplace` | `0x6D`+`0x6E` | — | `0x106D`+`0x106E` | = | 2 | DVE | `[mb:2][col][newV3]` via shared helper | HIGH |
| 92 | `Gather` | `0x67`+`0x68` | `0x1067`+`0x1068` | = | (copy) | 2 | POOL | `[mb:2]` TRN3+ → plain COPY | CERTAIN |
| 93 | `InlineASMBytes` | (user) | (user bytes) | = | = | * | any | opcode = user `byte0`; no fixed value | CERTAIN |
| 94 | `TensorCompletion` | `0xDE` | `0x10DE` | = | = | 1 | CC | `PseudoTensorCompletion` | CERTAIN |
| 95 | `MatmultMx` | `0x09`+`0x0A` | — | — | `0x1009`+`0x100A` | 2 | PE | `[split][mb:2][newV4]` ALWAYS 2 bundles | CERTAIN |
| 96 | `QuantizeMx` | `0xE3` | — | — | `0x10E3` | 1 | PE/DVE | `[newV4]` ★ `byte[1] \|= 1` on SATURATE | CERTAIN |
| 97 | `Rand2` | `0xE2` | — | — | `0x10E2` | 1 | DVE | `[newV4]` bounded RNG | CERTAIN |
| 98 | `RandSetState` | `0x78` | — | `0x1078` | = | 1 | Pool/DVE | `[newV3]` | CERTAIN |
| 99 | `RandGetState` | `0x77` | — | `0x1077` | = | 1 | Pool/DVE | `[newV3][col]` shares `0x77` w/ GetRandState IT58 | CERTAIN |
| 100 | `Rng` | `0x4D` | `0x104D` | = | = | 1 | DVE | `[col]` ★ SAME wire opcode as Memset(Random) IT10 | CERTAIN |
| 101 | `ActivationReadAccumulator` | `0x24` | `0x1024` | = | = | 1 | ACT | `[col]` shares `0x24` w/ IT5 (HEADER-only) | CERTAIN |
| 102 | `DveReadAccumulator` | `0x9B` | `0x109B` | = | = | 1 | DVE | — | CERTAIN |
| 103 | `Exponential` | `0x30` | — | — | `0x1030` | 1 | ACT | `[newV4]` fused exp+sum | CERTAIN |
| 104 | `NonzeroWithCount` | `0xF2` | — | `0x10F2` | = | 1 | DVE | `[newV3]` | CERTAIN |
| 105 | `Loop` | — | (lowered) | = | = | lo | — | `[low]` lowered to branch IR; no `sameInst` arm | HIGH |
| 106 | `DynamicForLoop` | — | (lowered) | = | = | lo | — | `[low]` lowered to branch IR | HIGH |
| 107 | `DMABlock` | — | (region) | = | = | lo | DMA | `[low]` DMA-block region carrier; no opcode | MEDIUM |
| 108 | `DoWhile` | — | (lowered) | = | = | lo | — | `[low]` lowered to branch IR | MEDIUM |
| 109 | `TongaReduceMacroSymbolic` | — | (macro) | = | = | lo | — | `[low]` macro/symbolic reduce carrier; no opcode | MEDIUM |

> **GOTCHA —** never assume `opcode ↔ IT` is one-to-one. The grid is non-injective in *both* directions: one IT can emit several L2 opcodes (`[split]`/`[mb]`), and one L2 opcode is shared by several ITs (`[col]`). A reimplementation that keys a decode table on the bare opcode byte will alias `Rng` with `Memset`, `Send` with `Recv`, the two branches, and the three DMA-direct ops. The disambiguator is always an in-bundle field, the direction bool, or the engine — see [§2](#2-l2-opcode-collisions) and [§3](#3-multi-bundle-and-split-ops).

---

## 2. L2 opcode collisions

Eight wire opcodes are emitted by more than one `InstructionType`. The silicon does **not** distinguish the colliding ITs by opcode; an in-bundle field, the direction bit, or the engine does. This is the `[col]` flag, gathered.

| L2 word | ITs that share it | Disambiguator |
|---|---|---|
| `0x104D` | `Rng`(100) == `Memset`-Random(10, mode 1) | fill immediate: Memset writes `Inst+0xF8` fill, Rng forces fill = 0 |
| `0x10CB` | `CollectiveSend`(49) == `CollectiveRecv`(50) | direction is an in-bundle field (encoder bodies are byte-identical) |
| `0x10A9` | `CompareAndBranch`(78) == `UnconditionalBranch`(79) | branch-condition presence (`CTRL_BR` struct) |
| `0x10D4` | `Load`(19) == `Save`(22) == `DMACopy`(32) | all route through `generateDynamicDMA`; an `IT==32` test re-distinguishes DMACopy |
| `0x1024` | `ReadActivationAccumulator`(5) == `ActivationReadAccumulator`(101) | distinct encoder bodies, same opcode |
| `0x1077` | `GetRandState`(58) == `RandGetState`(99) | distinct ITs, same RNG-state-read micro-op |
| `0x1069` | `StreamShuffle`(39).MASK == `StreamTranspose`(40).MASK | the *data-move* word differs (`0x6A` vs `0x6B`) |
| `0x106D`+`0x106E` | `MaxIndex`(89) == `MaxIndexAndMatchReplace`(91) | shared `generateInstMatchReplaceWithOptionalMaxIndex` helper |

> **QUIRK —** `Rng`(100) and `Memset(Random)`(10) collapse to the *same* `0x4D` DVE-RNG slot — two distinct IR ops compiling to one micro-op, separated only by whether the fill immediate is forced to zero. A decoder cannot recover which IR op produced a `0x104D` bundle from the opcode alone; that information is gone by L3.

---

## 3. Multi-bundle and `[split]` ops

### Multi-bundle ops (`[mb:n]` — one IR op → *n* wire bundles)

A reimplementation must iterate the op's **COLLECT set** (it is a `std::set<u32>`, legally holding more than one opcode), not assume one opcode per instruction.

| IT | Op | Bundle sequence | Count |
|---|---|---|---|
| 8 | `Matmult` | LdWeights `0x01` + MatMul `0x02` | 1–4 (cache-hit skips LdW → 1; fp32 2-pass → 4) |
| 9 | `MatmultSparse` | Ldtags `0x06` + LoadWeights `0x01` + MatMulSparse `0x07` | 2–5 (fp32 split → 5) |
| 95 | `MatmultMx` | LdWeightMx `0x09` + MatmulMx `0x0A` | **always 2** |
| 89 | `MaxIndex` | `0x6D` (value-load) + `0x6E` (index) | 2 |
| 91 | `MaxIndexAndMatchReplace` | `0x6D` + `0x6E` | 2 |
| 39 | `StreamShuffle` | `0x69` mask + `0x6A` data-move | 2 |
| 40 | `StreamTranspose` | `0x69` mask + `0x6B` data-move | 2 |
| 92 | `Gather` | `0x67` buffer-load + `0x68` gather | 2 |
| 53 | `CustomOp` | `0x85` header + `0x86`×(1 + num_args) | ≥2 |
| 37/38 | `BNBackprop`/`BNBackprop2` | `0x64` control + `0x65`/`0x8E` param-load | 1–2 |
| 81/84 | `Return`/`Call` | broadcast `0xD2`/`0xD3` once per active engine | 1×N |

`RangeSelect`(63) sits just outside this family. Its opcode is the single `0x10BC`, but the CoreV3 encoder may *append* a `0x9B` (`DveReadAccumulator`) drain word, so the practical emission is two wire words. It is one logical op with an accumulator-drain tail — not a `[split]`, and not a two-opcode instruction — which is why the table row carries `0xBC` alone and notes the append.

*Anchors: `generateMatMulSparse` @ `0x135d150` (stamps `0x07`), `generateLoadTagsSparse` @ `0x135e960` (stamps `0x06`) — the `MatmultSparse` pair.*

### `[split]` ops (one IT → multiple L2 opcodes selected by a runtime field)

The opcode is chosen at encode time by a sub-type discriminator, not fixed per IT:

- `TensorReduce`(27): TR `0x7C`/`0x7D` + CR `0x42`/`0x52`/`0x83`/`0x84` — selected by `(apply_transpose, isBitVec)` (6 opcodes total).
- `TensorScalar`(28)/`TensorScalarPtr`(29): `0x43`/`0x93`/`0x53`/`0x9A`/`0x9C`/`0x9E`/`0xE5`/`0x74` — one dispatcher (`generateTensorScalarOrPtr`); IT28 has no own encoder. V3/V4 widen the field bands, **not** the opcode.
- `TensorScalarCache`(30): `0x9A` (Reduce) / `0xE6` (Cumulative+Scan) by `TSCMode`.
- `TensorTensor`(31): `0x41`/`0x51` by `isBitVecInstruction` (+ GPSIMD int32 re-stamp `0x8A`).
- `BNStats`(34): `0x61`/`0x82` by `apply_transpose`. `BNGradients`(36): `0x63`/`0x94` by coeff source.
- `Memset`(10): `0x49` (Const) / `0x4D` (Random). `CollectiveCompute`(48): `0xD9`/`0xDA` (fine/coarse).
- `TensorLoad`(75): `0xCE`/`0xAA` (imm/reg). `TensorSave`(76): `0xAB`/`0xCD` (imm/reg).
- `SetRandState`(59): `0x78` (Pool) / `0xD0` (DVE) by `EngineType`.
- `TensorCopy`(23): `0x46` (no cast) → `0x47` (cast). The `[+1=cast]` low bit is decided on the **raw** src/dst dtype, not the reinterpreted one.
- `IndirectLoad`(43)/`IndirectSave`(45): `0xC4` / `0xD6` (`PseudoRangeCheck`), selected by the `isLoad` direction bool.

### The indirect load/save pair

`IndirectLoad`(43) and `IndirectSave`(45) share one encoder — `generateIndirectLoadSave` @ `0x1268c00` — but **not** one opcode. The function's `bool` direction argument gates two mutually-exclusive branches, each with its own COLLECT dword and GENERATE byte stores: the `0xC4` (196) path and the `0xD6` (214, `PseudoRangeCheck`) path. So the pair is a `[split]`, not a `[col]` collision: one IT-pair, two distinct wire opcodes chosen at encode time.

The decompile does not name which bool value maps to Load versus Save, so the *per-direction* assignment of `0xC4` and `0xD6` remains open; both immediates are byte-present, only their pairing with a direction is unpinned.

*Anchors: `0xC4` path — COLLECT dword @ `0x1268f89`, GENERATE bytes @ `0x12690cd` / `0x12694b8`; `0xD6` path — dword @ `0x1269576`, bytes @ `0x1269a6c` / `0x1269bd3`.*

---

## 4. Byte witnesses — five highest-risk rows

These five rows are the ones most likely to be wrong in a transcription (per-gen divergence, gen-gated families, the collisions). Each is pinned by the encoder body's COLLECT dword-store / GENERATE byte-store immediates and the `0x10<<8|byte` word-store, read from the libwalrus disasm sidecars (cp310, VA == file offset).

| # | Row | Binary witness | Wire result |
|---|---|---|---|
| 1 | `Activation`(4) **V2 ≠ V4** | CoreV2 `visitInstActivation` @ `0x12596f0`: `mov dword[rbp-150h],0x21` @`0x125984f`, `byte 0x21` @`0x12599fc`. CoreV4 @ `0x143bbb0`: `mov dword[rbp-B0h],0x25` @`0x143bc28`, `byte 0x25` @`0x143c25f`, `mov esi,0x1025` @`0x143c27e`. | V2/V3 = `0x1021`, V4 = `0x1025`. `[ΔV4]` genuine renumber |
| 2 | `MatmultMx`(95) two-bundle | `generateLdweightMx` @ `0x143e350`: dword `0x09` @`0x143e43b`, `mov esi,0x1009` @`0x143e4cc`. `generateMatmultMx` @ `0x143ebd0`: dword `0x0A` @`0x143ecbb`, `mov esi,0x100A` @`0x143ed5a`. | `0x1009`+`0x100A`, always 2 bundles, V4-only |
| 3 | `QuantizeMx`(96) + SATURATE | `visitInstQuantizeMx` @ `0x143dc60`: dword `0xE3` @`0x143dd4b`, byte `0xE3` @`0x143ddd2`, `mov esi,0x10E3` @`0x143ddea`; OR-s `byte[1]` to `0x11` on SATURATE. | `0x10E3`, the sole high-byte perturbation |
| 4 | `Rng`(100) / `Memset`(10) collision | `visitInstRng` @ `0x12376a0`: dword `0x4D` @`0x1237788`, byte `0x4D` @`0x12377fb`. `visitInstMemset` @ `0x125b320`: `0x4D` (Random) @`0x125b37e`, `0x49` (Const) @`0x125b530`. | both stamp `0x4D` → `0x104D` collision |
| 5 | `CollectiveSend`(49) / `Recv`(50) | `visitInstCollectiveSend` @ `0x1272440`: dword `0xCB` @`0x127252b`, byte `0xCB` @`0x12725ad`. `visitInstCollectiveRecv` @ `0x1272ab0`: dword `0xCB` @`0x1272b9b`, byte `0xCB` @`0x1272c1d`. | byte-identical encoder bodies → `0x10CB` collision |

The two DVE-engine `[newV3]` pins carry the same class of witness: `RangeSelect` CoreV3 @ `0x135f8c0` stamps `0xBC` (`0x10BC`) @`0x135f95f`; `NonzeroWithCount` CoreV3 @ `0x1355a30` stamps `0xF2` (`0x10F2`) @`0x1355b18`. Both are gen3-introduced — neither has a CoreV2 sibling.

> **GOTCHA —** `NonzeroWithCount`'s L1 ordinal is **104**, not 231. `231` (`0xE7`) is `IndirectCopy`'s *L2* opcode, and reading it as an IT is the classic cross-layer conflation INV-3 exists to prevent. This op's own numbers are the ordinary `L1(104) ≠ L2(242 = 0xF2)`.

---

## 5. Reading the table — reimplementer notes

- **Vestigial / not-emitted arms.** Of the 110 ITs, ~25 reach no `setupHeader`: the abstract bases (`MatmultBase`(7), `DMA`(18), `Collective`(47), `Terminator`(77), `DMADescriptor`(68)); the lowered-away ops (`Generic`(0), `GenericRelu`(2), `AbstractCopy`(3), `GenericCopy`(1), `TensorCopyDynamicSrc/Dst`(24/25), `GenericIndirectLoad/Save`(42/44), `Select`(51), `Loop`(105), `DynamicForLoop`(106), `DoWhile`(108), `DMABlock`(107), `TongaReduceMacroSymbolic`(109)); the kernel/debug carriers (`BIRKernel`(54), `NKIKernel`(55), `NKIKLIRKernel`(56), `DevicePrint`(57), `Break`(83)); and `Rand`(60) (shipped as `Rand2`/IT97). `InlineASMBytes`(93) has no fixed opcode — the user's first hex byte *is* the opcode.
- **No-`sameInst` arms.** `NKIKernel`(55), `NKIKLIRKernel`(56), and `Call`(84) `__assert_fail "Not Implemented"` if `sameInst` is called — CSE/dedup over those ops is unsupported by design. The five structured-control opcodes (105–109) also have no meaningful structural-eq (they are region/loop carriers). See [§sameInst](../bir/instruction-type.md#structural-equality--sameinst).
- **Per-gen reading.** Read across the V2/V3/V4 columns left to right; `=` carries the prior word forward. A `—` then a word (e.g. `RangeSelect`: `—` / `0x10BC` / `=`) marks the generation the op was introduced. The only genuine *byte renumber* of an existing op across generations is `Activation` (`0x21` → `0x25` at V4); every other V3/V4 override either keeps the opcode and only widens field bands, or is a gen-new op.
- **Confidence.** `CERTAIN` rows have a binary-pinned encoder (the ~85 concrete ITs). `HIGH` rows are transcribed from the COLLECT site without a per-byte witness (e.g. `ReadVarAddr`). `MEDIUM` rows are the lowered/abstract ITs with no encoder body — classified by the absence of a `visitInst*` body and a `sameInst` arm, consistent across both libraries.

---

## Related Components

| Component | Relationship |
|---|---|
| L1 `bir::InstructionType` | The 110-arm IR enum (`InstructionType2string` @ `0x2d5bf0`); the IT and Name columns here are its ordinal→name table |
| L2 `NEURON_ISA_TPB_OPCODE` | The engine micro-op enum the COLLECT arm records; the L2 column is its low byte |
| L3 `setupHeader` | The byte-identical 3-arch header writer; the V2/V3/V4 columns are its output words |
| `Generator` vtable | `setupHeader` is reached through slot `+0x48`; per-engine `CoreV{2,3,4}GenImpl` carry the `visitInst*` overrides |

## Cross-References

- [InstructionType](../bir/instruction-type.md) — the 110-opcode L1 enum, the opcode field at `+0x58`, and the `sameInst` structural-equality masks (the source of the IT/Name columns and the `[base]`/family flags)
- [The Opcode Master](../walrus/opcode-master.md) — `setupHeader`, the L3 wire word, and the per-op L1↔L2↔L3 reconciliation (the source of the V2/V3/V4 word columns and the INV-1..3 contract)
- [Instruction Bundle](../isa/instruction-bundle.md) — the 64-byte bundle the header word leads; `inst_word_len = 0x10` = 16 dwords
- [DVE Opcode Table](../isa/dve-opcode-table.md) — the on-device 256-slot opcode→microcode-row map the DVE sequencer indexes with the L2 opcode byte
- [Build & Version Provenance](../reference/versions.md) — the cp310/311/312 wheel pins and the VA-drift note for cross-version address translation
