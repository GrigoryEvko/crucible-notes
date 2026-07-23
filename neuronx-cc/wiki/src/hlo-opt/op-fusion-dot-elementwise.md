# Neuron Op-Fusion — Dot, Elementwise, Transcendental Families

> *All addresses on this page apply to neuronx-cc `2.24.5133.0+58f8de22` (cp310), binary `neuronxcc/starfish/bin/hlo2penguin`. Other builds will differ. **VA ≠ file-offset on this binary:** for `.text` and `.rodata`, `file-offset = VA − 0x200000` (`.text` 0x1ec0d00 → 0x1cbfd00; verified via "Elementwise" VA 0x2669e3 → 0x669e3). The IDA JSON sidecars already carry VA; only raw `xxd`/`dd` needs the subtraction.*

## Abstract

`hilo::NeuronOpFusion` is a per-`func::FuncOp` MLIR pass (`runOnOperation` @0x2101680) that runs **six fusion sub-passes** over the mhlo dialect before the graph is handed to the Penguin (Tensorizer) backend. Each sub-pass recognizes a specific HLO idiom, packages the matched ops into a `hilo::FusionCluster`, and lowers that cluster through one shared codegen routine — `FusionCluster::codeGen` @0x2104640 — into an `mhlo.fusion` op carrying an inherent `FusionKind` StringAttr. That `mhlo.fusion` is the Penguin fused op: downstream, `MhloToPythonPrinter` dispatches on the `FusionKind` string to emit `neuronxcc.starfish.penguin` Python. This page reverse-engineers three of the six families: **fuseDotLogisticOp** (dot+logistic), **fuseElementwiseOps** (the `isElementwiseOp` / `growFusionOpUpwards` greedy upward grower), and the three **transcendental-idiom** sub-passes **fuseSubExp / fuseExpm1Op / fuseLog1pOp**.

If you know LLVM's pattern-rewrite drivers, the model is familiar with one twist: instead of a `RewritePattern` matcher table, each sub-pass is a hand-written matcher that walks the SSA def-use graph by `getDefiningOp`/`use_begin`, and the "rewrite" is not an in-place op replacement but the *encapsulation* of a connected subgraph into a region (`mhlo.fusion`). The cluster object (`FusionCluster`, 0xD8 bytes) is the unit of work: it owns the member-op set, the recomputed region live-ins, and the region live-outs, and `codeGen` turns those three sets into a fusion op with block arguments (= live-ins), a body (= members moved in), and an `mhlo.return` (= live-outs).

Two families fuse *existing* subgraphs without rewriting the math (dot-logistic, elementwise, log-sum-exp); two *expand* a precise primitive the hardware lacks into a tagged fusion that preserves provenance (`expm1`→`exp(x)−1`, `log1p`→`log(x+1)`). The page covers the match predicate, the cluster construction, the boundary rules, and the flag-gated ordering — enough to reimplement each sub-pass and the shared `codeGen` it feeds.

For reimplementation, the contract is:

- The `FusionCluster` data structure (0xD8-byte heap object) and its three SetVectors: members, live-ins, live-outs — this is what becomes a Penguin fused op.
- Each sub-pass's match predicate over the mhlo def-use graph, including the single-use / single-cluster-membership legality gates.
- `isElementwiseOp`'s op set, the structural-boundary op set, and the greedy upward `growFusionOpUpwards` worklist.
- The shared `codeGen` lowering: live-in/out computation → `mhlo.fusion` shell → body splice → `mhlo.return` → `FusionKind` attr stamp.
- The flag-gated pass ordering in `runOnOperation` and the two `cl::opt` value bytes that gate it.

| | |
|---|---|
| **Pass entry** | `hilo::NeuronOpFusion::runOnOperation` @0x2101680 (144 B) |
| **Sub-passes (this page)** | `fuseDotLogisticOp` @0x2100160, `fuseElementwiseOps` @0x20fcac0, `fuseSubExp` @0x20ff720, `fuseExpm1Op` @0x20ff1e0, `fuseLog1pOp` @0x20feeb0 |
| **Shared codegen** | `hilo::FusionCluster::codeGen(DominanceInfo*, PostDominanceInfo*)` @0x2104640 (3008 B) |
| **Cluster object** | `hilo::FusionCluster` — 0xD8 bytes; ctor @0x20faa20 |
| **Predicate** | `hilo::isElementwiseOp(Operation*)` @0x21c3e90 (95 B) |
| **IR level** | mhlo dialect, post-HLO-ingestion, pre-Penguin lowering |
| **Source file** | `hilo/MLIRPasses/Transforms/NeuronOpFusion.cc` (VA 0x2be288); cluster in `hilo/MLIRPasses/ADT/FusionCluster.cc` |
| **Fusion-kind tags emitted** | `"DotLogistic"`, `"Elementwise"`, `"MulRedSqrt"`, `"Expm1"`, `"Log1p"` |

---

## The Pass Driver — `runOnOperation` and Flag-Gated Ordering

### Purpose

`runOnOperation` @0x2101680 is the entry point. It calls the six sub-passes in a fixed but **conditionally-gated** order, driven by two `cl::opt<bool>` value bytes. Only one sub-pass (`fuseSubExp`) is unconditional; the rest hide behind opt-in flags.

### Algorithm

```c
function NeuronOpFusion_runOnOperation():            // 0x2101680
    func = getOperation()                            // func::FuncOp
    fuseSubExp(func)                                 // ALWAYS — correctness/stability transform
    if clopts::fuseDotLogistic.value:                // byte @0x9c717b8 (obj @0x9c71740 +0x78)
        fuseDotLogisticOp(func)
        fuseMulRedSqrt(func)                         // DotLogistic gate also runs MulRedSqrt
    if clopts::generalElementwiseFusion.value:       // byte @0x9c716f8 (obj @0x9c71680 +0x78)
        fuseExpm1Op(func)
        fuseLog1pOp(func)
        fuseElementwiseOps(func)                     // tail-jmp @0x210170b
```

> **NOTE —** `fuseSubExp` (the stable log-sum-exp) is **always-on**, never behind a flag. That placement is itself evidence it is a numerical-stability transform rather than a perf knob: the matched form is already the numerically-stable `max + log(Σ exp(x−max))`, and the pass exists to keep that tree glued together so a later pass cannot re-associate the `x−max` cancellation away from its `exp` and re-introduce overflow. (Pass-ordering CERTAIN from disasm @0x2101680; the exact flag→`fuseMulRedSqrt` coupling is HIGH.)

The `cl::opt` objects have stride 0xC0 with the bool value at +0x78 (two-point verified). The `generalElementwiseFusion` flag carries the key string `general-elementwise-fusion` / help "General elementwise-op fusion." Two related help strings survive verbatim in `.rodata` — `aEnableTheNeuronOpFusionFlagToProperlyFuseLog1popBefo` and `...FuseExpm1opBefo` — which ties the Log1p/Expm1 expanders to this flag.

> **GOTCHA —** because `fuseElementwiseOps` runs *after* `fuseExpm1Op`/`fuseLog1pOp` under the same flag, the `exp`/`log`/`sub`/`add` ops those expanders synthesize are themselves candidates for the elementwise grower — but they are already inside an `mhlo.fusion` region, and the elementwise seed-walk **skips ops whose parent is a `FusionOp`** (§ Elementwise, seed lambda). A reimplementation that re-seeds inside fusion regions will double-fuse.

---

## `fuseDotLogisticOp` — Dot + Logistic

### Purpose

`fuseDotLogisticOp` @0x2100160 (5405 B) recognizes a matmul whose result feeds a sigmoid — `dot → logistic [→ multiply]` — and wraps it as a `"DotLogistic"` fusion. The function body holds **two independent machineries** that run back-to-back over the same collected list of `LogisticOp`s:

- **Pass A — cluster collector:** matches `dot → logistic [→ mul]`, builds a `FusionCluster` named `"DotLogistic"`, and lowers it via `codeGen`.
- **Pass B — reshape reassociation:** matches `dot → reshape(2 users) → logistic → mul` and **reorders** it to `dot → logistic → mul → reshape`, sinking the reshape below the elementwise ops so they run on the un-reshaped dot output. Pass B builds *no* cluster and emits *no* composite — it is a pure SSA rewrite.

> **GOTCHA — one function, two unrelated machineries.** It is tempting to read `fuseDotLogisticOp` as a single "match `dot→logistic`, build cluster, emit composite" routine. It is not: Pass A clusters and emits a composite, Pass B reassociates SSA and emits nothing. The two share only the collected `LogisticOp` vector.

### Entry Point

```text
fuseDotLogisticOp (0x2100160)
  ├─ walk#1 collector (0x20f9bf0)      ── push every mhlo::LogisticOp into SmallVector<Op*,100>
  ├─ lambda#2 cluster-builder (0x20fc400, 1138 B)  ── PASS A: match dot→logistic[→mul]
  │    └─ FusionCluster::FusionCluster(ArrayRef<Op*>, "DotLogistic")  (0x20faa20)
  ├─ getAnalysis<DominanceInfo,FuncOp> (0x20fc8e0)
  ├─ FusionCluster::codeGen(dom, nullptr)  (0x2104640)   ── per cluster
  └─ PASS B inline matcher (loc 0x21002f1)  ── dot→reshape→logistic→mul reassociation
```

### Algorithm — Pass A cluster matcher (lambda @0x20fc400)

```c
function matchDotLogistic(LogisticOp L):             // 0x20fc400
    parent = L.getBlock().getParentOp()              // 0x20fc427
    if isa<mhlo::FusionOp>(parent): return           // already fused — anti-revisit

    dotDef = getDefiningOp(L.operand[0])             // 0x20fc47a (operand off +0x48, value +0x18)
    if isa<mhlo::DotGeneralOp>(dotDef):              // TypeID 0x9d306f8 — PREFERRED
        accept
    elif isa<mhlo::DotOp>(dotDef):                   // TypeID 0x9d306f0 — fallback (loc 0x20fc5df)
        accept
    else: return
    if hilo::getNumUsers(L) != 1: return             // 0x20fc4ae — logistic must be single-use

    u = *L.result.use_begin()                        // 0x20fc4d7
    userOp = u.getOwner()
    if !isa<mhlo::MulOp>(userOp):                    // TypeID 0x9d305f8
        cluster = {L, dotDef}                         // 2-OP arm (loc 0x20fc5fd), size 2
    else:
        mul = userOp                                  // count how many mul operands trace to the dot
        n_dot = count(i : getDefiningOp(mul.operand[i]) == dotDef)   // 0x20fc51c..
        if n_dot == 2: cluster = {mul, L, dotDef}     // 3-OP arm (loc 0x20fc718), size 3
        else:          cluster = {L, dotDef}          // 2-OP arm

    fc = new FusionCluster(cluster, "DotLogistic")    // operator new(0xD8); 0x20fc60f / 0x20fc735
    clusters.push_back(unique_ptr(fc))                // 0x20fc640 / 0x20fc766
```

> **QUIRK —** the producer is matched as `DotGeneralOp` *first* and `DotOp` only as a fallback. A reimplementation that keys off `mhlo.dot` will miss the common case — by the time op-fusion runs, the bulk of matmuls are `mhlo.dot_general`. (TypeID 0x9d306f8 preferred, 0x9d306f0 fallback — CERTAIN, both checked in disasm.)

> **NOTE —** the 3-op arm fires only when **both** operands of the multiply trace back to the same `dot` (`n_dot == 2`), i.e. the `dot²·σ(dot)`-style gating idiom where the multiply consumes the dot result on both sides. Otherwise the multiply is left outside the cluster and only `{logistic, dot}` is fused.

### Algorithm — Pass B reshape reassociation (inline, loc 0x21002f1)

```c
for L in collectedLogistics:                          // 0x21002f1
    r = getDefiningOp(L.operand[0])                    // 0x2100316
    if !isa<mhlo::ReshapeOp>(r): continue              // TypeID 0x9d30558
    if hilo::getNumUsers(r) != 2: continue             // 0x2100343 — reshape MUST have exactly 2 users
    dot = getDefiningOp(r.operand[0])
    if !(isa<DotGeneralOp>(dot) || isa<DotOp>(dot)): continue   // 0x2100399 / 0x2100550

    // scan L's users; find the MulOp; require exactly the expected user count consumed
    mul = null;  remaining = 2
    for use in L.result.uses:                          // 0x21003d1
        if use.getOwner() == L: { remaining--; continue }
        if isa<mhlo::MulOp>(use.getOwner()):           // 0x21004b4
            getNumUsers(use.getOwner())                // diagnostic side-effect
            mul = use.getOwner();  remaining--
    if remaining != 0: continue                        // 0x21007e0 — exact-count gate

    // REORDER: rebuild logistic→mul→reshape on the un-reshaped dot output
    newLog     = build mhlo::LogisticOp(dot.result, dot.resultType, mul.loc)   // 0x2100933
    newMul     = build mhlo::MulOp(newLog, ...)                                // 0x2100a8d
    newReshape = build mhlo::ReshapeOp(newMul)                                 // 0x2100bca
    mul.result.replaceAllUsesWith(newReshape)                                  // 0x2100c44
    erase mul   // src line 360                                                // 0x2100dc3
    erase L     // src line 367                                                // 0x2100f3a
    erase r     // src line 374                                                // 0x21010b1
    // the original dot is NOT erased
```

The three "Number of … users = " strings (`LogisticOp` @0x25e53f, `reshapeOp` @0x24662c, `MulOp` @0x2368d0) are diagnostic traces gated on the global logging flag `qword_9C71878`; only the `getNumUsers(reshape) == 2` test and the `remaining == 0` count actually gate the match.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `fuseDotLogisticOp` | 0x2100160 | 5405 | Driver: collect logistics, Pass A cluster + Pass B reassoc | CERTAIN |
| walk#1 collector lambda | 0x20f9bf0 | ~0x120 | Push every `LogisticOp` into `SmallVector<Op*,100>` | CERTAIN |
| Pass A cluster-builder lambda | 0x20fc400 | 1138 | Match `dot→logistic[→mul]`, build `"DotLogistic"` cluster | CERTAIN |
| `getAnalysis<DominanceInfo,FuncOp>` [clone] | 0x20fc8e0 | 472 | Obtain `DominanceInfo` for `codeGen` | CERTAIN |
| `hilo::getNumUsers(Operation*)` | 0x21bfee0 | 222 | Count uses of result#0 (`use_begin..use_end`) | CERTAIN |

---

## `fuseElementwiseOps` — Seeded Upward Cluster Grower

### Purpose

`fuseElementwiseOps` @0x20fcac0 (9198 B) is **not** a flat "walk ops, test `isElementwiseOp`" sweep. It is a **seeded, greedy, upward-growing** cluster builder. It seeds at `mhlo.reduce` or `mhlo.concatenate` ops, walks the SSA def-use graph **upward** (operand-ward), pulls in elementwise producers that satisfy a single-cluster-membership legality rule, topologically sorts the collected ops, and emits one `mhlo.fusion`. The same function body emits **two** fusion kinds, selected by the seed type:

| Seed op (walked anchor) | Emitted `FusionKind` |
|---|---|
| `mhlo.concatenate` or `mhlo.reshape` | `"Elementwise"` |
| `mhlo.reduce` | `"MulRedSqrt"` |

> **NOTE — `"MulRedSqrt"` is not exclusive to `fuseMulRedSqrt`.** The separate `fuseMulRedSqrt` @0x20ff510 is the obvious source of that kind string, but the reduce-anchored elementwise grower emits it too: the string at `0x2560e7` is set at `0x20fe4a4` inside *this* function. Both passes produce the same `FusionKind`.

### Entry Point

```text
fuseElementwiseOps (0x20fcac0)
  ├─ walk seed lambda (0x20fa080, 195 B)   ── seed {mhlo.reduce, mhlo.concatenate}
  ├─ growFusionOpUpwards (INLINED)         ── greedy upward worklist
  │    ├─ isElementwiseOp (0x21c3e90)       ── elementwise gate
  │    └─ all_of membership lambda (0x20fa6e0)  ── ∀ user(cand) ∈ cluster ?
  ├─ computeTopologicalSorting (0x20fd617) ── def-before-use ordering
  ├─ FusionCluster::FusionCluster(ArrayRef<Op*>, kind)  (0x20faa20)
  └─ FusionCluster::codeGen (0x2104640)
```

### Algorithm — seed walk (lambda @0x20fa080)

```c
function seedWalk(Operation* op):                     // 0x20fa080
    parent = op.getBlock().getParentOp()
    if isa<mhlo::FusionOp>(parent): return SKIP        // 0x20fa0b5 — never re-seed inside a fusion
    if isa<mhlo::ReduceOp>(op):       { seeds.push(op); return ADVANCE }   // 0x20fa0d8
    if isa<mhlo::ConcatenateOp>(op):  { seeds.push(op); return ADVANCE }   // 0x20fa110
    return ADVANCE                                     // not a seed
```

The seed *set* is exactly `{reduce, concatenate}`. Reshape is handled later as a **re-route**: a concatenate-path body whose first relevant producer is a reshape jumps to the concatenate code-path (0x20fe443), which is why reshape appears in the seed→kind table above.

### `isElementwiseOp` @0x21c3e90 — the predicate

The predicate reads op identity as `op->getName().getTypeID()` = `*(*(op+0x30)+0x10)` and compares against per-class `TypeIDResolver<OpClass,void>::id` symbols. It is a two-stage filter:

```c
function isElementwiseOp(Operation* op):               // 0x21c3e90
    tid = op.name.typeID                                // [[op+0x30]+0x10]
    if tid == TypeIDResolver<void,void>::id:           // 0x21c3ea4 — unregistered/null name
        goto isa_tail                                  // → false on the sentinel
    // (A) fast-path head: five mhlo bitwise/sign ops
    if tid in { AndOp, OrOp, NotOp, NegOp, XorOp }: return true   // 0x21c3eac..0x21c3eda
  isa_tail:                                            // 0x21c3ee8
    return llvm::isa<…~55 typeIDs…>(op)                // 4-link chained isa<> (mhlo + stablehlo)
```

The full elementwise set (CERTAIN, transcribed from the `isa<>` type list):

| Category | Ops (each exists for **both** `mhlo::` and `stablehlo::`) |
|---|---|
| Unary / transcendental | `cbrt, ceil, exp, erf, expm1, floor, log, logistic, log1p, sqrt, rsqrt, tan, tanh, negate` |
| Binary arith | `add, subtract, multiply, divide, maximum, minimum, power` |
| Bitwise / logical | `and, or, not, xor` |
| Conversion / reinterpret / shape | `convert, bitcast, bitcast_convert, reshape` |
| Comparison | `compare` |

> **QUIRK —** `reshape`, `bitcast`, `bitcast_convert`, `convert`, and `compare` are all in the elementwise set — pure data-movement and dtype casts are fusible. But `transpose`, `broadcast_in_dim`, `slice`, and `concatenate` are **deliberately absent**: the grower treats them as structural cluster *boundaries* (they stay as live-in producers). A reimplementation that lumps all "cheap" data-movement ops together will wrongly absorb a transpose into the elementwise body.

> **NOTE —** `isElementwiseOp` is **one** symbol shared by the MHLO and StableHLO pipelines — it handles both dialects. Only the *fusion drivers* fork into MHLO vs StableHLO twins; the predicate does not.

### Algorithm — `growFusionOpUpwards` (inlined greedy grower)

There is no standalone `growFusionOpUpwards` symbol — it is inlined — but its signature survives in the mangled lambda symbols: it takes one growing `SetVector<Operation*, SmallVector<Op*,0>, DenseSet<Op*>>` — a DenseSet for O(1) `contains` plus an ordered SmallVector for iteration.

```c
function growFusionOpUpwards(SetVector<Op*>& cluster): // inlined @0x20fcd4e..0x20fd0b8
    worklist = copy(cluster.members)
    while worklist not empty:
        curOp = worklist.pop()
        for k in [0, curOp.numOperands):               // operands @[curOp+0x48], count @[curOp+0x44]
            cand = curOp.operand[k].getDefiningOp()
            if cand == null: continue                  // block arg — never fusible, stays a live-in

            // (R2) STRUCTURAL BOUNDARY VET  (0x20fce80 / 0x20fda70)
            if cand in { transpose, fusion, concatenate, slice, reshape, broadcast_in_dim }:
                continue                               // boundary — remains a live-in producer

            // (R3) ELEMENTWISE GATE
            if !isElementwiseOp(cand): continue        // 0x20fced2

            // (R4) SINGLE-CLUSTER-MEMBERSHIP all_of  (callback @0x20fa6e0)
            if !all_of(cand.result.users(), λu. cluster.contains(u)):  // 0x20fcf1e..0x20fd026
                continue                               // some user lives OUTSIDE — do not absorb

            // ADMIT — upward growth
            cluster.insert(cand)
            worklist.push(cand)                        // visit cand's own operands next
```

> **GOTCHA (the R4 legality) —** an elementwise producer is absorbed only when **every** user of its result is already in the cluster. This is the single most important rule. It prevents an internal SSA value from fanning out to a consumer *outside* the fusion — which would force materializing an internal temporary. Drop R4 and you produce fusions whose "internal" values are silently read by ops the fusion region cannot expose, breaking the live-out accounting in `codeGen`. (CERTAIN — `all_of` callback @0x20fa6e0 over `ValueUserIterator`.)

The `all_of` membership callback @0x20fa6e0 iterates `cand`'s result use-list; for each use it probes the cluster DenseSet (hash `(p>>4)^(p>>9)` masked, empty-bucket sentinel `0xFFFFFFFFFFFFF000`) and returns true only if every owner was found before `use_end`.

### Cluster finalize

Once growth stops, the member SmallVector is fed to `mlir::computeTopologicalSorting(MutableArrayRef<Op*>, …)` @0x20fd617 (only for ≥2 members; single-member clusters skip), then a manual swap-reversal loop produces a deterministic def-before-use ordering for the region body. The ordered ops + kind string go to `new FusionCluster(ops, kind)`.

### Boundary rules (summary)

An op is pulled into an Elementwise/MulRedSqrt cluster iff all hold:

| Rule | Condition |
|---|---|
| R1 | reachable by walking operands upward from a seed (reduce / concatenate / reshape) |
| R2 | **not** in `{transpose, fusion, concatenate, slice, reshape, broadcast_in_dim}` |
| R3 | `isElementwiseOp(it)` is true |
| R4 | **every** user of its result is already in the cluster |
| R5 | it has a defining op (block args are never pulled in — they are live-ins) |

The cluster boundary is the frontier where R2/R3/R4 first fail; those frontier producer values become live-ins, and any cluster result with an external consumer becomes a live-out. The seed itself is in the body even though a `reduce`/`concatenate` is *not* in `isElementwiseOp` — seeds are admitted by the walk, growth is gated by the predicate.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `fuseElementwiseOps` | 0x20fcac0 | 9198 | Seed + grow + codegen driver (Elementwise + MulRedSqrt) | CERTAIN |
| seed-walk lambda | 0x20fa080 | 195 | Seed `{reduce, concatenate}` | CERTAIN |
| all_of membership lambda | 0x20fa6e0 | 193 | `∀ user(cand) ∈ cluster ?` | CERTAIN |
| `hilo::isElementwiseOp` | 0x21c3e90 | 95 | Elementwise predicate (head + 4-link `isa<>`) | CERTAIN |
| `computeTopologicalSorting` | 0x20fd617 | — | Def-before-use ordering of members | CERTAIN |

---

## Transcendental-Idiom Sub-Passes — `fuseSubExp`, `fuseExpm1Op`, `fuseLog1pOp`

### Purpose

Three sub-passes recognize transcendental idioms. One *encapsulates* an existing numerically-stable tree (`fuseSubExp`); two *expand* a precise primitive the Neuron TPB engines have no native instruction for (`fuseExpm1Op`, `fuseLog1pOp`).

| Sub-pass | Addr | Matcher lambda | Idiom |
|---|---|---|---|
| `fuseSubExp` | 0x20ff720 | `{lambda(mhlo::AddOp)#1}` @0x20fb540 | 2-way stable **log-sum-exp** (max-subtraction) |
| `fuseExpm1Op` | 0x20ff1e0 | `{lambda(mhlo::Expm1Op)#1}` @0x20fae00 | **expand** `expm1(x) → exp(x) − 1.0` |
| `fuseLog1pOp` | 0x20feeb0 | `{lambda(mhlo::Log1pOp)#1}` @0x20fbcc0 | **expand** `log1p(x) → log(x + 1.0)` |

> **GOTCHA — these three run in the opposite direction from their names.** `fuseExpm1Op` and `fuseLog1pOp` are **expanders**, not folders: each consumes an existing `mhlo.expm1` / `mhlo.log1p`, synthesizes the constant 1.0, and emits the two- or three-op expansion. And `fuseSubExp` is not a `sub∘exp` fold at all — it matches the full 8-op log-sum-exp tree and performs no math rewrite whatsoever.

### Algorithm — `fuseSubExp` matcher (lambda @0x20fb540)

```c
function matchLogSumExp(AddOp root):                   // 0x20fb540
    if isa<mhlo::FusionOp>(root.parentOp): return      // already fused (0x20fb584)
    m   = getDefiningOp(root.op0); require MaxOp        // 0x20fb5d5 (TypeID 0x9d30610)
    lg  = getDefiningOp(m.op1);    require LogOp        // 0x20fb60a (0x9d30628)  — NB: root.op1 path
    inn = getDefiningOp(lg.op0);   require AddOp        // 0x20fb647 (0x9d30830)
    e1  = getDefiningOp(inn.op0);  require ExpOp        // 0x20fb688 (0x9d30698)
    e2  = getDefiningOp(inn.op1);  require ExpOp        // 0x20fb6d7
    require e1.hasOneUse                                // [e1+0x2E] sign (0x20fb6e3)
    s1  = getDefiningOp(e1.op0);   require SubtractOp   // 0x20fb735 (0x9d304a0)
    require e2.hasOneUse                                // 0x20fb741
    s2  = getDefiningOp(e2.op0);   require SubtractOp   // 0x20fb79f
    // structural equalities — the two subtracts share max as subtrahend:
    require s1.op0 == m.op0                             // 0x20fb7b9 :  a
    require s2.op0 == m.op1                             // 0x20fb7cb :  b
    require s2.op1 == s1.op1                            // 0x20fb7d9 :  same subtrahend
    require s1.op1 == m.result                          // 0x20fb7e8 :  subtrahend IS max(a,b)
    // matched: out = max(a,b) + log( exp(a−max) + exp(b−max) )
    cluster = {root, lg, inn, m, s2, e2, s1, e1}        // 8 ops
    push FusionCluster(cluster, "Elementwise")          // 0x20fb816..0x20fb845
```

The whole 8-op subtree is wrapped in one `"Elementwise"` fusion — **no new ops, no erase, no math rewrite**. The max-subtraction guarantees every `exp` argument ≤ 0 (so `exp ∈ (0,1]`, no overflow); encapsulating the tree keeps the `x−max` cancellation glued to its `exp` and lets the backend schedule it as a single elementwise tile-kernel instead of materializing `exp(a−max)`/`exp(b−max)` as separate tensors.

> **NOTE —** `fuseSubExp` reuses the `"Elementwise"` tag and rides the generic `printArbitraryFusionOp` emitter — it is **not** a distinct named composite. Its name "sub-exp" refers only to the `exp(x−max)` subtraction inside the matched tree.

### Algorithm — `fuseExpm1Op` / `fuseLog1pOp` expanders (lambdas @0x20fae00 / @0x20fbcc0)

The two lambdas are structurally identical, op-substituted:

```c
function expandExpm1(Expm1Op op):                      // 0x20fae00  (Log1p twin @0x20fbcc0)
    if !op.hasOneUse: return                           // [op+0x2E] sign (0x20fae23 / 0x20fbce3)
    et = op.result.shapedType.elementType              // 0x20fae96
    require isa<mlir::FloatType>(et)                    // 0x20faeaa
    sem = et.getFloatSemantics()                       // 0x20faf2b — per-dtype: bf16/f16/f32
    one = APFloat(IEEEFloat(sem, 1))                   // value 1.0 at the EXACT element semantics
    attr = DenseElementsAttr::get(op.resultType, {one})// splat 1.0 (0x20faf75)
    c    = build mhlo::ConstantOp(loc, type, attr)     // 0x20fafb3

    // --- Expm1 branch ---
    ex   = build mhlo::ExpOp(loc, type, op.operand, ResultAccuracyAttr)  // 0x20fb03c
    sub  = build mhlo::SubtractOp(loc, type, ex, c)    // 0x20fb123  → exp(x) − 1.0
    for u in op.result.uses: u.set(sub)                // rewire (0x20fb188..)
    push FusionCluster({c, ex, sub}, "Expm1")          // 3 ops, tag len 5 (0x20fb206)

    // --- Log1p twin ---
    add  = build mhlo::AddOp(loc, type, op.operand, c) // 0x20fbf04  → x + 1.0
    lg   = build mhlo::LogOp(loc, type, add, ResultAccuracyAttr)         // 0x20fbfe3 → log(x+1.0)
    for u in op.result.uses: u.set(lg)                 // 0x20fc048..
    push FusionCluster({c, add, lg}, "Log1p")          // 3 ops (0x20fc0c6)
```

The original `expm1`/`log1p` is left dead by the use-rewire and erased in the driver tail (`Operation::erase` @0x20ff35e Expm1 / @0x20ff02e Log1p).

> **QUIRK —** the synthesized constant 1.0 is built at the matched op's **exact** float semantics via `getFloatSemantics()` — a bf16 `expm1` gets a bf16 `1.0` splat, an f32 one gets an f32 splat. A reimplementation that materializes a fixed-precision 1.0 and casts will perturb the result near x≈0, exactly the regime `expm1`/`log1p` exist to make accurate. CERTAIN.

> **NOTE —** `ExpOp::build`/`LogOp::build` carry a `mhlo::ResultAccuracyAttr` copied from the original op, preserving its accuracy mode. `SubtractOp`/`AddOp` are plain 2-operand builds. Each build is guarded by `RegisteredOperationName::lookup(<TypeID>, ctx)`; a missing dialect op-name triggers `report_fatal_error("Building op \`<name>\` but it isn't known in this MLIRContext…")` (prefix @0x286e24, suffix @0x2df9e8) — op-name strings `mhlo.exponential` @0x272915 / `mhlo.subtract` @0x27ef81 (Expm1) and `mhlo.add` @0x276985 / `mhlo.log` @0x276a1c (Log1p).

> **NOTE —** wrapping the `{const, exp, sub}` / `{const, add, log}` triple in an `Expm1`/`Log1p`-tagged fusion preserves *provenance*: a precision-aware Tensorizer kernel for "Expm1"/"Log1p" can still be selected instead of the naive exp-minus-one, even though the math has been lowered.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `fuseSubExp` | 0x20ff720 | — | Driver: match log-sum-exp tree | CERTAIN |
| `fuseSubExp` matcher lambda | 0x20fb540 | — | 8-op `Add(Max,Log(Add(Exp(Sub),Exp(Sub))))` | CERTAIN |
| `fuseExpm1Op` | 0x20ff1e0 | — | Driver: expand `expm1` | CERTAIN |
| `fuseExpm1Op` lambda | 0x20fae00 | — | Synthesize `exp(x)−1.0`, tag `"Expm1"` | CERTAIN |
| `fuseLog1pOp` | 0x20feeb0 | — | Driver: expand `log1p` | CERTAIN |
| `fuseLog1pOp` lambda | 0x20fbcc0 | — | Synthesize `log(x+1.0)`, tag `"Log1p"` | CERTAIN |

---

## The `FusionCluster` Data Structure and Shared `codeGen`

### `FusionCluster` — what becomes a Penguin fused op

Every sub-pass builds the same heap object: `new FusionCluster(ArrayRef<Operation*> ops, StringRef kind)`, 0xD8 bytes, ctor @0x20faa20 (ABI: `rdi=this`, `rsi=ops.ptr`, `rdx=ops.size`, `rcx=kind.ptr`, `r8=kind.len`). The ctor copies `ops` into the member SetVector and calls `populateLiveInLiveOuts()` to compute the region boundary.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `fusionKind` | +0x00 | `std::string` (SSO, inline buf +0x10) | `"DotLogistic"` \| `"Elementwise"` \| `"MulRedSqrt"` \| `"Expm1"` \| `"Log1p"` — becomes the `FusionKind` attr value |
| members (set) | +0x20 | `SetVector<Op*>::DenseSet` | O(1) membership |
| members (vec) | +0x38 | `SetVector<Op*>::SmallVector<Op*,0>` | ordered cluster ops (topo order) |
| liveIn (set) | +0x48 | `SetVector<Value,…,4>::DenseSet` | region-input set |
| liveIn (vec) | +0x60 | `SetVector<Value,…,4>::SmallVector<Value,4>` | ordered region inputs |
| liveOut (set) | +0x90 | `SetVector<Value,…,4>::DenseSet` | region-output set |
| liveOut (vec) | +0xA8 | `SetVector<Value,…,4>::SmallVector<Value,4>` | ordered region outputs |

(total 0xD8). Live-ins/outs are computed per member op:

```c
populateLiveIns(op):    // 0x2103760  — for each OPERAND v of op:
    d = v.getDefiningOp()
    if d == null /*block arg*/ or d ∉ cluster:  liveIn.insert(v)    // producer outside cluster

populateLiveOuts(op):   // 0x2103680  — for each RESULT r of op:
    if ∃ user u of r with u ∉ cluster:          liveOut.insert(r)   // consumed outside cluster
```

By the R4 all_of gate, internal elementwise producers have all users inside the cluster, so they are *not* live-outs — only the seed/anchor result and genuine fan-out points become region outputs.

### `codeGen` — cluster → `mhlo.fusion`

`FusionCluster::codeGen(DominanceInfo*, PostDominanceInfo*)` @0x2104640 (3008 B) is shared by all seven callers (the six sub-passes plus `ScheduleFusion::fuseAllGatherReduceScatter`). It materializes the fusion op:

```c
function codeGen(dom, postdom):                        // 0x2104640
    loc     = FusedLoc::get(memberLocs, null, ctx)     // 0x2104725 — fused location over members
    liveIns = copy(cluster.liveIn)                     // 0x2104743
    inputs  = updateInputs(liveIns, &valueRemap)       // 0x2104758 — remap through clone map
    resultTypes = liveOut value types                  // SmallVector<Type,1>
    fusion  = OpBuilder.create<mhlo::FusionOp>(loc, resultTypes&, inputs&)   // 0x21048b9
    blk     = new Block                                // 0x210490f
    fusion.region.push_back(blk)                       // 0x2104960
    for op in cluster.members (reverse): op.moveBefore(blk, blk.end())       // 0x21049ba
    blk.addArguments(liveInTypes, locs)                // 0x2104ada — block args = live-ins
    for (v_i, a_i) in zip(liveIns, blockArgs):
        v_i.replaceUsesWithIf(a_i, λu. uIsInsideBlock(u))   // 0x2104b61 (pred lambda @0x2101720)
    // terminator
    st = OperationState(loc, RegisteredOperationName::lookup(mhlo::ReturnOp, ctx))   // 0x2104bb1
    mhlo::ReturnOp::build(builder, st, liveOutValues)  // 0x2104c20 → builder.create(st) 0x2104c33
    // STAMP the FusionKind inherent attribute
    nameAttr = StringAttr::get(ctx, cluster.fusionKind)// 0x2104cb2
    keyAttr  = StringAttr::get(ctx, "FusionKind")      // 0x2104cf9 (str @0x266931)
    fusion.setInherentAttr(keyAttr, nameAttr)          // 0x2104d65 / 0x2105189
    recursivelyMoveDependentOps(fusion, dom, postdom)  // 0x2104db7 — dominance-legal dep hoist
    // finalize live-out rewiring to fusion results                          // 0x2104fae
```

The result is a single `mhlo.fusion { <ordered body> ; mhlo.return <liveOuts> } {FusionKind="<kind>"}`.

> **GOTCHA — the post-dominance argument is null on the DotLogistic path.** `fuseDotLogisticOp` calls `codeGen` with `PostDominanceInfo = nullptr` (`xor edx,edx` @0x2100683). The signature takes a post-dom pointer, but nothing on that path reads it — dominance flows only into `recursivelyMoveDependentOps`, which tolerates a null. Other callers, such as `fuseElementwiseOps`, may pass a real one.

> **GOTCHA —** the *only* attribute `codeGen` writes is `FusionKind`. There is no numeric `composite.version` or `backend_config` emitted here. Any `composite.version`/`composite.attributes` on the StableHLO side are added by a downstream FusionToComposite stage, not by this routine. (`composite.name` @0x27713d / `composite.attributes` @0x2122ce exist for the StableHLO twin only.)

### Downstream emission

`MhloToPythonPrinter::print<mhlo::FusionOp>` @0x20f5046 dispatches on the `FusionKind` string. Dedicated emitters exist **only** for `DotLogistic` (`printDotLogisticFusionOp` @0x20f3050), `MulRedSqrt` (`printMulRedSqrtFusionOp` @0x20f1f60), and `ScheduleFusion` (`printScheduleFusionOp` @0x20f4ce0). `Elementwise`, `Expm1`, and `Log1p` fall through to the generic `printArbitraryFusionOp` @0x20f3f00, whose emitted Penguin op name *is* the FusionKind string. The full printer-recognized kind roster (from the two error strings) is seven: `ScheduleFusion, MulRedSqrt, DotLogistic, Elementwise, Expm1, Log1p, DotSoftmax` (`mhlo.fusion` error @0x3a9bc0; `stablehlo.composite` error @0x2e0258). `DotSoftmax` is consumed by the printers but **not produced** by any of these six sub-passes — its producer is elsewhere.

---

## Evidence summary

**Read directly from the binary.**

- **Every sub-pass entry address** resolves in `names.json`: `fuseDotLogisticOp@0x2100160`, `fuseElementwiseOps@0x20fcac0`, `fuseSubExp@0x20ff720`, `fuseExpm1Op@0x20ff1e0`, `fuseLog1pOp@0x20feeb0`, `fuseMulRedSqrt@0x20ff510`, `runOnOperation@0x2101680`, `FusionCluster::FusionCluster@0x20faa20`, `codeGen@0x2104640`, `isElementwiseOp@0x21c3e90`, `getNumUsers@0x21bfee0`.
- **All twelve TypeID resolver symbols** likewise: SubtractOp=0x9d304a0, ReshapeOp=0x9d30558, MulOp=0x9d305f8, MaxOp=0x9d30610, LogisticOp=0x9d30620, LogOp=0x9d30628, Log1pOp=0x9d30630, Expm1Op=0x9d30690, ExpOp=0x9d30698, DotOp=0x9d306f0, DotGeneralOp=0x9d306f8, AddOp=0x9d30830.
- **The FusionKind strings**, dumped byte-for-byte at their file offsets: `Elementwise`@0x669e3, `DotLogistic`@0x15ec2, `FusionKind`@0x66931, `MulRedSqrt`@0x560e7, `Expm1`@0x327b3, `Log1p`@0x1a2eb, `DotSoftmax`@0x5e528. Note the VA-to-file-offset conversion for `MulRedSqrt`: 0x2560e7 − 0x200000 = 0x560e7.
- **The `growFusionOpUpwards` signature and its all-users gate.** The mangled symbol demangles to the exact type `SetVector<Operation*, SmallVector<Op*,0u>, DenseSet<Op*>>` with a nested `all_of(...{lambda(OpOperand)})` over `ValueUserIterator<ResultRange::UseIterator, OpOperand>`, which is what establishes that growth runs upward over operands under an all-users-in-cluster predicate.
- **The flag gating.** The help strings `aEnableTheNeuronOpFusionFlagToProperlyFuseLog1popBefo`, `...Expm1opBefo`, and `general-elementwise-fusion` place Log1p, Expm1, and Elementwise behind the flag; the disasm at `0x2101680` shows `fuseSubExp` called unconditionally first.

### Limits of this reading

Four things are weaker. The precise coupling of `fuseMulRedSqrt` to the DotLogistic flag is disasm-derived rather than independently re-traced. The `[op+0x2E]` sign-bit reading as `hasOneUse` is **INFERRED** — the helper is inlined with no symbol of its own, though the interpretation is consistent across all three transcendental matchers. `recursivelyMoveDependentOps` @0x2103a10 is documented at role level only; its internal dominance walk was not traced. And the StableHLO twins (`StableHLONeuronOpFusion::*`) are assumed byte-parallel to the MHLO originals by symmetry, not compared.

---

## Related Components

| Component | Relationship |
|---|---|
| `fuseMulRedSqrt` @0x20ff510 | Sixth sub-pass; explicit `mul→reduce→rsqrt` matcher; shares `codeGen`, emits `"MulRedSqrt"` — see 4.35 |
| `ScheduleFusion::fuseAllGatherReduceScatter` | Seventh `codeGen` caller; collective-schedule fusion — see 4.36 |
| `StableHLONeuronOpFusion::*` @0x2133b10 | StableHLO twin of the whole pass; same cluster tags, emits `stablehlo.composite` |
| `MhloToPythonPrinter` @0x20f5046 | Consumes `FusionKind` to emit Penguin Python — Part 5 |

## Cross-References

- [RMSNorm Fusion & Cluster Codegen](rmsnorm-fusion-cluster-codegen.md) — 4.35; `fuseMulRedSqrt` and the shared `codeGen` body in depth
- [Schedule-Fusion & Fusion-to-Composite](schedule-fusion-composite.md) — 4.37; the seventh `codeGen` caller (collective schedule fusion)
- [HLO → Native / NKI Kernel Lowering](hlo-to-native-kernel-lowering.md) — where fused ops feed the Penguin backend
- [Softmax Legalization](softmax-legalize.md) — the upstream pass that often produces the `dot`/`logistic`/log-sum-exp shapes matched here
