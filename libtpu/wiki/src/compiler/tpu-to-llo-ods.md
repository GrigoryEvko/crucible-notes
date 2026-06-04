# tpu → LLO ODS Lowering

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VA == file offset). Other versions will differ; treat every VA as version-pinned.*

## Abstract

`createLowerToLLOPass` (`0x11203ba0`) is the MLIR `FunctionPass` that drops the `tpu` dialect onto the LLO dialect — the last dialect level before the bundle packer. The companion page [MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md) covers the *descent into* `tpu`; this page covers the *descent out of it* into LLO, and specifically the three artifacts a reimplementer needs that the pass-shape overview does not carry, each central to reproducing the lowering: **the per-op ODS signatures of the LLO targets** (operand/attribute order), **the gain/staging register table** that the MXU rewrite threads through the matmul-triple ops, and **the per-op emission logic** of the LowerToLLO conversion patterns.

The central fact this page rests on is that **each `mlir::llo::FooOp`'s ODS operand/attribute declaration is recoverable verbatim from its generated `build`/`create` factory symbol**. TableGen emits one `build(OpBuilder&, OperationState&, <args>)` static method per declared `OpBuilder`, and the demangled C++ argument list — after the two MLIR-machinery leaders — is exactly the operand+attribute declaration in source order. `Value → operand`, `Type/TypeRange → explicit result`, `<Enum>Attr / unsigned / bool / ArrayRef → attribute`. 322 distinct `mlir::llo::*Op` classes exist; 301 carry a typed `build` *or* `create`, so for the overwhelming majority the ODS shape is not inferred but read off the binary.

LLO is a register-machine ISA dialect, not a tensor dialect: an `llo.*` SSA value is one native vreg (one VPU/MXU/Vmem slot per bundle), masks are an `llo::VectorMaskType` register file, scalar predicates are `llo::PredicateType`. The reader should hold the familiar MLIR dialect-conversion frame — `RewritePattern`, `ConversionTarget`, `TypeConverter`, `applyFullConversion` — and read this page as the LLO-specific filling of those slots. Where LowerToLLO diverges from a textbook `applyPartialConversion` lowering (it uses **full** conversion, treats the function as a closed leaf, and pushes constant-slot selection *downstream* to the packer) the divergence is called out.

For reimplementation, the contract is:

- **The ODS signature extraction rule** — how to turn a `build`/`create` factory arg list into the operand/result/attribute declaration, and the [GEN] fallback for the ~95 elementwise ops that use the inference builder.
- **The gain/staging register model** — the three MXU enums (`MatmulMode`, `GainMatrixRegister`, `MatrixStagingRegister`) plus `GainLatchMode`, which LLO op carries which, the `GetGainLatchModeAndScalingFactor` selector, and the `Lgmr`/`Msra` hardware-ISA form mapping.
- **The arith.constant legalizer** — the deterministic type-dispatch decision tree, and why slot selection is *not* made here.
- **The four composite rewrites** — `all_reduce`, `prng_random_bits`, `stochastic_convert_elementwise`, `create_subelement_mask` — at LLO-op-multiset depth.

| | |
|---|---|
| **Pass factory** | `mlir::tpu::createLowerToLLOPass(xla::jellyfish::Target const&)` — `0x11203ba0` |
| **Pass entry** | `LowerToLLOPass::runOnOperation()` — `0x11204200` (0xa1ef-byte body, ~40 KB, ending at `0x1120e3ef`) |
| **Pass CLI name** | `lower-to-llo` (`.rodata` anchor) |
| **Driver** | `mlir::applyFullConversion` (`0x1c958ac0`) — *not* partial |
| **IR in / out** | `tpu` + `arith`/`math`/`vector`/`memref`/`cf` → `llo` + structural `scf`/`func`/`memref` |
| **LLO target count** | 322 distinct `mlir::llo::*Op` classes |
| **ODS source coverage** | 301/322 typed `build` or `create`; 21 default-builder ([SIB]) |
| **MXU register enums** | `MatmulMode` `round`/`high`/`low`; `GainMatrixRegister` `gmr0..3`; `MatrixStagingRegister` `MSRA`/`MSRB`; `GainLatchMode` (xpose×dtype) |
| **Downstream consumer** | LLO bundle packer — [MXU Slot](../isa/slot-mxu.md), [Immediate Slot](../isa/slot-immediate.md) |

---

## ODS Signature Model

### Purpose

This unit establishes the decode rule that the rest of the page relies on: how a demangled `build`/`create` symbol becomes an ODS signature. Without it the signature tables below read as assertions; with it they are mechanical readings of the binary.

### Algorithm

A TableGen-generated MLIR op carries, per declared `OpBuilder`, a static `build` method plus a `create` forwarder. The demangled signature of the leading non-generic builder is the ground-truth ODS shape:

```c
// build(OpBuilder&, OperationState&, <ODS args in declaration order>)
// create(OpBuilder&, Location,      <same ODS args>)
//
// Decode each trailing arg → ODS role:
mlir::Value                  -> 1 SSA operand
mlir::ValueRange             -> variadic operand list
mlir::Type                   -> 1 explicit result type (inference-free op)
mlir::TypeRange              -> multiple / variadic result types
mlir::<X>Attr (IntegerAttr…) -> 1 typed attribute
llo::<Enum> / llo::<Enum>Attr-> 1 enum attribute (GainLatchMode / MatmulMode /
                                MatmulDataFormat / VpackFormat / SequencerType…)
unsigned int / unsigned long -> 1 integer attribute (count / size / mxu-id)
bool                         -> 1 unit/bool attribute (flag)
llvm::ArrayRef<int|long>     -> 1 dense-array attribute (shape / strides)
StringRef / StringAttr       -> 1 string attribute
```

A builder whose *only* form is the generic `(TypeRange, ValueRange, ArrayRef<NamedAttribute>)` means the op uses the **default** builder: result type(s) are inferred (`InferTypeOpInterface` / `SameOperandsAndResultType`), and the concrete operand count is read from the op-name family (unary = 1, binary = 2). These are tagged **[GEN]** below.

> **NOTE —** the [GEN] vs typed split is observable directly. The reduce family makes it crisp: `VectorAddReduceS32Op::build(…, Value)` (`0x13f97a20`) is the *typed* form — one explicit `Value` operand — while its sibling `VectorAddReduceF32Op::build(…, ValueRange, ArrayRef<NamedAttribute>)` (`0x13f970a0`) is the *generic* [GEN] form with an inferred result. Same op family, two builder shapes, both confirmed in the binary; the integer-typed reduces pin their operand, the float reduces infer it.

### Function Map

| Source | Count | What it means | Confidence |
|---|---|---|---|
| `mlir::llo::*Op` classes | 322 | distinct LLO targets (matches the `tpu`-dialect inventory's LLO count) | CERTAIN |
| `*Op::build(` symbols | 231 | 225 typed + 6 generic occurrences across 215 distinct ops | HIGH |
| `*Op::create(` symbols | 329 | 301 distinct ops covered by `build` OR `create` (every `build` op also has a `create`) | HIGH |
| default-builder ops | 21 | no typed factory; signature taken from sibling op ([SIB]) | MEDIUM |

> **QUIRK —** for the ~95 elementwise scalar/vector arithmetic and transcendental ops the builder *omits* the result `Type` because the op declares `SameOperandsAndResultType`. Their result type is therefore not in the factory signature; it is recovered from the `F32`/`BF16`/`S16`/`S32`/`U32` suffix in the op name and confirmed by `verifyInvariantsImpl`. A reimplementer must restore the `let results =` constraint from the name, not the builder.

---

## Per-Op ODS Signatures

The full table is 322 rows; pasting it verbatim would violate the dump rule. Instead, each family below carries its **shape grammar** (the operand/attr pattern shared by the family) plus the representative rows whose signatures are binary-confirmed. Legend: `V`=Value operand, `VR`=ValueRange (variadic), `T`=Type result, `TR`=TypeRange, `A`=typed Attr, `u`=unsigned int-attr, `b`=bool flag-attr, `AR`=ArrayRef dense-array attr, `Str`=string attr, `[GEN]`=inference builder, `[SIB]`=sibling-recovered.

### Structural / control / memory / DMA

The structural family is where the explicit-`Type` builders dominate (these ops have no result-type inference). The heaviest is `EnqueueDMAOp`, the LLO realization of `tpu.enqueue_dma`.

```text
ConstantOp        : T, Attribute                         (result, value)
AllocaSmemOp      : T, IntegerAttr                        (result, size)
AllocaSyncFlagOp  : T, u                                  (result, count)
AllocaVmemOp      : u                                     (size → vmem ref)
AddrScaledOp      : V, V, IntegerAttr                     (base, index, scale)
AssumeMultipleOp  : V, u                                  (value, multiple)
DMADoneOp         : V, V, b                               (sflag, dma, is_remote)
EnqueueDMAOp      : V,V,V,V,V, AR<int>,AR<int>, VR, V,V, SequencerType, u, b
                    (src, dst, srcoff, dstoff, sflag, src_strides, dst_strides,
                     dyn_sizes, deviceid, coreid, seq, count, remote)
ErrorIfOp         : V, StringAttr                         (pred, message)
LogicalDeviceIdOp : T   ;  ChipIdOp / CoreIndexOp : ()    (i32 results)
RegionOp          : TR  ;  YieldOp : VR
TraceOp           : TR, StringAttr, IntegerAttr           (region; name, level)
```

The `EnqueueDMAOp::build` signature is byte-confirmed at `0x13f64100` (`build(OpBuilder&, OperationState&, Value, Value, Value, Value, Value, ArrayRef<int>, ArrayRef<int>, ValueRange, Value, Value, SequencerType, j, b)`): five SSA operands (src/dst/srcoff/dstoff/sflag), two dense-array stride attributes, a variadic dynamic-size operand list, the two cross-chip routing operands, then the `SequencerType` enum + count + remote flag. The dialect-conversion driver pushes the actual tile decomposition (the rolled/retiled loop nest) into the shared `LowerPassBase` helpers — see [LowerToMlo DMA Bridge-Cast](lower-to-mlo-dma-bridge.md).

### Scalar address / load / store / ALU

`Scalar*` ops model the SPU. Binary ALU ops split between two builder shapes: the bitwise/shift forms take `(V, V)` (no explicit `Type`, result inferred), while a few sign-typed ops carry an explicit result `T`.

```text
ScalarAddress{Smem,Vmem,Cmem,Hbm,Sflag}Op : V, V          (base, offset)
ScalarLoadOp   : T, V, V        ScalarStoreOp : V, V
ScalarToVectorOp : T, V (broadcast)   ScalarBitcastOp : T, V
ScalarSelectOp : T, V, V, V     (pred, t, f)
ScalarAddS32Op / ScalarMulU32Op / ScalarBitwise{And,Or,Xor}Op /
  ScalarShll/Shra/ShrlOp / ScalarFloor{Div,Rem}S32Op       : V, V
ScalarSubS32Op : T, V, V
ScalarCmp{Eq,Ge,Gt,Le,Lt,Ne}{F32,S32,U32}Op : V, V → i1
Scalar{Abs,Add,Sub,Mul,Div,Max,Min,Rem,Neg}F32 + transcendentals : [GEN]
ScalarConvert{S32ToF32,U32ToF32,F32ToU32,F32ToS32}Op : T, V
ScalarConvert{F32ToNarrowFloat,NarrowFloatToF32}Op   : T, V, T  (value, narrow-type)
```

The scalar ALU maps the `arith`/`math` source ops via the `ScalarElementwisePattern<>` template family (45 instantiations). The full source→target table lives on the pass-shape companion; this page asserts only the *LLO-side* shape.

### Vector elementwise / convert / pack / mask

Vector arithmetic is overwhelmingly [GEN]. The explicit-`T` exceptions are the comparison ops (which produce a *mask* type distinct from the operand type, so `SameOperandsAndResultType` cannot apply) and a handful of bitwise/shift ops.

```text
VectorAddF32 / VectorAddS32 / VectorMulF32 / VectorMulBF16 / VectorOrU32 / VectorXOrU32 : V, V
VectorAndU32 / VectorShiftLeftLogical / VectorShiftRight{Arithmetic,Logical} : T, V, V
Vector{Add,Sub,Mul,Max,Min,Div,Rem,Neg}{BF16,F32,S16,S32,U16,U32} (no explicit-T) : [GEN]
Vector{Abs,Ceil,Cos,Erf,Exp,Floor,Log,Log1p,Pow,Pow2,Round,RoundEven,Rsqrt,
       Sin,Sqrt,Tan,Tanh,Trunc}{F32,BF16}, VectorAtan2,
       VectorCountLeadingZeros, VectorPopulationCount                       : [GEN]
VectorCmp{Eq,Ge,Gt,Le,Lt,Ne}{BF16,F32,S16,S32,U32}Op : T, V, V → mask
VectorConvert{S32ToF32,U32ToF32,F32ToU32,F32ToS32}Op : T, V
VectorConvertF32To{If8,E4M3,E5M2,Bf16}StochasticOp   : T, V, V   (value, dither)
ConvertVectorF8NegativeZeroToZeroOp : T, V
VectorPackOp   : T, VpackFormat, V, V       VectorUnpackOp : T, u, VpackFormat, V
VectorMaskAndOp : T, V, V   VectorMaskNegateOp : T, V   VectorMask{Or,Xor}Op : [GEN]
VectorCreateMaskOp : T, u, u, u, u          (4 bound attrs)
VectorCreateSublaneMaskOp : T, V
VectorSelectOp : T, V, V, V                 (pred, true, false)
```

`VectorCreateMaskOp::build(…, Type, m, m, m, m)` is byte-confirmed at `0x13fb38e0` — one result `Type` plus four `unsigned long` bound attributes (the per-dim `[start,end]` pair for ≤2 dims). The stochastic-convert factories are confirmed at `0x13fae680` (`If8`), `0x13fad180` (`Bf16`), `0x13fad880` (`E4M3`), `0x13fadf80` (`E5M2`), each `create(…, Type, Value, Value)`: result type + value + per-lane dither, exactly the 2-operand shape the stochastic-round composite needs. The mask-type vector ops route through the [M-Register file](../isa/slot-vcreate-mask-mregister.md).

### Lane / sublane shuffle / transpose / reduce

```text
VectorLaneSeqOp / VectorLaneSeq{Compressed,Interleaved}B16Op : T   (0 operands, 1 lane-index result)
VectorLaneBroadcastOp     : T, V, V, V, BitDataFormat
VectorBroadcastSublaneChunkOp : T, V, V    VectorSublaneReplicateOp : T, V, V
VectorSublaneReverseOp    : T, V           VectorSublaneRotateOp    : T, V, V
VectorSublaneShuffleOp    : T, V, DenseArrayAttr<int>
VectorSublanePermuteOp    : T, V, V, PermSlanePatternGranularity
VectorRotateOp            : T, V, V, IntegerAttr
VectorTransposeOp         : V, VxposeMode, V, u, u, u, IntegerAttr
VectorBitcastOp           : T, V
VectorAddReduceS32Op / VectorMaxReduceS32Op / VectorMinReduceS32Op : V       (typed)
VectorAdd/Max/MinReduce{BF16,F32}Op : [GEN]                                  (1 operand, inferred)
VectorAdd/Max/MinSublaneReduce{BF16,F32,S32}Op : V
VectorMax/MinIndexReduceF32Op : TR, VR, [Attrs]    (2 results: index + value)
```

`VectorLaneSeqOp` and its B16 variants are confirmed as `ZeroRegions, OneResult` MLIR ops (e.g. the op model at `0x13f07200`) — zero operands, one result — matching the `0`-operand ODS. They are the LLO ops whose hardware ISA mnemonics are `vslaneid`/`vxlaneid`; the underlying instruction factory is `LloInstruction::CreateVectorLaneSequence{,CompressedB16,InterleavedB16}` (`0x1d4d0480` / `0x1d4d04c0` / `0x1d4d0500`). When a lowering names `tpu.iota → llo.vslaneid / llo.vxlaneid + add`, the LLO *op* it means is the `VectorLaneSeqOp` family — `vslaneid`/`vxlaneid` are the ISA-level mnemonics of those ops, not separate dialect ops — and B16 iotas pack through the `Interleaved`/`Compressed` B16 variants.

---

## MXU Gain / Staging Register Table

### Purpose

The matmul-triple ODS attributes are the densest reimplementation hazard in the pass: three *different* MXU register enums ride on the matmul ops, and a fourth (`GainLatchMode`) rides on the latch ops. Getting the wrong attribute on the wrong op produces LLO the bundle packer will mis-pack. This unit pins each enum, each carrier op, and the selector that chooses values.

### The four enums

| Enum | MLIR mnemonic | Members (binary strings) | Role | Confidence |
|---|---|---|---|---|
| `llo::MatmulMode` | `round` / `high` / `low` | round (`kRound`, normal/value 0), high (`kHigh`, value 1), low (`kLow`, value 2), plus `soft_low_of_eight`/`soft_middle_of_eight` variants | which precision pass of the f32-on-bf16-MXU emulation (rounded vs high/low mantissa half) the matmul performs | HIGH |
| `llo::GainLatchMode` | `xpose.*` / `packed_*` | `GAIN_LATCH_MODE_{NONE, NO_XPOSE_F32, NO_XPOSE_HI_F32, XPOSE_F32, XPOSE_HI_F32, XPOSE_LOW_F32, XPOSE_NIBBLE0/1, XPOSE_S4/S8/U4/U8, PACKED_*}` | how the STATIONARY operand is latched (transpose + dtype staging) | HIGH |
| `llo::GainMatrixRegister` | `gmr` | gmr0, gmr1, gmr2, gmr3 | which of the 4 GMR banks the gains reside in | HIGH |
| `llo::MatrixStagingRegister` | `msr` | MSRA, MSRB (= `MATPUSH_TARGET_MSRA`/`_MSRB`) | which MSR stages the MOVING operand | HIGH |

All four are registered as MLIR attributes by one `Dialect::addAttributes<MatrixStagingRegisterAttr, GainMatrixRegisterAttr, GainLatchModeAttr, MatmulModeAttr, …>` call (`0x13e5e860`). Attribute factories: `GainMatrixRegisterAttr::get(MLIRContext*, GainMatrixRegister)` (`0x13e4f5c0`, parse `0x13e4f6a0`); `MatmulModeAttr` via `symbolizeMatmulMode` (`0x13e4e660`); the storage classes `llo::detail::{GainMatrixRegisterAttrStorage, MatrixStagingRegisterAttrStorage}` are present. The enum string literals `GAIN_LATCH_MODE_*`, `DONE_WITH_GAINS_MODE_{NONE,TRANSPOSED}`, `MATPUSH_TARGET_MSR{A,B}`, and `MRF_SOURCE_MRF_{0,1,2,3}` are all live in `.rodata`.

> **GOTCHA —** `GainMatrixRegister` and `MatrixStagingRegister` are not the same axis. GMR (gmr0..3) names where the *stationary* gains live; MSR (MSRA/MSRB) names which double-buffer stages the *moving* operand. A reimplementation that collapses them into one "MXU register" attribute will emit matmul ops the packer cannot resolve to a slot.

### Which op carries which register

The carrier mapping is read directly from the MXU op `build` signatures — these are the rows that thread the registers.

| LLO op | ODS signature (after `OperationState&`) | GMR | MSR | mode | `build` VA |
|---|---|---|---|---|---|
| `VectorLatchOp` | `V, GainLatchMode, u(mxu)` | – | – | – | `0x13fbd020` |
| `VectorLatchIOp` | `V, u, GainLatchMode, MatrixStagingRegisterAttr, u(mxu)` | – | yes | – | `0x13fbbf40` |
| `VectorMatprepSubrOp` | `V, GainLatchMode, u(mxu)` | – | – | – | `0x13fcd0a0` |
| `VectorMatprepMubrOp` | `V, MatmulMode, MatmulDataFormat, u(mxu)` | – | – | yes | `0x13fcc120` |
| `VectorMatmulOp` | `V, MatmulMode, u(sublanes), b(transposed), MatmulDataFormat` | – | – | yes | `0x13fcad80` |
| `VectorMatmulMubrOp` | `V, MatmulMode, GainMatrixRegisterAttr, MatrixStagingRegisterAttr, u, b, MatmulDataFormat, IntegerAttr` | yes | yes | yes | `0x13fc9520` |
| `VectorMatresOp` | `T(result), MatmulDataFormat, u(sublanes), IntegerAttr(align)` | – | – | – | `0x13fce2a0` |
| `VectorDoneWithGainsOp` | `u(mxu)` | – | – | – | – |

The canonical conv/dot matmul (the `.mubr` form) carries the **full** `{MatmulMode, GMR, MSR, sublane_count, transposed, MatmulDataFormat, alignment}` set on *one* `VectorMatmulMubrOp`. This is byte-confirmed: `VectorMatmulMubrOp::build(OpBuilder&, OperationState&, Value, MatmulMode, GainMatrixRegisterAttr, MatrixStagingRegisterAttr, …)` at `0x13fc9520` (create at `0x13fc98e0`). The plain `VectorMatmulOp` (non-mubr) omits the explicit GMR/MSR and uses the implicit single-GMR/MSR path for simple dots. `VectorLatchIOp` is the only latch form that pins the MSR target explicitly (`build` at `0x13fbbf40`: `Value, j, GainLatchMode, MatrixStagingRegisterAttr, j`). The `getGainLatchMode()` accessors on `VectorLatchOp` (`0x13fbcfa0`), `VectorLatchIOp` (`0x13fbbe60`), and `VectorMatprepSubrOp` (`0x13fcd020`) confirm those ops carry the `GainLatchMode` attribute.

### Hardware ISA form ↔ attribute mapping

The dialect attributes resolve, at emit time, to the named MXU ISA forms. The `Lgmr` in a matmul opcode means "read gains from a GMR"; the opcode names both the source GMR and the staging MSR.

| Hardware MXU op (VectorExtended ISA) | LLO op + attributes |
|---|---|
| `LoadMatrixRegisterGmrMsra` / `GmrMsrb` | `VectorLatch(I)` + `GainLatchMode` + MSR ∈ {A,B} |
| `LoadMatrixRegisterGmrWithBf16ConversionMsr{a,b}` | `VectorLatchI` + `GAIN_LATCH_MODE_*_TO_BF16` |
| `MatrixMultiplyBf16Lgmr{Msra,Msrb}[Masked]` | `VectorMatmulMubr` (mode ∈ {round,high,low}, GMR=gmrN, MSR ∈ {A,B}, fmt=bf16) |
| `MatrixMultiply{S4,S8,U4,U8}Lgmr{Msra,Msrb}` | `VectorMatmulMubr` (fmt=int) |
| `MatrixMultiplyF32RoundedLgmr{Msra,Msrb}[Masked]` | `VectorMatmulMubr` (fmt=f32, rounded path) |

`Masked` variants take an extra `Vmask` operand for predicated-lane matmul. The 4 result FIFOs `MRF_SOURCE_MRF_0..3` are drained by `VectorMatres` (the result-mode selects which MRF). For the slot-bit encoding of these forms see [MXU Slot](../isa/slot-mxu.md), [Matprep/IAR/Latch Sub-Slots](../isa/slot-matprep-iar-latch.md), and [ResultFifo and ArchRegister Enums](../isa/resultfifo-archregister.md).

### The selector

```c
// GetGainLatchModeAndScalingFactor(Operation*, VectorType, bool isLhs,
//                                  bool isRhs, Target const&)   @ 0x112433a0
GainLatchMode select_latch(op, vty, isLhs, isRhs, target):
    // element-type dispatch (isF32 / isBF16 / isSignlessInteger callees):
    if vty.isF32():   return one of {NO_XPOSE_F32, HI_F32, LOW_F32}  // split-feed
    if vty.isBF16():  return PACKED_BF16          // after VectorPack of a pair
    if int8:          return NO_XPOSE_{S8,U8}     // after VectorPack low
    if int4:          return NO_XPOSE_{S4,U4} / NIBBLE0/1
    if f8:            return {F8E4M3*_TO_BF16, F8E5M2_TO_BF16}
    // geometry callees: target().LaneCount() / SublaneCount()
    // packing callees: VectorPackOp::create (×2), VectorUnpackOp::create (×4),
    //                  RollVectorsOp::create  (operand packing into one vreg)
    return (mode, scaling_factor)
```

`GetGainLatchModeAndScalingFactor` is confirmed at `0x112433a0`; the data-format sibling `GetMatmulDataFormatAndScalingFactor` (`0x11242b80`) picks `MatmulDataFormat`. The selector returns the *(GainLatchMode, scaling)* pair.

> **NOTE —** the *numeric* GMR index (0..3) and the A/B alternation of the MSR are **not** chosen by this selector. They are the matmul allocator's per-tile assignment: the GMR index is a running counter bounded by systolic depth, and adjacent matpreps alternate MSRA → MSRB. That allocation lives in the dot/conv→MXU descent — see [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md). This page's claim is bounded to the *enum members and carrier ops* (HIGH); the per-(gen, dtype, latch_idx) numeric schedule is **out of scope** (LOW here, owned downstream).

---

## arith.constant Legalization

### Purpose

`arith.constant` survives into LowerToLLO and must be legalized to `llo.constant`. The decision rule was previously suspected to be a non-deterministic slot-admission rule; it is in fact a pure, deterministic **type legalizer**. This unit pins the decision tree and corrects the earlier inference.

### Algorithm

The lambda body is inlined into the `__call_func` policy thunk at `0x11223100` (0x340 bytes; present in the function table). Its only decisions are type tests; it emits exactly one `llo.constant` and never selects a slot.

```c
// arith::ConstantOp lowering lambda — thunk @ 0x11223100
LogicalResult legalize_constant(op, adaptor, rewriter):
    attr = op.getValueAttr()                        // @0x11223125
    ty   = attr.getType()
    // --- scalar dispatch ---
    if ty.isUnsignedInteger() || ty.isSignlessInteger():  goto INT_PATH
    if ty.isF32():                                        goto EMIT_DIRECT
    // --- vector / splat path ---
    if DenseElementsAttr::classof(attr) && DenseElementsAttr::isSplat(attr)
       && (isRepresentableVectorType(ty) || isMaskVectorType(ty)):
        goto EMIT_DIRECT                             // vector splat
    // --- index path: the ONLY rewrite ---
    if ty.isIndex():
        i32ty = Builder::getI32Type()
        i32a  = Builder::getI32IntegerAttr((i32) IntegerAttr::getInt(attr))
        v = llo::ConstantOp::create(b, loc, i32ty, i32a)   // index → i32
        rewriter.eraseOp(op)
        return success
  INT_PATH:                                          // @0x1122320b
    require IntegerType, signless, width <= 31 (cmp 0x1f)
    // (cmp 0x40 = APInt single-word vs multi-word value storage, NOT a slot test)
    goto EMIT_DIRECT
  EMIT_DIRECT:                                        // @0x112231ce
    llo::ConstantOp::create(b, loc, ty, attr)         // 1 result Type + 1 Attribute
    rewriter.replaceOp(...); return success
  // default: emitOpError(...) -> failure                @0x112233a9
```

The emitted shape `llo::ConstantOp::create(OpBuilder&, Location, Type, Attribute)` matches the `ConstantOp` ODS `T, Attribute`. `i1`, signless integer ≤31-bit, F32, and representable/mask dense-splat vector constants are legalized **in place**; `index` constants are the **only** ones rewritten (rebuilt as i32 because the TPU scalar register is 32-bit) and the original erased. Anything else (e.g. a non-splat dense vector) hits `emitOpError` and is illegal.

> **NOTE — the lowering pass is a pure type legalizer; constant placement is decided later.** The lambda emits a single `llo.constant`; the constant's *fate* — a hardwired-constant `ScalarYEncoding` reference for 0/±1/±0.5/±π/±e, a bundle immediate slot if it fits the 16-bit (JF) / 20-bit (V5+) width, or an SMEM constant-pool load otherwise — is a value-dependent, pack-time decision in the bundle packer. See [Immediate Slot](../isa/slot-immediate.md) and [SPU / Scalar Slot](../isa/slot-spu-scalar.md). The 31-bit `cmp 0x1f` here is the IR-level immediate ceiling check, not a slot selection.

---

## Composite Rewrites

### Purpose

Four `tpu` ops fan out to large, distinct LLO op multisets rather than a 1:1 target. Each composite's LLO set is decoded from the rewrite-lambda body's `llo::*Op::create` call sites. Knowing the *multiset* (not just the primary op) is what lets a reimplementer reproduce the emitted instruction stream and estimate its bundle cost.

### tpu.prng_random_bits → EmitThreefryRound ×N

`EmitThreefryRound` (`0x1125ada0`) emits, per round, the Threefry-2x32 mix. The lane rotate is **synthesized**, not a hardware rotate.

```c
// EmitThreefryRound(Operation*, ConversionPatternRewriter&, VectorType,
//                   Value key, Value ctr, int round)   @ 0x1125ada0
//   add  = VectorAddS32Op(x, y)                        // mix-add
//   rot  = VectorOrU32Op( VectorShiftLeftLogicalOp(x, k),
//                         VectorShiftRightLogicalOp(x, 32-k) )   // (x<<k)|(x>>(32-k))
//   xor  = VectorXOrU32Op(a, b)                        // lane-XOR
//   ConstantOp ×2                                      // rotate amount + add const
```

The grep of the decompiled body confirms exactly one each of `VectorAddS32Op`, `VectorShiftLeftLogicalOp`, `VectorShiftRightLogicalOp`, `VectorOrU32Op`, `VectorXOrU32Op`, plus two `Constant`s — and **zero** `vrot` / `VectorRotate` references.

> **NOTE —** the lane rotate is synthesized as `(x << k) | (x >> (32-k))` via `VectorShiftLeftLogical | VectorShiftRightLogical | VectorOrU32`; there is no hardware `vrot` in the round. Total LLO ops per `prng_random_bits` vreg ≈ `6 × rounds + fold`. Relevant ODS: `VectorShiftLeftLogicalOp : T, V, V`; `VectorOrU32Op : V, V`; `VectorAddS32Op : V, V`; `VectorRNGSetSeedOp : V` (the `prng_set_seed_32` carrier).

### tpu.stochastic_convert_elementwise → F8/Bf16 stochastic-round family

```text
per target format:
  scale         : VectorMulF32Op
  stochastic    : VectorConvertF32To{If8,E4M3,E5M2,Bf16}StochasticOp  (value + dither)
  fix-up -0.0   : ConvertVectorF8NegativeZeroToZeroOp
  NaN/special   : VectorCmpNeF32Op (×2) → VectorSelectOp (×3)
  + ScalarConvert{F32ToNarrowFloat,NarrowFloatToF32}Op, VectorBitcastOp (×2), ConstantOp (×4)
≈ 25 LLO ops; lambda @ 0x1125be00
```

The stochastic `create` factories are confirmed at `0x13fae680`/`0x13fad180`/`0x13fad880`/`0x13fadf80`, each `(Type, Value, Value)` — the second `Value` is the per-lane random dither. The 3-operand `VectorSelectOp : T, V, V, V` (pred, true, false) picks between the rounded result, the special-case value, and the passthrough.

### tpu.create_subelement_mask → sublane-mask + negate + and

```text
VectorCreateSublaneMaskOp (×1) : T, V
VectorMaskNegateOp        (×1) : T, V      (complement region)
VectorMaskAndOp           (×1) : T, V, V   (AND with per-subelement const mask)
ConstantOp                (×2)
≈ 5 LLO ops; lambda @ 0x11237800
```

### tpu.all_reduce → reductor + comms chain

The heaviest single TPU→LLO rewrite (lambda `0x11238820`), fanning out to ~30 distinct LLO ops. The dimension structure, not the 30 rows, is the reimplementation content:

| Axis | Values | Notes |
|---|---|---|
| reduce-kind | add, max, min, argmax, argmin | argmax/argmin use `VectorMax/MinIndexReduceF32Op` (2 results) |
| element-type | bf16, f32, s32 | selects the reductor suffix (`*F32` typed/[GEN] per dtype) |
| reduce-axis | lane, sublane | emits a lane-reduce then a sublane-reduce |
| comms / staging | scratch store/load + cross-core shuffle | `VectorStore[Masked]`, `Vst[Masked]WithArbitrarySlaneStride`, `Vld…`, `VectorLoadSublaneShuffle`, `ScalarAddressVmem` (×3) |

The lambda picks the reductor by `(reduce-kind × element-type)`, reduces lane-then-sublane, spills partials to a scratch VMEM buffer (`ScalarAddressVmem` + `VectorStore`), shuffles across cores via the arbitrary-slane-stride store/load (plus masked variants), then reloads and finalizes. Cross-chip remote reduction signals through the sflag path (`vsync.add.remote`). The reduce-op ODS confirms the [GEN]/typed split noted earlier: `VectorAddReduceS32Op : V` (typed, `0x13f97a20`) vs `VectorAddReduceBF16Op : ValueRange, ArrayRef<NamedAttribute>` ([GEN], `0x13f95b60`).

---

## What Is Not On This Page

- **The full 322-row ODS table verbatim** — by design (the dump rule). The family grammars plus binary-confirmed representatives above let a reimplementer reconstruct each row from the `build`/`create` symbol; the families with no typed factory (21 ops, [SIB]) inherit their sibling's shape (MEDIUM).
- **The result-type *constraints*** (`LLO_Vreg` / `LLO_Predicate` / `LLO_Mask` predicates). The `build` signature gives operand+attr shape and the explicit result type when present; the `let results =` predicate lives in each op's `verifyInvariantsImpl` (e.g. `ConstantOp::verifyInvariantsImpl` `0x13f5f320`) and was not per-op decoded.
- **The numeric per-(gen, dtype, latch_idx) GMR/MSR schedule** — owned by the dot/conv→MXU descent (the systolic allocator), cross-referenced not re-derived here.
- **The pass shape itself** — the `TypeConverter` callbacks, the `MloConversionTarget` legal-set, the dynamic SCF/func legality callbacks, and the full ~242-pattern source→target inventory are the companion overview's material; this page assumes them.

---

## Cross-References

- [The tpu MLIR Dialect](tpu-dialect-and-ops.md) — the source dialect this pass consumes; the `tpu.*` op surface
- [MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md) — the descent *into* `tpu`, upstream of this pass
- [Compile Phases](compile-phases.md) — where `lower-to-llo` sits in the ordered phase sequence
- [The TPU Compiler](overview.md) — the five-phase dialect descent overview
- [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md) — the systolic latch/matmul allocator that assigns the numeric GMR index and MSR alternation
- [LowerToMlo DMA Bridge-Cast](lower-to-mlo-dma-bridge.md) — the sibling SparseCore lowering sharing `LowerPassBase` DMA tiling
- [MXU Slot](../isa/slot-mxu.md) — bundle-slot encoding of the matmul ops this pass emits
- [Matprep, IAR, and Latch Sub-Slots](../isa/slot-matprep-iar-latch.md) — slot encoding of the latch/matprep ops
- [Immediate Slot](../isa/slot-immediate.md) — where `llo.constant` slot selection actually happens (pack-time)
- [SPU / Scalar Slot](../isa/slot-spu-scalar.md) — scalar constant resolution and the hardwired-constant table
- [ResultFifo and ArchRegister Enums](../isa/resultfifo-archregister.md) — the `MRF_SOURCE_MRF_0..3` result FIFOs drained by `VectorMatres`
- [vcreate_mask and the M-Register File](../isa/slot-vcreate-mask-mregister.md) — the mask register file the vector-mask ODS ops target
- [LloOpcode Enum](../isa/llo-opcode-enum.md) — the LLO opcode space these ODS ops linearize into
