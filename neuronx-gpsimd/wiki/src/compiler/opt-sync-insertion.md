# Optimization + Inter-Engine Sync Insertion

This page is the **sync-insertion backend** of the GPSIMD compiler guide: how
`libwalrus.so` (the `neuronx-cc` dep-based C++ backend) turns a scheduled BIR
program into the per-engine semaphore wait/update commands that order the **five
engines** — `PE`, `ACT`, `POOL`, `DVE`, `SP` — so a GPSIMD(`POOL`) op never reads
a tensor before its producer on another engine has finished writing it. It also
inventories the **GPSIMD-relevant optimization passes** that run before sync
insertion and that shrink the dependency graph the sync passes have to wire.

This is the direct continuation of the scheduler page
[Tiling / Memory-Planning / Scheduling Backend](tiling-memory-scheduling.md):
that page produces the per-engine instruction order and the first `AntiDependency`
edges; **this** page is what happens to those edges — semaphore allocation,
trigger-engine choice, wait/set placement, the GPSIMD↔DVE deadlock break, and the
multi-buffer rotation. The sync fields these passes emit are *consumed* by
[Per-Engine Instruction-Block Assembly Pipeline](../neff/assembly-pipeline.md)
(the `<eng>.bin` 64-byte SEQ slots and the `def.json` `dma_queue` rows). The
device-side decode of the emitted `EVENT_SEMAPHORE` / `DRAIN` / `CoreBarrier`
ops lives under the Part-13 control pages (inline path:
`neuronx-gpsimd/wiki/src/control/...`). The pass roster and the
`emit_*`→opcode map are in [The GPSIMD-Relevant Compiler Map](compiler-map.md);
the collective sync ops these passes lower into are in
[collective-loadtime-rewrite.md](collective-loadtime-rewrite.md).

All symbols, addresses, docstrings and strings below are read directly
from the shipped, **not-stripped** `neuronx-cc 2.24.5133.0+58f8de22` wheel —
the Cython codegen passes carry verbatim `.py` method names as `__pyx_*` symbols
plus docstrings, and `libwalrus.so` carries C++ RTTI, assert strings and
build-path leaks of the form
`/opt/workspace/KaenaCompilerNativeBuild-311/.../neuronxcc/walrus/<pass>/src/<pass>.cpp`.
`libwalrus` addresses are from `nm -DC libwalrus.so`; no quoted string is
paraphrased.

> **Confidence tags.** `[OBSERVED]` byte-exact from a shipped string/symbol/
> docstring/address; `[INFERRED]` reasoned over observed control/data
> flow; `[CARRIED]` carried from a cited sibling page, re-grounded here.
> `HIGH/MED/LOW` = confidence.

> **GUARD.** `sunda`/`tonga`/`cayman`/`mariana`/`maverick` are the known per-gen
> **codegen-target / arch-ISA** axis (= CoreV2/V1/V3/V4/V5), not new silicon. The
> `SetupSemaphoreUpdate` per-slot-kind dispatch per `core_v2/v3/v4` and the per-gen
> `dbg_is_valid_drain` validators are that codegen axis. The five engines
> `PE/ACT/POOL/DVE/SP` are the shipped compiler's own `EngineType` enum
> (`libwalrus`: `"ACTIVATION DMA DVE PE POOL SP UNKNOWN_ENGINE"`). **GPSIMD = the
> `POOL`-engine Vision-Q7 cores** (int / gather / custom-op + SB2SB-collective +
> SW-DGE lane). The GPSIMD-cannot-touch-PSUM rule from
> [tiling-memory-scheduling.md §4.4](tiling-memory-scheduling.md) still holds and
> directly shapes the dependency model (§2.2).

---

## 1. The shape of the problem — five engine streams, one happens-before graph

The scheduler hands the backend **five independent instruction streams** (one per
engine: `PE=0`, `ACT=1`, `POOL=2`, `DVE=3`, `SP=4` — the same `al_hal_tpb_eng_type_t`
ordinals the runtime asserts in
[assembly-pipeline.md](../neff/assembly-pipeline.md)). Within one stream the
hardware sequencer executes in program order, so **same-engine** hazards are free.
The job of sync insertion is to cover every **cross-engine** hazard with a
semaphore: a consumer on engine `Y` must not start until a producer on engine
`X ≠ Y` has finished.

The whole pipeline is a fixed-order sequence of registered `libwalrus` passes
(each leaks its `neuronxcc/walrus/<pass>/src/<pass>.cpp` build path and a
`register_generator_<pass>` symbol). The sync-relevant spine, in order: `[HIGH/OBSERVED]`

```text
build_fdeps  ->  constant_propagate  ->  value_numbering(gvn)  ->  dep_reduction  ->
pre_sched (TimeAwareScheduler)  ->  memory_analysis_after_pre_sched  ->
instruction_reorder  ->  post_sched  ->  memory_analysis_after_post_sched  ->
anti_dependency_analyzer {-SB, -PSUM, -DRAM, -Alias [, _post_shared_dram, _post_lnk]}  ->
sync_dve_pool / sync_before_global_cc / sync_shared_allocations  ->
expand_replication / expand_scheduling_units  ->
assign_hwdge_engine  ->  assign_trigger_engine  ->
alloc_semaphores (Synchronizer)  ->  insert_ptcom_flat  ->
lower_sync (+ lower_dma / lower_dve / lower_ac / lower_control / lower_branch /
            lower_select / lower_local_collectives / lower_symbolic_inst)  ->
address_rotation_{sb, psum, dram} [_post_schedule]  ->
insert_dma_switch_queue_instance  ->
lnc_splitter / vnc_link / vnc_remote_addr_map / lnc_verifier / lnc_barriercheck  ->
bir_linker  ->  codegen (descgen / generator / debug_info_writer)
```

The division of labour:

| stage | pass(es) | what it computes |
| --- | --- | --- |
| **order** | `pre_sched` / `instruction_reorder` / `post_sched` (`TimeAwareScheduler`) | the per-engine stream order, topo-correct (§5) |
| **hazards** | `anti_dependency_analyzer` (per memory class) | the cross-engine FLOW/ANTI/OUTPUT/ORDERED edges (§2) |
| **deadlock** | `sync_dve_pool` / `ProcessInstLoopDeps` | the GPSIMD↔DVE drain + 2-update break (§3.4) |
| **trigger** | `assign_hwdge_engine` / `assign_trigger_engine` | which engine fires each DMA (§3.5) |
| **allocate** | `alloc_semaphores` / `Synchronizer` | the semaphore IDs + the wait/update commands (§3.1–3.3) |
| **barrier** | `InsertCoreBarrier` / `insert_ptcom_flat` | the all-engine fence / multi-core completion (§3.6–3.7) |
| **rotate** | `address_rotation_{sb,psum,dram}` | the multi-buffer (modulo) semaphore rotation (§3.8) |
| **stamp** | `lower_sync` + `CoreV{2,3,4}GenImpl` | the concrete 64-byte SEQ-slot bytes (§4) |

The output of the whole chain is the `$S[k]>0 $S[j]++@complete` semaphore
condition the assembly pipeline reads back — e.g. the `pe.asm` slot
*"`$S[10]>0 $S[22]++@complete ctype=ALL_REDUCE …`"* quoted in
[assembly-pipeline.md §5](../neff/assembly-pipeline.md): `$S[10]>0` is the
**consumer WAIT**, `$S[22]++@complete` is the **producer UPDATE** (§3.2).
`[CARRIED assembly-pipeline.md; the producer/consumer split OBSERVED here]`

---

## 2. The cross-engine dependency model — `AntiDependencyAnalyzer`

File: `libwalrus.so`, `neuronxcc::backend::AntiDependencyAnalyzer`
(build path `neuronxcc/walrus/anti_dependency_analyzer/src/anti_dependency_analyzer.cpp`;
vtable `_ZTVN9neuronxcc7backend22AntiDependencyAnalyzerE @0x3d8c008`; methods
`getSBPartitionRange @0x8c1860`, `getPSUMPartitionRange @0x8c1700`,
`analyzeDependencies @0x8c5d50`, `populatePsumUseMaps @0x8c73c0`,
`collectAccessPatternRanges` — **two overloads**, `@0x8cb950` (`AccessPattern&`) /
`@0x8c4800` (`PhysicalAccessPattern&`); registered as
`register_generator_anti_dependency_analyzer__`, with a re-run
`register_generator_anti_dependency_analyzer_post_shared_dram__`). `[HIGH/OBSERVED]`

### 2.1 The five edge kinds `[HIGH/OBSERVED]`

For every ordered pair of instructions whose memory footprints overlap, the
analyzer classifies the dependence as one `bir::EdgeKind`. The enum-name table is
a single concatenated string in the `.rodata` of `libwalrus`, byte-exact:
`ANTI_DEPENDENCEFLOW_DEPENDENCEINVALIDORDEREDOUTPUT_DEPENDENCE`
(and `dep.getEdgeKind() == EdgeKind::Ordered` is asserted at a call site):

| `EdgeKind` | hazard | semaphore consequence |
| --- | --- | --- |
| `FLOW_DEPENDENCE` | **RAW** — consumer reads what producer wrote | consumer **WAITs** on producer's completion semaphore |
| `ANTI_DEPENDENCE` | **WAR** — writer overwrites what a reader still needs | writer **WAITs** for the reader to finish |
| `OUTPUT_DEPENDENCE` | **WAW** — two writers to the same location | later writer **WAITs** for the earlier |
| `ORDERED` | program-order / control edge, no data overlap | order preserved, no semaphore unless it crosses an engine |
| `INVALID` | sentinel | — |

Only FLOW/ANTI/OUTPUT edges carry data hazards; the scheduler may reorder freely
**within** the `ORDERED`-respecting topological order (§5), but **every**
FLOW/ANTI/OUTPUT edge that crosses an engine boundary becomes an `EVENT_SEMAPHORE`
wait/set in §3. `[HIGH/OBSERVED — enum string + `EdgeKind::Ordered` assert]`

### 2.2 Why one analyzer instance per memory class `[HIGH/OBSERVED]`

The analyzer runs **separate instances over the partitioned address space**, each
tagged with its memory class (the instance-name strings are OBSERVED):

| instance | memory | range getter |
| --- | --- | --- |
| `AntiDependencyAnalyzer-SB` | the 176-KiB SBUF partitions | `getSBPartitionRange` |
| `AntiDependencyAnalyzer-PSUM` | the 8 PSUM banks | `getPSUMPartitionRange` |
| `AntiDependencyAnalyzer-DRAM` | HBM | (DRAM-class range) |
| `AntiDependencyAnalyzer-Alias` | aliased / union-set addresses | `populateAliasUseMap` |

plus the re-runs `_post_shared_dram` (after shared-DRAM resolution) and
`_post_lnk` (after `bir_linker`). Each instance builds per-instruction footprints
with `collectAccessPatternIntervals` / `collectAccessPatternRanges` /
`collectPartitionRanges` over `boost::icl::right_open_interval<unsigned>` /
`split_interval_set` partition ranges, and indexes them with `populateSbUseMap` /
`populatePsumUseMaps` / `populateAliasUseMap` (walked by `enqueueUseBatch`). Two
ops overlap (and earn an edge) **iff their partition ranges *and* their byte
ranges intersect in the same class** — the hazard test is an interval overlap,
not a whole-tensor compare. SB and PSUM are analyzed independently because they
are physically disjoint memories; `Alias` handles two logical tensors mapped to
one physical region. `[HIGH/OBSERVED — method surface + tag strings]`

> **GOTCHA — a GPSIMD op is *always* in the `-SB` class, never `-PSUM`.** Because
> a GPSIMD(`POOL`) op is SBUF-only (the verifier rejects `GPSIMD engine cannot
> access PSUM`, see [tiling-memory-scheduling.md §4.4](tiling-memory-scheduling.md)),
> its hazards are computed only by `getSBPartitionRange` in the
> `AntiDependencyAnalyzer-SB` instance; it never appears in `-PSUM`. A
> reimplementer's anti-dep pass **must classify every GPSIMD op into the SB memory
> class exclusively** — a GPSIMD op that shows up in the PSUM analyzer is a bug,
> not a hazard. `[HIGH/INFERRED from the SB-only rule + the per-class instances]`

### 2.3 The edge log + the dep graph `[HIGH/OBSERVED]`

Edges are added with the byte-exact logs `Begin inserting dependency edges for
kernel: `, `Added flow dep from `, `Added ordered dependency from `, `Added index
dependency from ` (the index-dep is for indirect/gather addressing — the GPSIMD
gather path), and the special `Added flow dep from memset to indirect load`. The
result is a **DepGraph** carried on each instruction: `numDescendents()` /
`numDependents()` / `InstNumDes[id]` — a descendent ref-count the scheduler
decrements as it releases successors (§5.2). The assert `can only add intermediate
flow deps before alloc_semaphores` pins the ordering: extra flow deps may be added
up to — but not after — the semaphore allocator runs.

### 2.4 Dependency reduction (`dep_reduction.cpp`) `[HIGH/OBSERVED]`

After the analyzer, `dep_reduction` prunes transitively-implied edges: if
`A→B` and `B→C` exist, the redundant `A→C` edge is dropped so it does not waste a
semaphore. The guard `Avoided Dependency Reduction due to too_many_spills
attribute` keeps the conservative (un-reduced) edge set when a tensor was spilled
— spilling adds reload DMAs whose exact ordering the reducer cannot prove
redundant, so the spilled tensor keeps its full edge set. `[HIGH/OBSERVED]`

```c
// AntiDependencyAnalyzer, one instance per memory class. All named symbols OBSERVED
// in neuronxcc/walrus/anti_dependency_analyzer/src/anti_dependency_analyzer.cpp.  [HIGH]
//
// EdgeKind (the bir enum, byte-exact concat string):
//   FLOW=RAW  ANTI=WAR  OUTPUT=WAW  ORDERED=control  INVALID=sentinel

void anti_dependency_analyzer(Function &fn, MemClass cls /* SB | PSUM | DRAM | Alias */) {
    // (1) footprint each inst as interval sets over the partitioned address space.
    UseMap use = {};
    for (Inst *I : fn.insts_in(cls)) {                  // GPSIMD ops -> cls==SB only (§2.2)
        IntervalSet parts = (cls == PSUM) ? getPSUMPartitionRange(I->ap)
                                          : getSBPartitionRange(I->ap);  // right_open_interval
        IntervalSet bytes = collectAccessPatternIntervals(I->ap);
        populateUseMap(use, I, parts, bytes);           // populateSbUseMap / populatePsumUseMaps
    }
    // (2) for every overlapping ordered pair, emit one EdgeKind edge.
    for (auto [P, C] : overlapping_ordered_pairs(use)) {  // interval-overlap, not whole-tensor
        EdgeKind k = classify(P, C);  // RAW->FLOW, WAR->ANTI, WAW->OUTPUT, else ORDERED
        if (k == INVALID) continue;
        add_edge(P, C, k);            // "Added flow dep from ..."; bumps C.numDependents,
                                      //                            P.numDescendents
    }
    // (3) transitive reduction, UNLESS a spilled tensor forces conservative deps.
    if (!fn.attr.too_many_spills)     // "Avoided Dependency Reduction due to too_many_spills"
        dep_reduction(fn);            // drop A->C when A->B->C exists
}
```

---

## 3. Sync insertion — allocate, trigger, wait/set, deadlock, barrier, rotate

Once the edges exist and the schedule is fixed, the sync sub-passes turn each
cross-engine edge into a per-engine SEQ semaphore wait/update. All in `libwalrus`.

### 3.1 The semaphore allocator / `Synchronizer` `[HIGH/OBSERVED]`

`alloc_semaphores/{alloc_semaphores,synchronizer}.cpp` —
`neuronxcc::backend::SynchronizerPass` + the two distinct semaphore classes
`AllocSemaphores` (the pass) and `AllocateSemaphores` (the per-instruction
assigner — both class names exist; do not conflate). Real methods + addresses
(`nm -DC`):

```text
AllocSemaphores::run(bir::Module&)                                  @0x1126ce0
AllocSemaphores::run(bir::Function&)                                @0x1124460
AllocSemaphores::allocateSemaphore(bool)                            @0x1121430
AllocSemaphores::incrementSemaphore(bir::Instruction*, unsigned, unsigned)        @0x1120f70
AllocSemaphores::addSemaphoreRotationWait(bir::Instruction*, unsigned)            @0x1123780
AllocSemaphores::setupSync(bir::Instruction*, std::vector<unsigned>, unsigned)    @0x1123e70
AllocSemaphores::setupSync(bir::InstDMABlock*, unsigned, std::vector<unsigned> const&, unsigned)  @0x1124360
AllocateSemaphores::assignDMASemaphore(bir::Instruction&)           @0x1138f10
AllocateSemaphores::assignEngineSemaphore(bir::Instruction&)        @0x1138b70
```

**Entry preconditions** (asserts): `Before Synchronizer, sequencer instructions
shouldn't need to update any semaphore, and shouldn't have descendents`, and
`Before alloc_sema, sequencer instructions (other than SwitchQueueInstance/
ResetQueueInstance/SetRandState) shouldn't have real descendents which need
semaphore alloc`. By the time the allocator runs, **only datapath instructions
carry real dep descendents**; the sequencer control ops are sync-free until
lowered.

**Two semaphore kinds:**

- `assignDMASemaphore` → the **DMA-completion** semaphore (bumped by the SDMA on
  `@complete`; `DMA Block was assigned a different semaphore on reuse after queue
  switch` guards reuse).
- `assignEngineSemaphore` → the **compute-engine** semaphore. One per
  `(basic-block × engine)`: `allocBBHolderEngineSemaphores` reserves the holder
  (`BBHolder has already assigned semaphore`; `<inst> has not assigned a semaphore
  yet!`).

### 3.2 The producer→consumer edge, realized `[HIGH/OBSERVED mechanism]`

For a FLOW(RAW) edge from producer `P` on engine `X` to consumer `C` on engine
`Y` (`X ≠ Y`), `alloc_semaphores`:

1. gives `P` an **UPDATE** on a semaphore `S` — `incrementSemaphore(P, S, +1)`
   (the increment fires on `@complete` for a DMA, on instruction-retire for a
   datapath op);
2. gives `C` a **WAIT** `$S[S] >= v` — `setupSync(C, {S}, v)` — where `v` is `P`'s
   final semaphore value for that location;
3. if `Y` already waits on a 3rd producer (fan-in > 2), the *EventSemaphore ≤ 2
   wait* rule (§3.3) forces an extra sync slot or folds the join into a
   `CoreBarrier` (§3.6).

A WAR/WAW edge is symmetric with reader/writer swapped. The asserts `Missing final
semaphore value for ` and `findIndirectLoadSaveSemaphoreWaitValue` pin that `C`'s
wait value must be `P`'s known completion count (for an indirect/gather DMA the
wait value is discovered from the descriptor). This is the model behind the
`$S[10]>0 $S[22]++@complete` read-back: `$S[10]>0` is `C`'s WAIT, `$S[22]++@
complete` is `P`'s UPDATE. `[HIGH — mechanism OBSERVED from methods + asserts;
the exact `v` = producer-final-count arithmetic INFERRED-HIGH]`

### 3.3 The EventSemaphore wire rules `[HIGH/OBSERVED — byte-exact verifier asserts]`

A SEQ `EVENT_SEMAPHORE` slot is constrained — these asserts are the wire contract
a reimplementer must honour:

| assert (byte-exact) | rule |
| --- | --- |
| `EventSemaphore must have <= 2 wait commands, and <= 1 update command. TODO: support 2 update command` | a slot WAITs on **≤ 2** producers, signals **≤ 1** semaphore |
| `EventSemaphore can wait for 1 or 2 instrs` | the wait fan-in is exactly 1 or 2 |
| `EU Instruction should update all its semaphores` | an **execution-unit** (datapath PE/ACT/POOL/DVE) inst signals **every** semaphore on its update list |
| `non-EU Instruction can only have one semaphore update` | a **non-EU** (sequencer) inst signals exactly **one** |
| `datapath semaphore must be updated by datapath instr` | the engine semaphore belongs to the datapath |
| `<inst> is not a datapath engine instruction, which must not share the engine semaphore with other synchronous engine instructions` | a sequencer op must not co-update the datapath engine semaphore |
| `currSemaphore < HwmCore->NumSemaphores` | the per-core semaphore-count ceiling (`semaphores are not enough for `) |
| `Too many sync wait commands` / `Too many sync update commands` | the reject forms |

`AllocSemaphores::setupSync` + `incrementSemaphore` place the actual wait/inc
commands; `Enable trivial semaphore allocation.` is the **LNC1 fast path** (one
semaphore per engine, no rotation). `GroupResetSemaphores`
(`bir::InstGroupResetSemaphores`) resets a semaphore **group** per inference
(`Duplicated semaphores found in GroupResetSemaphores sema_group.`) — the
`add_sema_reset` the [assembly-pipeline.md POSTAMBLE](../neff/assembly-pipeline.md)
carries. `[HIGH/OBSERVED]`

> **QUIRK — the ≤ 2-wait / ≤ 1-update asymmetry is *the* reason CoreBarrier
> exists.** A semaphore slot cannot express a 3-way join (fan-in > 2) or a 2-way
> fan-out signal. So the sync hierarchy is two-tier: **pairwise `EVENT_SEMAPHORE`**
> for intra-loopnest cross-engine edges with fan-in ≤ 2, **`CoreBarrier`** (§3.6)
> for cross-loopnest / shared-HBM / fan-in > 2 joins. A reimplementer who tries to
> express a 4-producer join in one slot hits `Too many sync wait commands`.
> `[HIGH/OBSERVED limit; the "barrier folds the over-fan-in join" reading
> INFERRED-HIGH]`

### 3.4 The GPSIMD↔DVE / Pool↔DVE deadlock preventer `[HIGH/OBSERVED — GPSIMD-critical]`

**THE PROBLEM.** `POOL`(GPSIMD) and `DVE` are both datapath engines sharing SBUF,
and each op both waits and signals. Two ops `P` (POOL) and `D` (DVE) can form a
**cyclic semaphore wait**: `P` waits on `D`'s semaphore, `D` waits on `P`'s — a
hang. (The general scheduler topo-order prevents this *across* a single dep DAG,
but loop-carried deps and the shared-SBUF rendezvous of a collective can
reintroduce the cycle.)

**THE FIX — three OBSERVED pieces:**

**(a) The dedicated pass** `neuronxcc::backend::sync_dve_pool(bir::Function&)`
(`_ZN9neuronxcc7backend13sync_dve_poolERN3bir8FunctionE @0x156f560`; build name
`sync-pool-dve`, symbol `_deadlock_preventer`), gated on
`I.getEngine() == EngineType::DVE || I.getEngine() == EngineType::Pool`. It walks
the POOL/DVE ops and inserts the breaking sync.

**(b) The two-semaphore-update trick.** The rule strings are byte-exact:

```text
must have sync info for handling GPSIMD/VecEng deadlock
to handle GPSIMD/VecEng deadlock, the GPSIMD instruction must have more than 1 semaphore to update
GPSIMD engine semaphore update value must be +1 for handling GPSIMD/VecEng deadlock
```

The GPSIMD(`POOL`) op is given a **second** semaphore update (value `+1`) so the
`DVE` side can observe partial progress and break the cycle. This is why a GPSIMD
op may legally carry **2** sema updates while the general non-EU rule (§3.3) is
exactly 1 — the *EU-updates-all-its-semaphores* rule admits it (GPSIMD is an EU).

**(c) The DRAIN.** `bir::InstDrain` is a **first-class BIR instruction**
(`CoreV2GenImpl::visitInstDrain @0x122d880`, plus visitors in `birsim` /
`birverifier`; the per-gen `dbg_is_valid_drain` for `core_v2/v3/v4`; the SEQ
opcode `s_opcode_drain`). It forces the engine to **flush all outstanding writes**
before the dependent op proceeds — converting a potential cyclic semaphore wait
into a linear flush+wait. `ProcessInstLoopDeps::insertDrainAfter(InstLoop*,
EngineType) @0x11424d0` inserts loop-scoped drains (`before_loop_drain_`,
`loop-carried-dep-drain-`); `ProcessInstLoopDeps::insertBarriers @0x1142d10`
inserts loop barriers; `BirUtils::addTc4Deadlock(MemoryLocation&, std::string
const&, Instruction&) @0xd402d0` adds the breaking transfer-complete wait
(`SBIns to add TC for deadlock prevention`). The scheduler **prices** the drain it
will insert via `getLatencyWriteDrain` — **not a single symbol but a per-`Hwm`
(per-gen) family**: the base `bir::Hwm::getLatencyWriteDrain` is undefined here,
with one concrete body per gen (`TrainiumHwm @0x18518a0`, `Gen3Hwm @0x1857c80`,
`CoreV4Hwm @0x185e500`), each pricing the drain per producing op class
(`Matmult` / `TensorTensor` / `DMACopy` / `CustomOp` / `BIRKernel` / `TensorLoad`
/ `Save` / `Memset` / `Select` / `IndirectLoad` / `CollectiveRecv` / …).

```c
// sync_dve_pool — the GPSIMD<->DVE deadlock break. All strings/symbols OBSERVED.  [HIGH]
// Build name "sync-pool-dve", symbol _deadlock_preventer.
void sync_dve_pool(Function &fn) {
    for (Inst *I : fn.insts()) {
        if (!(I->getEngine() == EngineType::DVE || I->getEngine() == EngineType::Pool))
            continue;                                   // gate: DVE || Pool only
        if (!forms_cyclic_wait_with_partner(I)) continue;

        // (c) DRAIN: flush the producer engine's outstanding writes first.
        //     bir::InstDrain — visitInstDrain per gen; loop-scope via insertDrainAfter.
        InstDrain *drn = insertDrainAfter(enclosing_loop(I), I->getEngine());

        // (b) give the GPSIMD(POOL) op a SECOND semaphore update of +1 so DVE
        //     observes partial progress and the cycle breaks. EU-updates-all admits 2.
        if (I->getEngine() == EngineType::Pool) {
            assert(I->numSemaphoreUpdates() > 1 &&
                   "to handle GPSIMD/VecEng deadlock, the GPSIMD instruction must have "
                   "more than 1 semaphore to update");
            incrementSemaphore(I, breaking_sem, /*delta=*/+1);  // "...must be +1..."
        }
        addTc4Deadlock(I->loc(), "tc_deadlock", *drn);  // "SBIns to add TC for deadlock prevention"
    }
}
```

> **GOTCHA — the DRAIN is a *compiler-scheduled* SEQ instruction, not a runtime
> artifact.** The `DRAIN` + `EVENT_SEMAPHORE` pair the runtime emits at a
> cross-engine join (the device-side `DRAIN` op) is the **lowered form of this
> pass** — the Pool↔DVE deadlock break plus the cross-engine join semaphore. A
> reimplementer who treats `DRAIN` as something the device inserts on its own will
> never reproduce the correct GPSIMD scheduling; the `DRAIN` is decided **here**,
> priced by `getLatencyWriteDrain`, and emitted into `pool.bin`. See the Part-13
> device sync/events page at `neuronx-gpsimd/wiki/src/control/...` for the device
> decode. `[HIGH/OBSERVED pieces; the cyclic-break reading INFERRED-HIGH]`

### 3.5 The trigger engine — `assign_hwdge_engine` then `assign_trigger_engine` `[HIGH/OBSERVED]`

Every DMA must be **fired** by some engine's sequencer (the engine writes the
queue tail-pointer doorbell). Which engine owns the queue, and which fires the
trigger, is a **two-pass** decision.

**`assign_hwdge_engine.cpp`** (`AssignHWDGEEngine`: vtable `@0x3d94eb0`,
`run(Module&) @0x117eb90`, `AssignHWDGEEngineImpl::assignEngine @0x1180310`)
picks which datapath engine owns each HWDGE (RTL/hardware descriptor-gen, NC-v3+)
queue:

```text
HWDGE must be on ACT/DVE/SP according to assign_hwdge_engine pass
Number of valid engines for HWDGE must be greater than or equal to 2
```

bounded by `board.device.core.RtHwdgeQueueLimit` (`NumHWDGE <= RtHwdgeQueueLimit`);
`core_v5` (maverick) has its own HWDGE-queue path
(`queue->getIsHWDGE() && archLevel >= core_v5`); `bir::isHWDGEDMAWithEngineSet`
(an undefined ref here — defined in the bir core lib) checks the
`(DGEType==HWDGE && engine-set)` pair.

**`assign_trigger_engine.cpp`** (`AssignTriggerEngine`: vtable `@0x3d94e48`,
`run(Module&) @0x1179b50`) then picks which engine fires each trigger
(`Assigned trigger engine for `; the scheduler-coupled `[Scheduling trigger
engine] Schedule ` under `enable-hwdge-trigger-engine-scheduling`; verifier
`Trigger engine does not match queue's engine`). `InstDMATrigger` is the fired
trigger inst.

**SWDGE is forced onto GPSIMD/POOL:** `SW DGE must be on GPSIMD engine`; the
spill/CC reload DMAs prefer SWDGE (`spill-reload-dmas-use-swdge`,
`cc-linked-use-swdge`).

| DGE kind | trigger engine | the semaphore consumers wait on |
| --- | --- | --- |
| **HWDGE** (NC-v3+) | `ACT` / `DVE` / `SP` (≥ 2 valid) | the ACT/DVE/SP queue semaphore |
| **SWDGE** | `POOL` (GPSIMD/Q7) | the `POOL`(GPSIMD) semaphore |

The consequence for sync: **the trigger engine's semaphore is the one the DMA's
consumers wait on.** Mis-assigning it (trigger on engine A, queue on engine B) is
the verifier error `Trigger engine does not match queue's engine`. So a SWDGE DMA
— the GPSIMD spill/CC-linked path — signals completion through the POOL semaphore,
and any consumer on another engine waits on it. `[HIGH/OBSERVED]`

### 3.6 The cross-loopnest / shared-HBM barrier — `InsertCoreBarrier` + `CoreBarrier` `[HIGH/OBSERVED]`

The **Cython** `InsertCoreBarrier` pass
(`targets/transforms/InsertCoreBarrier.cpython-311.so`) docstrings are byte-exact:
*"InsertCoreBarrier -- Insert Corebarrier on shared HBM tensors"* and *"Insert
corebarrier if there are raw, war, and waw dependencies cross loopnests"*
(`transformCoreBarrierIntrinsic` / `insertCoreBarrierOnIO`). The backend
`CoreBarrier` (`bir::InstCoreBarrier`; the `libwalrus` strings `CoreBarrier(`,
`core barrier id: `, `Create separate corebarriers for shared lifetime extension
instead of re-using existing corebarriers`, `Done renumbering core barriers.`) is
an **all-engine barrier** (`_all_eng_barrier`, `Add All-Engine-Barrier for LOC`):
every engine stops at it, so a cross-loopnest RAW/WAR/WAW — or a shared-HBM tensor
that would need too many pairwise semaphores, or a fan-in > 2 join (§3.3) — is
resolved by **one barrier** instead of `O(n²)` pairwise semaphores.

> **CORRECTION (vs an earlier report).** The two `InsertCoreBarrier` docstrings
> (*"Insert Corebarrier on shared HBM tensors"* / *"…raw, war, and waw … cross
> loopnests"*) are **not** in `libwalrus.so` — `strings libwalrus.so` has no
> *"Insert Corebarrier"* / *"shared HBM tensors"* / *"cross loopnest"* match. They
> live in the **separate Cython pass-host** `InsertCoreBarrier.cpython-311.so`
> (`targets/transforms/`), where they are byte-exact. The backend `libwalrus`
> strings (`CoreBarrier(`, `core barrier id:`, `Done renumbering core barriers.`,
> `_all_eng_barrier`) and the `bir::InstCoreBarrier` ctor references *are* in
> `libwalrus`. A reimplementer who greps only `libwalrus` for the docstrings finds
> nothing — the docstring source is the Python pass, the runtime side is the C++
> backend. The mechanism is unchanged; only the cited binary is corrected.
> `[HIGH/OBSERVED — `strings libwalrus.so` absence + Cython pass presence.]`

`lnc_barriercheck.cpp` verifies `active_barriers == 0 || active_barriers ==
Options.vnc_nc_count` — a barrier must be entered by **all** `vnc_nc_count` cores.

The device-side execution of `CoreBarrier` is the `ln` 256-semaphore array:
`arrive` writes `ln +0x1800 + i*4`, `wait` polls `ln +0x1000 + i*4 >= N`, optional
reset writes `ln +0x1C00 + i*4` (see §3.9 and
[collective-loadtime-rewrite.md](collective-loadtime-rewrite.md) / the device
core-barrier page). `[HIGH/OBSERVED docstrings + backend strings; CARRIED device
`ln` decode]`

### 3.7 LNC > 1 point-to-point completion — `insert_ptcom_flat` `[HIGH/OBSERVED]`

For multi-NeuronCore (`LNC > 1`), `InsertPTCOMFlat` inserts a PTCOM
(point-to-point completion) after a `CoreBarrier` following the last write to an
output tensor. Real methods + addresses (`nm`):

```text
InsertPTCOMFlat::insertPTCOMs(std::vector<std::pair<bir::MemoryLocation*, bir::Instruction*>> const&)  @0x16acc10
InsertPTCOMFlat::extendLastWrites(...)                @0x16abd40
InsertPTCOMFlat::findCBOnCore0(bir::InstCoreBarrier const*)   @0x16a7fd0
InsertPTCOMFlat::run(bir::Module&)
```

The byte-exact option doc: *"For LNC > 1, PTCOM instructions are inserted after a
CoreBarrier following the last write to an output tensor. This controls how many
CoreBarriers after the last write the PTCOM should be inserted … The default value
is 3. This option does nothing for LNC1."* (`meetsPTCOMRequirements(bb,cfg)`;
`enablePTCOM` / `-edp-ptcom`). For LNC1 the whole LNC machinery is skipped
(`Detected LNC=1; running LNC=1 post_sched`; `Enable trivial semaphore
allocation.`). `[HIGH/OBSERVED]`

### 3.8 The multi-buffer semaphore / address rotation — `address_rotation_{sb,psum,dram}` `[HIGH/OBSERVED]`

`AddressRotation::run(Module&)` (TBB-parallel: `blocked_range` over
`sb_rotation(SB_ROTATE_TARGET, …)` + `clearInstDeps`) rotates the SBUF/PSUM/DRAM
**address** of a multi-buffered tensor across iterations (the host realization of
the `ModuloAllocation` bank rotation in
[tiling-memory-scheduling.md §4.3](tiling-memory-scheduling.md)) **and** rotates
the **semaphore** guarding each buffer:
`AllocSemaphores::addSemaphoreRotationWait` places the rotation wait so iteration
`i+1`'s producer waits on iteration `i-1`'s consumer of the **same physical
buffer** (the classic N-deep software-pipeline semaphore rotation).

Scopes: `AddressRotationScope::{None, Global, Kernel}`; `allreduce-rotation-dis` /
`Allreduce rotation distance`; `dram-rotation-size` (default 8G); a post-schedule
re-run `address_rotation_psum_post_schedule`;
`memory_analysis_after_address_rotation_{sb,psum,dram}` re-validates. The assert
`engine != Unassigned && "Have to specify an EngineType when using SaveToEngine/
EngineToLoad as rotation target"` pins that the rotation target is per-engine. So
the SEQ semaphores that order a pipelined loop's buffers are the rotation
semaphores, sized by the modulo (multi-buffer) factor. `[HIGH/OBSERVED]`

### 3.9 The device EVT_SEM windows the emitted sync resolves to `[CARRIED — Part-10 device pages]`

The abstract `(OnWait, OnUpdate)` SyncInfo this backend produces lowers (§4) to a
SEQ slot that the device's TOP_SP-hosted **`ln`** 256-semaphore unit executes
through four operation-aliased windows (`+ idx*4`, `ArraySize 256`), confirmed
byte-identical across all four shipped gens:

| op | window | role |
| --- | --- | --- |
| READ / WAIT-GE | `ln + 0x1000` | the consumer's `$S[k] >= v` poll |
| SET (init/reset) | `ln + 0x1400` | semaphore init / `GroupResetSemaphores` |
| INC (signal) | `ln + 0x1800` | the producer's `$S[j]++@complete` arrive |
| DEC (consume) | `ln + 0x1C00` | the WAIT-GE-and-DEC consume leg |

So the compiler's UPDATE → device `ln +0x1800 + idx*4` INC, the WAIT → device
`ln +0x1000 + idx*4` WAIT-GE, the `GroupResetSemaphores` POSTAMBLE → device
`ln +0x1400` SET. This is the seam between this page (the producer) and
[collective-enums.md](../collectives/ops/collective-enums.md) /
[architecture-synthesis.md](../collectives/ops/architecture-synthesis.md) (the
device consumer). `[CARRIED — `ln` windows OBSERVED on the Part-10 pages
`cayman_get_sp_sema_{r,s,i,d}_ofst @0x25c130/0x25c360/0x25c2e0/0x25c260`.]`

---

## 4. Stamping the wait/set into the slot — `SetupSemaphoreUpdate` + `lower_sync`

The abstract `(OnWait, OnUpdate)` SyncInfo is written onto each instruction by
`SetupSemaphoreUpdate` (real methods + addresses, `nm`):

```text
SetupSemaphoreUpdate::visitInstruction          @0x113e8b0    // per-inst dispatch
SetupSemaphoreUpdate::setupDMASemaphoreUpdate    @0x113dee0    // the DMA-completion case
SetupSemaphoreUpdate::enterInstLoop              @0x1146290    // loop scopes
```

`visitInstruction` writes the `bir::SyncInfo {OnWait, OnUpdate}` onto the
instruction (dispatched by ISA slot kind — `CTRL_AL/BR/ER/MV/NO`, `D1_RD`,
`D4_IOTA/MR/RAND`, `MEM_2D`, `EXTENDED`, `CUSTOM_OP_PAYLOAD`, `PSEUDO_BR_HINT`, …,
per gen `core_v2/v3/v4`); `setupDMASemaphoreUpdate` is the DMA specialization;
`enterInstLoop` handles loop scopes. The verifier re-checks every SyncInfo field
(`OnWait`/`OnUpdate` `id`/`mode`/`size`/`type`/`value` mismatch;
`getLocalSemaphore(SI.OnUpdate) == semaphore`; `hasSingleLocalUpdate(SI.OnUpdate)`).

> **CORRECTION (vs an earlier report).** That report named the per-slot stamping
> method `setupSyncUpdate<NEURON_ISA_TPB_*_STRUCT>` (a templated function with one
> instantiation per slot kind). **`strings`/`nm` of `libwalrus.so` carry no
> `setupSyncUpdate` symbol or string at all** — the actual stamping surface is the
> `SetupSemaphoreUpdate` class above (`visitInstruction @0x113e8b0` /
> `setupDMASemaphoreUpdate @0x113dee0` / `enterInstLoop @0x1146290`); the
> per-slot-kind dispatch is inside `visitInstruction`, not a separate templated
> `setupSyncUpdate`. The separate `AllocSemaphores::setupSync` (§3.1, the
> wait/inc placement) is a *different* method — do not conflate the two. The
> mechanism (per-(slot-kind, gen) SyncInfo write, then `lower_sync`) is unchanged;
> only the method name is corrected. `[HIGH/OBSERVED — `SetupSemaphoreUpdate`
> symbols present with addresses; `setupSyncUpdate` absent from binary.]`

Then `lower_sync.cpp` (`Instruction must have correct engine type assigned before
lower_sync pass`) **lowers** the abstract `SyncInfo` to the concrete per-slot
bytes: `instr.semaphore_wait_value` / `instr.semaphore_wait_value_hi` (the 64-bit
wait threshold split lo/hi) + the set/increment field. The codegen generator
`CoreV2GenImpl` then emits the 64-byte SEQ slot with those fields (real visitor
addresses, `nm`):

```text
CoreV2GenImpl::visitInstEventSemaphore         @0x1217da0
CoreV2GenImpl::visitInstGroupResetSemaphores   @0x122b9f0
CoreV2GenImpl::visitInstDrain                   @0x122d880
CoreV2GenImpl::visitInstCustomOp               @0x1261370
```

The full producing chain for the `$S[10]>0 $S[22]++@complete` slot:

```text
AntiDependencyAnalyzer edge  (§2)
  -> alloc_semaphores assigns IDs 10 (wait) / 22 (update)  (§3.1-3.2)
  -> SetupSemaphoreUpdate::visitInstruction @0x113e8b0 writes
        SyncInfo{ OnWait=$S[10]>0, OnUpdate=$S[22]+=1@complete }  (§4)
  -> lower_sync stamps slot bytes semaphore_wait_value / _hi + the set field
  -> CoreV2GenImpl::visitInstEventSemaphore @0x1217da0 emits the 64-byte SEQ slot
  -> <eng>.bin  ->  NEFF (assembled by ib_create_one_block, assembly-pipeline.md)
  -> device: ln WAIT-GE +0x1000 (the $S[10] poll) / ln INC +0x1800 (the $S[22]++)  (§3.9)
```

`[HIGH/OBSERVED — `SetupSemaphoreUpdate`/`CoreV2GenImpl` visitor symbols+addresses
+ `lower_sync` asserts; the `.bin`/`ln` ties CARRIED
[assembly-pipeline.md](../neff/assembly-pipeline.md) + the Part-10 pages.]`

---

## 5. The per-engine stream ordering — `TimeAwareScheduler` (the correctness floor)

Before the semaphores are stamped, the schedule must already be **topo-correct**,
or no semaphore set can save it. `neuronxcc::backend::TimeAwareScheduler`
(`dep_based_optims/time_aware_sched.cpp` + `inst_sch.cpp` + `sched_common.cpp` +
`pre_sched.cpp` + `post_sched_pass.cpp` + `shift_left_scheduler.cpp` +
`lnc_aware_sched.cpp`). Real methods + addresses:

```text
TimeAwareScheduler::linearize_insts_by_simulation(bir::BasicBlock&)            @0xc61180
TimeAwareScheduler::pick_candidate()                                          @0xc618f0
TimeAwareScheduler::reschedule_cc_related_inst(TimeAwareScheduler::Candidate&) @0xc577a0
TimeAwareScheduler::is_better_trigger(...)
```

### 5.1 The correctness invariant — a topological order `[HIGH/OBSERVED]`

`khan_topo_sort(const BasicBlock&)` and `df_topo_sort` produce a linear order in
which **every producer precedes every consumer** — a Kahn topological sort over
the `AntiDependencyAnalyzer` edges (§2). `pre_sched::dfs_topo_sort` over
`MemoryLocation*` is the memory-side variant. Because the order respects the
FLOW/ANTI/OUTPUT/ORDERED edges, the subsequent semaphore allocator only ever has
to enforce edges that survive **between different engines** — within one engine
the program order already serializes. **This is the structural guarantee that the
inserted semaphores are sufficient:** any RAW/WAR/WAW not covered by same-engine
program order crosses an engine boundary and gets a semaphore. `[HIGH/OBSERVED
topo-sort methods; the sufficiency reading INFERRED-HIGH]`

### 5.2 The ready-list + priority `[HIGH/OBSERVED]`

`mark_as_ready` adds an inst to the ready list once its predecessor ref-count
(`InstNumDes`, §2.3) hits zero. `ready_list_insert_by_eng_start` /
`_remove_by_eng_start` / `ready_list_find` keep a **per-engine** ready list keyed
by the earliest time that engine can start the inst; `pick_candidate` /
`pick_non_lnc_sync_candidate` select the next; `is_better_instruction` (the
`ReadyInstCmp::operator()`) is the priority comparator. `schedule_inst` places
one; `revert_schedule_inst` un-places on backtrack;
`update_ready_time_in_ready_list` refreshes start times. `InstMetrics` holds the
per-inst cost (from `InstructionLatencyModel` /
[tiling-memory-scheduling.md §6.2](tiling-memory-scheduling.md)).

### 5.3 Register-pressure awareness `[HIGH/OBSERVED]`

`check_skip_due_to_register_pressure` + `update_reg_pressure_for_candidate` skip a
candidate whose scheduling would exceed the live-byte budget (the
`def_bytes`/`kill_bytes`/`alloc_size_in_bytes` tie-break, the same liveness bias
the Python list scheduler uses — the two-level scheduler agrees).

### 5.4 GPSIMD / CC + DMA-trigger special cases `[HIGH/OBSERVED]`

`reschedule_cc_related_inst` re-places the GPSIMD/compute-cluster ops specially —
their large fixed launch cost (the ~150-cycle `GPSIMD_START`,
[tiling-memory-scheduling.md §6.2](tiling-memory-scheduling.md)) makes the
scheduler **cluster** GpSimd work coarsely rather than interleave it finely.
`is_better_trigger` orders the DMA triggers; `lower_hwdge_triggers` materializes
HWDGE triggers; `delete_dma_triggers` / `delete_unselected_generic_insts` clean
up; `replace_generic_with_lowering_choices` picks the concrete lowering for an
engine-poly op. The post-schedule compaction is `shift_left_scheduler.cpp` (ASAP);
`lnc_aware_sched.cpp` does the LNC > 1 ordering
(`postsched-cc-dma-priority` / `postsched-cc-dma-max-reschedule` tune it). The
bounded out-of-order reorder window is `instruction_reorder.cpp`
(`InstructionReorder::reorder`; `getReorderWindowSize`; `disable-reorder-opt`),
producing the `bir.scheduled.after-ooo.json` checkpoint. `[HIGH/OBSERVED]`

---

## 6. The GPSIMD-relevant optimization passes

These run **before** sync insertion. Every op they remove is a dep edge that does
not need a semaphore, so they directly shrink §2–§3.

### 6.1 `InferIntrinsicOnCC` — the FMA-to-DMA offload (the keystone GPSIMD opt) `[HIGH/OBSERVED]`

File: `targets/sunda/passes/InferIntrinsicOnCC.cpython-311.so` (pyx symbols +
docstring). Docstring, byte-exact:

> *"InferIntrinsicOnCC - Find and offload the `T = c0 * T0 + c1 * T1` … to the DMA
> engine."*

The pass (`transformStmts` → `inferIntrinsicOnDAG` → `checkAndExtractRoot`) walks
an elementwise DAG (`enumerate_expr_tree_leaves`), peels the scale coefficients
(`extract_scale` / `scale_tensors`), and rewrites the chain into **one** of:

| output | what it folds | device meaning |
| --- | --- | --- |
| `OffloadedFMA` | `c0*T0 + c1*T1 (+…)` | a DMA whose **CCE meta-control word** does `scale*src + accum`; the compute op + its sync edges vanish |
| `OffloadedMemCpy` | a reshape-only move (`is_memcpy`) | folded into the DMA |
| `OffloadedMemCast` | a reshape→cast | folded into the DMA |
| `BroadcastScalar` | a scalar store across a partition (`infer_broadcast_dag` / `isScalar`) | `Number of scalar stores broadcasted to a whole partition` |

**THE GATES (the policy):**

- `min_nonlocals_to_do_fma` — only fuse when the count of **non-local** operands
  (`num_src_nonlocal + num_dst_nonlocal + num_nonlocals`, computed in
  `setNonLocalTensors` / `is_intrinsic_candidate_tensor`) `>=` this threshold.
  Rationale: if the operands already cross SBUF↔HBM via DMA, the in-transfer FMA
  is **free**; if they are all SBUF-local, a POOL/DVE compute op is cheaper than
  forcing a DMA.
- `keep_mult_on_pool` — keep a pure multiply on the `POOL`(GPSIMD) engine rather
  than offloading (when the multiply is the only op and POOL is idle).
- `PoolOnlyInt32Ops` — int32 ops pinned to `POOL`/GpSimd; they are **not** FMA
  candidates (the CCE FMA is float).
- `internal-disable-fma-on-ios` (`InputOrOutputOrConstOrNonLocal`) — *"Disable
  generating FMA operations on input or output tensors"*: never fold an FMA whose
  root touches a kernel arg/const (the arg must stay materialized).

Counters: `num_fma_inferred` (*"Number of offloaded FMA inferred"*),
`num_broadcast_scalar`.

**GPSIMD meaning.** This is the **host half** of the CCE in-transfer reduce: the
compiler removes a whole `c0*T0 + c1*T1` compute op *and* its cross-engine
semaphores by folding it into a DMA the schedule needed anyway. The base
`transforms/InferIntrinsic` carries the same docstring; `InferIntrinsicOnCC` is
the `sunda`/CC-region specialization. `[HIGH/OBSERVED docstring + gate strings +
counters; the FMA↔CCE identity CARRIED from the collective pages.]`

### 6.2 The scalar / IR optimizations — "make the dep graph smaller" `[HIGH/OBSERVED]`

Each fewer op = fewer dep edges = fewer semaphores:

| pass | file | what it rewrites (docstring/symbol, byte-exact) |
| --- | --- | --- |
| **`ValueNumbering`** (GVN/CSE) | `transforms/ValueNumbering` | *"performs value numbering to eliminate fully redundant instructions … by hashing the penguin `<IR>`"* (`transformBlock` / `transformElementwiseOp` / `transformAffineLoad`). Two identical ops collapse to one. The backend re-runs its own `value_numbering/gvn.cpp` + a `tonga` `TongaValueNumbering`. |
| **`DeadCodeElimination`** | `transforms/DeadCodeElimination` | *"Eliminate instructions with no users. This pass will be run iteratively till no more dead instructions can be eliminated"* (`eliminateDeadInst` / `recursivelyDCE`). |
| **`SpmdDCE`** | `targets/transforms/SpmdDCE` | the SPMD variant (`_is_tensor_dead` / `simplifySPMDCCop`). |
| **`DeadStoreElimination`** | `transforms/DeadStoreElimination` | drops dead writes. |
| **`TensorOpSimplifier`** | `transforms/TensorOpSimplifier` | **copy/op FUSION**: *"Simplify TensorOps, including fusion copy ops … OffloadedMemCpy supports reshape; OffloadedMemCast supports reshape→cast; OffloadedTranspose supports reshape→cast→transpose"* (`_fuseSubTensorOpToHigherTensorOp` / `_fuseCascadedOffloadedSlice` / `_fuse_transpose_permutations` / `_fuseOnDstMultiMemcpies`). A copy fused into its producer/consumer removes a SEQ slot **and** a sync edge. |
| **`Simplifier`** | `transforms/Simplifier` | canonicalizes (`fold_copy_src/dst`, `fold_broadcast_reduce`, `fold_tensorcontract_reduce`, `can_eliminate_load_store_pair`). |
| **`SuperSimplifier`** | `transforms/SuperSimplifier` | *"Iterative optimizations of multiple simplification passes"* — an `IterativePassManager` looping `{Simplifier + ValueNumbering + DeadCodeElimination}` to a **fixpoint**. |
| **`constant_propagate`** | backend `constant_propagate.cpp` | folds known constants (`constant_propagate_visitor`), enabling more DCE/CSE. |

These are scheduled in `CodeGenFlow`'s `codegen_optimization_eager` /
`codegen_prepare_eager` / `optimize_eager` (the per-op NKI/eager path) and the
main flow. `[HIGH/OBSERVED docstrings + pyx symbols + `CodeGenFlow` module refs.]`

### 6.3 The CC-op / loop fusion family — the GPSIMD-cluster coarsening `[HIGH/OBSERVED]`

`CCOpFusion` / `FineGrainedCCOpFusion` / `CoalesceCCOp` / `BucketizeCCOp`
(`Bucketing ccop in compiler`; `enable_ccop_compute_overlap` /
`enable_fine_grained_ccop_compute_overlap`) fuse adjacent compute-cluster ops into
one `NeuronMacro` (the BIR-level `finalizeSIMDMacro`,
[tiling-memory-scheduling.md §2.2](tiling-memory-scheduling.md)), overlapping CC
compute with the collective DMA. `LoopFusion` / `NeuronLoopFusion` /
`PartialLoopFusion` / `PartialSimdFusion` / `TritiumFusion` fuse loop nests
(`enable-tritium-loopfusion`; `enable-memory-pressure-driven-loop-fusion` *"may
rematerialize weight loads"*). `Rematerialization` (`transforms/Rematerialization`
+ backend `remat_optimization.cpp`) **re-computes** a cheap tensor at its use
instead of keeping it live — trading a sync-guarded live buffer for a recompute
(fewer live edges, less spill). `coalesce_dma_blocks` /
`coalesce_multichannel_cc_ops` / `input_dma_coalescing` merge adjacent DMAs (fewer
DMA-completion semaphores); `dma_optimization` (`allocation_optimization.cpp`) +
`load_cloning` + `tensor_cp_elim` (`tensor_cp_elim.cpp`, the backend copy-elim)
round out the DMA-side cleanup. `[HIGH/OBSERVED]`

### 6.4 The QoS labeling — `AssignDMAQoSLabels` (the sync↔DMA bridge) `[HIGH/OBSERVED]`

`AssignDMAQoSLabels` (`targets/transforms/`): *"Static assignment of DMA QoS
labels for Collectives."* Per-op-class transforms (`transformMatMulOp` /
`transformSendRecvOp` / `transformSendRecvCCEOp` / `transformCollectiveComputeOp`
/ `assign_weight_prefetch_labels`) stamp the `DMAQoSClass` so the SDMA arbiter
prioritizes the right stream; `_dma_qos_for_spill_code` labels spill DMAs;
`max_dma_qos_classes` / `internal-dma-qos-class-count` bound the class count. The
backend `label_dma_qos` pass carries it through. This is the optimization that
decides **which DMA gets priority**, feeding the §3.5 trigger engine.
`[HIGH/OBSERVED]`

---

## 7. End-to-end — a GPSIMD RmsNorm tile through sync insertion `[HIGH]`

For a GPSIMD-touching RmsNorm tile (`square(ACT)` → `reduce(DVE)` → `mul(DVE)` →
`ones-matmul(PE)` → `rsqrt(ACT)` → `mul(DVE)`, input staged by DMA, optionally an
LNC2 sendrecv on `POOL`):

1. **OPT (§6).** `SuperSimplifier` (CSE + DCE + canonicalize) + `TensorOpSimplifier`
   fold the copies; `InferIntrinsicOnCC` checks the `mul`/`add` chain — if operands
   are non-local it offloads an `OffloadedFMA` to the staging DMA (else keeps the
   DVE mul); `AssignDMAQoSLabels` labels the input-stage DMA + any sendrecv;
   `CCOpFusion` packs the elementwise body into one CC macro.
2. **SCHEDULE (§5).** `TimeAwareScheduler` `khan_topo_sort`s the dep DAG so
   `square → reduce → ones-matmul → rsqrt → final-mul`; interleaves the five engine
   streams (ACT: `square,rsqrt`; DVE: `reduce,mul,mul`; PE: `ones-matmul`; POOL:
   the sendrecv) by earliest engine-start, skipping any candidate that blows
   live-byte pressure; `instruction_reorder` applies the bounded OOO window;
   `post_sched` compacts.
3. **DEP EDGES (§2).** `AntiDependencyAnalyzer-SB` finds `square(ACT)→reduce(DVE)`
   FLOW, `reduce(DVE)→ones-matmul(PE)` FLOW; `AntiDependencyAnalyzer-PSUM` finds
   `ones-matmul(PE)→rsqrt(ACT)` FLOW (the only PSUM tensor) and `rsqrt(ACT)→mul(DVE)`
   FLOW; the `input-DMA→square(ACT)` FLOW. `dep_reduction` prunes the transitive
   `square→mul` edge.
4. **SYNC INSERT (§3).** `assign_hwdge_engine` / `assign_trigger_engine` pick the
   input-DMA's trigger (ACT/DVE/SP if HWDGE, POOL if SWDGE);
   `alloc_semaphores`/`Synchronizer` assigns a completion semaphore to each
   producer and a wait to each consumer:

   ```text
   reduce(DVE)      WAIT  $S[a] >= v_sq    square(ACT)      UPDATE $S[a]+=1
   ones-matmul(PE)  WAIT  $S[b] >= v_red   reduce(DVE)      UPDATE $S[b]+=1
   rsqrt(ACT)       WAIT  $S[c] >= v_mm    ones-matmul(PE)  UPDATE $S[c]+=1@complete  (PSUM)
   mul(DVE)         WAIT  $S[d] >= v_rsq   rsqrt(ACT)       UPDATE $S[d]+=1
   ```

   `SetupSemaphoreUpdate::visitInstruction` stamps the `(OnWait, OnUpdate)` onto
   each slot. If the POOL
   sendrecv and a DVE op mutually wait, `sync_dve_pool` inserts a `DRAIN` + gives
   the POOL(GPSIMD) op a 2nd `+1` update to break the cycle (§3.4). If the tile
   crosses a loopnest or shares an HBM tensor with the next tile, `InsertCoreBarrier`
   adds an all-engine `CoreBarrier`. For LNC2, `insert_ptcom_flat` adds the PTCOM
   after the last-write `CoreBarrier`.
5. **ROTATE + LOWER (§3.8/§4).** If the BxS loop is multi-buffered,
   `address_rotation_psum` rotates the PSUM tensor's bank + its rotation-wait
   semaphore; `lower_sync` stamps the concrete `semaphore_wait_value`/`_hi` into
   each slot.
6. **EMIT.** `CoreV{2,3,4}GenImpl` emits the descriptors + the semaphore fields
   into the per-engine 64-byte SEQ slots → the `def.json` `<eng>.bin`
   [assembly-pipeline.md](../neff/assembly-pipeline.md) loads; the POSTAMBLE's
   `GroupResetSemaphores` re-arms the semaphores per inference; on device the
   waits/updates resolve to the `ln` WAIT-GE `+0x1000` / INC `+0x1800` windows
   (§3.9).

`[HIGH — sync backend OBSERVED; the exact `$S[..]` values INFERRED-HIGH;
emit/wrap CARRIED assembly-pipeline.md.]`

---

## 8. Adversarial self-verification of the five strongest claims

Each claim re-grounded against the shipped artifacts; a sceptic's re-run is given.

1. **"The dep model is `AntiDependencyAnalyzer`, run once per memory class, with
   five `EdgeKind`s."** Re-run: `nm -DC libwalrus.so | rg AntiDependencyAnalyzer`
   → the vtable `_ZTVN9neuronxcc7backend22AntiDependencyAnalyzerE` and distinct
   `getSBPartitionRange` / `getPSUMPartitionRange` methods; `strings libwalrus.so |
   rg 'AntiDependencyAnalyzer-'` → `-SB` and `-PSUM` instance strings;
   `rg ANTI_DEPENDENCE` → the concatenated enum table
   `ANTI_DEPENDENCEFLOW_DEPENDENCEINVALIDORDEREDOUTPUT_DEPENDENCE`. **Three
   independent anchors — vtable, per-class instance strings, enum table. Holds.**
   The `tiling-memory-scheduling.md §6.3` predecessor page independently quotes the
   same `-SB`/`-PSUM` instances and enum table — **agreement, no divergence.**

2. **"The EventSemaphore wire limit is ≤ 2 wait / ≤ 1 update, verifier-pinned."**
   Re-run: `strings -n 8 libwalrus.so | rg 'EventSemaphore must have'` →
   `EventSemaphore must have <= 2 wait commands, and <= 1 update command. TODO:
   support 2 update command`, plus `EventSemaphore can wait for 1 or 2 instrs` and
   the rejects `Too many sync wait commands` / `Too many sync update commands`.
   **The exact byte-string is present; `tiling-memory-scheduling.md §6.5` quotes the
   identical assert. Holds.** Consequence (the CoreBarrier-for-fan-in-> 2 reading) is
   flagged INFERRED-HIGH in §3.3.

3. **"GPSIMD↔DVE deadlock is broken with a DRAIN + a +1 second semaphore update."**
   Re-run: `strings libwalrus.so | rg 'GPSIMD/VecEng deadlock'` → all three
   byte-exact rules (`must have sync info…`, `…must have more than 1 semaphore to
   update`, `…update value must be +1…`); `nm -DC | rg 'sync_dve_pool|addTc4Deadlock|
   insertDrainAfter|visitInstDrain'` → the pass + the drain inserter + the first-class
   `InstDrain` visitor. **The pass, the 2-update rule and the DRAIN are all real
   symbols/strings; the device `DRAIN` op in `pool.bin` is the lowered form
   (CARRIED assembly-pipeline.md). Holds.** The "breaks a cyclic Pool↔DVE wait"
   reading is INFERRED-HIGH from the engine gate (`DVE||Pool`) + the rule texts.

4. **"SWDGE triggers ride POOL(GPSIMD); HWDGE rides ACT/DVE/SP."** Re-run:
   `strings libwalrus.so | rg 'DGE must be on'` → `SW DGE must be on GPSIMD engine`
   and `HWDGE must be on ACT/DVE/SP according to assign_hwdge_engine pass`;
   `rg 'Number of valid engines for HWDGE'` → `… must be greater than or equal to
   2`; `rg 'Trigger engine does not match'` → the verifier reject. **Both DGE
   strings, the ≥ 2 rule and the verifier are present; this is the host root of the
   DMA `dma_queue {owner, semaphore}` rows assembly-pipeline.md consumes. Holds.**

5. **"`InferIntrinsicOnCC` offloads `c0*T0+c1*T1` to the DMA engine."** Re-run:
   `strings -n 5 …/InferIntrinsicOnCC.cpython-311*.so | rg -i 'offload.*DMA'` →
   *"InferIntrinsicOnCC - Find and offload the `T = c0 * T0 + c1 * T1` … to the DMA
   engine."*; `rg -i 'min_nonlocals|keep_mult_on_pool|PoolOnlyInt32|OffloadedFMA'`
   → the four gate/output strings; `rg 'Number of offloaded FMA inferred'` → the
   counter. **The docstring is byte-exact and the gate vocabulary is present; the
   FMA folds into the CCE in-transfer reduce the collective pages document. Holds.**
   The `min_nonlocals_to_do_fma` *numeric* threshold is a `.so` immediate, not a
   string — flagged MED/INFERRED in §9.

---

## 9. Confidence ledger & corrections

**HIGH / OBSERVED** (byte-exact): the registered pass spine
(`register_generator_<pass>` + `neuronxcc/walrus/<pass>/src/<pass>.cpp` leaks);
`AntiDependencyAnalyzer` vtable + `-SB`/`-PSUM`/`-DRAM`/`-Alias` instances +
`getSBPartitionRange`/`getPSUMPartitionRange`/`populate*UseMap`/`collect*Ranges` +
the `EdgeKind` enum table + the `Added flow/ordered/index dependency` logs +
`dep_reduction` `too_many_spills`; `AllocSemaphores`/`AllocateSemaphores` methods
+ addresses + the EventSemaphore wire asserts + `GroupResetSemaphores`; the
GPSIMD/VecEng deadlock strings + `sync_dve_pool`/`_deadlock_preventer` +
`addTc4Deadlock` + `bir::InstDrain` (`visitInstDrain`, `dbg_is_valid_drain` per
gen, `s_opcode_drain`) + `insertDrainAfter @0x11424d0`/`insertBarriers @0x1142d10` +
the per-`Hwm` `getLatencyWriteDrain` family; `assign_hwdge_engine` (ACT/DVE/SP, ≥ 2,
`RtHwdgeQueueLimit`, `core_v5`) + `assign_trigger_engine` + `SW DGE must be on
GPSIMD engine`; `InsertCoreBarrier` docstrings (Cython pass) + the `libwalrus`
`CoreBarrier`/`_all_eng_barrier` strings + `lnc_barriercheck`; `InsertPTCOMFlat`
methods (`insertPTCOMs @0x16acc10`) + the PTCOM-default-3 option doc;
`AddressRotation::run` + `addSemaphoreRotationWait`;
`SetupSemaphoreUpdate::visitInstruction`/`setupDMASemaphoreUpdate`/`enterInstLoop`
(per slot-kind, gen) + `lower_sync` `semaphore_wait_value`/`_hi`;
`InferIntrinsicOnCC` docstring + gates + counters; `ValueNumbering`/`DeadCode
Elimination`/`SpmdDCE`/`TensorOpSimplifier`/`SuperSimplifier` docstrings;
`CCOpFusion` family + `Rematerialization` + `AssignDMAQoSLabels`;
`TimeAwareScheduler` methods + addresses + the topo/ready-list/reg-pressure
surface.

**MED / INFERRED**: the exact semaphore-value arithmetic (`v` = producer-final-count
— the `assign*`/`setupSync` methods + the `Missing final semaphore value` /
`findIndirectLoadSaveSemaphoreWaitValue` asserts are OBSERVED, the precise
`v`-arithmetic INFERRED-HIGH from them + the read-back `$S[..]` examples); the
cyclic-wait → drain + 2-update deadlock **mechanism** (pieces OBSERVED, the
"break a Pool↔DVE semaphore cycle" reading INFERRED-HIGH from the `DVE||Pool`
gate + the rule texts); the `min_nonlocals_to_do_fma` numeric threshold (gate
OBSERVED, value a `.so` immediate — INFERRED); the CoreBarrier-vs-EventSemaphore
decision boundary (both mechanisms OBSERVED, the "pairwise for intra, barrier for
cross/fan-in> 2" reading INFERRED-HIGH from the ≤ 2-wait limit + the
`InsertCoreBarrier` docstring).

**LOW / CARRIED**: the 64-byte SEQ-slot **bit-field packing** of the wait/set
commands (CARRIED [assembly-pipeline.md](../neff/assembly-pipeline.md) — the
`OnWait`/`OnUpdate` field set + the `lower_sync` values are OBSERVED, the slot bit
layout is the assembly/NEFF page's); the device `ln` window decode of the emitted
WAIT/INC/SET/DEC (CARRIED the Part-10 `collectives/` pages,
`ln +0x1000/0x1400/0x1800/0x1C00`); the `InstructionLatencyModel` cycle immediates
that price the schedule + the DRAIN (CARRIED
[tiling-memory-scheduling.md §6.2](tiling-memory-scheduling.md)).

**Corrections issued** (all byte-confirmed against
`libwalrus.so` `nm`/`strings`):

| # | claim corrected (earlier report) | truth | §  |
| - | --- | --- | --- |
| 1 | the per-slot stamping method is `setupSyncUpdate<NEURON_ISA_TPB_*_STRUCT>` | **no `setupSyncUpdate` symbol/string in `libwalrus`**; the surface is `SetupSemaphoreUpdate::visitInstruction @0x113e8b0` / `setupDMASemaphoreUpdate @0x113dee0` / `enterInstLoop @0x1146290`; `AllocSemaphores::setupSync` is a *different* method | §4 |
| 2 | the two `InsertCoreBarrier` docstrings are `libwalrus` strings | they are **not in `libwalrus`**; they live in the Cython `InsertCoreBarrier.cpython-311.so`; the C++ `CoreBarrier`/`_all_eng_barrier`/`Done renumbering core barriers.` *are* in `libwalrus` | §3.6 |
| 3 | `getLatencyWriteDrain<InstClass>(EngineType)` is a single templated symbol | it is a **per-`Hwm` (per-gen) family** — base `bir::Hwm::getLatencyWriteDrain` undefined here, bodies `TrainiumHwm @0x18518a0` / `Gen3Hwm @0x1857c80` / `CoreV4Hwm @0x185e500` | §3.4 |

**No model-level disagreements.** Every other claim reproduces the sibling pages
exactly: the five-engine model (`PE=0/ACT=1/POOL=2/DVE=3/SP=4`) matches the runtime
`al_hal_tpb_eng_type_t` ordinals in
[assembly-pipeline.md](../neff/assembly-pipeline.md); the
`AntiDependencyAnalyzer-SB`/`-PSUM` instances + the `EdgeKind` enum (stored
**concatenated without spaces**: `ANTI_DEPENDENCEFLOW_DEPENDENCEINVALIDORDEREDOUTPUT_DEPENDENCE`)
+ the `AllocSemaphores`/`AllocateSemaphores` split + the EventSemaphore ≤ 2-wait
limit + the trigger-engine model all reproduce
[tiling-memory-scheduling.md §6](tiling-memory-scheduling.md) verbatim; the device
EVT_SEM windows (read `0x1000` / set `0x1400` / inc `0x1800` / dec `0x1C00`) match
[collective-enums.md](../collectives/ops/collective-enums.md) and the Part-10
`architecture-synthesis` / `core-barrier` / `top-sp-lowering` pages exactly. Every
cited `libwalrus` address (`AllocateSemaphores::assignDMASemaphore @0x1138f10`,
`AllocSemaphores::allocateSemaphore @0x1121430`, the `AntiDependencyAnalyzer`
vtable `@0x3d8c008`, `sync_dve_pool @0x156f560`, `InsertPTCOMFlat::insertPTCOMs
@0x16acc10`, `CoreV2GenImpl::visitInstCustomOp @0x1261370`, …) resolves exactly in
both `nm -DC libwalrus.so` and the IDA `_native_exports.json`.
`[HIGH/OBSERVED — cross-checked against binary + IDA JSON.]`

---

*The scheduler that produces the edges this page wires:
[Tiling / Memory-Planning / Scheduling Backend](tiling-memory-scheduling.md). The
NEFF the emitted sync fields land in:
[Per-Engine Instruction-Block Assembly Pipeline](../neff/assembly-pipeline.md). The
collective sync ops these passes lower into:
[collective-loadtime-rewrite.md](collective-loadtime-rewrite.md). The pass roster +
`emit_*`→opcode: [The GPSIMD-Relevant Compiler Map](compiler-map.md). The device
decode of the emitted `EVENT_SEMAPHORE` / `DRAIN` / `CoreBarrier`: the Part-13
control pages at `neuronx-gpsimd/wiki/src/control/...`.*
