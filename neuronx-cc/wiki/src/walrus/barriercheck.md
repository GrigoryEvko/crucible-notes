# barriercheck — Inter-Core Missing-Barrier Checker

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 share the `.text` logic). The barrier-range analysis (`BarrierRanger`), the disjointness predicate (`checkBarrierRangeIntersect`), and the race driver (`MissingBarrierChecker`) live in `neuronxcc/starfish/lib/libLncBarriercheck.so` (md5 `fc6e133cb4763d8b584997bb4551d514`, 33,384,000 B; `.text` base `0x98e20`, `.rodata` base `0x366000`, both VA == file offset). The 14 LNC legality assertions (`LncVerifier`) live in `neuronxcc/starfish/lib/libwalrus.so` (md5 `1d93972b81e619ce6d178a0e4b9003b3`; `.text` base `0x62d660`, `.rodata` base `0x1c72000`, VA == file offset). The AP geometry primitives (`getPartitionIntervals`, `getAddressIntervals`, `doAccessesOverlap`) and the `optional<NeuronCoreId>` accessor are externs resolved from `libBIR.so`. The standalone `.so` is a packaging duplicate of the same `LncBarrierCheck` class that also lives in `libwalrus.so`. Other wheels differ — treat every address as version-pinned.*

## Abstract

Under **LNC** (Logical Neuron Core) sharding a kernel is split across `lnc_size` cooperating cores, each its own `bir::Module` (see [`lnc_splitter`](lnc-splitter.md), 8.30). Two cores that touch the same shared buffer with no `CoreBarrier` provably between them are an inter-core data race. This page is the checker that proves the barriers are present. It is the **cross-core twin** of the intra-core racecheck ([8.42](racecheck.md)): racecheck orders ops on one core with semaphore vector-clocks; barriercheck orders ops *across* cores with `CoreBarrier`-rendezvous **index ranges**. Both share the `PhysicalAccessPattern` geometry and nothing else.

The checker has three pieces, in pipeline order. First, **`BarrierRanger`** assigns every instruction, per engine, an integer band `[first, second]` = `[latest preceding CoreBarrier index, earliest following CoreBarrier index]` — the inter-barrier *epoch* the instruction lives in. It computes the band with two monotone dataflow sweeps over a per-engine happens-before DAG whose `CoreBarrier` (`InstructionType == 0x57 == 87`, see [Opcode Master](opcode-master.md), 8.33) nodes carry sequential indices. Second, **`checkBarrierRangeIntersect`** is the disjointness predicate: two ops are barrier-separated on an engine iff their bands are disjoint (`hi1 <= lo2 || lo1 >= hi2`); a missing-barrier race is flagged only when the bands **overlap on every engine**. The `compareTwoAPs` driver gates this with cross-core-ID, MemoryLocationSet, and Boost.ICL partition+address interval filters first. Third, the **`LncVerifier`** pass (the `lnc_verifier` BackendPass) is the structural-legality gate that runs *before* the race check: 14 `NeuronAssertion`s (ErrorCodes 1187–1203) proving the per-core module set is a legal, consistent split — the same shard-id count, barrier inventory, and shared-tensor map on every core.

Keep the two layers separate. `LncVerifier` answers *"are the per-core modules a legal split?"* — shape, shard-ids, barrier count/id/name, remote-target resolution. `BarrierRanger` + `checkBarrierRangeIntersect` answer the harder dynamic question *"given a legal split, are the barriers sufficient to prevent races?"*. The 14 assertions are the headline of the first; the band model and the disjointness test are the headline of the second.

For reimplementation, the contract is:

- The **`[first, second]` band model**: `first` = latest `CoreBarrier` ancestor index (MAX over the descendant sweep), `second` = earliest `CoreBarrier` descendant index (MIN over the ancestor sweep), invariant `first <= second`. Despite the names, the *latest* sweep fills the low bound and the *earliest* sweep fills the high bound.
- The **two monotone sweeps**: seed every node to the full band `[minId, maxId]`, pin each `CoreBarrier` node to its own index, then relax `.first` upward along successor edges (MAX) and `.second` downward along predecessor edges (MIN) to a fixpoint.
- The **disjointness predicate** `hi1 <= lo2 || lo1 >= hi2` per engine, the same-core early-exit (`crossCoreId(A) == crossCoreId(B) ⇒ safe`), and the separate `GPSIMDSB2SB` cross-core ID space.
- The **14 LNC assertions** (codes 1187–1203): 3 in the symbolic pre-split checker, 11 in the concretized post-split checker, each with its predicate and ErrorCode.

| | |
|---|---|
| **Race driver** | `MissingBarrierChecker::collectRacesPerFunction` @ `0x101770` (7687 B, libLncBarriercheck) |
| **Per-pair driver** | `BarrierCheckInstListAccessInfo::compareTwoAPs` @ `0xff6d0` |
| **Disjointness predicate** | `…::checkBarrierRangeIntersect` @ `0xfcfa0` (3957 B) |
| **Band analysis** | `BarrierRanger::computeBarrierRange` @ `0xf08b0` |
| **Two sweeps** | latest (backward) `0xee610`, earliest (forward) `0xeed60` |
| **ICL intersection** | `boost::icl::intersects<interval_set<u64,…>>` @ `0x10a840` |
| **Structural gate** | `LncVerifier::run(vector<Module>&)` @ `0x167b620` (libwalrus) |
| **Symbolic checker** | `checkSubgraphsWithSymbolicShardIds` @ `0x16762c0` — codes 1200/1201/1202 |
| **Concretized checker** | `checkSubgraphsWithConcretizedShardIds` @ `0x16772a0` — codes 1187–1199, 1203 |
| **CoreBarrier opcode** | `InstructionType == 0x57 == 87` (`Inst+0x58`); `GPSIMDSB2SB == 0x21 == 33` |
| **LNC gate knob** | `PassOptions+0x1A4` = `lnc_size` (default 1; `!= 1` ⇒ multi-core path) |

---

## BarrierRanger — the `[first, second]` band analysis

### Purpose

`BarrierRanger` is the producer of the per-(instruction, engine) barrier-index band that the disjointness test consumes. The band `[first, second]` is the static stand-in for "which inter-barrier epoch the instruction executes in": `first` is the index of the **latest `CoreBarrier` it is guaranteed to have passed**, `second` is the index of the **earliest `CoreBarrier` it is guaranteed to reach**. Two instructions whose bands are disjoint are provably separated by at least one `CoreBarrier`. The analysis never simulates execution — it derives the band purely from reachability over a happens-before DAG.

The object is essentially stateless. The ctor `BarrierRanger(logging::Logger&)` @ `0xedb80` copies only a `boost::log` composite logger into `+0..+48`; there are no working maps on the object. Every data structure the algorithm touches — the `nodeToBarrierRange` map, the `instBarrierRange` map, the per-engine `CoreBarrier`-node seed set, the worklist deque — is passed by reference or lives on the `InstGraph`. `BarrierRanger` is a thin, log-emitting analysis driver. (CONFIRMED — ctor body copies a Logger only; all `*PerEngine` functions take maps by reference.)

> **CORRECTION (D-G08) —** an earlier survey (S2-05 §5.2) cited the `*PerEngine`/`update`/`init`/`print`/`save` functions at addresses `0x9xxxx` (`0x98d30`, `0x963c0`, `0x97540`, `0x98870`, `0x977e0`, `0x98b60`, `0x98a90`, `0x98270`). Those are **PLT stubs** (`jmp *GOT(%rip)`); e.g. `0x963c0` disassembles to `jmp *0x1f3be02(%rip)` → GOT `0x1fd21c8`. The real `.text` bodies are the un-dotted `_Z…` symbols: `init 0xf44e0`, `earliest 0xeed60`, `latest 0xee610`, `perEngine 0xf4b40`, `updStart 0xee2d0`, `updEnd 0xeea20`, `save 0xf0140`. The orchestrator `0xf08b0` and per-engine driver `0xf4b40` in that survey were correct. The same survey also had the band swapped: it is `[latest-preceding, earliest-following]`, **not** `[earliest-preceding, latest-following]`.

### Entry Point

```text
MissingBarrierChecker::collectRacesPerFunction (0x101770)   ── TBB per-function cross-core race scan
  └─ BarrierRanger::computeBarrierRange (0xf08b0)            ── TOP orchestrator (TBB parallel over engines)
       ├─ (per engine, parallel) sub_F5A90                  ── TBB task body
       │    └─ BarrierRanger::computeBarrierRangePerEngine (0xf4b40)
       │         ├─ initializeBarrierRangePerEngine (0xf44e0)        ── SEED: all nodes = [minId,maxId]
       │         ├─ computeLatestBarrierRangePerEngine (0xee610)     ── BACKWARD sweep → .first (MAX)
       │         └─ computEarliestBarrierRangePerEngine (0xeed60)    ── FORWARD sweep → .second (MIN)
       └─ saveInstToInstToBarrieRangeMap (0xf0140)          ── fold per-engine ranges → cross-engine result
```

### Algorithm

The per-engine driver runs the seed pass, then the two sweeps; note the **latest (backward) sweep runs before the earliest (forward) sweep**. Both read the same per-engine `CoreBarrier`-node seed set (`InstGraph::EngineToCoreBarrierNodes` at `InstGraph+0x38`) and the same `nodeRange`/`instRange` maps; they are independent (one writes `.first`, the other `.second`).

```c
// BarrierRanger::computeBarrierRangePerEngine @ 0xf4b40
function computeBarrierRangePerEngine(InstGraph* G, string& engineName, bool useDefault):
    if useDefault:  (minId, maxId) = (0, 2)            // default 2-barrier band
    else:           (minId, maxId) = (G.base.first, G.base.second)   // = [startId, startId+1]
                    assert maxId > minId               // ErrorCode 1164, BarrierRanger.cpp:264
    LOG "Initializing barrier range for " << engineName << " with " << minId << ", " << maxId

    initializeBarrierRangePerEngine(G, engineName, nodeRange, instRange, minId, maxId)  // SEED

    LOG "Computing latest barrier range starts for " << engineName       // .rodata 0x3772f0
    computeLatestBarrierRangePerEngine(G, engineName, nodeRange, instRange)    // BACKWARD, .first MAX
    LOG "Computing earliest barrier range starts for " << engineName     // .rodata 0x377350
    computEarliestBarrierRangePerEngine(G, engineName, nodeRange, instRange)   // FORWARD, .second MIN
```

```c
// BarrierRanger::initializeBarrierRangePerEngine @ 0xf44e0  — SEED pass (BFS over all engine nodes)
function initializeBarrierRangePerEngine(G, engineName, nodeRange, instRange, minId, maxId):
    for node in BFS(G.engineNodes[engineName]):           // deque worklist, follow successors +64/+80
        nodeRange[node] = (minId, maxId)                  // widest band
        if node.isCoreBarrier:                            // node+0x32 (50) flag
            nodeRange[node] = (node.barrierIndex, node.barrierIndex)   // node+0x34 (52), both halves
        if node.inst != null:                             // node+0 = bir::Instruction*
            instRange[node.inst] = nodeRange[node]         // seed the per-inst mirror

// BarrierRanger::computeLatestBarrierRangePerEngine @ 0xee610  — BACKWARD, fills .first (LOW bound) via MAX
function computeLatestBarrierRangePerEngine(G, engineName, nodeRange, instRange):
    worklist = deque(G.coreBarrierNodes[engineName])      // EngineToCoreBarrierNodes @ G+0x38
    while worklist:
        n = worklist.pop_front()
        for s in n.SUCCESSORS:                            // n+0x40 ptr / n+0x50 size  (downstream)
            if nodeRange[n].first > nodeRange[s].first:    // MAX-relaxation
                nodeRange[s].first = nodeRange[n].first     // raise .first upward
                updateInstBarrierRangeStartId(s, nodeRange, instRange)   // mirror to inst map
                worklist.push_back(s)
    // monotone: .first only INCREASES, bounded above → fixpoint

// BarrierRanger::computEarliestBarrierRangePerEngine @ 0xeed60  — FORWARD, fills .second (HIGH bound) via MIN
function computEarliestBarrierRangePerEngine(G, engineName, nodeRange, instRange):
    worklist = deque(G.coreBarrierNodes[engineName])
    while worklist:
        n = worklist.pop_front()
        for p in n.PREDECESSORS:                          // n+0x58 ptr / n+0x68 size  (upstream)
            if nodeRange[n].second < nodeRange[p].second:  // MIN-relaxation
                nodeRange[p].second = nodeRange[n].second   // tighten .second downward
                updateInstBarrierRangeEndId(p, nodeRange, instRange)
                worklist.push_back(p)
    // monotone: .second only DECREASES, bounded below → fixpoint
```

> **QUIRK —** the sweep names invert the bound they fill. The **latest** sweep relaxes `.first` to a MAX along *successor* edges; the **earliest** sweep relaxes `.second` to a MIN along *predecessor* edges. Reading the names literally and wiring the latest sweep to the high bound produces an inverted band and silently breaks every disjointness test. The invariant that makes it self-consistent is `first <= second`: an instruction's band is `[latest-ancestor-CB, earliest-descendant-CB]` with `lo <= hi`. The "forward/backward reachable *from* coreBarriers" debug-dump names (`0xf2380`/`0xf3430`) describe the *graph-walk direction from a barrier*, not the bound.

The node→inst mirror (`updateInstBarrierRangeStartId` @ `0xee2d0`, `updateInstBarrierRangeEndId` @ `0xeea20`) copies the changed half into the per-`Instruction*` map and asserts the instruction was seeded (`instBarrierRangeMap.find(instNode->inst) != …end()`, ErrorCode 1162 at `:89` / 1163 at `:106`). The `.first`-only vs `.second`-only split (`+0` vs `+4` of the packed `pair<u32,u32>`) is what keeps the two sweeps independent.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `BarrierRanger::BarrierRanger(logging::Logger&)` | `0xedb80` | ctor — copies a Logger into `+0..+48`; no working state | CERTAIN |
| `BarrierRanger::computeBarrierRange(InstGraph&, map&)` | `0xf08b0` | TOP orchestrator; TBB parallel over engines | CERTAIN |
| `BarrierRanger::computeBarrierRangePerEngine` | `0xf4b40` | per-engine driver: init → latest → earliest | CERTAIN |
| `BarrierRanger::initializeBarrierRangePerEngine` | `0xf44e0` | seed all nodes to `[minId, maxId]`, pin CB nodes | CERTAIN |
| `BarrierRanger::computeLatestBarrierRangePerEngine` | `0xee610` | backward sweep → `.first` (MAX over successors) | CERTAIN |
| `BarrierRanger::computEarliestBarrierRangePerEngine` | `0xeed60` | forward sweep → `.second` (MIN over predecessors) | CERTAIN |
| `BarrierRanger::updateInstBarrierRangeStartId` | `0xee2d0` | node `.first` → inst `.first` mirror (Assert 1162) | CERTAIN |
| `BarrierRanger::updateInstBarrierRangeEndId` | `0xeea20` | node `.second` → inst `.second` mirror (Assert 1163) | CERTAIN |
| `BarrierRanger::saveInstToInstToBarrieRangeMap` | `0xf0140` | fold per-engine ranges into cross-engine result map | CERTAIN |
| `InstGraph::addHB(InstNode*, InstNode*, reason)` | `0xcaf30` | edge: `src+0x40 ← dst` (succ), `dst+0x58 ← src` (pred) | CERTAIN |
| `BarrierRangeResults::BarrierRangeResults(…, bool)` | `0xf8680` | packages the result maps for the consumer | HIGH |

### Considerations

The `CoreBarrier` index counter is per-engine, held on the `InstGraph` not on `BarrierRanger`. `InstGraph::initializeBarrierRangeStartIdAndOffset(startId, offset)` @ `0xc7490` sets `this+0 = startId`, `this+4 = startId+1` (the base pair `[startId, startId+1]` — a one-barrier-wide initial window) and `this+424 = offset`. Each `CoreBarrier` op on the engine then owns a sequential index, and the final index → `InstCoreBarrier*` map lives on `BarrierRangeResults` @ `0xf8680` as a nested `module → engine → barrierIdx → InstCoreBarrier*`. The `coreBarrierPossibleEngines` string-set names which engines a given barrier applies to (asserted present in `drawHBsPerBB`).

The DAG edge direction is the trap. `addHB(src, dst)` @ `0xcaf30` inserts `dst` into `src`'s successor set at `src+0x40` and `src` into `dst`'s predecessor set at `dst+0x58` (raw: `lea 0x40(%r15),%rsi` then `lea 0x58(%rax),%rsi` near `0xcb5b5`/`0xcb5ce`). The successor/predecessor offsets `+0x40`/`+0x58` are exactly what the two sweeps walk; swapping them inverts ancestor/descendant and the band model collapses. The `InstNode` flag triplet `+0x30`/`+0x32`/`+0x33` (`isDMATrigger`/`isCoreBarrier`/`isDummyCoreBarrier`) and how `+0x34` `barrierIndex` is assigned are `InstGraph`-build territory (8.33 and the racecheck/InstGraph deep-dive); only their read semantics are pinned here.

---

## checkBarrierRangeIntersect — the disjointness predicate

### Purpose

`BarrierCheckInstListAccessInfo::checkBarrierRangeIntersect` @ `0xfcfa0` (3957 B; dynsym-confirmed export) decides whether a `CoreBarrier` provably separates two instructions. It returns **1 if their barrier-ranges intersect on *some* engine** (no guaranteed barrier ⇒ unsafe) and **0 if they are disjoint on *every* engine, or are on the same core** (safe). It is the temporal half of the conflict test; the spatial half is the Boost.ICL overlap done earlier by `compareTwoAPs`.

### Algorithm

```c
// BarrierCheckInstListAccessInfo::checkBarrierRangeIntersect @ 0xfcfa0
// args: instA(rdi), instB(rsi),
//       frEng (rdx) = unordered_map<string, pair<u32,u32>>  instA's per-engine [lo,hi]
//       srEng (rcx) = unordered_map<string, pair<u32,u32>>  instB's per-engine [lo,hi]
//       ccId1 (r8)  = unordered_map<Instruction*, u32>       general cross-core ID space
//       ccId2 (r9)  = unordered_map<Instruction*, u32>       GPSIMDSB2SB cross-core ID space
function checkBarrierRangeIntersect(instA, instB, frEng, srEng, ccId1, ccId2) -> bool:
    // (1) SAME-CORE early exit
    if !isGPSIMDSB2SB(instA) || !isGPSIMDSB2SB(instB):     // IT != 0x21 at Inst+0x58
        if ccId1[instA] == ccId1[instB]: return 0          // 0xfd604 cmp ebx,[rax]; jz → same core, SAFE
    else:                                                  // both IT==0x21 (GPSIMDSB2SB)
        if ccId2[instA] == ccId2[instB]: return 0          // same SB2SB sync domain, SAFE

    // (2) PER-ENGINE disjointness loop (walks frEng's hashtable node list)
    for engineName in frEng.keys():
        (lo1, hi1) = frEng[engineName]
        (lo2, hi2) = srEng[engineName]                     // assert found, else ErrorCode @:672
        assert lo1 <= hi1                                  // 0xfd1e7 cmp; ja → @:676
        assert lo2 <= hi2                                  // 0xfd1fd cmp; ja → @:679
        if hi1 <= lo2 || lo1 >= hi2:                       // *** DISJOINT on this engine ***
            return 0                                       // 0xfd20d jbe / 0xfd215 jae → a barrier separates, SAFE
        // else: ranges overlap here, advance to next engine
    return 1                                               // 0xfd264 mov $0x1 → overlap everywhere, MISSING BARRIER
```

The disjointness test is the standard half-open-interval predicate for `[lo1,hi1)` vs `[lo2,hi2)`: disjoint iff `hi1 <= lo2` (A entirely before B) or `lo1 >= hi2` (A entirely after B). Both branches are byte-confirmed in the binary: `0xfd20d: cmp %ecx,%eax; jbe 0xfd61a` is `hi1 <= lo2`, `0xfd215: cmp %esi,%edx; jae 0xfd61a` is `lo1 >= hi2`, both falling to the `xor eax,eax` return-0 at `0xfd61a`; the loop-exhausted `mov $0x1,%eax` at `0xfd264` returns 1.

> **GOTCHA —** the predicate returns 1 (unsafe) only when the ranges overlap on **every** engine. The first engine that produces disjointness short-circuits to return 0 (safe). A reimplementation that flags a race on the first overlapping engine over-reports massively: a barrier on *any* engine that separates the two ops is sufficient, so the loop must survive every engine before concluding "missing barrier."

> **NOTE —** `GPSIMDSB2SB` (`InstructionType == 0x21 == 33`, the SB-to-SB cross-core sync primitive) carries a **second cross-core ID space** (`ccId2`). It is itself a synchronization mechanism, so two SB2SB ops in the same sync domain are mutually ordered and exit safe via `ccId2`, not the general `ccId1`. The both-IT33 test is at `0xfd027`/`0xfd676` (`cmpl $0x21,0x58(%rax)`); `ccId2` lookups go through `_Map_base::at` (throws `out_of_range` if absent).

### The three internal asserts vs the user race

The three `NeuronAssertion`s inside `0xfcfa0` are **consistency checks on the range maps**, not the user race report:

| File:line | Predicate | Fires when |
|---|---|---|
| `MissingBarrierChecker.cpp:672` | `secondEngineToBarrierRange.find(engineName) != …end()` | instB has no range for an engine instA has |
| `MissingBarrierChecker.cpp:676` | `firstBarrierRange.first <= firstBarrierRange.second` | `BarrierRanger` produced an inverted band for instA |
| `MissingBarrierChecker.cpp:679` | `secondBarrierRange.first <= secondBarrierRange.second` | inverted band for instB |

All three are built with `fmt::v10` named args `{engineName, instName, startId, endId}` and the resolution token `RESOLUTION_CONTACT_SUPPORT` — they surface as contact-support errors, fired only if `BarrierRanger` violated its own invariant. The **actual** missing-barrier finding is a data record, not a string: when the predicate returns 1, `compareTwoAPs` inserts `(instA, instB)` into a de-dup `set<pair<Instruction*,Instruction*>>` and pushes a `RaceInfo` onto the sink. `collectRacesPerFunction` @ `0x101770` aggregates these in a TBB `enumerable_thread_specific<vector<RaceInfo>>`; the user-facing message and ErrorCode are emitted by the `LncVerifier` caller, not here.

---

## compareTwoAPs — the per-pair race driver and the icl intersect

### Purpose

`compareTwoAPs` @ `0xff6d0` is the per-AP-pair driver that decides whether one pair of `PhysicalAccessPattern`s is an inter-core race. The disjointness test is the **last** of three filters, not the conflict test itself: a pair must survive sharding/indirect pruning, share a `MemoryLocationSet`, **spatially overlap** (the icl intersect), and only *then* is the barrier-range test consulted.

### Algorithm

```c
// BarrierCheckInstListAccessInfo::compareTwoAPs @ 0xff6d0
function compareTwoAPs(this, apA, pairA{instA, engToBR_A}, apB, pairB{instB, engToBR_B}) -> bool:
    // (1) PRUNING — suppress structurally-ordered or sharded pairs
    if isSameOutputInstructionFromSameKernel(apA, apB)        // 0xf9d60
       || isAutoShardedDMACopyPair(apA, apB)                  // 0x98c60
       || (!this.indirectMode && (isIndirectKernelAccess(apA) // 0xfa640
                                   || isIndirectKernelAccess(apB))):
        return 0
    // (2) SAME MemoryLocationSet gate (asserts MemoryLocation kind == 4 = DRAM/HBM)
    this.cmpCount++                                            // this+76 stat
    // (3) SPATIAL OVERLAP — INTER-CORE GEOMETRY via boost::icl (NOT doAccessesOverlap)
    if !boost::icl::intersects(apB.getPartitionIntervals(),   // libBIR extern; partition dim
                               apA.getPartitionIntervals()):
        return 0
    isPsum = (partitionKind == 16)
    if !boost::icl::intersects(apA.getAddressIntervals(isPsum, 0),   // right-open byte ranges
                               apB.getAddressIntervals(isPsum, 0)):
        return 0
    this.overlapCount++                                       // this+77 stat
    // (4) TEMPORAL — barrier-range separation
    if !checkBarrierRangeIntersect(instA, instB, engToBR_A, engToBR_B,
                                   this.crossCoreId1, this.crossCoreId2):
        return 0                                              // a barrier separates → SAFE
    // (5) RECORD the missing-barrier race
    insert (instA, instB) into reportedPairs set (this+624, deduped)
    if this.raceSink (this+84): raceSink.push_back(RaceInfo{instA, instB, …})
    return 1
```

> **QUIRK —** the inter-core spatial test does **not** use `bir::doAccessesOverlap`. It uses two `boost::icl::interval_set` intersections — `getPartitionIntervals()` (partition coverage) then `getAddressIntervals()` (right-open byte ranges). `doAccessesOverlap` @ `0x96370` is referenced exactly once in this `.so`, by `InstGraph::drawHBsPerBB` (intra-basic-block happens-before edge construction), and never on the inter-core `compareTwoAPs` path. The two checkers diverge on overlap **geometry**, not only on the temporal model: racecheck (8.42) uses `doAccessesOverlap`, barriercheck uses icl intersection. The icl symbol is a real exported template instantiation `boost::icl::intersects<interval_set<unsigned long, …>>` @ `0x10a840`.

### AP bucket classification

Each instruction's access patterns are sorted by `classifyAP` @ `0xffeb0` into 6 primary buckets (read/write × local/remote/dram), keyed on `MemoryType` ({8 = SBUF/local, 16 = PSUM/allocated}), `MemoryAddressSpace`/`TensorKind` ({0,1,4,5,6,7}), `isAllocated`, and the remote target (via `getRemoteMemLoc` @ `0xfc580`). `detectRaces` @ `0xffcf0` then cross-tests the buckets — 9 `detectRacesBetweenMemPAPInfo` over the memory-keyed pairs plus 12 `detectRacesBetweenPAPInfo` over the AP-vector buckets — each terminating in a `compareTwoAPs` call. The matrix always has a writer on at least one side of every pair: the cross-core analogue of racecheck's WAW/WAR/RAW (a race needs ≥1 writer). The exact bucket-pair enumeration is reverse-engineered at HIGH confidence and not reproduced row-by-row here.

### Pruning rationale

The pruning helpers encode the cases where **no barrier is needed** between two cross-core accesses to the same buffer:

| Helper | Addr | Why no barrier is needed |
|---|---|---|
| `isSameOutputInstructionFromSameKernel` | `0xf9d60` | same kernel, same output — already ordered by program structure |
| `isAutoShardedDMACopyPair` | `0x98c60` | the two halves of one auto-sharded DMA copy — disjoint shards by construction |
| `isIndirectKernelAccess` | `0xfa640` | indirect/gather kernel; data-dependent footprint — conservatively exempt unless `indirectMode` |
| `isDynamicDMAWithTensorizerId` | (roster) | dynamic-offset DMA — same conservative exemption |
| `getRemoteMemLoc` | `0xfc580` | resolves a remote (other-core) `MemoryLocation` for the remote buckets |

---

## LncVerifier — the 14 LNC legality assertions

### Purpose

`LncVerifier` (the `lnc_verifier` BackendPass, in `libwalrus.so`) is the **structural-legality gate** for the LNC split. Where the barrier-range checker proves dynamic sufficiency, `LncVerifier` proves the per-core module set is a *consistent split at all*: the same shard-id count, the same control-flow skeleton, the same barrier inventory, and the same shared-tensor map on every core. It is the correctness oracle for [`lnc_splitter`](lnc-splitter.md) (8.30) and the structural sibling of the barrier-range race check.

Two `run` overloads exist; only one does work. `run(bir::Module&)` @ `0x1674860` is a no-op shim (sets status field = 2, formats an empty result). `run(vector<unique_ptr<Module>>&)` @ `0x167b620` is the real pass, invoked by the multi-module dispatch with the per-core module vector. The top gate reads `lnc_size` off the stored `PassOptions`:

```c
// LncVerifier::run(vector<unique_ptr<Module>>&) @ 0x167b620
function run(modules):
    if (this.PassOptions->lnc_size != 1):       // 0x167b637 mov 0x28(%rsi),%rax; 0x167b63b cmpl $1,0x1a4(%rax)
        if inputModulesHaveSymbolicShardIds(modules):       // 0x1674f00
            checkSubgraphsWithSymbolicShardIds(modules)       // PRE-split  → codes 1200/1201/1202
        else:
            sgId = bir::getSubgraphId(modules[0])
            checkSubgraphsWithConcretizedShardIds(modules, sgId)  // POST-split → 1187-1199, 1203
    status = 0; formatResult()
```

The branch key is `getNeuronCoreId().has_value()`: `inputModulesHaveSymbolicShardIds` @ `0x1674f00` returns true iff the vector has exactly one element (`cmp $0x8,%rax` — one `unique_ptr` = 8 B) **and** `modules[0]->getNeuronCoreId()` has no value (`call getNeuronCoreId@plt; xor $0x1,%r8d`). A single module with no concrete core-id is still symbolic (pre-splitter); a multi-module vector, or a module that already carries a `NeuronCoreId`, is concretized.

> **CORRECTION (D-G11) —** an earlier survey read the gate as a Module field `modules[0]+0x1A4 != 1`. The disasm shows `mov 0x28(%rsi),%rax; cmp [rax+0x1A4],1` where `rsi = this` and `this+0x28` is the stored `PassOptions*` (ctor `0x167b730`: `*((QWORD*)this+5) = a2`). The gate reads `PassOptions->lnc_size`, not a Module field. For `lnc_size == 1` (single-core, the default) the whole pass is skipped.

### The 14 assertions

The legality is exactly **14 `NeuronAssertion`s, ErrorCodes 1187–1203**, all in `lnc_verifier.cpp` — **3 in the symbolic checker** (1200/1201/1202) and **11 in the concretized checker** (1187–1199, 1203; note 1190 is not an LNC assert). Each ErrorCode immediate was read from the call-site (`mov $0xNNN,%esi`), each appearing 3× per the standard assert-emit pattern; every predicate string is present verbatim in `.rodata`.

| Code | Checker | `lnc_verifier.cpp` | Predicate (verbatim) | What it guards |
|---|---|---|---|---|
| 1200 | symbolic | `:195` | `func0.getShardIds().size() == 1` | each function carries exactly **one** shard-id symbol |
| 1201 | symbolic | `:208` | `active_barriers == 0 \|\| active_barriers == Options.vnc_nc_count` | a `CoreBarrier` is active on **zero or all** cores — never a partial subset |
| 1202 | symbolic | `:222` | `expected_numsb2sb == sb2sbCounts[lncId]` | uniform `GPSIMDSB2SB` count across all cores |
| 1203 | concretized | `:50` | `modules[ncId]->getNeuronCoreId().has_value()` | every per-core module has a concrete core-id |
| 1199 | concretized | `:52` | `function.getShardIds().empty()` | no residual symbolic shard-id survives concretization |
| 1193 | concretized | `:64` | `modules[0]->size() == modules[ncId]->size()` | identical **#functions** across cores |
| 1191 | concretized | `:80` | `func != nullptr` | every core-0 function exists by name on core `ncId` |
| 1192 | concretized | `:81` | `func->size() == func0.size()` | identical **#BBs** per function across cores |
| 1194 | concretized | `:85` | `all_blocks[0][blockIdx]->getName() == bb.getName()` | identical BB **names** (same order) across cores |
| 1187 | concretized | `:108` | `expectedCBs == coreBarriers[ncId].size()` | identical `CoreBarrier` **count** per core |
| 1198 | concretized | `:116` | `expectedSB2SBs == actualSB2SBs` | identical `GPSIMDSB2SB` count per core |
| 1197 | concretized | `:133` | `memLoc.getRemoteLocalTarget().has_value()` | every shared post-DRAM-alloc memloc on a non-zero core has a concrete remote target |
| 1195 | concretized | `:142` | `targetMemLoc && targetMemLoc->getAddress() == memLoc.getAddress()` | the resolved remote target exists and aliases the same address |
| 1188 | concretized | `:157` | `index == coreBarriers[ncId][index]->getId()` | barrier IDs dense + ordered identically (i-th barrier has id == i) |
| 1189 | concretized | `:164` | `expectedName == coreBarriers[ncId][index]->getName()` | barrier **names** match core 0 at each index |
| 1196 | concretized | `:181` | `sharedTensors[ncId].count(address) > 0` | the **set** of shared-tensor addresses is identical across cores |

### The two checkers

```c
// checkSubgraphsWithSymbolicShardIds @ 0x16762c0 — PRE-SPLIT (shard-id symbolic execution)
function checkSubgraphsWithSymbolicShardIds(module):
    ncount = PassOptions->lnc_size
    sb2sbCounts = new ulong[ncount]{0}
    for func in module.functions:
        sz = func.getShardIds().size()                 // func+240
        if sz == 0: continue
        if sz != 1: THROW 1200 @:195                    // (a) one-shard-id rule
        for inst in func:
            if inst.IT == 87 /*CoreBarrier*/:           // Inst+0x58 == 0x57
                active = 0
                for lncId in 0..ncount-1:
                    DenseMap[shardIdSymbol] = lncId      // bind shard symbol
                    active += inst.evalMask(DenseMap)    // does this barrier fire on this core?
                if active != 0 && active != ncount: THROW 1201 @:208   // (b) no partial-core barrier
            else if inst.IT == 33 /*GPSIMDSB2SB*/:       // Inst+0x58 == 0x21
                for lncId in 0..ncount-1:
                    DenseMap[shardIdSymbol] = lncId
                    sb2sbCounts[lncId] += inst.evalMask(DenseMap)
    for lncId in 1..ncount-1:
        if sb2sbCounts[lncId] != sb2sbCounts[0]: THROW 1202 @:222      // (c) uniform SB2SB
```

The symbolic checker is a shard-id symbolic execution: it never materializes the cores. It binds each function's single shard-id symbol to `0..lnc_size-1` and uses `evalMask` (the affine predication mask) to predict, per core, whether each barrier/SB2SB op executes — proving the symbolic graph is **splittable** into `lnc_size` consistent replicas *before* the splitter runs.

The concretized checker @ `0x16772a0` runs after the split, when the vector holds the materialized per-core modules. It takes core 0 as the reference and cross-compares every core `ncId ∈ 1..lnc_size-1`: structural identity (functions/BBs/names), barrier inventory (count/id/name), SB2SB count, and the remote-access map. The comparison is **structural-identity-up-to-sharding** — the cores must be the same control-flow skeleton, differing only in concretized shard-dependent fields (engine/partition assignment, remote targets), which encodes the invariant that the splitter produced `lnc_size` SPMD replicas of one symbolic graph.

> **NOTE —** the remote-access pair 1197/1195 (`:133`/`:142`) is the cross-core shared-DRAM legality. For a shared post-DRAM-alloc memloc on a non-zero core (guard `memtype == 7 && isSharedPostDRAMAlloc && ncId > 0`), the verifier resolves the `RemoteLocalTarget` via `getFunctionByName(name).getMemoryLocationByName(name)` and asserts the target exists and aliases the **same address** as the local memloc. This is the gate that the cross-core shared-memory mapping is resolved before codegen — the static precondition that makes the barrier-range race check (above) meaningful: only resolved shared addresses can race.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `LncVerifier::run(bir::Module&)` | `0x1674860` | single-module no-op shim (status = 2) | CERTAIN |
| `LncVerifier::run(vector<unique_ptr<Module>>&)` | `0x167b620` | real LNC pass — gate + symbolic/concretized dispatch | CERTAIN |
| `LncVerifier::inputModulesHaveSymbolicShardIds` | `0x1674f00` | branch key: `size==1 && !getNeuronCoreId()` | CERTAIN |
| `LncVerifier::checkSubgraphsWithSymbolicShardIds` | `0x16762c0` | pre-split legality — codes 1200/1201/1202 | CERTAIN |
| `LncVerifier::checkSubgraphsWithConcretizedShardIds` | `0x16772a0` | post-split legality — codes 1187–1199, 1203 | CERTAIN |
| `LncVerifier::LncVerifier(PassOptions const&)` | `0x167b730` | ctor — stores `PassOptions*` at `+0x28` | CERTAIN |
| `bir::Instruction::evalMask(DenseMap<AffineIdx*,long>&)` | `0x3fd9488` | affine predication-mask evaluator (symbolic counting) | HIGH |
| `LncBarrierCheck::run(vector<unique_ptr<Module>>&)` | `0x16885f0` | the barrier-range race check (this page's `.so`) | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `lnc_splitter` / `expand_replication` (8.30) | the producer: clones the symbolic module ×`lnc_size` and concretizes; `LncVerifier` is its correctness oracle |
| racecheck ([8.42](racecheck.md)) | the intra-core twin — semaphore vector-clocks + `doAccessesOverlap`, vs this checker's barrier ranges + icl intersect |
| Execution & Sync Model (1.14) | the dynamic `CoreBarrier` rendezvous this pass models statically as an index band |
| `birverifier` (per-op) | pipeline-order sibling — checks each instruction's L1 legality on one module; shares no code with `LncVerifier` |

## Cross-References

- [`lnc_splitter` / `expand_replication`](lnc-splitter.md) — 8.30, the multi-core split this verifier proves correct; the symbolic↔concretized branch key is the bit the splitter sets
- [Execution & Sync Model — Semaphores & Barriers](../arch/execution-sync-model.md) — 1.14, the `CoreBarrier`/all-engine-barrier hardware sync model whose index band this checker reasons over
- [Opcode Master](opcode-master.md) — 8.33, `InstCoreBarrier` = opcode `0x57` (87) and `GPSIMDSB2SB` = `0x21` (33), the instruction types this pass keys on at `Inst+0x58`
