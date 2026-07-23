# The Penguin BIR Instruction Set + BIR→ISA Map

> **Scope.** The complete penguin **BIR `Inst*` class roster** recovered byte-exact
> from `libBIR.so` (**110 distinct concrete classes**), and for each GPSIMD-relevant
> class: its operands, engine binding, the `SundaISel` codegen rule that lowers it, the
> resulting `SundaISAInst`, and the numeric TPB opcode. This page sits one level *above*
> the [`emit_*`→opcode table](compiler-map.md): both the NKI `emit_` surface and the
> XLA-HLO frontend converge on the **same BIR `Inst*` nodes**, so the BIR→ISA map here is
> the *superset* of the emit table. Numeric opcodes are reconciled against the device
> opcode roster in [opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md).
>
> **Audience.** A reimplementer building a Vision-Q7-compatible engine who needs to know,
> for any IR node the compiler can produce, which device byte it ultimately decodes to —
> and, conversely, which device opcodes the compiler *never* emits (firmware-internal).

All facts below derive from static analysis of the shipped neuronx-cc
`2.24.5133.0+58f8de22` wheel: the stripped-but-symbol-bearing `libBIR.so` dynamic symbol
table, the Cython `SundaISAInst` / `SundaISel` extension modules (whose qualnames survive
in `.rodata`), and the committed device opcode ledger. Confidence is tagged per claim as
`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`, where OBSERVED = read directly from a shipped
artifact, CARRIED = OBSERVED in a cited in-repo page and reused.

---

## 1. Where `Inst*` lives in the lowering pipeline

The compiler walks five hops from a frontend op to a NEFF instruction byte. The BIR
`Inst*` node is the pivot — the single IR vocabulary both frontends target:

```
  nki.isa.<op>  /  XLA-HLO op
    └─▶ penguin BIR  Inst<Name>            (libBIR.so: IRBuilder / createFromJson builds it)
          └─▶ [SundaISel.transformT<Name>Operator → <Name>CodeGen]   (the rewrite rule)
                └─▶ SundaISAInst.<Name>Op   (the target ISA inst — 33 of them)
                      └─▶ [libwalrus CoreVNGenImpl get_bytes]
                            └─▶ TPB opcode byte  +  NEURON_ISA_TPB_*_STRUCT descriptor → NEFF
```

| Stage | Artifact (OBSERVED) | Node type |
|---|---|---|
| HLO/NKI frontend | `hlo2penguin` / `NKICodeGenFlow` / IRBuilder | builds BIR `Inst*` |
| **Penguin BIR (IR)** | `lib/libBIR.so` — **110 `Inst*` classes** | `Inst<Name>` ← **HERE** |
| Lowering / legalize | `penguin/transforms/Lower*.so` | BIR-generic → BIR-target |
| Sunda ISel | `targets/sunda/passes/SundaISel.so` | `Inst<Name>` → `<Name>Op` |
| Sunda ISA | `targets/sunda/SundaISAInst.so` — **33 `*Op`** | `SundaISAInst.<Name>Op` |
| Descriptor emit | `lib/libwalrus.so` `CoreVNGenImpl` | TPB opcode + STRUCT bytes |

**Each concrete `Inst*` ships a fixed method-set** `[HIGH/OBSERVED]`. Every class in the
roster carries the same mangled-symbol skeleton in `libBIR.so`'s dynamic table — this is
how the roster is enumerable from a stripped binary:

```text
  _ZN3bir Inst<Name> C1/C2 ...        constructor(name, BasicBlock[, fields])
  _ZN3bir Inst<Name> 14createFromJson ...    NEFF JSON round-trip (the canonical marker)
  _ZN3bir Inst<Name> 16getDefaultEngineEv    engine binding   (107 of 110 carry it)
  _ZTVN3bir<len>Inst<Name>E                   vtable           (110 of 110 carry it)
```

The **JSON op-tag** that lands in the NEFF `*.json` instruction stream is the class
basename minus the `Inst` prefix (`InstTensorTensor` → `"TensorTensor"`,
`InstQuantizeMx` → `"QuantizeMx"`). That tag is exactly what the firmware decode in
[opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md) keys against once
`SundaISel` has bound it to an opcode.

> **NOTE — the three classes without `getDefaultEngine`.** `createFromJson` and the
> vtable are present for all 110, but `getDefaultEngineEv` is present for **107**. The
> three pure-control terminators that carry no engine are the structural-control nodes
> (`InstTerminator`/`InstExit`/`InstReturn` family); they expand to SEQ/RT-band pseudos,
> never to a data-engine opcode. `[MED/INFERRED]`

---

## 2. The full 110-class BIR `Inst*` roster

Recovered byte-exact by joining the `createFromJson` and `_ZTV` symbol sets in
`libBIR.so` (both yield the identical 110-element set; see §8 self-verify). Grouped by
lowering family. `[HIGH/OBSERVED]`

**A. POOL/DVE elementwise + reduce compute (30)**
`InstTensorTensor` `InstTensorScalar` `InstTensorScalarPtr` `InstTensorScalarCache`
`InstTensorScalarAffineSelect` `InstTensorReduce` `InstSelect` `InstCopyPredicated`
`InstReciprocal` `InstMemset` `InstIota` `InstDropout` `InstExponential` `InstPool`
`InstMax` `InstMaxIndex` `InstMaxIndexAndMatchReplace` `InstMatchReplace`
`InstStreamShuffle` `InstStreamTranspose` `InstRangeSelect` `InstNonzeroWithCount`
`InstGetSequenceBounds` `InstDveReadAccumulator` `InstAbstractCopy` `InstTensorCopy`
`InstTensorCopyDynamicSrc` `InstTensorCopyDynamicDst` `InstGenericRelu`
`InstTongaReduceMacroSymbolic`

**B. Gather / indirect / generic load-store (9)**
`InstGather` `InstIndirectCopy` `InstIndirectLoad` `InstIndirectSave`
`InstIndirectSaveAccumulate` `InstGenericIndirectLoad` `InstGenericIndirectSave`
`InstGenericCopy` `InstGeneric`

**C. PE / systolic (4)**
`InstMatmult` `InstMatmultBase` `InstMatmultMx` `InstMatmultSparse`

**D. ACT / activation (4)**
`InstActivation` `InstActivationReadAccumulator` `InstReadActivationAccumulator`
`InstLoadActFuncSet`

**E. Batch-norm (5)**
`InstBNStats` `InstBNStatsAggregate` `InstBNBackprop` `InstBNBackprop2` `InstBNGradients`

**F. MX microscaling (1)**
`InstQuantizeMx`

**G. RNG (7)**
`InstRng` `InstRand` `InstRand2` `InstRandGetState` `InstRandSetState` `InstGetRandState`
`InstSetRandState`

**H. Collective / distributed (8)**
`InstCollective` `InstCollectiveCompute` `InstCollectiveSend` `InstCollectiveRecv`
`InstGPSIMDSB2SB` `InstGetGlobalRankId` `InstGetCurProcessingRankID` `InstReadVarAddr`

**I. DMA / descriptor (9)**
`InstDMA` `InstDMACopy` `InstDMABlock` `InstDMATrigger` `InstDMADescriptor`
`InstDMADescriptorCCE` `InstDMADescriptorCopy` `InstDMADescriptorReplicate`
`InstDMADescriptorTranspose`

**J. Load / store (4)**
`InstLoad` `InstSave` `InstTensorLoad` `InstTensorSave`

**K. Control / terminator / register (24)**
`InstNoOp` `InstHalt` `InstDrain` `InstEventSemaphore` `InstAllEngineBarrier`
`InstCoreBarrier` `InstGroupResetSemaphores` `InstTensorCompletion` `InstBranchHint`
`InstCompareAndBranch` `InstUnconditionalBranch` `InstLoop` `InstDoWhile`
`InstDynamicForLoop` `InstBreak` `InstExit` `InstReturn` `InstCall` `InstTerminator`
`InstSwitchQueueInstance` `InstResetQueueInstance` `InstRegisterAlu` `InstRegisterMove`
`InstDevicePrint`

**L. Kernel / custom-op containers (5)**
`InstCustomOp` `InstInlineASMBytes` `InstBIRKernel` `InstNKIKernel` `InstNKIKLIRKernel`

`30+9+4+4+5+1+7+8+9+4+24+5 = `**`110`**`.`

> **CORRECTION — the `C2ERKNSt` reproduction recipe undercounts by one.** The repro command
> `nm libBIR.so | rg -o 'Inst…C2ERKNSt' | sort -u` yields **109**, not 110, because one
> class's constructor does not take a `std::string` reference as its first argument:
> **`InstDynamicForLoop`** (its ctor signature carries loop-bound operands first). Its
> `createFromJson` (`_ZN3bir18InstDynamicForLoop14createFromJsonE…`) and vtable
> (`_ZTVN3bir18InstDynamicForLoopE`) are both present, so it *is* the 110th concrete
> class. **Ground the count on `createFromJson` or `_ZTV`, not on the `C2ERKNSt`
> constructor pattern** — the latter silently drops `InstDynamicForLoop`. `[HIGH/OBSERVED]`

> **NOTE — delta vs the earlier "73".** The "73" was the GPSIMD/compute-touching subset
> (families A–I minus the control spine K and the load/store J). The full 110 adds the
> 24-class control spine (K), the 4 load/store (J), and the 5 kernel containers (L). No
> contradiction — same hierarchy at two granularities. `[HIGH/INFERRED]`

> **GOTCHA — `Instruction` and `InstructionBasicBlockHolder` are *not* in the 110.** A
> naive vtable grep `_ZTVN3bir…Inst…E` returns **112**; two of those
> (`_ZTVN3bir11InstructionE` @ `0x8fced8`, `_ZTVN3bir27InstructionBasicBlockHolderE` @
> `0x8fbfc0`) are the **abstract base classes** of the hierarchy, not concrete leaf
> instructions. Filter them out (`rg -v '^Instruction'`) to land on the exact 110.
> `[HIGH/OBSERVED]`

---

## 3. The complete BIR `Inst*`→ISA→TPB-opcode map

Columns: **BIR class** `[engine]` \| **operands / semantics** \| **`SundaISel` rule** \|
**`SundaISAInst`** \| **TPB opcode (numeric)** \| **conf**. Opcode numerics are the
uppercase-hex values from [opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md);
every numeric below was cross-checked against that ledger (engine column too).
Where the report and ledger differ, a **CORRECTION** row follows.

### 3.1 POOL/DVE elementwise + reduce

| BIR class `[engine]` | operands / semantics | SundaISel rule | SundaISAInst | TPB opcode | conf |
|---|---|---|---|---|---|
| `InstTensorTensor` `[POOL/DVE]` | `dst = op(src0,src1)`, 3D–4D access | direct + dtype/engine fan-in (§3.5) | `TensorTensor` / `TensorTensorScanOp` | `0x41` TENSOR_TENSOR_ARITH \| `0x51` _BITVEC; scan→`0xE5`; bf16→`0x8A/0x8B/0x8F`; int-wide→`0xF3` | HIGH/OBS |
| `InstTensorScalar` `[POOL/DVE/ACT]` | `dst = op(src, scalar[s])` + cache/reverse flags | direct | `TensorScalar` | `0x43` _ARITH \| `0x53` _BITVEC | HIGH/OBS |
| `InstTensorScalarPtr` `[POOL]` | tensor-scalar, pointer/multi operand | direct | `TensorScalarPtr` | `0x44`/`0x4F`/`0x54`/`0x5F`/`0x70`/`0x71`; SUNDA dual `0x87`/`0x88` (dormant) | HIGH/OBS |
| `InstTensorScalarCache` `[POOL]` | cache reduce / cumulative | direct | `TensorScalarCache` | `0x9A` _CACHE_REDUCE \| `0xE6` _CACHE_CUMULATIVE | HIGH/OBS |
| `InstTensorScalarAffineSelect` `[POOL]` | `pred = cmp(affine,0)`, select | direct | `TensorScalarAffineSelect` | `0x92` TENSOR_SCALAR_AFFINE_SELECT | HIGH/OBS |
| `InstSelect` `[POOL]` | tensor select (the `0x98` path) | direct | (`TensorScalarSelect`) | `0x98` TENSOR_SCALAR_SELECT | HIGH/OBS |
| `InstCopyPredicated` `[POOL]` | predicated copy, mask-gated | direct (flag-routed, §4.1) | `CopyPredicated` | `0x72` (plain) \| `0x99` CAST_PRED \| `0xEA` SELECT_REDUCE \| `0xE8` _SCALAR | HIGH/OBS |
| `InstReciprocal` `[POOL/DVE]` | `dst = 1/src` | direct | `Reciprocal` | `0x48` RECIPROCAL (maintained gap) | HIGH/OBS |
| `InstMemset` `[POOL/DVE]` | fill `dst` with constant | direct | `Memset` | `0x49` MEMSET (maintained gap) | HIGH/OBS |
| `InstIota` `[POOL]` | ramp `0..n` then dtype cast | direct | `Iota` | `0x7E` IOTA | HIGH/OBS |
| `InstDropout` `[POOL/DVE]` | `mask*scale`, rng-gated | direct | `Dropout` | `0x7F` DROPOUT | HIGH/OBS |
| `InstExponential` `[DVE]` | `e^x` | direct | `Exponential` | `0x30` EXPONENTIAL | HIGH/OBS |
| `InstPool` `[POOL]` | avg/max pool window | direct | `Pool` | `0x45` POOL; max-select `0x58` | HIGH/OBS |
| `InstMax` / `InstMaxIndex` / `InstMaxIndexAndMatchReplace` / `InstMatchReplace` `[DVE]` | top-8 search/select family | direct | `SundaMax8` / `MaxIndexAndMatchReplace` | `0x6C` MAX8 \| `0x6D` MATCH_VALUE_LOAD \| `0x6E` FIND_INDEX8 \| `0x6F` MATCH_REPLACE8 | HIGH/OBS |
| `InstStreamShuffle` `[POOL/DVE]` | within-stream lane shuffle | direct | `StreamShuffleInst` / `LNCShuffleOp` | `0x6A` STREAM_SHUFFLE (maintained gap) | HIGH/OBS |
| `InstStreamTranspose` `[POOL/DVE]` | streaming 32×32 transpose | direct | `StreamShuffleInst` | `0x6B` STREAM_TRANSPOSE | HIGH/OBS |
| `InstRangeSelect` `[NX/DMA]` | range-bounded select | direct | `RangeSelect` | `0xBC` RANGE_SELECT | HIGH/OBS |
| `InstNonzeroWithCount` `[POOL]` | nonzero indices, count in last elem | direct | `NonzeroWithCount` | `0xF2` NONZERO_WITH_COUNT | HIGH/OBS |
| `InstGetSequenceBounds` `[POOL]` | per-segment `[start,end)`; `id==0→[n,-1]` | direct | `GetSequenceBounds` | `0xBE` GET_SEQUENCE_BOUNDS | HIGH/OBS |
| `InstDveReadAccumulator` `[POOL/DVE]` | read DVE accumulator | direct | (`DveReadAccum`) | `0x9B` DVE_READ_ACCUMULATOR; indices `0xE9` | HIGH/OBS |
| `InstAbstractCopy` / `InstTensorCopy` / `…DynamicSrc` / `…DynamicDst` `[POOL/DVE]` | copy w/ dtype-cast; dynamic addr | direct | `TensorCopy` | `0x46` COPY \| `0x47` CAST | HIGH/OBS |
| `InstGenericRelu` `[POOL/ACT]` | fused relu activation | `InferIntrinsicOnCC` fold | (act intrinsic) | folds into `0x21` ACTIVATE | MED/CARR |
| `InstTensorReduce` `[POOL/DVE]` | axis reduce (add/max/or/and) | direct | `LocalReduceOp` | `0x42` _ARITH \| `0x52` _BITVEC; transpose `0x83`/`0x84`; cumulative `0x4E`/`0x5E` | HIGH/OBS |
| `InstTongaReduceMacroSymbolic` `[POOL]` | symbolic partition/cross-lane reduce **MACRO** | `LegalizePartitionReduce` (§4.2) | `LocalReduceOp` (+loop) | `0x7C` CROSS_LANE_REDUCE_ARITH \| `0x7D` _BITVEC | HIGH/OBS |

### 3.2 Gather / indirect / generic

| BIR class `[engine]` | operands / semantics | SundaISel rule | SundaISAInst | TPB opcode | conf |
|---|---|---|---|---|---|
| `InstGather` `[POOL]` | within-partition gather, **u32** idx, OOB→0 | `transformTGatherOperator` / `GatherCodegen` | `PoolGather` | `0x68` GATHER | HIGH/OBS |
| `InstIndirectCopy` `[POOL]` | 8-core/16-part gather, **u16** idx, `≤4096` | direct | `IndirectCopy` | `0xE7` INDIRECT_COPY | HIGH/OBS |
| `InstGenericIndirectLoad` / `…Save` / `InstGenericCopy` / `InstGeneric` `[POOL/NX]` | generic memory ops; can lower to gather | `codegenGenericLoad` / `GenericStoreCodegen` / `ScatterCodegen` | (gather or DMA) | `0x68` when `can_lower_generic_load_to_gather`, else `0xBB` DMA_INDIRECT / `0xE7` | HIGH/OBS |
| `InstIndirectLoad` / `InstIndirectSave` / `InstIndirectSaveAccumulate` `[POOL/NX]` | indexed load/save, scatter-accumulate | `ScatterCodegen` / `transformTGenericAtomicRMWOperator` | (scatter) | `0x79` EMBEDDING_UPDATE (scatter-accum) / `0xBB` DMA_INDIRECT | HIGH/OBS |

### 3.3 PE / ACT / Batch-norm / MX / RNG

| BIR class `[engine]` | operands / semantics | SundaISel rule | SundaISAInst | TPB opcode | conf |
|---|---|---|---|---|---|
| `InstMatmult` / `InstMatmultBase` `[PE]` | systolic `C += A·B` | direct | (`MatMulOp`) | `0x01` LDWEIGHTS + `0x02` MATMUL | HIGH/OBS |
| `InstMatmultMx` `[PE]` | MX-scaled matmul | transform → `MatMulMXOp` | `MatMulMXOp` | v4: `0x09` LDWEIGHTS_MX + `0x0A` MATMUL_MX; v5: folded into `0x01`/`0x02` | HIGH/OBS |
| `InstMatmultSparse` `[PE]` | sparse matmul + tags | direct | (`MatMulOp`+tags) | `0x06` LDTAGS + `0x07` MATMUL_SPARSE | HIGH/OBS |
| `InstActivation` `[ACT]` | `f(x·scale+bias)` + optional summation | direct | (`ActivateOp`) | `0x21` ACTIVATE; multipass→`0x25`/`0x26` (§4.3) | HIGH/OBS |
| `InstActivationReadAccumulator` / `InstReadActivationAccumulator` `[ACT]` | read ACT accumulator | direct | — | `0x24` ACTIVATION_READ_ACCUMULATOR | HIGH/OBS |
| `InstLoadActFuncSet` `[ACT]` | load PWL/activation table | direct | — | `0x23` ACTIVATION_TABLE_LOAD; RT-pseudo `0xC6` LOAD_ACT_FUNC_SET | HIGH/OBS |
| `InstBNStats` `[DVE]` | batch-norm stats | `transformTBNStatsOperator` / `BNStatsCodegen` | — | `0x61` BATCH_NORM_STATS2; transpose `0x82` | HIGH/OBS |
| `InstBNStatsAggregate` `[DVE]` | aggregate stats | `transformTBNAggrOperator` / `BNAggrCodegen` | — | `0x62` BATCH_NORM_AGGREGATE | HIGH/OBS |
| `InstBNBackprop` / `InstBNBackprop2` `[DVE]` | BN backprop | `transformTBNBackpropOperator` / `BNBackpropCodeGen` | — | `0x65` BATCH_NORM_BACK_PROP | HIGH/OBS |
| `InstBNGradients` `[DVE]` | BN grad accumulate | `transformTBNGradOperator` / `BNGradientCodeGen` | — | `0x63`/`0x94` BATCH_NORM_GRAD_ACCUMULATE(2) | HIGH/OBS |
| `InstQuantizeMx` `[DVE]` | **FORWARD** pack `src`→fp8+E8M0 scale, 32-elem block (8part×4elem) | `transformTQuantizeMXOperator` / `QuantizeMXCodeGen` | `QuantizeMXOp` | `0xE3` QUANTIZE_MX | HIGH/OBS |
| `InstRng` `[POOL]` | XORWOW rng stream | direct | — | `0x4D` RNG | HIGH/OBS |
| `InstRand` / `InstRand2` `[DVE]` | random draw | direct | — | `0x76` RAND (dormant) / `0xE2` RAND2 | HIGH/OBS |
| `InstRandGetState` / `InstRandSetState` / `InstGetRandState` / `InstSetRandState` `[DVE]` | rng seed-state get/set | direct | `SundaSetRandState` | `0x77` RAND_GET_STATE / `0x78` RAND_SET_STATE (SEQ-inline) | HIGH/OBS |

> **CORRECTION (MX forward direction, vs the earlier 0x7B reading).**
> `InstQuantizeMx` is the **forward** pack and lowers to **`0xE3`
> QUANTIZE_MX on DVE**, *not* `0x7B`. `0x7B` TENSOR_DEQUANTIZE (POOL) is the **inverse**
> dequant and has **no forward BIR producer** — it is the firmware-internal dequant the
> MX matmul consumes (§4.3a). The ledger confirms `0xE3 QUANTIZE_MX | DVE` and `0x7B
> TENSOR_DEQUANTIZE | POOL`. `[HIGH/OBSERVED]`

> **NOTE — `InstMatmultMx` is the only PE-MX *forward* producer.** The MX matmul itself
> consumes packed weights; the per-element dequant is firmware-internal. So the MX path
> has exactly two host-visible BIR producers: `InstQuantizeMx` (`0xE3`, the activation
> pack) and `InstMatmultMx` (`0x09`/`0x0A` or folded `0x01`/`0x02`). `[HIGH/INFERRED]`

### 3.4 Collective / DMA / Load-store / Control / Containers

| BIR class `[engine]` | semantics | SundaISel rule | SundaISAInst | TPB opcode | conf |
|---|---|---|---|---|---|
| `InstCollective` `[GpSimd/SP]` | AllReduce/AllGather/ReduceScatter/AllToAll(V), subtype = JSON ctype tag | `transformAllReduce`/`AllGather`/`ReduceScatterOp` + `codegenTiledCCOpWith(out)Rank` | `TiledAllGatherOp`/`TiledReduceScatterOp`/`TiledAlltoAllOp`/`TiledCollective(Permute)(Reduce)Op` | **macro (§4.2)** → PSEUDO `0xC7` TRIGGER_ALL_REDUCE / `0xC8` TRIGGER_COLLECTIVE / `0xD9` TRIGGER_COLLECTIVE2 + DGE/CCE | HIGH/OBS |
| `InstCollectiveCompute` `[GpSimd]` | reduce-compute leg | `codegenElementwiseAllReduceOp` | `LocalCollectiveOp` / `LocalReduceOp` | CCE_OP descriptor (no standalone opcode) | HIGH/OBS |
| `InstCollectiveSend` / `InstCollectiveRecv` `[GpSimd/SP]` | point-to-point | (send/recv) | `SendRecvOp` / `SendRecvCCEOp` | `0xBF` SB2SB_COLLECTIVE / PSEUDO `0xCB` SEND_RECV | HIGH/OBS |
| `InstGPSIMDSB2SB` `[POOL]` | SB-to-SB remote copy | direct | — | `0xBF` SB2SB_COLLECTIVE | HIGH/OBS |
| `InstGetGlobalRankId` / `InstGetCurProcessingRankID` / `InstReadVarAddr` `[SP]` | rank-id / var-addr load | direct | — | PSEUDO `0xDC` GID_LOAD / `0xDB` CUR_PROCESSING_RANK_ID / `0xC9` READ_VAR_ADDR | HIGH/OBS |
| `InstDMA` / `InstDMACopy` / `InstDMABlock` `[NX/DMA]` | HBM↔SB copy | DGE-backed | — | `0xB8` DMAMEMCPY / `0xB9` DMA_MEMCPY2 / `0xBA` DMA_IMMEDIATE | HIGH/OBS |
| `InstDMATrigger` `[RT→NX]` | pseudo DMA trigger w/ queue name | **relocated** | — | PSEUDO `0xC1` DMATRIGGER (runtime-patched to a WRITE; §4.3) | HIGH/OBS |
| `InstDMADescriptor` / `…Copy` / `…CCE` / `…Replicate` / `…Transpose` `[NX/DMA]` | DGE descriptor variants | `codegenTiledOffloadedMemInstr` / `transformOffloadedMemCpy` | `TiledOffloadedMemCpy` | `0xBB` DMA_INDIRECT / `0xBD` DMA_TRANSPOSE / `0xF1` DMA_GATHER_TRANSPOSE | HIGH/OBS |
| `InstLoad` / `InstTensorLoad` `[NX]` | SBUF/PSUM tensor load | direct | — | `0xAA` TENSOR_LOAD | HIGH/OBS |
| `InstSave` / `InstTensorSave` `[NX]` | tensor store | direct | — | `0xAB` TENSOR_STORE | HIGH/OBS |
| `InstNoOp` / `InstHalt` / `InstDrain` / `InstEventSemaphore` / `InstGroupResetSemaphores` `[NX]` | SEQ control spine | direct | — | `0x9F` ENGINE_NOP / `0xA1` HALT / `0xA2` DRAIN / `0xA0` EVENT_SEMAPHORE / `0xB0` _RANGE_CLEAR | HIGH/OBS |
| `InstAllEngineBarrier` / `InstCoreBarrier` / `InstTensorCompletion` `[NX/SP]` | barriers / completion | direct | `CoreBarrierOp` | PSEUDO `0xD5` SYNC_BARRIER / `0xD8` CORE_BARRIER / `0xDE` TENSOR_COMPLETION | HIGH/OBS |
| `InstCompareAndBranch` / `InstUnconditionalBranch` / `InstBranchHint` `[NX]` | branch + prefetch | direct | — | `0xA9` COMPARE_BRANCH / `0xB5` BRANCH_PREFETCH_HINT / PSEUDO `0xDD` | HIGH/OBS |
| `InstRegisterAlu` / `InstRegisterMove` `[NX]` | scalar reg ALU / move | direct | — | `0xA8` ALU_OP / `0xA7` MOVE | HIGH/OBS |
| `InstLoop` / `InstDoWhile` / `InstDynamicForLoop` / `InstBreak` / `InstExit` / `InstReturn` / `InstCall` / `InstTerminator` `[NX]` | structural control flow | unrolled at tiling | `ExitOp` | PSEUDO `0xD1` FUNCTION_BEGIN / `0xD2` FUNCTION_RETURN / `0xD3` FUNCTION_CALL | HIGH/OBS |
| `InstSwitchQueueInstance` / `InstResetQueueInstance` `[NX]` | DMA queue mgmt | direct | — | PSEUDO `0xCF` DMASWAP_QUEUE_SET | HIGH/OBS |
| `InstDevicePrint` `[host]` | host-side debug | direct | — | **no device opcode** | HIGH/OBS |
| `InstCustomOp` `[POOL]` | C++ custom-op (ulib) container | `transformTSIMDOperator` | (custom ABI) | `0x85` CUSTOM_OP_HEADER + `0x86` CUSTOM_OP_PAYLOAD + `0xF0` EXTENDED_INST | HIGH/OBS |
| `InstInlineASMBytes` `[POOL]` | raw hex-byte Xtensa kernel | `SundaSIMDCodeGen` | — | `0xF0` EXTENDED_INST | HIGH/OBS |
| `InstBIRKernel` / `InstNKIKernel` / `InstNKIKLIRKernel` `[POOL]` | NKI/BIR kernel container | `Rms/Softmax CodeGen` / `codegenTiledCCOp*` | `TiledRmsNormOp`/`TiledSoftmaxOp`/`TiledNativeKernel{Attention,MLP,QKV,RMSNormQuant}` | **macro (§4.2)** → POOL/PE/ACT/DVE schedule (no single opcode) | HIGH/OBS |

> **CORRECTION (engine of `0xF1`).** An earlier grouping lists
> `InstDMADescriptorTranspose`'s `0xF1 DMA_GATHER_TRANSPOSE` under the DMA family, which
> reads as an NX/DMA opcode. The committed ledger assigns **`0xF1` to the POOL engine**
> (`0xF1 | DMA_GATHER_TRANSPOSE | POOL`). Treat `0xF1` as a POOL-engine gather-transpose,
> not an NX/DMA descriptor, for engine-fan-in accounting. `[MED/OBSERVED]`

### 3.5 The fan-in: one BIR class, many opcodes

`InstTensorTensor` is the canonical polymorphic node — a single class whose opcode is
selected at `SundaISel` by `{dtype, engine, gen}`, the mechanism analysed in
[dtype-engine-fanin-synthesis](dtype-engine-fanin-synthesis.md):

| `InstTensorTensor` selector | TPB opcode | route |
|---|---|---|
| int32 add/sub/mul, no PSUM operand | `0x41` TENSOR_TENSOR_ARITH (POOL) | the xdref 32-bit-wrap int leaf |
| bitwise / logical | `0x51` TENSOR_TENSOR_BITVEC (POOL) | bitvec datapath |
| fp | DVE datapath | DVE elementwise |
| **scan** flag set | `0xE5` TENSOR_TENSOR_SCAN_ARITH | `TensorTensorScanOp` ISA inst |
| **bf16** (sunda) | `0x8A`/`0x8B`/`0x8F` ADD/MULT/SUB_BF16 | SUNDA-only bf16 |
| **int-wide** (maverick) | `0xF3` TENSOR_TENSOR_INT_WIDE | maverick `+` |

The same one-to-many shape applies to `InstCopyPredicated` (`0x72`/`0x99`/`0xEA`/`0xE8`),
`InstActivation` (`0x21`/`0x25`/`0x26`), and `InstTensorReduce`
(`0x42`/`0x52` plus transpose `0x83`/`0x84` and cumulative `0x4E`/`0x5E`).

---

## 4. The two boundary sub-rosters

### 4.1 The 2:1 splits — one concept, two BIR classes, two opcodes `[HIGH/OBSERVED]`

These look like a single operation but are genuinely *two distinct BIR classes* lowering
to *two distinct ISA insts*:

- **`InstGather` (`0x68` `PoolGather`) vs `InstIndirectCopy` (`0xE7` `IndirectCopy`).**
  Both `.PoolGather` and `.IndirectCopy` qualnames are present in `SundaISAInst.so` — two
  classes, two ISA insts, two opcodes. `InstGather` is within-partition u32 (OOB→0);
  `InstIndirectCopy` is 8-core/16-part u16 with a `≤4096` bound. Not a name trap — a real
  BIR-level split. The flag-routed variants (`InstCopyPredicated` → four device forms;
  `InstMatmultMx` → v4 pair vs v5 folded) are *one* class with an opcode picked by flag.

### 4.2 BIR ops with NO direct opcode — macro / fan-out `[HIGH/OBSERVED]`

A `SundaISel` transform expands one BIR node into many ISA insts:

- **`InstCollective`** (AllReduce/AllGather/ReduceScatter/AllToAll) → `Tiled*Op` ISA insts
  → PSEUDO `0xC7`/`0xC8`/`0xD9` collective triggers + DGE/CCE descriptors. Relocated to a
  TOP-SP instruction series at load. No single device opcode. See
  [fused-cc-lowering](fused-cc-lowering.md) for the CC expansion.
- **`InstCollectiveCompute`** → CCE_OP descriptor (the reduce leg) — no standalone opcode.
- **`InstTongaReduceMacroSymbolic`** → CrossLaneReduce `0x7C`/`0x7D` + a loop
  (`LegalizePartitionReduce`). One symbolic BIR node, a reduce-loop micro-schedule.
- **`InstBIRKernel` / `InstNKIKernel` / `InstNKIKLIRKernel`** and the Tiled fused-CC ops
  (`TiledRmsNormOp` / `TiledSoftmax(Dx/RSum)Op` /
  `TiledNativeKernel{Attention,MLP,QKV,RMSNormQuant}`): each is **one BIR kernel node**
  that `RmsNormCodeGen` / `SoftmaxCodeGen` / `codegenTiledCCOp*` expands into a
  **sequence** of POOL/ACT/DVE opcodes. The kernel *is* a micro-schedule, not an opcode.
- **`InstGeneric*`** (generic load/store/copy): conditionally lower to `0x68` gather
  (when `can_lower_generic_load_to_gather`) OR `0xBB` DMA_INDIRECT OR a copy — the opcode
  is a `SundaISel` *decision*, not a fixed bind.
- **Structural control** (`InstLoop`/`InstDoWhile`/`InstCall`/`InstReturn`): unrolled or
  expanded at tiling; the `FUNCTION_BEGIN`/`CALL`/`RETURN` pseudos (`0xD1`–`0xD3`) are
  RT-band, relocated. No data-engine opcode.

### 4.3 Device opcodes with NO BIR producer — firmware-internal `[HIGH/OBSERVED + CARRIED]`

Opcodes in the device roster that *no BIR `Inst*` emits* — materialized by firmware:

- **(a) Inverse MX dequant `0x7B` TENSOR_DEQUANTIZE (POOL).** The MX matmul and the
  dequant kernel consume packed MX data; the compiler emits only the forward
  `InstQuantizeMx → 0xE3`. `0x7B` has no forward BIR producer.
- **(b) The cptc codec impls (inside `0xE4` CONV_LUT_LOAD).** dtype-selected sub-paths
  *inside* the `0xE4` dispatcher — no opcode, no BIR class; `InstLoadActFuncSet` reaches
  the dispatcher entry, the impls are internal.
- **(c) BN param-loads `0x64`/`0x66`/`0x8E`.** The 256-reciprocal RAM load
  (`0x66` LOAD_PARAMETER_RAM) and param2 load are firmware-internal sub-steps of the BN
  codegen, not distinct BIR nodes (the BIR has only `InstBNStats`/`Aggregate`/`Backprop`/
  `Gradients`).
- **(d) `0x25` ACTIVATE2 / `0x26` ACTIVATE_MULTIPASS** — reached as lowering *targets* of
  `InstActivation` by a dtype/multipass flag (a fan-in like §3.5), no distinct BIR class.
- **(e) Maverick int-wide `0xF3`/`0xF4` + SUNDA bf16 `0x8A`–`0x8F`** — pure dtype-routed
  targets of `InstTensorTensor`/`InstTensorReduce`. No named BIR class, no `emit_` name.
- **(f) The NX SEQ sync ops `0xB1`–`0xB4`/`0xB6`** (`SET_ORDERING_MODE`, `MOVE_SHAPE`,
  `POLL_SEM`, `TEST_EVENT_SEM`, `COMPACT_CONTROL_INST`) — SEQ/NX-firmware control with no
  host op. (The *controllable* sync ops `0xA0`/`0xA1`/`0xA2`/`0xB0` **do** have BIR
  producers: `InstEventSemaphore`/`InstHalt`/`InstDrain`/`InstGroupResetSemaphores`.)
- **(g) The dormant band** (`0x04` WEIGHT_MASK, `0x05` WEIGHT_SHIFT, `0x4A`–`0x4C`
  REG_LOAD/STORE/SHUFFLE, `0x73` ROI_ALIGN, `0x81` JPEG_DECODE) — legacy tonga opcodes
  the current BIR never emits.

> **NOTE — the RT-pseudo band `0xC1`–`0xDF` is the inverse case.** These 31 PSEUDO
> opcodes **do** have BIR producers (`InstDMATrigger`→`0xC1` DMATRIGGER,
> `InstCollective`→`0xC7`/`0xC8`/`0xD9`, `InstReadVarAddr`→`0xC9`,
> `InstSwitch/ResetQueueInstance`→`0xCF`, `InstGetGlobalRankId`→`0xDC`,
> `InstGetCurProcessingRankID`→`0xDB`, `InstAllEngine/CoreBarrier`→`0xD5`/`0xD8`,
> `InstTensorCompletion`→`0xDE`) but are **relocated** by libnrt to real
> WRITE/SEMAPHORE/descriptor-trigger instructions before any device decode. So: **BIR
> producer YES, device-firmware decode NO** — the opposite of the §4.3 set. `[HIGH/CARRIED]`

---

## 5. The `SundaISel` rewrite rule set

The BIR→ISA rewrite rules read byte-exact from `SundaISel.so` `.rodata` qualnames:
**34 `transform*` methods + 21 `*Codegen`/`*CodeGen` methods**. The pattern is
two-phase — `transformT<X>Operator` canonicalizes the BIR-DAG, `<X>CodeGen` emits the ISA
inst. `[HIGH/OBSERVED]`

| rule (transform / codegen) | BIR `Inst*` | → ISA / TPB result |
|---|---|---|
| `transformTRmsNormOperator` / `RmsNormCodeGen` | `InstBIRKernel(rmsnorm)` | `TiledRmsNormOp` → POOL+ACT seq |
| `transformTSoftmaxOperator` / `SoftmaxCodeGen` | `InstBIRKernel(softmax)` | `TiledSoftmaxOp` → ACT+POOL seq |
| `transformTSoftmaxDxOperator` / `codegenSoftmaxDxOp` | `InstBIRKernel(softmaxDx)` | `TiledSoftmaxDxOp` → seq |
| `transformTQuantizeMXOperator` / `QuantizeMXCodeGen` | `InstQuantizeMx` | `QuantizeMXOp` → `0xE3` (DVE) |
| `transformTGatherOperator` / `GatherCodegen` | `InstGather` | `PoolGather` → `0x68` |
| `transformAllReduceOp` / `codegenElementwiseAllReduceOp` | `InstCollective(AR)` | → `0xC7` / CCE descriptors |
| `transformAllGatherOp` / `codegenElementwiseAllGatherOp` | `InstCollective(AG)` | `TiledAllGatherOp` → `0xC8` |
| `transformReduceScatterOp` | `InstCollective(RS)` | `TiledReduceScatterOp` → `0xC8`/`0xD9` |
| `transformTBNStatsOperator` / `BNStatsCodegen` | `InstBNStats` | → `0x61` |
| `transformTBNAggrOperator` / `BNAggrCodegen` | `InstBNStatsAggregate` | → `0x62` |
| `transformTBNBackpropOperator` / `BNBackpropCodeGen` | `InstBNBackprop(2)` | → `0x65` |
| `transformTBNGradOperator` / `BNGradientCodeGen` | `InstBNGradients` | → `0x63`/`0x94` |
| `transformTParReduceBNMeanVarOperator` / `ParReduceBNMeanVarCodegen` | `InstBNStats(parreduce)` | → CrossLaneReduce + BN seq |
| `transformOffloadedFMA` | `InstTensorTensor(fma)` | `InferIntrinsicOnCC` fold → CC kernel |
| `transformOffloadedMemCpy` / `codegenTiledOffloadedMemInstr` | `InstDMADescriptor*` | → DGE descriptor |
| `transformTGenericStoreOperator` / `GenericStoreCodegen` | `InstGenericIndirectSave` | → `0xBB`/`0x79` |
| `transformTGenericAtomicRMWOperator` | `InstIndirectSaveAccumulate` | → `0x79` EMBEDDING_UPDATE |
| `transformTSIMDOperator` / `SundaSIMDCodeGen` | `InstInlineASMBytes`/`InstCustomOp` | → `0xF0` EXTENDED_INST |
| `codegenGenericLoad` / `SundaGenericLoadCodegen` | `InstGeneric(Load)` | → `0x68` gather OR `0xBB` DMA |
| `codegenTiledCCOpWithRank` / `…WithoutRank` | `InstCollective`/`InstBIRKernel` | → tiled CC schedule |
| `ScatterCodegen` | `InstIndirectSave` | → scatter (`0x79`/`0xBB`) |
| `PartitionReductionCodeGen` / `ReductionCodeGen` | `InstTongaReduceMacroSymbolic`/`InstTensorReduce` | → `0x7C`/`0x7D` + loop / `0x42`/`0x52` |

**The four `SundaISel` predicate symbols** (OBSERVED in `SundaISel.so`) that
drive the conditional routes:

- **`can_lower_generic_load_to_gather`** — turns `InstGeneric(Load)` → `0x68` gather when
  the index pattern is within-partition u32; otherwise falls to `0xBB` DMA_INDIRECT.
- **`can_use_dge`** — selects swdge / hwdge / none for the DMA-descriptor path.
- **`should_shard_offloaded`** — whether an offloaded mem op is sharded across cores.
- **`replaceLastAPAddrWithIndex`** — the indirect-addressing rewrite (the gather/scatter
  access-pattern index splice).

> **NOTE — `InferIntrinsicOnCC` / `LegalizePartitionReduce` are CARRIED, not OBSERVED.**
> These two transform names appear in the lowering narrative but did **not** surface as
> literal `.rodata` strings in `SundaISel.so` — they likely live in a separate
> `transforms/Lower*.so` or are inlined. The opcode results they produce (`0x21` relu
> fold; `0x7C`/`0x7D` cross-lane reduce) *are* OBSERVED via the ledger. Treat the rule
> *names* as `[MED/CARRIED]` pending a `Lower*.so` sweep. `[MED/INFERRED]`

### 5.1 Annotated codegen — the gather lowering

`transformTGatherOperator` → `GatherCodegen` is the cleanest non-trivial rule and the one
whose 2:1 split (§4.1) is most instructive. The reconstructed shape:

```c
/* SundaISel: lower InstGather  ->  SundaISAInst.PoolGather  ->  TPB 0x68 GATHER
 * Symbols: SundaISel.transformTGatherOperator (.rodata qualname)
 *          SundaISel.GatherCodegen / SundaGatherCodegenT
 *          predicate SundaISel.replaceLastAPAddrWithIndex (the AP index splice)
 *          target   SundaISAInst.PoolGather (qualname OBSERVED in SundaISAInst.so)
 * Device:  0x68 GATHER, POOL engine, S4D4_GT struct, idx u8/16/32, OOB -> 0
 */
ISAInst *gather_codegen(InstGather *bir) {
    /* (1) index dtype gate — InstGather carries the u32 within-partition contract;
     *     InstIndirectCopy is a *separate* BIR class for the u16 / <=4096 case (§4.1). */
    assert(bir->index_dtype == U32 && bir->within_partition);

    /* (2) splice the index access-pattern onto the last source AP dimension —
     *     this is replaceLastAPAddrWithIndex: the gather reads src[idx[lane]]. */
    AccessPattern src_ap = bir->src_ap;
    replace_last_ap_addr_with_index(&src_ap, bir->index_operand);

    /* (3) emit the PoolGather ISA inst — single opcode, no fan-out. OOB indices
     *     resolve to 0 in the device datapath (the GATHER struct's OOB-zero contract). */
    return new_PoolGather(/*opcode=*/0x68, src_ap, bir->dst_ap, /*oob=*/ZERO);
}
```

Contrast `codegenGenericLoad`, which is *conditional* — it only reaches `0x68` when the
predicate holds, otherwise it emits a DMA descriptor:

```c
/* SundaISel.codegenGenericLoad / SundaGenericLoadCodegen */
ISAInst *generic_load_codegen(InstGeneric *bir) {
    if (can_lower_generic_load_to_gather(bir))   /* within-partition u32 idx */
        return new_PoolGather(0x68, ...);        /* same target as InstGather */
    else
        return new_DMADescriptor(0xBB /*DMA_INDIRECT*/, ...);  /* fall to DGE path */
}
```

---

## 6. Cross-check — BIR roster vs the device opcode ledger

`[HIGH]`

1. **Every GPSIMD-relevant BIR `Inst*` maps to a ledger opcode** (or a §4.2 macro of
   them). The §3 map binds each compute/gather/PE/ACT/BN/MX/RNG/collective/DMA class to
   an opcode present in [opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md).
   **Zero BIR `Inst*` produces an opcode the ledger lacks.** Verified by
   look-up of all ~80 cited opcodes against the ledger's uppercase-hex table.
2. **The ledger's `NONE` (decode-gap) opcodes re-classified by BIR producer.** Most
   maintained-`NONE` opcodes (`0x48` Reciprocal, `0x49` Memset, `0x72` CopyPredicated,
   `0x6A` StreamShuffle, `0x85`/`0x86` CustomOp) **have** a BIR producer — the gap is a
   *firmware decode* debt, not a compiler-coverage gap. The 8 dormant-`NONE` opcodes
   (§4.3g) are the only ones with neither a BIR producer nor a maintained decode.
3. **The three-dispatch-surface consistency holds at the BIR layer.** The BIR JSON op-tag
   (== `Inst` basename) → `SundaISAInst` → TPB opcode byte threads cleanly into the .bin
   TPB-seq slot, the SEQ ASCII dispatch (`index = opcode − 0x41`), and the POOL Q7
   `kernel_info_table`. The POOL-compute BIR ops (`InstTensorTensor` `0x41`,
   `InstTensorCopy` `0x46`/`0x47`, `InstIota` `0x7E`, `InstTongaReduceMacroSymbolic`
   `0x7C`/`0x7D`, `InstGetSequenceBounds` `0xBE`, `InstNonzeroWithCount` `0xF2`,
   `0x7B` inverse-dequant, `InstCustomOp`/`InstInlineASMBytes` `0xF0`) are exactly the
   `kernel_info_table` opcode column. `[HIGH/CARRIED]`

---

## 7. The extended `emit_*`→opcode table, now as BIR `Inst*`→ISA

The 62 `emit_` rows are the **NKI-exposed subset** of the 110 BIR classes. The §3
map adds the BIR ops the NKI surface does *not* name (reached only via XLA-HLO,
dtype-routing, or macro internals):

- The PE sparse path (`InstMatmultSparse` → `0x06`/`0x07`) — no `emit_`.
- The generic load/store/scatter (`InstGeneric*`/`InstIndirect*` → `0x68`/`0xBB`/`0x79`) —
  the XLA generic-memory path, no `emit_`.
- The BN gradient/backprop (`InstBNBackprop`/`Gradients` → `0x65`/`0x63`/`0x94`) — reached
  via nxd, not a direct `emit_`.
- The control/terminator spine (family K, 24 classes → SEQ/RT opcodes) — IR plumbing.
- The RNG state mgmt (`InstRng`/`InstRand*`/`InstGetRandState` →
  `0x4D`/`0x76`/`0x77`/`0x78`) — no `emit_`.
- The kernel containers (`InstBIRKernel`/`InstNKIKernel` → fused-CC schedules) — the nki
  kernel-decorator path, not a single `emit_`.

So the §3 table is the **complete** BIR→ISA map: `emit_` (62) + the XLA/macro/control
delta (~48) = the 110-class roster, all bound to ledger opcodes or §4.2 macros.

---

## 8. Adversarial self-verify — the 5 strongest claims

Each claim is re-derived from the binary, attacked, and resolved. `[HIGH/OBSERVED]`

**(1) The roster is exactly 110 — not 73, not 109, not 112.**
Three independent symbol families in `libBIR.so` must agree. `createFromJson` (the
canonical concrete-class marker): **110**. `_ZTVN3bir…E` vtables, after filtering the two
abstract bases `Instruction`/`InstructionBasicBlockHolder`: **110**. The two sets are
*byte-identical* (`diff` empty). Attack: does the count depend on the cp-tag? The
cp310/cp311/cp312 wheels are byte-identical per the version anchor, so the dynamic table
is the same. **Verdict: 110, triple-grounded.** The "73" was the compute subset; "112"
counts the abstract bases; "109" is the `C2ERKNSt` recipe artifact (claim 2).

**(2) The `C2ERKNSt` repro recipe undercounts by exactly one — `InstDynamicForLoop`.**
`createFromJson` − `C2ERKNSt` = `{InstDynamicForLoop}`. Attack: is it a real concrete
class or a parse artifact? Both `_ZN3bir18InstDynamicForLoop14createFromJsonE…`
(`T`, text section) and `_ZTVN3bir18InstDynamicForLoopE` @ `0x8fd700` (`V`, weak vtable)
are present — it is concrete; its constructor simply does not take a `std::string&`
first. **Verdict: the count must rest on `createFromJson`/`_ZTV`, never on `C2ERKNSt`.**

**(3) `SundaISAInst` ships exactly 33 `*Op` qualnames.**
`strings SundaISAInst.so | rg -o 'SundaISAInst\.[A-Z][A-Za-z0-9_]+' | sort -u | wc -l` =
**33**, including the split-confirming pair `.PoolGather` and `.IndirectCopy` (the §4.1
2:1 split) plus the five `TiledNativeKernel{,Attention,MLP,QKV,RMSNormQuant}` containers.
Attack: are these runtime-constructed (so undercounted)? They are Cython class qualnames
baked into `.rodata` at build, not dynamically named. **Verdict: 33 ISA insts.**

**(4) `InstQuantizeMx` forward = `0xE3` (DVE), and `0x7B` has no forward BIR producer.**
The ledger row `0xE3 | QUANTIZE_MX | DVE` and `0x7B | TENSOR_DEQUANTIZE | POOL` are
distinct opcodes on distinct engines. `InstQuantizeMx` is the only quantize BIR class in
the 110; there is no `InstDequantizeMx`. Attack: could `0x7B` be reached via a flag on
`InstQuantizeMx`? No — they are opposite directions on opposite engines, and the dequant
is consumed by the MX matmul internally. **Verdict: forward `0xE3`, inverse `0x7B`
firmware-internal; corrects any `0x7B`-forward reading.**

**(5) The collective family is a macro — no single device opcode.**
`InstCollective` lowers via `transformAllReduce`/`AllGather`/`ReduceScatterOp` →
`Tiled*Op` ISA insts → the RT-pseudo triggers `0xC7`/`0xC8`/`0xD9`, which the ledger
documents as `PSEUDO_*` in the `0xC1`–`0xDF` band (NRT-lowered, *not* device-decoded).
Attack: is there a hidden single "collective" opcode? The ledger's `0xBF SB2SB_COLLECTIVE`
is the *point-to-point* leg (`InstGPSIMDSB2SB`/`InstCollectiveSend`/`Recv`), not the
group collective. **Verdict: group collectives are macro fan-outs to pseudos + DGE/CCE
descriptors; only the SB2SB p2p leg has a real opcode.**

---

## 9. Version anchors

- **neuronx-cc** `2.24.5133.0+58f8de22` — `libBIR.so` (the 110-class roster + the
  `createFromJson`/`_ZTV` symbols), `SundaISAInst.cpython-311-*.so` (33 `*Op`),
  `SundaISel.cpython-311-*.so` (34 `transform*` + 21 `*Codegen`/`*CodeGen`) all read.
  cp310/cp311/cp312 byte-identical.
- The 110-class BIR roster is gen-agnostic — one IR for all targets. The per-gen opcode
  split (sunda / cayman / mariana / maverick) is applied at `SundaISel`/walrus, not at the
  BIR layer; no silicon-generation fact is inferred from any compiler descriptor.

---

## See also

- [The GPSIMD-Relevant Compiler Map + `emit_*`→opcode](compiler-map.md) — the NKI-exposed
  subset this page is the superset of.
- [SundaISel Deep-Dive](sundaisel.md) — the BIR-DAG match / canonicalize internals.
- [Fused-CC / nkilib Kernel Lowering](fused-cc-lowering.md) — how the §4.2 kernel macros
  expand to POOL/ACT/DVE schedules.
- [Dtype/Engine/Gen Fan-In + CC-Lane Synthesis](dtype-engine-fanin-synthesis.md) — the
  §3.5 one-class-many-opcode mechanism.
- [Opcode Catalog Ledger](../firmware/kernels/opcode-catalog-ledger.md) — the device-side
  opcode roster every numeric here is reconciled against.
