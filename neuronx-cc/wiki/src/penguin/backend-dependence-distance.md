# Backend Polyhedral Dependence-Distance (libwalrus)

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. Driver bodies (`neuronxcc::backend::LoopOptimization`) live in `neuronxcc/starfish/lib/libwalrus.so`; the distance primitive (`bir::getDefUseDistance`) lives in `neuronxcc/starfish/lib/libBIR.so`. Both retain a full C++ symbol table (`nm -DC`), so every body named here is a demangled symbol, not a `sub_*`. cp310 addresses are primary; cp311/cp312 differ by a small fixed delta (table in [§1](#1-symbol-map)).*
>
> *Provenance: `libwalrus.so` and `libBIR.so` are present in the corpus — as per-symbol decompiled/disasm sidecars and as full IDA databases under `ida/` — so the cited bodies (e.g. `getDefUseDistance @ libBIR 0x20d4e0`) are disassembled, not merely declared. The same provenance holds on [Symbolic-AP Register-ALU](symbolic-ap-register-alu.md), [DGE Level Selection](dge-level-dynamic-dma.md), [the Dynamic For-Loop](dynamic-for-loop.md), and [Dynamic-Shape Synthesis](dynamic-shape-synthesis.md).*

## Abstract

The backend loop optimizer in `libwalrus.so` ships a function named `constructPolyhedralDependenceGraphWithDistance`. The name is a decoy. There is **no isl** anywhere in this gate — not in the driver, not in the per-loop builder, not in the distance kernel. The word "polyhedral" is nominal. The actual machine is custom Neuron C++ built over `boost::icl::interval_set` (exact byte-address intervals), the Boost Graph Library (`boost::adjacency_list`), and LLVM ADT (`MapVector`, `DenseMap`, `SmallVector`). The real isl-based polyhedral path is a **separate** analysis living in the Penguin/affine bridge — see [5.16 ISL Dependence-Graph](isl-dependence-graph.md) for the genuine article. This page documents the impostor, and documents it as exactly that.

The gate answers one question for the loop fusion / interchange legality checker: *if I merge these two loops, does any read end up consuming a value that its producer writes at a later relative iteration?* It answers it not by subtracting symbolic base offsets — the cheap thing — but by **tiling each access footprint into iteration groups, materializing the exact byte-address intervals each group touches as a `boost::icl::interval_set`, and testing pairwise interval intersection over the iteration cross-product.** The returned "distance" is the largest def-iteration index whose footprint is still live at some use iteration. A strictly positive value flags an illegal backward loop-carried dependence. That mechanism — verified instruction-by-instruction against the real `getDefUseDistance` body at `libBIR 0x20d4e0` — is the heart of the page.

Three functions form the gate. `constructPolyhedralDependenceGraphWithDistance(Module&)` is a two-pass module driver that populates a per-loop SAP×SAP distance cache. `constructLoopDependenceGraph(InstLoop*)` builds, per loop, that distance cache plus a debug dependence-direction-vector graph. `bir::getDefUseDistance(SAP, SAP, int)` is the interval-overlap kernel both rely on, also consumed directly by the fusion-legality predicate `has_negative_distance(InstLoop*, InstLoop*)`.

## Reimplementation contract

To rebuild this gate you must reproduce:

- **The two-pass driver** — pass 1 buckets instructions by loop nesting level; pass 2 visits every `InstLoop` (opcode `105`) and builds its dependence graph, keeping only the side-effect distance cache at `InstLoop+0x210`.
- **The dependence-existence gate** — two SAPs are a candidate dependence iff they touch the **same** `MemoryLocationSet` (pointer-equal after a `-280` normalization), or, in the per-loop builder, the same tensor name (`memcmp` of the name bytes).
- **The distance kernel** — evaluate both access patterns, tile into `niters` iteration groups, build per-group `boost::icl::interval_set<u64, right_open_interval>`, and return `max{ i : ∃ j, intersects(def[i], use[j]) }`.
- **The `niters` formula** — `niters = gcd(tripcount(L1), tripcount(L2)) × Π tripcount(shared parent loops)`.
- **The sign rule** — `distance > 0 ⇒ illegal` (the `has_negative_distance` reject), distance `≤ 0 ⇒ legal`.
- **The def/use polarity** — DEF = output-arg SAP (`ArgumentKind == 2`), USE = input-arg SAP (`ArgumentKind == 1`).

| | |
|---|---|
| **Module driver** | `LoopOptimization::constructPolyhedralDependenceGraphWithDistance(Module&)` @ libwalrus `0xbae000` |
| **Per-loop builder** | `LoopOptimization::constructLoopDependenceGraph(InstLoop*)→DDGTy` @ libwalrus `0xbabe50` |
| **Distance kernel** | `bir::getDefUseDistance(SAP const&, SAP const&, int)` @ **libBIR** `0x20d4e0` |
| **Fusion-legality gate** | `LoopOptimization::has_negative_distance(InstLoop*, InstLoop*)` @ libwalrus `0xb8f100` |
| **Distance cache** | `InstLoop + 0x210` (528) = `MapVector<SAP*, MapVector<SAP*, int>>` |
| **DDG type** | `std::multimap<std::pair<Instruction const*, Instruction const*>, std::vector<int>>` |
| **Footprint substrate** | `boost::icl::interval_set<unsigned long, right_open_interval>` — **zero isl** |
| **`__FILE__` anchors** | `neuronxcc/walrus/ir/lib/IR/AccessPattern.cpp:0x554`; `…/loop_optimization/src/loop_profitable_fusion.cpp` |

> **GOTCHA — the name lies.** `constructPolyhedralDependenceGraphWithDistance` contains zero `isl_*` calls, and neither does `getDefUseDistance`. If you are looking for the schedule-tree legality machinery, it is not here — it is in [5.16](isl-dependence-graph.md). This gate is "polyhedral" in name only; it is exact byte-interval overlap over an unrolled iteration space.

---

## 1. Symbol Map

All in `class neuronxcc::backend::LoopOptimization` (libwalrus) unless noted. Addresses are `nm -DC`-confirmed across three ABIs.

| Symbol | cp310 | cp311 | cp312 | Conf |
|---|---|---|---|---|
| `constructPolyhedralDependenceGraphWithDistance(Module&)` | `0xbae000` | `0xbadfd0` | `0xbadf80` | CERTAIN |
| `constructLoopDependenceGraph(InstLoop*)→DDGTy` | `0xbabe50` | `0xbabe20` | `0xbabdd0` | CERTAIN |
| `has_negative_distance(InstLoop*, InstLoop*)` | `0xb8f100` | `0xb8f0d0` | `0xb8f080` | CERTAIN |
| `viewDependenceGraph(multimap&, string&)` | `0xbab900` | — | — | CERTAIN |
| `collectInstructionsByNestingLevel(BasicBlock&)` | `0x5f3c20`† | — | — | CERTAIN |
| `collect_def_in_loop(InstLoop*, vector<SAP*>&)` [free fn] | `0xb8e740` | — | — | CERTAIN |
| `collect_use_in_loop(InstLoop*, vector<SAP*>&)` [free fn] | `0xb8e8a0` | — | — | CERTAIN |
| `collect_illegal_common_ap(InstLoop*, InstLoop*)` | `0xb8ea70` | — | — | CERTAIN |
| `bir::getDefUseDistance(SAP const&, SAP const&, int)` | libBIR `0x20d4e0` | — | — | CERTAIN |
| `bir::doAccessesOverlap(SAP const&, SAP const&, int)` | libBIR `0x20f0b0` | — | — | CERTAIN (sibling; not called by the three targets) |
| `bir::intersecting(APINFO const&, APINFO const&)` | libBIR `0x20c840` | — | — | CERTAIN (per-AP footprint test) |
| `bir::getAddressIntervals(…)` | libBIR `0x17b570` / `0x20a380` | — | — | CERTAIN (returns into `boost::icl`) |

> † `collectInstructionsByNestingLevel` is exported in the per-symbol decompiled set at the libwalrus-internal VA `0x5f3c20` (as `LoopOptimization::collectInstructionsByNestingLevel(bir::BasicBlock&)`; a sibling `LoopOptDoctor::` overload sits at `0x5ef870`). The `nm -D` driver-VA for the same body is reported as `0xba2ab0`; both refer to the same function.

> **NOTE — two image bases.** The per-symbol `decompiled/…libwalrus.so__*.c` corpus is a thunk-and-internal-VA view (e.g. `collect_use_in_loop` appears as a tail-call thunk at `0x5f1210`). The `0xbae000`-range addresses in this table are the `nm -DC` body addresses of `libwalrus.so` proper. Where a body is reachable in both views the symbol name is identical; only the offset frame differs.

### Recovered types

```c
// The graph the per-loop builder emits (from viewDependenceGraph's
// demangled signature + the _M_emplace_equal call):
using DDGTy = std::multimap< std::pair<bir::Instruction const*, bir::Instruction const*>,  // key = (defInst, useInst)
                             std::vector<int> >;                                           // val = DIRECTION VECTOR {-1,0,+1}

// The persistent per-loop distance cache (lea r12,[rax+210h] @0xbac093):
using PerLoopDistMap =                                                       // dist[def][use] = int (stored symmetrically)
    llvm::MapVector< bir::SymbolicAccessPattern*,
                     llvm::MapVector< bir::SymbolicAccessPattern*, int > >;  // cached @ InstLoop + 0x210 (528)

// The SCRATCH build graph (destroyed before constructLoopDependenceGraph returns):
using Graph = boost::adjacency_list< boost::vecS, boost::listS, boost::directedS,
                 boost::property< neuronxcc::backend::instruction_name_t, bir::Instruction* >,  // vertex bundle
                 neuronxcc::backend::DependenceEdgeT,                                           // edge bundle (40 bytes)
                 boost::no_property, boost::listS >;
```

### Struct offsets

| Offset | Field | Use |
|---|---|---|
| `SAP + 0x38` (56) | owning `StorageBase*` | dependence-existence gate root |
| `SAP + 0x20` (32) | owning `bir::Instruction*` | `getLoopnest` / graph vertex |
| `StorageBase + 0x110` (272) | type tag (`== 1` ⇒ `MemoryLocationSet`) | the `isa<MemoryLocationSet>` gate |
| `StorageBase + 0x188` (392) | `MemoryLocationSet*` (normalise `−280`) | identity key in the kernel |
| `StorageBase + 0x128/0x130` (296/304) | tensor-name `char*` / length | `memcmp` identity in the per-loop builder |
| `InstLoop + 0x210` (528) | `PerLoopDistMap` | the persistent distance cache |
| `Instruction + 0x50` (80) | opcode (`105` == `InstLoop`) | the loop-detection tag |
| `Instruction + 0x98/0xA0` (152/160) | input-arg ilist head | `collect_use` (`ArgumentKind == 1`) |
| `Instruction + 0xB8/0xC0` (184/192) | output-arg ilist head | `collect_def` (`ArgumentKind == 2`) |

---

## 2. The driver — `constructPolyhedralDependenceGraphWithDistance`

`@ 0xbae000`. Signature `(LoopOptimization* this, bir::Module& M)`. The module is an ilist of `BasicBlock`; `M[1]` is the first block, `&M` itself is the sentinel. Two passes.

```c
// PASS 1 — build the nesting-level index.
void constructPolyhedralDependenceGraphWithDistance(LoopOptimization* this, Module& M) {
    for (BasicBlock& BB : M)                                   // ilist walk
        collectInstructionsByNestingLevel(this, BB);           // @0xbae05f call
                                                               // buckets instructions by loop depth into
                                                               // the per-level worklists the rest of LoopOptimization consumes

    // PASS 2 — per-loop dependence distance.
    for (BasicBlock& BB : M)
        for (Instruction& k : BB)
            if (*(uint*)(&k + 0x50) == 0x69) {                 // opcode == 105 == InstLoop
                                                               // disasm @0xbae10e: cmp dword [r15+50h], 69h
                InstLoop* L = (InstLoop*)((char*)&k - 0x60);   // InstLoop sub-object = inst − 0x60
                DDGTy ddg = constructLoopDependenceGraph(this, L);   // @0xbae120

                // DISCARD ddg: walk its boost-adjacency edge nodes (48-byte nodes,
                // node freed with operator delete[](node, 0x48)). The multimap RESULT is
                // thrown away here. The PERSISTENT output is the side effect inside
                // constructLoopDependenceGraph: PerLoopDistMap @ L + 0x210.
            }
}
```

The driver's job is to **populate every `InstLoop`'s `+0x210` SAP×SAP distance map** (and emit the per-loop `viewDependenceGraph` debug dump). The returned per-loop multimap is consumed only for that debug view; it is not retained module-wide.

```text
GROUNDING (disasm 0xbae000):
  0xbae05f   call collectInstructionsByNestingLevel        ; pass 1
  0xbae10e   cmp dword ptr [r15+50h], 69h  ; 'i'           ; opcode == 105 == InstLoop
  0xbae113   jnz  <skip non-loops>
  0xbae119   lea  rdx, [r15-60h]                            ; InstLoop* arg
  0xbae120   call constructLoopDependenceGraph
  0xbae12f.. the ddg-node free loop (delete[] 0x48 nodes)
```

Opcode `105` (`0x69`) at `inst+0x50` is the same `InstLoop` tag the `collect_def/use_in_loop` recursors test (`cmp [v+0x50], 69h`) — consistent across all four functions in the gate.

---

## 3. The per-loop builder — `constructLoopDependenceGraph`

`@ 0xbabe50`. Returns `DDGTy`. Asserts bake in the source path `…/loop_optimization/src/loop_profitable_fusion.cpp`. This is the loop-level heart.

### Step A — collect access patterns

```c
DDGTy constructLoopDependenceGraph(LoopOptimization* this, InstLoop* L) {
    std::vector<SAP*> defs, uses;
    collect_loop_outputs(L, &defs);   // the loop's WRITE/output SAPs (local analogue of collect_def_in_loop)
    collect_loop_inputs (L, &uses);   // the loop's READ/input  SAPs (local analogue of collect_use_in_loop)

    Graph graph;                      // boost::adjacency_list, SCRATCH
    DDGTy& ddg = *(DDGTy*)this;       // the returned multimap is constructed in-place in `this`
```

### Step B — def × use pairing, gated on same tensor

```c
    for (SAP* d : defs)
      for (SAP* u : uses) {
        StorageBase* sbD = *(StorageBase**)((char*)d + 0x38);
        StorageBase* sbU = *(StorageBase**)((char*)u + 0x38);
        assert(*(int*)((char*)sbD + 0x110) == 1);  // isa<MemoryLocationSet>  (disasm @0xbabfd1/@0xbabff4: cmp [rax+110h],1)
        assert(*(int*)((char*)sbU + 0x110) == 1);

        char*  ptrD = *(char**)((char*)sbD + 0x128); size_t lenD = *(size_t*)((char*)sbD + 0x130);  // tensor NAME @0xbac001/@0xbac00f
        char*  ptrU = *(char**)((char*)sbU + 0x128); size_t lenU = *(size_t*)((char*)sbU + 0x130);  // @0xbac008/@0xbac016

        if (lenD == lenU && (lenD == 0 || memcmp(ptrU, ptrD, lenD) == 0)) {   // SAME tensor
            int dist = bir::getDefUseDistance(d, u, niters);                  // ⭐ the distance (see §4)

            // store symmetrically into the per-loop SAP×SAP cache @ L + 0x210:
            PerLoopDistMap& m = *(PerLoopDistMap*)((char*)L + 0x210);         // disasm @0xbac093: lea r12,[rax+210h]
            m[d][u] = dist;
            m[u][d] = dist;

            // add a boost-graph edge d.inst → u.inst (stored_edge_property<void*,DependenceEdgeT>,
            // 40-byte edge node, list-hooked). The owning Instruction* of a SAP = *(Instruction**)(SAP + 0x20).
            ...
        }
      }
```

> **NOTE — the per-loop `niters` is the same divisor.** Inside this body the third arg to `getDefUseDistance` is the loop-local iteration count derived from `d`'s evaluated predicate count; the decompiler mistypes it as a `SAP*` (`v9 = (int)uses[1]` is the predicate count, not a pointer). The unambiguous `niters` derivation is in `has_negative_distance` ([§5](#5-the-fusion-legality-gate--has_negative_distance)): `gcd(tripcounts) × Π parent tripcounts`. That the two agree follows from the shared contract, not from a second read.

### Step C — the dependence direction vector

For each matched pair the builder computes a classic **dependence direction vector**, one element per shared loop level:

```c
        auto evalD = SAP::getEvaluatedAps(d); auto loopD = Instruction::getLoopnest(d->inst);
        auto evalU = SAP::getEvaluatedAps(u); auto loopU = Instruction::getLoopnest(u->inst);

        std::vector<int> dirVec;
        // walk loopD / loopU axis lists in lock-step; for each common axis map it to its
        // per-iteration index via two DenseMaps (DenseMap<pelican::AffineIdx*,long> and
        // DenseMap<bir::LoopAxis*,unsigned>), yielding iD (def axis idx) and iU (use axis idx):
        for (axis a : common_axes) {
            int delta = iD - iU;                 // disasm: *(v129+8) − *(v130+2)
            if      (delta > 0) dirVec.push_back(+1);   // def AFTER use  ('>')
            else if (delta < 0) dirVec.push_back(-1);   // def BEFORE use ('<')
            else                dirVec.push_back( 0);   // same iteration ('=')
        }
```

The canonical `{<, =, >}` is encoded as `{-1, 0, +1}`. The debug banner literally prints `", distance vector = [" … "]"`, a `.rodata` string.

### Step D — emplace into the DDG multimap

```c
        auto key = std::make_pair((const Instruction*)d->inst, (const Instruction*)u->inst);
        // a std::set<std::vector<int>> per key DEDUPES identical direction vectors (find → _M_insert_unique),
        // then:
        ddg._M_emplace_equal(key, dirVec);       // multimap insert, loop_profitable_fusion.cpp:0x423/0x424
```

### Step E — debug dump + teardown

```c
    viewDependenceGraph(this, ddg, "<loop-tag>");   // @0xbab900 — pretty-prints the multimap
    // destroy defs/uses and the scratch boost::adjacency_list `graph`
    return ddg;   // constructed in-place in `this`; the boost graph is local scratch, destroyed first
}
```

> **QUIRK — the graph the function is named for is thrown away.** Two structures are built: a transient `boost::adjacency_list` (scratch, destroyed before return) and the `DDGTy` multimap (returned, then discarded by the driver in [§2](#2-the-driver--constructpolyhedraldependencegraphwithdistance)). Neither survives. The only persistent product is the integer `PerLoopDistMap` cache at `InstLoop+0x210` plus the human-readable debug dump. The "dependence graph" is, in production flow, debug scaffolding.

Debug strings present exactly once each in `libwalrus.so` `.rodata`: `"(DEBUG) def = "`, `"(DEBUG) use = "`, `"(DEBUG) def -> use: …, dependent = …"`, `"(DEBUG) def [offset, tensor_id] = …"`, `"(DEBUG) use [offset, tensor_id] = …"`, `", distance vector = ["`. The `dependent=` boolean is `bir::intersecting(APINFO, APINFO)` (libBIR `0x20c840`) — the per-pair footprint-intersect test; the direction vector is only meaningful when the footprints actually intersect.

---

## 4. The distance kernel — `bir::getDefUseDistance`

`@ libBIR 0x20d4e0`. Returns `int` = "max iteration distance" (always `≥ 0`). **This is not a base-offset subtraction.** It is exact byte-interval overlap over the unrolled iteration space. Every claim in this section is verified directly against the decompiled `0x20d4e0` body.

### Preflight — same memory location or bail

```c
int getDefUseDistance(const SymbolicAccessPattern& def, const SymbolicAccessPattern& use, int niters) {
    StorageBase* sbDef = *((StorageBase**)&def + 7);          // def + 0x38
    if (*(int*)((char*)sbDef + 272) != 1) assert_fail();      // +0x110 == 1 ⇒ isa<MemoryLocationSet>  (decompile :260)
    void* mlsDef = *(void**)((char*)sbDef + 392);             // +0x188
    if (mlsDef) mlsDef = (char*)mlsDef - 280;                 // normalise base  (:262-264)

    StorageBase* sbUse = *((StorageBase**)&use + 7);
    if (*(int*)((char*)sbUse + 272) != 1) assert_fail();
    void* mlsUse = *(void**)((char*)sbUse + 392);
    int maxDist = 0;                                          // v183 = 0  (:270)
    if (mlsUse) mlsUse = (char*)mlsUse - 280;

    if (mlsUse != mlsDef) return maxDist;                     // different MemoryLocationSet ⇒ NO dependence ⇒ 0  (:273)
```

> **GOTCHA — two different existence gates.** The per-loop builder ([§3](#3-the-per-loop-builder--constructloopdependencegraph)) selects candidate pairs by tensor-**name** `memcmp`; the kernel re-verifies by `MemoryLocationSet` **pointer** equality after the `−280` normalization and short-circuits to distance 0 on mismatch. They agree in the common case but are separate code paths, and a reimplementation needs both.

### Evaluate and tile

```c
    resetEvaluatedAps(&def); resetEvaluatedAps(&use);                   // :275-276
    auto evDef   = getEvaluatedAps(&def);   auto evUse   = getEvaluatedAps(&use);
    auto predDef = getEvaluatedPredicates(&def);                        // :279
    auto predUse = getEvaluatedPredicates(&use);                        // :280

    int niters1 = #predicates(predDef) * #aps(def);    // total def iterations in evaluated space (v11, :283)
    int niters2 = #predicates(predUse) * #aps(use);    // total use iterations              (v14, :287)
    assert(niters1 / niters != 0 && niters2 / niters != 0);            // AccessPattern.cpp:0x554  (:292-296)
    // groupsDef = niters1 / niters ; groupsUse = niters2 / niters
```

The `__assert_fail` message and path are recovered verbatim: `"niters1 / niters != 0 && niters2 / niters != 0"`, `"…/neuronxcc/walrus/ir/lib/IR/AccessPattern.cpp"`, line `0x554`.

### Build per-iteration byte intervals

```c
    // For each evaluated AP, for each active predicate bit (the predicate bitset gates live iterations):
    //   dtype = *(ap + 4)            (≤ 0x13, else "Unknown dtype")
    //   bytes = elementSize[dtype] * extent
    //   getAddressIntervals(bytes, addrSet, dtype, scratch)           // → byte ranges this iter touches
    //   iterKey = elemIdx % niters                                    // bin into one of `niters` groups
    //   defGroups[iterKey] |= interval;   // boost::icl::interval_set<u64, right_open_interval>, segmental join
    // (same loop again for use → useGroups)
    std::unordered_map<int, icl::interval_set<u64,right_open_interval>> defGroups, useGroups;   // type @:1249
```

`getAddressIntervals` demangles with `boost::icl` in its very return type (`…RN5boost3icl12…`), and returns the `interval_set`. The interval merge is `boost::icl` segmental `join_left/join_right`.

### Overlap → distance

```c
    int v183 = 0;
    for (int gj = 0; gj < niters; ++gj)                                  // outer: use iter-group  (v193)
      for (int gi = 0; gi < niters; ++gi) {                              // inner: def iter-group  (v190)
        bool hit = boost::icl::intersects(defGroups[gi], useGroups[gj]); // @:1255
        if (hit) { int cand = gi; if (cand > v183) v183 = cand; }        // running max  (:1270-1272)
      }
    return v183;                                                         // :1364
}
```

The reduction is exactly the decompiled `if ((int)v183 >= (int)v194) v76 = v183; v183 = v76;` running maximum, with both loop bounds `v201 == niters`. `getDefUseDistance` returns the **largest def-iteration index whose byte footprint is still touched by some use iteration** = the longest live def→use span measured in iterations.

There *is* a base-offset subtraction in this machinery, but it is not the answer. The `sub` pair over evaluated base offsets is the cheap pre-step *inside* `getAddressIntervals` that positions each footprint in the address space before it is turned into intervals. The value the kernel returns is the `boost::icl::intersects` max-iteration reduction over the binned interval sets — the `0x20d4e0` body holds `boost::icl::intersects`, an `unordered_map<int, interval_set<…right_open_interval…>>`, and the `v183` max reduction, and no `isl_*` reference at all.

> **GOTCHA — `niters` is a divisor, not a count, and not an axis index.** `niters` is the iteration-group *size*: the kernel divides the total evaluated-iteration counts (`niters1`, `niters2`) by it to get the number of groups, and the two `niters1/niters != 0` asserts fire if a caller passes a `niters` larger than either footprint. This is why the `has_negative_distance` formula ([§5](#5-the-fusion-legality-gate--has_negative_distance)) must be the *shared* iteration granularity of the fused loops, never a raw trip count and never a partition-axis index.

---

## 5. The fusion-legality gate — `has_negative_distance`

`@ 0xb8f100`. Returns `1` iff fusing `L1` and `L2` would introduce a backward (negative) loop-carried dependence; `0` otherwise. This is the predicate behind the `"[STOP FUSION] negative distance dep in def-use"` diagnostic.

### Step A — collect

```c
int has_negative_distance(InstLoop* L1, InstLoop* L2) {
    std::vector<SAP*> uses, defs;
    collect_use_in_loop(L2, &uses);   // L2 input/read SAPs  (ArgumentKind == 1)
    collect_def_in_loop(L1, &defs);   // L1 output/write SAPs (ArgumentKind == 2)
```

### Step B — co-located pairing

```c
    std::vector<std::pair<SAP*,SAP*>> pairs;
    for (SAP* u : uses)
      for (SAP* d : defs) {
        void* mlsU = *(void**)((char*)*(void**)((char*)u+0x38) + 0x188); if (mlsU) mlsU = (char*)mlsU - 280;
        void* mlsD = *(void**)((char*)*(void**)((char*)d+0x38) + 0x188); if (mlsD) mlsD = (char*)mlsD - 280;
        if (mlsU == mlsD) pairs.push_back({d, u});     // same MemoryLocationSet ⇒ candidate dep
      }
    // def is pair.first, use is pair.second  (disasm @0xb8f34d-0xb8f357: rdi=[rbx]=def, rsi=[rbx+8]=use, edx=niters)
```

### Step C — the authoritative `niters` formula

```c
    int tc1 = L2->getTripCount();          // call [vtbl+0x140]  @0xb8f298
    int tc2 = L1->getTripCount();          // call [vtbl+0x140]  @0xb8f281
    int niters = gcd(tc1, tc2);            // Euclidean: idiv r12 @0xb8f2b5
    InstLoop* p1 = getParentLoop(L1);      // @0xb8f2c8
    InstLoop* p2 = getParentLoop(L2);      // @0xb8f2d4
    while (p1 && p2 && p1 == p2) {         // multiply by every COMMON enclosing parent loop
        niters *= p1->getTripCount();      // [vtbl+0x140] @0xb8f2f7
        p1 = getParentLoop(p1);            // @0xb8f304
        p2 = getParentLoop(p2);            // @0xb8f30f
    }
    // niters = gcd(tripcount L1, tripcount L2) × Π tripcount(shared parent loops)
```

This is the divisor whose `niters1/niters`, `niters2/niters` non-zero asserts fire inside the kernel — the granularity of the common iteration space the two loops would share after fusion.

The dataflow leaves no room for another reading of that third argument: three `getTripCount` calls (`[vtbl+0x140]`), a GCD `idiv`, and four `getParentLoop` calls produce one integer, and that integer is what gets passed.

### Step D — the sign test

```c
    for (auto& [d, u] : pairs) {
        int dist = bir::getDefUseDistance(d, u, niters);          // @0xb8f35a
        assert(*(int*)((char*)*(void**)((char*)u+0x38) + 0x110) == 1);   // use MLSet gate
        if (dist > 0) {                                           // test r14d,r14d; jle skip @0xb8f388
            LOG("ZZZZZZZZZZZZmax iteration distance <tensor>@<...> : use -> def : %d "
                "(iteration groups:%d)\n", dist, niters);
            return 1;                                             // NEGATIVE distance ⇒ illegal
        }
    }
    return 0;
}
```

### Why "positive distance" means "negative dependence"

The sign convention reads backwards until you line it up with what the kernel returns. `getDefUseDistance` gives the *max def-iteration index still live at some use iteration*, so on a `(def ∈ L1, use ∈ L2)` pair a **positive** value means `L2`'s read consumes a value `L1` writes at a *later* relative iteration. That is fine as long as the loops stay separate — `L1` runs to completion first. Fuse `L1` and `L2` into one body and the same read now precedes its producing write within the same iteration carry: a **backward**, i.e. negative, loop-carried dependence.

So `has_negative_distance` rejects on `dist > 0` and skips on `dist ≤ 0`, which is exactly the `test r14d,r14d ; jle <continue>` at `0xb8f388`. The diagnostic banner's `"max iteration distance … use -> def"` phrasing describes the pre-fusion direction; the function name describes the post-fusion consequence.

> **GOTCHA —** a *non-positive* distance is the legal case. Reading `has_negative_distance` as "rejects when the distance is negative" inverts the test: no negative number is ever produced, since the kernel's running max starts at 0.

The direction vector is a separate product. `constructLoopDependenceGraph` ([§3C](#3-the-per-loop-builder--constructloopdependencegraph)) does materialize a per-pair `std::vector<int>` of `{-1,0,+1}` into the `DDGTy` multimap, one element per shared loop level — but the legality *decision* still reduces to the scalar `dist > 0` test, and the vector's only observed consumer is the debug projection.

---

## 6. `collect_def_in_loop` / `collect_use_in_loop`

`@ 0xb8e740` / `0xb8e8a0`. Free functions. Both recurse into nested loops and harvest access-pattern args from every non-loop instruction.

```c
void collect_def_in_loop(InstLoop* L, std::vector<SAP*>& out) {
    for (Instruction& inst : body(L)) {
        if (*(uint*)((char*)&inst + 0x50) == 0x69) {              // opcode == 105 == InstLoop
            collect_def_in_loop((InstLoop*)((char*)&inst - 8), out);   // recurse
            continue;
        }
        for (Arg& a : output_arg_ilist(inst /* +0xB8..+0xC0 */))  // OUTPUT args
            if (*(int*)((char*)&a + 0x10) == 2)                   // ArgumentKind == 2 ⇒ write
                out.push_back((SAP*)((char*)&a - 8));             // cast<SymbolicAccessPattern>
    }
}
// collect_use_in_loop is identical but walks the INPUT-arg ilist (inst +0x98..+0xA0)
// and selects ArgumentKind == 1 (reads).
```

> **DEF = output/written SAP, USE = input/read SAP** — the standard def-use polarity. The ilist heads and the discriminant values 1/2 are read directly; the enum *name* `ArgumentKind` is **INFERRED**. `collect_use_in_loop` carries the full signature `(bir::InstLoop*, std::vector<bir::SymbolicAccessPattern*>&)` in the symbol table.

---

## 7. Consumers and place in the loop optimizer

`has_negative_distance` (`0xb8f100`) is called from these named bodies (disasm xref):

| Caller | Address | Role |
|---|---|---|
| `check_loop_fusion(BasicBlock&, int)` | `0xb95a70` | non-greedy fusion legality |
| `check_loop_fusion_fast(BasicBlock&)` | `0xb92d60` | fast-path fusion legality |
| `checkLoopFusionGreedy(BasicBlock&, int)` | `0xba9210` | greedy fusion legality |
| `check_loop_interchange(…)` | `0xb93430` | interchange legality |

The dependence-distance machinery is the **legality gate for loop fusion and loop interchange** inside `LoopOptimization`. Flow: `run()` → per-BB `checkLoopFusionGreedy` / `check_loop_fusion` → for each candidate loop pair (within the positional `distanceThreshold = 8` window) → `has_negative_distance(L1, L2)` → if true, emit `"[STOP FUSION] negative distance dep in def-use"` and reject; else fusion/interchange may proceed.

The per-loop `PerLoopDistMap` (`InstLoop+0x210`) built by `constructLoopDependenceGraph` / the polyhedral driver is the cached SAP×SAP integer distance table the same fusion-profit/legality code consults, alongside `collect_illegal_common_ap` (`0xb8ea70`). The DDG multimap + direction vectors are the human-readable projection (`viewDependenceGraph`) and the structured form a reorder check would read (direction vector all-`=` or leading-`<` ⇒ legal).

### Relation to the real isl path

There is **no structural relation** to the genuine isl machinery. The Penguin loop transforms validate against this access-pattern dependence graph (byte-interval overlap over iteration groups), not the isl schedule checker. The stock islpy / TongaIsl analysis ([5.16](isl-dependence-graph.md), [`isl-schedule-tree-legality`](isl-schedule-tree-legality.md)) is a separate machine in the Penguin/affine bridge and is invoked by **none** of the three functions on this page — there is no `isl_*` reference anywhere in `getDefUseDistance @ 0x20d4e0`.

> **NOTE — downstream live ranges.** The dependence distance also bounds def→use live ranges: a use that reads `k` iterations after its def needs `k` live buffer copies. Symbolic-AP buffer materialization and software-pipeline-depth sizing consume the same integer `k` this gate produces, though that consumption is established on the buffer-sizing side rather than here.

---

## 8. Evidence summary and known gaps

The five strongest claims and their anchors:

1. **"Zero isl — boost::icl, name-only polyhedral."** The `getDefUseDistance @ 0x20d4e0` body has no `isl_` token at all, against 72 `icl` tokens, 24 `interval_set`, and one `boost::icl::intersects`.
2. **`getDefUseDistance` is interval-overlap, not offset subtraction.** Decompile line 1255 is `boost::icl::intersects(defGroups[gi], useGroups[gj])`; lines 1270–1272 are the `v183` running max; both loops are bounded by `v201 == niters`.
3. **Existence gate `+0x110 == 1` and `+0x188 − 280` location identity.** Decompile lines 260/266 (`+272 == 1` ⇒ `isa<MemoryLocationSet>`), 262–272 (`+392`, `−280`), 273 (`if (mlsUse != mlsDef) return 0`).
4. **`niters` = GCD × parent trips.** `has_negative_distance` shows three `getTripCount` (`[vtbl+0x140]`), a GCD `idiv`, and four `getParentLoop`.
5. **Sign: `dist > 0 ⇒ reject`.** `test r14d,r14d ; jle <continue>` @ `0xb8f388`.

**Not byte-pinned:** the `ArgumentKind ∈ {1,2}` enum *name*; that the per-loop `niters` equals the kernel divisor (the predicate-count dataflow and shared contract make it near-certain, but the decompiler mistypes the argument); `DependenceEdgeT` node size = 40 bytes, from `tc_new(40)`.

**Open gaps:** the exact `DependenceEdgeT` member layout — only the 40-byte size and the stored target `Instruction*` are recovered; whether a multi-level mixed-sign direction vector (e.g. `[<,>]`) is ever consumed for a reorder decision beyond the scalar `dist > 0` reject, since only the scalar consumption is observed; and the `getAddressIntervals` dtype byte-size table (`Dtype.h`, `≤ 0x13` dtypes), which is used here but not enumerated.
