# DialectConversion Legalizer

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VA == file offset). Other versions will differ; treat every VA as version-pinned.*

## Abstract

When the `tpu`-dialect program descends to LLO ([tpu → LLO Lowering](tpu-to-llo-ods.md)), to the Mlo DMA representation ([LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md)), or when a SparseCore kernel drops to LLVM ([LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md)), the engine that actually *runs* the conversion patterns is **not** the flat-benefit greedy driver. It is MLIR's `DialectConversion` machinery, whose private dispatcher is the anonymous-namespace `OperationLegalizer`. That dispatcher differs from the greedy `GreedyPatternRewriteDriver` ([ConversionPatternRewriter](conversion-pattern-rewriter.md)) in exactly one structural way that this page exists to pin: it is **depth-aware**. Where the greedy driver tries patterns in descending raw `PatternBenefit`, the legalizer tries them in *ascending legalization depth* — the length of the chain of further lowerings each candidate pattern's output would itself trigger before the IR is fully legal.

The legalizer shares the `PatternApplicator` core and the `Pattern` vtable ABI with the greedy driver; the only injected difference is the cost function it hands to `PatternApplicator::applyCostModel`. The two pieces a reimplementer needs that the per-pass lowering pages do not carry are therefore: **the integer depth recurrence** (`computeOpLegalizationDepth` / `applyCostModelToPatterns`, byte-decoded below) and **the `ConversionTarget` legality model** — the `LegalizationAction` enum, the recursively-legal packed bit, and the per-pass legal/illegal/dynamically-legal declarations for the three TPU lowerings. The recursive `legalize`-to-fixpoint driver that consumes both ties the page together.

The reader should hold the textbook MLIR `DialectConversion` frame — `ConversionTarget`, `TypeConverter`, `RewritePattern`, `applyFullConversion` — and read this page as the byte-level recovery of how that frame's *legalizer* ranks and applies patterns. Where the recovered behavior diverges from the naive "try highest benefit" intuition (it does not; depth wins, benefit only tie-breaks), the divergence is called out.

For reimplementation, the contract is:

- **The depth recurrence.** `legalizationDepth(op) = min over patterns P rooted at op of (1 + max over ops Q that P produces of legalizationDepth(Q))`, with `legalizationDepth(Q)=0` for legal / pattern-less ops, memoized, cycles broken by an in-progress sentinel.
- **The sort + cost injection.** Patterns are stable-sorted ascending by depth, tie-broken on `PatternBenefit` at `Pattern+0xc`; this is the *only* difference from the greedy driver's cost model.
- **The legality model.** `LegalizationAction {Legal=0, Dynamic=1, Illegal=2}`, the 48-byte `LegalizationInfo` record, `isLegal`'s 16-bit return with the recursively-legal bit `0x100`, and `setOpAction`/`setDialectAction`/`markOpRecursivelyLegal`/`setLegalityCallback`.
- **The legalization driver.** `OperationLegalizer::legalize` as a recursive descent to a fixpoint: a pattern "succeeds" only if the sub-tree it produces is itself fully legalizable; the depth cost front-loads the shallowest sub-tree.

| | |
|---|---|
| **Dispatcher** | `(anon)::OperationLegalizer` (private to MLIR's `DialectConversion.cpp`) |
| **Depth recurrence** | `OperationLegalizer::computeOpLegalizationDepth(OperationName, DenseMap<OperationName,unsigned>&, DenseMap<OperationName,SmallVector<Pattern const*,1>>&)` — `0x1c9607c0` |
| **Cost function** | `OperationLegalizer::applyCostModelToPatterns(...)` — `0x1c960940` (returns min depth, sorts patterns) |
| **Sort comparator** | `applyCostModelToPatterns::$_0` inside `__stable_sort_move` — `0x1c9614a0` (primary depth, tie-break benefit) |
| **Legalize driver** | `OperationLegalizer::legalize(Operation*)` — `0x1c953820`; `legalizeWithFold` — `0x1c95baa0` |
| **Legality query** | `ConversionTarget::isLegal` — `0x1c957ce0`; `setOpAction` — `0x1c957780`; `markOpRecursivelyLegal` — `0x1c9580a0` |
| **Legality enum** | `ConversionTarget::LegalizationAction {Legal=0, Dynamic=1, Illegal=2}` |
| **Drivers** | `mlir::applyFullConversion` (mode=1) — `0x1c958ac0` · `mlir::applyPartialConversion` (mode=0) — `0x1c958a60` |
| **Confidence** | CONFIRMED (byte-anchored) for the depth recurrence + legality model; HIGH where a row says so |

---

## Two Dispatchers, One Applicator

### Purpose

The legalizer is easy to misread as "the greedy driver applied to conversion patterns." It is not. They share machinery but order patterns by opposite keys. This unit fixes the relationship before the formula, because the formula only makes sense against the contrast.

### The shared core and the single difference

Both the greedy rewriter and the conversion legalizer drive the *same* `PatternApplicator` (`0x1c9965c0`) over the *same* frozen `Pattern` set, and both reach the per-pattern `matchAndRewrite` through the identical `Pattern` vtable ABI ([ConversionPatternRewriter](conversion-pattern-rewriter.md)). The applicator buckets patterns by root op and, within each bucket, applies them in an order fixed by a cost function passed to `PatternApplicator::applyCostModel`. The *only* structural difference between the two engines is which cost function they inject — and, downstream of that, that the legalizer recurses on the ops a pattern produces rather than running a single greedy worklist pass.

| Dispatcher | Cost fn passed to `applyCostModel` | Bucket sort order | Tie-break | Engine |
|---|---|---|---|---|
| `GreedyPatternRewriteDriver` | `applyDefaultCostModel` (raw `PatternBenefit` at `Pattern+0xc`) | **descending** raw benefit | stable | `mlir::applyPatternsGreedily` `0x1c968400` (drives the `(anon)::GreedyPatternRewriteDriver` class) |
| `OperationLegalizer` (conversion) | `computeLegalizationGraphBenefit::$_0` | **ascending** legalization depth | `PatternBenefit` at `Pattern+0xc` | `0x1c953820` |

> **NOTE — the legalizer's preference inverts the greedy driver's.** Greedy: try the *most beneficial* pattern first, single pass. Legalizer: try the *shortest chain to fully-legal IR* first, then recurse on whatever that pattern produced. A reimplementer who reuses the greedy cost model for a conversion pass will produce correct IR (any complete path legalizes) but will explore deeper sub-trees first, blowing up the recursion the [legalize driver](#the-legalization-driver) depends on. The depth model is a *performance* invariant of the conversion engine, not a correctness one — but the TPU passes' pattern sets were built assuming it.

### The Pattern struct layout (benefit at +0xc, not +0x8)

The depth sort's tie-break reads the `PatternBenefit`, so the `Pattern` layout must be pinned. The relevant offsets, recovered from the `Pattern` ctor and confirmed by the sort comparator's byte reads:

```text
Pattern+0x00  rootValue          (RootKind-discriminated pointer / TypeID)
Pattern+0x08  RootKind           (uint32 discriminator; == 1 ⇒ fixed root op)
Pattern+0x0c  PatternBenefit     (uint16)         ← benefit lives HERE
Pattern+0x10  context            (MLIRContext*)
Pattern+0x18  generatedOps.data  (ArrayRef<OperationName>.data)   ← the produced-op set
Pattern+0x20  generatedOps.size  (ArrayRef<OperationName>.size)
```

> **NOTE — the `PatternBenefit` is at `Pattern+0xc`, and `+0x8` is the `RootKind` discriminator, not the benefit.** The sort comparator (`__stable_sort_move` at `0x1c9614a0`) reads the tie-break key as `*(_WORD *)(*pair + 12)` — a `uint16` at `Pattern+0xc`. `Pattern+0x8` is the `RootKind` (`uint32`), which the graph builder and `computeLegalizationGraphBenefit::$_0` test with `cmp [Pattern+0x8], 0x1` to decide "has a fixed root op." (A `+0x8` benefit reading is easy to land on by mistake: that offset *is* the benefit in the separate `SmallDenseMap<Pattern*,PatternBenefit>` cost-cache pair, but not in the `Pattern` struct itself.) The `impossibleToMatch` sentinel is the all-ones `uint16` `0xFFFF` (the `PatternBenefit(unsigned)` ctor stores the raw value with no clamp).

The `generatedOps` array at `Pattern+0x18`/`+0x20` is the input to the entire depth recurrence: it is the set of `OperationName`s the pattern declares it will create, and the depth of a pattern is one plus the worst depth among them.

---

## The Depth Recurrence

### Purpose

This is the formula. It is the substance of why a "legalizer" exists separately from a "rewriter," and it is fully byte-decoded from the two cost functions. The recurrence is a memoized DAG traversal with a cycle-breaking sentinel.

### The formula

```text
legalizationDepth(op) =
    0                                            if op is already legal OR has no pattern
    min over patterns P rooted at op of
        ( 1 + max over ops Q that P generates of
                  legalizationDepth(Q) )         otherwise

  · memoized in depthMap (DenseMap<OperationName, unsigned>), each op computed once
  · cycles broken by the in-progress sentinel 0xFFFFFFFF (= effectively +inf,
    so a back-edge to an op under computation never wins the min)
```

Read in words: an op that is *already legal* (the conversion target accepts it) or that no pattern is rooted at has depth `0` — it is a leaf of the legalization graph. Otherwise its depth is the *cheapest* (minimum) way to legalize it, and a single pattern's cost is `1` plus the *worst* (maximum) further-legalization depth among the ops that pattern emits. So depth is the **length of the longest chain of further lowerings** the chosen pattern's output triggers, minimized over the choice of pattern.

### `computeOpLegalizationDepth` — the per-op driver

`OperationLegalizer::computeOpLegalizationDepth(OperationName op, DenseMap<OperationName,unsigned>& depthMap, DenseMap<OperationName, SmallVector<Pattern const*,1>>& graph)` at `0x1c9607c0`. The control flow, byte-decoded:

```c
// computeOpLegalizationDepth(op, depthMap, graph)   @ 0x1c9607c0
unsigned computeOpLegalizationDepth(OperationName op, depthMap, graph):
    // 1. MEMOIZATION LOOKUP — probe depthMap (DenseMap<OperationName,unsigned>)
    //    linear-probe bucket = ((opHash>>4) ^ (opHash>>9)) & (cap-1)
    if op found live in depthMap:
        return bucket->value           // mov eax,[bucket+8] ; cached depth, done

    // 2. GRAPH LOOKUP — probe graph (op -> SmallVector<Pattern*,1>)
    if op NOT in graph  ||  graph[op].patternCount == 0:
        return 0                       // xor eax,eax ; legal / pattern-less ⇒ depth 0

    // 3. RECURSE — mark in-progress, then cost the patterns
    depthMap.insert(op, 0xFFFFFFFF)    // sentinel: "currently computing" cycle-breaker
    unsigned d = applyCostModelToPatterns(graph[op].patterns, depthMap, graph)
    depthMap[op] = d                   // store final depth back into the bucket
    return d
```

Step 1 is the cache hit (`return *((unsigned int *)bucket + 2)` — bucket value at `+0x8`). Step 2 returns `0` when the op is missing from the graph *or* its pattern-list count (`graph-bucket+0x10`) is zero: **a legal or pattern-less op is a depth-0 leaf.** Step 3 is the critical part: it first inserts `op → 0xFFFFFFFF` (the `v25[0] = -1` store feeding `lookupOrInsertIntoBucket`) *before* recursing, so that if the recursion re-enters `op` (pattern for A produces B whose pattern produces A), the inner lookup hits the sentinel — which, being the max `unsigned`, can never win a `min` and so terminates the cycle without a stack overflow. The real depth from `applyCostModelToPatterns` then overwrites the sentinel.

### `applyCostModelToPatterns` — the cost + the sort

`OperationLegalizer::applyCostModelToPatterns(SmallVector<Pattern const*,1>& patterns, depthMap, graph)` at `0x1c960940`. This is *the* depth-aware cost function. It runs three passes and returns the minimum per-pattern depth.

```c
// applyCostModelToPatterns(patterns, depthMap, graph)   @ 0x1c960940
unsigned applyCostModelToPatterns(patterns, depthMap, graph):
    SmallVector<pair<Pattern*,unsigned>, 4> local;
    unsigned minDepth = 0xFFFFFFFF;                  // running global min (r14d)

    // PASS 1 — per-pattern depth
    for Pattern* p in patterns:
        unsigned d = 1;                              // base: a pattern is at least depth 1
        for OperationName gen in p->generatedOps:    // Pattern+0x18 data / Pattern+0x20 count
            unsigned childDepth = computeOpLegalizationDepth(gen, depthMap, graph);
            d = max(d, childDepth + 1);              // inc + cmova
        // (if generatedOps is empty, d stays 1)
        local.emplace_back({p, d});                  // growAndEmplaceBack pair
        minDepth = min(minDepth, d);                 // cmovb

    // PASS 2 — stable sort (ascending depth, then ascending PatternBenefit)
    std::stable_sort(local, $_0);                    // 0x1c9611c0 -> 0x1c9614a0

    // PASS 3 — rebuild the input `patterns` vector in sorted (depth-cheapest-first) order
    patterns.clear();
    for (p, d) in local: patterns.push_back(p);
    return minDepth;                                 // = legalizationDepth(op)
```

Every step is confirmed in the decompile: the per-pattern `d = 1` initialization (`v37[0] = 1`), the inner `childDepth + 1` then `max` (`v11 = computeOpLegalizationDepth(...) + 1; if (d > v11) v11 = d`), the empty-`generatedOps` branch that leaves `d = 1`, the `growAndEmplaceBack<Pattern const*&, unsigned&>` of the `{pattern, depth}` pair, the running `min` (`if (depth < minDepth) minDepth = depth`, seeded `0xFFFFFFFF`), the `__stable_sort`, the input-vector rebuild (the count at `patterns+0x8` is zeroed then re-pushed), and the `return minDepth`.

### The sort comparator — two keys

The comparator `$_0` (inlined into `__stable_sort_move` at `0x1c9614a0`) is a two-key strict-weak ordering on `pair<Pattern*, unsigned depth>`:

```c
// $_0(lhs, rhs) — strict weak ordering, byte-decoded from the 2-element base case
bool less(pair lhs, pair rhs):
    if (lhs.depth != rhs.depth)            // primary key: unsigned depth at pair+0x8
        return lhs.depth < rhs.depth;      //   cmp [lhs+8], [rhs+8]
    // depths equal ⇒ tie-break on PatternBenefit (uint16 at Pattern+0xc)
    return lhs.pattern->benefit < rhs.pattern->benefit;   // movzx WORD [pat+0xc]; cmp; jb
```

The 2-element base case in the decompile shows it exactly: it compares `*((_DWORD *)lhs + 2)` against `*((_DWORD *)rhs + 2)` (the `unsigned` depth at `pair+0x8`), and *only when those are equal* falls through to compare `*(_WORD *)(lhs->pattern + 12)` against `*(_WORD *)(rhs->pattern + 12)` (the `uint16` benefit at `Pattern+0xc`). The result: the **shortest conversion chain to fully-legal IR sorts first**, and among equal-depth patterns the lower stored benefit sorts first.

> **NOTE — depth dominates benefit, always.** A pattern with depth 1 and benefit 0 sorts ahead of a pattern with depth 2 and benefit `0xFFFE`, because depth is the primary key. This is the concrete sense in which the legalizer is "depth-aware": no amount of declared `PatternBenefit` can promote a longer lowering chain ahead of a shorter one. Benefit only decides ties between equal-depth candidates. (How `computeLegalizationGraphBenefit::$_0` at `0x1c961d60` encodes the resulting depth back into the 16-bit `PatternBenefit` the applicator's stable-sort consumes — direct depth vs an inverted `maxDepth-depth` — is recovered structurally but the exact `depth→uint16` arithmetic was not traced to its store: **LOW** on that one encoding step. It does not affect the ordering semantics above, which are byte-confirmed in the legalizer's own comparator.)

### `buildLegalizationGraph` — the input to the recurrence

The two `DenseMap`s the recurrence walks are built before legalization begins. `buildLegalizationGraph::$_0` (`0x1c95eea0`, the per-pattern visitor) walks every native `Pattern` in the frozen set via `PatternApplicator::walkAllPatterns`. For each pattern it reads the root `OperationName` (when `RootKind` at `Pattern+0x8 == 1`), asks the `ConversionTarget` (`getOpInfo`, `0x1c957a20`) whether that op is already legal (a graph *leaf*), and inserts the pattern into:

```text
graph : DenseMap<OperationName, SmallVector<Pattern const*,1>>
        op  ->  patterns rooted at op            (consumed by computeOpLegalizationDepth)
        pattern -> generated ops                 (from Pattern+0x18 / +0x20)
```

This is the directed dependency graph (`op → {patterns} → {produced ops}`) the depth recurrence traverses. `computeLegalizationGraphBenefit::$_0` (`0x1c961d60`) is the `function_ref` the legalizer hands to `PatternApplicator::applyCostModel`: it reads the pattern's `RootKind`, looks the root op up in `graph`, and returns a `PatternBenefit` derived from the depth — which is precisely the injection point that turns the shared applicator into a depth-aware one.

---

## The ConversionTarget Legality Model

### Purpose

The depth recurrence's depth-0 base case ("op is already legal") is decided by the `ConversionTarget`. This unit pins the legality enum, the storage record, the `isLegal` packed return, and the API that the three TPU passes use to declare their legal/illegal/dynamically-legal sets.

### The LegalizationAction enum

`ConversionTarget::LegalizationAction` is a real nested enum (the mangled name `ConversionTarget::LegalizationAction` appears in `setOpAction`/`setDialectAction`/`setLegalityCallback` signatures). Pinned from `setOpAction` (`0x1c957780`), `getOpAction` (`0x1c9579a0`), and `isLegal` (`0x1c957ce0`):

| Value | Action | `isLegal` behavior |
|---|---|---|
| `0` | **Legal** | statically legal; `isLegal` returns `0x1` (`+0x100` if recursively legal) |
| `1` | **Dynamic** | calls the per-op `std::function<optional<bool>(Operation*)>` legality callback; `isLegal` does `call [OpInfo+0x28]` then tests the optional-has-value bit and the low (legal) bit |
| `2` | **Illegal** | `isLegal` returns `0` ⇒ the op must match a conversion pattern |

### The LegalizationInfo record + storage

The target stores legality as a `SmallVector<pair<OperationName, LegalizationInfo>>` indexed by a side `DenseMap<OperationName, unsigned>`. `setOpAction` (`0x1c957780`) confirms the record is **48 bytes** with these fields:

```text
LegalizationInfo (48 bytes, per op)
  +0x00  OperationName             (the keyed op)
  +0x08  uint32  action            (LegalizationAction 0/1/2)     ← setOpAction writes here
  +0x18  (zeroed on insert)
  +0x20  std::function legalityCallback storage (policy fn pointer + data)
  +0x28  legalityCallback invocable (default = __empty_func on a static-only entry)
  recursivelyLegal flag is packed into the getOpInfo return
```

`setOpAction` looks the op up in the index map, grows the `SmallVector` (`growAndEmplaceBack<piecewise_construct_t, tuple<OperationName const&>, tuple<>>`) if new — zero-initializing the callback `std::function` to the empty policy func — and writes the action `a3` into `info+0x8`. The 48-byte stride (`48 * index`) and the action store `*(_DWORD *)(result + 8) = a3` are both byte-visible.

### The legality API

| Function | VA | Role |
|---|---|---|
| `ConversionTarget::setOpAction(OperationName, LegalizationAction)` | `0x1c957780` | set one op's action |
| `ConversionTarget::setDialectAction(ArrayRef<StringRef>, LegalizationAction)` | `0x1c957880` | set a whole dialect's action by name (e.g. mark `llo` Legal) |
| `ConversionTarget::getOpAction(OperationName)` | `0x1c9579a0` | read an op's action |
| `ConversionTarget::getOpInfo(OperationName)` | `0x1c957a20` | fetch the `LegalizationInfo` (also packs the recursively-legal flag) |
| `ConversionTarget::isLegal(Operation*)` | `0x1c957ce0` | the 16-bit packed legality query (below) |
| `ConversionTarget::markOpRecursivelyLegal(OperationName, std::function<optional<bool>(Operation*)>)` | `0x1c9580a0` | mark recursively legal + optional callback |
| `ConversionTarget::setLegalityCallback(ArrayRef<StringRef>, std::function<optional<bool>(Operation*)>)` | `0x1c9583c0` | install the dynamic-legality predicate |
| `ConversionTarget::addLegalOp<...>` / `addIllegalOp<...>` | many (variadic templates) | sugar over `setOpAction` for a fixed op pack |

The variadic `addLegalOp<...>` / `addIllegalOp<...>` instantiations are abundant in the binary (e.g. `addIllegalOp<scf::ForOp, IfOp, IndexSwitchOp, ParallelOp, WhileOp, ExecuteRegionOp>` at `0x167508a0`), each a thin forwarder to `setOpAction` per op in the pack.

### isLegal's packed 16-bit return

`isLegal` (`0x1c957ce0`) returns a value the legalize driver reads as a 16-bit word:

```text
bit 0   (0x001)  legal-now
bit 8   (0x100)  recursively-legal  (the op's whole sub-tree is legal; do not descend)
```

For a `Dynamic` op, `isLegal` calls the per-op callback at `LegalizationInfo+0x28` and tests `eax & 0x100` for the optional-has-value bit before reading the low bit. The legalize driver branches on the recursively-legal bit with `cmp ecx, 0x100; jae` — at or above `0x100` means "recursively legal, walk children and stop."

---

## Per-Pass Legality Declarations

The three TPU conversion passes each construct a `ConversionTarget` and declare their legal set. The declarations are recovered from the `setOpAction`/`setDialectAction`/`markOpRecursivelyLegal`/`addDynamicallyLegalOp` call census in each pass body. All three use `applyFullConversion` — a leftover illegal op aborts the pass.

### LowerToLLO

`createLowerToLLOPass` ([tpu → LLO Lowering](tpu-to-llo-ods.md)); the legality declarations re-pinned against the `addDynamicallyLegalOp<…>` typeinfo names:

| Entity | Action | Mechanism | Confidence |
|---|---|---|---|
| `llo` dialect (entire) | Legal | `setDialectAction(["llo"], 0)` | HIGH |
| `scf.{If,For,While,Condition,Yield}` | Dynamic | `addDynamicallyLegalOp` — legal when region/yield types are in the converted set | HIGH |
| `func.{FuncOp,ReturnOp}` | Dynamic | `addDynamicallyLegalOp` — legal when arg + result types are converted | HIGH |
| `builtin.unrealized_conversion_cast` | Legal | allowed in transit (reconciled at the end) | HIGH |
| all `tpu` / `arith` / `math` / `vector` / `cf` / ... | Illegal | default ⇒ must match a pattern | HIGH |

Driver: `applyFullConversion` (mode = 1) — aborts if any `tpu.*` op survives.

### LowerToMlo

`LowerToMloPass::runOnOperation` (`0x1322b200`). The legal-dialect set is the `MloConversionTarget` (ctor confirmed: `mlir::tpu::MloConversionTarget::MloConversionTarget(MLIRContext&)`; `insertLegalDialects(DialectRegistry&)` confirmed):

| Entity | Action | Mechanism | Confidence |
|---|---|---|---|
| `MloConversionTarget` legal dialects | Legal | ctor `SmallDenseMap<TypeID>`: Builtin, `LLVM::LLVMDialect`, `sparse_core::LlvmTpuDialect`, `arith`, `cf`, `func`, `math`, `memref`, `scf`, `sparse_core::ScDialect`, `vector` | HIGH |
| `func.FuncOp` | Dynamic | `addDynamicallyLegalOp<func::FuncOp>` — legal when arg/result types converted | HIGH |
| `builtin.unrealized_conversion_cast` | Dynamic | `addDynamicallyLegalOp<UnrealizedConversionCastOp>` | HIGH |
| 2× `setOpAction` + 2× `setLegalityCallback` | mixed | per-op overrides (specific op names + predicates not per-lambda decoded) | MEDIUM |

Type converter: a shared `populateTypeConverter` with 10 `registerConversion` entries (`TupleType`, `IndexType`, `FloatType`, `LLVM::LLVMPointerType`, `tpu::DMASemaphoreType`, `tpu::SemaphoreType`, `sparse_core::WordType`, `IntegerType`, `VectorType`, `MemRefType`) plus 3 `registerTypeAttributeConversion` (`BaseMemRefType` ↔ {`tpu::MemorySpaceAttr`, `sparse_core::MemorySpaceAttr`, `IntegerAttr`}). Patterns: `populateTpuToMloConversionPatterns` (`0x1322c920`, ~180) + `populateSCFStructuralTypeConversions`. Driver: `applyFullConversion` (mode = 1). Detail: [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md).

### sparse_core → LLVM

`LowerToSparseCoreLlvmPass::runOnOperation` (`0x13566d00`; `CreateLowerToSparseCoreLlvmPass` confirmed). This pass runs two `applyFullConversion`s — first an SCF→CFG leg (`lowerScfToCfg`), then the LLVM leg:

| Entity | Action | Mechanism | Confidence |
|---|---|---|---|
| `LLVMConversionTarget` (`llvm` dialect) | Legal | `mlir::LLVMConversionTarget(ctx)` marks the LLVM dialect legal | HIGH |
| `scf.{for,if,parallel,while,index_switch}` | Recursively-Legal | `markOpRecursivelyLegal` ×5 (op-name strings read byte-exact: `scf.for`, `scf.if`, `scf.parallel`, `scf.while`, `scf.index_switch`) | HIGH |
| `scf.{If,Parallel,While,IndexSwitch}` | Dynamic | `addDynamicallyLegalOp<scf::IfOp, ParallelOp, WhileOp, IndexSwitchOp>` | HIGH |
| 4× `setOpAction` + 2× `setLegalityCallback` | mixed | per-op overrides | MEDIUM |
| arith/math packed-operand ops (40+) | Dynamic | `PackedOperandsLowering::AddDynamicallyLegalAluEpOps<Op, UnpackFOp, PackFOp>` / `AddDynamicallyLegalCmp<Op, {UnpackFOp,PackFOp} or {UnpackSIOp,PackSIOp}>` — legal iff operands already unpacked | HIGH |

Type converter: `mlir::LLVMTypeConverter(ctx, LowerToLLVMOptions)` + custom `registerConversion` for SparseCore types: `I32PairType` (1:N → 2×i32), `TupleType` (1:N → element types), `VectorType`, `Float8E4M3B11FNUZ`/`Float8E4M3FN`/`Float8E5M2`, `WordType`, plus `registerTypeAttributeConversion` `BaseMemRefType` ↔ `sparse_core::MemorySpaceAttr`. Patterns: `populateSCFToControlFlowConversionPatterns` + `populateFinalizeMemRefToLLVMConversionPatterns` + the 115 `SCConvertOpToLLVMPattern<Op>`. Detail: [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md).

> **NOTE — the SparseCore pass is the only place the 1:N type path is heavily exercised.** `I32PairType → 2×i32` and `TupleType → element types` are genuine one-to-many conversions; `populateSCFStructuralTypeConversions` adapts the SCF region signatures for the resulting `ValueRange`s. The TensorCore LowerToLLO/Mlo type set is overwhelmingly 1:1 (vector → one vreg, `i1` → predicate, `index` → `i32`, semaphore → `llo.semaphore`). See [ConversionPatternRewriter](conversion-pattern-rewriter.md) for the `ConversionPattern` slot4 (1:1 `ArrayRef<Value>`) vs slot5 (1:N `ArrayRef<ValueRange>`) dispatch.

---

## The Legalization Driver

### Purpose

The depth cost and the legality model feed one recursive function. This unit pins how `legalize` consumes them: it is a recursive descent to a fixpoint where a pattern "succeeds" only if the sub-tree it produces is itself legalizable, and the depth cost exists to make that recursion shallow.

### `OperationLegalizer::legalize`

`OperationLegalizer::legalize(Operation* op)` at `0x1c953820`:

```c
// OperationLegalizer::legalize(op)   @ 0x1c953820
LogicalResult legalize(Operation* op):
    if ConversionPatternRewriterImpl::isOpIgnored(op):       // 0x1c94e680
        return success                                       // already handled

    uint16 leg = target.isLegal(op)                          // 0x1c957ce0
    if leg >= 0x100:                                         // recursively-legal bit
        walk(op.children)                                    // mlir::detail::walk 0xea2c5e0
        return success                                       // children need no visit

    // not-yet-legal: read the OpConversionMode (set by applyConversion, below)
    mode = rewriterImpl[+0x178][+0x2c]                       // Partial=0, Full=1, Analysis=2
    if mode == 1 (Full):
        legalizeWithFold(op)                                 // 0x1c95baa0, try constant fold first

    // legalizeWithPattern, inlined via three lambdas:
    if PatternApplicator::matchAndRewrite(op, rewriter,      // 0x1c9971e0
                                          canApply=$_0,       //   0x1c95c100
                                          onSuccess=$_1,      //   0x1c95c1a0
                                          onRecurse=$_2):     //   0x1c95c6e0
        return success
    if mode == 2 (Analysis): legalizeWithFold(op) (again)
    else:
        return op.emitError("failed to legalize operation '<name>'")   // 0x1c953a12
```

The three lambdas are the meat of `legalizeWithPattern` (their mangled symbols — `OperationLegalizer::legalizeWithPattern::$_1` and `$_2` — are present in the binary, confirming the inlining):

- **`$_0` canApply (`0x1c95c100`)** — tests `Pattern+0x10` bit `0x4` (`hasBoundedRewriteRecursion`) and maintains a `SmallPtrSet` of in-flight patterns to detect and reject unbounded recursive application of the same pattern to the same op.
- **`$_1` onSuccess (`0x1c95c1a0`)** — rewriter-state bookkeeping after a successful match: `Operation::erase`, `resetState` (`0x1c95bf60`), clearing the `DenseSet<UnrealizedConversionCastOp>` and `DenseMap<Operation*>` tracking maps.
- **`$_2` onRecurse (`0x1c95c6e0`)** — walks the *new* ops the pattern produced (`mlir::detail::walk<ForwardIterator>`, `0x0ea2c5e0`) and **recursively calls `legalize` on each** (two call sites back into `0x1c953820`); on a produced op that cannot be legalized it routes to `reportNewIrLegalizationFatalError` (confirmed symbol; the failure message is assembled by an `llvm::join` over the offending op names rather than emitted as a single static string). Returns `success` iff **every** produced op is itself legalized.

### `legalizeWithFold`

`legalizeWithFold` (`0x1c95baa0`) is the constant-fold attempt: `startOpModification` → `OpBuilder::tryFold` (`0x1d8563e0`) → `finalizeOpModification` → `replaceOp`; on success it **recursively legalizes** the materialized constant ops, and `cancelOpModification` rolls back on fold failure.

> **GOTCHA — a pattern does not "succeed" just because it matched.** `legalize` only returns success for an op if the *entire sub-tree* the chosen pattern produced is itself fully legalized (the `$_2` recurse returns success only when every produced op legalizes). This is why the depth cost matters: the legalizer is going to recurse into the produced ops regardless, so front-loading the pattern whose produced sub-tree is *shallowest* (KF depth) minimizes the expected recursion depth and the amount of speculative rewriting that must be rolled back when a deeper candidate path dead-ends. A reimplementer who applies patterns in arbitrary order will get correct IR but pathological recursion on ops with multiple legal lowering chains.

### applyFullConversion / applyPartialConversion → mode

The pass driver selects the mode the legalizer reads:

| Driver | Mode | Behavior |
|---|---|---|
| `mlir::applyFullConversion(op, target, patterns, config)` | `Full = 1` | attempts fold; **errors** on any unlegalized op |
| `mlir::applyPartialConversion(op, target, patterns, config)` | `Partial = 0` | leaves unlegalized ops in place, records them |
| (internal) Analysis | `Analysis = 2` | analyzes legalizability without committing |

`applyConversion` stores the `OpConversionMode` into the `OperationConverter` frame (the field `legalize` reads as `rewriterImpl[+0x178][+0x2c]`) and wraps the whole run in `MLIRContext::executeActionInternal<ApplyConversionAction>` when an action handler is registered. **All three TPU lowering passes use `applyFullConversion`** — a leftover `tpu.*` / `mlo.*` op aborts the pass. `mlir::applyFullConversion(Operation*, ConversionTarget const&, FrozenRewritePatternSet const&, ConversionConfig)` (`0x1c958ac0`) and `mlir::applyPartialConversion(...)` (`0x1c958a60`) are both thin wrappers over the `applyConversion(ArrayRef<Operation*>, ConversionTarget const&, FrozenRewritePatternSet const&, ConversionConfig, (anon)::OpConversionMode)` internal driver at `0x1c958840`, which carries the `OpConversionMode` as its trailing argument.

---

## Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `OperationLegalizer::computeOpLegalizationDepth` | `0x1c9607c0` | per-op depth recurrence (memoized, sentinel-broken) | CONFIRMED |
| `OperationLegalizer::applyCostModelToPatterns` | `0x1c960940` | per-pattern depth + stable sort + min | CONFIRMED |
| `applyCostModelToPatterns::$_0` (in `__stable_sort_move`) | `0x1c9614a0` | comparator: primary depth, tie-break benefit `+0xc` | CONFIRMED |
| `buildLegalizationGraph::$_0` | `0x1c95eea0` | builds the `op → patterns → produced ops` graph | HIGH |
| `computeLegalizationGraphBenefit::$_0` | `0x1c961d60` | cost `function_ref` injected into `applyCostModel` | HIGH |
| `OperationLegalizer::legalize` | `0x1c953820` | recursive legalize-to-fixpoint driver | CONFIRMED |
| `OperationLegalizer::legalizeWithFold` | `0x1c95baa0` | `tryFold` + recurse on materialized constants | CONFIRMED |
| `ConversionTarget::setOpAction` | `0x1c957780` | write `LegalizationAction` into 48-byte `LegalizationInfo+0x8` | CONFIRMED |
| `ConversionTarget::setDialectAction` | `0x1c957880` | set a whole dialect's action by name | CONFIRMED |
| `ConversionTarget::getOpAction` | `0x1c9579a0` | read an op's action | CONFIRMED |
| `ConversionTarget::getOpInfo` | `0x1c957a20` | fetch `LegalizationInfo` (+ recursively-legal pack) | HIGH |
| `ConversionTarget::isLegal` | `0x1c957ce0` | 16-bit packed legality (bit0 legal, bit8 recursive) | CONFIRMED |
| `ConversionTarget::setLegalityCallback` | `0x1c9583c0` | install dynamic legality predicate | CONFIRMED |
| `mlir::applyFullConversion` | `0x1c958ac0` | mode=1 driver (errors on leftover) | CONFIRMED |
| `mlir::applyPartialConversion` | `0x1c958a60` | mode=0 driver (records leftover) | CONFIRMED |
| `applyConversion` (internal) | `0x1c958840` | mode-carrying conversion entry | CONFIRMED |
| `MloConversionTarget` ctor / `insertLegalDialects` | confirmed symbols | LowerToMlo legal-dialect set | HIGH |
| `LowerToSparseCoreLlvmPass::runOnOperation` | `0x13566d00` | SparseCore → LLVM driver (2× full conversion) | CONFIRMED |

---

## What Is Not On This Page

- **The exact `depth→uint16` `PatternBenefit` arithmetic** in `computeLegalizationGraphBenefit::$_0` (`0x1c961d60`) — whether it stores the depth directly or an inverted `maxDepth - depth` so the applicator's descending stable-sort yields ascending depth. Recovered structurally; the store was not traced (**LOW**). The legalizer's own comparator ordering (depth primary, benefit tie-break) is byte-confirmed and is unaffected.
- **The LowerToMlo per-op dynamic-legality lambda bodies** (the 2× `setLegalityCallback` / 2× `setOpAction` op names + predicates). The call census is recovered; the lambda bodies are not per-decompiled (**MEDIUM**). See the proposed follow-up on the [DMA bridge](lower-to-mlo-dma-bridge.md) page.
- **The materialization machinery** — `buildUnresolvedMaterialization`, the `builtin.unrealized_conversion_cast` bridge, `reconcileUnrealizedCasts`, and the 1:N `convertTypeImpl` dual cache. These belong to the rewriter/type-converter substrate; see [ConversionPatternRewriter](conversion-pattern-rewriter.md).
- **The `ConversionConfig` struct field offsets** (the by-value arg of `applyFullConversion`: `buildMaterializations`, `legalizableOps`/`unlegalizedOps` sets, `notifyCallback`, `allowPatternRollback`). Passed by value (≥0x40 bytes); only the `OpConversionMode` is pinned here.
- **The per-op rewrite bodies** — what each `tpu.*` lowers to is the subject of [tpu → LLO Lowering](tpu-to-llo-ods.md), [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md), and [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md), not this page.

---

## Cross-References

- [The TPU Compiler](overview.md) — the five-phase dialect descent this legalizer drives inside Phase 2a.
- [The tpu MLIR Dialect](tpu-dialect-and-ops.md) — the source dialect whose ops the legalizer ranks and applies patterns against.
- [tpu → LLO Lowering](tpu-to-llo-ods.md) — the LowerToLLO conversion target whose legality declarations Table 2 row 1 summarizes.
- [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md) — the `MloConversionTarget` legal-dialect set and the shared `populateTypeConverter`.
- [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md) — the SparseCore → LLVM target, the only heavy 1:N path, and the `markOpRecursivelyLegal` SCF set.
- [ConversionPatternRewriter](conversion-pattern-rewriter.md) — the shared `PatternApplicator` core, the `Pattern` vtable ABI, the rollback/rewrite-log substrate, and the 1:1 vs 1:N `ConversionPattern` slot dispatch.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / MLIR lowering chain — [back to index](../index.md)
