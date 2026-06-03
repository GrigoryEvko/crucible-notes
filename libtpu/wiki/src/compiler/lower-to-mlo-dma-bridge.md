# LowerToMlo DMA Bridge-Cast

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VA == file offset; build `libtpu_lts_20260413_b_RC00`). Other versions will differ; treat every VA as version-pinned.*

## Abstract

`createLowerToMloPass` (`0x1322adc0`) is the MLIR **`ModuleOp`** pass that drops the `tpu` dialect onto the SparseCore mid-level (`Mlo` / `sparse_core`) dialect. It is the SparseCore sibling of [tpu → LLO ODS](tpu-to-llo-ods.md) (which is the TensorCore `FunctionPass` descent into LLO). Where LowerToLLO lowers *every* `tpu` op to an LLO target in one shot, LowerToMlo is a **two-stage** lowering: most `tpu` ops lower directly to `sc_tpu.*` (`sparse_core` dialect) / `arith` / `memref` ops in this pass, but the DMA-and-sync family — `tpu.enqueue_dma`, `tpu.enqueue_indirect_dma`, `tpu.wait_dma2`, `tpu.fetch_and_add_sync` — does **not** lower here. Those ops materialize a tagged `builtin.unrealized_conversion_cast` (the *bridge-cast*) that defers the real DMA emission to a downstream SparseCore pass.

This page owns the LowerToMlo-specific machinery of that bridge:

- **The 2-stage DMA bridge-cast** — how `tpu.enqueue_dma` (and the three sibling DMA/sync ops) lowers to a per-operand `builtin.unrealized_conversion_cast` tagged `sc.unlowering`, with the source op marked `sc.unlowered`, and what the cast carries forward.
- **The block-signature conversion** — the `func::FuncOp` dynamic-legality predicate that detects a SparseCore *sequencer* function and forces its signature through the `TypeConverter`, plus the `UnrealizedConversionCastOp` legality guard that lets the bridge-cast pass through the legalizer in transit.
- **The deferred-DMA materialization resolution** — how the tagged bridge-cast survives `applyFullConversion` and is later consumed by `substituteUnloweringConversionCastOp` (`0x134e73e0`) inside `ExpandTiledMemRefsPass` to emit the real SparseCore DMA ops.

The generic speculative-apply / rollback engine that makes the legalization *safe* (the `IRRewrite` action log, `undoRewrites` / `resetState` / `applyRewrites`, the `UnresolvedMaterializationRewrite` record) is **not** on this page — it lives on [ConversionPatternRewriter](conversion-pattern-rewriter.md). This page covers only the LowerToMlo bridge, the LowerToMlo legality predicates, and the deferred-DMA resolution.

| | |
|---|---|
| **Pass factory** | `mlir::tpu::createLowerToMloPass(xla::jellyfish::Target const&, sparse_core::LowerToMloPassContext*)` — `0x1322adc0` |
| **Pass entry** | `mlir::tpu::(anon)::LowerToMloPass::runOnOperation()` — `0x1322b200` |
| **Pass kind** | **`ModuleOp`** pass (`jaxlib::mlir::Pass<LowerToMloPass, mlir::ModuleOp>`) — *not* a `FunctionPass` |
| **Driver** | `mlir::applyFullConversion(module, MloConversionTarget, frozenPatterns, ConversionConfig)` — *not* partial |
| **Target dialect** | `sparse_core` (the `Mlo` mid-level) + `arith`/`memref`/`scf`/`vector`/`cf`/`func`/`math`/`LLVM` |
| **Legal-dialect set** | `MloConversionTarget` ctor `0x13245900` (vtable `0x21903b98`): 11 dialect `TypeID`s |
| **Pattern set** | `populateTpuToMloConversionPatterns` (`0x1322c920`) — 44 functional `ConversionPattern` lambdas + 1 catch-all |
| **DMA bridge ops** | `tpu.enqueue_dma` (`0x13239e00`), `tpu.enqueue_indirect_dma` (`0x1323a660`), `tpu.wait_dma2` (`0x1323ae00`), `tpu.fetch_and_add_sync` (`0x1323b600`) |
| **Bridge target** | `builtin.unrealized_conversion_cast` tagged `sc.unlowering` (per-operand) + source op `sc.unlowered` |
| **Stage-2 consumer** | `substituteUnloweringConversionCastOp` (`0x134e73e0`) under `ExpandTiledMemRefsPass` |
| **Source provenance** | `.rodata` string `platforms/xla/mosaic/dialect/tpu/transforms/lower_to_mlo.cc` (line 405 in the EnqueueDMA body) |

---

## Why Two Stages — The Direct-vs-Bridge Split

### Purpose

Every `tpu` op LowerToMlo handles falls into one of two buckets. Establishing the split first is what makes the rest of the page legible: the bridge-cast is not a fallback, it is a *deliberate deferral* for the one op family whose final shape is not known until tile expansion has run.

### The split

`populateTpuToMloConversionPatterns` (`0x1322c920`) installs 44 functional `ConversionPattern` lambdas (signature `LogicalResult(SrcOp, SrcOpAdaptor, ConversionPatternRewriter&)`). 40 of them lower their source op **directly** to one or more `sc_tpu.*` (Mlo / `sparse_core` dialect) ops — frequently a 1:N explosion (e.g. `tpu.iota` → `sc_tpu.vlaneseq` [+ `arith.index_cast`], `tpu.device_id` → 6 ops, `tpu.delay` → an `scf.for` nest of `sc_tpu.sdelay`). Those direct lowerings are the SparseCore analogue of the LowerToLLO bodies and are surveyed on [tpu → LLO ODS](tpu-to-llo-ods.md) for the TensorCore side; this page does not re-table them.

The remaining **four** ops are the DMA-and-sync family. They lower to **nothing** in LowerToMlo. Instead each emits a `builtin.unrealized_conversion_cast` — the *bridge-cast* — that carries the original op's operands forward in the converted (Mlo) type system and is tagged for a later pass to find.

| `tpu` source op | LowerToMlo lambda | Lowers to | Stage |
|---|---|---|---|
| `tpu.enqueue_dma` | `0x13239e00` | tagged `builtin.unrealized_conversion_cast` | **bridge** |
| `tpu.enqueue_indirect_dma` | `0x1323a660` | tagged `builtin.unrealized_conversion_cast` | **bridge** |
| `tpu.wait_dma2` | `0x1323ae00` | tagged `builtin.unrealized_conversion_cast` | **bridge** |
| `tpu.fetch_and_add_sync` | `0x1323b600` | tagged `builtin.unrealized_conversion_cast` | **bridge** |
| `tpu.wait_indirect_dma` | `0x1323be00` | `sc_tpu.stream_wait` (`StreamWaitOp`) | direct |

> **NOTE —** `tpu.wait_indirect_dma` (`0x1323be00`) is *not* a bridge op — it lowers directly, emitting a `sparse_core::StreamWaitOp` (`sc_tpu.stream_wait`) after computing dynamic sizes and granule counts (`lowering_util::GetDynamicSizes` / `AssertAlignmentAndGetNumGranules`). Only the four ops above defer. The reason for the split is structural: an `enqueue_dma`'s final SparseCore form (rolled / retiled transfer loop, granule decomposition, host-IOVA vs intra-chip routing) depends on the *tiled* memref layout, which is not resolved until `ExpandTiledMemRefsPass` runs. LowerToMlo cannot know the shape, so it preserves the operands behind a typed cast and lets the tile-aware pass finish the job. This is confirmed by the consumer being `ExpandTiledMemRefsPass::addPattern<tpu::EnqueueDMAOp>` (`0x134eef60`), `addPattern<tpu::WaitDMA2Op>` (`0x134ef720`), `addPattern<tpu::FetchAndAddSyncOp>` (`0x134f1240`), `addPattern<tpu::EnqueueIndirectDMAOp>` (`0x134ef340`) — the four bridge ops are exactly the four ops that pass re-handles.

---

## Stage 1 — The DMA Bridge-Cast (`tpu.enqueue_dma` → tagged cast)

### Purpose

This is the heart of the page: the exact emission a reimplementer must reproduce so that the deferred op survives full conversion and is recoverable downstream. The `tpu.enqueue_dma` lambda body (`0x13239e00`) is decompiled in full; the other three bridge ops follow the same shape.

### Algorithm

The lambda walks the op's operands, replaces each operand whose **converted type differs** from its source type with a per-operand `builtin.unrealized_conversion_cast` (tagged `sc.unlowering`), then marks the *whole source op* with `sc.unlowered` and re-inserts it as legal-in-transit. It does **not** erase or replace the op — the op survives, now type-bridged.

```c
// LowerToMlo EnqueueDMAOp lambda — @ 0x13239e00
// (lower_to_mlo.cc:405; same shape for EnqueueIndirectDMA / WaitDMA2 / FetchAndAddSync)
LogicalResult lower_enqueue_dma(EnqueueDMAOp op, EnqueueDMAOpAdaptor adaptor,
                                ConversionPatternRewriter& rw):
    srcOperands = op.getOperands()             // type-pointer list @ op+0x48
    newOperands = adaptor remapped operands    // ValueRange (already type-converted)

    // --- fast pre-scan: are ALL operand types already equal? ---
    //   compares (type_ptr & ~7) of src[i] vs remapped[i]  (line 102 / 137)
    if every src operand type == its remapped type:
        goto NO_REWRITE                         // nothing to bridge; op already legal

    rw.startRootUpdate(op)                       // [rw_vtable+0x28] (line 111)
    for i, (srcVal, newVal) in zip(srcOperands, newOperands):
        if (srcVal.type & ~7) != (newVal.type & ~7):          // type changed
            loc  = LocationGenerator::Visitor(op.getLoc())     // sc-location synthesis
            castTy = TypeRange{ newVal.type }                  // 1 result type (line 162)
            cast = UnrealizedConversionCastOp::create(rw, loc, castTy, srcVal)  // (line 175)
            cast->setAttr("sc.unlowering", UnitAttr::get(ctx))                  // (line 176-182)
            // splice: replace srcOperand[i] use with the cast result (intrusive list, line 183-199)
            op.setOperand(i, cast.getResult(0))
    op->setAttr("sc.unlowered", UnitAttr::get(ctx))            // mark source op (line 212-219)
    rw.finalizeRootUpdate(op)                    // [rw_vtable+0x30] (line 221)

NO_REWRITE:
    // trace-region tagging (independent of the bridge)
    if pass.insideTraceRegion:                   // v62[71] (line 224)
        op->setAttr("sc.inside_trace_region", UnitAttr::get(ctx))   // (line 226-232)
    return success                               // return 1 (line 234)
```

### The three attribute tags

The emission deposits up to three `UnitAttr` markers — all confirmed as `.rodata` string literals consumed by the stage-2 pass:

| Attribute | Set on | Meaning | Confidence |
|---|---|---|---|
| `sc.unlowering` | each emitted `builtin.unrealized_conversion_cast` | "this cast bridges a not-yet-lowered operand into the Mlo type system; the stage-2 pass owns it" | CERTAIN |
| `sc.unlowered` | the source `tpu.enqueue_dma` op itself | "this op was intentionally left un-lowered by LowerToMlo; expand it during tile expansion" | CERTAIN |
| `sc.inside_trace_region` | the source op, only if the pass is mid-trace-region | propagates trace nesting so the expanded DMA stays inside the trace scope | CERTAIN |

> **CORRECTION (MLO-DMA-1) —** an earlier reading of this pass left the bridge-cast attribute set untraced and conjectured the consumer was `LowerToSparseCoreLlvmPass` (`0x13566d00`). The decompiled `0x13239e00` body pins the tags exactly: `sc.unlowering` on the per-operand cast (`UnitAttr`, set at line 182), `sc.unlowered` on the source op (line 219), and `sc.inside_trace_region` when nested (line 232). The downstream consumer is `substituteUnloweringConversionCastOp` (`0x134e73e0`), registered by **`ExpandTiledMemRefsPass`** — not `LowerToSparseCoreLlvm`. The bridge therefore resolves one stage *earlier* than previously assumed.

> **GOTCHA —** the bridge is **per-operand selective**, not whole-op. The pre-scan (line 95-108) and the inner loop (line 137) compare the low-3-bits-masked type pointer of each source operand against its remapped operand; only operands whose type actually changed get a cast. An `enqueue_dma` whose operands all keep their type emits **no** cast and merely gets the `sc.unlowered` marker. A reimplementation that casts every operand unconditionally will emit dead identity casts the stage-2 pass must then strip.

> **NOTE —** the lambda uses `startRootUpdate` / `finalizeRootUpdate` (the in-place modification protocol, `rw_vtable+0x28` / `+0x30`), not `replaceOp`. The mutation is therefore logged as a `ModifyOperationRewrite` record (the in-place-modification record on [ConversionPatternRewriter](conversion-pattern-rewriter.md)), and the inserted casts as `CreateOperationRewrite` + `UnresolvedMaterializationRewrite` records — so the whole bridge emission is rollback-safe under the speculative legalizer.

---

## Stage 1b — Block-Signature Conversion (the `func::FuncOp` legality predicate)

### Purpose

Before any DMA op is reached, LowerToMlo must decide which functions need their *signature* rewritten through the `TypeConverter`. SparseCore code is structured as sequencer functions whose argument/result types (memref memory-space attrs, `TupleType` 1:N pairs, `WordType`) must be converted; ordinary functions are left alone. This is the block-signature side of the lowering, and it is driven entirely by a dynamic-legality predicate.

### The three dynamic-legality predicates

`runOnOperation` (`0x1322b200`) installs three legality callbacks via `addDynamicallyLegalOp` (the call census shows four `addDynamicallyLegalOp` invocations + two static `setOpAction`; the four resolve to the three distinct predicate lambdas below, one of which is registered for two op classes). All three return a 16-bit value where bit 0 = legal-now and bit 8 (`0x100`) = "answer present" (the form `ConversionTarget::isLegal` reads).

| Predicate | Lambda | Op(s) it gates | Confidence |
|---|---|---|---|
| `$_0` FuncOp signature | `0x13231300` | `func::FuncOp` | HIGH |
| `$_1` cast-in-transit | `0x13231560` | `builtin.UnrealizedConversionCastOp` | HIGH |
| `$_2` result-type catch-all | `0x132315e0` | the `OpResultTypeConversionPattern` target | HIGH |

#### `$_0` — `func::FuncOp` legality (`0x13231300`)

```c
// func::FuncOp dynamic legality — @ 0x13231300
optional<bool> func_is_legal(FuncOp op):
    core   = TPUDialect::GetCoreTypeAttr(op)         // 0x14aa6020
    seqTy  = LowerMemrefToMlo::getSequencerType(op)  // 0x13507760
    // is this a SparseCore SEQUENCER function?  (op-name compares vs "sc"/"execute")
    if not is_sequencer(core, seqTy):
        return legal                                  // 0x100 — no signature rewrite
    // it IS a sequencer: walk the signature, every type must already be converted
    for ty in FunctionType.getInputs() ++ getResults():
        if typeConverter.convertType(ty) != ty:
            return illegal                            // signature still needs conversion
    return legal_recursive                            // 0x101 when input/result lists empty
```

A function is **legal as-is** (no signature rewrite) unless it is a SparseCore *sequencer* function — detected by `TPUDialect::GetCoreTypeAttr` (`0x14aa6020`) plus `LowerMemrefToMlo::getSequencerType` (`0x13507760`), which test the function's core-type attribute and name against the ASCII tokens `"sc"` and `"execute"`. For a sequencer function, the predicate walks `getInputs()` ++ `getResults()` and calls `TypeConverter::convertType` on each: the function is legal iff *every* arg and result type already converts to itself. If any type still needs conversion the FuncOp is **illegal**, which fires the FuncOp signature-conversion pattern (`0x13231ca0`) to rewrite the block signature through the converter.

> **NOTE —** the FuncOp signature-conversion pattern (`0x13231ca0`) is what performs the actual block-argument rewrite. The 1:N type expansions (a `TupleType` argument splitting into multiple SparseCore scalars, an `I32Pair`) drive block-argument multiplication; the rollback of those block-level edits (`BlockTypeConversionRewrite`) is the block-rewrite half of the action log documented on [ConversionPatternRewriter](conversion-pattern-rewriter.md).

#### `$_1` — `UnrealizedConversionCastOp` legality (`0x13231560`)

This is the predicate that lets the **bridge-cast pass through the legalizer in transit**. Without it, the unrealized cast the DMA lambda just emitted would itself be reported illegal and the pass would abort.

```c
// UnrealizedConversionCastOp dynamic legality — @ 0x13231560
optional<bool> cast_is_legal(UnrealizedConversionCastOp op):
    attr = op.getInherentAttr("sc.unlowering")        // len 0xd; falls back to DictionaryAttr lookup
    if attr is absent:
        return 0x100                                  // "answer present", low bit clear → ILLEGAL
    // legal iff that attribute is the UnitAttr the bridge emits
    return (attr.getTypeID() == UnitAttr::id) | 0x100
```

A cast is legal **only when it carries the `sc.unlowering` `UnitAttr`** — exactly the tag the DMA bridge lambda deposits on each per-operand cast at `0x13239e00`. Any unrealized cast without that tag (e.g. a stray reconciliation cast the type-converter inserts at a signature boundary) is reported illegal (`0x100` with the low bit clear) and must be materialized by the rewriter. The predicate is thus a direct lock-and-key pairing with the Stage-1 emission: the lambda writes `sc.unlowering`, this predicate admits exactly those casts.

#### `$_2` — `OpResultTypeConversionPattern` target legality (`0x132315e0`)

```c
// runOnOperation $_2 — @ 0x132315e0
optional<bool> result_types_legal(Operation* op):
    // fast accept: op's own dialect is in the target's legal-dialect DenseSet
    if op.getDialect().getTypeID() in MloConversionTarget.legalDialects:
        goto CHECK_RESULTS
    // the four DMA bridge ops are legal iff already tagged sc.unlowered (UnitAttr)
    if op is {EnqueueDMAOp, EnqueueIndirectDMAOp, WaitDMA2Op, FetchAndAddSyncOp}:
        u = op.getInherentAttr("sc.unlowered")        // len 0xc
        return u && (u.getTypeID() == UnitAttr::id) ? 0x101 : 0x100
    // memref-reshaping ops (EraseLayout / MemRefSqueeze / MemRefBitcast /
    //   MemRefSlice / ReinterpretCast / AssumeMultiple) are legal only when
    //   no operand/result still carries a tpu::TiledLayoutAttr memref
    ...                                               // TiledLayoutAttr guards
CHECK_RESULTS:
    for ty in op.getResultTypes():
        if typeConverter.convertType(ty) != ty:
            return 0x100                              // a result still needs conversion
    return 0x101                                      // legal (0x101 when no results)
```

The catch-all `OpResultTypeConversionPattern` (the `MatchAnyOpTypeTag` pattern, vtable `0x21903b40`, `matchAndRewrite` `0x132456e0` → `mlir::convertOpResultTypes` `0x1c9572c0`) is the generic 1:1 result-type fixer for any op the explicit patterns do not cover: when an op's operands were remapped, this pattern converts the op's result types. `$_2` is its legality gate — after the dialect / bridge-op / tiled-layout fast paths, an op is legal iff `TypeConverter::convertType` maps **every** result type to itself (i.e. no result still needs conversion).

> **NOTE (low confidence) —** `$_2` is not a pure result-type predicate: the decompiled body at `0x132315e0` first short-circuits on the op's dialect membership in the `MloConversionTarget` legal-dialect set, then special-cases the four DMA bridge ops (legal once `sc.unlowered`-tagged) and the memref-reshaping ops (gated on `tpu::TiledLayoutAttr`), and only the fall-through path runs the `convertType(ty) == ty` result-type loop. The pseudocode above is the verified shape; the exact branch ordering of the tiled-layout guards is summarized, not byte-decoded.

---

## Stage 2 — Deferred-DMA Materialization Resolution

### Purpose

The bridge-cast is only half the story; the deferral has to be *resolved* eventually. This unit pins where and how the tagged cast becomes a real SparseCore DMA — closing the loop the LowerToMlo lambda opens.

### Resolution

`applyFullConversion` (the LowerToMlo driver) finishes with the four DMA ops still present, now marked `sc.unlowered`, their changed operands fed by `sc.unlowering`-tagged `builtin.unrealized_conversion_cast` ops, and the cast-legality predicate (`$_1`) having declared those casts legal-in-transit so the pass does **not** abort on them.

A later SparseCore pass, **`ExpandTiledMemRefsPass`**, owns stage 2. It registers conversion patterns for exactly the four bridge ops:

| Stage-2 registration | VA |
|---|---|
| `ExpandTiledMemRefsPass::addPattern<tpu::EnqueueDMAOp>` | `0x134eef60` |
| `ExpandTiledMemRefsPass::addPattern<tpu::EnqueueIndirectDMAOp>` | `0x134ef340` |
| `ExpandTiledMemRefsPass::addPattern<tpu::WaitDMA2Op>` | `0x134ef720` |
| `ExpandTiledMemRefsPass::addPattern<tpu::FetchAndAddSyncOp>` | `0x134f1240` |
| `sparse_core::expandTPUFetchAndAddSync(FetchAndAddSyncOp, adaptor, rw)` | `0x134e60c0` |
| `sparse_core::substituteUnloweringConversionCastOp(UnrealizedConversionCastOp, adaptor, rw)` | `0x134e73e0` |

The cast resolver `substituteUnloweringConversionCastOp` (`0x134e73e0`) is the function that finds an `sc.unlowering`-tagged cast and substitutes it — undoing the bridge once the tiled-memref layout is known, so the now-shaped DMA op can be expanded into the real SparseCore transfer ops (the rolled / retiled / granule-decomposed transfer chain). Because `ExpandTiledMemRefsPass` runs after the memref tiling is resolved, this is the first point at which the DMA's final form is computable — which is exactly why LowerToMlo deferred it.

> **NOTE —** the real DMA emission (the `LloRegionBuilder::EnqueueDmaGeneral` / `EnqueueDmaInGranules` / `EnqueueDmaToHostIova` family, `0x1d543600` / `0x1d546700` / `0x1d548b20`, and the rolled/retiled transfer helpers) is the *content* of stage 2 and is owned by the SparseCore-LLVM lowering pages. This page's claim stops at the bridge contract: *what tag is written, which pass reads it, which resolver substitutes it.* The granule-level transfer algebra is cross-referenced, not re-derived.

### End-to-end trace

```text
  STAGE 1 — LowerToMlo (ModuleOp pass, applyFullConversion)
    tpu.enqueue_dma %src, %dst, %sflag, ...                 (operands in tpu types)
      └─ lambda 0x13239e00:
           per changed operand → %c = builtin.unrealized_conversion_cast %x
                                       {sc.unlowering}        (→ Mlo type)
           op.setAttr("sc.unlowered")                        (+ sc.inside_trace_region?)
      ⇒ op SURVIVES, type-bridged; cast declared legal by $_1 (0x13231560)
  ── applyFullConversion succeeds with the bridge intact ──
  STAGE 2 — ExpandTiledMemRefsPass (after memref tiling resolved)
    addPattern<EnqueueDMAOp> 0x134eef60
      └─ substituteUnloweringConversionCastOp 0x134e73e0   (resolve sc.unlowering casts)
      └─ expand sc.unlowered DMA → real SparseCore transfer ops
           (EnqueueDmaGeneral / InGranules / ToHostIova: 0x1d543600 / 0x1d546700 / 0x1d548b20)
```

---

## What Is Not On This Page

- **The generic rollback engine** — the `IRRewrite` 12-record action log, `undoRewrites` (LIFO), `resetState`, `applyRewrites` (two-pass commit), and the `UnresolvedMaterializationRewrite` record that backs every cast the bridge emits — is owned by [ConversionPatternRewriter](conversion-pattern-rewriter.md). This page references those records but does not decode them.
- **The depth-aware legalize-to-fixpoint loop** — the cost model and `ConversionTarget` legality enum that decide *which* pattern fires and *in what order* — is owned by [DialectConversion Legalizer](dialect-conversion-legalizer.md).
- **The 40 direct (non-bridge) tpu→Mlo pattern bodies** — `tpu.iota`, `tpu.device_id`, `tpu.semaphore_signal`, `tpu.delay`, and the rest — are surveyed for the TensorCore analogue on [tpu → LLO ODS](tpu-to-llo-ods.md); only the four DMA bridge ops are this page's subject.
- **The stage-2 transfer algebra** — the granule decomposition, rolled/retiled transfer loop nest, and the `LloRegionBuilder::EnqueueDma*` emission — is owned by [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md) and the [DMA](../dma/rolled-strided-general.md) pages; this page ends at the bridge-resolution boundary.
- **The `TypeConverter` callback bodies** — the 10 `registerConversion` + 3 `registerTypeAttributeConversion` rules (memref memory-space attrs, `TupleType` 1:N, `WordType`) shared with the SparseCore type system — are owned by [SCTypeConverter](sc-type-converter.md).

---

## Cross-References

- [ConversionPatternRewriter](conversion-pattern-rewriter.md) — the `IRRewrite` action log + `undoRewrites`/`resetState`/`applyRewrites` rollback engine that makes the bridge emission speculative-safe; the `UnresolvedMaterializationRewrite` record behind each cast
- [DialectConversion Legalizer](dialect-conversion-legalizer.md) — the depth-aware legalize-to-fixpoint cost model and `ConversionTarget` legality enum this pass drives
- [tpu → LLO ODS Lowering](tpu-to-llo-ods.md) — the TensorCore `FunctionPass` sibling; the per-op ODS signatures and the LowerToLLO `EnqueueDMAOp` direct realization
- [The tpu MLIR Dialect](tpu-dialect-and-ops.md) — the source dialect this pass consumes; the `tpu.enqueue_dma` / `tpu.wait_dma2` / `tpu.fetch_and_add_sync` op surface
- [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md) — the per-class SparseCore→LLVM rewrite bodies, including the real DMA transfer emission downstream of `ExpandTiledMemRefsPass`
- [SCTypeConverter](sc-type-converter.md) — the shared SparseCore `TypeConverter` whose `convertType` the FuncOp legality predicate calls
- [The TPU Compiler](overview.md) — the five-phase dialect descent overview where LowerToMlo sits
- [Compile Phases](compile-phases.md) — the ordered phase sequence placing `lower-to-mlo` before tile expansion
- [Rolled / Strided / General Transfer](../dma/rolled-strided-general.md) — the granule-level transfer the stage-2 pass emits once the bridge is resolved
- [Tile Index Expansion](../dma/tile-index-expansion.md) — the tiled-memref resolution that `ExpandTiledMemRefsPass` performs before substituting the bridge-cast
