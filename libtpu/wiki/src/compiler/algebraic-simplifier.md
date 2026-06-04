# Algebraic Simplifier

> *All symbol names, VAs, and the build-id below apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). `.text` VMA == file offset. Other versions differ; treat every VA as version-pinned.*

## Abstract

The **algebraic simplifier** is the TPU compiler's peephole rewriter — the HLO pass that walks every computation and replaces locally-recognizable instruction patterns with cheaper equivalents: identity elimination (`add x 0`, `multiply x 1`, `x AND true`), reciprocal-fold (`a/b → a * (1/b)` when `b` is constant), `min/max → clamp` strength reduction, broadcast/reshape/transpose canonicalization and folding, dot/convolution operand-transpose absorption, dead tuple-leg pruning through `GetTupleElement`, and a long tail of arithmetic canonicalizations. On the TPU backend it is **pass #60** of the HLO pre-pass pipeline (see [hlo-pre-passes.md](hlo-pre-passes.md)), instantiated not as the open-source `xla::AlgebraicSimplifier` but as `xla::jellyfish::TpuAlgebraicSimplifier` — a subclass whose visitor (`TpuAlgebraicSimplifierVisitor`) emits **17 `Handle*` methods** (16 overriding base handlers with TPU-layout-aware variants, plus one TPU-only `HandleRngBitGenerator`) on top of the **56 handlers** the base `AlgebraicSimplifierVisitor` provides.

The reader who knows LLVM should think of this as `InstCombine` for the HLO graph, with two structural differences. First, dispatch is **not benefit-ordered pattern matching** (the MLIR `PatternApplicator` machinery documented in the [rewrite-dispatch pages](#cross-references) is a *separate* engine on a *different* IR). It is plain C++ **virtual dispatch keyed on opcode**: the base class `xla::DfsHloVisitorBase<HloInstruction*>` is a ~140-slot vtable with one `Handle<Op>` slot per `HloOpcode`, a rewrite pass overrides only the slots for opcodes it cares about, and `HloInstruction::Visit` issues exactly one indirect call per instruction. The "registry" of rules *is* the C++ class hierarchy. Second, the simplifier is wrapped in a **run-to-fixpoint loop**: each computation is re-visited until a full pass makes no change, capped at 50 iterations with a circular-loop diagnostic.

This page documents three things, each byte-anchored: (1) the **visitor dispatch** — the opcode-vtable mechanism, the two override sets (base XLA vs the TPU subclass), and the per-computation `Run` entry; (2) a **representative rewrite-rule catalog** — the handlers that carry the bulk of the simplifications, with the helper functions each calls; and (3) the **run-to-fixpoint driver** `TpuAlgebraicSimplifier::RunImpl` and the **`AlgebraicSimplifierOptions`** gates (the TPU-specific knobs that enable/disable rule families). It does **not** re-derive every handler body (the rule arithmetic of all 56 base handlers is upstream XLA), the MLIR-side `PatternApplicator`, or the fusion priority queue — those have their own pages.

| | |
|---|---|
| **TPU pass class** | `xla::jellyfish::TpuAlgebraicSimplifier` (subclass of `xla::AlgebraicSimplifier`) |
| **TPU run-to-fixpoint driver** | `TpuAlgebraicSimplifier::RunImpl(HloModule*, execution_threads)` @ `0x13443f40` |
| **Base run driver** | `xla::AlgebraicSimplifier::RunImpl(HloModule*, …)` @ `0x1dd488c0` |
| **Per-computation entry** | `AlgebraicSimplifierVisitor::Run(HloComputation*, AlgebraicSimplifierOptions const&, AlgebraicSimplifier*)` @ `0x1dd010e0` → `bool` (changed) |
| **TPU visitor** | `xla::jellyfish::TpuAlgebraicSimplifierVisitor` (vtable `0x2190bcc8`); 17 `Handle*` (16 overrides + 1 TPU-only) |
| **Base visitor** | `xla::AlgebraicSimplifierVisitor` (vtable `0x21d1d1e8`, 147 slots); 56 `Handle*` overrides |
| **Dispatch base** | `xla::DfsHloVisitorBase<HloInstruction*>` (vtable `0x21d2c320`, 140 virtual slots = one `Handle<Op>` per opcode) |
| **Options struct** | `xla::AlgebraicSimplifierOptions` (dtor `0x1343ba00`); stored inline in the pass at `+8` |
| **Pass-add (TPU)** | `HloPassPipeline::AddPass<TpuAlgebraicSimplifier, Target const&, AlgebraicSimplifierOptions&>` @ `0x10954400` |
| **Pass-add (base)** | `HloPassPipeline::AddPass<AlgebraicSimplifier, AlgebraicSimplifierOptions&>` @ `0x14bcb600` |
| **TPU source unit** | `platforms/xla/service/jellyfish/tpu_algebraic_simplifier.cc` (string-anchored, lines 961/985/991) |
| **Base source unit** | `…/xla/hlo/transforms/simplifiers/algebraic_simplifier.cc` (string-anchored) |
| **Fixpoint cap** | 50 iterations per computation (`0x32`), then a non-fatal "circular simplification loop" `LOG` |
| **Match EDSL** | `xla::match::` combinators (1,117 `Match<>` instantiations binary-wide) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## Where It Sits

The simplifier runs inside Phase 1 of the compile pipeline, as pass **#60** of the ordered HLO pre-pass set ([hlo-pre-passes.md](hlo-pre-passes.md)), at the tail of `HloOptimizeThroughLayoutAssignment` (just after the `HloPassFix<TpuReduceWindowRewriter>` fixed-point and before `GatherOptimizer`). It runs **post-layout-assignment**, which is the whole reason the TPU subclass exists: the TPU overrides are *layout-aware* — they consult `IsValidLayout` / `UpdateLayout` so a rewrite never produces a tensor whose tiled layout the `tpu`→LLO legalizer cannot realize. The open-source simplifier, by contrast, treats layout as opaque.

```text
… LayoutAssignment (commits tiled layouts)
  → HloPassFix<TpuReduceWindowRewriter>
  → TpuAlgebraicSimplifier (#60)   ← THIS PAGE   RunImpl @ 0x13443f40
      └─ for each HloComputation (skip if >1 caller):
           AlgebraicSimplifierVisitor::Run(comp, options, this)   [vtable-dispatched]
           re-run to fixpoint (cap 50) if options.run_to_fixed_point()
  → GatherOptimizer → AllReduceSimplifier → …
```

The same `RunImpl` is the body the pass executes wherever it is added; the [hlo-pass-registry.md](hlo-pass-registry.md) catalogs the pass under its `HloPassInterface` RTTI entry, and [optimization-barrier.md](optimization-barrier.md) documents one specific handler of this visitor (the dead-tuple-leg pruning through `OptimizationBarrier`) as the *only* rewrite that operates around an opt-barrier.

---

## Dispatch: An Opcode-Keyed Virtual Table, Not a Pattern Pool

The crux for a reimplementer: there is no candidate list, no benefit score, and no per-op pattern bucket. Rule **selection** is the C++ vtable.

### The base vtable

`xla::DfsHloVisitorBase<HloInstruction*>` (vtable `0x21d2c320`) is **140 virtual slots wide** (`0x21d2c790 − 0x21d2c320 = 0x470` raw entries, minus offset-to-top + RTTI). Each slot is one `Handle<Opcode>` method. Slot-walking (resolving the `R_X86_64_RELATIVE` reloc addends) labels them one-per-opcode:

```text
slot 0/1  ~DfsHloVisitorBase (D2/D0)
slot 2    HandleElementwiseUnary      slot 3    HandleElementwiseBinary
slot 4/5  __cxa_pure_virtual          (HandleClamp / HandleSelect — pure in base)
slot 6    HandleMaximum               slot 7    HandleMinimum
slot 9    HandleConvert               slot 10   HandleBitcastConvert
slot 11   HandleStochasticConvert     slot 12   HandleCopy
slot 13   HandleComplex               slot 14   HandleMultiply
slot 18   HandlePower  slot 19 HandleSqrt  slot 20 HandleRsqrt  slot 21 HandleCbrt
…                                       (140 total; __cxa_pure_virtual where the base
                                         forces the subclass to implement — e.g.
                                         HandleParameter / HandleConstant / HandleReduce)
```

A concrete `HloInstruction`'s opcode byte (this build: `*(BYTE*)(inst + 12)`, the same `+12` offset documented on [optimization-barrier.md](optimization-barrier.md)) indexes the slot. `HloInstruction::Visit(visitor)` reads the opcode and issues an indirect call through that slot. **One indirect call per instruction.** Passes that subclass `DfsHloVisitorWithDefault` get a no-op `DefaultAction` for every opcode they do not override, so an unhandled opcode is simply left untouched.

### Why this is *not* the MLIR `PatternApplicator`

`libtpu` contains two structurally distinct rewrite-dispatch engines on two different IRs, and a reimplementer must not conflate them. The MLIR side (`stablehlo`/`tpu`/`llo` dialects) uses `mlir::PatternApplicator` — a pool of `RewritePattern` objects each carrying a 16-bit `PatternBenefit`, bucketed per-`OperationName` into a `DenseMap`, stable-sorted descending by benefit, dispatched first-match-wins. The HLO algebraic simplifier uses **none** of that:

| Axis | HLO algebraic simplifier (this page) | MLIR `PatternApplicator` |
|---|---|---|
| IR | `xla::HloModule` / `HloInstruction` | MLIR `Operation` (tpu/llo/stablehlo dialects) |
| Rule registry | the C++ class hierarchy (overridden vtable slots) | `FrozenRewritePatternSet` pool of `RewritePattern*` |
| Selection | virtual dispatch on opcode (one slot per opcode) | benefit-sorted per-op-kind bucket, first-match-wins |
| Ordering | none — exactly one handler per opcode | 16-bit `PatternBenefit` at `Pattern+0x8`, stable-sorted DESC |
| Match logic | `xla::match::` EDSL evaluated inline | `matchAndRewrite` per candidate, with `canApply` gate |
| Run loop | re-visit computation to fixpoint (cap 50) | greedy worklist *or* depth-aware conversion legalizer |

The practical consequence: there is no way to "register an extra rule" for the algebraic simplifier without editing the visitor class. The set of simplifications a pass performs is fixed at compile time by which `Handle<Op>` slots it overrides — which is exactly why the TPU specialization is a *subclass* (`TpuAlgebraicSimplifierVisitor`) rather than an added pattern.

### The two override sets

A rewrite pass's *surface* is exactly the set of vtable slots it overrides. Two override sets matter here, both enumerated directly from the decompile (each `Handle<Op>` is its own emitted function).

**Base — `xla::AlgebraicSimplifierVisitor` (vtable `0x21d1d1e8`, 147 slots): 56 overrides.**

```text
Abs Add AllGather AllReduce AllReduceOrReduceScatter AllToAll And Bitcast
BitcastConvert Broadcast Clamp Compare Complex Concatenate Conditional Constant
Convert Convolution Copy CustomCall Divide Dot DynamicSlice DynamicUpdateSlice
Exp Gather GetTupleElement Imag Iota Log Map Maximum Minimum Multiply Negate Not
OptimizationBarrier Or Pad Power Real Reduce ReducePrecision ReduceScatter
ReduceWindow Remainder Reshape Reverse Rsqrt Scatter Select Slice Sort Sqrt
Subtract Transpose
```

**TPU — `xla::jellyfish::TpuAlgebraicSimplifierVisitor` (vtable `0x2190bcc8`): 17 `Handle*`** — 16 overriding base handlers + the TPU-only `HandleRngBitGenerator` (each TPU handler takes a `Target const*` so it can query the per-generation layout model):

```text
Add Bitcast Broadcast Compare Concatenate Convert Convolution Copy Dot
DynamicSlice DynamicUpdateSlice Pad Reshape RngBitGenerator Slice Sort Transpose
```

> **NOTE — decompile cross-check on the override counts.** The base set is **56** distinct `AlgebraicSimplifierVisitor::Handle*` symbols (top-level `Handle<Op>(HloInstruction*)` methods) and the TPU set is **17** distinct `TpuAlgebraicSimplifierVisitor::Handle*` symbols, enumerated by listing the emitted handler functions (`nm -C | rg`). Of the 17 TPU handlers, 16 override a base handler of the same opcode and one — `HandleRngBitGenerator` — is **TPU-only**: the base `AlgebraicSimplifierVisitor` emits no `HandleRngBitGenerator` symbol, the TPU subclass *adds* it (`0x13443440`). The RTTI vtable widths corroborate the *slot* count (independent of the override count): base vtable `0x21d1d1e8` spans to its typeinfo at `0x21d1d690` = `0x4a8/8 = 149` entries (≈147 virtual slots after offset-to-top + RTTI); TPU subclass vtable at `0x2190bcc8` is the same width. [Confidence: CONFIRMED on both handler sets and the TPU-only `RngBitGenerator` fact.]

Every TPU override is a refinement: the dispatch reaches the *most-derived* slot, so for the 16 TPU-overridden opcodes the TPU body runs (it may delegate to or replace the base logic), for `RngBitGenerator` the TPU-only handler runs, and for the other 40 base handlers the base body runs unchanged. The match logic *inside* each handler is written with the `xla::match::` combinator EDSL (`Op()`, shape and operand matchers — the HLO analogue of LLVM's `m_*`), evaluated inline, not benefit-ranked.

---

## The Per-Computation Entry: `AlgebraicSimplifierVisitor::Run`

`AlgebraicSimplifierVisitor::Run(comp, options, simplifier)` (`0x1dd010e0`) is the single-computation worker invoked by both the base and TPU drivers. Its signature, recovered exactly:

```cpp
// 0x1dd010e0
bool AlgebraicSimplifierVisitor::Run(
    HloComputation* computation,
    const AlgebraicSimplifierOptions& options,
    AlgebraicSimplifier* simplifier);   // returns: did anything change?
```

It resets the visitor's `changed` flag, walks the computation's instructions in post-order, and for each instruction dispatches through the opcode vtable (above). Each `Handle<Op>` that performs a rewrite calls back into the simplifier's replace helpers (`ReplaceWith…`, `ReplaceInstruction`) which set the `changed` flag and update the worklist. `Run` returns that flag. A `true` return is what the driver's fixpoint loop watches.

---

## The Run-to-Fixpoint Driver: `TpuAlgebraicSimplifier::RunImpl`

`TpuAlgebraicSimplifier::RunImpl` (`0x13443f40`) is the pass body. Recovered structure (the VLOG dump-before/after and the iteration cap are byte-exact):

```cpp
// 0x13443f40  — name() anchored to tpu_algebraic_simplifier.cc
absl::StatusOr<bool> TpuAlgebraicSimplifier::RunImpl(
    HloModule* m,
    const absl::flat_hash_set<absl::string_view>& execution_threads) {
  // VLOG(2): "TpuAlgebraicSimplifier::RunImpl(), before:\n" + m->ToString()
  //          tpu_algebraic_simplifier.cc:961
  bool changed = false;
  for (HloComputation* comp : m->computations(execution_threads)) {
    // GATE: only simplify computations with <= 1 caller_instructions().
    //   v19 = comp->caller_instructions(...).size();  if (v19 > 1) skip.
    if (comp->caller_instructions(/*317*/).size() > 1) continue;

    if (AlgebraicSimplifierVisitor::Run(&visitor, comp, options_, this)) {
      changed = true;
      // RUN TO FIXPOINT — only if options_.run_to_fixed_point() (the bool at
      //   this + 135) is set; otherwise one pass per computation.
      if (run_to_fixed_point_ /* *(BYTE*)(this+135) == 1 */) {
        long runs = 1;
        while (runs - 1 < 0x32 /* 50 */) {            // hard cap
          ++runs;
          if (!AlgebraicSimplifierVisitor::Run(&visitor, comp, options_, this))
            break;                                    // converged
        }
        if (runs - 1 >= 0x32) {
          // tpu_algebraic_simplifier.cc:985 — NON-FATAL LOG, not a CHECK
          LOG(WARNING) << "Algebraic simplifier is likely stuck in a circular "
                          "simplification loop and ran for " << runs
                       << " runs on computation " << comp->name();
        }
      }
    }
  }
  // VLOG(2): after-dump at tpu_algebraic_simplifier.cc:991
  return changed;
}
```

Three facts a reimplementer must preserve, all byte-confirmed:

- **Single-caller gate.** A computation is simplified only when it has **≤ 1** `caller_instructions()`. Computations called from multiple sites are skipped here (they are simplified in their inlined/flattened form by other passes); rewriting a multiply-called computation in place would be unsound under the simplifier's local assumptions.
- **Fixpoint is option-gated and capped at 50.** The re-run loop fires only when the `run_to_fixed_point` bool (the byte at `this + 135`) is `1`. Each re-run is a full `Run` over the same computation; convergence is "`Run` returned `false`." If the loop hits **50** iterations without converging it emits a **non-fatal** `LOG` (`tpu_algebraic_simplifier.cc:985`) naming the computation and the run count, then moves on — it does **not** `CHECK`-fail or abort. (Contrast the pipeline-level `HloPassFix` convergence behaviour on [hlo-pre-passes.md](hlo-pre-passes.md), which is a separate mechanism.)
- **The options live inside the pass.** `RunImpl` reads `options_` at `this + 8` and the `run_to_fixed_point` flag at `this + 135`; the `AddPass` trampoline (`0x10954400`) copies the `AlgebraicSimplifierOptions` argument inline into the 184-byte (`0xB8`) pass object starting at `+8`. There is no shared/global options singleton — each pass instance carries its own gate configuration.

The base `xla::AlgebraicSimplifier::RunImpl` (`0x1dd488c0`) is the same shape without the TPU `Target`; the TPU subclass differs only by routing dispatch to the TPU visitor and by the layout-aware overrides.

---

## Representative Rewrite Rules

The rule **bodies** are largely upstream XLA; this section catalogs the handlers that carry the bulk of the work and the helper functions each calls, so a reimplementer knows the rule *surface* and where the TPU specializations diverge. Every address below is an emitted function in this build.

### Base-XLA handlers (40 run unchanged on TPU)

| Handler | Addr | Representative simplifications (recovered surface) |
|---|---|---|
| `HandleAdd` | (base) | `x + 0 → x`; `x + (-y) → x - y`; constant fold; reassociation of constants |
| `HandleMultiply` | `0x1dd...` | `x * 1 → x`; `x * 0 → broadcast(0)`; `x * 2^k` strength patterns |
| `HandleDivide` | `0x1dd106a0` | `a / b → a * (1/b)` when `b` constant; `x / x → 1`; `0 / x → 0`; per-dtype switches (`IntegralTypeSwitch` `0x1dd61e00`, `FloatingPointTypeSwitch` `0x1dd63d20`) |
| `HandleMaximum`/`HandleMinimum` | `0x1dd22a40` / `0x1dd23960` | feed `MinMaxToClamp` (`0x1dd23380`): `max(min(x,hi),lo) → clamp(lo,x,hi)` |
| `HandleClamp` / `HandleSelect` | `0x1dd23f00` / `0x1dd421e0` | redundant-clamp elimination; `select(true,a,b) → a` |
| `HandleBroadcast` | `0x1dd27a80` | broadcast-of-broadcast collapse; broadcast-of-scalar canonicalization; sink broadcasts past elementwise |
| `HandleReshape` | `0x1dd2eb40` | reshape-of-reshape collapse; `reshape → bitcast` when `options.ReshapeIsBitcast(from,to)` (`0x1dd0e0c0`); reshape-or-copy bitcast chain (`BitcastingOperandOfReshapeOrCopyChain` `0x1dd0df60`) |
| `HandleTranspose` | (base) | identity-transpose removal (`RemoveTranspose` `0x...`); transpose-of-transpose compose (`SimplifyTranspose` `0x...`); push transpose toward leaves (`TryToReorder…` / `TryToSink…`) |
| `HandleDot` | `0x1dd1d3a0` | dot canonicalization; `OptimizeDot` family (`0x1dd...`); transpose/convert absorption; zero/empty contracting-dim folds; dot→reduce/multiply strength reduction |
| `HandleConvolution` | `0x1dd48280` | `FoldConv…`/`SwapConv…` operand folding; trivial-window simplification |
| `HandleReduce` | `0x1dd3b540` | `MergeReduces` (`0x1dd3adc0`): fuse adjacent reduces over compatible dims; reduce-of-broadcast / reduce-of-reshape folds |
| `HandleConcatenate` | (base) | single-operand concat elimination; adjacent-constant concat fold; concat-of-slices reassembly |
| `HandlePad` | (base) | zero-pad elimination; pad-of-pad compose; negative-padding handling (option-gated) |
| `HandleGetTupleElement` | (base) | `GTE(Tuple(...), i) → operand_i` (tuple/GTE collapse) |
| `HandleOptimizationBarrier` | `0x1dd27360` | **the one rewrite-around-fence handler** — see below |

### The dead-tuple-leg pruning handler (cross-referenced)

`AlgebraicSimplifierVisitor::HandleOptimizationBarrier` (`0x1dd27360`) is the only handler that rewrites *around* an opaque op without crossing it. When the barrier wraps a tuple, it walks the barrier's users while they are `GetTupleElement` (opcode 64), marks each index live (GTE has >1 user, indexes the entry root, or is side-effecting), drops dead legs, rebuilds a smaller `kTuple` operand, and re-indexes the surviving GTEs — a defensive `CHECK(use->opcode() == kGetTupleElement)` at `algebraic_simplifier.cc:5302` guards the walk. This is documented end-to-end on [optimization-barrier.md](optimization-barrier.md) (handler #6); it proves the simplifier can do dead-code-style cleanup of tuple legs but never reorders or merges compute across the fence.

### TPU handlers (17 — 16 layout-aware overrides + 1 TPU-only)

The TPU subclass overrides handlers whose open-source bodies could produce a tensor with a layout the TPU codegen cannot tile. Each TPU handler takes `Target const*` and guards rewrites with `TpuAlgebraicSimplifierVisitor::IsValidLayout(Shape const&)` (`0x13443660`) and re-stamps layouts via `TpuAlgebraicSimplifier::UpdateLayout(Shape*)` (`0x13444860`).

| TPU handler | Addr | TPU-specific behaviour (recovered) |
|---|---|---|
| `HandleDot` | `0x13441ce0` | dot-strength-reduction gated by `xla_tpu_enable_dot_strength_reduction`; uses `ShouldStrengthReduceDotToReduce` (`0x13443800`) to decide `dot → reduce(multiply)` only when the resulting shape tiles favourably |
| `HandleConvolution` | `0x13442f20` | conv input-pad folding via `FoldConvInputPad` (`0x1343dca0`) — absorb a `Pad` into the conv's `window` rather than materializing it |
| `HandleAdd` | `0x1343ddc0` | layout-preserving add canonicalization; lambda `$_1` (`0x1343e640`) handles the operand-reorder case |
| `HandleConcatenate` | `0x1343e8a0` | concat canonicalization that keeps the concat dim on a TPU-favoured (minor) axis |
| `HandleSlice` / `HandleDynamicSlice` / `HandleDynamicUpdateSlice` | `0x1343f000` / `0x1343f140` / `0x1343f0c0` | slice/DUS simplification that re-stamps the result layout (`UpdateLayout`) |
| `HandleReshape` / `HandleTranspose` / `HandleBitcast` | `0x1343f1e0` / `0x1343ffe0` / `0x134401c0` | reshape/transpose/bitcast folds gated on `IsValidLayout` of the result |
| `HandleBroadcast` | `0x13440100` | broadcast canonicalization toward a TPU-favoured target dim (complements the separate `TpuBroadcastRewriter` pass) |
| `HandleCompare` / `HandleConvert` / `HandleCopy` | `0x13440540` / `0x13441060` / `0x13441200` | compare/convert/copy folds; `HandleCopy` removes layout-no-op copies the open-source pass would keep |
| `HandlePad` | `0x1343f360` | TPU pad simplification (pairs with `FoldConvInputPad`) |
| `HandleSort` | `0x134414c0` | sort-operand simplification; uses a comparator predicate (`$_1`/`$_2` lambdas `0x13441a20`/`0x13441c00`) to decide redundant-operand removal |
| `HandleRngBitGenerator` | `0x13443440` | **TPU-only** handler (no base equivalent) — simplify RNG-bit-generator patterns after the RNG expander family has run |

Two shared TPU helpers underpin these: `SetupDerivedInstruction(HloInstruction*, HloInstruction*, bool)` (`0x134445c0`) propagates metadata/layout to a newly-created replacement, and `UpdateLayout` (`0x13444860`) stamps the chosen tiled layout so the rewritten op is immediately legal for the post-layout pipeline.

> **NOTE — why a TPU subclass at all.** The simplifier runs **after** layout assignment ([layout-assignment.md](layout-assignment.md)), so every instruction already carries a committed tiled layout. The open-source `HandleReshape`/`HandleTranspose`/`HandleCopy` can rewrite to a shape whose layout is undefined or untileable for the MXU's 128×128 geometry; the TPU overrides add the `IsValidLayout` guard and the `UpdateLayout` re-stamp so a peephole rewrite never breaks the layout invariant the `tpu`→LLO legalizer ([tpu-to-llo-ods.md](tpu-to-llo-ods.md)) depends on. The 40 non-overridden handlers (e.g. `HandleGetTupleElement`, the collective and reduce-family handlers) are layout-transparent, so the base body is sound as-is.

---

## `AlgebraicSimplifierOptions` — the Gates

The pass is configured by an `xla::AlgebraicSimplifierOptions` struct passed by reference to the constructor and copied inline into the pass at `+8`. It is the open-source XLA options struct (the same one the GPU/CPU backends use), so it carries the full XLA gate family; the decompile confirms its presence (dtor `0x1343ba00`), the `ReshapeIsBitcast(Shape, Shape)` accessor (`0x1dd0e0c0`) consulted by `HandleReshape`, and the `run_to_fixed_point` bool read by `RunImpl` at `+135`. The gates relevant to the TPU path:

| Option (semantic) | How it gates a rule | Evidence |
|---|---|---|
| `run_to_fixed_point` | enables the re-run-to-fixpoint loop in `RunImpl` (else one pass/computation) | byte at pass `+135`, read at `0x13443f40` |
| `is_layout_sensitive` | makes the simplifier respect committed layouts (TPU runs it post-layout) | TPU overrides query `IsValidLayout` `0x13443660` |
| `enable_dot_strength_reduction` | enables `dot → reduce/multiply` rewrites in `HandleDot` | flag `xla_tpu_enable_dot_strength_reduction` (string-confirmed); `ShouldStrengthReduceDotToReduce` `0x13443800` |
| `ReshapeIsBitcast(from, to)` | decides whether `HandleReshape` folds a reshape into a `bitcast` | accessor `0x1dd0e0c0`; called from `BitcastingOperandOfReshapeOrCopyChain` `0x1dd0df60` |
| `enable_conv_simplification` | gates `HandleConvolution` fold/swap | TPU `FoldConvInputPad` `0x1343dca0` |
| `minmax_propagate_nan` | controls `min/max → clamp` NaN semantics in `MinMaxToClamp` | helper `0x1dd23380` |
| `enable_negative_padding_replacement` | gates negative-pad handling in `HandlePad` | base `HandlePad` body |

> **NOTE — the only TPU-specific *flag string* recovered is `xla_tpu_enable_dot_strength_reduction`.** It surfaces verbatim in the strings table (e.g. `"4\n%xla_tpu_enable_dot_strength_reduction"` and the bare `xla_tpu_enable_dot_strength_reduction`), confirming dot-strength-reduction is a TPU-gated rule family. The other rows are the standard `AlgebraicSimplifierOptions` boolean members whose names are upstream-XLA; they are present as struct fields (the struct is constructed and copied), but their exact byte offsets and the corresponding `xla_*` flag strings were **not** individually pinned in the bounded grep budget. Treat the *existence* of these gates as CONFIRMED (it is the upstream options struct) and the per-field offset as unrecovered. [Confidence: as marked per row.]

---

## What Is Not Recovered

- **Full per-field layout of `AlgebraicSimplifierOptions`.** Only `ReshapeIsBitcast` (accessor `0x1dd0e0c0`) and `run_to_fixed_point` (pass `+135`) are byte-pinned; the remaining boolean gates are known to exist (upstream struct, constructed and copied by the `AddPass` trampoline) but their offsets and flag-string bindings were not individually traced. [Confidence: LOW on offsets.]
- **Every base handler body.** The 40 non-overridden handlers run the upstream `algebraic_simplifier.cc` logic; their full rule arithmetic is not re-derived here (it is open-source). The TPU-override surface and the helpers are byte-anchored; the *contents* of each TPU `Handle*` body are sketched from the helper calls, not exhaustively decompiled. [Confidence: HIGH on the override set and helpers; MEDIUM on per-handler rewrite detail.]
- **The complete 140-slot `DfsHloVisitorBase` opcode map.** ~24 slots are labelled from the reloc-addend walk; the remaining slots and the exact pure-virtual set (the opcodes every visitor *must* implement, e.g. `HandleParameter`/`HandleConstant`/`HandleReduce`) need the full vtable walk. [Confidence: HIGH on the labelled slots, LOW on the unlabelled remainder.]
- **The `xla::match::` EDSL grammar.** The match expressions inside each `Handle*` are built from the `xla::match::` combinators (1,117 instantiations binary-wide); the combinator ABI is a separate decode and is not reproduced here.

---

## Confidence Summary

| Claim | Evidence |
|---|---|
| TPU pass is `jellyfish::TpuAlgebraicSimplifier`, subclass of `AlgebraicSimplifier` | symbols `TpuAlgebraicSimplifier::RunImpl` `0x13443f40`, `AddPass<TpuAlgebraicSimplifier,Target const&,AlgebraicSimplifierOptions&>` `0x10954400` |
| Dispatch is opcode-keyed virtual table, not benefit pattern matching | `DfsHloVisitorBase<HloInstruction*>` vtable `0x21d2c320`, 140 slots; per-`Handle<Op>` emitted functions |
| Base visitor emits 56 `Handle*`; TPU subclass emits 17 (16 overrides + 1 TPU-only) | 56 `AlgebraicSimplifierVisitor::Handle*` + 17 `TpuAlgebraicSimplifierVisitor::Handle*` emitted functions |
| `HandleRngBitGenerator` is TPU-only | TPU `0x13443440`; no base `AlgebraicSimplifierVisitor` RngBitGenerator symbol emitted |
| Per-computation entry `Run` returns a changed-bool | `AlgebraicSimplifierVisitor::Run` `0x1dd010e0`, signature recovered |
| Run-to-fixpoint loop, cap 50, non-fatal log on stuck | `RunImpl` `0x13443f40`; cap `0x32`; `tpu_algebraic_simplifier.cc:985` log string |
| Single-caller gate (skip computations with >1 caller) | `caller_instructions()` size check (`v19 > 1` skip) in `RunImpl` |
| Options stored inline at pass `+8`; `run_to_fixed_point` at `+135` | `AddPass` trampoline `0x10954400` copies options to `+8`; `RunImpl` reads `+135` |
| TPU overrides are layout-aware (`IsValidLayout`/`UpdateLayout`) | `IsValidLayout` `0x13443660`, `UpdateLayout` `0x13444860`, `SetupDerivedInstruction` `0x134445c0` |
| `xla_tpu_enable_dot_strength_reduction` gates dot strength reduction | flag string in strings table; `ShouldStrengthReduceDotToReduce` `0x13443800` |
| Source units `tpu_algebraic_simplifier.cc` / `…/simplifiers/algebraic_simplifier.cc` | string-anchored (lines 961/985/991; base file path) |
| Full `AlgebraicSimplifierOptions` field layout / flag bindings | only 2 fields byte-pinned |

---

## Cross-References

- [HLO Pre-Passes](hlo-pre-passes.md) — the ordered pre-pass set; this is pass **#60** (`TpuAlgebraicSimplifier`), at the tail of `HloOptimizeThroughLayoutAssignment`.
- [Compiler overview](overview.md) — the five-phase spine; the simplifier is in Family 1 (HLO optimization passes), Phase 1.
- [Optimization Barrier](optimization-barrier.md) — documents `HandleOptimizationBarrier` (handler #6 of this visitor), the only rewrite that prunes dead tuple legs around an opaque op.
- [Layout Assignment](layout-assignment.md) — commits the tiled layouts that the TPU subclass's `IsValidLayout`/`UpdateLayout` guards respect (the reason a TPU subclass exists).
- [HLO Pass Registry](hlo-pass-registry.md) — the `HloPassInterface` RTTI catalog this pass is one entry of.
- [Compile Phases 0–3](compile-phases.md) — where Phase 1 HLO optimization sits relative to the MLIR lowering crossing.
- [Fusion Patterns](fusion-patterns.md) — the fusion passes that run after simplification; their priority-queue dispatcher is a *third* engine, distinct from this opcode-vtable visitor.
- [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md) — the eventual lowering of the `Dot`/`Convolution` ops the TPU `HandleDot`/`HandleConvolution` canonicalize.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / Front-end and pipeline — [back to index](../index.md)
