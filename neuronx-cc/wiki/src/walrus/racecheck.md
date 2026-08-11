# Intra-Core Race Checker

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 VAs drift, struct offsets and ordinals are ABI-stable). The vector-clock engine, the per-BB driver, and the race report live in `libBIRRacecheck.so` (build-id `4b6176662ca4…`, md5 `68a61c25…`); the shared access-pattern geometry (`doAccessesOverlap` and the Diophantine lattice test) lives in `libBIR.so` (build-id `a9b1ea38…`, md5 `12bb979f…`); the `bir_racecheck` `BackendPass` and its CLI gate live in `libwalrus.so` (build-id `92b4d331…`). For `.text`/`.rodata` the virtual address equals the file offset. Many symbols carry two IDA address frames — an nm body frame and a per-symbol sidecar/PLT frame; both are noted where they diverge. Treat every address as version-pinned.*

## Abstract

`bir_racecheck` is the walrus backend's intra-core data-race verifier: given a *scheduled, semaphore-synchronized* BIR basic block running on a single LNC, it proves that no two memory accesses that can execute simultaneously touch overlapping bytes with at least one writer. It is a hard verifier — the first race it finds is thrown as a `std::runtime_error("Data race detected:\n…")` that aborts the compile, not a warning it collects. The model is the textbook one a reader already owns: a **Fidge/Mattern vector clock** decides *concurrency* (the temporal half), and an **affine-lattice intersection** decides *byte overlap* (the spatial half). A race is the conjunction of the two plus a write.

The temporal half is classical and lives entirely in `libBIRRacecheck.so`. Each memory-touching instruction becomes a `racecheck::Graph::Op` carrying a per-engine timestamp vector — one `int32` component per engine (Pool/Activation/PE/DMA/DVE/SP). `computeTimestamps` assigns clocks in a topological pass over the post-synchronizer dependency edges: join same-engine predecessors, tick the Op's own engine component by one, then join cross-engine (sync-edge) predecessors. Two Ops are **concurrent** iff their clocks are *incomparable* (each is ahead of the other on some engine) — meaning no chain of program-order and semaphore edges orders them, so the hardware may run them at once. A per-engine segment-window prune with binary search keeps the concurrency scan near `O(n · #engines · log n)` instead of the naive `O(n²)`.

The spatial half is **not** in `libBIRRacecheck.so` at all: `PhysicalAccessPattern::doAccessesOverlap` is a `UND` import resolved at load time to `libBIR.so 0x3add90`. That single libBIR symbol is the shared overlap engine — the inter-core barrier checker's `.so` ([8.43](barriercheck.md), where it backs the intra-BB happens-before builder `InstGraph::drawHBsPerBB`, *not* the inter-core `compareTwoAPs` spatial test, which uses `boost::icl` interval-set intersection) and the libBIR write-in-place verifier link the *same* body — so the geometry documented here is the one source of truth for the strided-lattice overlap test. Its headline is the **Diophantine DFS** (`APOverlapModularDFSMethod` → `APOverlapDFSHelper`): a binary-GCD-pruned, memoized depth-first search that decides whether two regularly-strided byte lattices share a single integer point. This is what catches two interleaved strided DMAs that *never* alias even though their bounding boxes do — a case a rectangle test false-positives and an exact byte-set enumeration would blow up on.

For reimplementation, the contract is:

- The `Op` layout and the `int32` **vector-clock** representation, and `computeTimestamps`' three-phase join (same-engine join → own +1 → cross-engine join) over the dep graph.
- `less_than` (strict happens-before) and `concurrent` (incomparable), and the per-engine `SegBound` window + `findMidSeg` binary search that prunes the concurrency scan.
- The driver: `fromBasicBlock` (walk → collect memory Ops → wire edges → clock) and `check` (concurrent-pair enumeration → TBB `parallel_for` of `interfere` → throw on first overlap).
- `doAccessesOverlap`'s decision tree — fast bailout, same-buffer gate, the boost::icl interval-set path, and the **GCD + DFS** exact strided lattice test — pinned as the shared libBIR symbol.
- The `bir_racecheck` `BackendPass` wiring: registered by factory, gated by `--enable-data-race-checker` (default OFF), run once post-schedule/post-RA.

| | |
|---|---|
| **Pass class** | `neuronxcc::backend::BirRaceCheck` (libwalrus), vtable `off_3D94A28`, 104 B |
| **Pass name** | `"bir_racecheck"` @ `0x1dc413d` (libwalrus `.rodata`) |
| **CLI gate** | `--enable-data-race-checker` @ `0x1dc319b`, default **false**; desc `0x1dc3182` |
| **Pipeline slot** | post-schedule, post-RA; last pre-codegen pass (after `lower_control`, `lnc_barriercheck`) |
| **Engine ctor** | `racecheck::RaceChecker::RaceChecker(Module&, bool)` @ `0x762b0` (imported) |
| **Per-BB driver** | `racecheck::RaceChecker::check(BasicBlock&)` @ `0x75240` |
| **Graph builder** | `racecheck::Graph::fromBasicBlock` @ `0x58f30` (nm) / `0x37eb0` (sidecar) |
| **Clock model** | Mattern vector clock, `std::vector<int32>` one component per engine |
| **Concurrency test** | `Op::concurrent` @ `0x55ad0` — clocks incomparable |
| **Overlap geometry** | `bir::PhysicalAccessPattern::doAccessesOverlap` @ **libBIR** `0x3add90` (UND import) |
| **Exact strided test** | `APOverlapModularDFSMethod` @ libBIR `0x3a9540` → `APOverlapDFSHelper` @ `0x3a8e20` |
| **Race precondition** | `concurrent(A,B)` ∧ `doAccessesOverlap(x,y)` ∧ (x or y is a write) |
| **Failure mode** | `std::runtime_error("Data race detected:\n…")` @ `0xab0f8`; aborts compile |
| **Source path** | `…/neuronxcc/walrus/ir/lib/Racecheck/Graph.cpp` @ `0xb28d0` (from asserts) |

The clock is a plain `std::vector<int32>`. `less_than`, `concurrent`, and `maximize` all read `Op+0x38`/`Op+0x40` as an `int32*` begin/end pair and compute `size = (end-begin) >> 2` — a 4-byte stride, in all three bodies.

> **GOTCHA — CRoaring is in the backend, but not in the race checker.** The substring `roaring` appears 120× in `libwalrus.so`, so a symbol sweep across the backend will suggest the clocks are Roaring bitmaps. They are not: there is **no** roaring symbol or string anywhere in `libBIRRacecheck.so` — `strings` and `nm` both come back empty. CRoaring is used elsewhere in the backend.

---

## The Vector-Clock Representation

### Purpose

The temporal half answers one question: *can these two instructions run at the same time?* On a scheduled, semaphore-synchronized core the answer is determined by the dependency edges the synchronizer left behind. The checker reconstructs a Mattern vector clock from those edges so that "can run together" reduces to "the two clocks are incomparable". Everything below — the `Op` struct, the clock-advance, the happens-before and concurrency predicates — exists to make that reduction exact and cheap.

### The `Op` struct

Every memory-touching instruction (after the BC180 gate, below) becomes one `racecheck::Graph::Op` — a 104-byte (`0x68`) record `new`'d in `addOp` @ `0x56d30`. The fields a reimplementer must reproduce:

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `engineId` | `+0x00` | `int32` | the engine's sequence index (NOT `EngineType`); `addOp` arg |
| `preds` | `+0x08`/`+0x10`/`+0x18` | `Op**` vector | predecessors; `addEdge` does `to.preds += from` |
| `succs` | `+0x20`/`+0x28`/`+0x30` | `Op**` vector | successors; `addEdge` does `from.succs += to` |
| `timestamps` | `+0x38`/`+0x40`/`+0x48` | `int32*` vector | **the vector clock**, one `int32` per engine |
| `engLocalIndex` | `+0x50` | `int32` | this Op's index within its engine's op-vector (init `-1`) |
| `engine` | `+0x58` | `Engine*` | the "same engine?" tag; `=&Engine.opVector` |
| `skip` | `+0x60` | `bool` | exempt from race reporting (uninit-read / opcode ∈ {33,78,87}) |

The clock length equals the engine count (`Graph+0x40`). Two facts pin the representation as a plain `std::vector<int32>` rather than any bitmap: every reader computes `size = (timestamps.end − timestamps.begin) >> 2` (4-byte element), and every reader asserts the two operands' sizes are equal against the literal string `"this->timestamps.size() == other->timestamps.size()"` (`Graph.cpp` @ `0xb2948`).

> **NOTE —** `engineId` (`+0x00`) is a per-graph sequence id assigned by `addEngine` in first-seen order, *not* the architectural `EngineType` (Pool=1, Act=2, PE=3, DMA=4, DVE=5, SP=6 — see [Execution & Sync Model](../arch/execution-sync-model.md)). The clock-component column for an Op's own engine is read separately as `*(uint32*)(engine + 0x20)` — a column index stored four bytes ahead of the engine's op-vector pointer. *Anchor: `computeTimestamps`' `++*(int*)(ts.begin + 4*col)` with `col = *(uint*)(engine+32)`.*

### Algorithm — clock advance

The clock is advanced from dependency *edges*, not from raw `SyncInfo` `Wait`/`Update` values: a semaphore wait that blocks B on A's post is, by the time racecheck runs, a dependency edge `A→B` that `fromBasicBlock` already wired. `computeTimestamps(Op)` then propagates clocks in build order (a valid topo order because BB instruction order respects deps):

```c
// racecheck::Graph::Op::maximize(Op* other)            // 0x55430 (body) / 0x38050 (sidecar)
//   elementwise clock JOIN with a same-value tiebreak tick.
function maximize(self, other):
    assert self.ts.size == other.ts.size                 // Graph.cpp:0x192
    for k in 0 .. self.ts.size:                          // ts at self+0x38, stride 4
        if other.ts[k] > self.ts[k]:                     // only when other is ahead
            self.ts[k] = max(self.ts[k] + 1, other.ts[k])  // the "+1" tick is real

// racecheck::Graph::computeTimestamps(Op* u)           // 0x554d0 (body) / 0x38500 (sidecar)
function computeTimestamps(u):
    eng = u.engine                                       // u+0x58 (a2[11], 11*8=0x58)
    // phase 1: join SAME-engine predecessors
    for p in u.preds:                                    // [u+0x08 .. u+0x10)
        if p.engine == eng:  u.maximize(p)               // p.engine == *(p+0x58)
    // phase 2: own program-order tick
    col = *(uint32*)(eng + 0x20)                         // u's clock column
    range_check(col < u.ts.size)                         // std::vector::_M_range_check
    u.ts[col] += 1
    // phase 3: join CROSS-engine predecessors (the sync edges)
    for p in u.preds:
        if p.engine != eng:  u.maximize(p)
```

The two phases are split deliberately: same-engine joins establish the program-order chain *before* the local tick, so the own component strictly increases along the engine timeline; the cross-engine joins then import the highest clock value seen across every sync edge into this Op. The graph-level driver `computeTimestamps(void)` @ `0x55580` first zero-allocates every Op's `int32[#engines]` clock, then calls the per-Op routine over the global Op vector in build order. The semantics: `u.ts[e]` = the number of engine-`e` program steps known to happen-before `u`, through program order on `e` or transitively through sync edges into `u`.

> **QUIRK —** `maximize` is `ts[k] = max(ts[k]+1, other[k])`, *not* a plain `max(ts[k], other[k])`. The `+1` only bites when `other[k] == self.ts[k]` after the increment would tie; in practice `other[k]` dominates and it degenerates to a standard max-join. It is a same-value tiebreak tick, not a second program-order increment. Whether it ever changes a *result* given build-order topological assignment is not exhaustively proven (the disasm is read precisely; the effect is `INFERRED` to be a no-op in practice). Reproduce it exactly anyway — a plain max-join is a different clock.

### Algorithm — happens-before and concurrency

```c
// racecheck::Graph::Op::less_than(Op* b) const          // 0x556e0 (body) / 0x381e0 (sidecar)
//   strict happens-before: a < b iff a ≤ b everywhere AND a < b somewhere.
function less_than(a, b):
    assert a.ts.size == b.ts.size                         // Graph.cpp:0x19A
    strictlyLess = false
    for k in 0 .. a.ts.size:
        if a.ts[k] > b.ts[k]:  return false               // a exceeds b on axis k → not ≤
        if a.ts[k] < b.ts[k]:  strictlyLess = true
    return strictlyLess                                   // equal clocks → false (a==b is not <)

// racecheck::Graph::Op::concurrent(Op* b) const          // 0x55ad0 (body) / 0x37xxx (sidecar)
//   incomparable: each is ahead of the other on some axis. THE race precondition.
function concurrent(a, b):
    assert a.ts.size == b.ts.size                         // Graph.cpp:0x1A9
    aGreater = false ; bGreater = false
    for k in 0 .. a.ts.size:
        if   a.ts[k] > b.ts[k]:  aGreater = true
        elif a.ts[k] < b.ts[k]:  bGreater = true
        if aGreater and bGreater:  return true            // short-circuit on first witness
    return aGreater and bGreater
```

`concurrent` is an inlined two-flag scan, **not** two `less_than` calls — it returns true the moment it has seen the source ahead on one axis and behind on another. Incomparable clocks mean no synchronization orders the two accesses, so they can execute simultaneously: a candidate race. It is exactly `¬(a<b) ∧ ¬(b<a) ∧ a≠b`.

### Function Map

| Function | Addr (body / sidecar) | Size | Role | Confidence |
|---|---|---|---|---|
| `Op::maximize` | `0x55430` / `0x38050` | ~95 B | clock join with `+1` tiebreak | CERTAIN |
| `computeTimestamps(Op*)` | `0x554d0` / `0x38500` | 173 B | per-Op three-phase clock assign | CERTAIN |
| `computeTimestamps(void)` | `0x55580` / `0x37b40` | 173 B | zero-alloc + drive over global vec | CERTAIN |
| `Op::less_than` | `0x556e0` / `0x381e0` | ~80 B | strict happens-before | CERTAIN |
| `Op::concurrent` | `0x55ad0` / — | ~95 B | incomparable (race precondition) | CERTAIN |
| `addOp(eng, seq)` | `0x56d30` / `0x385e0` | 307 B | `new(0x68)` Op; append to engine+global | CERTAIN |
| `addEdge(from, to)` | `0x56bd0` / `0x374a0` | 110 B | `from.succs += to`; `to.preds += from` | CERTAIN |
| `addEngine(name)` | `0x57ef0` / `0x37450` | 837 B | name→id; assign next engine seq id | HIGH |

---

## The Concurrency Scan

### Purpose

Naively, finding every pair of concurrent Ops is `O(n²)` `concurrent` calls. The checker exploits a structural fact: on a *single* engine — a totally ordered timeline by `engLocalIndex` — the Ops concurrent with a fixed source `u` form a **contiguous window**. Everything at or below `u`'s latest predecessor on that engine happens-before `u`; everything at or above `u`'s earliest successor happens-after `u`; only the strip in between can be concurrent. The scan computes that strip per engine and binary-searches its edges.

### Algorithm — segment-bound window

```c
// racecheck::Graph::SegBound::SegBound(int n)            // 0x55200 / 0x37a00
//   two int32[n] arrays: lower[]=-1 (memset 0xFF), upper[]=INT_MAX (0x7FFFFFFF).

// racecheck::Graph::findMidSegLowerUpperBoundPerEng(u)   // 0x55390 / 0x37d10
function perEngBounds(u) -> SegBound:
    sb = SegBound(#engines)                               // lower=-1, upper=INT_MAX
    for p in u.preds:
        e = *(uint*)(p.engine + 0x20)                     // p's clock column
        sb.lower[e] = max(sb.lower[e], p.engLocalIndex)   // latest direct predecessor on e
    for s in u.succs:
        e = *(uint*)(s.engine + 0x20)
        sb.upper[e] = min(sb.upper[e], s.engLocalIndex)   // earliest direct successor on e
    return sb

// racecheck::Graph::findMidSeg(Engine& E, Op* u, &lo, &hi, SegBound&)  // 0x55880 / 0x38480
function findMidSeg(E, u, sb) -> (lo, hi):
    col = *(uint*)(E.engine + 0x20)
    lo  = max(0, sb.lower[col] + 1)                       // just past last predecessor
    hi  = (sb.upper[col] == INT_MAX) ? E.ops.size()-1     // no successor → end of timeline
                                     : sb.upper[col] - 1  // just before first successor
    if lo <= hi and lo + 10 < hi:                         // window wide → bisect the edges
        if   u.less_than(E.ops[lo]):  hi = lo + 1         // whole strip after u → clamp
        elif E.ops[hi].less_than(u):                      // whole strip before u →
             hi = improveMidSegLowerBound(E, u, lo, hi)   //   find first non-(<u)
        else: hi = improveMidSegUpperBound(E, u, lo, hi)  //   find last non-(u<)
        // a symmetric probe tightens lo the same way
    return (lo, hi)                                       // windows ≤10 wide left for linear scan
```

```c
// improveMidSegLowerBound @0x55760/0x38160 — first m in (lo,hi] with E.ops[m] NOT < u
function improveMidSegLowerBound(E, u, lo, hi):
    while lo < hi: m = (lo+hi)/2;   if E.ops[m].less_than(u): lo = m+1 else hi = m
    return lo
// improveMidSegUpperBound @0x557f0/0x376a0 — last m where u NOT < E.ops[m]
function improveMidSegUpperBound(E, u, lo, hi):
    while lo < hi: m = (lo+hi+1)/2; if u.less_than(E.ops[m]): hi = m-1 else lo = m
    return lo
```

Both binary searches rely on the timeline being monotone in happens-before, which holds because same-engine program order is a clock chain. The driver then linearly tests only the survivors:

```c
// racecheck::Graph::findConccurent(srcEngId, u, &resultMap)   // 0x5a340 / 0x37a70
function findConccurent(u, resultMap):
    if u.skip: return                                    // u+0x60 exempt source
    sb = perEngBounds(u)
    for eng in 0 .. #engines-1:
        E = engineTree.at(eng)                           // map::at — throws if missing
        if E.ops empty: continue
        (lo, hi) = findMidSeg(E, u, sb)
        for j in lo .. hi:
            c = E.ops[j]
            if not c.skip and concurrent(u, c):          // c+0x60 exempt target
                resultMap[u].insert(c)                   // DenseMap<Op*, DenseSet<Op*>>

// racecheck::Graph::collectConccurentPairs(&resultMap)        // 0x5a8a0 / 0x383a0
function collectConccurentPairs(resultMap):
    for E in engineTree:
        for u in E.ops:  findConccurent(u, resultMap)
```

Net cost per source Op is `O(#engines · log(window))` instead of `O(n)`. The `skip` flag prunes both ends: a skipped Op is neither a race source (`findConccurent` returns immediately) nor a candidate target (the inner `!c.skip` test).

### Function Map

| Function | Addr (body / sidecar) | Role | Confidence |
|---|---|---|---|
| `SegBound::SegBound(int)` | `0x55200` / `0x37a00` | two `int32[n]`: lower=-1, upper=INT_MAX | CERTAIN |
| `findMidSegLowerUpperBoundPerEng` | `0x55390` / `0x37d10` | per-engine pred/succ window bounds | CERTAIN |
| `findMidSeg` | `0x55880` / `0x38480` | window → tight `[lo,hi]`, bisect if wide | CERTAIN |
| `improveMidSegLowerBound` | `0x55760` / `0x38160` | binary-search lower edge via `less_than` | CERTAIN |
| `improveMidSegUpperBound` | `0x557f0` / `0x376a0` | binary-search upper edge via `less_than` | CERTAIN |
| `findConccurent` | `0x5a340` / `0x37a70` | per-engine windowed concurrency scan | CERTAIN |
| `collectConccurentPairs` | `0x5a8a0` / `0x383a0` | drive `findConccurent` over all Ops | CERTAIN |

---

## The Driver

### Purpose

`fromBasicBlock` is the single per-BB builder: it walks the scheduled instruction stream, turns the memory-touching instructions into clock-graph Ops, wires the three edge sources (sync deps, program order, the opcode-46 special edge), and assigns clocks. `check` then enumerates the concurrent pairs and runs the spatial test in parallel.

### Algorithm — graph construction

```c
// racecheck::Graph::fromBasicBlock(BB, &op2inst)         // 0x58f30 / 0x37eb0
function fromBasicBlock(bb, op2inst) -> Graph:
    G = new Graph                                         // engine Rb_tree empty
    DenseMap<Inst*, vector<Op*>> inst2ops                 // local, for edge wiring

    // ---- WALK + COLLECT ----
    for inst in bb.instructions:                          // next = *(inst+0x08)
        op = inst.opcode                                  // *(inst+0x50), InstructionType
        if op == 81: continue                             // IT81 dropped entirely
        isMemOp = (op == 18) or (op in 19..67 and BC180[op-19])  // @0xbc180 gate
        if not isMemOp: continue
        eng = addEngine(EngineType2string(inst))          // engine name → seq id
        o   = addOp(eng, seq++); o.engLocalIndex = engine.ops.size()-1
        op2inst[o] = inst; inst2ops[inst].push_back(o)
        o.skip = inst.canInstReadFromUninitMem()
              or ((op-33) in mask 0xFFBFDFFFFFFFFFFE)     // op ∈ {33,78,87} exempt
        if inst.second != 0:                              // 2-Op marker (*(inst+0xE8))
            eng2 = addEngine(string(*(inst+0x68)))        // 2nd-engine name (call/ret)
            o2   = addOp(eng2, seq++); inst2ops[inst] += o2
            addEdge(o, o2)                                // asserts fromOps,toOps ∈ {1,2}

    // ---- CROSS-ENGINE SYNC EDGES (post-synchronizer dep graph) ----
    for inst in bb.instructions:
        for edge in inst.deps:                            // hash-set @inst+0xC8
            target = *(edge+0x10) & ~7                    // untag EdgePtr PointerIntPair
            if engineOf(target) != engineOf(inst):        // cross-engine only
                addEdge(inst2ops[target].back(), inst2ops[inst].front())

    // ---- PROGRAM-ORDER EDGES (per engine timeline) ----
    for E in engineTree:
        prev = null
        for o in E.ops:  if prev: addEdge(prev, o);  prev = o

    // ---- opcode-46 IndirectSaveAccumulate overlap edge ----
    acc = null
    for inst in bb.instructions:
        if inst.opcode == 46:
            if acc and doAccessesOverlap(acc.pap, inst.pap):  // libBIR geometry
                addEdge(inst2ops[acc].front(), inst2ops[inst].front())
            acc = inst

    computeTimestamps(G)
    return G
```

The memory-op gate is a 49-byte table at `0xbc180` indexed by `opcode-19`; the set bytes are at indices `{0,3,13,22,23,24,25,26,27,48}` — read out of the table with `xxd`, where the first row is `01 00 00 01 …` and the `22..27` run is six consecutive `01`s — which decode to opcodes `{19,22,32,41,42,43,44,45,46,67}`, plus opcode 18 hard-coded. Opcode 81 is dropped before the gate; the skip mask `0xFFBFDFFFFFFFFFFE` over `(opcode-33)` clears bits for `{33,78,87}`. The sync edges are exactly the post-synchronizer dependency edges — the `& ~7` strips the 3-bit `EdgeKind` from the `llvm::PointerIntPair` documented on the [Dependence Graph](dependence-graph.md) page; racecheck *consumes* that graph, it does not re-derive sync from `SyncInfo` values.

> **GOTCHA —** the sync edges are wired only *across different engines*. Same-engine ordering is added separately as the program-order chain. A reimplementation that adds every dependency edge regardless of engine double-counts the same-engine timeline and corrupts the clock — the `engineOf(target) != engineOf(inst)` guard is mandatory.

### Algorithm — the check driver

```c
// racecheck::RaceChecker::check(BasicBlock& bb)          // 0x75240 / 0x25a99
function check(bb) -> int:
    DenseMap<Op*, Inst*> op2inst
    Graph G = fromBasicBlock(bb, op2inst)                 // ops + edges + clocks
    G.collectConccurentPairs(this.pairs)                  // this+0x08: incomparable pairs
    stable_vector<pair<Inst*,Inst*>> work                 // flatten DenseSet → Inst pairs
    for (u, S) in this.pairs:
        for v in S:  work.push_back({op2inst.at(u), op2inst.at(v)})  // at() asserts present
    ApplyInterferenceCheck body(&work)                    // captures ONLY &work
    tbb::parallel_for(blocked_range(0, work.size()), body)  // re-raises body's throw
    return 0                                              // nonzero only in unused collect mode

// racecheck::ApplyInterferenceCheck::operator()(range)   // 0x4f8f0 / 0x38260 (Hex-Rays FAIL; disasm)
function operator()(r):
    for i in r.begin() .. r.end():
        iv = interfere(work[i].first, work[i].second)     // via ::check @0x4f8d0
        if (bool)iv:                                      // operator bool: papX && papY
            throw runtime_error("Data race detected:\n" + iv.toString())
```

The TBB body captures only the work-vector pointer (`ctor` @ `0x4fa80`), not the `RaceChecker` or its collect-all flag — so the wired path always *throws* on the first overlapping pair. `RaceChecker::check` returns nonzero only in a non-throwing collect-all mode that the wired pass never reaches.

### Algorithm — interfere (access collection + conflict polarity)

`interfere` supplies the read/write polarity that `doAccessesOverlap` (which is polarity-agnostic) does not. It walks the two instructions' operand lists and pairwise-overlaps them in three phases:

```c
// racecheck::RaceChecker::interfere(Inst* A, Inst* B)    // 0x74d70 / 0x37b10
function interfere(A, B) -> Interference:
    // OUTPUTS: intrusive list @Inst+0xC0/+0xC8; an Argument is an OUTPUT iff *(int)(arg+0x10)==1
    // IN/OUT : two-segment list @Inst+0xA0..+0xB8; each cast<PhysicalAccessPattern> (isa-filtered)
    for wx in outputs(A):
        for ry in (outputs(B) ++ inouts(B)):
            if doAccessesOverlap(cast<PAP>(wx), cast<PAP>(ry)):     // libBIR 0x3add90
                return Interference(wx, WRITE, ry, ry in outputs(B) ? WRITE : READ)  // WAW / WAR
    for rx in inouts(A):
        for wy in outputs(B):
            if doAccessesOverlap(cast<PAP>(rx), cast<PAP>(wy)):
                return Interference(rx, READ, wy, WRITE)                              // RAW
    return Interference{}                                  // all-zero ⇒ operator bool false ⇒ no race
```

There is **no** read-read phase. The loops only pair `outputs × (outputs ∪ inouts)` and `inouts × outputs`, so every reported pair carries at least one write by construction — two reads are structurally a no-race. `Interference` is a 32-byte record `{papX, typeX, papY, typeY}` (`type 1=WRITE, 0=READ`); `operator bool` @ `0x74d50` is `papX != 0 && papY != 0`.

### End-to-end predicate

A race is reported between concurrent instructions A, B iff:

```text
concurrent(A, B)                              // clocks incomparable — no sync orders them
  ∧  ∃ x∈access(A), y∈access(B) :
        doAccessesOverlap(x, y)               // footprints share bytes (libBIR geometry)
  ∧  at least one of {x, y} is a WRITE        // WAW / WAR / RAW; RR never tested
  ⇒  throw "Data race detected:\n…"
```

### Function Map

| Function | Addr (body / sidecar) | Size | Role | Confidence |
|---|---|---|---|---|
| `fromBasicBlock` | `0x58f30` / `0x37eb0` | ~3.6 K | walk → collect → edges → clock | CERTAIN |
| `RaceChecker::check` | `0x75240` / `0x25a99` | ~1.4 K | pairs → work-vec → TBB interfere | CERTAIN |
| `RaceChecker::interfere` | `0x74d70` / `0x37b10` | 1228 B | WAW/WAR/RAW × `doAccessesOverlap` | CERTAIN |
| `ApplyInterferenceCheck::operator()` | `0x4f8f0` / `0x38260` | 397 B | TBB body; throw on first race | HIGH (disasm) |
| `ApplyInterferenceCheck::ctor` | `0x4fa80` / `0x38550` | 5 B | captures work-vec ptr only | CERTAIN |
| `Interference::operator bool` | `0x74d50` / `0x37ad0` | ~12 B | `papX && papY` | CERTAIN |
| `RaceChecker::RaceChecker(Module&,bool)` | `0x762b0` / `0x25b42` | ~1.4 K | ctor: walk funcs→BBs, `+= check(BB)` | HIGH |

---

## The Overlap Geometry (shared libBIR symbol)

### Purpose

`doAccessesOverlap` answers the spatial half: *do these two access patterns' byte footprints intersect?* It is conflict-agnostic (`interfere` supplies read/write polarity) and it is **shared** — the same body backs the inter-core barrier checker and the libBIR write-in-place verifier. The geometry must be exact: a false negative misses a real race; a false positive aborts a correct compile.

> **NOTE —** `bir::PhysicalAccessPattern::doAccessesOverlap` is a `UND` import in `libBIRRacecheck.so` (`nm -D` shows `U _ZNK3bir21PhysicalAccessPattern17doAccessesOverlapERKS0_`; the IDA "body" @ `0xf5a58` is the extrn placeholder, reached via PLT `0x383f0 jmp cs:off_DD9E0`). The real body is `libBIR.so 0x3add90` (`T` in `nm -D`; per-symbol sidecar frame `0x17f270`). The kind-mux dispatcher `bir::doAccessesOverlap(AP&, AP&, int)` is at `0x20fd10`/`0x175140`. This is the shared-symbol claim, verified against both binaries.

### Algorithm — decision tree

```c
// bir::PhysicalAccessPattern::doAccessesOverlap(const PhysicalAccessPattern& other)  // libBIR 0x3add90
//   field shorthand (the BIR AP layout): location@+0x38, Dtype@+0x30,
//   Pattern.data@+0x50 ([0].step,[+8]=[0].num), offset@+0xD0, DynamicAPINFO*@+0x1D8.
function doAccessesOverlap(this, other) -> bool:
    // Step 0 — FAST BAILOUT (cheap, sound bounding box)
    if qIntersectFastBailout(this, other):  return false   // provably disjoint

    // Step 1 — SAME MemoryLocation + identical indirect access ⇒ no self-conflict
    if this.location == other.location:
        if this.dynInfo and other.dynInfo
           and indirect actual_ap lattices + aff_expr extents agree element-wise:
            return false                                   // identical access claims no race

    // Step 2 — cross-location OR not-both-indirect-equal ⇒ INTERVAL-SET path
    if !feature_gate(unk_91C1B8) or !hasSameDynamicOffset(this,other) or loc_a != loc_b:
        A = getAddressIntervals(this,  !isDRAM(this),  0)  // boost::icl right-open set
        B = getAddressIntervals(other, !isDRAM(other), 0)
        if |A|==1 or |B|==1:  return Rb_tree.find(single interval) != end
        if |A| >= 0x8000 or |B| >= 0x8000:  return icl::intersects(A, B)
        goto LABEL_9                                       // both ≤0x7FFF → lattice path

    // Step 3 — same buffer + same dynamic offset ⇒ indirect partition range + interval
    if memType(loc) != DRAM:
        if partition ranges [pA, pA+num-1] and [pB, pB+num-1] disjoint:  return false
    return interval_overlap(indirect A, indirect B)

LABEL_9:  // STRIDED LATTICE FORK
    if isShiftedPartitionAccess(this) or isShiftedPartitionAccess(other):
        return icl::intersects(A, B)                       // conservative: exact byte-set hull
    return APOverlapModularDFSMethod(this, other)          // EXACT GCD/DFS lattice test
```

The fast bailout `qIntersectFastBailout` @ `0x3a21e0` is the only bounding-box test: `(buffers can't touch)` OR `(partition ranges disjoint)`. `MemoryLocation::overlaps` @ `0x33a890` collapses SBUF/DRAM to a flat byte interval and tests PSUM as a `{bank} × {partition} × {byte}` rectangle; the partition span uses each AP's effective base partition `base + ⌊offsetBytes / perPartitionBytes⌋`. The bailout is sound — any true overlap survives it.

### Algorithm — the Diophantine DFS (the headline)

When both interval sets are small (`≤0x7FFF`) and the buffers share storage, and neither access shifts diagonally across partition rows (`isShiftedPartitionAccess` @ `0x2030f0`: `dtype·Pattern[0].step mod perPartitionBytes != 0`), the engine runs the **exact** analytic test. The question is a Diophantine reachability: *do the two affine byte-lattices share an integer point?*

```c
// bir::PhysicalAccessPattern::APOverlapModularDFSMethod(this, other)   // libBIR 0x3a9540 / 0x17d4c0
function APOverlapModularDFSMethod(this, other) -> bool:
    addrA = getAddress(loc_a) + (dtype_a * offset_a) mod perPart_a
    addrB = getAddress(loc_b) + (dtype_b * offset_b) mod perPart_b
    d = addrB - addrA                                      // signed residual to solve for
    if dtype_a == dtype_b and all of {d, addrA, addrB} divisible by dtype:
        divide everything by dtype                         // work in element units, tightens GCD
    // combined difference lattice: this's strides as +dtype_a*step_i,
    //                              other's as -dtype_b*step_j, each with its num bound
    strides = build_and_sort_by_abs_desc(this, other)      // heapify 0x3a08d0 + insort 0x3a0750
    g[axis] = binary_GCD(strides)                          // Stein: tzcnt/shift/subtract loop
    if d mod gcd(all strides) != 0:  return false          // GCD divisibility prune
    return APOverlapDFSHelper(dim=0, residual=d, strides, num, g)

// bir::PhysicalAccessPattern::APOverlapDFSHelper(...)      // libBIR 0x3a8e20 / 0x181700
//   memoized DFS over (dim, residual); memo = MapVector<tuple<u64,i64>, bool>.
function APOverlapDFSHelper(dim, d):
    if memo has (dim, d):  return memo[(dim, d)]
    if dim == num_dims:                                    // base case
        return (1 - num_inner) <= d < stride_inner         // residual lands in innermost run
    s = stride[dim]; n = num[dim]; g = gcd[dim]
    if d mod g != 0:  return memoize(false)                // per-axis residual infeasible
    kRange = [ ceil((d - (num_other-1)) / s) , floor(d / s) ]  // sign-correct, clamp to [0, n-1]
    for k in kRange:
        if APOverlapDFSHelper(dim+1, d - k*s):  return memoize(true)
    return memoize(false)
```

The result is exact: it returns true iff there exist integer index vectors (`kᵢ` in `this`, `mⱼ` in `other`) with `Σ kᵢ·dtype_a·step_i − Σ mⱼ·dtype_b·step_j = d`, with each index in its `[0, num)` bound. This is precisely the gather/strided-DMA resolver: two regularly-strided accesses can interleave their footprints without ever sharing a byte — a bounding box false-positives that, an exact byte-set enumeration over-allocates on it, and the GCD-pruned DFS decides it in polynomial time (the memo on `(dim, residual)` collapses the recursion).

> **QUIRK —** the GCD test and the DFS do different jobs and a reimplementation needs both. The **binary GCD** (Stein's algorithm, the inline `tzcnt`/shift/subtract loop) is the *pruning gate*: if the residual `d` is not a multiple of `gcd(strides)`, no solution exists and the search never starts. The **DFS** is the *exact membership* test that runs only after the GCD admits feasibility. Skipping the GCD makes the DFS correct but slow; skipping the DFS and trusting the GCD alone is *unsound* — divisibility is necessary, not sufficient, because the per-axis `[0, num)` bounds can still exclude every solution.

### Function Map (libBIR — shared)

| Function | Addr (body / sidecar) | Size | Role | Confidence |
|---|---|---|---|---|
| `PhysicalAccessPattern::doAccessesOverlap` | `0x3add90` / `0x17f270` | 1347 B | the decision tree | HIGH |
| `doAccessesOverlap(AP,AP,int)` dispatcher | `0x20fd10` / `0x175140` | 117 B | kind-mux Phys/Sym | CERTAIN |
| `qIntersectFastBailout` | `0x3a21e0` / `0x177190` | 287 B | partition×byte rectangle bailout | HIGH |
| `MemoryLocation::overlaps` | `0x33a890` | 963 B | per-type byte/partition/bank rectangle | HIGH |
| `getAddressIntervals` (cached) | `0x3a80c0` / `0x17a790` | 3065 B | boost::icl interval-set builder | HIGH |
| `getAddressIntervals` (free) | `0x20a380` / `0x17b570` | 1805 B | dim-merge + recursive emitter | HIGH |
| `isShiftedPartitionAccess` | `0x2030f0` / `0x179de0` | 122 B | partition-stride-not-multiple guard | HIGH |
| `APOverlapModularDFSMethod` | `0x3a9540` / `0x17d4c0` | 2622 B | binary-GCD + DFS setup | HIGH |
| `APOverlapDFSHelper` | `0x3a8e20` / `0x181700` | 1811 B | memoized Diophantine DFS | HIGH |
| `boost::icl::intersects(set,set)` | `0x22bfa0` | 484 B | nonempty set-intersection | HIGH |

---

## The Pass Wiring

### Purpose

`bir_racecheck` is the libwalrus `BackendPass` that runs the engine. It is flag-gated and OFF by default — the opposite of the always-on, re-interposed `birverifier` — and runs exactly once, late, after the schedule and register allocation are final so that the cross-engine sync edges it consumes are stable.

### Algorithm — the BackendPass

```c
// neuronxcc::backend::BirRaceCheck::run(Module& mod)     // libwalrus 0x11160b0
function run(mod):
    push boost-log "module_name" = mod.getName()
    RaceChecker rc(mod, /*collectAll=*/true)              // IMPORTED ctor; walks funcs→BBs→check
    if rc.raceCount != 0:                                 // rc+0x24 (unused collect path)
        rc.printRaces()                                   // std::clog dump
        throw runtime_error(to_string(count))
    // ⇒ in practice the first race throws inside check()'s TBB body before this point
```

The `RaceChecker` ctor (`0x762b0`) walks `mod.functions → basicBlocks` and accumulates `raceCount += check(bb)`. Because `check`'s TBB body throws on the first overlapping concurrent pair, the live abort is the `"Data race detected:\n…"` from the engine, not the ctor's count-and-`printRaces` fallback — the latter is a non-throwing collect-all mode that the wired flow never reaches. That collect path is present in the class API but appears dead in this build.

### Related Knobs

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `enableDataRaceChecker` | `cl::opt<bool>` | **false** | CLI `--enable-data-race-checker` @ `0x1dc319b`, desc `0x1dc3182`; registrar `sub_7C2890` @ `0x7c66a5`, `initializer(false)` @ `0x7c6668` |

### Considerations

The pass is added in the pre-codegen pipeline builder `sub_80A6E0` @ instruction-index 1786 (`if (enableDataRaceChecker && unk_3DF7B18) addModParallelPass("bir_racecheck", 1)`), the last pass before the low-level/codegen pipeline, right after `lower_control`, `lnc_barriercheck`, and `dep_reduction`. A complementary low-level site `sub_805870` @ 514 adds it under the inverse gate `!unk_3DF7B18` as a core-parallel pass; the bss flag `unk_3DF7B18` selects which stage hosts the single run. Both are post-schedule and post-RA — which is *why* racecheck consumes the scheduled, semaphore-synchronized dependency graph rather than re-deriving synchronization from `SyncInfo` values.

The race report has two layers. The live throw builds `"Data race detected:\n"` (`0xab0f8`) + `Interference::toString()` (`0x76e30`), where each PAP line is `"<W|R>: <buffer> By: <owner>"` (`toString(PAP,Type)` @ `0x76960`; `<buffer>` = the conflicting `MemoryLocation` name, `<owner>` = the PAP-owner string, separator `" By: "` @ `0xab5fb`). The structured `printRaces` dump (`0x772c0`, to `std::clog`: `"Race N:"` @ `0xab601` / `"instructions:"` @ `0xab607` / `"data interference:"` @ `0xab615`) iterates a result `DenseMap` that the throwing path never populates — dead in the wired flow.

### Function Map (libwalrus — pass)

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `BirRaceCheck::run(Module&)` | `0x11160b0` | construct `RaceChecker(mod,true)` → throw on race | CERTAIN |
| `BirRaceCheck::BirRaceCheck(PassOptions&)` | `0x1115d10` | ctor: Logger + `PassOptions@+0x60`; vt `off_3D94A28` | CERTAIN |
| `register_generator_bir_racecheck__` (invoke) | `0x1116460` | `tc_new(104)` → ctor → name `"bir_racecheck"` | CERTAIN |
| `sub_80A6E0` (pre-codegen builder) | `0x80a6e0` | adds `bir_racecheck` @ 1786 (gated) | CERTAIN |
| `sub_805870` (low-level builder) | `0x805870` | adds `bir_racecheck` @ 514 (alt gate) | CERTAIN |
| `sub_7C2890` (cl::opt registrar) | `0x7c2890` | registers `--enable-data-race-checker` (default 0) | CERTAIN |

---

## Related Components

| Name | Relationship |
|---|---|
| `lnc_barriercheck` ([8.43](barriercheck.md)) | the **inter-core** counterpart; uses barrier-range disjointness for the temporal half and `boost::icl::interval_set` intersection (`getPartitionIntervals`/`getAddressIntervals`) for the spatial half — **not** `doAccessesOverlap` on the inter-core `compareTwoAPs` path (that symbol is used in the barrier-check `.so` only by `InstGraph::drawHBsPerBB`, the intra-BB happens-before builder). Both checkers do link the libBIR `doAccessesOverlap` body, but racecheck (8.42) is the page where it is the spatial engine; see [8.43](barriercheck.md) for the geometry divergence. |
| Dependence Graph (`build_fdeps` / `anti_dependency_analyzer`) | builds the `EdgePtr` dependency edges racecheck untags (`& ~7`) and consumes as the sync graph |
| `birverifier` (Strand G/L1) | the structural BIR verifier; always-on and re-interposed, contrasting racecheck's single flag-gated late run |
| libBIR write-in-place verifier | third caller of the shared `doAccessesOverlap` overlap engine |

## Cross-References

- [The Dependence Graph](dependence-graph.md) — the `EdgePtr` `PointerIntPair` and the post-synchronizer dependency edges racecheck reconstructs the clock from
- [Execution & Sync Model — Semaphores & Barriers](../arch/execution-sync-model.md) — the `EngineType` enum and the semaphore/barrier model whose edges drive the vector clock
