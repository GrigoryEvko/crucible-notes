# CanonicalizeForTensorizer

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The pass lives in `neuronxcc/starfish/bin/hlo2penguin`, compiled from `hilo/MLIRPasses/Transforms/CanonicalizeForTensorizer.cc`. The binary is built with `NVOPEN_IDA_SKIP_DECOMPILE`, so every body here is recovered from disassembly, not Hex-Rays; `.text`/`.rodata` VA equals file offset. Other wheels differ — treat every address as version-pinned.*

## Abstract

`CanonicalizeForTensorizer` is the MHLO module-level cleanup pass that runs immediately before the dialect crosses from MHLO into the Penguin tensorizer. It is the **MLIR→Penguin legalization gate**: a small set of peephole rewrites that normalize the handful of MHLO constructs the tensorizer cannot ingest into the canonical forms it can. Think of it as the analogue of LLVM's `InstCombine` plus `-canonicalize` run once at a dialect boundary — not a fixed-point rewriter, but a single forward sweep of eight hand-written `walk`-and-mutate rewriters, each matching exactly one MHLO op kind by `TypeID` and replacing it in place.

The eight rewriters fall into three duties. **Operand/structure pruning** removes IR that carries information the tensorizer reconstructs intrinsically: `fuseIotaSort` drops the synthetic iota index-ramp from an argsort, and `canonicalizeTupleOp` folds `get_tuple_element(tuple(...), i)` to the i-th operand and DCEs the dead tuple. **Constant materialization** evaluates any op whose input is already a literal so the tensorizer never sees a compile-time-foldable computation: `replaceGetDimensionSize` (the dynamic-shape glue — folds `get_dimension_size` on a static dim to an `i32` constant), `replaceConvertsWithConstants`, and `replaceBroadcastInDimWithConstants` (under a configurable element budget). **Op-form legalization** rewrites legacy or back-end-hostile op kinds into the tensorizer's accepted vocabulary: `replaceBroadcastsWithBroadcastInDim` (legacy `mhlo.broadcast` → `mhlo.broadcast_in_dim`), `legalizeBackendConfig`/`replaceInvalidBackendConfigs` (sanitizes an `AwsNeuronErf` custom-call config), and `lowerResizeNearestGrad` (lowers the `ResizeNearestGrad` custom-call to an average-pool of `reduce_window` + `divide`).

The class name is `CanonicalizeForTensorizer`, but its registered pass-argument is **`canonicalize-for-penguin`** — a naming seam worth flagging up front (see [the GOTCHA below](#pass-identity-and-the-stablehlo-twin)). A StableHLO twin, `StableHLOCanonicalizeForTensorizer`, carries a structurally identical rewriter set over `stablehlo::*` ops (pass-arg `stablehlo-canonicalize-for-penguin`) plus one extra rewriter the MHLO side lacks.

For reimplementation, the contract is:

- The eight-rewriter dispatch order out of `runOnOperation`, and why the two broadcast rewriters must run last and in their specific relative order.
- The shared "collect-then-mutate" idiom: a `PostOrder` `mlir::detail::walk` keyed by `TypeIDResolver<Op>::id`, a `SmallVector` worklist, then a second loop that mutates each match by inlined `replaceAllUsesWith` + `Operation::erase`, with dead-source erase gated by an all-results-`use_empty` check.
- Each rewriter's match predicate and normalized output form, with full treatment of `replaceGetDimensionSize` (static-shape const-fold) and `lowerResizeNearestGrad` (the average-pool lowering).
- The MLIR `Operation` field offsets this build uses (`+0x24` numResults, `+0x2E` operand-storage gate, `+0x44` numOperands, `+0x48` storage base, OpOperand stride `0x20`, value at `+0x18`).

| | |
|---|---|
| **Pass class** | `CanonicalizeForTensorizer` (global namespace, not `hilo::`) |
| **Pass-arg (CLI)** | `canonicalize-for-penguin` (string @ `0x21a240`) — **not** `canonicalize-for-tensorizer` |
| **Driver** | `runOnOperation` @ `0x20869d0` (629 B) → `hilo::getMainFunction(ModuleOp&)` |
| **Factory** | `mlir::createCanonicalizeForTensorizerPass()` @ `0x207d710` |
| **IR level** | MHLO module, pre-tensorizer (the MLIR→Penguin boundary) |
| **Rewriter count** | 8 (MHLO); StableHLO twin has 9 (extra `removeDeadZeroSizedTensorOps`) |
| **StableHLO twin** | `StableHLOCanonicalizeForTensorizer::runOnOperation` @ `0x21266b0`, pass-arg `stablehlo-canonicalize-for-penguin` |
| **Element-budget knob** | `broadcast-num-elements-to-fold` (str @ `0x39e1a0`; global @ `0x9c719f8`) |

---

## The dispatch driver and the shared rewriter idiom

### Purpose

`runOnOperation` is the only entry point. It does no rewriting itself: it resolves the module's main function and calls the eight rewriters in a fixed sequence. Every rewriter operates on that one `func::FuncOp`. There is no env-var or `cl::opt` gate on the pass as a whole — once scheduled, all eight rewriters run unconditionally (only `replaceBroadcastInDimWithConstants` carries an internal budget gate).

### Entry Point

```text
CanonicalizeForTensorizer::runOnOperation @0x20869d0 (629 B)
  └─ hilo::getMainFunction(ModuleOp&)            ── resolve the main func
       1. fuseIotaSort(FuncOp&)                       @0x207ffb0   walks mhlo::SortOp
       2. canonicalizeTupleOp(FuncOp&)                @0x2083180   walks mhlo::GetTupleElementOp
       3. replaceGetDimensionSize(FuncOp&)            @0x2080240   walks mhlo::GetDimensionSizeOp
       4. replaceConvertsWithConstants(FuncOp&)       @0x20807c0   walks mhlo::ConvertOp
       5. legalizeBackendConfig walk (CustomCallOp) → replaceInvalidBackendConfigs @0x2085df0
       6. lowerResizeNearestGrad walk (CustomCallOp)  lambda @0x207df40
       7. replaceBroadcastsWithBroadcastInDim(FuncOp&) @0x2083f90  walks mhlo::BroadcastOp
       8. replaceBroadcastInDimWithConstants(FuncOp&)  @0x20815f0  walks mhlo::BroadcastInDimOp
```

> **NOTE —** the dispatch order is read verbatim from the `runOnOperation` disasm `0x2086a20`–`0x2086bda` (CERTAIN). The relative order of the last two rewriters is deliberate and not interchangeable: **#7 must precede #8.** `replaceBroadcastsWithBroadcastInDim` turns a legacy `mhlo.broadcast(const)` into `mhlo.broadcast_in_dim(const)`; only then can `replaceBroadcastInDimWithConstants` materialize it to a literal. Running them in the same sweep collapses `broadcast(const) → broadcast_in_dim(const) → const` in one `runOnOperation`. Reversing them would leave every legacy `broadcast` of a constant unfolded.

### Algorithm — the collect-then-mutate skeleton

All eight rewriters share one idiom. A `PostOrder` (`WalkOrder=1`) `mlir::detail::walk` drives a `llvm::function_ref` callback that matches one op kind by `TypeID` equality and appends matches to a `SmallVector<Operation*>` worklist; a second loop then mutates each. Separating collection from mutation is the standard MLIR safety pattern — mutating during the walk would invalidate the walk's iterators.

```c
// The shared shape of every rewriter (e.g. replaceGetDimensionSize @0x2080240)
function rewriter(FuncOp f):
    worklist = SmallVector<Operation*>{}          // inline cap baked into the seed const
    // PHASE 1 — collect
    walk(f.body, PostOrder, [&](Operation* op) {  // mlir::detail::walk, WalkOrder=1
        if (op->getTypeID() == TypeIDResolver<TargetOp>::id)   // cmp vs a .data id addr
            worklist.push_back(op)                // SmallVectorBase::grow_pod on overflow
    })
    // PHASE 2 — match + rewrite
    for (op : worklist):
        if (!match_predicate(op)): continue       // per-rewriter legality gate, silent skip
        replacement = build_or_compute(op)        // new op via OperationState+OpBuilder, or an operand
        replaceAllUsesWith(op, replacement)       // inlined ResultRange use-list splice
        Operation::erase(op)
        maybe_erase_dead_source(op)               // gated by all-results-use_empty __find_if
```

The match is always `TypeID` equality, never a string compare: `op->getName().getTypeID()` is compared against the `TypeIDResolver<mhlo::XxxOp,void>::id` cs-relative `.data` address for the target op. The relevant id anchors in this build (`_names.json`, all `.data`-resident):

| Op | `TypeIDResolver::id` | | Op | `TypeIDResolver::id` |
|---|---|---|---|---|
| `mhlo::SortOp` | `0x9d304c0` | | `mhlo::IotaOp` | `0x9d30640` |
| `mhlo::GetTupleElementOp` | `0x9d30660` | | `mhlo::TupleOp` | `0x9d30460` |
| `mhlo::GetDimensionSizeOp` | `0x9d30668` | | `mhlo::ConstantOp` | `0x9d30748` |
| `mhlo::RankedTensorType` | `0x9d3fa00` | | (null void-TypeID) | `0x9d3fb48` |

> **QUIRK —** the operand/result accessors are raw struct offsets in this build, not virtual calls. A reimplementer reading the bodies must know the MLIR `Operation` layout this build uses: `[op+0x24]` = numResults, `[op+0x44]` = numOperands, `[op+0x48]` = `OperandStorage` base with OpOperand stride `0x20` and `operand[i].value` at `[+0x18 + i*0x20]` (so `operand[0].value=[+0x18]`, `operand[1].value=[+0x38]`). `[op+0x2E]` is an operand-storage flag whose **high bit must be set** before operands are readable (`cmp byte[op+2Eh],0 ; jns skip`); every rewriter guards on it. These offsets are consistent across all eight bodies (HIGH; no `Operation` struct in `_structures.json`, so the bitfield semantics of `+0x2E` are inferred, not declared).

Every op the rewriters *build* is guarded by `RegisteredOperationName::lookup(TypeID<Op>, ctx)`; a NULL return triggers `llvm::report_fatal_error("Building op `<mnemonic>` but it isn't known in this MLIRContext: …")` (tail string @ `0x2df9e8`; e.g. mnemonic `mhlo.constant` @ `0x24a511`). This is the standard MLIR builder guard, not a Neuron diagnostic.

---

## fuseIotaSort — strip the iota index-operand of an argsort

### Purpose

XLA materializes argsort / top-k index tracking as `mhlo.sort(%data, %indices)`, where `%indices = mhlo.iota` is a synthetic index ramp threaded as a *second* comparand so the sort permutes indices alongside values. The Penguin tensorizer tracks the permuted indices intrinsically inside its sort lowering, so the explicit iota operand is dead weight. `fuseIotaSort` prunes it. The name is a misnomer: it builds no fused op — "fuse" here means folding the index ramp into the intrinsic sort, not an `mhlo.fusion`.

### Algorithm

```c
// CanonicalizeForTensorizer::fuseIotaSort @0x207ffb0 (644 B, 36 BB) — no diag strings
function fuseIotaSort(FuncOp f):
    worklist = collect<mhlo::SortOp>(f)               // seed cap 100 (0x6400000000); TypeID cmp 0x9d304c0
    for (sort : worklist):
        if (!(sort[0x2E] & 0x80)): continue           // 0x2080110, operands readable
        if (sort.numOperands != 2): continue          // 0x2080118, EXACTLY two operands
        iota = sort.operand(1).getDefiningOp()        // 0x2080120, operand[1].value=[+0x38]
        if (iota.getTypeID() != mhlo::IotaOp::id): continue   // 0x2080154, cmp 0x9d30640
        // matched: %r = mhlo.sort(%data, %iota)
        eraseOperands(sort.storage, /*start=*/1, /*count=*/1)  // 0x208016b, drop the iota operand
        if (all_results_use_empty(iota)):             // 0x208018a __find_if<!use_empty> over results
            Operation::erase(iota)                    // 0x208019b — DCE the now-orphaned iota
```

The data operand (`operand[0]`) is unconstrained — only the second comparand must be an `iota`. The sort op is not recreated; only its operand list is shortened in place, so its SSA result and users are untouched. The iota is erased only if every one of its results is now `use_empty` (it may be shared with other consumers, in which case it stays).

**Net:** `mhlo.sort(%data, mhlo.iota)` ⇒ `mhlo.sort(%data)` + conditional DCE of the iota. Match CERTAIN; the "argsort/top-k index tracking" intent is HIGH (inferred from the iota-as-second-comparand structure; no string anchor).

---

## canonicalizeTupleOp — fold get_tuple_element + DCE dead tuples

### Purpose

The tensorizer is a flat-value IR with no first-class tuples. `canonicalizeTupleOp` eliminates the `tuple`/`get_tuple_element` pair that XLA uses to bundle multi-output ops: it folds `get_tuple_element(tuple(a0..an), i)` to `a_i`, then erases any `tuple` producer left with no users. The reverse identity `tuple(gte(x,0), …, gte(x,n)) → x` is *emergent*, not a dedicated matcher — once the GTEs fold to the underlying operands, the wrapping tuple becomes use-empty and is DCE'd in the same pass.

### Algorithm

```c
// driver @0x2083180 (1359 B); per-GTE lambda @0x2082e70 (727 B)
function canonicalizeTupleOp(FuncOp f):
    gtes      = SmallVector<Operation*>{}     // pass member [this+0x150]
    producers = DenseSet<Operation*>{}        // stack var_170; distinct defining ops of the GTEs

    // PHASE 1 — lambda over each GetTupleElementOp: pure bookkeeping, no mutation
    walk<mhlo::GetTupleElementOp>(f, [&](gte) {        // @0x2082e70
        gtes.push_back(gte)                            // 0x2082ea6
        assert(gte[0x2E] & 0x80)                       // 0x2082eb5, else ud2/trap
        producers.insert(gte.operand(0).getDefiningOp())   // 0x2082ede, hash (h>>4)^(h>>9)
    })

    // PHASE 2 — fold gte(tuple,i) → operand_i
    for (op : gtes):                                   // 0x20832a0
        if (!(op[0x2E] & 0x80)): continue              // 0x20832cc
        producer = op.operand(0).getDefiningOp()       // 0x20832dd
        if (producer.getTypeID() != mhlo::TupleOp::id): continue   // 0x2083316, cmp 0x9d30460
        i   = GetTupleElementOp::getIndex(op)          // 0x2083329 @0x8f7d6a0
        src = producer.operand(i).value                // 0x2083330: (i<<5)+[producer+0x48] → [+0x18]
        replaceAllUsesWith(op.results, src)            // 0x208336d..: manual RAUW over result range
        if (op.results_use_empty()): Operation::erase(op)   // 0x208351f

    // PHASE 3 — erase now-dead tuple producers
    for (p : producers):                               // 0x208353a, skip empty(0xF000)/tombstone(0xE000)
        if (p.results_use_empty()):
            splice_residual_uses(p); Operation::erase(p)    // 0x2083693
```

The producer `DenseSet` (sentinels: empty `0xFFFF…F000`, tombstone `0xFFFF…E000`; backing freed via `llvm::deallocate_buffer`) holds the candidate `tuple` ops to recheck for deadness after the fold loop. Phase 2's index read `(i<<5)+[producer+0x48]` is the OpOperand-stride (`0x20`) arithmetic that reaches the i-th operand's `Value` at `+0x18`.

**Net:** (a) `get_tuple_element(tuple(a0..an), i)` ⇒ `a_i`; (b) dead `mhlo.tuple` producers ⇒ erased; (b) subsumes `tuple(gte(x,*)) → x` as an emergent identity. Direction (a) CERTAIN; direction (b) HIGH (structural, no named matcher).

---

## replaceGetDimensionSize — the dynamic-shape glue

### Purpose

`mhlo.get_dimension_size %t, dimension = d` reads one dimension of a (possibly dynamic) tensor at run time. It is the only place the MHLO dynamic-shape front-end ([4.31](mlir-dynamic-shape-frontend.md)) leaks a "shape query" op into the graph. The Penguin tensorizer is a **static-shape** IR — it has no run-time dimension-read op. `replaceGetDimensionSize` is the bridge: whenever the queried dimension is statically known (the common case, since the bucketing front-end resolves shapes before this point), it const-folds the query to a scalar `i32` `mhlo.constant` holding the size. Only genuinely dynamic dims or unranked operands are left for a downstream handler.

This is the central reason the pass exists: it is the one rewriter that turns a *dynamic-shape* construct into a *static-shape* literal, and it is precisely the form the tensorizer requires.

### Algorithm

```c
// CanonicalizeForTensorizer::replaceGetDimensionSize @0x2080240 (1393 B) — CERTAIN
function replaceGetDimensionSize(FuncOp f):
    worklist = collect<mhlo::GetDimensionSizeOp>(f)   // seed cap 6 (0x600000000); TypeID cmp 0x9d30668
    kDynamic = 0x8000000000000000                     // ShapedType::kDynamic, loaded @0x2080383
    for (op : worklist):
        operand = op.operand(0).value                 // 0x208039d, [+0x18]
        d       = GetDimensionSizeOp::getDimension(op)// 0x20803ab, the queried dim index
        ty      = operand.getType()
        if (ty.getTypeID() != mhlo::RankedTensorType::id): continue   // 0x20803be, cmp 0x9d3fa00
        shape = RankedTensorType::getShape(ty)        // 0x20803e1
        if (shape[d] == kDynamic): continue           // dim d MUST be static (else skip @0x2080390)

        // REWRITE — materialize the static size as a scalar i32 constant
        shape   = ShapedType::getShape(operand)       // 0x208048b (lazy ShapedType TypeID register)
        dimSize = (i32) shape[d]                       // 0x20804ec
        resultTy = cast<ShapedType>(op.result.getType())   // 0x2080501 — tensor<i32>
        attr = DenseElementsAttr::getRawIntOrFloat(    // 0x208052a — splat scalar int
                  resultTy, ArrayRef<char>{&dimSize, 4},
                  /*dataEltSize=*/4, /*isInt=*/true, /*isSigned=*/true)
        name = RegisteredOperationName::lookup(mhlo::ConstantOp, ctx)   // 0x2080556
        if (!name): report_fatal_error("Building op `mhlo.constant` …")  // 0x208073d
        cst = OpBuilder::create(mhlo::ConstantOp::build(state, attr))    // 0x20805a1/0x20805b4
        replaceAllUsesWith(op.results, cst)           // 0x2080615
        Operation::erase(op)                          // 0x208061d
```

The lazy `ShapedType` TypeID registration (`__cxa_guard` + `FallbackTypeIDResolver::registerImplicitTypeID("mlir::ShapedType")` @ `0x20806ba`) is one-time MLIR plumbing to recast the ranked operand as a `ShapedType` for the second `getShape`; it does not affect semantics.

**Net:** `mhlo.get_dimension_size %t` (ranked, dim d static) ⇒ `mhlo.constant <i32 shape[d]>` + RAUW + erase. Dynamic dims and unranked operands are untouched. CERTAIN.

> **GOTCHA —** the result is `i32`, not `index` or `i64`. `get_dimension_size` returns a 0-D `tensor<i32>` in MHLO, and the fold preserves that element width by reading `op.result`'s type (`cast<ShapedType>` @ `0x2080501`) rather than synthesizing a fresh scalar type. A reimplementer who hard-codes `i64` will produce a type-mismatched constant that fails verification downstream.

---

## replaceConvertsWithConstants — const-fold dtype conversions

### Purpose

`mhlo.convert(mhlo.constant)` is a dtype cast of a literal — the tensorizer should never see a run-time convert fed by a compile-time constant. This rewriter value-materializes the cast: it re-reads the source literal's elements, narrows/widens to the destination element width, and emits a folded `mhlo.constant`.

### Algorithm

```c
// CanonicalizeForTensorizer::replaceConvertsWithConstants @0x20807c0 (3295 B, 168 BB) — CERTAIN
function replaceConvertsWithConstants(FuncOp f):
    for (cvt : collect<mhlo::ConvertOp>(f)):          // callback @0x2080879
        src = cvt.operand(0).getDefiningOp()          // 0x208092e
        if (src.getTypeID() != mhlo::ConstantOp::id): continue   // 0x2080951
        srcAttr = ConstantOp::getValue(src)           // 0x208096c @0x8f76d80, ElementsAttr
        elemTy  = ShapedType::getElementType(cvt.result.type)     // 0x2080a13 @0x9b21870
        if (isa<FloatType>(elemTy)):                  // probe @0x2080a1b
            vals = [ (double)e for e in srcAttr.getValues<float>() ]  // cvtss2sd, SmallVector cap 100
            newAttr = getRawIntOrFloat(resultTy, vals, /*elemW*/8, isInt=1, signed=1)  // 0x9afdd10
        else:  // IntegerType
            vals = srcAttr.getValues<int>()           // 0x207fda0
            newAttr = getRawIntOrFloat(resultTy, vals, 8, 1, 1)
        cst = OpBuilder::create(ConstantOp::build(state, resultType, newAttr))  // 0x2080c1e
        replaceAllUsesWith(cvt.results, cst)          // 0x2080c7e @0x9b650c0
        if (all_results_use_empty(cvt)): Operation::erase(cvt)   // 0x2080e63
```

Both element-type paths route through `DenseElementsAttr::getRawIntOrFloat` with `elemW=8`; the float path goes float → double → `getRawIntOrFloat`, which re-narrows to the destination element width. Splat and non-splat are both handled by per-element iteration. No size limit (unlike the broadcast folder).

**Net:** constant-folds `convert(constant)` so the tensorizer sees only a literal. CERTAIN.

---

## replaceBroadcastsWithBroadcastInDim — legacy broadcast → broadcast_in_dim

### Purpose

`mhlo.broadcast` is the legacy form that *prepends* `broadcast_sizes` leading dims, mapping the operand's existing dims to the trailing output dims. The tensorizer (and the downstream constant-folder #8) only accept the canonical `mhlo.broadcast_in_dim`, which carries an explicit `broadcast_dimensions` attribute. This rewriter computes that attribute and swaps the op kind, preserving the operand unchanged.

### Algorithm

```c
// CanonicalizeForTensorizer::replaceBroadcastsWithBroadcastInDim @0x2083f90 (7765 B, 326 BB) — CERTAIN
function replaceBroadcastsWithBroadcastInDim(FuncOp f):
    for (b : collect<mhlo::BroadcastOp>(f)):          // cmp @0x2084098
        src = b.operand(0).getDefiningOp()            // 0x2084131
        // CONSTANT-OPERAND CHECK IS ADVISORY (CORRECTION below): if src is not a
        // ConstantOp-with-DenseElementsAttr, emit NeuronLogger ERROR (level 4,
        // line 119, ErrorCode 3 via hilo::formatErrorMessage) and CONTINUE anyway.  // 0x2084196
        rty = dyn_cast_if_present<ShapedType>(b.result.type)   // 0x207d420
        if (!rty.hasRank()): emit_warning(line 123); // and continue
        P = len(BroadcastOp::getBroadcastSizes(b))    // 0x2084639 @0x8f73fc0 — prepended-prefix count
        R = operand_rank
        // validate trailing |R| output dims == operand dims (two DenseSet/SmallVector passes
        // @0x20846d8, 0x2084840; TensorType::getShape @0x9b23640)
        bcastDims = [P, P+1, …, P+R-1]                // memcpy-assembled identity range @0x2084afe
        attrTy = RankedTensorType::get([R], i64)      // 0x9b4ef00 / 0x9b2a740
        attr   = getRawIntOrFloat(attrTy, bcastDims, 8, 1, 1)   // 0x2084b85 — DenseIntElementsAttr
        bid = OpBuilder::create(BroadcastInDimOp::build(state, resultType, b.operand(), attr))  // 0x2084c8e
        replaceAllUsesWith(b.results, bid)            // 0x2084ce8
        Operation::erase(b)                           // 0x2084d25
```

`broadcast_dimensions = [P, P+1, …, P+R−1]` is exactly the trailing-identity mapping that re-expresses the prepend semantics of `mhlo.broadcast` in `broadcast_in_dim` form.

> **CORRECTION (D-C09) —** an earlier reading treated the "operand must be a constant `DenseElementsAttr`" check as a hard match gate. It is **advisory only**: a non-constant operand drives a `NeuronLogger` ERROR diagnostic (`setCurLogLevel(4)` @ `0x2084196`, source line 119, ErrorCode 3) but the broadcast→broadcast_in_dim conversion proceeds regardless (both branches fall through to `0x20843b9`). The conversion is purely structural; it never needs the operand to be a literal.

---

## replaceBroadcastInDimWithConstants — materialize broadcast of a literal

### Purpose

Once #7 has canonicalized broadcasts, this rewriter fully evaluates `broadcast_in_dim(constant)` into a dense literal, computing each output element from the source via `broadcast_dimensions` — but only when the result fits a configurable element budget, so the pass never explodes a tiny constant into a multi-gigabyte literal.

### Algorithm

```c
// CanonicalizeForTensorizer::replaceBroadcastInDimWithConstants @0x20815f0 (5788 B, 257 BB) — CERTAIN
function replaceBroadcastInDimWithConstants(FuncOp f):
    for (bid : collect<mhlo::BroadcastInDimOp>(f)):   // cmp @0x20816e9
        src = bid.operand(0).getDefiningOp()          // 0x208177b
        if (src.getTypeID() != mhlo::ConstantOp::id): continue   // 0x2081796
        srcAttr = ConstantOp::getValue(src)           // DenseElementsAttr::classof @0x20817bd
        rty = dyn_cast_if_present<ShapedType>(bid.result.type)   // 0x20817ee
        if (!rty || !rty.hasRank()): continue         // 0x2081811
        if (any_dynamic_dim(rty.getShape())): continue   // __find_if<isDynamicShape> @0x207c6c0
        bdims = BroadcastInDimOp::getBroadcastDimensions(bid)    // 0x208184c @0x8f73ca0

        // SIZE BUDGET GATE (CERTAIN):
        nOut = ShapedType::getNumElements(rty)        // 0x20819ae
        if (broadcast-num-elements-to-fold < nOut         // 0x20819bc, cl::opt global @0x9c719f8
            || secondary_flag != 0): continue         // 0x20819ca — bail, leave broadcast intact

        // INDEX-MAPPING MATERIALIZER:
        mappedSrcDim[*] = 1                            // var_480
        for (k : 0..len(bdims)): mappedSrcDim[bdims[k]] = outShape[k]   // 0x2081b80, scatter
        out = []
        for (outIdx : 0..nOut):                       // 0x2081cd0: idiv remainder by each mapped dim,
            srcIdx = map_linear(outIdx, mappedSrcDim, strides)   //   accumulate source linear index
            out.push(read_element(srcAttr, srcIdx))   // float→APFloat / int→APInt
        newAttr = DenseElementsAttr::get(rty, out)    // @0x9afccd0 (float) / @0x9afd250 (int)
        cst = OpBuilder::create(ConstantOp::build(state, newAttr))  // 0x2082059/0x2082068
        replaceAllUsesWith(bid.results, cst); Operation::erase(bid)  // 0x20820ed
        if (all_results_use_empty(src)): Operation::erase(src)        // 0x2082121
```

### Related Knobs

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `broadcast-num-elements-to-fold` | int (`cl::opt`) | **set by static init (not read here)** | Max output element count to materialize; a `broadcast_in_dim(const)` larger than this is left intact. Flag str @ `0x39e1a0`, help "Compute the number of elements to fold into the broadcast.", runtime global @ `0x9c719f8`. |

> **NOTE —** the default value of `broadcast-num-elements-to-fold` is stamped by a static initializer this body does not read; it is recovered only as "a configurable budget" (MED). A reimplementer must pick a sane cap (e.g. a few thousand elements) — the gate is `threshold < numOutputElems → bail`, plus a secondary flag word at `var_4C8` that, if nonzero, also bails.

**Net:** fully evaluates `broadcast_in_dim(literal)` into a dense literal under an element-count budget; both int and float element types.

---

## legalizeBackendConfig / replaceInvalidBackendConfigs — sanitize AwsNeuronErf

### Purpose

This is the one custom-call config sanitizer. It targets **only** the `AwsNeuronErf` custom-call: if its `backend_config` string carries any character the tensorizer's Erf handler cannot parse, the rewriter rebuilds the custom-call with `backend_config = "None"`, so the back-end sees a parseable (effectively empty) config.

The dispatcher slot #5 is a two-stage collect-then-execute: a distinct collector lambda `legalizeBackendConfig` (callback @ `0x2086ae1`) walks every `mhlo::CustomCallOp` into a worklist, and the post-walk loop (`0x2086b70`) calls the per-op worker `replaceInvalidBackendConfigs` (isra.0 @ `0x2085df0`).

### Algorithm

```c
// CanonicalizeForTensorizer::replaceInvalidBackendConfigs @0x2085df0 (isra.0, 3038 B) — CERTAIN
function replaceInvalidBackendConfigs(Operation* cc, FuncOp f):
    if (CustomCallOp::getCallTargetName(cc) != "AwsNeuronErf"): return   // 0x2085eaf, str @0x236819
    cfg = StringAttr::getValue(CustomCallOp::getBackendConfig(cc))       // @0x8f78ee0 / 0x9af0c80
    cnt = count(c in cfg where !isalnum(c) && c != '"')                  // 0x2085f90
    if (cnt == 0): return                            // already valid → 0x2086707
    NeuronLogger::warn(level 2, line 359)            // 0x2085fcd
    new = OpBuilder::create(CustomCallOp::build(state, resultTypes, operands, {  // 0x2086585
        call_target_name = StringAttr(original target),                 // 0x2086260
        backend_config   = StringAttr("None"),                          // 0x20862cb
        has_side_effect  = BoolAttr(false),                             // 0x2086300
        api_version      = I32IntegerAttr(getApiVersion(cc)),           // 0x2086341
        // custom_call_output_operand_aliasing appended only if non-empty // 0x208641f
    }))
    replaceAllUsesWith(cc.results, new); Operation::erase(cc)           // 0x208665c
```

"Invalid" means the config contains a byte that is neither alphanumeric nor a double-quote — i.e. any structured config with `{ } : , =`. The rewrite preserves target name, api_version, and output-operand aliasing; it only zeroes the config and forces `has_side_effect=false`.

> **CORRECTION (D-C09) —** this is `AwsNeuronErf`-specific, not a generic backend-config rewriter, and the collector lambda `legalizeBackendConfig` is distinct from the worker `replaceInvalidBackendConfigs`. An earlier listing named only the worker and implied it ran on all custom-calls.

---

## lowerResizeNearestGrad — average-pool lowering of the resize gradient

### Purpose

`mhlo.custom_call{target="ResizeNearestGrad"}` is the gradient of a nearest-neighbour upsample. Forward nearest-resize replicates each input pixel `scale^ndim` times; the gradient w.r.t. each input pixel is therefore the **mean** of the `scale^ndim` co-located output gradients. This rewriter lowers the custom-call to that average-pool: a non-overlapping `reduce_window` sum over each upsampling tile, divided by the window volume.

### Algorithm

```c
// CanonicalizeForTensorizer::lowerResizeNearestGrad lambda @0x207df40 (isra.0, 5451 B) — CERTAIN
function lowerResizeNearestGrad(CustomCallOp cc) -> WalkResult:
    name = CustomCallOp::getCallTargetName(cc)        // @0x8f78e40
    if (name.size() != 17 || name != "ResizeNearestGrad"): return advance   // imm cmp @0x207df80

    // LEGALITY — each failure → emitOpError + InFlightDiagnostic::report
    if (cc.numOperands != 1): return error("ResizeNearestGrad: only one operand allowed")   // 0x207e000
    if (cc.numResults  != 1): return error("ResizeNearestGrad: only one output allowed")    // 0x207e180
    if (rank(grad) != rank(input)): return error("ResizeNearestGrad: input and output have different rank")  // 0x207e372
    scale = []
    for (i : 0..ndim):                                // 0x207e511
        q, r = ldiv(outDim[i], inDim[i])
        if (r != 0): return error("ResizeNearestGrad: output dim {0:N} is not an integral "
                                  "multiple of input dim {0:N}, which is unsupported")        // 0x207efd7
        scale.push(q)                                 // per-dim upsample factor

    // REWRITE — reduce_window(sum) then divide by window volume
    init0 = create<ConstantOp>(i64 0)                 // 0x207e61a
    rw = create(ReduceWindowOp::build(grad, init0,    // 0x207e8f7 @0x8f98cb0
            window_dimensions = scale, window_strides = scale,   // stride == size → non-overlapping
            base_dilations = 1s, window_dilations = 1s, padding = 0s))
    // region body: 2 scalar block args → AddOp → ReturnOp(add)  (the reduction is SUM)
    //   Block::addArgument ×2 (0x207ea29/0x207ea50); AddOp @0x8fb6750; ReturnOp @0x207ee1d
    vol = Π scale[i]                                  // window volume
    volC = create<ConstantOp>(FloatAttr(vol))         // 0x207eb21
    volB = create(BroadcastOp::build(resultType, volC, dims))   // 0x207ec05 @0x8f98b90
    div = create(DivOp::build(rw, volB))              // 0x207ecc2 @0x8fb6960
    replaceAllUsesWith(cc.results, div); Operation::erase(cc)   // 0x207ee82
    return advance
```

The window stride equals the window size (non-overlapping tiles), padding is 0, dilations are 1 — exactly the geometry of a per-tile sum over each upsampling window. The divide-by-volume converts the sum to a mean.

> **CORRECTION (D-C09) —** `ResizeNearestGrad` is **not** a scatter/gather lowering (an early task hypothesis). It is an average-pool: `Div(ReduceWindow_sum(grad, window=scale), Broadcast(Π scale))`. The reduce-window region body is an `AddOp` (sum), confirmed at `0x207edad`.

> **NOTE —** the four `emitOpError` legality strings ("only one operand allowed", "only one output allowed", "input and output have different rank", "output dim … is not an integral multiple …") are verbatim in `.rodata` and confirm this rewriter is the only consumer of `ResizeNearestGrad` in the MHLO pipeline. A separate downstream diagnostic ("op ResizeNearestGrad operation should be lowered to supported operations before reaching this stage", routed through `hilo::lookup_cause(ErrorCode)` @ a different site) fires only if a `ResizeNearestGrad` survives *past* this pass — i.e. it is this rewriter's safety net, not part of its body.

---

## Pass identity and the StableHLO twin

### The naming seam

The class is `CanonicalizeForTensorizer`, the source file is `CanonicalizeForTensorizer.cc`, the factory is `createCanonicalizeForTensorizerPass`, and the dispatched workers normalize "for the Tensorizer" — but the pass's registered CLI argument, returned by `getArgument` @ `0x207c500`, is the string `"canonicalize-for-penguin"` (@ `0x21a240`). "Penguin" and "Tensorizer" are the same target under two names.

> **GOTCHA —** if you drive this pass from `--pass-pipeline` or `--passes`, you must use **`canonicalize-for-penguin`** (or `stablehlo-canonicalize-for-penguin` for the StableHLO twin). `canonicalize-for-tensorizer` is **not** a registered pass-arg in this binary — the only `canonicalize-for-*` strings present are the two `-penguin` forms. A reimplementer who registers the pass under the class name will not match the wheel's `--passes` table ([4.1](pass-registry.md)).

### The StableHLO twin

`StableHLOCanonicalizeForTensorizer::runOnOperation` @ `0x21266b0` (pass-arg `stablehlo-canonicalize-for-penguin`) carries the same rewriter set over `stablehlo::*` op TypeIDs — `fuseIotaSort`, `canonicalizeTupleOp`, `replaceGetDimensionSize`, `replaceConvertsWithConstants`, `legalizeBackendConfig`/`replaceInvalidBackendConfigs`, `lowerResizeNearestGrad`, and both broadcast rewriters — each confirmed as a distinct symbol. It has **one additional rewriter the MHLO side lacks**: `removeDeadZeroSizedTensorOps` @ `0x2121530` (callback @ `0x211e9b0`), which DCEs ops producing zero-element tensors. The StableHLO bodies were not separately disassembled here; structural identity to the MHLO twin is HIGH (matched symbol-for-symbol), but the per-body offsets above are the MHLO copy only.

---

## Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `runOnOperation` | `0x20869d0` | 629 B | Dispatch driver, 8 rewriters in order | CERTAIN |
| `fuseIotaSort` | `0x207ffb0` | 644 B | Strip iota index-operand of sort | CERTAIN |
| `canonicalizeTupleOp` | `0x2083180` | 1359 B | Fold GTE + DCE dead tuples | CERTAIN |
| `replaceGetDimensionSize` | `0x2080240` | 1393 B | Static dim → i32 constant (dynamic-shape glue) | CERTAIN |
| `replaceConvertsWithConstants` | `0x20807c0` | 3295 B | Const-fold `convert(const)` | CERTAIN |
| `replaceInvalidBackendConfigs` (isra.0) | `0x2085df0` | 3038 B | Sanitize `AwsNeuronErf` config → "None" | CERTAIN |
| `lowerResizeNearestGrad` (lambda, isra.0) | `0x207df40` | 5451 B | Resize-grad → reduce_window+div (avg-pool) | CERTAIN |
| `replaceBroadcastsWithBroadcastInDim` | `0x2083f90` | 7765 B | Legacy broadcast → broadcast_in_dim | CERTAIN |
| `replaceBroadcastInDimWithConstants` | `0x20815f0` | 5788 B | Materialize `broadcast_in_dim(const)` (budgeted) | CERTAIN |
| `StableHLO…::removeDeadZeroSizedTensorOps` | `0x2121530` | — | StableHLO-only 9th rewriter | HIGH |

---

## Adversarial self-verification

The five strongest claims on this page, re-challenged against the binary:

1. **Pass-arg is `canonicalize-for-penguin`, not `canonicalize-for-tensorizer`.** Re-checked: the strings table contains only `canonicalize-for-penguin` (@ `0x21a240`) and `stablehlo-canonicalize-for-penguin` (@ `0x348f10`); no `canonicalize-for-tensorizer` exists. The former's sole xref is from `CanonicalizeForTensorizer::getArgument` @ `0x207c500`. CONFIRMED — this **corrects** both backing reports, which named `canonicalize-for-tensorizer` as the pass-arg.

2. **Eight rewriters in the stated order, broadcasts last.** All eight `_ZN25CanonicalizeForTensorizer…` symbols and their addresses match `_function_addresses.json` exactly (`fuseIotaSort` `0x207ffb0` … `replaceBroadcastInDimWithConstants` `0x20815f0`). The intra-`runOnOperation` order (`0x2086a20`–`0x2086bda`) is from disasm. CONFIRMED for the symbol set; the relative call order is CERTAIN per the backing disasm trace (I did not re-walk the 629-byte driver instruction-by-instruction here — STRONG on order, CERTAIN on membership).

3. **`replaceGetDimensionSize` folds to `i32` via `op.result` type.** The `kDynamic` sentinel `0x8000000000000000`, the `RankedTensorType` TypeID gate (`0x9d3fa00`), and `getRawIntOrFloat(..., dataEltSize=4, isInt=true)` are all in the backing disasm with addresses. The `i32` claim rests on reading `op.result`'s type (`cast<ShapedType>` @ `0x2080501`) and the 4-byte element width. CONFIRMED.

4. **`lowerResizeNearestGrad` is average-pool, region body is `AddOp`.** The four `emitOpError` strings are verbatim in `.rodata` (`_strings.json`); `ReduceWindowOp::build` @ `0x8f98cb0`, `AddOp::build` @ `0x8fb6750`, `DivOp::build` @ `0x8fb6960` are the builder calls. The lambda addr `0x207df40` is confirmed in `_function_addresses.json`. CONFIRMED.

5. **`replaceInvalidBackendConfigs` is `AwsNeuronErf`-only.** The string `AwsNeuronErf` (@ `0x236819`) is referenced by exactly `replaceInvalidBackendConfigs` (both mhlo and StableHLO copies) plus two cost-analysis/checker functions, never by any other rewriter. The `"None"` replacement and `has_side_effect=false` are from the backing disasm. CONFIRMED.

No claim required downgrading to SPECULATIVE. The one genuine **CORRECTION** surfaced by re-verification is the pass-arg name (claim 1). The `broadcast-num-elements-to-fold` default value and the `+0x2E` bitfield semantics remain the honest gaps (MED / inferred).

---

## Related Components

| Name | Relationship |
|---|---|
| MLIR Dynamic-Shape Front-End | Produces the `get_dimension_size` ops that `replaceGetDimensionSize` folds |
| TensorizerLegalization | The next stage; consumes the canonical forms this pass produces |
| `StableHLOCanonicalizeForTensorizer` | StableHLO twin (`stablehlo-canonicalize-for-penguin`), +`removeDeadZeroSizedTensorOps` |
| hlo-opt Pass Registry | Where `canonicalize-for-penguin` is registered in the `--passes` table |

## Cross-References

- [MLIR Dynamic-Shape Front-End & the Bucketing Non-Mechanism](mlir-dynamic-shape-frontend.md) — emits the `get_dimension_size` ops this pass const-folds (4.31)
- [hlo2penguin MLIR Pipeline Order & Entry Flow](hlo2penguin-mlir-pipeline.md) — where this pass sits in the MHLO→Penguin sequence
- [The hlo-opt Pass Registry](pass-registry.md) — the `--passes` table that registers `canonicalize-for-penguin`
- [TensorizerLegalization](tensorizer-legalization.md) — the downstream legalizer that consumes the normalized forms (4.40)
- [Pool Engine — Windowed Pooling and the Reduce Leg](../arch/pool-engine.md) — the hardware reduce-window the `ResizeNearestGrad` lowering ultimately targets (6.8.5/6.8.6)
