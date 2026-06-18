# NeuronInstCombine (MLIR reduce-slice-reduce simplify)

> *All addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22`, binary `neuronxcc/starfish/bin/hlo2penguin` (cp310). The hlo2penguin ELF is **not** VA==fileoff: `.rodata` fileoff = VA − 0x200000, `.text` fileoff = VA − 0x201000. Other builds will differ.*

## Abstract

`neuron-inst-combine` is an MLIR module pass — `hilo::NeuronInstCombine` (`OperationPass<ModuleOp>`) — that runs in the hlo2penguin dialect pipeline on `mhlo` IR. It is the MLIR-stage twin of the HLO-level InstCombine peepholes ([4.21](instcombine-peephole.md)), but they operate on completely different IRs: 4.21 rewrites `xla::HloInstruction` graphs *before* the IR is imported into MLIR; this pass rewrites `mlir::mhlo` ops *after* import. They share a name and a spirit (algebraic peephole simplification) and nothing else — no code, no pattern table, no driver.

Despite being module-scoped, the pass does **not** walk the module. It fetches the single compile-entry function via `hilo::getMainFunction(ModuleOp&)` (@0x21c1e70), builds a greedy `RewritePatternSet` holding **exactly one** pattern — `hilo::SimplifyReduceSliceReducePattern` (PatternBenefit=1, rooted on `mhlo.reduce`) — and runs `applyPatternsGreedily` once per region of that function. The pass is otherwise empty: no other patterns, no analysis, no diagnostics.

That one pattern collapses the idiom **`reduce(slice(reduce(X)))`** — two `mhlo::ReduceOp`s separated by a single `mhlo::SliceOp` — into **one** reduce, by hoisting the slice *below* the merged reduce (rewriting it to run over the inner reduce's input `X`). The collapse is legal only when the two reduces are body- and init-equivalent, their reduction-dim sets are **disjoint** (so their union is well-defined), and every dimension the slice actually trims is either size-1 or one of the reduced dims (so the slice commutes past the reduce). Two `llvm::any_of` predicate lambdas enforce the last two conditions.

For reimplementation, the contract is:

- The pass driver: `getMainFunction` → one-pattern set → `GreedyRewriteConfig{maxIterations=10, maxNumRewrites=-1, topDown=true}` → `applyPatternsGreedily` per region; failure (not "changed") propagates to `signalPassFailure`.
- The match legality: SliceOp single-use, body/init equivalence of the two reduces, the two `any_of` gates.
- The rewrite: project the slice's start/limit/stride attrs back through the inner reduction, rebuild the slice over `X` (or elide it to a `mhlo.reshape` / to `X` itself), replace the outer reduce.

| | |
|---|---|
| **Pass arg** | `neuron-inst-combine` |
| **Pass class** | `hilo::NeuronInstCombine : PassWrapper<…, OperationPass<ModuleOp>>` |
| **Driver** | `runOnOperation` @0x20f94b0 (~801 B) |
| **Factory** | `mlir::createNeuronInstCombinePass()` @0x20f70d0 (~415 B) |
| **Sole pattern** | `hilo::SimplifyReduceSliceReducePattern`, benefit 1, root `mhlo.reduce` |
| **matchAndRewrite** | @0x20f7ef0 (~4793 B) |
| **Root op** | `mhlo.reduce` (string @0x25e533) |
| **Rewritten ops** | `mhlo.slice` (@0x2222ac), `mhlo.reshape` (@0x23e451) |
| **IR level** | MLIR `mhlo` dialect, inside hlo2penguin |
| **Vtables** | pattern `_ZTVN4hilo32SimplifyReduceSliceReducePatternE`@0x41fda8; pass `_ZTVN4hilo17NeuronInstCombineE`@0x41fde0 |

> **NOTE —** the HLO-level InstCombine (4.21) and this MLIR pass are *not* the same pass at two pipeline points. They are independent implementations on different IR. Do not assume a pattern present in one exists in the other; the MLIR pass carries a single pattern that the HLO pass does not.

---

## Pass Driver — `NeuronInstCombine::runOnOperation`

### Purpose

Apply the one reduce-slice-reduce pattern greedily over the main function. Module-scoped only to attach at module granularity; the actual rewriting is function-scoped.

### Entry Point

```text
mlir::createNeuronInstCombinePass()        0x20f70d0  ── new(0x150), vtable 0x41fdf0, op "builtin.module"
  └─ hilo::NeuronInstCombine::runOnOperation  0x20f94b0  ── driver
       ├─ hilo::getMainFunction(ModuleOp&)     0x21c1e70  ── the single entry FuncOp
       ├─ RewritePatternSet::addImpl<SimplifyReduceSliceReducePattern>  0x20f9330  ── SOLE pattern, benefit 1
       └─ applyPatternsGreedily(region, frozen, cfg)              ── per region
```

### Algorithm

```c
void NeuronInstCombine::runOnOperation():           // 0x20f94b0
    ModuleOp m   = getOperation();                  // [this+0x28] & ~7
    FuncOp   main = hilo::getMainFunction(m);        // call 0x20f94e8 -> r12
    MLIRContext *ctx = main.getContext();            // 0x20f94fc

    RewritePatternSet patterns(ctx);
    patterns.addImpl<SimplifyReduceSliceReducePattern>(/*labels=*/{}, ctx); // 0x20f95b5 — only pattern
    FrozenRewritePatternSet frozen(move(patterns));  // 0x20f95d2

    GreedyRewriteConfig cfg = {                       // built inline 0x20f9724..0x20f9759 (HIGH)
        .maxIterations  = 10,        // 0xA  (kDefaultMaxIterations)
        .maxNumRewrites = -1,        // kNoLimit (two -1 int fields)
        .useTopDown     = true,      // word 0x101 -> two adjacent bools = 1
    };

    bool failed = false;                              // r12d
    for (Region &r : main->getRegions()):             // loop 0x20f9720..0x20f9775
        bool ok = applyPatternsGreedily(r, frozen, cfg, &unused); // 0x20f975f
        failed |= (ok ^ 1);                           // 0x20f9764 — accumulate FAILURE, not "changed"
    if (failed):
        signalPassFailure();                          // 0x20f978c: [this+0x28] |= 4
```

> **GOTCHA —** the accumulator tracks *failure*, not *change*. `applyPatternsGreedily` returns `LogicalResult`; the driver computes `ok ^ 1` and ORs it, so the pass calls `signalPassFailure()` iff some region's greedy driver returned failure. A reimplementation that propagates "did the IR change" instead of "did a region fail" will mis-signal pass status.

### Pattern construction — `addImpl` @0x20f9330

```c
// operator new(0x60); pattern object size 0x60
Pattern::Pattern("mhlo.reduce" /*len 0xB*/, PatternBenefit(1), ctx, /*generated=*/{}); // 0x20f9382
vptr = off_41FDB8;   // vtable 0x41fda8 + 0x10, stamped 0x20f9393
```

The pattern is an `OpRewritePattern<mhlo::ReduceOp>` with benefit 1, rooted on the **outer** reduce mnemonic.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `NeuronInstCombine::runOnOperation` | 0x20f94b0 | ~801 B | Driver: getMainFunction → 1 pattern → greedy per region | CERTAIN |
| `mlir::createNeuronInstCombinePass` | 0x20f70d0 | ~415 B | Factory (module-level, vtable 0x41fdf0) | CERTAIN |
| `RewritePatternSet::addImpl<…>` | 0x20f9330 | ~371 B | Builds the sole pattern, benefit 1, root `mhlo.reduce` | CERTAIN |
| `hilo::getMainFunction(ModuleOp&)` | 0x21c1e70 | ~259 B | Returns the compile's entry FuncOp | HIGH |

> **NOTE —** function sizes are derived from the address gaps in `function_addresses.json` (the binary was extracted disasm-only, `NVOPEN_IDA_SKIP_DECOMPILE`); names and entry addresses are verbatim from the symbol table. matchAndRewrite spans 0x20f7ef0→0x20f91b0 ≈ 4800 B; runOnOperation 0x20f94b0→0x20f97e0 ≈ 816 B.

---

## The Pattern — `SimplifyReduceSliceReducePattern::matchAndRewrite`

### Purpose

Collapse `outerReduce(slice(innerReduce(X)))` into a single reduce. Root op = the **outer** (second) reduce; the inner reduce is found by walking up through the slice.

### Algorithm — Match phase

```c
LogicalResult matchAndRewrite(mhlo::ReduceOp op, PatternRewriter &rw):   // 0x20f7ef0
    // M1: input must be a SliceOp
    Operation *slice = op.getInputs()[0].getDefiningOp();   // [op+0x48][+0x18], 0x20f7f37
    if (!isa<mhlo::SliceOp>(slice)) return failure();       // TypeID cmp 0x20f7f52

    // M2: the slice result has exactly ONE use (this outer reduce)
    if (count(slice.getResult().getUses()) != 1) return failure();  // iter 0x20f7f89..0x20f8022

    // M3: outer reduce dims as a DenseElementsAttr (i64)
    auto outerDims = op.getDimensions();                    // 0x20f8075, isValidIntOrFloat(8,1,1)

    // M4: shape-diff scan — which dims does the slice actually trim?
    SmallVector<long,6> sliceDims;                          // @var_270
    for (i in reduceInput.shape):                           // loop 0x20f8170..0x20f81da
        if (reduceInput.shape[i] != slice.shape[i]) sliceDims.push(i);

    // M5: find the INNER reduce above the slice
    mhlo::ReduceOp inner = null;
    for (cand : producers(slice.operand)):                  // 0x20f81ee..0x20f8207
        if (!isa<mhlo::ReduceOp>(cand) || cand == op) continue;
        if (!OperationEquivalence::isRegionEquivalentTo(op.body, cand.body, /*Flags=*/1)) continue; // 0x20f82ba
        if (op.init_values.size() != cand.init_values.size()) continue;                              // 0x20f82c7
        if (!all_pairwise(op.inits, cand.inits, isEquivalentTo)) continue;                           // 0x20f83b5
        // M6: inner reduce dims
        auto innerDims = cand.getDimensions();              // 0x20f8420
        // M7: disjointness gate (lambda#1)
        if (any_of(outerDims_vec, [&](long d){ return is_contained(innerDims, d); })) // 0x20f84d2
            continue;                                       // shares a reduced axis -> reject this cand
        // M8: slice-hoistability gate (lambda#2)
        if (any_of(slice.start_indices, [&](long e){ ... }))  // 0x20f8528
            continue;                                       // slice trims a kept, non-degenerate dim -> reject
        inner = cand; break;
    if (!inner) return failure();                           // 0x20f8550
    // -> rewrite
```

### Algorithm — Rewrite phase

```c
    // R1: reposition inner reduce relative to X's producer
    inner->moveAfter(producer(X));      // 0x20f8580 (or moveBefore 0x20f8f83)

    // R2: project slice index attrs through the inner reduction onto X
    //     drop the inner-reduced dims; surviving dims keep start/limit/stride
    start' = project(slice.getStartIndices(), innerDims);   // 0x20f85c2, loop 0x20f86c0..
    limit' = project(slice.getLimitIndices(), innerDims);   // 0x20f85f8
    stride'= project(slice.getStrides(),      innerDims);   // 0x20f862e

    // R3: materialize rebuilt DenseIntElementsAttrs (i64) for start'/limit'/stride'
    //     RankedTensorType::get({N}, i64); DenseElementsAttr::getRawIntOrFloat(...)  0x20f888c..0x20f89aa

    // R4: build new slice over X
    newSlice = mhlo::SliceOp::build(rw, X, start', limit', stride');  // 0x20f8a3e/0x20f8a4d

    // R5: no-op elision
    if (newSlice.in.shape == newSlice.out.shape):           // memcmp 0x20f8ae7
        value = X;                                          // identity slice — drop it
    elif (only a reshape is needed):                        // 0x20f8cd6
        value = mhlo::ReshapeOp::build(rw, resultType, X);  // 0x20f8d4a
    else:
        value = newSlice;

    // R6: replace the OUTER reduce; the inner reduce (dims now the union) survives
    rw.replaceOp(op, { value });                            // vtable slot 0, 0x20f8b11

    // R7: erase the now-dead original slice if it had extra uses
    if (origSlice still referenced): rw.eraseOp(origSlice); // vtable +0x10, 0x20f8df0
    return success();
```

> **QUIRK —** the merged reduce is the **inner** reduce re-pointed, and the merge of the two dim sets is realized *implicitly* by R2's geometry — the rebuilt slice runs over `X` with the inner-reduced dims projected out, so the surviving reduce's footprint already covers the union. R6 replaces the *outer* reduce with the rebuilt-slice value; only one `mhlo.slice` and one `mhlo.reduce` remain. Whether a fresh `ReduceOp` is constructed with union dims or the inner one's `dimensions` attr is mutated in place is not separately emitted (MED).

> **NOTE —** the `report_fatal_error("Building op \`mhlo.slice\` but it isn't known in this MLIRContext…")` guard (@0x20f9010) and its `mhlo.reshape` twin (@0x20f90a8) are the stock `OpBuilder::create` dialect-registration check, not pattern logic. They fire only if the mhlo dialect is unloaded.

---

## Legality Predicates — the two `any_of` lambdas

Both lambdas use the shared membership helper `llvm::is_contained<ElementsAttrRange<DenseElementsAttr::ElementIterator<long>>&, long>` @0x20f7270 — a linear scan of a long value over an i64 `DenseElementsAttr` range.

### lambda#1 @0x20f7360 — disjoint reduction dims (CERTAIN)

Symbol: `any_of<SmallVector<long,6>&, …matchAndRewrite…UllE_>`. For each outer-reduce dim `d`, returns true if `d` is also an inner-reduce dim. `any_of` true ⇒ **reject** (continue scanning M5).

```c
bool lambda1(long d) { return is_contained(innerReduceDims, d); }  // 4-way unrolled @0x20f73a8
```

The two reduces must reduce **disjoint** axes. Reducing the same axis on both sides of a slice is not a single equivalent reduce; their union is only well-defined when the sets do not overlap. The overlapping case is left for other folds.

### lambda#2 @0x20f7520 — slice hoistability (HIGH)

Symbol: `any_of<ElementsAttrRange<DenseElementsAttr::ElementIterator<long>>&, …UllE0_>`. Iterates the slice's `start_indices`; captures `{reducedDims range, sliceDims vector, reduceInput RankedTensorType*}`.

```c
bool lambda2(long e):                                  // 4-way unrolled @0x20f7580
    bool reduced = is_contained(reducedDims, e);       // 0x20f759e
    long idx     = index_of(sliceDims, e);             // unrolled scan 0x20f75d8..0x20f7606
    bool sizeOne = (idx valid) && (reduceInput.shape[idx] == 1);  // 0x20f7844/0x20f785b
    // element is OK iff (sizeOne || reduced); any non-OK element -> predicate TRUE -> reject
    return !(sizeOne || reduced);                      // fall-through 0x20f786f
```

The slice may be hoisted below the merged reduce **only if every dimension it trims is either (a) a reduced dim or (b) of extent 1**. A slice that trims a *kept, non-degenerate* axis cannot commute with the reduce — it would change the reduce's input footprint on a surviving axis — so the fold is rejected.

> **GOTCHA —** the precise role of the iterated element `e` (slice-start coordinate vs dim-id) is inferred from the `shape[idx]` indexing pattern, not byte-proven against a live input (HIGH). The `{size-1 OR reduced}` legality conclusion is directly readable from the two branch tests; the element semantics are the inferred part.

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the binary:

1. **Single pattern, benefit 1, root `mhlo.reduce`.** `addImpl` symbol @0x20f9330 confirmed; the only pattern symbol in the binary is `SimplifyReduceSliceReducePattern`; `mhlo.reduce` confirmed byte-exact at fileoff 0x5e533 (`6d 68 6c 6f 2e 72 65 64 75 63 65`). CONFIRMED.
2. **Two `any_of` lambdas distinguished by operand type.** Both mangled symbols recovered verbatim: `…UllE_` over `SmallVector<long,6>&` (@0x20f7360), `…UllE0_` over `ElementsAttrRange<…ElementIterator<long>>` (@0x20f7520). The `_` vs `0_` suffix proves distinct closures from the same enclosing function. CONFIRMED.
3. **Module-level pass over getMainFunction.** `_ZN4hilo15getMainFunctionERN4mlir8ModuleOpE` @0x21c1e70 confirmed; pass typeinfo is `PassWrapper<…OperationPass<ModuleOp>>` (`_ZTSN4mlir11PassWrapperIN4hilo17NeuronInstCombineENS_13OperationPassINS_8ModuleOpEEEEE`). CONFIRMED.
4. **Rewritten ops `mhlo.slice` / `mhlo.reshape`.** Both strings confirmed byte-exact (0x2222ac, 0x23e451); `is_contained` and `matchAndRewrite` symbols confirmed. CONFIRMED.
5. **Outer reduce is the replaced op; slice hoisted below the merged reduce.** R6 replaces `op` (the root = outer reduce) via vtable slot 0; the rebuilt slice runs over `X`. The exact union-dim mechanism (R2 geometry vs explicit attr mutation) is tagged MED. STRONG for the replace-outer/hoist-slice shape; MED for the union mechanism.

Tagged inferred: GreedyRewriteConfig field *names* (bit-pattern `{10, -1, topDown}` is HIGH; the leading discriminator semantics MED); lambda#2 element semantics (HIGH); union-dim realization (MED).

---

## Related Components

| Name | Relationship |
|---|---|
| HLO-level InstCombine ([4.21](instcombine-peephole.md)) | The pre-MLIR twin: same name/spirit, different IR (`HloInstruction` vs `mhlo`), disjoint code |
| hlo2penguin MLIR pipeline ([4.32](hlo2penguin-mlir-pipeline.md)) | Where `neuron-inst-combine` is scheduled among the mhlo passes |
| Concatenation Optimizations ([concat-optimizations.md](concat-optimizations.md)) | Sibling mhlo-level structural simplifier |
| DUS/DS Simplifier ([dus-ds-simplifier.md](dus-ds-simplifier.md)) | Sibling slice/dynamic-slice mover in the same stage |

## Cross-References

- [InstCombine Peephole Passes](instcombine-peephole.md) — 4.21, the HLO-level InstCombine this pass twins (different IR)
- [hlo2penguin MLIR Pipeline](hlo2penguin-mlir-pipeline.md) — 4.32, pass ordering for `neuron-inst-combine`
- [The hlo-opt Pass Registry](pass-registry.md) — `--passes` table the pass arg is registered in
