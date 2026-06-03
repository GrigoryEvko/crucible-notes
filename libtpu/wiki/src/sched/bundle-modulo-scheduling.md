# Bundle Modulo Scheduling

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. All `.text` addresses are virtual; for this binary `.text` VMA == file offset `0xe63c000`.*

## Abstract

`TPUMachinePipeliner` is the LLVM-side **software pipeliner** the TPU backend runs on inner loops after instruction selection and before the [LLO/MachineInstr bundle packer](llo-bundle-packing.md). It is a third, orthogonal scheduling machine, distinct from both the XLA-side list scheduler and the per-cycle bundle packer: where the packer answers *"which VLIW slot does each op take in one cycle?"*, the modulo scheduler answers *"how many cycles does the steady-state loop kernel span, and which iteration's copy of each op lives in each cycle?"*. The span is the **initiation interval** (II) — the number of cycles between successive loop iterations entering the pipeline — and the per-op iteration offset is the **stage**. Once an II and a stage assignment are found, an *expander* rewrites the loop into prologue / steady-state kernel / epilogue (or, on hardware-counted generations, leaves the kernel for the silicon loop counter to drive).

The reader who knows LLVM should hold one analogy and two divergences. The analogy: this is classic Lam-style **iterative modulo scheduling** — pick a minimum II from resource and recurrence lower bounds, try to place every node into a cyclic schedule of that II, and if placement fails grow the II and retry. The first divergence: the per-II legality test is a **modulo reservation table** (`ModuloHazardRecognizer`) that maps each scheduled cycle `c` to the column `c mod II` and rejects two ops that would book the same functional unit in the same column — the divergence from upstream `MachinePipeliner` is that the TPU recognizer reuses the same `MCWriteProcResEntry` proc-resource table the bundle packer and the [bundle-aware cost model](../cost/bundle-aware-cost.md) read, at per-cycle rather than per-bundle granularity. The second divergence: there are **two** TPU modulo DAGs (a greedy *swing* scheduler and a force-based *slack* fallback) and **six** expanders, and the choice of expander is gated on whether the loop is a hardware-counted BarnaCore loop or a software back-edge (see the [hardware loop-counter](../isa/slot-loop.md)).

This page is the Part VIII anchor for the II search and the modulo reservation check: the `min-II = max(5 calculators)` composition in `initialize`, the `for (II = lower; II < upper; ++II)` increment loop in `findSchedule`, the `slot = cycle mod II` reservation test in `ModuloHazardRecognizer`, the `checkPostModuloSchedule` structural verifier and its return-code-driven `++II` (and the single-stage `rc=2` case, which abandons pipelining for that loop), and how the accepted II flows into `emitScheduleForLoop`'s expander dispatch. The per-cycle bundle that each scheduled cycle becomes is owned by the [bundle packer](llo-bundle-packing.md); the proc-resource cycle counts the calculators consume come from the [bundle-aware cost model](../cost/bundle-aware-cost.md).

For reimplementation, the contract is:

- **`min-II = max(ResourceMII, FillResourceMII, SuperPassMII, LargestLatencyMII, per-gen-floor, RecurrenceMII)`** — computed once per loop in `initialize` (`0x13c36160`). The resource bound is `ceil(busiest-FU cycles / units)`; the recurrence bound is `ceil(cycle-latency / iteration-distance)`. These accumulate into `[dag+0xebc]` (the lower bound). The upper bound `[dag+0xeb8]` is the bundle-packing-cost estimate from `fastBundlePackingCost`, overridable to `IIUpperBound + 1`.
- **The II search is `for (II = lowerII; II < upperII; ++II)`** (`findSchedule`, `0x13c367c0`). For each II it resets the hazard recognizer (`setII(II)`), schedules every SU in priority order, runs the structural verifier, and accepts the first II that passes resource, stage-count, FIFO, and register-pressure checks. A node that fails reordering can retry the same II up to `swing-modulo-retry-same-node` (=64) times before the II grows.
- **The per-II legality test is the modulo reservation table** — `ModuloHazardRecognizer::canEmitInstructionResourceInternal` (`0x13c14d60`) computes `slot = cycle mod II` (`idiv` by `II = [hr+0x40]`), walks the op's proc-resource entries, and rejects (`return 0`) if the FU bit is already set in the per-slot reservation bitmap `DenseMap<int,uint>` keyed by `slot`.
- **`checkPostModuloSchedule` (`0x13c149c0`) returns a code that drives the loop** — `2` = single-stage (no cross-iteration overlap, so nothing to pipeline; `findSchedule` returns the no-schedule sentinel and the loop is left un-pipelined), `3` = stage count exceeds `pipeliner-max-stages` → `++II`, `4` = split-live-range retry, `1` = FIFO check passed → proceed to the register-pressure gate; a clean pass accepts the II.
- **The accepted II flows to `emitScheduleForLoop` (`0x13ba0ac0`)**, which builds a `ModuloSchedule` and dispatches one of six expanders: `expandScheduleForBarnaCore` for hardware-counted loops, a rotating-predicate expander on V5+ (gated on `EnableRotatingPredicate`), or a peeling / predicated-epilogue expander otherwise.

| | |
|---|---|
| **Pass driver** | `TPUMachinePipeliner::runOnMachineFunction` @ `0x13ba15e0` |
| **Per-loop emit** | `TPUMachinePipeliner::emitScheduleForLoop` @ `0x13ba0ac0` |
| **Swing DAG (primary)** | `TPUScheduleDAGSwingModulo` — ctor `0x13c35ce0`, `initialize` `0x13c36160`, `findSchedule` `0x13c367c0` |
| **Slack DAG (fallback)** | `TPUScheduleDAGSlackModulo` — ctor `0x13c23840`, `findSchedule` `0x13c27560` |
| **Min-II driver** | `TPUScheduleDAGModulo::startModuloSchedule` @ `0x13c041c0` (upper-II via `fastBundlePackingCost` @ `0x13b67ea0`) |
| **Resource MII** | `calculateResourceMII` @ `0x13c0bee0` — `ceil(FU-cycles / units)`, `vpmaxud` over FUs |
| **Recurrence MII** | `calculateRecurrenceMII` @ `0x13c0ccc0` — max of `Recurrence[+0x28]`, stride `0x58` |
| **Reservation table** | `ModuloHazardRecognizer::canEmitInstructionResourceInternal` @ `0x13c14d60` (`slot = cycle mod II`) |
| **`setII`** | `ModuloHazardRecognizer::setII` @ `0x13c14d00` → `[hr+0x40] = II` |
| **Structural verifier** | `checkPostModuloSchedule` @ `0x13c149c0`; FIFO `checkFifoOverflow` @ `0x13c0da00`; stage-0 `checkForStageZeroInstructions` @ `0x13c14900` |
| **DAG field offsets** | upper-II `[dag+0xeb8]`, lower/min-II `[dag+0xebc]`, current-II `[dag+0xec0]`, Res/Fill-MII scratch `[dag+0xec4]`, Rec-MII `[dag+0xec8]`, stageCount `[dag+0x410]` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Where the Pipeliner Sits — Three Schedulers, One Resource Model

The TPU compile pipeline contains three independent scheduling machines on the LLVM-MIR side, and the modulo scheduler is the one that runs *first* on the loops it accepts:

```text
instruction selection → MachineInstr
  → TPUMachinePipeliner            (THIS PAGE: software-pipeline inner loops)
       per accepted loop: choose II, assign stages, expand prologue/kernel/epilogue
  → BundlePacker MachineFunctionPass   (per-cycle VLIW packing — see llo-bundle-packing.md)
  → TPUMCCodeEmitter                   (raw bundle bytes)
```

The pipeliner runs **only** on loops accepted by `analyzeLoopForPipelining` (`0x13c03f00`) → `TPUInstrInfo::analyzeLoopForTPUPipelining` (`0x13b804c0`), which classifies each loop as either hardware-counted (BarnaCore / AddressHandler — the silicon loop counter drives the back-edge) or software-counted (TensorCore / SparseCore — an explicit IV/compare/branch back-edge). That classification is documented on the [hardware loop-counter](../isa/slot-loop.md) page; it picks which `PipelinerLoopInfo` subclass the scheduler carries, and downstream it picks which expander runs. Loops the pipeliner rejects fall through to the plain list scheduler and are packed cycle-by-cycle with no software pipelining.

The crucial reuse fact: the modulo scheduler **does not invent a new resource model**. Its resource lower bound (`calculateResourceMII`) and its per-cycle reservation table (`ModuloHazardRecognizer`) both read the LLVM `MCSchedClassDesc` proc-resource list (`MCWriteProcResEntry` records: `{ProcResourceIdx, Cycles}`) — the same `TPUStages` table that the bundle packer's `ResourceSolver` and the [bundle-aware cost model](../cost/bundle-aware-cost.md) consume. The difference is granularity: the cost model reduces a *bundle* to a cycle count, the packer counts *slots in one cycle*, and the modulo scheduler prices *occupancy across a cyclic kernel of II columns*.

The driver `runOnMachineFunction` (`0x13ba15e0`) builds the per-loop proc-resource depth model (`addProcResourceDepths`, `0x13c14ae0`), then for each loop in preorder runs the **primary** swing scheduler (`TPUScheduleDAGSwingModulo`); if the achieved II is too loose relative to the `min-ii-slack` gate, it undoes the swing result and re-runs the **fallback** slack scheduler (`TPUScheduleDAGSlackModulo`), a force-based scheduler that can find tighter placements at the cost of eject/retry work. A `TPUMachinePipelinerSuperPass` (`0x13ba6280`) optionally clones the function, discovers a common II across all copies of a loop, and feeds it back via `getSuperPassMII` so every copy shares one II.

---

## Step 1 — The Minimum II (`initialize` and `startModuloSchedule`)

The lower bound on II is the maximum of several independent lower bounds, each of which must hold for *any* legal cyclic schedule. `TPUScheduleDAGSwingModulo::initialize` (`0x13c36160`) calls `startModuloSchedule` to set the upper bound, enumerates recurrences, then composes the five lower-bound calculators. The composition, decompiled from `initialize` (DWORD field indices are relative to the `TPUScheduleDAGSwingModulo` object; `this + N` is byte offset `4*N`):

```c
// TPUScheduleDAGSwingModulo::initialize(MBB)  @ 0x13c36160   (field offsets in DWORDs)
startModuloSchedule(dag, MBB, MLI, AA, FifoFillAnalysis);   // sets upper-II at this[942] (0xeb8)
findBackedges(dag, MBB, AA);                                // loop-carried SDeps
findRecurrences(dag, MBB, /*flag=*/0);                      // enumerate dependency cycles

this[946] = calculateRecurrenceMII(dag, MBB, recurrences);  // [dag+0xec8] RecMII
this[945] = calculateResourceMII(dag, MBB, /*flag=*/0);     // [dag+0xec4] scratch = ResMII
this[945] = calculateFillResourceMII(dag, MBB, ResMII);     // [dag+0xec4] scratch = FillResMII

this[943] = max(FillResMII, RecMII);                        // [dag+0xebc] lower-bound seed
this[943] = max(this[943], getSuperPassMII(dag, MBB));      // [dag+0xebc] max= SuperPassMII
if (!subtarget_is_barnacore /* [subtarget+290]==0 */) {     // largest-latency + gen-floor are NOT-BarnaCore-only
    this[943] = max(this[943], calculateLargestLatencyMII(...));  // max= LargestLatencyMII
    if (unk_224E34D0 == 1)                                  // per-gen virtual floor: (*subtarget+0x308)()+2
        this[943] = max(this[943], subtargetMIIFloor() + 2);
}
if (unk_224E3518)                                           // second optional floor (dword_224E3588)
    this[943] = max(this[943], dword_224E3588);
// this[943] (0xebc) is the LOWER bound; this[942] (0xeb8) is the UPPER bound.
```

```c
// TPUScheduleDAGModulo::startModuloSchedule(...)  @ 0x13c041c0  (tail)
dag[10 /*+0x28*/] = fastBundlePackingCost(MBB, ...);   // bundle-packing-cost upper estimate
if (word_224E3180 /* IIUpperBound cl::opt present */)
    dag[10] = dword_224E31F0 /*IIUpperBound*/ + 1;     // override the upper bound
// this inner-DAG [+0x28] becomes the SwingModulo this[942] (0xeb8) upper bound.
```

> **NOTE — offset labels corrected against the decompile.** The accumulation byte-anchors as: `[dag+0xebc]` (`this[943]`) is the **lower / minimum** II (it accumulates the resource, fill-resource, super-pass, largest-latency, and per-gen-floor maxima), and `[dag+0xeb8]` (`this[942]`) is the **upper** bound (the `fastBundlePackingCost` estimate, or `IIUpperBound+1`). `this[944]` (`0xec0`) is the running current II; `this[945]` (`0xec4`) is the resource/fill scratch; `this[946]` (`0xec8`) is the recurrence MII.

### Resource MII — `calculateResourceMII` (`0x13c0bee0`)

The resource bound is the floor imposed by the busiest functional unit: if all the ops in the loop body together demand `C` cycles on a functional unit that has `U` parallel units, then no schedule can issue them more often than every `ceil(C / U)` cycles, so `II >= ceil(C / U)`. The decompile walks each SU (skipping `G_PHI` (`SchedClass` field == 74) and bundle-marker ops detected via `MachineInstr::hasPropertyInBundle(0x400)`), reads its `MCWriteProcResEntry` list from the sched-class table, and for each proc-resource increments a per-FU usage counter. The unit count comes from `__popcnt` of the proc-resource unit mask, floored at 1:

```c
v33 = __popcnt(unit_mask);          // number of parallel units of this FU
if ((unsigned)v33 < 2) v33 = 1;     // getProcResourceUnits: 1 if zero/one unit
ResMII_for_FU = ceil(usage[FU] / v33);
```

The final reduction is the SIMD `vpmaxud` cascade over all FU buckets (`[r14+0x270]..[r14+0x354]`) plus a few scalar maxima — i.e. `ResMII = max over FUs of ceil(usage[FU] / units[FU])`. The `FillResourceMII` path layers FIFO-fill occupancy on top via `MCBundleInfoFactory::create`, accounting for cross-iteration FIFO residency (the same hardware FIFOs the verifier's `checkFifoOverflow` guards). Both write the `[dag+0xec4]` scratch, and the result folds into the `[dag+0xebc]` lower bound.

### Recurrence MII — `calculateRecurrenceMII` (`0x13c0ccc0`)

A loop-carried dependency cycle of total latency `L` that spans `D` iterations cannot be issued faster than `ceil(L / D)` — the cyclic dependency must close within `D` kernels. `findRecurrences` (`0x13c07ae0`) enumerates the dependency cycles using the back-edges from `findBackedges`, and for each one calls `computeMinDist` (`0x13c06ea0`), a Bellman-Ford-style longest-path relaxation that produces the cycle's latency-vs-distance ratio. That ratio is stored per recurrence; `calculateRecurrenceMII` then takes the max:

```c
// calculateRecurrenceMII @ 0x13c0ccc0  (Recurrence struct stride = 0x58 = 88 bytes)
int RecMII = 0;
for (Recurrence &r : recurrences)          // SIMD vpmaxsd over r[+0x28]
    RecMII = max(RecMII, *(int*)((char*)&r + 0x28));   // per-recurrence MII at +0x28
return RecMII;                              // 0 if no recurrences
```

The byte evidence: the SIMD path strides `88` bytes per element and reads the DWORD at offset `+40` (`0x28`), reducing with `vpmaxsd`; the tail loop does the same scalar max. The recurrence struct is `0x58` bytes; the per-recurrence MII lives at `[Recurrence+0x28]`.

So `min-II = max(ResMII, FillResMII, SuperPassMII, RecMII, [if not BarnaCore: LargestLatencyMII, per-gen-floor])`. The accumulation is byte-exact in `initialize`: `[dag+0xebc]` is seeded with `max(FillResMII, RecMII)`, then `getSuperPassMII` is folded in unconditionally, and only **on non-BarnaCore subtargets** (`[subtarget+290]==0`) are `calculateLargestLatencyMII` and the per-generation virtual floor (`unk_224E34D0` path, a subtarget virtual call `+2`) added — a second optional floor (`unk_224E3518` → `dword_224E3588`) folds in last. The largest-latency bound (`calculateLargestLatencyMII`, `0x13c0b840`) is `max over all SDep edges of getEdgeLatency` — the single longest dependency latency must fit inside one kernel window unless it is hidden across stages; the super-pass bound (`getSuperPassMII`, `0x13c0ba40`) is the cross-function shared II if the super-pass discovered one, else `-1`.

---

## Step 2 — The II-Increment Loop (`findSchedule`, `0x13c367c0`)

`findSchedule(MBB, retries, lowerArg, upperArg)` clamps the caller's bounds to the computed min/max-II, then walks `II` upward, trying to build a valid cyclic schedule at each value. The decompile, condensed (the SwingModulo object is `this`; `this[N]` is DWORD index `N`):

```c
// TPUScheduleDAGSwingModulo::findSchedule(MBB, retries=a3, lowerArg=a4, upperArg=a5)  @ 0x13c367c0
initializeSUnitsComposed(dag.fifo, MBB, ...);
if (decomposed_fifo) decomposeModuloFifo(dag.fifo, MBB);

if (this[942] /*upper-II*/ < upperArg) upperArg = this[942];   // clamp upper down
if (this[943] /*lower-II*/ > lowerArg) lowerArg = this[943];   // clamp lower up
if (lowerArg >= upperArg) return /*fail*/ 0;                   // empty search window

calculateASAPAndALAP(this);                                    // ASAP/ALAP per SU window
ModuloHazardRecognizer *hr = new ModuloHazardRecognizer(maxSchedLatency,
                                                         TwistStageZeroOrdering);
findPriorityOrder(this);                                       // height/mobility order

unsigned II = max(lowerArg, 1);          // II >= 1   ( (a4==0)+a4 )
this[944] = II;                          // [dag+0xec0] current II
int sameNodeRetry = 0;
while (II < upperArg) {
    hr->reset();
    hr->setII(this[944]);                // [hr+0x40] = II
    hr->setPipelinerInfo(this[475]);     // loop-info for stage gating
    schedule_map.clear();
    this[912] = 0;
    resetMinMaxCycle(this);

    bool placed_all = true;
    for (SUnit *su : this->priorityOrder) {           // this[922] count, this[460] array
        if (!scheduleSU(this, su)) {                  // per-II reservation-table placement
            // reordering failure for this node at this II:
            if (++retriesSameNode[su] > MaxRetriesSameNode /*=64, dword_224E4058*/)
                return 0;                             // give up entirely
            placed_all = false; break;                // else fall through to ++II
        }
    }
    if (placed_all) {
        if (decomposed_fifo) scheduleDecomposedFifo(dag.fifo, MBB, ...);
        createScheduleByCycle(this, ...);             // materialize cycle schedule
        if (checkForStageZeroInstructions(dag, MBB, ...) != 1) goto next_II;
        int rc = checkPostModuloSchedule(dag, MBB, ..., /*splitRetry=*/sameNodeRetry==4);
        if (rc == 3) { sameNodeRetry = 0; goto next_II; }     // stage-count exceeded → ++II
        if (rc == 4) {                                        // split-live-range retry
            if (sameNodeRetry != 4) { --this[944]; sameNodeRetry = 4; } else sameNodeRetry = 0;
        } else if (rc == 2) {
            return 0;                                          // single-stage: nothing to pipeline → no-schedule sentinel (loop left un-pipelined)
        } else {                                               // rc == 1: FIFO passed
            // BarnaCore live-register-pressure gate (prolog + epilog windows):
            if (subtarget_is_barnacore) {
                BarnaCoreLiveRegPressure p0(MF, kernel, prolog, this[1192], ...);
                if (p0.s <= 32/this[1192] && p0.v <= 32/this[1192]) {
                    BarnaCoreLiveRegPressure p1(MF, kernel, epilog, this[1192], ...);
                    if (p1.s <= 8/this[1192] && p1.v <= 8/this[1192])
                        return this[944] | 0x100000000;         // ACCEPTED: pack II in low 32
                }
            } else return this[944] | 0x100000000;              // ACCEPTED
        }
    }
next_II:
    this[944] = ++II;                     // grow II and retry
    if (II >= upperArg) return 0;         // exhausted window → fail
}
```

The salient points:

- **The search window is `[lowerII, upperII)`** and `II` starts at `max(lowerII, 1)` — the `(a4==0)+a4` idiom guarantees `II >= 1`. If the clamped window is empty (`lowerII >= upperII`) the search fails immediately.
- **Each II resets the reservation table** (`hr->reset(); hr->setII(II)`) before scheduling, so the modulo columns start empty for every candidate II.
- **A node that cannot be placed at this II first retries** (re-order priority) up to `MaxRetriesSameNode` (`swing-modulo-retry-same-node`, default 64) times; only after exhausting retries does the search abandon that II and increment.
- **The return value packs the accepted II in the low 32 bits with a "found" flag in bit 32** (`II | 0x100000000`); `0` means no II in the window worked.

### The slack fallback (`findSchedule` @ `0x13c27560`)

The slack scheduler is structurally identical but force-based: it clamps `upperII = min(upperII, lowerII + slack-modulo-limit-ii-range)` (default range 256), and instead of pure-greedy `scheduleSU` it uses `determineCriticalInstructions` + `forceScheduleSU` + `ejectInstruction` (place-and-backtrack). Its conflict detection makes the modulo arithmetic explicit — `identifySlotConflicts` (`0x13c25860`) and `identifyResourceConflicts` (`0x13c26180`) compute `cycle mod II` directly with `% *(_DWORD*)(dag + 0xd90)` (the slack scheduler stores its II at `[dag+0xd90]`):

```c
// TPUScheduleDAGSlackModulo::identifySlotConflicts (excerpt)  @ 0x13c25860
int II = *(int*)(dag + 0xd90);
v23 = (int)cycleA % II;                       // slot of candidate A
v24 = cycleB    % II;                          // slot of candidate B
conflict = (v23 != v24) ? ...                  // different columns may still alias under latency
         : ((cycleA + latency) % II != cycleB % II);
```

---

## Step 3 — The Modulo Reservation Table (`ModuloHazardRecognizer`)

The per-II legality of placing an op at cycle `c` is decided by the **modulo reservation table**: an op issued at cycle `c` occupies the *same* physical functional unit in *every* iteration at column `c mod II`. Two ops mapping to the same `(FU, c mod II)` cell are a structural hazard, and either the op must move to a cycle whose column is free or the II must grow. The recognizer holds II at `[hr+0x40]` (written by `setII`, `0x13c14d00` — `*((_DWORD*)this + 16) = II`) and the reservation table as a `DenseMap<int /*slot*/, uint /*FU-bitmask*/>`.

```c
// ModuloHazardRecognizer::canEmitInstructionResourceInternal(SU, cycle, table)  @ 0x13c14d60
// returns 1 if the op can be emitted at `cycle` without an FU-column conflict, else 0.
char canEmit(SU *su, int cycle, DenseMap<int,uint> &reservation) {
    if (!su->schedClassDesc) return 1;                    // no resources → always legal
    int II = *((int*)this + 16);                          // [hr+0x40]
    MCWriteProcResEntry *e   = procResBegin(su);          // 6-byte records {idx, cycles}
    MCWriteProcResEntry *end = procResEnd(su);
    for (; e != end; ++e) {
        int FU      = e->ProcResourceIdx;                 // *e (the FU index, a byte)
        int cycles  = *((u16*)e + 1);                     // record +2: occupancy-cycle count
        if (II < cycles) continue;                        // skip if II < that same field (occupancy > II)
        int slot = cycle;
        do {
            uint booked = reservation.lookupOrInsert(slot).value;  // FU bitmask at this column
            if (_bittest(&booked, FU)) return 0;          // FU already booked in this column
            if (cycles == 0) break;
            slot = (slot + 1) % II;                       // advance one modulo column
        } while (slot != (cycle + cycles) % II);          // span the op's occupancy window
    }
    // fixed-latency structural-hazard variant repeats with getMaxSchedLatency window:
    if (su->mi->getDesc().SchedClass & 0x3F) {
        int maxLat = TPUSubtarget::getMaxSchedLatency(MF, su->latency);   // @ 0x13c5b420
        for (each proc-res entry; same modulo walk anchored at cycle + maxLat) { ... }
    }
    return 1;
}
```

The byte-exact mechanics confirmed in the decompile: the divisor in the two `(slot + 1) % v13` / `% v22` reductions is `*(_DWORD*)(a1 + 64)` = `[hr+0x40]` = II; the column index `slot` is initialized to the cycle (`v30[0] = a3`) and stepped modulo II; the FU test is `_bittest(&booked, *v10)` against the per-slot bitmask fetched via `DenseMap::lookupOrInsertIntoBucket`; the occupancy window terminates at `(cycle + cycles) mod II`. The second half re-runs the same modulo walk anchored at `cycle + getMaxSchedLatency(...)` to catch fixed-latency structural hazards (the `canEmitInstructionHazardInternal` companion, `0x13c14f80`, does the equivalent for the hazard table). `emitInstructionResource` (`0x13c155a0`) is the commit that sets the FU bits once a cycle is accepted.

This is the textbook modulo reservation table, and it is the heart of the per-II test: `scheduleSU` only accepts a cycle for an SU if `canEmitInstructionResourceInternal` returns 1 for every modulo column the op touches.

The full per-II validity test combines five gates, applied in order, and the first failure either reschedules the node, retries the same II, or grows II:

| Gate | Function | On failure |
|---|---|---|
| Modulo reservation (FU column conflict) | `canEmitInstructionResourceInternal` @ `0x13c14d60` | node re-placed; if no cycle works, retry/`++II` |
| Stage-0 reachability (every op fits a stage) | `checkForStageZeroInstructions` @ `0x13c14900` | `++II` |
| Stage count / split / structural | `checkPostModuloSchedule` @ `0x13c149c0` | `rc=3` → `++II`; `rc=4` → split retry |
| FIFO depth not exceeded | `checkFifoOverflow` @ `0x13c0da00` (via verifier) | `++II` |
| Live-register pressure (prolog/epilog windows) | `BarnaCoreLiveRegPressure` (inline in `findSchedule`) | retry up to 64×, then `++II` |

---

## The Structural Verifier — `checkPostModuloSchedule` (`0x13c149c0`)

Once every SU is placed and the cycle schedule materialized, the verifier decides whether the schedule is structurally legal at this II, and its integer return code drives the search. The decompile is small and exact:

```c
// TPUScheduleDAGModulo::checkPostModuloSchedule(MBB, schedule, ..., maxStages a8, splitOK a9, splitRetry a10)  @ 0x13c149c0
int checkPostModuloSchedule(...) {
    int stageCount = *((int*)this + 260);          // [dag+0x410] = ceil(span / II)
    if (stageCount == 1) return 2;                 // single stage → no overlap, trivially done
    if (stageCount <= maxStages) {
        long capStages = *((long*)this + 131);     // [dag+0x418] configured stage cap (-1 = none)
        if (capStages != -1 && capStages < stageCount) return 3;     // exceeds cap → ++II
        if (!EnableExperimentalCopyRotate /*224E42C8*/ && splitOK && EnableSplitLiveRanges /*224E3868*/
            && manualSplitLiveRange(this, MBB, ...)) {
            undoManualSplitLiveRange(this, MBB);
            return 3 + splitFlag;                  // 4 → retry with split live ranges
        }
        if (checkFifoOverflow(this, MBB, schedule)) {   // @ 0x13c0da00 — hardware FIFO depths
            undoManualSplitLiveRange(this, MBB);
            return 3;                              // FIFO overflow → ++II
        }
        return 1;                                  // FIFO OK → proceed to reg-pressure gate
    } else {                                       // stageCount > maxStages
        if (!subtarget_is_barnacore && !EnableRotatingPredicate /*224E1278*/)
            report_fatal_error("Max stage count too low.");   // needs more stages than allowed
        return 3;                                  // ++II
    }
}
```

The return codes and their effect in `findSchedule`:

| Code | Meaning | `findSchedule` action |
|---|---|---|
| `1` | stage count within bounds, FIFO check passed | proceed to the register-pressure gate; pass → **accept II** |
| `2` | single stage — no cross-iteration overlap to exploit | `findSchedule` returns the **no-schedule sentinel** (`v18 = 0`, same value as window exhaustion) and the loop is left un-pipelined |
| `3` | stage count exceeds `pipeliner-max-stages` or the stage cap, or FIFO overflowed | reset same-node retry, **`++II`** |
| `4` | `manualSplitLiveRange` succeeded — retry this II with split live ranges | decrement II once, set split-retry flag, re-attempt |

`stageCount` is `[dag+0x410]` = `ceil(span / II)`, the number of pipeline stages = number of in-flight iterations in the steady-state kernel. The `"Max stage count too low."` fatal fires only when the schedule genuinely needs more stages than `pipeliner-max-stages` permits **and** rotating-predicate expansion (which can run all stages in one body without peeling) is disabled on a non-BarnaCore generation. `checkFifoOverflow` constructs a `FifoAnalysis` over the per-generation hardware FIFOs (DRF / V2SF / SFRF / HMF) and verifies the pipelined schedule never exceeds any FIFO depth across overlapping iterations.

After a `1` return, `findSchedule` runs the inline `BarnaCoreLiveRegPressure` gate: it computes the live scalar/vector register pressure of the prolog window and (if that passes) the epilog window against the per-class file limits (`32 / lanes` for the prolog, `8 / lanes` for the epilog, where `lanes = this[1192]`). Only when both pressures are within budget does the II get accepted (`return II | 0x100000000`).

---

## Step 4 — Emitting the Schedule (`emitScheduleForLoop`, `0x13ba0ac0`)

With an accepted II and stage assignment in hand, `emitScheduleForLoop` records the per-MBB II and stage count, builds an `llvm::ModuloSchedule` (the `{instruction order, per-instr cycle, per-instr stage}` triple), and dispatches one of six expanders. The dispatch, decompiled:

```c
// TPUMachinePipeliner::emitScheduleForLoop(MF, loop, dag, II, MBB)  @ 0x13ba0ac0
int stageCount = *((int*)dag + 260);           // [dag+0x410]
record_per_MBB_stageCount(MBB, stageCount);
record_per_MBB_II(MBB, II);                    // II stored for the super-pass map
ModuloSchedule sched = dag->createModuloSchedule(MF, loop, scheduleByCycle);   // vtable +0x20 → 0x13c045e0

byte *subtarget = MF->subtarget;
if (subtarget[290] == 1) {                      // BarnaCore: hardware-counted loop
    expandScheduleForBarnaCore(sched, this);    // @ 0x13c37bc0 — reorder kernel only, HW counter drives back-edge
} else if (EnableRotatingPredicate /*224E11C0*/ == 1
           && !flag_224E1278 && !flag_224E1108) {
    TPURotatingPredicateModuloExpander::expand(sched, II);   // @ 0x13bf1f00 — V5+ rotating predicates
} else {
    // peeling / predicated-epilogue / SSI / stitching expander (selected by PipelinerStrategy)
    ...
}
```

The expander choice is the place the [hardware loop-counter](../isa/slot-loop.md) classification pays off:

| Expander | Address | When | What it does with the II |
|---|---|---|---|
| `expandScheduleForBarnaCore` | `0x13c37bc0` | hardware-counted BarnaCore loop (`subtarget[290]==1`) | reorders the kernel only; the silicon `bcLOOP` counter handles the prolog/epilog boundary, so no software peeling |
| `TPURotatingPredicateModuloExpander::expand` | `0x13bf1f00` | V5+ with `EnableRotatingPredicate` | gates each stage's ops with a rotating predicate register so all stages run in one body without unrolling; stage `k` of iteration `i` is predicated by `ring[i-k]` |
| `TPURotatingPredicateEmuModuloExpander::expand` | `0x13bf0c20` | gens lacking the predicate ring | emulates the rotating ring in software |
| `TPUPredicatedEpilogModuloExpander::expand` | `0x13bd2860` | otherwise | peels prolog + predicated epilog |
| `TPUSSIModuloExpander::expand` | `0x13bf5dc0` | single-stage-interval loops | LICM-hoisted prologs, speculative-code predication |
| `TPUPeelingModuloExpander` (over generic `PeelingModuloScheduleExpander` @ `0x190592a0`) | `0x13bc01c0` (markPrologEpilogs) | fallback | peels `stageCount-1` prolog and epilog copies, leaving a steady-state kernel of II cycles |

After expansion, `emitScheduleForLoop` runs `optimizeScheduleLateSwap` (`0x13c0d1c0`), `cleanupSideEffectMoves` (`0x13c0cee0`), and `addBundleLimiters` (`0x13c0d540`), then hands the rewritten loop to the bundle packer.

### How the chosen II reaches the bundle packer

The modulo scheduler produces a **cycle schedule**: each cycle of the steady-state kernel now holds the set of MIs (from possibly different iterations, per their stage offsets) that should issue together. The [bundle packer](llo-bundle-packing.md) then packs the MIs scheduled into each cycle into a legal VLIW bundle, using the same proc-resource model. The two passes share the resource table but operate at different granularity: the modulo scheduler guarantees that no cycle over-subscribes a functional unit *across the cyclic kernel* (the modulo reservation table), and the packer guarantees that the ops in *one cycle* fit the per-generation slot limits. A worked example: a Viperfish loop with two matmuls, two vector loads, a loop-carried accumulate, an exp, and a store has `ResMII = max(ceil(2/2 MXU), ceil(1/4 VALU), ceil(2/3 loads), ceil(1/2 XLU)) = 1` and a recurrence MII of `2` from the accumulate's self-cycle, so `min-II = 2`; the reservation table at II=2 has two columns that the seven ops distribute across without an FU collision, the verifier accepts, and the resulting kernel runs one iteration every two cycles with the matmul's long latency hidden across the pipeline stages.

---

## Configuration Knobs

All are LLVM `cl::opt` statics; the value byte/word lives at `static_addr + 0x78`.

| `cl::opt` | Static addr | Default | Effect |
|---|---|---|---|
| `min-ii` (IILowerBound) | — | — | force the lower II bound |
| (IIUpperBound) | `0x224e3178` | — | force the upper II bound (`II = IIUpperBound + 1` cap when present) |
| `min-ii-slack` (MinIISlack) | `0x224e1370` | — | gate for the swing→slack fallback retry |
| `slack-modulo-limit` | `0x224e3d00` | — | slack-scheduler iteration cap |
| `slack-modulo-limit-ii-range` (LimitRangeII) | `0x224e3c48` | 256 | slack upper-II clamp = `lowerII + range` |
| `swing-modulo-limit` | `0x224e4098` | — | swing-scheduler iteration cap |
| `swing-modulo-retry-same-node` (MaxRetriesSameNode) | `0x224e3fe0` | 64 | re-order retries for one node before `++II` |
| `pipeliner-max-stages` (MaxPipeliningStages) | `0x224e1428` | — | max pipeline stages before the fatal / `++II` |
| `tpu-use-swing-modulo-sched` (UseSwingModuloPipeliner) | `0x224e4198` | — | swing vs generic `MachinePipeliner` |
| `tpu-pipeliner-strategy` (PipelinerStrategy) | `0x224e0fb0` | — | which expander |
| `tpu-enable-pipeliner-super-pass` | `0x224e4530` | — | cross-function shared-II discovery |
| `EnableRotatingPredicate` | `0x224e1200` (flag word `0x224e1278`) | — | rotating-predicate expander vs peeling |
| `TwistStageZeroOrdering` | `0x224e3f28` | — | stage-0 op ordering in the hazard recognizer |
| `EnableSplitLiveRanges` | `0x224e38f0` (flag `0x224e3868`) | — | enable `manualSplitLiveRange` (verifier `rc=4`) |

Diagnostic strings emitted by the search:

| String | Address | When |
|---|---|---|
| `"Invalid Minimal Initiation Interval: 0"` | `0x9fd0b4c` | min-II computed as 0 |
| `"Minimal Initiation Interval too large: "` | `0xa27bd56` | min-II exceeds the upper bound |
| `"Schedule found with Initiation Interval: "` | `0xa265fcf` | success, logging the accepted II |
| `"Max stage count too low. ... -pipeliner-max-stages."` | `0x9fe03bb` / `0xa025008` | stage count > `pipeliner-max-stages` and no rotating predicate |
| `"Use the tpu-specific swing-modulo scheduler instead of the MachinePipeliner."` | `0xa03a22b` | scheduler selection |

---

## Function Map

| Function | Address | Identity |
|---|---|---|
| `TPUMachinePipeliner::runOnMachineFunction` | `0x13ba15e0` | pass driver; swing-then-slack per loop |
| `TPUMachinePipeliner::emitScheduleForLoop` | `0x13ba0ac0` | builds `ModuloSchedule`, dispatches expander |
| `TPUMachinePipelinerSuperPass::runSuperPass` | `0x13ba6280` | cross-function shared-II discovery |
| `TPUScheduleDAGSwingModulo::initialize` | `0x13c36160` | min-II composition |
| `TPUScheduleDAGSwingModulo::findSchedule` | `0x13c367c0` | the II-increment loop |
| `TPUScheduleDAGSlackModulo::findSchedule` | `0x13c27560` | force-based fallback II loop |
| `TPUScheduleDAGModulo::startModuloSchedule` | `0x13c041c0` | upper-II via `fastBundlePackingCost` |
| `TPUScheduleDAGModulo::calculateResourceMII` | `0x13c0bee0` | `ceil(FU-cycles / units)` |
| `TPUScheduleDAGModulo::calculateRecurrenceMII` | `0x13c0ccc0` | max of `Recurrence[+0x28]` |
| `TPUScheduleDAGModulo::calculateFillResourceMII` | `0x13c0bb00` | FIFO-fill resource MII |
| `TPUScheduleDAGModulo::calculateLargestLatencyMII` | `0x13c0b840` | longest single edge latency |
| `TPUScheduleDAGModulo::getSuperPassMII` | `0x13c0ba40` | per-MBB super-pass II lookup |
| `TPUScheduleDAGModulo::findRecurrences` | `0x13c07ae0` | enumerate dependency cycles |
| `TPUScheduleDAGModulo::computeMinDist` | `0x13c06ea0` | Bellman-Ford latency/distance |
| `ModuloHazardRecognizer::setII` | `0x13c14d00` | `[hr+0x40] = II` |
| `ModuloHazardRecognizer::canEmitInstructionResourceInternal` | `0x13c14d60` | `slot = cycle mod II` reservation test |
| `ModuloHazardRecognizer::canEmitInstructionHazardInternal` | `0x13c14f80` | fixed-latency hazard variant |
| `ModuloHazardRecognizer::emitInstructionResource` | `0x13c155a0` | commit FU bits |
| `TPUScheduleDAGModulo::checkPostModuloSchedule` | `0x13c149c0` | structural verifier, return code |
| `TPUScheduleDAGModulo::checkForStageZeroInstructions` | `0x13c14900` | every op reaches a valid stage |
| `TPUScheduleDAGModulo::checkFifoOverflow` | `0x13c0da00` | hardware FIFO depth check |
| `TPUScheduleDAGSlackModulo::identifySlotConflicts` | `0x13c25860` | `cycle mod II` slot hazard (slack) |
| `expandScheduleForBarnaCore` | `0x13c37bc0` | hardware-counted loop expander |
| `TPURotatingPredicateModuloExpander::expand` | `0x13bf1f00` | V5+ rotating-predicate expander |
| `TPUInstrInfo::analyzeLoopForTPUPipelining` | `0x13b804c0` | HW-vs-SW loop classification |

---

## Cross-References

- [LLO → Bundle Packing](llo-bundle-packing.md) — the per-cycle VLIW packer this scheduler drives; each cycle of the modulo kernel becomes one packed bundle there, sharing the proc-resource model.
- [LatencyHidingScheduler Core](latency-hiding-scheduler-core.md) — the HLO-level list scheduler; the modulo scheduler is its loop-only LLVM-side counterpart and runs at a different IR level.
- [Bundle-Aware Cost](../cost/bundle-aware-cost.md) — the `MaxResourceCycles` bundle cost and the `MCWriteProcResEntry` proc-resource cycles that the MII calculators and the reservation table consume.
- [Hardware Loop-Counter](../isa/slot-loop.md) — the per-generation hardware-vs-software loop classification (`analyzeLoopForTPUPipelining`) that selects the modulo expander and drives the back-edge.
- [Bundle Model](../isa/bundle-model-overview.md) — what a VLIW bundle is, the per-generation slot taxonomy, and the slot limits the packer enforces on each cycle the scheduler produces.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VIII — Instruction Scheduling & Bundle Packing — [back to index](../index.md)
