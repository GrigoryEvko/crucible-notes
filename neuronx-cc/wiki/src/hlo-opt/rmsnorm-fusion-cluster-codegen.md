# MulRedSqrt / RMSNorm Fusion and FusionCluster CodeGen

> *All addresses on this page apply to `hlo2penguin` from `neuronx_cc 2.24.5133.0+58f8de22` (cp310). Other versions and other Python ABIs (cp311/cp312) will differ. IDA VA is used throughout; for this binary `.rodata` VA = fileoff + 0x200000 and `.text` VA = fileoff + 0x201000 (NOT VA==fileoff — verify raw xxd with the offset applied).*

## Abstract

`fuseMulRedSqrt` is the third of the structural fusion sub-passes that `hilo::NeuronOpFusion::runOnOperation` (@0x2101680) runs over the MHLO body. It recognizes the *denominator core of an RMSNorm* — the `mhlo.multiply → mhlo.reduce → mhlo.sqrt` chain that computes `sqrt(reduce(x·x))` — and packs exactly those three ops into a single `mhlo.fusion` op tagged `FusionKind = "MulRedSqrt"`. The matcher is anchored on the **reduce** op (the pass walks for `mhlo::ReduceOp`), then probes one step down to the unique `SqrtOp` consumer and one step up to the `MulOp` producer, with single-use guards on both interior edges. This is the same family of pattern-match-then-cluster fusion as the dot/logistic and softmax legalizations, but here the matched window is deliberately *narrow*: the `+eps`, the reciprocal, the broadcast, and the final multiply-by-weight that complete a full RMSNorm all remain in the surrounding graph.

The mechanical work of turning a matched cluster into IR is **not** in the matcher. It lives in `hilo::FusionCluster`, a small RAII region-builder that *every* C-strand fusion shares (DotLogistic, Elementwise, SubExp, Expm1, Log1p, MulRedSqrt, ScheduleFusion, and their StableHLO twins — 14 ctor callers, 7 `codeGen` callers). The ctor (@0x20faa20) dedups the clustered ops into a `SetVector`, copies the kind tag string, and immediately runs the live-in/live-out boundary analysis. `codeGen` (@0x2104640) then reads those two boundary sets to build the fusion op: **operands = the remapped live-ins, result types = the live-out types, terminator operands = the live-out values**, body = the original cluster ops *moved* (not cloned) into the region, and a `FusionKind` inherent `StringAttr` stamped with the tag that came from the ctor. The fusion-kind discriminator is *purely* that one string; the codegen path itself is name-agnostic.

This page reconstructs both halves: the RMSNorm idiom matcher (`fuseMulRedSqrt` and its lambda) and the shared region-builder (`FusionCluster::codeGen` with its explicit live-range computation). It closes with the MHLO-moves-vs-StableHLO-clones divergence, the Penguin emission shape, and the crosswalk to the separate `AwsNeuronRmsNorm` whole-op lowering path.

For reimplementation, the contract is:

- **The match predicate.** Walk anchor = `ReduceOp`; the five predicates P0–P5 (not-already-fused, reduce single-use, consumer is `SqrtOp`, producer is `MulOp`, mul single-use, init captured) and the exact operand-slot offsets that read them.
- **The `FusionCluster` struct layout** and the two boundary algorithms — defined-outside-used-inside ⇒ live-in, defined-inside-used-outside ⇒ live-out — over a `DenseSet` membership oracle.
- **The `codeGen` build sequence**: FusedLoc, the `updateInputs` inter-fusion operand legalization, FusionOp build, body Block + BlockArguments, the two complementary `replaceUsesWithIf` predicates, the `mhlo.return` terminator, the `FusionKind` attr, and the `recursivelyMoveDependentOps` dominance legalization.

| | |
|---|---|
| **Pass driver** | `hilo::NeuronOpFusion::fuseMulRedSqrt(func::FuncOp)` @0x20ff510 (520 B) |
| **Matcher lambda** | `…{lambda(mhlo::ReduceOp)}::operator()` @0x20fb9a0 (648 B) |
| **Walk trampoline** | `function_ref::callback_fn<…ReduceOp…>` @0x20fbc30 (143 B) |
| **Cluster ctor** | `FusionCluster::FusionCluster(ArrayRef<Operation*>, StringRef)` @0x20faa20 (989 B) |
| **MHLO region builder** | `FusionCluster::codeGen(DominanceInfo*, PostDominanceInfo*)` @0x2104640 (3008 B) |
| **StableHLO builder** | `FusionCluster::stableHLOCodeGen(…)` @0x21053a0 (6103 B) |
| **Live-range** | `populateLiveInLiveOuts` @0x2103850 → `populateLiveIns` @0x2103760 / `populateLiveOuts` @0x2103680 |
| **Kind tag string** | `"MulRedSqrt"` @VA 0x2560e7 (len 0xA); attr key `"FusionKind"` @0x266931 |
| **IR level** | MHLO (post-ingestion, pre-Penguin); StableHLO twin emits `stablehlo.composite` |

---

## fuseMulRedSqrt — the RMSNorm idiom matcher

### Purpose

Detect the RMS denominator subgraph `sqrt(Σ x²)` and box it into one op so the Penguin back-half can route it onto a fused RMSNorm kernel. The recognized window is three ops — `mul`, `reduce`, `sqrt` — and nothing more. The reciprocal (`1/…`), the `+eps`, the broadcast, and the scale-by-weight that finish a true RMSNorm are intentionally left in the graph; the `MulRedSqrt` marker is a *hint*, not the whole normalization.

> **GOTCHA — the matched consumer is `sqrt`, not `rsqrt`.** The idiom is widely written `mul → reduce → rsqrt`, because that is the shape a hand-written RMSNorm takes. This pass matches `mhlo::SqrtOp` (TypeID @`0x9d304b0`); there is no reciprocal anywhere in the fused cluster. A matcher keyed on `rsqrt` never fires.

### Entry Point

```text
hilo::NeuronOpFusion::runOnOperation (@0x2101680)   ── disasm-verified call order
  ├─ fuseSubExp                              ── ALWAYS first (unconditional, call @0x2101696)
  ├─ if clopts::fuseDotLogistic (byte @0x9c717b8):
  │    ├─ fuseDotLogisticOp                  ── call @0x21016de
  │    └─ fuseMulRedSqrt (@0x20ff510)        ── call @0x21016f2 — THIS pass, runs right after DotLogistic
  │         ├─ AnalysisManager::getAnalysis<DominanceInfo>   (@0x20ff567)
  │         ├─ Operation::walk(callback_fn<ReduceOp>)         (@0x20ff59f)
  │         │     └─ trampoline @0x20fbc30  ── filter op==ReduceOp + "already-fused" guard
  │         │            └─ matcher @0x20fb9a0  ── P0..P5 + FusionCluster build
  │         ├─ for each cluster: FusionCluster::codeGen(dom, /*postdom=*/null)  (@0x20ff5c9)
  │         └─ for each cluster: operator delete(0xD8)         ── destroy
  └─ if clopts::generalElementwiseFusion (byte @0x9c716f8):
       ├─ fuseExpm1Op                        ── call @0x21016b3
       ├─ fuseLog1pOp                         ── call @0x21016be
       └─ fuseElementwiseOps                  ── tail-jmp @0x210170b
```
The call order above is the one `runOnOperation` @`0x2101680` actually executes, and it is
worth reading carefully: `fuseSubExp` goes first and unconditionally (@`0x2101696`);
`fuseMulRedSqrt` runs immediately after `fuseDotLogisticOp` (@`0x21016f2`), sharing its
`fuseDotLogistic` gate; and `fuseElementwiseOps` is the tail call (@`0x210170b`, under
`generalElementwiseFusion`). See [op-fusion](op-fusion-dot-elementwise.md) for the sibling
passes.

> **NOTE —** the matched window is anchored on the *reduce*, not the mul or the sqrt. The pass `walk`s for `mhlo::ReduceOp`; from each reduce it probes *down* to the sqrt consumer and *up* to the mul producer. Anchoring on the reduce is deliberate: it is the one op in the chain whose identity (a reduction) most narrows the search, and it sits in the middle so both directions are a single hop.

### Algorithm

```c
// Walk trampoline @0x20fbc30 — runs for every op the walk visits.
function callback_fn_ReduceOp(op):
    if op.typeID() != TypeID<mhlo::ReduceOp>:        // 0x20fbc4x — wrong op kind
        return WalkContinue
    parentOp = op.block().getParentOp()              // 0x20fbc74
    if parentOp != null && parentOp.typeID() == TypeID<mhlo::FusionOp>:  // @0x9d30678
        return WalkContinue                          // P0: already inside a fusion → skip
    return matcher(op)                               // tail-call @0x20fb9a0

// Matcher @0x20fb9a0 — the five predicates, then cluster build.
function matchMulRedSqrt(reduce):                    // mlir::mhlo::ReduceOp
    // P0 (again, matcher side, 0x20fb9de): enclosing region must be untyped (func body),
    //     i.e. parentOp.typeID() == TypeID<void> (@0x9d3fb48). Else reject.
    if enclosingRegionTypeID(reduce) != TypeID<void>:
        return WalkContinue

    // P1: reduce is single-use (0x20fba00). getNumUsers @0x21bfee0 counts ResultRange uses.
    if getNumUsers(reduce) > 1:                       // cmp eax,1 ; ja reject
        return WalkContinue

    // P2: the unique consumer is mhlo::SqrtOp (0x20fba0d..54).
    if reduce.getNumResults() == 0:                  // [reduce+0x24]
        return WalkContinue
    sqrt = reduce.result(0).use_begin().owner()      // ResultRange::use_begin @0x20fba2c → first user
    tid  = sqrt.typeID()                             // [sqrt+0x30]->[+0x10]
    if tid == TypeID<void> || tid != TypeID<mhlo::SqrtOp>:   // @0x9d304b0
        return WalkContinue                          // reduce -> sqrt required

    // P3: the reduced operand's producer is mhlo::MulOp (0x20fba56..91).
    mul = reduce.operand(0).getDefiningOp()          // [reduce+0x48]->[+0x18] ; getDefiningOp @0x20fba66
    if mul == null: return WalkContinue
    tid = mul.typeID()                               // [mul+0x30]->[+0x10]
    if tid == TypeID<void> || tid != TypeID<mhlo::MulOp>:    // @0x9d305f8
        return WalkContinue                          // mul -> reduce required

    // P4: the mul is single-use (0x20fba97). Keeps the chain linear.
    if getNumUsers(mul) > 1:
        return WalkContinue

    // P5: capture reduce.operand(1) = the reduction init/identity (0x20fbaa8).
    //     [reduce+0x48]->[+0x38] is operand-index 1 (4 operand-slots past operand(0) at +0x18).
    //     Its defining op is fetched but its TypeID is NOT checked — captured for liveness/context only.
    init = reduce.operand(1)                          // mean/sum identity; getDefiningOp called, type unverified
    ctx  = init.getContext()                          // Attribute::getContext @0x20fbac1

    // Full match — build the 3-op cluster (0x20fbace..b0f).
    ops = { mul, reduce, sqrt }                       // var_70 / var_68 / var_60 (stack array of 3)
    cluster = new FusionCluster(0xD8)                 // operator new(0xD8)
    FusionCluster::FusionCluster(cluster, ArrayRef<Operation*>{ops, 3}, StringRef{"MulRedSqrt", 0xA})
    driver.clusters.push_back(unique_ptr(cluster))   // SmallVector<…,100> ; inline cap 100 (0x6400000000)
    return WalkContinue
```

The matched subgraph, in MHLO:

```text
%sq    = mhlo.multiply(%x, %x)               // x·x   — single-use (P4), feeds only the reduce
%ms    = mhlo.reduce(%sq, %init) {add}       // Σ x²  along reduce_dims — single-use (P1)
%denom = mhlo.sqrt(%ms)                       // sqrt(Σ x²) = the RMS / L2 denominator (P2 consumer)
```

> **GOTCHA —** the reduction *region body* opcode is never checked. P5 captures `operand(1)` (the init value) but the matcher does not verify the reduce computes `kAdd` (sum/mean) versus `max` or any other reduction. Disambiguation rests entirely on the `sqrt(mul(x,x))` envelope plus what the downstream consumer does with the result. A reimplementer who fuses on the shape alone matches exactly what this binary matches; one who additionally insists on an add-reduction body is *stricter* than the real pass. No region-opcode check exists in @0x20fb9a0. (LOW-confidence that none exists downstream either.)

> **QUIRK —** P5 reads `operand(1)` at struct offset `[reduce+0x48]->[+0x38]`, exactly four OpOperand slots (4 × 0x20 + 0x18) past `operand(0)` at `+0x18`. Both the `+0x18` (operand Value within an OpOperand) and the `0x20` stride are the standard MLIR `OpOperand` layout; reading them by raw offset rather than through `getOperand(i)` is how the matcher was compiled, and the offsets are how we confirm *which* operand is captured.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `NeuronOpFusion::fuseMulRedSqrt(FuncOp)` | 0x20ff510 | 520 | Driver: getAnalysis → walk → per-cluster codeGen → destroy | CERTAIN |
| matcher `…{lambda(ReduceOp)}::operator()` | 0x20fb9a0 | 648 | P0–P5 + FusionCluster build | CERTAIN |
| walk trampoline `callback_fn<ReduceOp>` | 0x20fbc30 | 143 | op-kind filter + already-fused guard → tail-call matcher | CERTAIN |
| `hilo::getNumUsers(Operation*)` | 0x21bfee0 | 222 | use-count over `ResultRange` (use_begin..use_end) | CERTAIN |
| `StableHLONeuronOpFusion::fuseMulRedSqrt` | 0x2139390 | — | StableHLO twin (matcher lambda @0x2135070, `stablehlo::Mul/Reduce/Sqrt`) | CERTAIN |
| `…::fuseMulRedSqrtCounter` (static) | 0x9c70580 | — | StableHLO-only counter; ctor `_GLOBAL__sub_I…` @0x2139880 zero-inits it | HIGH |
| `MhloToPythonPrinter::printMulRedSqrtFusionOp` | 0x20f1f60 | 4032 | MHLO Penguin emitter | CERTAIN |
| `StableHLOToPythonPrinter::printMulRedSqrtCompositeOp` | 0x2191450 | 4187 | StableHLO Penguin emitter | CERTAIN |

> **NOTE — only the StableHLO twin has a counter.** `fuseMulRedSqrtCounter` exists as `hilo::StableHLONeuronOpFusion::fuseMulRedSqrtCounter` @`0x9c70580`. The MHLO `NeuronOpFusion::fuseMulRedSqrt` driver has no named counter symbol and emits no diagnostic at all — it is a pure IR rewrite. Even on the StableHLO side the counter's use-site was not located; its constructor only zero-initializes it.

---

## FusionCluster — the shared region builder

### Purpose

`FusionCluster` is the one piece of code that turns *any* matched cluster into a fusion op. The matcher's only job is to hand it an `ArrayRef<Operation*>` and a kind-tag `StringRef`; everything structural — boundary analysis, operand legalization, region construction, BlockArgument remapping, dominance repair — is in here and is **fusion-kind-agnostic**. The kind tag is consumed in exactly one place: the `FusionKind` (MHLO) / `CompositeKind` (StableHLO) inherent `StringAttr`.

The source path string `"hilo/MLIRPasses/ADT/FusionCluster.cc"` (@VA 0x318f60, xxd-confirmed) marks this as an ADT helper, consistent with its RAII/shared character.

### Struct layout

Reconstructed from the ctor @0x20faa20 and cross-checked against the populate/codeGen reads. All offsets CERTAIN unless noted.

| Offset | Type | Meaning |
|---|---|---|
| `+0x00` | `std::string` (SSO buf @+0x10) | **FusionKind tag** — copied from the ctor `StringRef` (`"MulRedSqrt"`, `"DotLogistic"`, `"Elementwise"`, `"ScheduleFusion"`) |
| `+0x20` | `DenseSet` bucket-ptr | **clustered-op membership set** (the O(1) "is X in the cluster" oracle) |
| `+0x28` | u32 | set #entries |
| `+0x2C` | u32 | set #tombstones |
| `+0x30` | u32 | set #buckets (mask base) |
| `+0x38` | `Op**` | clustered-op **vector** data-ptr (ordered iteration list) |
| `+0x40` | u32 | vector size |
| `+0x44` | u32 | vector capacity |
| `+0x48 .. 0x90` | `SetVector<Value, SmallVector<Value,4>, DenseSet, 4>` | **LIVE-INS** — +0x48 DenseSet, +0x60 vec-data→+0x70 inline(4), +0x68 packed size\|cap (init 0x400000000) |
| `+0x90 .. 0xB8` | `SetVector<Value, SmallVector<Value,4>, DenseSet, 4>` | **LIVE-OUTS** — +0x90 DenseSet, +0xA8 vec-data→+0xB8 inline(4), +0xB0 size, +0xB4 cap |

The membership probe used by both populate functions is the `DenseSet` at `+0x20`/`+0x30`: hash `((h>>4) ^ (h>>9)) & (#buckets-1)`, with the standard MLIR sentinels `0xFFFFFFFFFFFFF000` (empty) and `0xFFFFFFFFFFFFE000` (tombstone). The clustered-op *vector* (`+0x38`) gives ordered iteration; the *set* (`+0x20`) answers membership.

Ctor flow: copy the tag string via `std::string::_M_construct`; zero the three containers; for each `Operation*` in the `ArrayRef`, insert into the `SetVector` (DenseSet dedup + vector push, growing via `DenseSet::grow` / `SmallVectorBase::grow_pod`); finally tail-call `populateLiveInLiveOuts()` @0x20facae.

### Live-range algorithm

`populateLiveInLiveOuts()` @0x2103850 iterates the clustered-op vector (`+0x38`, size `+0x40`) **from end-1 down to begin** (reverse), calling `populateLiveIns(op)` then `populateLiveOuts(op)` for each.

```c
// LIVE-INS (operand side) — populateLiveIns(op) @0x2103760.  CERTAIN
// Rule: a value DEFINED OUTSIDE the cluster but USED INSIDE is a live-in.
function populateLiveIns(op):
    if (op.flags[0x2E] & 0x80) == 0:            // op has no inline operand storage → nothing to scan
        return
    n    = op.numOperands                        // [op+0x44]
    base = op.operandStorage                     // [op+0x48] ; OpOperand stride 0x20, Value at +0x18
    for i in [0, n):
        v     = base[i*0x20 + 0x18]              // i-th operand Value
        defOp = v.getDefiningOp()                // null for a BlockArgument
        if defOp == null:                        // region-external block-arg
            liveIns.insert(v)                    // this+0x48 (SetVector dedups)
        else if defOp NOT in clusterSet(this+0x20):   // defined outside the cluster
            liveIns.insert(v)
        // else defOp in cluster → internal edge, not a boundary value

// LIVE-OUTS (result side) — populateLiveOuts(op) @0x2103680.  CERTAIN
// Rule: a value DEFINED INSIDE the cluster but USED OUTSIDE is a live-out.
function populateLiveOuts(op):
    m = op.numResults                            // [op+0x24]
    for r in [0, m):
        res = OpResultImpl::getNextResultAtOffset(op, r)   // r-th OpResult Value
        for use in res.uses():                   // use-list: first @[res], next @[use], owner @[use+0x10]
            userOp = use.owner                   // [use+0x10]
            if userOp NOT in clusterSet(this+0x20):       // a consumer outside the cluster
                liveOuts.insert(res)             // this+0x90 ; insert once
                break
```

After `populateLiveInLiveOuts`, `this+0x48` holds the boundary inputs and `this+0x90` holds the boundary outputs — the future fusion operands and results, modulo the input remap in `updateInputs` (below). For the three-op MulRedSqrt cluster the live-ins are `%x` (and the captured `%init`) and the single live-out is `%denom` (the sqrt result) — which is why `codeGen` builds a fusion with one result type.

> **NOTE —** the same `DenseSet` at `this+0x20` is the membership oracle for *both* directions. A live-in is "the operand's defining op is not in this set (or is a block-arg)"; a live-out is "some user's op is not in this set". The reverse iteration order over the cluster vector does not change the result sets (a `SetVector` is order-insensitive for membership), but it keeps the live-out insertion order aligned with the op order the terminator will later return.

### codeGen — MHLO build sequence

`codeGen(DominanceInfo*, PostDominanceInfo*)` @0x2104640 materializes `mhlo.fusion`. The MulRedSqrt driver calls it with `postDom = null` (`xor edx,edx` @0x20ff5c4) — only `DominanceInfo` is computed. Step-by-step (block addresses in parens):

```c
function codeGen(this, domInfo, postDomInfo):           // @0x2104640
    // 4a. FusedLoc (0x210465b..72a)
    locs = []                                           // SmallVector<Location,5> (init cap 5)
    for op in this.clusteredOps (reverse):              // +0x38 / +0x40
        locs.push(op.loc)                               // [op+0x18]
    ctx     = locs[0].getContext()                      // Attribute::getContext
    fusedLoc = FusedLoc::get(locs, /*metadata=*/null, ctx)   // @0x9b4b060

    // 4b. Live-in remap — inter-fusion operand legalization (0x2104731..78b)
    operands = updateInputs(copy(this.liveIns), /*cloneMap=*/null)   // @0x2102630 ; null for MHLO

    // 4c. Result types from live-outs (0x2104790..7fc, 0x2104826..49)
    outVals  = this.liveOuts.values()                   // SmallVector<Value,6> (terminator operands)
    outTypes = [ v.getType() for v in outVals ]         // SmallVector<Type,1> ; type ptr = ([v+8] & ~7)

    // 4d. FusionOp::build (0x2104849..8be)
    builder.setInsertionPointBefore(this.anchorOp)      // r14 = last/anchor clustered op
    fusionOp = builder.create<mhlo::FusionOp>(fusedLoc, outTypes, operands)   // @0x2101c10 → build @0x8f9bcb0
        // name "mhlo.fusion" @0x23a789 ; report_fatal_error @0x2df9e8 if dialect unloaded

    // 4e. Body Region + Block + BlockArguments (0x21048c5..adf)
    block = new mlir::Block(0x48)
    fusionOp.region(0).push_back(block)                 // ilist splice
    for op in this.clusteredOps:                        // +0x38 / +0x40
        op.moveBefore(block, block.end())               // @0x9b59140 — MOVE, not clone
    block.addArguments(operandTypes, operandLocs)       // @0x9ae4760 — one BlockArg per operand

    // 4f. Operand → BlockArgument remap, inside the body (0x2104af7..b7c)
    for i in [0, fusionOp.numOperands):                 // [fusionOp+0x44]
        arg = block.argument(i)                         // [...+i*0x20+0x18]
        fusionOp.operand(i).replaceUsesWithIf(arg, lambda_inBody)   // @0x9b76210
        // lambda_inBody @0x2101720 (setz): keep use IFF use.owner.block == body block

    // 4g. mhlo.return terminator (0x2104b7c..c44)
    ret = builder.create<mhlo::ReturnOp>(outVals)       // build @0x8f81de0 ; name "mhlo.return" @0x21a259
    block.push_back(ret)                                // terminator operands = live-out VALUES

    // 4h. FusionKind attribute (0x2104c44..da2)
    key = StringAttr::get(ctx, "FusionKind")            // @0x266931
    val = StringAttr::get(ctx, this.kindTag)            // this+0x00, e.g. "MulRedSqrt"
    fusionOp.setInherentAttr(key, val)                  // through op's DictionaryAttr [op+0x38]

    // 4i. Live-out result rewire (0x2104c5c..fc0)
    for j, res in enumerate(fusionOp.results):
        outVals[j].replaceUsesWithIf(res, lambda_outOfBody)
        // lambda_outOfBody @0x2101730 (setnz): keep use IFF use.owner.block != body block

    // 4j. Dominance legalization (0x2104da2)
    recursivelyMoveDependentOps(fusionOp, domInfo, postDomInfo)     // @0x2103a10 ; capped, FATAL-on-loop

    // 4k. Cleanup (0x2104dbc..e6d) — free 7 stack SmallVectors; cluster ops are NOT erased here.
```

> **QUIRK —** the two `replaceUsesWithIf` predicates are exact complements and *are* the BlockArgument mapping. `lambda_inBody` @0x2101720 is a `setz` (keep uses whose owner block **is** the body block) — it rewires interior references of an outer operand to the matching block argument. `lambda_outOfBody` @0x2101730 is a `setnz` (keep uses whose owner block **is not** the body block) — it rewires *external* consumers of a now-internal value to the FusionOp's result. One pass each; together they re-thread every edge that crossed the new region boundary.

> **GOTCHA —** the FusionOp's operands are **not** the raw live-ins. `updateInputs` @0x2102630 legalizes each live-in: if its defining op lives inside a *previously-built* fusion (TypeID `mhlo::FusionOp`) or composite (`stablehlo::CompositeOp`), the operand is replaced by that prior op's **external result** (found by locating the value in the prior op's `mhlo.return` via `getOperationReturnOp` @0x21c2f50 and taking the matching result). Skip this and a second fusion will take an SSA value that no longer dominates — the IR is now inside another region. This is operand legalization, not mere dedup.

> **NOTE —** in the MHLO path the cluster ops are **moved** into the body (`Operation::moveBefore`), so the originals *are* the body and no clone map is needed (`updateInputs` is passed `null`). The terminating `erase` of the now-dead matched ops is done by the calling `fuse*` lambda, not by `codeGen`. The result-type list is `SmallVector<Type,1>` and the operand list is `SmallVector<Value,6>` — i.e. the build template bakes in "≤1 result, ≤6 operands", consistent with a single-output normalization denominator.

### recursivelyMoveDependentOps — dominance repair

Splicing a window of ops into one fusion can leave ops that previously interleaved with the cluster violating dominance or post-dominance. `hilo::(anonymous namespace)::recursivelyMoveDependentOps(anchor, dom, postDom)` @0x2103a10 (internal linkage) walks result-uses (`ResultRange::UseIterator`), tests `DominanceInfo::properlyDominates` / `PostDominanceInfo::properlyPostDominates`, and `moveBefore`/`moveAfter`s offenders to restore a legal schedule. Iteration is **capped**; on overflow it logs through `NeuronLogger` + `tsl::LogMessage` and `report_fatal_error`s:

```text
"Maximum iterations reached while moving dependent ops. Infinite loop likely"   (@0x2b1b50)
```

For MulRedSqrt, `postDom` is `null`, so only forward-dominance moves are exercised. CERTAIN.

---

## MHLO fusion vs StableHLO composite

The newer pipeline runs the StableHLO twin `StableHLONeuronOpFusion::fuseMulRedSqrt` (@0x2139390, matcher @0x2135070, matching `stablehlo::MulOp/ReduceOp/SqrtOp`) and emits through `FusionCluster::stableHLOCodeGen` @0x21053a0 instead of `codeGen`. The two builders share the same boundary sets, the same `updateInputs`, the same `recursivelyMoveDependentOps`, the same FusedLoc, and the same two `replaceUsesWithIf` predicates (StableHLO twins @0x2101740/0x2101750). They diverge in three ways:

| Axis | MHLO `codeGen` | StableHLO `stableHLOCodeGen` |
|---|---|---|
| Op built | `mhlo.fusion` (@0x23a789) with **inline body** | `stablehlo.composite` (no inline body) + a separate `func.func` decomposition |
| Body handling | cluster ops **moved** into the region | cluster ops **cloned** (via `DenseMap<Op*,Op*>` clone map + `IRMapping`-style lookup) into a private `func.func` |
| Kind discriminator | inherent `FusionKind` StringAttr (@0x266931) | inherent `CompositeKind` (@0x21a28c) + `composite.name` (@0x27713d) + `composite.attributes` + `version` uint + `decomposition` symbol-ref |

The decomposition function is emitted with a generated symbol name: prefix `"genComposite"` (@0x256067) + a decimal counter (itoa fast-path via the two-digit table @0x40a580), under the `"hilo."` namespace (@0x23e50b). It is marked `Private` visibility and annotated `"noDelete"` (@0x27eef7) to prevent symbol-DCE, terminated by `func.return` (@0x226311). The `decompNames` `DenseMap<ulong,string>` dedups decomposition names across multiple composites in one module. `func`/`func.func` dialect availability is checked via `MLIRContext::getOrLoadDialect("func", …)` @0x9b50190 — a load the MHLO path never performs.

> **NOTE —** the seam in one line. MHLO = inline `mhlo.fusion { <moved cluster ops>; mhlo.return } [FusionKind="MulRedSqrt"]`. StableHLO = `stablehlo.composite [name="MulRedSqrt", version, composite.attributes]` with `decomposition = @genComposite<N>`, and a separate private `func.func @genComposite<N> { <cloned cluster ops>; func.return }`.

The StableHLO `FusionToComposite` conversion (`0x2096d30`) is what wires the kind into the composite's `name`. The authoritative roster of composite kinds is the printer's dispatch error:

```text
0x2e0258: "Unexpected CompositeOp kind '%s'. Supported kinds are:
           ScheduleFusion, MulRedSqrt, DotLogistic, Expm1, Log1p, Elementwise, and DotSoftmax."
```

> **QUIRK —** that roster is **7** kinds, not the 6 `fuse*` sub-passes the driver runs. Two surprises: `Elementwise` and `DotSoftmax` appear as composite kinds, and `DotSoftmax` has *no* `fuse*` sub-pass in the driver loop above — it is a separately-recognized composite. Conversely `fuseSubExp` runs but produces no listed kind. The composite-kind set is **not** the same as the fusion-sub-pass set; do not assume a 1:1 mapping.

---

## Penguin emission

Both emitters — `printMulRedSqrtFusionOp` @0x20f1f60 (MHLO) and `printMulRedSqrtCompositeOp` @0x2191450 (StableHLO) — print the same Penguin form: a `NeuronTensorOp` call carrying `op="MulRedSqrt"` and a `reduce_dims` list. The op label printed is `"mhlo.fusion"` (@0x23a789).

| Emitted token | String @VA | Meaning |
|---|---|---|
| `.NeuronTensorOp(` | 0x25e508 | Penguin IR ctor for the fused tensor op |
| `op="MulRedSqrt"` | key 0x2327b0 / val 0x2560e7 | the composite kind, emitted as the `op=` attribute |
| `xla_op` | 0x256085 | XLA op marker (second attribute key) |
| `reduce_dims` | 0x22a652 | the reduce dimensions list |
| `hilo_fusion_op` | 0x252356 | Penguin import/helper module for the fused op |

`reduce_dims` is read from the inner reduce — MHLO via `mhlo::ReduceOp::getDimensions()` @0x8F807B0, StableHLO via `stablehlo::ReduceOp::getDimensions()` @0x91345E0 — with the dimension integers iterated through `DenseElementsAttr::IntElementIterator` (@0x9AF1030, deref @0x9AF11B0). The StableHLO emitter must first walk *into* the composite to reach the inner reduce: `stablehlo::CompositeOp::getDecomposition()` @0x912ACA0 + `SymbolTable::lookupSymbolIn` @0x9B6ED30, with `getCompositeReturnOp` @0x21bffe0 resolving the decomposition's terminator.

```python
# Reconstructed Penguin emission (from the string/callee sequence).
<dst> = <ctx>.NeuronTensorOp(<src0>, <src1>, …,
                             op="MulRedSqrt",
                             reduce_dims=[<d0>, …],
                             xla_op=<…>)
```

---

## RMSNorm lowering: two distinct paths

`MulRedSqrt` is *not* the only way an RMSNorm reaches the Neuron backend. There is a second, whole-op path through the `AwsNeuronRmsNorm` custom-call. The two converge on the same nkilib kernel family but enter very differently:

| Path | Trigger | hlo2penguin op | Emitter | Captures |
|---|---|---|---|---|
| **MulRedSqrt** (this page) | structural detection of `sqrt(reduce(mul(x,x)))` | `mhlo.fusion` `FusionKind="MulRedSqrt"` / `stablehlo.composite` `name="MulRedSqrt"` | `printMulRedSqrt{Fusion,Composite}Op` → `NeuronTensorOp(op="MulRedSqrt")` | **only** the RMS denominator; eps/weight stay in the graph |
| **AwsNeuronRmsNorm** | a whole-op custom-call `AwsNeuronRmsNorm` (@0x27ad8d) / `mhlo.rms_norm` (@0x2368bc) emitted upstream | `mhlo.RmsNorm` op | `MhloToPythonPrinter::printRmsNorm(Operation*, bool)` @0x20e1a80 (StableHLO @0x217ddc0) | **full** RMSNorm; carries explicit epsilon + weight |

Diagnostics that belong to the `AwsNeuronRmsNorm` path (and confirm the two are separate):

```text
0x2a6b08: "'%s' op RmsNorm: epsilon is not a scalar. Expected a single scalar
           constant value for epsilon parameter."
0x2b1e30: "Operation AwsNeuronRmsNorm encountered size mismatch along the specified
           (normalization) dimension %d. …"
```

A matching backward op exists: `AwsNeuronRmsNormBackward` (@0x2628ab) / `mhlo.rms_norm_backward` (@0x22638a) → `printRmsNorm(…, /*backward=*/true)`.

> **NOTE —** the `NeuronTensorOp(op="MulRedSqrt")` marker and the `AwsNeuronRmsNorm` marker are both lowered, downstream in the Penguin middle-end / Walrus backend, onto the nkilib RMSNorm kernels under `neuronxcc/nki/_pre_prod_kernels/rms_norm/` (`rmsnorm_quant*.py`) and `…/rmsnorm_tkg.py`. The op→kernel binding and the back-half reconstruction of `+eps`, the reciprocal, and the weight broadcast from the `MulRedSqrt` hint are a Penguin/Walrus concern, not traced here. See the RMSNorm kernel page.

---

## Evidence summary and limits of this reading

This binary ships without decompiled bodies, so every structure on this page is transcribed
from disassembly plus TypeID and string anchors rather than from reconstructed C.

Four claims are pinned at specific instructions. The matched consumer is `SqrtOp`: predicate
P2 (@`0x20fba0d`) compares the consumer TypeID against `TypeID<mhlo::SqrtOp>` @`0x9d304b0`,
so the cluster is exactly three ops. The walk anchor is `ReduceOp`: the trampoline
@`0x20fbc30` filters on `TypeID<mhlo::ReduceOp>` before tail-calling the matcher, which then
probes down to sqrt and up to mul. `codeGen`'s operand/result/terminator contract —
`updateInputs`-remapped live-ins, live-out types, live-out values — sits in build steps
4b/4c/4d/4g, anchored to `updateInputs` @`0x2102630` and `mhlo::FusionOp::build`
@`0x8f9bcb0`, with the remap being a genuine legalization through `getOperationReturnOp`
@`0x21c2f50` rather than a dedup. And the live-in / live-out definitions are literally what
`populateLiveIns` @`0x2103760` and `populateLiveOuts` @`0x2103680` compute over the
`DenseSet` at `this+0x20`.

`FusionKind` being the sole kind discriminator is equally direct — step 4h stamps
`StringAttr("FusionKind", this.kindTag)` from the constructor's `StringRef`, and the
composite roster @`0x2e0258` lists MulRedSqrt among seven kinds — subject to the caveat
above that the composite-kind set is not the same as the `fuse*`-pass set.

Four things stay open. The reduce region's opcode is never checked: P5 captures `operand(1)`
without verifying `kAdd`, and no downstream check was found either. The BlockArgument array
read in step 4f, `[fusionOp+0x48 + i*0x20 + 0x18]`, has a stride that matches
`BlockArgument`, but whether the base is the argument array or an operand impl is not
settled — the semantics are certain, the offset less so. `getCompositeReturnOp` @`0x21bffe0`
is taken to be the `func.return`-walking twin of `getOperationReturnOp` without a
line-by-line transcription. And the StableHLO `fuseMulRedSqrtCounter` use-site — what it
counts and where it is logged — was never located.

---

## Related Components

| Name | Relationship |
|---|---|
| `fuseDotLogisticOp` / `fuseElementwiseOps` / `fuseSubExp` / `fuseExpm1Op` / `fuseLog1pOp` | sibling `fuse*` sub-passes driven by the same `runOnOperation`; all call `FusionCluster::codeGen` |
| `ScheduleFusion` | non-`NeuronOpFusion` caller of `FusionCluster::codeGen` (7th codeGen caller) |
| `FusionToComposite` (@0x2096d30) | converts `mhlo.fusion` → `stablehlo.composite`, wiring the kind tag into `composite.name` |
| `printRmsNorm` (@0x20e1a80) | emitter for the *other* RMSNorm path (`AwsNeuronRmsNorm` whole-op custom-call) |

## Cross-References

- [Neuron Op-Fusion — Dot, Elementwise, Transcendental Families](op-fusion-dot-elementwise.md) — 4.34, the `NeuronOpFusion::runOnOperation` six-sub-pass driver and the sibling fusions that share `FusionCluster::codeGen`
- [Softmax Legalization](softmax-legalize.md) — 4.27, the related pattern-match-and-fuse legalization; `DotSoftmax` is the 7th composite kind in the @0x2e0258 roster
- [HLO → Native / NKI Kernel Lowering](hlo-to-native-kernel-lowering.md) — how the `MulRedSqrt` / `AwsNeuronRmsNorm` markers reach the nkilib RMSNorm kernels (6.7.5)
