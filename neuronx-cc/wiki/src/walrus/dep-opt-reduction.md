# Dependence-Graph Reduction and Scheduling-Unit Expansion

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). Every pass body lives in `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331…`). For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset; `.data` carries a +0x400000 delta. The `0x5e9xxx` "bodies" in the symbol sidecars are 6-byte `.plt` GOT thunks — every address below is the real `.text` body at its `nm -DC` address. Other wheels differ — treat each address as version-pinned.*

## Abstract

Three Walrus backend passes have similar names and adjacent jobs, and they are routinely conflated into "the transitive-reduction pass". They are not three flavours of the same thing. **`expand_scheduling_units`** (order 96) is *structural flattening*: it walks the BIR, finds every coarse scheduling-unit wrapper that the pre-scheduler treated as one atom, and recursively splices the wrapper's per-engine constituent instructions up into the parent basic block — pure container surgery that touches no dependency edge. **`dep_opt`** (order 104) is a *rebuild*: after the schedulers have churned the program order, it tears down the stale flow graph and re-derives a correct one from the materialised IR, then patches the storage-specific serialisation chains (PSUM accumulation, DVE pool, tiled-matmul) — a pass whose net effect on edge count is *up or stable*. **`dep_reduction`** (order 130) is the *true transitive reduction*: per dependency edge `A→C` it asks "is `A` already reachable through some other path `A→B→C`?" and, if so, drops the now-redundant edge — net effect *strictly down*. The three reduce to one mnemonic: **96 = shape, 104 = rebuild, 130 = minimise + verify**.

The headline mechanism is how `dep_reduction` answers the reachability question in `O(1)` per query. It carries a `SyncState` per program point — a happens-before summary built from **CRoaring** compressed bitmaps (the project statically links the whole `roaring_bitmap_*` C library into `libwalrus.so`). A `SyncState` is two banks of eight per-engine Roaring bitmaps; a bit set at `(engine e, position i)` means "the instruction at engine-position `i` on engine `e` is guaranteed to have completed by here." Transitive *closure* is propagated by set-union (`roaring_bitmap_or_inplace`); the redundancy test is a single `roaring_bitmap_contains`; the async-window minimisation is a `roaring_bitmap_andnot_inplace`. Reducing the edge set here is exactly what makes the downstream data-race check's surviving sync set both correct and minimal — `dep_reduction` thins the dependency edges that `bir_racecheck` later reconstructs its clock from.

> **GOTCHA — the race checker does not share this clock.** The `SyncState` Roaring bitmaps on this page live in `libwalrus.so`. The race checker is a separate binary, `libBIRRacecheck.so`, and its vector clock is a plain `std::vector<int32>` — no `roaring` symbol or string exists in that `.so` at all. The two passes are coupled by the *edge set* `dep_reduction` minimizes, not by a shared clock representation. See [racecheck.md](racecheck.md).

The reference frame is an ordinary scheduler dependency DAG, with one twist worth internalising before reading the algorithm: the reachability summary is *not* a dense `N×N` reachability matrix and *not* a per-node bitset over instructions. It is a **vector clock** — eight bitmaps indexed by *engine position*, not by instruction identity. An edge `P→C` is dropped iff `P`'s engine-position bit is already set in the union of the cached closures of `C`'s *other, earlier-visited* predecessors. That keeps the per-instruction state proportional to the number of distinct engine positions reached, which Roaring compresses to near-nothing for the dense runs typical of a tiled tensor program.

For reimplementation, the contract is:

- The **`SyncState` layout**: two banks (`+0x000`, `+0x140`) each an `array[8]` of `roaring_bitmap_t` handles at stride `0x28`, indexed by `bir::EngineType`; and the three primitive mutators (`setCompleted` / `remove` / `merge`) decoded byte-exact below.
- The **transitive-reduction loop** in `applyReorderReduction`: per consumer, fold predecessors in program order, `mergeSyncStates` (union the cached closures), `hasCompleted` (Roaring `contains`) → on hit `Instruction::removeDependency(EdgeKind::Flow)`, on miss `setCompleted`; then `recordSyncState` to cache the consumer's closure.
- The **async (multi-slot) handling**: `procAsync` / `procAsyncSecondaryEdges` add completion-latency edges; `minimizeSyncState` marks the async window with `add_range_closed` and subtracts already-implied descendants with `andnot_inplace`; `RedundantAsyncRemover` drops covered async descendant edges.
- The **anti/output phase** `applyAntiDependenceReduction`: drop WAR/WAW edges already enforced by an intervening same-engine instruction (via the engine↔datapath position maps), then `verifyAntiDependence` recomputes the expected `EdgeKind` and emits `"Inconsistent dependence labeling on:"` on mismatch.
- The **`expand_scheduling_units` recursion**: the opcode-`0x69` (=105) unit discriminator, the nested-body recursion, the rename-then-`insertIntoSymboltable`-then-`removeInstruction` splice, and the fact that it modifies no edge.
- The **`dep_opt` rebuild chain**: the three `PassOptions` ints it reads, and the per-function helper sequence headlined by `build_flow_deps::constructBIRGraph`.

| | |
|---|---|
| **`expand_scheduling_units`** | `ExpandSchedulingUnits::run(bir::Module&)` @ `0x1698500` → `expand` @ `0x1695ea0` (order 96) |
| **`dep_opt`** | `DepOpt::run(bir::Module&)` @ `0xc47370` → `backend::dep_opt(…,int,int,int)` @ `0xc472c0` → `reduce_dependencies::dep_opt(…,int,int)` @ `0xc464e0` (order 104) |
| **`dep_reduction`** | `DepReduction::run(bir::Module&)` @ `0x1068230` → `runOnFunction` @ `0x1067b60` (order 130) |
| **Transitive-reduction core** | `DepReduction::applyReorderReduction` @ `0x1064e40` |
| **Redundancy test** | `DepReduction::hasCompleted(SyncState const&, Inst*, Inst*)` @ `0x1063af0` |
| **Closure propagate** | `DepReduction::mergeSyncStates(SyncState&, Inst*)` @ `0x1064840` |
| **Anti/output + verify** | `DepReduction::applyAntiDependenceReduction` @ `0x1064040`; `verifyAntiDependence` @ `0x105fe20` |
| **`SyncState` primitives** | `setCompleted(EngineType,long,bool)` @ `0x105a010` · `merge(SyncState const&)` @ `0x105a040` · `remove(EngineType,long,bool)` @ `0x105a090` |
| **CRoaring (statically linked)** | `roaring_bitmap_add` @ `0x1867b70` · `_or_inplace` @ `0x186aee0` · `_contains` @ `0x1872ac0` · `_andnot_inplace` @ `0x186ca50` · `_add_range_closed` @ `0x18663b0` · `_init_with_capacity` @ `0x1865950` |
| **Closure cache** | `DenseMap<const bir::Instruction*, shared_ptr<SyncState>>` (symbol confirmed in dynsym) |
| **Pipeline order** | 96 `expand_scheduling_units` · 104 `dep_opt` · 130 `dep_reduction` (last opt before `bir_racecheck` @ 131) |

This page documents the three passes in pipeline order. The dependency edge model they operate on — the three dep sets (FLOW/RAW, ANTI, OUTPUT/ORDERED), the `EdgeKind` enum, and the `EdgePtr` `PointerIntPair` tag — is the subject of the sibling *Dependence Graph* page (Part 8.9, planned `walrus/dependence-graph.md`); the consumer that re-derives a vector clock over the same eight-engine model is the *Walrus Scheduler* page (Part 8.11, planned). This page treats the edge model as given and concentrates on how the graph is shaped, rebuilt, and minimised.

---

## 1. `expand_scheduling_units` (order 96) — structural flattening

### Purpose

A `SchedulingUnit` is a *coarse* grouping of instructions that the pre-scheduler treats as a single atom. It is the BIR opcode-`0x69` (=105) operation — the Loop/unit op — carrying a nested body whose blocks yield the unit's constituent per-engine instructions. The pre-scheduler schedules the unit as one critical-path node so its early list scheduler need not reason about every per-engine micro-op; that keeps the pre-scheduler's `MemoryLocation` DAG small and its topo-sort cheap, and it preserves loop/tensorizer structure as an atomic blob. Post-scheduling, register allocation, and per-engine codegen all need the *flat* per-engine instruction stream, so `expand_scheduling_units` is the bridge: **coarse-for-presched, expanded-for-postsched**. The unit/Loop opcode identity cross-checks against the BIR verifier's `isSchedulingUnit()` assertion; the coarse-then-expand rationale is design inference from the call structure.

### Entry Point

```text
ExpandSchedulingUnits::run(bir::Module&)            @ 0x1698500   (BackendPass body)
   per function → per BasicBlock → per instruction:
      ExpandSchedulingUnits::expand(parentBB, I, insertPoint)   @ 0x1695ea0   (call site 0x16986ad)
```

`run`'s body is a standard BackendPass shell: push a `boost::log` pass-name attribute, then iterate the module's functions and walk each basic block's instruction ilist. The discrimination of "is this actually a unit" is made *inside* `expand`, not in `run` — `run`'s branch maze at `0x16986cb…0x16987a3` is only `get_logging_enabled` / `open_record` boilerplate and always falls through to the `expand` call. The iterator is re-read after each `expand` (`[rsp+0x30] = inst->next` at `0x16986b2`) because `expand` splices and erases in place, so the walk is splice-safe.

### Algorithm

```c
// ExpandSchedulingUnits::expand @ 0x1695ea0
// expand(BasicBlock& parent, Instruction* I, BasicBlock& insertPoint)
void expand(BasicBlock &parent, Instruction *I, BasicBlock &insertPoint) {
    // Core discriminator @ 0x1695f3c
    if (I->opcode /* [I+0x50] */ == 0x69) {            // 105 = scheduling-unit (Loop) wrapper
        BasicBlockHolder *body = I->body;              // [I+0xf0]
        // empty-region guards on [body+0x20]/[+0x28]/[+0x30] == 0
        for (BasicBlock *b : body->blocks()) {         // plt 0x5ee930
            for (Instruction *child = b->first; child; child = child->next /* [+0x8] @0x1696010 */)
                expand(parent, child, insertPoint);    // *** RECURSIVE *** @ 0x1695fc3
        }
        // (nested units inside the body are handled by this recursion)
    } else {
        // Leaf path (non-105): re-home this single constituent into the parent BB.
        // 1. derive a unique symbol-table name (28× std::string::_M_append + locale/stream)
        //    so the flattened inst does not collide in the parent BB's namespace
        // 2. NamedObjectContainer<BasicBlock,Instruction>::insertIntoSymboltable(name, child)  // plt 0x61c940
        // 3. std::vector<Instruction*>::_M_realloc_insert (collect, 2 sites)
    }
    BasicBlock::removeInstruction(I);                  // plt 0x5eb930 — delete the wrapper
}
```

The recursion is the whole point: a unit whose body itself contains units is flattened all the way down, in program order, into the parent block. The body of the pass is dominated by 28× `std::string::_M_append` plus locale/stream calls — the **rename** step: each expanded constituent gets a unique symbol-table name derived from the unit name before `insertIntoSymboltable`, because the flattened instructions now live in the parent block's namespace and must not collide. The discriminator and the recursion are read off the disassembly; the rename *intent* is inferred from the `insertIntoSymboltable` call following the appends.

> **QUIRK — `expand_scheduling_units` touches no dependency edge.** There is no call into the per-instruction dependency block (`Inst+0xD0`), no `addDependency`, no `removeDependency` anywhere in `expand`. The edges the wrapper carried are inherited by the spliced constituents *structurally* — each constituent keeps its own `EdgePtr` set; the wrapper was only ever a scheduling proxy. A reimplementer who tries to "fix up edges" during expansion is solving a problem the binary does not have. The absence of dependency calls is directly observed; the structural-inheritance claim is [INFERRED] — the disassembly does not positively trace it.

---

## 2. `dep_opt` (order 104) — rebuild, not reduce

### Purpose

After the schedulers have rearranged the program, the flow graph derived earlier is stale. `dep_opt` throws it away and re-derives a clean, correct one from the post-schedule IR, then patches the storage-specific serialisation chains the generic flow builder cannot infer. Its direction is the **opposite** of a transitive reduction: it *adds* the edges needed for correctness after scheduling churn (erasing only the loop-carried edges that are no longer valid). The "opt" in `dep_opt` means "optimise the dependency set for the new program shape", not "minimise edge count". The helper chain is read directly; the net-direction characterisation is inference from which helpers add versus remove.

### Entry Point

```text
DepOpt::run(bir::Module&)                                  @ 0xc47370   (BackendPass entry)
   reads 3 PassOptions ints, then:
backend::dep_opt(Module&, Logger&, int, int, int)          @ 0xc472c0   (5-arg wrapper)
reduce_dependencies::dep_opt(Module&, Logger&, int, int)   @ 0xc464e0   (4-arg core)
```

`DepOpt::run` reads three ints out of the pass object's `PassOptions` and forwards them:

```c
// DepOpt::run @ 0xc47370 
ecx = opts[+0x004];   // optlevel / arch-ish
edx = opts[+0x0e0];
r8d = opts[+0x1a4];
backend::dep_opt(Module, Logger, ecx, edx, r8d);   // plt 0x6018f0 → 0xc472c0
// the 5-arg wrapper forwards to the 4-arg core (reduce_dependencies::dep_opt @ 0xc464e0),
// dropping/renaming one arg (call to thunk 0x5effb0)
```

### Algorithm

The core walks the module's functions (via `BasicBlockHolder::blocks` @ `0xc46520`; the `cmp [rdx+0x50], 0x69` at `0xc46549` skips unit wrappers when locating the entry block). It reads the function's arch generation byte (`func[+0xac]`, the same field the flow-dep producer reads) into a scratch and lets an `r14d` phase/level argument gate which sub-steps fire. The per-function body, in order, calls these backend helpers (every target confirmed by its `.plt` entry):

```c
// reduce_dependencies::dep_opt @ 0xc464e0, per-function body
build_flow_deps::constructBIRGraph(int, bool);      // 0x612160  *** REBUILD flow/RAW deps from IR
erase_loop_dependencies(Function&, Logger&);        // 0x615e40  drop loop-carried edges
llvm::deallocate_buffer(...);                       // 0x60dd80  free the temp dep buffer
sync_psum_accesses(Function&);                      // 0x624260  PSUM accumulation re-serialise
if (arch == 0x14 || globalFlag)
    sync_dve_pool(Function&);                       // 0x62b500  DVE-pool re-sync
if (arch == 0x28)
    sync_tiled_matmult_matmultmx(Function&);        // 0x614780  tiled-MM MX re-sync
set_first_matmults(Function&);                      // 0x6088b0
sanitize_matmult_stc_bits(Function&, DenseMap<…acc_grp>); // 0x626a70
if (level > 0) {
    draw_trivial_deps(Function&);                   // 0x5f1390  structural/fallback deps
    insert_memsets(Function&, long&);               // 0x6156e0  init uninitialised PSUM/SB
}
if (arch == 0xa)
    flip_one_matmult(Function&);                    // 0x615de0
// FunctionAttribute write (boost::variant): stamps a 12-char attr key marking "deps rebuilt"
// optional dump_schedule(Function&, ostream&)  @ 0x612d30  (--debug dump, opens a basic_filebuf)
```

The headline call is `build_flow_deps::constructBIRGraph` — `dep_opt` *re-runs the flow-dep construction* on the post-schedule IR. The other helpers fix up edges the generic builder cannot infer: PSUM accumulation ordering, DVE pool hazards, tiled-matmul MX serialisation, trivial/structural deps (the same fallback the flow builder uses on spill overflow), and memset insertion for uninitialised PSUM/SB regions. There is **no reachability analysis** anywhere in this pass — no Roaring bitmap, no `SyncState`.

> **QUIRK — the exact numeric meaning of arch values `0xa` / `0x14` / `0x28` is not resolved here.** They gate `flip_one_matmult` / `sync_dve_pool` / `sync_tiled_matmult` respectively, and clearly select a hardware generation, but the mapping to a named arch enum is left to a future cross-reference. [GAP]

---

## 3. `dep_reduction` (order 130) — true transitive reduction

This is the pass the page title is really about. It runs late — the *last* optimisation before the data-race checker (order 131) — and it is the only one of the three that uses Roaring bitmaps and computes reachability.

### 3.0 The `SyncState` abstraction

`neuronxcc::backend::SyncState` is a per-program-point "what has happened-before here" summary, represented as **two banks of eight per-engine CRoaring bitmaps**. The three primitives are decoded byte-exact:

```c
// SyncState::setCompleted(EngineType e, long id, bool bank) @ 0x105a010 
off = e * 0x28;
if (bank) roaring_bitmap_add(this + 0x140 + off, id);   // bank B
else      roaring_bitmap_add(this + 0x000 + off, id);   // bank A

// SyncState::remove(EngineType e, long id, bool bank)     @ 0x105a090
//   roaring_bitmap_remove on the same (bank ? +0x140 : +0x000) + e*0x28 slot

// SyncState::merge(SyncState const& o)                    @ 0x105a040
for (int e = 0; e < 8; ++e) {
    roaring_bitmap_or_inplace(this + 0x000 + e*0x28, o + 0x000 + e*0x28);   // bank A union
    roaring_bitmap_or_inplace(this + 0x140 + e*0x28, o + 0x140 + e*0x28);   // bank B union
}
```

So the object is: **bank A @ `+0x000`, bank B @ `+0x140`, each an `array[8]` of `roaring_bitmap_t` handles at stride `0x28`**, indexed by `bir::EngineType` (the eight datapath engines). A bit set at `(engine e, id i)` means "the instruction at engine-position `i` on engine `e` is guaranteed complete." `merge` is set-union — the join used to propagate transitive reachability. The width 8 is `bir::numDataPathEngines()`; the same `std::array<…, 8>` is taken by `RedundantAsyncRemover`'s constructor (`Lm8E` in the mangled name) and is the same eight-engine clock width as the post-`dep_reduction` race checker — one idiom reused throughout the backend. The layout and the three primitives are decoded byte-exact; what the *two* banks mean — most plausibly completed-vs-in-flight, or committed-vs-pending — is [INFERRED].

### 3.1 `run` — gating

```c
// DepReduction::run @ 0x1068230 
r15d = arch/level field;
if (r15d == 0x0a)                                  // arch 10
    { emit "Skipping dependency reduction pass." (rodata @ 0x1d34db8); return; }
if (globalByte @ register_generator_dep_reduction__+0x158)  // cl::opt off-switch
    return;
// Module flag [Module+0x1ec] gates a variant; r15d == 0x13 (19) selects another arch variant
for (Function &F : Module.functions())
    runOnFunction(F);                              // plt 0x62a920 → 0x1067b60
```

`dep_reduction` is **arch-gated off on some targets and behind a `cl::opt`**; when off it leaves the full post-`dep_opt` edge set intact for the scheduler-independent late passes. A reimplementer must treat the reduction as optional and prove correctness with it disabled.

### 3.2 `runOnFunction` — orchestration

```c
// DepReduction::runOnFunction @ 0x1067b60 
assignIds();                          // 0x10671b0  per-engine + datapath position ids
applyReorderReduction();              // 0x1064e40  FLOW/ordered transitive reduction
if (cl::opt @ reg+0x98)               // "enable-anti-dependence-reduction"
    applyAntiDependenceReduction();   // 0x1064040  ANTI/OUTPUT reduction + label verify
```

`procAsync` / `procAsyncSecondaryEdges` are **not** called from here directly — they are invoked from inside `applyReorderReduction` and `minimizeSyncState`.

### 3.3 `assignIds` — engine and datapath positions

`assignIds` @ `0x10671b0` walks each engine's instruction stream, reads `bir::Instruction::getInstSyncType()` (values `1`/`2` are the synchronising kinds), and stamps each instruction with an engine-position id (`Inst+0x90` — the field `hasCompleted`/`setCompleted` later index by). It also builds the two bidirectional position maps the position helpers consume:

```text
DepReduction[+0x280 + engine*0x40] = std::map<enginePos → datapathPos>   (Rb-tree)
DepReduction[+0x288 + engine*0x40] = std::map<datapathPos → enginePos>   (Rb-tree)
```

These let the pass reason about *when* an instruction executes on the global datapath versus its per-engine program order — necessary because async ops occupy a *range* of datapath slots.

### 3.4 `applyReorderReduction` — the transitive reduction

```c
// DepReduction::applyReorderReduction @ 0x1064e40 
procAsync();                                          // 0x5f1350 (thunk → 0x1062600) — async edges first
for each engine: roaring_bitmap_init_with_capacity(...)  // 0x625960 — scratch SyncState @ rsp+0x140

// Main per-consumer loop. C = consumer (r12 / [rbp+0x20]);
// predecessors walked via the Rb-tree of C's dependency set at Inst+0xD0, in program order.
for (Instruction *C : block) {
    SyncState workState = {};
    for (Instruction *P : C->predecessors_in_program_order()) {   // inner loop @ 0x1065548
        mergeSyncStates(workState, P);          // 0x5fa580 (→ 0x1064840): workState |= recorded[P]
        --P->successorCounter;                  // Inst+0x4c — fan-out bookkeeping
        if (hasCompleted(stateA, workState, P)) {            // 0x60f650 (→ 0x1063af0)
            C->removeDependency(P, EdgeKind::Flow /*4*/, false);  // plt 0x61d880 → libBIR  *** DROP
            ++stats[+0x64];                     // redundant-edge counter
        } else {
            setCompleted(workState, P);         // 0x5e9a10 (→ 0x1062a00) — mark P reachable
            ++stats[+0x68];                     // kept-edge counter
        }
        // getInstSyncType() in {1,2} short-circuits — sync ops are ordering barriers
    }
    recordSyncState(C, sharedWorkState);        // 0x5ebf50 (→ 0x1063e30) — cache C's closure
}
```

This is the textbook transitive-reduction invariant, realised on a Roaring vector clock. Predecessors are visited in **program order**, and `workState` accumulates the *cached closure* of each already-visited predecessor (`mergeSyncStates` unions in `recorded[P]`, which is `P`'s full transitive reachability set). The moment `P` is found already present in `workState`, a transitive path `C ← … ← P` exists through an earlier predecessor, so the direct `P→C` FLOW edge is provably implied and dropped. The edge kind is FLOW: `mov edx, 0x4` appears immediately before `removeDependency`. The `EdgePtr` low-3-bit tag is untagged with `and eax, 0x7 ; cmp 0x1` at `0x106559c`, exactly as the `PointerIntPair` edge model predicts.

```c
// mergeSyncStates @ 0x1064840 
//   look P up in DenseMap<const Instruction*, shared_ptr<SyncState>>  (recordSyncState cache, plt 0x602400)
//   SyncState::merge it into the out-state  → 8×2 roaring_bitmap_or_inplace
// This is the transitive-closure PROPAGATION step:
//   reach(C) = (union over preds P of recorded[P]) + {the preds themselves}
```

```c
// hasCompleted @ 0x1063af0 — THE REDUNDANCY TEST
bool hasCompleted(SyncState const &state, Instruction *workCtx, Instruction *P) {
    long pid; if (DenseMap_lookup(P, &pid) /* LookupBucketFor, plt 0x5ec300 */) { /* … */ }
    if (P->getInstSyncType() in {1,2}) return true;      // sync ops are ordering anchors
    if (roaring_bitmap_contains(state.bank[P->enginePos * 0x28], pid))  // plt 0x6070b0
        return true;                                     // bit already set → redundant
    // degenerate fallbacks: roaring_bitmap_is_empty / same-engine program-order compare (Inst+0x90)
    return false;
}
```

`hasCompleted` is the `O(1)` "is `P` transitively reachable" query: one `roaring_bitmap_contains` against the relevant engine's completion bitmap.

> **QUIRK — predecessors must be folded in program order, or the reduction is wrong.** The invariant depends on `workState` already holding the closures of `C`'s earlier predecessors when `P` is tested. Visit predecessors out of order and a redundant edge can be tested before the path that implies it has been merged in, so it survives — a missed reduction (conservative, still correct) — or, worse, an edge can be dropped before its implying path is established if the bookkeeping is reordered. The binary keys the walk on the `Inst+0xD0` dependency Rb-tree's program-order sort precisely to guarantee this. The program-order walk is directly observed; the consequence of breaking it is reasoning.

### 3.5 `procAsync` / `minimizeSyncState` — async (multi-slot) ops

An async instruction (a DMA or a long matmul) does not complete at a *point* but over a *range* of datapath positions; its successors only become "completed" once the async effect lands. `procAsync` @ `0x1062600` builds a `bir::DepGraph` and calls `procAsyncSecondaryEdges` @ `0x1061390` to add the secondary completion-latency edges, checking an `isAsync` byte (`Inst+0x38`). `minimizeSyncState` @ `0x105a730` is the heavy lifter:

```c
// minimizeSyncState @ 0x105a730
roaring_bitmap_add_range_closed(bm, lo, hi);   // 0x18663b0 — mark the whole async window
roaring_bitmap_andnot_inplace(bm, implied);    // 0x186ca50 — *** subtract elements already
                                               //     implied by a longer path = set-minimise
roaring_bitmap_maximum(bm);                    // 0x1871f30 — + roaring iterators
// constructs RedundantAsyncRemover(Logger&, vector<Inst*>&, uint&, vector<Inst*>&,
//                                  std::array<vector<Inst*>, 8>&)            @ 0x105d4c0
//   whose procAsyncRedundantDescendants @ 0x105c220 drops async descendant edges already covered
```

The `roaring_bitmap_andnot_inplace` *is* the transitive reduction expressed on the bitmap: subtract the elements a longer path already covers, leaving the minimal set. The `std::array<…, 8>` passed to `RedundantAsyncRemover` is the per-engine instruction lists (`Lm8E` in the constructor's mangled name).

### 3.6 `applyAntiDependenceReduction` — WAR/WAW reduction and label verification (gated)

```c
// applyAntiDependenceReduction @ 0x1064040 (cl::opt "enable-anti-dependence-reduction")
for each ANTI(2) / OUTPUT(3) edge prod→cons {            // cmp eax,0x2 / 0x1 dispatch
    // use the engine-position maps to find an intervening SAME-engine instruction
    // that already enforces the ordering:
    p = getNearestDatapathPosBeforeEnginePos(...);        // 0x1059f00
    d = getDatapathPosForEnginePos(...);                  // 0x105a610
    if (physical ordering already exists)
        prod.removeDependency(cons, ...);                 // 0x10645b3 — drop the storage-hazard edge
    // then VERIFY the surviving label:
    EdgeKind computed = verifyAntiDependence(prod, cons, edgePtr);   // 0x105fe20 (via minimizeSyncState)
    if (computed != edge.labeledKind)
        printDependenceError(...);                        // 0x105f2a0
        //   "Inconsistent dependence labeling on:" / "computed:" / "labeled:"
}
```

This phase is **both** a reduction (drop physically-implied WAR/WAW edges) **and** a late correctness verifier of the surviving edge labels. ANTI/OUTPUT edges are physical-storage-reuse artifacts the allocator creates; only at order 130 — post register allocation, post matmul rotation — is the physical engine schedule final enough to prove that one storage hazard implies another. The phase is gated off by default-ish via the `cl::opt`. The dispatch, `removeDependency`, `verifyAntiDependence`, and the error strings are read directly; the "post-RA is why it is late" rationale is reasoning from the pipeline position.

---

## 4. `dep_opt` (104) vs `dep_reduction` (130) — the precise distinction

| | `dep_opt` (104) | `dep_reduction` (130) |
|---|---|---|
| **Pipeline position** | mid (post `post_sched`, pre-codegen) | late (last opt before `bir_racecheck` @ 131) |
| **Class / entry** | `reduce_dependencies::dep_opt` (free fn + `DepOpt::run` wrapper) | `DepReduction` (BackendPass) |
| **Direction** | REBUILD + ADD edges | MINIMISE / REMOVE edges |
| **Headline op** | `build_flow_deps::constructBIRGraph` (re-derive RAW/flow from IR) | `applyReorderReduction` (transitive reduction) + `applyAntiDependenceReduction` |
| **What it touches** | FLOW/RAW + structural (PSUM/DVE/MM accumulation, loop deps, memsets) | FLOW (reorder) + ANTI/OUTPUT (gated); drops `A→C` implied by `A→B→C` |
| **Algorithm** | sequence of fix-up helpers per function (no reachability analysis) | per-engine Roaring-bitmap completion sets + happens-before union/contains |
| **Data structure** | `DenseMap<MemoryLocation>` + function attributes | `SyncState` = 8 engines × 2 banks of Roaring bitmaps |
| **Correctness aim** | make the edge set CORRECT after the scheduler rearranged the program | give the scheduler MORE FREEDOM by removing provably-redundant edges, then VERIFY labels |
| **Gating** | always (per-function, arch-conditional sub-steps) | arch `r15d != 0x0a` (else skip), `cl::opt` switches |
| **Net edge count** | typically UP / stable | strictly DOWN |
| **Shared idiom** | — | 8-engine Roaring bitmaps == the race-check vector clock, reused |

**Mnemonic:** `dep_opt` *repairs* the graph for the new program order; `dep_reduction` *prunes* the repaired graph to its transitive minimum just before codegen and the race check.

---

## 5. Why the edges matter — connection to schedulers and lowering

The dependency edges these passes shape are produced upstream (`build_fdeps` produces FLOW; the anti-dependency analyzer produces ANTI/OUTPUT; order constraints produce ORDERED) and consumed downstream by the schedulers and the late sync machinery. The two ends frame why each of these three passes sits where it does:

- **`expand_scheduling_units` @ 96** flattens the coarse pre-scheduler units into the per-engine instructions that `post_sched`, register allocation, and per-engine codegen (`lower_act` / `lower_dve` / `lower_ap` / the coloring allocator) operate on. It is purely the materialisation step between coarse scheduling and fine lowering.
- **`dep_opt` @ 104** re-runs the FLOW producer on the post-schedule IR and patches the storage-serialisation chains — it makes the edge set *correct* for the new program order.
- **`dep_reduction` @ 130** prunes the full set to its transitive minimum. This matters concretely: every surviving dependency that crosses engines must be realised as a *hardware sync* (a semaphore or event) at lowering. **Fewer edges ⇒ more scheduling freedom ⇒ fewer semaphores and events emitted.** Transitive reduction at 130 directly minimises the sync footprint right before lowering.

`dep_reduction` is immediately followed by `lower_control` (128) / `lnc_barriercheck` (129) and `bir_racecheck` (131). The data-race checker re-derives a vector clock over the *same* eight-engine model — so `dep_reduction` having minimised the edges is exactly what makes the race check's surviving sync set both correct and minimal. The eight-engine Roaring clock is one mechanism shared end-to-end: reachability summary here, race detection there.

---

## 6. Confidence and gaps

Read directly off the binary: every symbol address (`nm -DC` / dynsym, cross-checked: `run`@`0x1068230`, `runOnFunction`@`0x1067b60`, `applyReorderReduction`@`0x1064e40`, `applyAntiDependenceReduction`@`0x1064040`, `hasCompleted`@`0x1063af0`, `mergeSyncStates`@`0x1064840`, `minimizeSyncState`@`0x105a730`, `assignIds`@`0x10671b0`, `procAsync`@`0x1062600`, `SyncState::setCompleted/merge/remove`@`0x105a010`/`40`/`90`, `ExpandSchedulingUnits::run`@`0x1698500`/`expand`@`0x1695ea0`, `dep_opt` triple @`0xc464e0`/`0xc472c0`/`0xc47370`); the opcode-`0x69` unit discriminator and the recursion in `expand`; `dep_opt`'s helper call chain and the 3-int `PassOptions` read; `SyncState` = 8×2 Roaring bitmaps (the three primitives decoded byte-exact); `applyReorderReduction`'s `removeDependency(EdgeKind::Flow)` on `hasCompleted==true`; `hasCompleted` = `roaring_bitmap_contains`; the anti/output edge-kind dispatch and the `printDependenceError` strings; the `run` arch/`cl::opt` gates including `"Skipping dependency reduction pass."`; and that the whole CRoaring `roaring_bitmap_*` library is statically linked (every cited op resolved to a real `.text` address in this binary's dynsym).

Established indirectly, one cross-reference away from the disassembly: `SchedulingUnit` == Loop(105) identity (via the BIR-verifier `isSchedulingUnit()` cross-reference); `expand` renames constituents (28× string append + `insertIntoSymboltable`); `minimizeSyncState`'s `andnot_inplace` as the bitmap transitive reduction; the async-window handling and `RedundantAsyncRemover`'s `array<…, 8>` of per-engine lists.

**[INFERRED]** — the precise semantic of the **two** `SyncState` banks (completed vs. in-flight is the most plausible reading, but not pinned); `dep_opt`'s `r14d` "phase" argument's exact mapping to optlevel; that `expand` inherits edges structurally (no dep-block calls are observed in `expand`, but the inheritance itself is not directly traced).

Nothing else on this page affects the algorithm.

**GAPS** — the exact numeric meaning of arch values `0xa` / `0x13` / `0x14` / `0x28` (a cross-reference to the arch enum is deferred to a future task); the two `cl::opt` default values that gate `dep_reduction` overall and `applyAntiDependenceReduction` specifically.
