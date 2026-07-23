# The Opcode Master: `setupHeader`, the L3 Wire-Opcode Word & the Per-Op Reconciliation Table

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The encoder bodies and `setupHeader` live in `neuronxcc/starfish/lib/libwalrus.so` (`.text`/`.rodata` base `0x62d660` / `0x1c72000`, VA == file offset; `.plt` thunks `0x5e9020`–`0x62d650` — cite the body, not a thunk). The defining `bir::InstructionType` enum is in `libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`). cp311/cp312 share the `.text` logic; the opcode integers and the L1/L2/L3 arithmetic are ABI-stable across cp3xx, only the VAs drift. Other wheels differ — treat every address as version-pinned. See [versions](../reference/versions.md).*

## Abstract

A BIR op carries **three** distinct integer identities, and conflating them is the most common cross-layer error when reading Walrus codegen. This page is the authoritative reconciliation of all three into one per-op master mapping:

- **L1** — the `bir::InstructionType` ordinal (`0..109`). The IR class id: *what the node is*. Defined in `libBIR.so`, recovered byte-exact from [`InstructionType2string`](../bir/instruction-type.md).
- **L2** — the `NEURON_ISA_TPB_OPCODE` enum value the `COLLECT_OPCODES` arm inserts into a `std::set<u32>`. The silicon micro-op id: *which engine micro-op the op compiles to*. Engine-relative; written as a clean dword in the COLLECT arm and as a byte in the GENERATE arm.
- **L3** — the 16-bit wire **header word** `setupHeader` stamps at `bundle[0:2]`: `(0x10 << 8) | L2byte`. The bytes that hit the [64-byte bundle](../isa/instruction-bundle.md) on the wire.

The header word is produced by one function — `setupHeader` — and the headline structural fact is that it is **byte-for-byte identical** in `CoreV2`, `CoreV3`, and `CoreV4`: six instructions, 15 bytes, no data dependence on anything but the opcode byte. There is exactly one variation point in the whole 110-op set: the opcode *byte* differs per generation for a handful of ops (most prominently **Activation: `0x21` on V2/V3 → `0x25` on V4**), and `QuantizeMx` OR-s bit 0 of `byte[1]` (`0x10 → 0x11`) on `SATURATE`. Everything else is constant.

The deliverable is the **110-row × {V2,V3,V4} opcode-word table** in [§3](#3-the-master-110-row-opcode-word-table-l1--l2--l3-per-arch), each row carrying its L1 ordinal, its L2 collect-int, its three per-arch L3 words, its bundle count, and the reconciliation flags. It is the source the per-engine encoding pages (PE matmul, Pool, Activation, …, DMA) and the planned opcode appendix cite for *which 16-bit word lands at `bundle[0:2]`*.

| | |
|---|---|
| **Header writer** | `CoreV2/V3/V4GenImpl::setupHeader` @ `0x1172120` / `0x1369280` / `0x143f440` — byte-identical |
| **Header word** | `bundle[0:4] = {opcode, 0x10, 0x00, 0x00}`; `*(u16*)bundle = 0x10NN` |
| **`inst_word_len`** | `byte[1] = 0x10` = 16 dwords = 64 B, hardcoded immediate, no non-16 length exists |
| **L3 word rule (INV-1)** | `L3word = (0x10 << 8) \| L2byte`; `QuantizeMx` sets `byte[1] \|= 1` on `SATURATE` |
| **L1 ≠ L2 (INV-3)** | the IT ordinal and the opcode int are **separate enums**; equal for no op |
| **Dispatch** | reached as a virtual through the `Generator` vtable slot **+0x48** (slot 9) |
| **Per-arch diff** | Activation byte `0x21` (V2/V3) → `0x25` (V4); else opcode byte is arch-stable |

---

## 1. `setupHeader` — byte-identical across all three generations

`setupHeader(void* this, void* bundle, void* opcodeByte)` writes the 4-byte header word. At all three nm-resolved addresses the bodies are identical to the byte (`0f b6 02 c6 46 01 10 88 06 31 c0 66 89 46 02 c3`):

```c
// CoreV2GenImpl::setupHeader @0x1172120 — byte-identical to
// CoreV3 @0x1369280 and CoreV4 @0x143f440; only the entry RVA differs.
// System V: rdi=this, rsi=&bundle (64-B buffer), rdx=&opcodeByte.
void setupHeader(void* this_, uint8_t* bundle, const uint8_t* opcodeByte) {
    uint8_t op = *opcodeByte;        // 1172120  movzbl (%rdx),%eax
    bundle[1]  = 0x10;               // 1172123  movb   $0x10,0x1(%rsi)  -- inst_word_len = 16 dwords
    bundle[0]  = op;                 // 1172127  mov    %al,(%rsi)       -- the L3 opcode byte
    *(uint16_t*)(bundle + 2) = 0;    // 117212b  mov    %ax,0x2(%rsi)    (eax already 0 via xor)
    // 1172129  xor %eax,%eax  ;  117212f  ret
}
```

The 4-byte header it produces:

| offset | width | value | meaning |
|---|---|---|---|
| `+0x00` | byte | opcode byte | the per-op L3 ISA opcode (the L2 low byte) — **not** the BIR IT ordinal |
| `+0x01` | byte | `0x10` | `inst_word_len` = 16 dwords = 64-byte bundle — **hardcoded immediate**, zero data dependence |
| `+0x02` | word | `0x0000` | format / reserved |

Read little-endian, `*(u16*)bundle = 0x10NN` where `NN` is the opcode byte. **Are there ANY non-16 `inst_word_len` values?** No. `byte[1]` is the literal `0x10` in all three bodies with no data dependence; every `CoreV{2,3,4}` bundle is exactly 64 bytes (`emplace_back<std::array<u8,64>>` + `fwrite(…, 0x40, …)`). The MX opcodes `0x1009 / 0x100A / 0x10E3` are **full words**: their high byte `0x10` *is* the length, the low byte (`0x09 / 0x0A / 0xE3`) is the opcode. The only op that perturbs the high byte is `QuantizeMx`, which OR-s bit 0 (`0x10 → 0x11`) when its `SATURATE` flag is set.

**Dispatch.** `setupHeader` is a virtual reached through the `Generator` vtable slot at `+0x48` (slot 9). In the canonical encoder body (the `CoreV2` `Rng` visitor `visitInstRng` @`0x12376a0`), the call site is `call *0x48(%rax)` @`0x1237802`, immediately after the opcode byte is materialized on the stack. The few MX ops write the word directly (`mov $0x10NN,%esi ; mov %si,(bundle)`) instead of calling `setupHeader`, but the wire bytes are byte-identical to the `{opcode, 0x10, 0, 0}` skeleton.

---

## 2. The emission skeleton — how a `visitInst` reaches `setupHeader`

Every `CoreV*` encoder shares one skeleton; the per-op deltas are body-field writes only. The codegen mode is read from `*((int*)this + 156)` (`[this+0x270]`):

| mode | name | behaviour | opcode witness |
|---|---|---|---|
| `0` | `COLLECT_OPCODES` | inserts the opcode **int** into a per-inst `std::set<u32>` in `DenseMap<Inst,set<u32>>` @`Generator+0x20`; **no bundle emitted** | clean `movl $op,-X(%rbp)` **dword** store — the L2 witness |
| `1` | `GENERATE_ISACODE` | `emplace_back<std::array<u8,64>>` → `memset 64→0` → `movb $op` → `setupHeader(this,&v,&op)` → fill body → `fwrite(v,1,0x40,…)`; bumps the per-opcode histogram @`Generator+0x1E0` and per-engine count @`+0x1C8` | `movb $op,-Y(%rbp)` **byte** store fed to `setupHeader` |
| `2` | `RUN_ISA_CHECKS` | builds the bundle in scratch + runs `runSingleISACheck`; no `fwrite` | (same field writes as mode 1) |
| else | error | `reportError "Wrong CodeGenMode…"` | — |

Each opcode therefore leaves a dual witness in its encoder body: `movl $op,-X(%rbp)` (mode 0) **and** `movb $op,-Y(%rbp)` (mode 1, immediately before `mov %rax,…+0x48` / `call *0x48(%rax)`). Both witnesses are present and agree for `Pool 0x45`, `Activation(V2) 0x21`, `Activation(V4) 0x25`, `QuantizeMx 0xE3`, `Rng 0x4D`, `CollectiveSend/Recv 0xCB`, `MaxIndex 0x6D+0x6E`, `RangeSelect 0xBC`, `NonzeroWithCount 0xF2`, and the MX word stores; the opcodes in [§3](#3-the-master-110-row-opcode-word-table-l1--l2--l3-per-arch) are read from that pair.

> **The engine is NOT a wire field.** The physical engine rides the BIR `Instruction` at `+0x90` (`EngineType`), pinned by I-strand codegen. The encoder only *reads* it for the census `DenseMap` key. The **Eng** column below is metadata, not a bundle field. Wire engine ordinals: `{PE=0, ACT=1, POOL=2, DVE=3, SP=4}`.

### Class hierarchy & the per-arch override rule

`Generator → CoreV2Gen/CoreV2GenImpl → CoreV3Gen/CoreV3GenImpl → CoreV4Gen/CoreV4GenImpl`. Target is selected by `std::variant<monostate, CoreV2Gen, CoreV3Gen, CoreV4Gen>`. **The opcode for an op is the opcode in the most-derived override that exists for the running target**; absent an override the base (`CoreV2`) opcode is inherited. The override rosters (nm-counted):

- `CoreV2GenImpl::visitInst*` — **68** distinct bodies (the full base set).
- `CoreV3GenImpl::visitInst*` — **13** overrides: `TensorTensor, Matmult, MatmultSparse, CoreBarrier, GPSIMDSB2SB, GetSequenceBounds, RangeSelect, MaxIndexAndMatchReplace, NonzeroWithCount, GetRandState, RandGetState, RandSetState, SetRandState`.
- `CoreV4GenImpl::visitInst*` — **5** overrides: `Activation, Exponential, MatmultMx, QuantizeMx, Rand2`.

Critically, **most overrides keep the same opcode byte** — the override only extends the int32 / extended-engine field bands (e.g. the `CoreV3` DVE overrides `TensorTensor`, `RangeSelect`, `MaxIndexAndMatchReplace`, `NonzeroWithCount`). The genuine per-arch *opcode renumber* is rare; see [§4(b)](#b-per-arch-l3-differences--v4-v3-newvn).

---

## 3. The master 110-row opcode-word table (L1 | L2 | L3 per arch)

The headline deliverable. Opcode words shown as `0x10NN` (`NN` = L2 low byte). **V2word** = `CoreV2` wire word; **V3word**/**V4word**: `=` inherits the prior arch verbatim, `—` = not encoded on that arch (gen-gated), `(lowered)` = no encoder. **#B** = 64-byte bundles this one BIR op emits. **Flags**: `[≠]` L1≠L2, `[COLn]` N ITs collide on one L2, `[ΔVn]` per-arch opcode renumber, `[NEWvn]` gen-new op, `[MB:n]` multi-bundle, `[SPLIT]` one IT forks opcodes by a runtime flag, `[LOW]` lowered / no encoder, `[BASE]` abstract base, `[+1=cast]` low bit = dtype-cast flag.

| IT | Name | L2(coll) hex/dec | V2word | V3word | V4word | #B | Eng | Flags / note |
|---|---|---|---|---|---|---|---|---|
| 0 | Generic | — | (lowered) | = | = | lo | — | `[LOW][BASE]` LowerAC |
| 1 | GenericCopy | — | (lowered) | = | = | lo | — | `[LOW]` no `visitInstGenericCopy` body |
| 2 | GenericRelu | — | (lowered) | = | = | lo | — | `[LOW]` → Activation |
| 3 | AbstractCopy | — | (lowered) | = | = | lo | — | `[LOW]` LowerAC pass |
| 4 | **Activation** | `0x21`/33 | `0x1021` | = | **`0x1025`** | 1 | ACT | `[≠][ΔV4]` ★ V4 byte `0x25` ≠ V2/V3 `0x21` |
| 5 | ReadActivationAccumulator | `0x24`/36 | `0x1024` | = | = | 1 | ACT | `[≠][COL2]` shares `0x24` w/ IT101 |
| 6 | LoadActFuncSet | `0x23`/35 | `0x1023` | = | = | 1 | ACT | `[≠]` `ActivationTableLoad` |
| 7 | MatmultBase | — | (base) | (base) | (base) | — | PE | `[BASE]` base of {7,8,9,95} |
| 8 | Matmult | {1,2} | `0x1001+0x1002` | = | = | 1–4 | PE | `[≠][SPLIT][MB:2]` LdWeights `0x01`+MatMul `0x02`; fp32→4 |
| 9 | MatmultSparse | {6,7}(,1) | — | `0x1006+0x1007` | = | 2–5 | PE | `[≠][SPLIT][MB:2+][NEWv3]` +LoadWeights `0x01`; fp32→5 |
| 10 | Memset | 73/77 | `0x1049/0x104D` | = | = | 1 | DVE | `[≠][SPLIT][COL2]` `0x49` Const / `0x4D` Random (==Rng IT100) |
| 11 | GetGlobalRankId | `0xDC`/220 | `0x10DC` | = | = | 1 | CC | `[≠]` `PseudoGidLoad` |
| 12 | NoOp | `0xA4`/164 | `0x10A4` | = | = | 1 | SP | `[≠]` `Nop` |
| 13 | EventSemaphore | `0xA0`/160 | `0x10A0` | = | = | 1 | SP | `[≠]` |
| 14 | GroupResetSemaphores | `0xB0`/176 | `0x10B0` | = | = | 1 | SP | `[≠]` `EventSemaphoreRangeClear` (NOT IT13) |
| 15 | AllEngineBarrier | `0xD5`/213 | `0x10D5` | = | = | 1 | SP | `[≠]` `PseudoSyncBarrier` |
| 16 | Drain | `0xA2`/162 | `0x10A2` | = | = | 1 | SP | `[≠]` |
| 17 | Halt | `0xA1`/161 | `0x10A1` | = | = | 1 | SP | `[≠]` |
| 18 | DMA | — | (base) | = | = | — | DMA | `[BASE]` accepts {18,19,22,32,41–46,67} |
| 19 | Load | `0xD4`/212 | `0x10D4` | = | = | 1 | DMA | `[≠][COL3]` →`generateDynamicDMA` (==Save,DMACopy) |
| 20 | Pool | `0x45`/69 | `0x1045` | = | = | 1 | POOL | `[≠]` |
| 21 | Reciprocal | `0x48`/72 | `0x1048` | = | = | 1 | DVE | `[≠]` |
| 22 | Save | `0xD4`/212 | `0x10D4` | = | = | 1 | DMA | `[≠][COL3]` →`generateDynamicDMA` |
| 23 | TensorCopy | `0x46`/`0x47` | `0x1046/0x1047` | = | = | 1 | DVE | `[≠][SPLIT][+1=cast]` `0x47` = dtype change |
| 24 | TensorCopyDynamicSrc | — | (lowered) | = | = | lo | — | `[LOW]` lowered before CoreV2 |
| 25 | TensorCopyDynamicDst | — | (lowered) | = | = | lo | — | `[LOW]` lowered before CoreV2 |
| 26 | IndirectCopy | `0xE7`/231 | `0x10E7` | = | (copy) | 1 | POOL | `[≠]` V2-only; → COPY on V3+ |
| 27 | TensorReduce | var | `0x107C/0x107D` + `0x1042/0x1052/0x1083/0x1084` | = | = | 1 | Pool/DVE | `[≠][SPLIT]` TR 124/125 + CR 66/82/131/132 (6 opcodes) |
| 28 | TensorScalar | var | `0x1043/0x1093/0x1053/0x109A` | (ext) | (ext) | 1 | DVE | `[≠][SPLIT]` no own enc; shares IT29 `OrPtr` |
| 29 | TensorScalarPtr | var | `0x1043/93/53/9A/9C/9E/E5/74` | (ext) | (ext) | 1 | DVE | `[≠][SPLIT]` dispatcher: OrPtr{43/93/53/9A}+STT{9C/9E/E5}+Addr 74; V3/V4 widen fields not opcode |
| 30 | TensorScalarCache | `0x9A`/`0xE6` | `0x109A/0x10E6` | (ext) | (ext) | 1 | DVE | `[≠][SPLIT]` `0x9A` Reduce / `0xE6` Cumul+Scan by `TSCMode` |
| 31 | TensorTensor | 65/81 | `0x1041/0x1051` | `0x1041/51` | = | 1 | DVE | `[≠][SPLIT]` +GPSIMD int32 re-stamp `0x8A`; V3 = field ext only |
| 32 | DMACopy | `0xD4`(/`0xDA`) | `0x10D4(/0x10DA)` | = | = | 1 | DMA | `[≠][COL3]` →`generateDynamicDMA`; `IT==32` test re-distinguishes |
| 33 | GPSIMDSB2SB | `0xBF`/191 | — | `0x10BF` | = | 1 | GPSIMD | `[≠][NEWv3]` `Sb2sbCollective` |
| 34 | BNStats | `0x61`/`0x82` | `0x1061/0x1082` | = | = | 1 | POOL | `[≠][SPLIT]` by `apply_transpose` |
| 35 | BNStatsAggregate | `0x62`/98 | `0x1062` | = | = | 1 | POOL | `[≠]` `BatchNormAggregate` |
| 36 | BNGradients | `0x63`/`0x94` | `0x1063/0x1094` | = | = | 1 | POOL | `[≠][SPLIT]` `0x63` AP-coeff / `0x94` register-coeff |
| 37 | BNBackprop | `0x64`(+`0x65`) | `0x1064(+0x1065)` | = | = | 1–2 | POOL | `[≠][MB:2]` BACK_PROP `0x64` + PARAM_LOAD `0x65` |
| 38 | BNBackprop2 | `0x64`(+`0x8E`) | `0x1064(+0x108E)` | = | = | 1–2 | POOL | `[≠][MB:2]` shares `0x64`; V2 visit @`0x12368e0` stamps `0x8E` param |
| 39 | StreamShuffle | `0x69`+`0x6A` | `0x1069+0x106A` | = | = | 2 | DVE | `[≠][MB:2][COL2]` mask `0x69` (==StreamTranspose) |
| 40 | StreamTranspose | `0x69`+`0x6B` | `0x1069+0x106B` | = | = | 2 | DVE | `[≠][MB:2][COL2]` mask `0x69` (==StreamShuffle) |
| 41 | ReadVarAddr | `0xB2`/178 | `0x10B2` | = | = | 1 | DMA | `[≠]` `generateMoveShape`; opcode read HIGH |
| 42 | GenericIndirectLoad | — | (lowered→43) | = | = | lo | — | `[LOW]` → IndirectLoad |
| 43 | IndirectLoad | `0xC4`/`0xD6` | `0x10C4/0x10D6`* | = | = | 1 | DMA | `[≠][SPLIT][COL2]` `generateIndirectLoadSave`; `0xC4` bound-checked / `0xD6` plain; see NOTE-A |
| 44 | GenericIndirectSave | — | (lowered→45/46) | = | = | lo | — | `[LOW]` → IndirectSave(Accum) |
| 45 | IndirectSave | `0xC4`/`0xD6` | `0x10C4/0x10D6`* | = | = | 1 | DMA | `[≠][SPLIT][COL2]` `generateIndirectLoadSave(isLoad)`; `0xC4` bound-checked / `0xD6` plain; see NOTE-A |
| 46 | IndirectSaveAccumulate | `0xCA`/202 | `0x10CA` | = | = | 1 | CCE/Pool/Act | `[≠]` scatter-add (`PseudoEmbeddingUpdate`) |
| 47 | Collective | — | (base) | = | = | — | POOL | `[BASE]` base of {47,48,49,50} |
| 48 | CollectiveCompute | `0xD9`/`0xDA` | `0x10D9/0x10DA` | = | = | 1 | POOL | `[≠][SPLIT]` fine `0xD9` / coarse `0xDA` |
| 49 | CollectiveSend | `0xCB`/203 | `0x10CB` | = | = | 1 | POOL | `[≠][COL2]` shares `0xCB` w/ Recv; direction = field |
| 50 | CollectiveRecv | `0xCB`/203 | `0x10CB` | = | = | 1 | POOL | `[≠][COL2]` shares `0xCB` w/ Send |
| 51 | Select | — | (lowered) | = | = | lo | DVE | `[LOW]` → GenericCopy + CopyPredicated(`0xEA`) |
| 52 | CopyPredicated | `0xEA`/234 | `0x10EA` | = | = | 1 | DVE | `[≠]` the masked-copy / SELECT emitter |
| 53 | CustomOp | `0x85`+`0x86` | `0x1085+0x1086` | = | = | ≥2 | any | `[≠][MB:≥2]` header `0x85` + `0x86`×(1+num_args) |
| 54 | BIRKernel | — | (no ISA enc) | = | = | none | — | `[LOW]` kernel container |
| 55 | NKIKernel | — | (no ISA enc) | = | = | none | — | `[LOW]` "Not Implemented" |
| 56 | NKIKLIRKernel | — | (no ISA enc) | = | = | none | — | `[LOW]` "Not Implemented" |
| 57 | DevicePrint | — | (debug-meta) | = | = | none | — | `[LOW]` emits `instruction_debug_info` protobuf, NO `setupHeader` |
| 58 | GetRandState | `0x77`/119 | `0x1077` | `0x1077` | = | 1 | DVE/Pool | `[≠][COL2]` shares `0x77` w/ IT99; V3 ovr same op |
| 59 | SetRandState | `0xD0`(/`0x78`) | `0x10D0` | `0x1078/0x10D0` | = | 1 | DVE/Pool | `[≠][SPLIT]` V2=DVE `0xD0`; V3 Pool-arm `0x78` / DVE-arm `0xD0` |
| 60 | Rand | — | (no V2 enc) | (no enc) | see Rand2 | — | DVE | `[LOW]` IT60 has no encoder; Rand2(IT97) is the gen form |
| 61 | Iota | `0x7E`/126 | `0x107E` | = | = | 1 | DVE | `[≠]` |
| 62 | TensorScalarAffineSelect | `0x92`/146 | `0x1092` | (ext) | (ext) | 1 | DVE | `[≠]` fixed `0x92`; 4-D mask + 2-bit compare |
| 63 | RangeSelect | `0xBC`/188 | — | `0x10BC` | = | 1 | DVE | `[≠][NEWv3]` +`0x9B` DVE_READ_ACC append (NOTE-B) |
| 64 | GetSequenceBounds | `0xBE`/190 | — | `0x10BE` | = | 1 | GPSIMD | `[≠][NEWv3]` |
| 65 | Dropout | `0x7F`/127 | `0x107F` | = | = | 1 | DVE | `[≠]` |
| 66 | GetCurProcessingRankID | `0xDB`/219 | `0x10DB` | = | = | 1 | CC | `[≠]` pairs w/ GetGlobalRankId |
| 67 | DMATrigger | `0xC1`/193 | `0x10C1` | = | = | 1 | DMA | `[≠]` `PseudoDMATrigger` |
| 68 | DMADescriptor | — | (no V2 enc) | = | = | none | DMA | `[LOW][BASE]` base of {68..72} |
| 69 | DMADescriptorCopy | — | (no V2 enc) | = | = | none | DMA | `[LOW]` |
| 70 | DMADescriptorCCE | — | (no V2 enc) | = | = | none | DMA | `[LOW]` |
| 71 | DMADescriptorTranspose | — | (no V2 enc) | = | = | none | DMA | `[LOW]` |
| 72 | DMADescriptorReplicate | — | (no V2 enc) | = | = | none | DMA | `[LOW]` |
| 73 | RegisterAlu | `0xA8`/168 | `0x10A8` | = | = | 1 | SP | `[≠]` `AluOp` |
| 74 | RegisterMove | `0xA7`/167 | `0x10A7` | = | = | 1 | SP | `[≠]` `Move` |
| 75 | TensorLoad | `0xCE`/`0xAA` | `0x10CE/0x10AA` | = | = | 1 | scalar | `[≠][SPLIT]` `0xCE` imm-addr / `0xAA` reg-mode |
| 76 | TensorSave | `0xAB`/`0xCD` | `0x10AB/0x10CD` | = | = | 1 | scalar | `[≠][SPLIT]` `0xAB` imm-addr / `0xCD` reg-mode |
| 77 | Terminator | — | (base) | = | = | — | SP | `[BASE]` base of {77,78,79,81,82}; rejects 80 |
| 78 | CompareAndBranch | `0xA9`/169 | `0x10A9` | = | = | 1 | SP | `[≠][COL2]` shares `0xA9` w/ UncondBranch |
| 79 | UnconditionalBranch | `0xA9`/169 | `0x10A9` | = | = | 1 | SP | `[≠][COL2]` shares `0xA9` w/ CmpBranch |
| 80 | BranchHint | `0xDD`/221 | `0x10DD` | = | = | 1 | SP | `[≠]` `PseudoBranchPrefetchHint` |
| 81 | Return | `0xD2`/210 | `0x10D2` | = | = | 1×N | SP | `[≠][MB:N]` broadcast 1 bundle / active engine |
| 82 | Exit | `0xDF`/223 | `0x10DF` | = | = | 1 | SP | `[≠]` `PseudoInst` (PSEUDO_EXIT) |
| 83 | Break | — | (control) | = | = | none | SP | `[LOW]` no standard ISA bundle |
| 84 | Call | `0xD3`/211 | `0x10D3` | = | = | 1×N | SP | `[≠][MB:N]` broadcast per engine |
| 85 | SwitchQueueInstance | `0xCF`/207 | `0x10CF` | = | = | 1 | DMA | `[≠]` `PseudoDMASwapQueueSet` |
| 86 | ResetQueueInstance | `0xC2`/194 | `0x10C2` | = | = | 1 | DMA | `[≠]` `PseudoDMARearm` |
| 87 | CoreBarrier | `0xD8`/216 | — | `0x10D8` | = | 1 | SP | `[≠][NEWv3]` `PseudoCoreBarrier` |
| 88 | Max | `0x6C`/108 | `0x106C` | = | = | 1 | DVE | `[≠]` `Max8` |
| 89 | MaxIndex | `0x6D`+`0x6E` | `0x106D+0x106E` | = | = | 2 | DVE | `[≠][MB:2][COL2]` shares `0x6D+0x6E` w/ IT91 |
| 90 | MatchReplace | `0x6F`/111 | `0x106F` | = | = | 1 | DVE | `[≠]` `MatchReplace8`; sameInst accepts {90,91} |
| 91 | MaxIndexAndMatchReplace | `0x6D`+`0x6E` | — | `0x106D+0x106E` | = | 2 | DVE | `[≠][MB:2][COL2][NEWv3]` shared helper |
| 92 | Gather | `0x67`+`0x68` | `0x1067+0x1068` | = | (copy) | 2 | POOL | `[≠][MB:2]` TRN3+ → plain COPY |
| 93 | InlineASMBytes | (user) | (user bytes) | = | = | * | any | `[≠]` opcode = user byte0; no fixed value |
| 94 | TensorCompletion | `0xDE`/222 | `0x10DE` | = | = | 1 | CC | `[≠]` `PseudoTensorCompletion` |
| 95 | MatmultMx | {9,10} | — | — | `0x1009+0x100A` | 2 | PE | `[≠][SPLIT][MB:2][NEWv4]` ALWAYS 2 bundles |
| 96 | QuantizeMx | `0xE3`/227 | — | — | `0x10E3` | 1 | PE/DVE | `[≠][NEWv4]` byte1`\|=`1 on SATURATE |
| 97 | Rand2 | `0xE2`/226 | — | — | `0x10E2` | 1 | DVE | `[≠][NEWv4]` bounded RNG |
| 98 | RandSetState | `0x78`/120 | — | `0x1078` | = | 1 | Pool/DVE | `[≠][NEWv3]` |
| 99 | RandGetState | `0x77`/119 | — | `0x1077` | = | 1 | Pool/DVE | `[≠][NEWv3][COL2]` shares `0x77` w/ GetRandState IT58 |
| 100 | Rng | `0x4D`/77 | `0x104D` | = | = | 1 | DVE | `[≠][COL2]` ★ SAME wire opcode as Memset(Random) IT10 |
| 101 | ActivationReadAccumulator | `0x24`/36 | `0x1024` | = | = | 1 | ACT | `[≠][COL2]` shares `0x24` w/ IT5 (HEADER-only) |
| 102 | DveReadAccumulator | `0x9B`/155 | `0x109B` | = | = | 1 | DVE | `[≠]` |
| 103 | Exponential | `0x30`/48 | — | — | `0x1030` | 1 | ACT | `[≠][NEWv4]` fused exp+sum |
| 104 | NonzeroWithCount | `0xF2`/242 | — | `0x10F2` | = | 1 | DVE | `[≠][NEWv3]` IT=104, L2=`0xF2` (NOTE-C) |
| 105 | Loop | — | (lowered) | = | = | lo | — | `[LOW]` lowered to branch IR |
| 106 | DynamicForLoop | — | (lowered) | = | = | lo | — | `[LOW]` lowered to branch IR |
| 107 | DMABlock | — | (region) | = | = | lo | DMA | `[LOW]` region carrier; no opcode. Encoder absence SPECULATIVE |
| 108 | DoWhile | — | (lowered) | = | = | lo | — | `[LOW]` lowered to branch IR. Encoder absence SPECULATIVE |
| 109 | TongaReduceMacroSymbolic | — | (macro) | = | = | lo | — | `[LOW]` macro/symbolic reduce carrier. Encoder absence SPECULATIVE |

**NOTE-A (IndirectLoad/Save L2 byte) — a split, not a single value.** `generateIndirectLoadSave` emits **two** different opcodes, chosen at runtime by the bound-check flag at `Inst+0x1ED`. With the flag set the op encodes as `0xC4` (range/bound-checked); that arm is guarded by the diagnostic `"Indirect DMA bound-check can only be on Pool engine"`, so bound-checked indirect DMA is Pool-only. With the flag clear it encodes as `0xD6` (plain). Both values reach the per-instruction `std::set<u32>` through ordinary COLLECT-arm `_M_insert_unique` calls in the same function, which is why the op carries `[SPLIT]` rather than one L2 byte.

*Anchors: `generateIndirectLoadSave` @ `0x1268c00`; flag test `cmp byte ptr [rax+0x1ED],0` @ `0x1268d0c`/`0x1268d13`; `movl $0xC4,-0xB0(%rbp)` @ `0x1268f89` + `_M_insert_unique` @ `0x1268f96`; `movl $0xD6,-0xB0(%rbp)` @ `0x1269576` + `_M_insert_unique` @ `0x1269583`.*

**NOTE-B (RangeSelect second word).** The V3 `RangeSelect` encoder @ `0x135f8c0` may **append** a `0x9B` (`DveReadAccumulator`) drain word, so the practical emission is 2 words: primary `0xBC` (188) + accumulator-drain `0x9B`. The op opcode itself is `0xBC`; the appended drain word is a HIGH-confidence read.

**NOTE-C (NonzeroWithCount).** L1 IT = **104**, agreed by both `InstructionType2string` and the `sameInst` arm; L2/L3 opcode = `0xF2` (242).

> **GOTCHA —** Do not read `NonzeroWithCount` as "bir 231". `231 = 0xE7` is `IndirectCopy`'s L2 opcode, not this op's instruction-type ordinal; there is no 231-vs-242 IT split.

IT 5 `ReadActivationAccumulator` and IT 101 `ActivationReadAccumulator` are two distinct read-accumulator bodies (`visitInstReadActivationAccumulator` @`0x125b830`, `visitInstActivationReadAccumulator` @`0x1234f70`) that both stamp `0x24`.

---

## 4. Cross-cutting flag rosters

### (a) L2 non-injective collisions `[COLn]` — distinct ITs → ONE L2 opcode

The wire opcode is **never** a unique per-IT key; these collisions are real and intentional, disambiguated by bundle fields / direction / engine:

| L2 word | ITs | distinguisher |
|---|---|---|
| `0x104D` | Rng(100) == Memset-Random(10) | fill imm: Memset writes `Inst+0xF8` fill, Rng forces fill=0 |
| `0x10CB` | CollectiveSend(49) == CollectiveRecv(50) | direction is an in-bundle field (encoder bodies byte-identical) |
| `0x10A9` | CompareAndBranch(78) == UnconditionalBranch(79) | branch-condition presence |
| `0x10D4` | Load(19) == Save(22) == DMACopy(32) | `IT==32` test inside `generateDynamicDMA` — 3-way |
| `0x10D6` / `0x10C4` | IndirectLoad(43) == IndirectSave(45) | `isLoad` bool arg; each also forks `0xC4` (bound-checked) vs `0xD6` (plain) on `Inst+0x1ED` — NOTE-A |
| `0x1024` | ReadActivationAccumulator(5) == ActivationReadAccumulator(101) | distinct bodies |
| `0x1077` | GetRandState(58) == RandGetState(99) | — |
| `0x1069` | StreamShuffle(39).MASK == StreamTranspose(40).MASK | data-move word differs (`0x6A` vs `0x6B`) |
| `0x106D+0x106E` | MaxIndex(89) == MaxIndexAndMatchReplace(91) | shared helper |

**NET: 9 collision groups.** A reimplementation must never assume opcode↔IT is 1:1.

### (b) Per-arch L3 differences `[ΔV4]` / `[ΔV3]` / `[NEWvn]`

- **Genuine opcode renumber** (different byte, same op, across arch): **Activation IT4 — V2=`0x21` / V3=`0x21`(inh) / V4=`0x25`.** This is the single clearest V2→V4 opcode-byte divergence; both bytes are byte-verified in the binary ([§5](#5-evidence-anchors-and-limits)). The V4 body opens with an act-accumulator-mode `[rax+0x128]` test (the fused-activation slot).
- **GEN3-NEW** (no CoreV2 sibling → abs→opcode at V3): MatmultSparse(9) `0x06+0x07`, GPSIMDSB2SB(33) `0xBF`, RangeSelect(63) `0xBC`, GetSequenceBounds(64) `0xBE`, CoreBarrier(87) `0xD8`, MaxIndexAndMatchReplace(91) `0x6D+0x6E`, RandSetState(98) `0x78`, RandGetState(99) `0x77`, NonzeroWithCount(104) `0xF2`, SetRandState(59) Pool-arm `0x78`.
- **GEN4-NEW** (abs→opcode at V4): MatmultMx(95) `0x09+0x0A`, QuantizeMx(96) `0xE3`, Rand2(97) `0xE2`, Exponential(103) `0x30`, plus the Activation(4) re-emit `0x25`.
- **Field-extension ONLY** (opcode UNCHANGED — V3/V4 widen the int32 / extended bands; **do not mistake for renumbers**): TensorTensor(31), TensorScalar/Ptr(28/29), TensorScalarCache(30), RangeSelect/NonzeroWithCount field bands.

### (c) Multi-bundle ops `[MB:n]` — one IT → n wire words

| IT | op | bundles |
|---|---|---|
| 8 | Matmult | LdWeights `0x01` + MatMul `0x02` (cache-hit may skip LdW → 1; fp32 2-pass → 4) |
| 9 | MatmultSparse | Ldtags `0x06` + LoadWeights `0x01` + MatMulSparse `0x07` (fp32 → 5) |
| 95 | MatmultMx | LdWeightMx `0x09` + MatmulMx `0x0A` = always 2 |
| 89 / 91 | MaxIndex / MaxIndexAndMatchReplace | `0x6D` + `0x6E` = 2 (value-load + index) |
| 39 / 40 | StreamShuffle / StreamTranspose | `0x69` mask + `0x6A`/`0x6B` data-move = 2 |
| 92 | Gather | `0x67` buffer-load + `0x68` gather = 2 |
| 53 | CustomOp | `0x85` header + `0x86`×(1+num_args) = ≥2 |
| 37 / 38 | BNBackprop / BNBackprop2 | `0x64` control + `0x65`/`0x8E` param-load = up to 2 |
| 81 / 84 | Return / Call | broadcast 1×`0xD2`/`0xD3` per active engine = 1×N |

**NET: 11 multi-bundle ops.** A reimplementation MUST iterate the op's COLLECT *set* (a `std::set<u32>` legally holding >1 opcode), not assume one opcode per instruction.

### (d) Lowered-away / no-encoder ITs `[LOW]`

- **Abstract / lowered (LowerAC etc.):** Generic(0), GenericRelu(2), AbstractCopy(3), GenericCopy(1), TensorCopyDynamicSrc(24), TensorCopyDynamicDst(25), GenericIndirectLoad(42), GenericIndirectSave(44), Select(51→GenericCopy+`0xEA`), Loop(105), DynamicForLoop(106), DoWhile(108), DMABlock(107), TongaReduceMacroSymbolic(109).
- **Kernel / container (no ISA bundle):** BIRKernel(54), NKIKernel(55), NKIKLIRKernel(56).
- **Debug / control (no `setupHeader`):** DevicePrint(57, protobuf only), Break(83).
- **No CoreV\* encoder (other op ships):** Rand(60 → Rand2/IT97).
- **Abstract bases (leaves carry opcodes):** MatmultBase(7), DMA(18), Collective(47), Terminator(77), DMADescriptor(68) + family (69–72).
- **User-defined opcode:** InlineASMBytes(93) — opcode = user byte0.

**NET: ~25 ITs with no own fixed opcode.** Of the 110 ITs, ~85 reach a concrete L2/L3 opcode (counting field-split forks once).

---

## 5. Evidence anchors and limits

For every opcode below the COLLECT dword store (mode 0) and the GENERATE byte store (mode 1) carry the same low value, and the L3 word equals `0x10<<8 | byte`.

| # | claim | binary anchor | confidence |
|---|---|---|---|
| 1 | `setupHeader` byte-identical 3-arch | `0x1172120`/`0x1369280`/`0x143f440` all = `0f b6 02 c6 46 01 10 88 06 31 c0 66 89 46 02 c3` (15 B) | CERTAIN |
| 2 | Activation V2=`0x21`, V4=`0x25` (★ renumber) | V2 `movl $0x21,-0x150(%rbp)` @`0x125984f`; V4 `movl $0x25,-0xb0(%rbp)` @`0x143bc28`, byte `movb $0x25,-0xf0(%rbp)` @`0x143c25f`, word `mov $0x1025,%r8d` @`0x143c27e` | CERTAIN |
| 3 | QuantizeMx = `0x10E3` | `movb $0xe3,-0xd0(%rbp)` @`0x143ddd2`; `mov $0x10e3,%esi` @`0x143ddea` | CERTAIN |
| 4 | RangeSelect(63)=`0xBC`, NonzeroWithCount(104)=`0xF2` (gen3-new) | RangeSelect `movl $0xbc,-0x180(%rbp)` @`0x135f95f`; NonzeroWithCount `movl $0xf2` @`0x1355b18` + `movb $0xf2` @`0x1355b87` | CERTAIN |
| 5 | Send==Recv `0xCB` collision (COL2) | CollectiveSend `movl $0xcb,-0xb0(%rbp)` @`0x127252b`; CollectiveRecv `movl $0xcb` @`0x1272b9b` + `movb $0xcb` @`0x1272c1d` — byte-identical | CERTAIN |

Further byte-exact anchors: Pool `movl $0x45` @`0x1239f51`; Rng `movl $0x4d` @`0x1237788`; MaxIndex two arms in one body `movl $0x6d` @`0x12546d3` + `movl $0x6e` @`0x1254732`; MX words `mov $0x1009,%esi` @`0x143e4cc` + `mov $0x100a,%esi` @`0x143ed5a`; the `+0x48` vtable dispatch `call *0x48(%rax)` @`0x1237802`.

**Limits.** Directly pinned in the binary: the three `setupHeader` bodies, the `+0x48` dispatch, and 11 opcode stores across V2/V3/V4 (rows 1–5 above plus Pool/Rng/MaxIndex/MX). The remaining ~70 opcode integers come from per-op reads of each `_M_insert_unique` site — HIGH confidence, consistent with the L2≡L3-low invariant, but not individually re-disassembled here. The **L2-name** column (`enum_variant_string_opcode` core_v4 decoder, reported at `0x143fd80`) is HIGH: the helper is an internal symbol absent from the `nm` table, so its address could not be independently pinned. The `[LOW]` classification of ITs 105–109 is **[INFERRED]** — no `visitInst` body was located, consistent with the absence of a `sameInst` arm — and encoder presence for IT107 DMABlock / IT108 DoWhile / IT109 TongaReduceMacroSymbolic remains **[SPECULATIVE]**.

---

## See also

- [The 64-Byte Instruction Bundle & Header Skeleton](../isa/instruction-bundle.md) — the bundle this header word opens; the slot families that fill the other 60 bytes.
- [InstructionType](../bir/instruction-type.md) — the L1 enum (110 ordinals) this table maps *from*; `sameInst` and the family predicates.
- The Walrus codegen driver page (Part 8) — the driver that selects the `CoreV{2,3,4}` target and calls `setupHeader` (in-flight at time of writing).
- The per-engine encoding pages (PE matmul, Pool, Activation, DVE, DMA, …) fill in the body fields for each opcode word above.
- The planned opcode appendix (Part 14) cites this page as the authoritative IT→opcode reference.
