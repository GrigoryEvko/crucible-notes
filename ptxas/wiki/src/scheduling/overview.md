# Instruction Scheduler Overview

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*
>
> *Size figures expressed in KB (e.g. "22 KB", "47 KB") refer to the decompiled C source produced by the analysis pipeline, not the native code footprint. The compiled binary sizes are roughly 4--6x smaller; for example `sub_8D0640` is 3953 bytes of x86-64 (839 instructions), `sub_688DD0` is 3643 bytes (785 instructions), and `sub_8C9320` is 10,154 bytes (2,116 instructions). The KB figures are still useful as a relative-importance indicator across functions within this subsystem.*

The ptxas instruction scheduler is a priority list scheduler with a 3-phase architecture. A single top-level orchestrator (`sub_8D0640`, ScheduleInstructions) drives three passes through one unified scheduling engine (`sub_688DD0`), each configured by a mode parameter that selects a different optimization objective: register pressure reduction, ILP/latency hiding, or dynamic batch optimization for tensor warpgroup operations. The scheduler runs twice in the ptxas pipeline -- once before register allocation on virtual registers (pre-scheduling) and once after physical register assignment (post-scheduling).

The scheduler consumes a dependency DAG built over the instruction list and produces a final instruction ordering together with SASS control words encoding stall counts, yield hints, barrier assignments, and scoreboard dependencies. The entire subsystem occupies two principal address ranges: the main scheduler core at `0x893000--0x8FE000` (428 KB of native code, ~115 functions including the orchestrator, engine, priority evaluator, dependency builder, DynBatch context, and per-SM HW profile builders) and a supporting infrastructure block at `0x67F000--0x6A0000` (132 KB; ready-list construction, instruction relink, region-init, and the unified-engine entry-point thunks). The scoreboard/control-word pipeline at `0xA22000--0xA3B000` (post-scheduling phases 114--116) accounts for an additional ~100 KB.

| | |
|---|---|
| **Orchestrator** | `sub_8D0640` (22 KB) -- ScheduleInstructions |
| **Unified engine** | `sub_688DD0` (20 KB) -- mode-parameterized scheduling loop |
| **Priority function** | `sub_8C9320` (47 KB) -- multi-criteria heuristic |
| **Ready list builder** | `sub_6820B0` (1.5 KB) -- zero-predecessor scan |
| **Dependency graph** | `sub_8CF880` (28 KB) + `sub_8D9930` (19 KB) |
| **Register budget** | `sub_8CEE80` (8.7 KB) -- occupancy-aware computation |
| **HW latency profiles** | `sub_8E7300`--`sub_8E9DC0` -- per-SM tables |
| **Opcode table** | `sub_896D50` (90 KB) -- ROT13-encoded SASS mnemonics |
| **Scheduling arena** | `sub_8E3970` / `sub_8E3A80` -- bump allocator |
| **Key knobs** | 76 `Sched*` knobs; see [Configuration](#scheduling-knobs) |
| **Enable gate** | `"ScheduleInstructions"` named option at `(a1+8)+1664` |

## 3-Phase Pipeline

The orchestrator `sub_8D0640` executes the following sequence. All three scheduling phases invoke the same unified engine `sub_688DD0` -- the only difference is the mode byte passed as the second argument.

```
function ScheduleInstructions(sched):
    // 1. Build dependency graph
    BuildDependencyGraph(sched, func)         // sub_8CF880
    vtable[29](sched)                         // InitScheduleData
    PreScheduleSetup(sched, opt_level > 2)    // sub_8CBAD0

    // 2. Gate check
    if not KnobGetBool("ScheduleInstructions"):
        return

    // 3. Set mode flags from knobs 419 (LivenessCountRegComp), 420 (LivenessUseHiLo)
    sched.flags |= (knob_419 << 3) | (knob_420 << 4)

    // 4. Optionally create register pressure tracker
    if sched.flags & 0x10:
        sched.scoreboard = alloc(952)         // sub_69A1A0
        sched.tracker    = alloc(208)         // sub_6B8F70
        if sched.flags & 0x100:
            sched.warp_analysis = alloc(856)  // sub_6BB7C0

    // 5. Reset per-instruction SchedNode fields between passes
    //    (iterates func+104 metadata chain, NOT instruction list)
    for sched_node in func.sched_node_list:       // linked via func+104
        sched_node.depChainHead  = 0              // QWORD +56
        sched_node.extendedState = 0              // QWORD +104
        sched_node.schedulingCost  = 0            // DWORD +76
        sched_node.schedulingClass = -1           // DWORD +84, sentinel

    // 6. Phase 1 — ReduceReg
    if KnobGetBool("ScheduleInstructionsReduceReg"):
        ScheduleEngine(sched, mode=0x39, ...)   // sub_688DD0

    // 7. Phase 2 — Reverse scheduling (ILP / latency)
    ReverseSchedule(sched)                      // sub_8CD6E0
    ComputeRegisterBudget(sched)                // sub_8CEE80

    // 8. Phase 3 — DynBatch
    if KnobGetBool("ScheduleInstructionsDynBatch"):
        AllocDynBatchData(sched)                // sub_8BF890
        ScheduleEngine(sched, mode=0x41, ...)   // sub_688DD0

    // 9. Cleanup
    FreeBitvector(sched.bv)                     // sub_BDC050
    ArenaFreeAll(sched.arena)                   // sub_8E3A80
```

### Mode Byte Decoding and Weight Vector Assignment

The three mode bytes 0x39, 0x49, 0x41 serve a dual purpose: they encode a C++ virtual member function pointer (Itanium ABI) that selects the `SelectBestInstruction` dispatch target, and they identify which scheduling objective governs the priority computation. The orchestrator also writes an integer mode identifier to `sched+240` which the engine's inner loop reads to gate mode-specific behavior.

**Itanium ABI member function pointer decode.** The low bit of each byte is 1, marking them as virtual calls. The vtable slot index is `(byte - 1) / 8`:

| Mode byte | Vtable offset | Vtable slot | Target VA | SelectBest backend |
|---|---|---|---|---|
| 0x39 (57) | 56 | [7] | `0x8DA6A0` | ReduceReg (core method) |
| 0x41 (65) | 64 | [8] | `0x8E0F18` | DynBatch (pipeline\_A[0]) |
| 0x49 (73) | 72 | [9] | `0x8E0F90` | ILP/Latency (pipeline\_A[1]) |

The vtable at `off_21DBC80` (77 entries; see `scheduling_vtable.json`) has slots [0..7] as core methods shared by all modes, followed by three 23-method pipeline groups A/B/C that provide SM-family-specific priority scoring backends. The member function pointer resolves through the object's vtable pointer, so derived classes (constructed per-SM from `sm_scheduling_seeds.json` variant codes) can override individual pipeline methods without affecting the core dispatch.

```
// sub_8D0640 -- mode configuration before each ScheduleEngine call
//
// Phase 1: ReduceReg
function ConfigureReduceReg(sched):
    sched.mode        = 1                          // DWORD at sched+240
    sched.forceReduce = true                       // BYTE  at sched+484
    sched.regPressure = true                       // BYTE  at sched+176
    sched.regTargetLo = KnobGet(776, default=250)  // DWORD at sched+296
    sched.regTargetHi = KnobGet(778, default=300)  // DWORD at sched+300
    // 0x12C000000FA packs {250, 300} as two DWORDs at sched+296
    sched.regTargetLo = AdjustForArchRegs(sched.regTargetLo)  // sub_6818D0
    sched.regTargetHi = AdjustForArchRegs(sched.regTargetHi)  // sub_6818D0
    ScheduleEngine(sched, mfp=0x39, ...)           // sub_688DD0

// Phase 2: ILP / Latency
function ConfigureILP(sched):
    sched.mode        = 0                          // DWORD at sched+240
    sched.forceReduce = false                      // sched+484
    sched.regPressure = false                      // sched+176
    sched.regBudget   = min(archLimit,             // DWORD at sched+432
                            int(0.95 * maxRegs))
    sched.maxDepth    = maxInstrDepth + 4          // DWORD at sched+432
    ScheduleEngine(sched, mfp=0x49, ...)           // sub_688DD0

// Phase 3: DynBatch
function ConfigureDynBatch(sched):
    sched.mode        = 2                          // DWORD at sched+240
    sched.forceReduce = false                      // sched+484
    sched.regPressure = false                      // sched+176
    sched.batchDepth  = KnobGet(805, default=8)    // DWORD at sched+404, [0..16]
    sched.loadsPerTex = KnobGet(741, default=3)    // DWORD at sched+408
    sched.rliveSlack  = KnobGet(761)               // DWORD at sched+412
    if hasDynBatchOps:
        AllocDynBatchContext(sched)                // sub_8BF890
    ScheduleEngine(sched, mfp=0x41, ...)           // sub_688DD0

// Inside ScheduleEngine (sub_688DD0), per-BB inner loop:
function SelectBestInstruction(sched, readyList, ...):
    // Resolve the member function pointer to a vtable call:
    //   if (mfp & 1):  target = *((*this) + (mfp - 1))
    //   else:           target = mfp  (non-virtual, unused here)
    //
    // The resolved target dispatches to one of:
    //   vtable[7]  sub_8DA6A0 -- ReduceReg:  minimize live register delta
    //   vtable[8]  sub_8E0F18 -- DynBatch:   batch-aware tensor grouping
    //   vtable[9]  sub_8E0F90 -- ILP:        maximize critical-path coverage
    //
    // Additionally, sched.mode (sched+240) gates per-iteration behavior:
    //   mode == 1: check "ScheduleInstructionsReduceReg" per-BB override
    //   mode == 2: check "ScheduleInstructionsDynBatch"  per-BB override
    //   mode == 0: no per-BB override check (always active)
    target = ResolveVirtualMFP(sched, mfp)
    return target(sched, readyList, ...)
```

### Phase 1 -- ReduceReg (mode 1, callback 0x39)

Goal: minimize register pressure so the register allocator has headroom. This phase reorders instructions to reduce the maximum number of simultaneously-live virtual registers.

- Enabled by the named option `"ScheduleInstructionsReduceReg"` (default: on at `-O3`).
- Register targets set from knobs 776 (`SchedReduceIncLimit`) and 778 (`SchedReduceIncLimitHigh`); defaults are exactly 250 and 300 (packed as `0x12C000000FA` in a single QWORD store to `sched+296`).
- The mode byte 0x39 resolves through `vtable[7]` to `sub_8DA6A0`, a core vtable method shared across all SM variants, implementing the register-pressure-minimizing priority score.
- The engine's inner dispatch reads `*(DWORD*)(sched+240) == 1` to enter the ReduceReg path, enabling per-BB knob overrides.

### Phase 2 -- ILP / Latency Hiding (mode 0, callback 0x49)

Goal: maximize instruction-level parallelism and hide memory latencies by interleaving independent operations.

- Always runs (no separate enable gate).
- Uses reverse post-order BB iteration via `sub_8CD6E0`: iterates basic blocks from last to first after resetting liveness with `sub_781F80(func, 0)`.
- Computes a register budget capped at `min(archLimit, 0.95 * maxRegs)` via `sub_8CEE80`.
- The mode byte 0x49 resolves through `vtable[9]` to `sub_8E0F90` (pipeline\_A[1]), implementing the latency-oriented priority score. This method is overridable by SM-specific derived vtables.
- After this phase, `sub_8CF5D0` evaluates dual-issue eligibility and produces a dual-issue benefit score stored at `scheduler+328`.

### Phase 3 -- DynBatch (mode 2, callback 0x41)

Goal: batch-aware scheduling for GMMA/WGMMA warpgroup tensor operations. Groups tensor instructions into batches that can execute as warpgroup-cooperative operations with minimal pipeline stalls.

- Enabled by the named option `"ScheduleInstructionsDynBatch"` and only activates when the function has varying instruction counts across BBs.
- Controlled by knob 742 (`SchedCrossBlock`, cross-block scheduling mode).
- Reads stall/batch depth limits from knobs 805 (`SchedTexBatchTargetSelectRegisterTarget`), 741 (`SchedCountLoadsPerTex`), 761 (`SchedMaxRLiveOKslack`), 762 (`SchedMaxRLiveOKslackColdBlocks`).
- Allocates a 184-byte DynBatch context (`sub_8BF890`) with an `8 * numBBs` sub-array for per-BB batch tracking.
- Context initialization (`sub_8C1BA0`): sets batch window to 0xFFFFFFFF (sentinel), copies register liveness from `func+832`.
- The mode byte 0x41 resolves through `vtable[8]` to `sub_8E0F18` (pipeline\_A[0]), implementing the batch-aware priority score. This method is overridable per SM variant.

### DynBatch Context Object (184 bytes)

`sub_8BF890` allocates a 184-byte DynBatch context from the scheduling arena at `sched+840` and stores the pointer at `sched+272`. The object is a flat structure containing a function context reference, a 20-slot working array, and a pointer to a variable-length per-BB sub-array.

| Offset | Size | Type | Init | Name | Purpose |
|---|---|---|---|---|---|
| +0 | 8 | `ptr` | funcCtx | funcContext | Pointer to CompilationContext (copied from `sched+8`) |
| +8 | 160 | `QWORD[20]` | 0 | batchWorkArray | Fixed-size working array for batch state tracking; likely holds instruction pointers or batch boundary markers during scheduling |
| +168 | 8 | `ptr` | alloc'd | perBBArray | Per-BB batch tracking sub-array; `8 * numBBs` bytes, zero-initialized. Each 8-byte entry holds a batch start/end instruction pointer for one basic block |
| +176 | 4 | `DWORD` | 0 | flags | Status/control flags |
| +180 | 4 | -- | -- | (padding) | Pad to 184-byte allocation |

The per-BB sub-array size is derived from `*(sched+392)` (maxBBSizeForAlloc), with an overflow check capping the multiplication at `0xFFFFFFFFFFFFFFF` (2^60 - 1) entries.

### DynBatch Working State (in scheduler context)

The bulk of the DynBatch working state lives directly in the scheduler context, initialized by `sub_8C1BA0` (InitDynBatchState). These fields are used by the priority function during Phase 3 scheduling.

| Offset | Size | Type | Init | Name | Purpose |
|---|---|---|---|---|---|
| +464 | 4 | `int32` | 0 | batchSlotCount | Number of instructions accumulated in the current batch |
| +468 | 4 | `int32` | -- | prevBatchSize | Size of previously-completed batch |
| +476 | 4 | `int32` | adj | adjustedBatchTarget | Adjusted batch depth target; capped to `min(maxStallCycles, batchTargetCount)`, halved when `2 * maxStall > target` |
| +480 | 4 | `int32` | -- | lastBatchEndPos | Scheduling position of the last instruction in the current batch |
| +488 | 8 | `QWORD` | 0xFFFFFFFF | batchWindow | Batch window start BB offset; sentinel 0xFFFFFFFF means "no batch active" |
| +492 | 4 | `int32` | 0 | regDelta | Register pressure delta accumulator across batch boundaries |
| +496 | 4 | `int32` | 0 | maxRegInBatch | Maximum register pressure observed within current batch |
| +500 | 4 | `int32` | from +72 | regBaseCount | Base register count; copied from `sched+72`, reset on batch boundary |
| +504 | 8 | `QWORD` | 0 | maxRegSpan | Maximum register span (pressure peak minus baseline) across all batches |
| +508 | 4 | `int32` | 0 | regBaseline | Register count baseline for delta computation |
| +512 | 4 | `int32` | 0 | minOverflowCost | Minimum overflow cost; updated when batch exceeds register budget |
| +516 | 4 | `int32` | -1 | batchDepthLimit | Per-batch maximum depth; -1 = unlimited (overwritten from BB analysis) |
| +520 | 1 | `byte` | 0 | batchOverflow | Set to 1 when batch exceeds register budget + base count |
| +521 | 1 | `byte` | 0 | batchAbort | Set to 1 when opcode 96 (WGMMA commit) detected with `sched+524` flag |
| +536+ | var | `ptr[]` | -- | batchSlots | Array of instruction pointers in the current batch; `sched+536 + 8*i` for slot `i` |

The batch target adjustment algorithm in `sub_8C1BA0`:
```
adjustedTarget = maxStallCycles           // from sched+404
if maxStallCycles > batchTargetCount:
    adjustedTarget = batchTargetCount     // cap to target
if batchTargetCount > maxStallCycles:
    if batchMode == 0 and maxStallCycles < batchTargetCount:
        if batchTargetCount >= 2 * maxStallCycles:
            adjustedTarget = batchTargetCount / ceil(batchTargetCount / maxStallCycles)
        else:
            adjustedTarget = batchTargetCount / 2
```

When a batch boundary is detected (instruction's BB start offset exceeds the batch window), `sub_8C1BA0` evaluates the batch: it computes the register pressure delta, checks whether the batch overflows the combined register budget (`regBaseCount + regDelta + maxRegSpan`), and either accepts the batch or trims it by walking backward through the `batchSlots` array to find a smaller valid batch.

### DynBatch Priority Function (sub\_8E0F18 via sub\_8C9320)

Vtable[8] enters the common priority evaluator (`sub_8C9320`) with `mode_field == 2`. Four mechanisms modify the score relative to the ILP/Latency baseline.

**Batch slot accumulation** (`sub_8C1BA0`, re-invoked when `batchSlotCount < 0`):

```
function InitDynBatchState(sched, funcCtx):               // sub_8C1BA0
    batchWindow = 0xFFFFFFFF; batchDepthLimit = -1; regBaseCount = regLiveCount
    for instr in ReadyList:
        node = instr.schedNode
        if batchSlotCount > 0:
            if (instr.opcode & 0xCFFF) == 96 and wgmmaCommitFlag:  // WGMMA.COMMIT
                batchAbort = 1; break
            if node.bbStartOffset <= batchWindow:         // inside current batch
                for each existing batchSlot:              // dependency scan
                    if slot has data-dep edge to instr: maxRegInBatch = max(...); goto Accept
                goto Boundary
        Boundary:
            if not node.isBarrier:
                combined = regBaseCount + regDelta + maxRegSpan
                if RegBudget(sched) < combined: batchOverflow = 1; break
            if batchSlotCount == 0: batchWindow = node.bbStartOffset
        Accept:
            batchSlots[batchSlotCount++] = instr; lastBatchEndPos = node.bbStartOffset
            if batchSlotCount == adjustedBatchTarget: break
            batchDepthLimit = max(batchDepthLimit, node.depth)
```

**Depth-limit tightening** (mode==2 path in `sub_8C9320`): during successor-chain scanning, DynBatch clamps the candidate's minimum depth: `minSuccDepth = succ ? min(minSuccDepth, succ.schedNode.bbStartOffset) : -1`.

**WGMMA/HMMA bonus and batch-membership bits** (8-bit priority vector):

```
    opcode = candidate.opcode & 0xCFFF
    hmmaFree   = (opcode == 39) and (operandSlot[adj] & 3) == 0  // HMMA free slot
    wgmmaBonus = (opcode == 96) and wgmmaCommitFlag               // WGMMA.COMMIT
    //  bit 7: hmmaFree  6: wgmmaBonus  5: wgmmaBonus  4: cutlassHint
    //  bit 3: pressureOK  2: batchMember  1: depthOK  0: tiebreaker
    priorityBits = (hmmaFree<<7)|(wgmmaBonus<<6)|(wgmmaBonus<<5)|(cutlassHint<<4)
                 | (pressureOK<<3)|(batchMember<<2)|(depthOK<<1)|tiebreaker
    // suppress wgmma/cutlass bonus when candidate precedes lastBatchEndPos
    if (wgmmaBonus<<5)|(cutlassHint<<4):
        if candidate.depth < lastBatchEndPos: wgmmaBonus = batchTargetCount > 0 ? wgmmaBonus : 0
    batchMember = (batchSlotCount > 0) and (batchSlotCount <= readyBatchCount)
    if batchCountdown and candidate.depth < lastBatchEndPos: batchMember = 1
    elif candidate != bestReady and candidate.depth < lastBatchEndPos: batchMember = 0
```

**Post-selection batch state update**:

```
    if winner.isBarrier:
        if not batchCountdown: batchCountdown = ComputeBatchWindow(readyCount, ...)
        batchTargetCount--; batchCountdown--
        if winner.depth >= 0: batchMode--
        // over-large batch detection: force re-init if ratio exceeded
        if batchMode==0 and batchTargetCount>1 and batchTargetCount-1 < maxStallCycles:
            batchCountdown=0; batchSlotCount=-1
        if batchCountdown == 0: batchSlotCount = -1
    elif batchCountdown > 0:                                  // non-barrier pressure trim
        if readyCount <= 3: batchCountdown = max(batchCountdown, readyBatchCount)
        elif totalReadyRegs + regDelta > regBudget: batchCountdown = batchSlotCount
```

## Unified Scheduling Engine

`sub_688DD0` (20 KB) is the single engine that all three phases invoke. Its behavior is parameterized by:

1. **Mode byte** (argument `a2`): 0x39 = ReduceReg, 0x49 = ILP/Latency, 0x41 = DynBatch.
2. **Rebuild flag** (argument `a4`): when true, reconstructs the dependency DAG via `sub_6833F0`.
3. **Vtable dispatch**: uses `*(a1+40)` and `*(a1+48)` for polymorphic pre/post scheduling hooks.

```
function ScheduleEngine(sched, mode, arg3, rebuild):
    if rebuild:
        InitScheduleRegion(sched)               // sub_6833F0
        // allocates 72-byte per-BB records, queries knobs 595 (PreserveSchedOrderSame), 743 (SchedCrossBlockInstsToSpeculate), 747 (SchedCrossBlockTexToSpeculate)

    for each bb in sched.basic_blocks:
        // 10 register pressure counters from per-BB record +4..+40 into context +48..+87
        InitResourceTracking(sched, bb)          // sub_A091C0
        ReadyList = BuildReadyList(sched)        // sub_6820B0

        while ReadyList is not empty:
            best = SelectBestInstruction(sched)  // via tagged-pointer vtable dispatch (see below)
            ScheduleInstruction(sched, best)     // sub_682200
            UpdateResourceState(sched, best)     // sub_A09530
            UpdateWARTracking(sched, best)       // sub_A09D40
            RelinkInstruction(best)              // sub_925510

            // Update dependency counts, add newly-ready instructions
            for each successor of best:
                successor.dep_count -= 1
                if successor.dep_count == 0:
                    ReadyList.insert(successor)
```

### SelectBestInstruction Tagged-Pointer Dispatch

The `mode` argument to `sub_688DD0` is not a simple integer -- it is a **tagged pointer** (low bit = 1). The engine uses this encoding to resolve the polymorphic `SelectBestInstruction` call at runtime without an explicit switch statement:

```
// Decompiled dispatch at lines 477-480 of sub_688DD0:
selectFn = (function_ptr) mode                          // raw value: 0x39, 0x41, or 0x49
if (mode & 1) != 0:                                    // low bit set -> indirect dispatch
    selectFn = *(function_ptr*)(sched.vtable + mode - 1)  // dereference through vtable
best = selectFn(sched, &prev_instr, last_scheduled)
```

The scheduling context stores its vtable pointer at offset +0 (`sched[0] = off_21DBC80`, the 77-entry scheduling function table). The mode byte indexes into this table after stripping the tag bit:

| Mode byte | Phase | `mode - 1` | Vtable offset | Index | Slot target (off_21DBC80) |
|---|---|---|---|---|---|
| `0x39` (57) | ReduceReg | +56 | `[7]` | core slot 7 | `0x8DA6A0` (`nullsub_261`, 2 B) -- base-class no-op; ReduceReg behaviour is selected by `sched+240==1` inside the priority evaluator |
| `0x41` (65) | DynBatch | +64 | `[8]` | pipeline_A slot 0 | `0x8E0F18` -- thunk entry inside `sub_8E0DB0` (DynBatch pipeline backend) |
| `0x49` (73) | ILP/Latency | +72 | `[9]` | pipeline_A slot 1 | `0x8E0F90` -- thunk entry inside `sub_8E0DB0` (ILP pipeline backend) |

Slots 8 and 9 are interior labels of the shared dispatcher `sub_8E0DB0` (~700 B), not standalone functions; multiple adjacent vtable entries in this table resolve to the same routine, which then branches on the originating slot offset. Slot 7 is a hard nullsub: when ReduceReg fires, the mode-1 path in `sub_8C9320` (via the `sched+240` selector and the `sched+484` ReduceReg flag) carries the entire register-pressure-minimizing heuristic.

The orchestrator `sub_8D0640` invokes the engine three times with these modes:

```
// Phase 1: ReduceReg -- minimize register pressure
sched.mode_field = 1                                    // *(DWORD*)(sched+240) = 1
sched.reduceReg_flag = 1                                // *(BYTE*)(sched+484) = 1
sub_688DD0(sched, 0x39, 0, false)                       // ReduceReg, no rebuild

// Phase 2: DynBatch -- batch-aware grouping (conditional on knobs)
sched.mode_field = 2                                    // *(DWORD*)(sched+240) = 2
sub_688DD0(sched, 0x41, 0, rebuild_flag)                // DynBatch, optional rebuild

// Phase 3: ILP/Latency -- maximize instruction-level parallelism
sched.mode_field = 0                                    // *(DWORD*)(sched+240) = 0
sub_688DD0(sched, 0x49, 0, false)                       // ILP, no rebuild
```

The three priority functions share the same signature `(sched_ctx*, prev_ptr*, last_instr) -> best_instr*` but implement different heuristics. All three call into the common 47 KB priority evaluator `sub_8C9320` which reads `sched.mode_field` at offset +240 to select weight vectors for the 8-bit priority encoding.

#### Pre/Post Scheduling Hooks

In addition to the SelectBest dispatch, the engine calls two polymorphic hooks per basic block via the scheduler context vtable at `sched[0]`:

| Hook | Vtable offset | Called when | Purpose |
|---|---|---|---|
| `*(sched.vtable + 40)` | `[5]` = `sub_8DA6B0` | Before `BuildReadyList` | Pre-BB scheduling setup (nullsub in core; overridden by backends) |
| `*(sched.vtable + 48)` | `[6]` = `sub_8DA680` | After `BuildReadyList` | Post-BB ready-list adjustment (nullsub in core; overridden by backends) |

These hooks are checked against sentinel values (`nullsub_39`, `nullsub_40`) and skipped when the backend provides no override.

The engine manages **10 register pressure counters** at scheduler context offsets 48--87 (copied from the [per-block record](#per-block-scheduling-record-72-bytes) offsets +4--+40 at BB entry). These correspond to the GPU register classes: R (general), P (predicate), UR (uniform), UP (uniform predicate), B (barrier), and 5 architecture-specific classes. Counter [0] (R class) uses a separate update path; counters [1]--[9] are decremented from a per-opcode resource cost table during the scheduling loop.

## Ready List Construction

`sub_6820B0` (1.5 KB) builds the initial ready list by scanning the instruction linked list for nodes with zero unsatisfied dependencies.

```
function BuildReadyList(sched):
    for instr in sched.instruction_list:
        if instr.opcode == 52:         // NOP/BB boundary
            continue                    // follow through to real instruction
        if instr.dep_count == 0:
            instr.next_ready = sched.ready_head
            sched.ready_head = instr
            vtable_call(sched, 104, instr)  // ready-list insertion callback
            instr.latency_counter = 0
```

The ready list is maintained as a sorted linked list (via pointer at instruction offset +16). The priority function determines sort order.

## Priority Function

`sub_8C9320` (47 KB decompiled, ~1300 lines) is the heart of instruction selection. It computes a scheduling priority score as an 8-bit packed encoding combining multiple heuristic factors. The function uses approximately 200 local variables and a 0x330-byte stack frame.

### Priority Factors

| Factor | Source | Weight adjustment |
|---|---|---|
| **Register pressure** | Current live count vs budget at `sched+432` | Primary factor in ReduceReg mode |
| **Instruction latency** | `sub_693BC0` latency query | Primary factor in ILP mode |
| **Critical path position** | DAG depth from `sched+464, sched+380` | Favors critical-path instructions |
| **FU contention** | 10-element resource vector via `sub_8C7290` | Avoids saturating a single pipe |
| **Hot/cold memory** | `sub_A9CDE0` (hot=global) / `sub_A9CF90` (cold=const) | Prioritizes latency-sensitive ops |
| **Anti-dependency** | WAR hazard cost | Breaks ties with anti-dep distance |
| **Barrier dependencies** | Barrier flag at `instr+376` | Defers barrier-blocked instructions |
| **Priority queue depth** | Knob 770 (default 4) | Limits lookahead window |

### Priority Encoding

The priority value is packed into an integer with 8-bit fields. Each field is computed from the corresponding factor and shifted into position. The packed encoding allows the ready list to maintain sort order with a single integer comparison, avoiding multi-key sorting overhead.

Key subroutines called during priority computation:

| Address | Purpose |
|---|---|
| `sub_8C67A0` | Compute resource cost for instruction and update BB resource table |
| `sub_8C7120` | Barrier tracking update |
| `sub_8C7290` | Copy 10-element resource vector from per-BB slot (SSE-optimized) |
| `sub_8C7720` | Instruction reordering within BB (red-black tree operations) |
| `sub_693BC0` | Memory space classification / latency query |
| `sub_6818D0` | Register count to hardware-aligned unit conversion |

## Resource Tracking

The scheduler tracks 10 functional unit resource counters per basic block. Each counter corresponds to a hardware execution pipe.

### Resource Vector Layout

Each per-BB resource slot occupies 84 bytes (21 DWORDs) stored at `*(scheduler+672) + 84 * slot_index`:

| Offset (within slot) | Size | Content |
|---|---|---|
| 0--36 | 10 x int32 | Current resource usage per FU |
| 40--76 | 10 x int32 | Resource pressure delta |
| 80 | int32 | BB-entered flag and auxiliary bits |

The 10 functional unit pipes (inferred from resource model queries):

| Index | Pipe | Typical instructions |
|---|---|---|
| 0 | Integer ALU | IADD, IMAD, ISETP, LOP, SHF |
| 1 | FP32 | FADD, FFMA, FMUL, FSETP |
| 2 | FP64 | DADD, DFMA, DMUL |
| 3 | Tensor core | HMMA, IMMA, BMMA, BGMMA |
| 4 | Load/store | LD, ST, LDG, STG, LDS, STS |
| 5 | Texture | TEX, TLD, TXQ |
| 6 | Branch/control | BRA, JMP, EXIT, RET, BAR |
| 7 | Shared memory | ATOMS, REDS, LDS, STS |
| 8 | Special function | MUFU (RCP, RSQ, SIN, COS, EX2, LG2) |
| 9 | Uniform/predicate | UPLOP, UISETP, uniform operations |

`sub_8C67A0` computes per-instruction resource costs by calling the resource model (`sub_A08A00`) three times:
- Mode 1: the instruction's own execution cost
- Mode 2: operand release costs for last-use operands
- Mode 3: combined instruction + BB-level impact

SSE intrinsics (`_mm_add_epi32`) are used for vector accumulation.

## Register Budget

`sub_8CEE80` (8.7 KB) computes the occupancy-aware register budget that the scheduler respects during instruction ordering.

```
function ComputeRegisterBudget(sched):
    hw = sched.func.sm_backend          // at func+1584 (provides hw latency profiles)
    maxRegs = hw[154]                   // architecture register limit

    coeff = KnobGetDouble(740)          // default 0.045
    if KnobGetBool(763):                // budget disabled
        budget = hw[157]                // use fixed count from profile
    else:
        physRegs = VirtToPhys(sched, maxRegs)   // sub_A99FE0
        budget = physRegs - (physRegs >> 6)     // 98.4% utilization

    // Maxwell (sm_50/sm_52/sm_53): apply dual-issue register budget
    // The Maxwell back-end reserves extra registers per warp so paired
    // co-issued instructions can keep both dispatch slots busy without
    // spilling. Gated by `target+1032 bit 7` (same gate as `sub_8CF5D0`).
    if arch_id == 5:
        budget = DualIssueBudget(budget)    // Maxwell-only register-target adjustment

    pressureCurve = ComputePressureCurve(sched, budget - 2)  // sub_8CE520

    sched.regBudget         = budget     // offset +432
    sched.committedTarget   = ...        // offset +324
    sched.minRegs           = ...        // offset +316
    sched.pressureSlack     = ...        // offset +320
```

### Pressure Curve (`sub_8CE520`)

`sub_8CE520(sched, regLimit, &nopDensity)` measures instruction density in a sliding window to decide whether reducing register pressure improves ILP. Returns weighted real-instruction density; writes NOP density to `nopDensity`; sets `sched.usePressure` (+522) and `sched.minPhysRegs` (+512).

**Seed initialization.** A seed object at `func[223]` (byte offset +1784) configures the curve. Default: `seed.SetBreakpoints(4, 2, 6)` via vtable+16 -- the three arguments are `windowSize`, `minIssueWidth`, `maxIssueWidth`, defining the piecewise linear occupancy-to-issue-width mapping. When `KnobIsSet(750)` is true, calls `seed.ParseString(KnobGetString(750))` via vtable+24 instead -- the `SchedEstimatedLoopIterations` string encodes custom per-loop iteration hints replacing the (4,2,6) defaults. If the function has no loops (`sched[668] == 0`), returns 0.0 with `usePressure = 0`.

```
function ComputePressureCurve(sched, regLimit):       // sub_8CE520
    seed = sched.func[223]
    if KnobIsSet(750): seed.ParseString(KnobGetString(750))   // vtable+24
    else:              seed.SetBreakpoints(4, 2, 6)            // vtable+16
    if !sched.hasLoops: sched.usePressure = 0; return 0.0

    W = min(sched.maxStallCycles, 16)                 // +404, capped
    Wt = sched.stallThreshold                         // +408
    // Two parallel tracks: A=real instrs, B=NOPs
    // Per-track state: {weight, total, fill, peakSpan, minPeak=0x1869F}
    loopWt = 1.0

    for each instruction in first BB linked list:
      on loop-header (opcode 52) or BB-boundary (masked opcode 96):
          total += ceil(fill / W) * loopWt            // flush partial window
          fill = 0;  if loop-header: loopWt = EstimateTrip(func, bb)
      on real instruction (track A):
          span = progPoint_delta
          if fill > 0 and span > regLimit and span > peakSpan: peakSpan = span
          if peakSpan > 0: minPeak = min(minPeak, peakSpan)
          slot[fill] = latencyClass;  fill += min(Wt, W - fill)
          if fill == W: weight += loopWt; minPeak tracks minimum; reset window
      on NOP (track B): symmetric

    total += ceil(fill / W) * loopWt                  // final flush
    physRegs = RegToHWUnits(sched, min(minPeakA, minPeakB))
    archCap  = hw[154] + hw[159]                      // via vtable[93]
    if physRegs > archCap:      usePressure = 0       // exceeds arch limit
    elif weightA <= totalA:     usePressure = (weightB > totalB)
    sched.usePressure = usePressure                   // +522
    sched.minPhysRegs = physRegs                      // +512
    return weightA  // *nopDensity = weightB
```

The caller (`sub_8CEE80`) converts densities to ILP ratios (`occupancy / density`), iterating up to 5 candidate budgets (3 when `hw[360] > 2`) and picking the maximum. Knob 740 (default 0.045) scales a per-register penalty when the budget overshoots the live-register count.

## Dependency Graph

The dependency DAG is built in two stages:

### Stage 1: Pre-scheduling scan (`sub_8CF880`, 28 KB)

Iterates basic blocks in reverse order. For each BB:
- Checks knobs 314 (`FenceInterference`) / 313 (`FenceCode`) for per-instruction scheduling fence conditions
- Walks the instruction linked list, identifying NOP/control instructions
- Builds dependency edges via `sub_8D9930`
- Manages memory arenas with SSE-optimized copies for instruction metadata arrays

### Stage 2: Edge construction (`sub_8D9930`, 19 KB)

For each pair of instructions in a BB, checks for:
- **RAW** (true) dependencies: read-after-write on the same register
- **WAR** (anti) dependencies: write-after-read
- **WAW** (output) dependencies: write-after-write
- **Memory** dependencies: through shared/global memory (conservative ordering)
- **Barrier** dependencies: through barrier/sync instructions

Uses operand analysis from `sub_894290` (27 KB) which processes 16-bit operand descriptors encoding register class, bank, and dependency type.

#### Edge node layout (32 bytes, allocated via `sub_6805C0`/`sub_680B60`)

```
Offset  Size   Field
 +0     QWORD  next            Singly-linked list pointer (head at producer+56)
 +8     QWORD  target          Pointer to consumer instruction
+16     DWORD  dep_class       Register class/bank index (operand category 0..9)
+20     WORD   edge_type_mask  Bitmask [9:0] -- which dependency types this edge carries
+24     DWORD  reason_mask     Bitmask [24:0] -- which operand positions triggered the edge
```

Edge type encoding in `edge_type_mask`:

| Bit | Type | Creator path | Meaning |
|---|---|---|---|
| 0 | RAW | `sub_6848D0` -> `sub_684470(..dep_type=0)` | True dependency: consumer reads a register the producer writes |
| 1 | WAR | `sub_684970` -> `sub_684470(..dep_type=1)` | Anti dependency: consumer writes a register the producer reads |
| 2 | WAW | `sub_684920` -> `sub_684470(..dep_type=2)` | Output dependency: both instructions write the same register |
| 10 | Memory | `sub_680B60(..dep_class=3, bit=10, reason=25)` | Memory ordering through shared/global space |
| 25 | Barrier | `sub_680B60(..dep_class=10, bit=25, reason=25)` | Barrier/sync ordering (`BAR`, `MEMBAR`, `BSSY`) |

A single edge can carry multiple type bits (e.g., bits 0+2 when a register has both RAW and WAW hazards between the same pair). `sub_684470` swaps producer/consumer for WAW edges (dep\_type==2) before calling `sub_6805C0`, so WAW edges always point from the earlier writer to the later writer.

#### Edge latency assignment pseudocode

The stall/barrier pipeline (`sub_8D7760`, 41 KB) computes per-edge latency when walking the DAG backward from each consumer. The latency depends on the edge type and the producer's scheduling class, which indexes into `per_sm_dependency_rules` (40-byte records):

```
function GetEdgeLatency(sm_backend, producer, consumer, edge):
    rule = LookupRule(sm_backend, producer.sched_class)
    //  per_sm_dependency_rules fields (40-byte record):
    //    .latency           uint8   pipeline cycles for RAW (0..255)
    //    .write_latency     int8    WAW override (-1 = use default)
    //    .read_latency      int8    read-after-read (-1 = unused)
    //    .barrier_latency   uint8   cycles for barrier-protected edges
    //    .stall_cycles      uint8   minimum HW-enforced stall
    //    .throughput_inv    uint8   reciprocal throughput (issue spacing)

    if edge.edge_type_mask & (1 << 0):           // --- RAW ---
        lat = rule.latency                        // full pipeline latency
        //  sm_100: FFMA=17, LDG=42, TEX=46, HMMA=72, WGMMA=255

    else if edge.edge_type_mask & (1 << 2):       // --- WAW ---
        if rule.write_latency != -1:
            lat = rule.write_latency              // explicit WAW latency
            //  sm_100: unit 11 -> 6, unit 35 -> 8 (write-port occupancy)
        else:
            lat = 1                               // default: 1 cycle (rename/commit)

    else if edge.edge_type_mask & (1 << 1):       // --- WAR ---
        lat = 0                                   // anti-deps: zero latency
                                                  // (read completes at decode, before write)

    else if edge.dep_class == 3:                  // --- Memory ---
        lat = rule.stall_cycles                   // conservative stall distance
        //  sm_100: LDG/STG=1..5, TEX=12, WGMMA=39

    else if edge.dep_class == 10:                 // --- Barrier ---
        lat = rule.barrier_latency                // barrier threshold from HW profile
        //  sm_100: most ALU/FP=56, TEX/SFU=8..14, WGMMA=56

    return lat
```

The final stall count for an instruction is `max(0, lat - scheduling_distance)` across all incoming edges, clamped to the architecture maximum (knobs 805/806, typically 15--16). When the required wait exceeds the stall ceiling, the scoreboard assigns a hardware dependency barrier instead (see [Scoreboards](scoreboards.md)).

### Supplementary dependency builders

| Address | Size | Purpose |
|---|---|---|
| `sub_68A690` | 31 KB | BuildDependencies -- def-use chain construction |
| `sub_6A97B0` | 26 KB | AddDependencyEdges -- register-level edges |
| `sub_6A2D30` | 11 KB | ChainDependencies -- memory ordering constraints |
| `sub_6A78F0` | 23 KB | ProcessOperands -- operand dependency extraction |

## Pre-Scheduling Setup

`sub_8CBAD0` (2.9 KB) performs BB scanning and resource allocation before the scheduling passes begin.

Key behaviors:
- Counts instructions per basic block. If any BB exceeds **4095 instructions**, it inserts a scheduling barrier (`sub_931920`) to split the block.
- Tracks maximum BB size at `scheduler+388`.
- Detects opcode 246 (texture operations) and sets `scheduler+384 = 1`.
- Allocates per-slot arrays:
  - `scheduler+672`: 84-byte scheduling slots (resource tracking)
  - `scheduler+280`: 48-byte analysis slots (if `opt_level > 2`)
  - `scheduler+248`, `scheduler+256`: register pressure bitvectors sized to `(numRegs+1)` or `(2*numRegs+2)` if knob 420 (`LivenessUseHiLo`, dual-register tracking) is active

## Pre-Scheduling vs Post-Scheduling

The scheduler runs at two distinct points in the ptxas pipeline:

| Aspect | Pre-scheduling | Post-scheduling |
|---|---|---|
| **Timing** | Before physical register allocation | After physical register allocation |
| **Register model** | Virtual registers | Physical registers |
| **Primary goal** | Reduce register pressure, order for regalloc | Hide latencies, minimize stalls |
| **Phases active** | All 3 (ReduceReg, ILP, DynBatch) | Refinement pass |
| **Budget source** | Occupancy model estimate | Actual allocation result |
| **Entry** | `sub_8D0640` | `sub_7F5D50` / `sub_A97600` (42 KB) |

Post-scheduling uses the actual physical register assignments for precise dependency distances and can make final decisions about stall insertion and scoreboard barrier placement.

## Scheduling Variants

The region 0x89C550--0x8BE320 contains 17+ specialized scheduling strategies, each implementing a different approach or targeting a different code pattern:

| Address | Size | Strategy | Notes |
|---|---|---|---|
| `sub_8B9390` | 23 KB | Software pipelining | Loop body overlapping |
| `sub_8B77C0` | 15 KB | Dual-issue scheduling | Pair co-issuable instructions |
| `sub_8BDC40` | 7.9 KB | Dual-issue pairing | Instruction pair selection |
| `sub_8B8900` | 12 KB | Tensor scheduling | HMMA/BMMA grouping |
| `sub_8BAAE0` | 15 KB | Loop-aware scheduling | Trip count + register awareness |
| `sub_8B6D60` | 12 KB | Pressure-optimized | Minimize live range overlap |
| `sub_8B5400` | 14 KB | Latency-optimized | Maximize memory latency hiding |
| `sub_8B1190` | 16 KB | Backtracking scheduler | Undo and retry on conflict |
| `sub_8B2D90` | 18 KB | Global schedule optimization | Cross-BB considerations |
| `sub_8B4590` | 13 KB | Permutation search | Try schedule permutations |
| `sub_8A9D80` | 21 KB | Depth-first scheduling | DFS-based instruction ordering |
| `sub_8AB750` | 9.8 KB | Critical path computation | DAG analysis for priorities |
| `sub_8BB9C0` | 8.2 KB | Prefetch scheduling | Memory prefetch insertion |
| `sub_8BC0B0` | 6.1 KB | Barrier coalescence | Merge adjacent barriers |
| `sub_8BC990` | 7.6 KB | Scoreboard optimization | Minimize scoreboard usage |
| `sub_8BCFA0` | 6.8 KB | Warp schedule optimization | Warp-level yield tuning |
| `sub_8BE320` | 25 KB | Complex scheduling pass | Multi-strategy combined pass |

### Strategy Selection

The scheduler selects strategies based on code features detected during pre-scheduling analysis (`sub_8CBAD0`). The decision cascades as follows:

```
function SelectStrategy(BB, scheduler, arch):
    if BB.is_loop_body AND scheduler.opt_level >= 3:
        if BB.has_tensor_ops (scheduler+384 == 1):
            return TensorScheduler         // sub_8B8900
        if BB.trip_count_known AND BB.instr_count <= 256:
            swpipe_ii = ComputeII(BB)      // initiation interval
            if swpipe_ii > 0 AND swpipe_ii < BB.instr_count:
                return SoftwarePipeline    // sub_8B9390
        return LoopScheduler               // sub_8BAAE0

    if (target+1032 bit 7 set) AND scheduler+328 > 0:  // dual-issue benefit > 0
        return DualIssueScheduler          // sub_8B77C0 -- gate covers sm_50/sm_52/sm_53 (Maxwell)

    if BB.instr_count <= 12:
        return PermuteSchedule             // sub_8B4590  (exhaustive)

    if scheduler.mode == ReduceReg:
        return PressureOptimized           // sub_8B6D60
    if scheduler.mode == ILP:
        return LatencyOptimized            // sub_8B5400

    return DefaultListSchedule             // sub_89C550 with backtracking
```

The backtracking, dual-issue, tensor, and software pipelining strategies are the most complex. Skeleton pseudocode for each follows.

### Backtracking Scheduler (`sub_8B1190`, 16 KB)

Extends standard list scheduling with depth-bounded rollback. When a scheduled instruction causes a resource conflict or pressure spike, the scheduler undoes previous decisions and tries alternatives.

```
function ScheduleWithBacktrack(BB, dag, ready_list):
    // Phase 1: Allocate state snapshots -- 64-byte slots x 773 max depth
    snapshot_buf = Alloc(773 * 64)         // 49408 bytes
    for i in 0..772:
        snapshot_buf[i].sched_id = -1      // unscheduled marker
        memset(snapshot_buf[i].state, 0, 52)

    history = Alloc(773 * 36)              // per-step resource delta
    rejection_set = Alloc(35 * 16)         // bitvector of rejected candidates
    committed = []
    depth = 0
    max_backtrack_depth = min(10, BB.instr_count / 4)

    while ready_list is not empty:
        best = SelectBestInstruction(ready_list, rejection_set[depth])
        if best == NULL:
            // All candidates rejected at this depth -- backtrack
            if depth == 0 OR depth > max_backtrack_depth:
                // Cannot backtrack further; force-commit cheapest
                best = ForcePick(ready_list)
                CommitInstruction(best, committed)
                continue
            // Rollback: restore snapshot, add last committed to rejection set
            depth -= 1
            RestoreSnapshot(snapshot_buf[depth])
            rejection_set[depth] |= (1 << committed.pop().slot_id)
            continue

        // Tentatively schedule
        SaveSnapshot(snapshot_buf[depth], scheduler_state)
        cost = EmitInstruction(best, dag)
        if cost.stalls > threshold OR cost.pressure > budget:
            // Reject this choice, try next candidate at same depth
            RestoreSnapshot(snapshot_buf[depth])
            rejection_set[depth] |= (1 << best.slot_id)
            continue

        // Accept
        CommitInstruction(best, committed)
        depth += 1
        rejection_set[depth] = 0           // clean slate for next position
```

### Dual-Issue Scheduler (`sub_8B77C0`, 15 KB)

Pairs compatible instructions into dual-issue slots on the Maxwell family (sm\_50/sm\_52/sm\_53). The gate is `target+1032` bit 7, set only for the Maxwell back-end class -- subsequent architectures (Pascal sm\_60+ onwards) clear this bit and the strategy is never selected. The outer loop walks scheduling slots; the inner loop finds a co-issuable partner via the dependency rule table.

`sub_8B77C0` orchestrates the per-slot search; the actual pair-record bookkeeping (rejection set, partner commit, cross-slot dependency flag) lives in `sub_8BDC40` (7.9 KB), which `sub_8B77C0` calls once per candidate. Compatibility predicates `sub_A9CDE0` (`IsHotMemory`) and `sub_A9CF90` (`IsColdMemory`) drive the partner choice -- see [Dual-Issue Rules](latency-model.md#dual-issue-rules) for the memory-space classification and the `pipe_masks_b` pairing matrix.

```
function DualIssueSchedule(scheduler, slot_start, slot_end, phase_mask):
    for slot in slot_start..slot_end:
        if not (phase_mask & (1 << slot)):
            continue
        // Iterate candidates in this slot's ready bucket
        bucket = scheduler.dep_table[slot]     // linked list from sub_8A4820
        for each candidate in bucket:
            if candidate.id == -1 OR candidate.id == current_instr:
                continue
            pair_record = scheduler.slot_array[candidate.id * 96]
            // Check pairing compatibility (sub_A9CDE0 / sub_A9CF90)
            if not IsDualIssuable(pair_record):
                continue
            if HasDataDependency(candidate, last_scheduled):
                continue
            // Found valid pair -- mark both for co-issue
            MarkDualIssue(pair_record, slot)
            scheduler.paired_count += 1
            break
    FinalizeSchedule(scheduler)
```

### Tensor Scheduler (`sub_8B8900`, 12 KB)

Groups HMMA/BMMA/BGMMA tensor core instructions into contiguous blocks, respecting accumulator register lifetimes. Iterates over scheduling slots using a bitmask of active tensor groups.

```
function TensorSchedule(ctx, group_mask, instr):
    phase_count = ctx+120                      // number of tensor phases
    if phase_count < 0:
        return

    for phase in 0..phase_count:
        slot_bit = 1 << phase
        if (slot_bit & group_mask) == 0:
            continue
        // Walk the tensor operation list for this phase
        group_head = ctx.slot_array[phase * 64]
        entry = group_head.first_op
        while entry != group_head.sentinel:
            node = LookupSchedNode(ctx.dag, entry.instr_id)
            if node == ctx.exit_node:
                continue
            // Check if this is an HMMA/BMMA/BGMMA via opcode class at node+166
            if node.is_tensor_op:
                // Scan accumulator def/use bitvector (node+104..node+136)
                for each acc_reg in node.acc_def_bits:
                    // Verify no intervening non-tensor use of acc_reg
                    dep_ok = CheckTensorDep(ctx.dep_graph, instr, acc_reg)
                    if dep_ok AND word(instr+12) != 4:  // not a barrier
                        group_mask |= slot_bit
            // Check write-after-read set (node+120..node+136)
            if node.has_war_deps:
                // scan WAR bitvectors identically to accumulator defs
                ...process WAR set with same pattern...
            entry = entry.next
```

### Software Pipelining (`sub_8B9390`, 23 KB)

Overlaps successive loop iterations by interleaving instructions from different iterations into a single schedule. Computes the initiation interval (II) and maps instructions to pipeline stages.

```
function SoftwarePipeline(ctx, loop_desc, stage_mask):
    trip_count = loop_desc+28                  // extracted from loop analysis
    prologue_start = trip_count * 24

    // Phase 1: Process pre-existing cross-iteration dependencies
    if ctx+48 (has_cross_iter_deps):
        for stage in loop_desc.first_stage .. loop_desc.last_stage:
            iter = IteratorInit(ctx+56, stage)
            while iter.valid:
                if iter.trip_distance > 0:
                    dep_node = LookupNode(ctx.dag, trip_count)
                    if dep_node AND dep_node.has_successors:
                        // Register cross-iteration edge in DAG
                        for each succ in dep_node.successors:
                            sub_8B5E20(ctx.slot[succ.id * 96 + 24], stage)
                iter.advance()

    // Phase 2: Compute per-stage schedule with register class partitioning
    num_stages = ctx+120
    has_epilogue = ctx+140
    if has_epilogue:
        num_stages += 1

    for stage in num_stages .. (ctx+128 + (ctx+128 == 0) - 1):
        if not (stage_mask & (1 << stage)):
            continue
        slot_base = ctx+264 + (stage << 6)
        first_instr = slot_base.first_op
        if first_instr == slot_base.sentinel:
            continue
        // Extract instruction's pipeline class from dep info
        dep_info = *(instr+112)
        flags = byte(dep_info+48)
        if (flags & 0x10) AND (flags >> 5) == stage:
            // Modulo-scheduled position: emit at this stage
            reg_range = LookupRegRange(ctx+8, dep_info+20)
            sub_8B9230(ctx, loop_desc, stage_bit)    // fast-path emit
        else if (flags & 0x01) AND ((flags >> 1) & 7) == stage:
            // Cross-iteration carried dependency
            reg_range = LookupRegRange(ctx+8, dep_info+20, offset=40)
            // Phase 3: Partition into register classes (7 classes)
            class = ClassifyRegister(ctx+16, reg_offset)
            // class boundaries: [0], [1], [2], [3], [4], [5], [6+]
            sub_8B81F0(ctx, loop_desc, instr, class,
                       reg_offset - class_base, 1, stage)
```

The 7-class register partitioning (visible in the cascade of comparisons against `ctx+16[0..6]`) maps instruction pipeline slots to hardware register file banks, ensuring the software-pipelined loop body does not exceed any single bank's capacity.

## Hardware Latency Profiles

Per-architecture latency and throughput tables are constructed by a family of functions at 0x8E7300--0x8E9DC0. Each table specifies pipeline latencies (integer, FP32, FP64, tensor, memory), scoreboard wait counts, barrier stall cycles, and dual-issue pair compatibility for the target GPU.

| Address | Architecture | Size |
|---|---|---|
| `sub_8E7300` | sm_70 (Volta) | 3.3 KB |
| `sub_8E7540` | sm_72 | 2.9 KB |
| `sub_8E7720` | sm_75 (Turing) | 3.5 KB |
| `sub_8E7940` | sm_80 base | 2.9 KB |
| `sub_8E7B40` | sm_80 (Ampere) | 3.3 KB |
| `sub_8E7D80` | sm_86 | 4.4 KB |
| `sub_8E8070` | sm_87 | 3.5 KB |
| `sub_8E8280` | sm_89 (Ada Lovelace) | 3.1 KB |
| `sub_8E8480` | sm_90 (Hopper) | 5.2 KB |
| `sub_8E8780` | sm_90a | 4.6 KB |
| `sub_8E8A90` | sm_100 (Blackwell DC) | 3.0 KB |
| `sub_8E8DB0` | sm_103 (Blackwell Ultra) | 1.7 KB |
| `sub_8E9000` | sm_120 (RTX 50xx) | 2.9 KB |
| `sub_8E92E0` | sm_120 extended | 5.5 KB |
| `sub_8E97B0` | Universal fallback | 8.8 KB |

The warp-level hardware profile (`sub_8E4400`) maps architecture IDs to dispatch parameters:

| Architecture range | Warps | Dispatch slots | Era |
|---|---|---|---|
| <= 20479 | 4 | 96 | sm_50 (Maxwell) |
| <= 24575 | 6 | 176 | sm_60 (Pascal) |
| <= 28672 | 7 | 192 | sm_70 (Volta) |
| <= 32767 | 7 | 208 | sm_75 (Turing) |
| <= 36863 | 8 | 224 | sm_80 (Ampere) |
| > 36863 | 16 | 240 | sm_90+ (Hopper, Blackwell) |

Sub-architecture variants (stored at profile offset +26) are assigned by specific SM version codes: 8193, 20481, 24576, 28674--28677, 32768, 36864--36869.

### Representative Per-SM Latency Values

The following table shows representative scheduling latencies extracted from the per-SM dependency rule tables (`per_sm_dependency_rules`). Each row is a scheduling class (unit\_id) corresponding to a key instruction category. Values are the `latency` field -- the scheduler's static cycle cost used for DAG edge weights and stall-count computation. The `tp_inv` column gives the inverse throughput (issue-to-issue delay for back-to-back instructions of the same class); 0 means fully pipelined (one per cycle).

| Instruction class | Sched class | sm\_70 | sm\_80 | sm\_86 | sm\_89 | sm\_90 | sm\_100 | sm\_103 |
|---|---|---|---|---|---|---|---|---|
| **ALU** (IADD3, predicate) | 2 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 |
| **ALU** (IMAD, LOP3, SHF) | 3 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 |
| **FP32** (FFMA, FADD) | 11 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 | 17 / 0 |
| **FP64** (DFMA, DADD) | 4 | 42 / 15 | 42 / 15 | 42 / 15 | 42 / 15 | 42 / 15 | 42 / 15 | 42 / 15 |
| **FP64 pair** (wide DFMA) | 683 | 70 / 31 | 70 / 31 | 70 / 31 | 70 / 31 | 70 / 31 | 70 / 31 | 70 / 31 |
| **Shared mem** (LDS/STS) | 20 | 28 / 5 | 28 / 5 | 28 / 5 | 28 / 5 | 28 / 5 | 28 / 5 | 28 / 5 |
| **Global mem** (LDG/STG) | 22 | 28 / 5 | 28 / 5 | 28 / 5 | 28 / 5 | 28 / 5 | 28 / 5 | 28 / 5 |
| **Texture** (TEX, TLD) | 28 | 74 / 33 | 72 / 34 | 74 / 33 | 74 / 33 | 74 / 33 | 72 / 34 | 74 / 33 |
| **SFU** (MUFU: RCP, RSQ) | 52 | 48 / 19 | 13 / 19 | 48 / 19 | 48 / 19 | 48 / 19 | 46 / 19 | 48 / 19 |
| **Tensor** (HMMA/BMMA) | 13 | -- | -- | -- | -- | -- | 46 / 19 | 46 / 19 |
| **WGMMA** (warpgroup MMA) | 745 | 65 / 28 | -- | -- | -- | -- | 65 / 28 | 65 / 28 |
| **DMMA** (FP64 tensor) | 118 | 49 / 19 | 15 / 19 | 14 / 19 | 15 / 19 | 14 / 19 | 15 / 19 | 15 / 19 |
| **Branch** (BRA, JMP) | 130 | 22 / 2 | 22 / 2 | 22 / 2 | 22 / 2 | 22 / 2 | 22 / 2 | 22 / 2 |
| **Conversion** (I2F, F2I) | 72 | 31 / 12 | 5 / 9 | 31 / 12 | 31 / 12 | 31 / 12 | 31 / 12 | 31 / 12 |
| **Uniform ALU** (UIMAD) | 15 | 255 / 35 | 255 / 35 | 22 / 2 | 22 / 2 | 22 / 2 | 22 / 2 | 22 / 2 |

*Format: latency / tp\_inv. "--" means the class is absent (instruction unsupported on that SM). Latency 255 is the sentinel for "unsupported" -- the scheduler treats it as maximum stall.*

Key observations from the extracted data:

- **Integer and FP32 ALU latencies are constant across all architectures** (17 cycles, fully pipelined). The scheduler treats these as single-cycle-issue, short-latency operations.
- **FP64 latency (42) is ~2.5x FP32 (17)**, with inverse throughput 15 vs 0 -- reflecting the hardware rate limiter on double-precision pipes.
- **Memory latencies (28 cycles) are identical for shared and global memory** in the scheduler's static model. The actual L2/DRAM latency is handled dynamically by the scoreboard; the scheduler uses this as a minimum stall estimate.
- **Texture is the most expensive non-tensor operation** (72--74 cycles), with inverse throughput 33--34 reflecting the deep texture pipeline.
- **sm\_80 shows anomalous low values for SFU (13) and conversion (5)** compared to other SMs. This is the base sm\_80 profile; the extended sm\_80 profile (`sub_8E7B40`) applies corrections.
- **Uniform datapath (class 15) transitions from unsupported (255) on sm\_7x to fully functional (22/2) on sm\_86+**, matching the hardware introduction of the uniform execution unit in Ada Lovelace.
- **Tensor core classes 13/14 only appear in sm\_100+ dependency rules**, reflecting the Blackwell scheduling model's explicit tensor pipe tracking. Earlier SMs use the WGMMA/HMMA scheduling classes (744, 745, 759) instead.
- **WGMMA (class 745) has latency 65 and tp\_inv 28** on architectures that support it (sm\_70 uses this slot for a different purpose; the values are meaningful only on sm\_90+/sm\_100+).

See [Latency Model](latency-model.md) for per-opcode latency tables and functional unit mapping.

## Scheduling Knobs

The scheduler reads approximately 76 knobs. The most significant ones (names decoded from ROT13 in the binary):

| Knob ID | Name | Type | Default | Purpose |
|---|---|---|---|---|
| 313 | `FenceCode` | when-list | -- | Skip scheduling for specific opcodes (per-instruction WHEN condition) |
| 314 | `FenceInterference` | when-list | -- | Mark interference fences for specific opcodes |
| 419 | `LivenessCountRegComp` | int32 | -- | Forward scheduling mode flag (bit 3 in `sched+1376`) |
| 420 | `LivenessUseHiLo` | int32 | -- | Dual-register hi/lo tracking (bit 4 in `sched+1376`) |
| 487 | -- | bool | true | Master scheduling/peephole enable |
| 510 | `OptimizeUniformAtomicMode` | int32 | -- | BB pre-optimization mode for uniform atomics |
| 595 | `PreserveSchedOrderSame` | when-list | -- | Preserve scheduling order (per-instruction WHEN condition) |
| 740 | `SchedBumpScaleAugmentFactor` | double | 0.045 | Register pressure bump scale augmentation coefficient |
| 741 | `SchedCountLoadsPerTex` | int32 | 3 | Load count per texture operation (stall threshold) |
| 742 | `SchedCrossBlock` | int32 | -- | Cross-block scheduling mode |
| 743 | `SchedCrossBlockInstsToSpeculate` | int32 | -- | Cross-block instruction speculation count |
| 747 | `SchedCrossBlockTexToSpeculate` | int32 | -- | Cross-block texture speculation count |
| 750 | `SchedEstimatedLoopIterations` | string | -- | Estimated loop iteration count override |
| 760 | `SchedMaxRLiveCarefulSlack` | int32 | -- | Reserved register headroom (careful slack for live registers) |
| 761 | `SchedMaxRLiveOKslack` | int32 | -- | Acceptable live-register slack (batch depth on non-sm_50) |
| 762 | `SchedMaxRLiveOKslackColdBlocks` | int32 | -- | Extra register slack for cold basic blocks |
| 763 | `SchedMaxRTarget` | int32 | -- | Maximum register target; 0 disables register budget |
| 769 | `SchedPrefFurthestDep` | when-list | -- | Per-BB scheduling query: prefer furthest dependency |
| 770 | `SchedReadAvailTarget` | int32 | 4 | Priority queue depth (read-availability lookahead window) |
| 776 | `SchedReduceIncLimit` | int32 | ~250 | Forward pass primary register increment limit |
| 778 | `SchedReduceIncLimitHigh` | int32 | ~300 | Forward pass secondary (high) register increment limit |
| 805 | `SchedTexBatchTargetSelectRegisterTarget` | int32 | -- | Texture batch register target stall limit (capped at 16) |
| 806 | `SchedTexBatchTargetSelectSchedulerTarget` | int32 | -- | Texture batch scheduler target stall limit (capped at 16) |

Knob names are stored ROT13-encoded in the binary (see [Knobs System](../config/knobs.md) for the obfuscation scheme). Types `when-list` indicate knobs that support per-instruction or per-BB conditional overrides via `WHEN=` syntax.

The full scheduling context configuration is performed by `sub_A95DC0` (35 KB), which reads dozens of knob values and populates the scheduling context structure.

## Data Flow Analysis

The scheduler includes a dedicated data flow analysis subsystem (0x8DBAF0--0x8DF1C0) that computes register liveness and propagates def-use information across BB boundaries:

| Address | Size | Purpose |
|---|---|---|
| `sub_8DB070` | 8.2 KB | Initialize liveness data structures |
| `sub_8DB5F0` | 8.4 KB | Compute per-BB liveness |
| `sub_8DBAF0` | 16 KB | Full liveness analysis |
| `sub_8DC3F0` | 3.0 KB | Compute data flow state |
| `sub_8DC620` | 3.3 KB | Update data flow on schedule |
| `sub_8DC880` | 10 KB | Propagate data flow information |
| `sub_8DCF20` | 23 KB | Build data flow graph for scheduling |
| `sub_8DE7A0` | 12 KB | Iterative data flow solver (fixed-point) |
| `sub_8DEF90` | 2.0 KB | Finalize data flow |

The iterative solver runs until convergence, updating per-BB liveness sets. This information feeds into the priority function's register pressure estimates and into the register budget computation.

## Scheduling Output

After instruction ordering is determined, the scheduling output pipeline (0x8F1EB0--0x8FDD60, ~57 KB) converts the abstract schedule into SASS control words:

```
// Stage 0 -- Per-function entry (sub_8F6530, ~10 KB)
// Manages a circular buffer of 6 barrier slots, one per HW barrier register.
// Each slot: 56-byte record {active:u8, count:u32, instr[2]:ptr, pad}.
function EmitScheduleForFunction(func, bb_index):
    slots[0..5] = {active=0, count=0}        // 6 HW barrier slots
    round_robin = 0                           // next barrier to allocate
    for each BB in func.bb_list[bb_index]:
        instr = BB.first
        while instr != BB.sentinel:
            opcode = instr+72;  nops = instr+80
            if opcode not in {SCHED_BARRIER, NOP_WITH_SCHED}:
                instr = instr.next; continue

            // Classify dest operand to pick barrier slot
            dst = instr + 84 + 8*(nops - ((opcode>>11)&2) - 5)
            dst_type = (dst[0] >> 28) & 7
            if dst_type == 1:                 // register destination
                reg = *(func+88 + 8*(dst[0] & 0xFFFFFF))
                if reg.refcount == 1 && !reg.is_paired:
                    goto fast_path            // single-def: skip full alloc

            // Allocate or recycle a barrier slot (round-robin)
            slot_idx = round_robin = (round_robin + 1) % 6
            slots[slot_idx].active = 0
            slots[slot_idx].count += 1
            slots[slot_idx].instr[count-1] = instr

            // Run the 5-stage pipeline
            ComputeStallCycles    (func, instr, slots, slot_idx)
            ComputeYieldHint      (func, instr)
            AssignBarrier         (func, instr, slots)
            ComputeScoreboardDeps (func, instr, slots, slot_idx)
            EncodeControlWord     (func, instr)
            instr = instr.next
```

```
// Stage 1 -- ComputeStallCycles (sub_8F3130 + sub_A09530)
// Walks source operands, accumulates stall per scoreboard entry.
function ComputeStallCycles(func, instr, slots, slot_idx):
    n_src = instr.operand_count;  n_dest = instr.dest_count
    total = n_src * n_dest
    rec   = func.sched_records + 40 * slot_idx     // 40-byte per-slot record
    for i in 0 .. total-1:
        op_slot = instr+84 + 8*(i / n_dest)
        if ((op_slot[0] >> 28) & 7) != 1:         // not a register ref
            if (instr+72) & 0xCFFF == 0xB7: continue   // NOP: skip
        rec.pending_uses += 1
        if i/n_dest == 0 && (op_slot[last_dest].flags & 1):   // paired reg
            rec.pending_uses += 1                  // double-count for pair
            rec.fp_weight += func.fp_latency_factor

    // sub_A09530: finalize stall count into SchedNode
    sched_node = *(instr+32)
    if (sched_node+13) & 2 == 0: return            // no sched metadata
    stall_bits = 0
    for each source operand with register type:
        reg = *(func+88 + 8*(operand & 0xFFFFFF))
        pipe = QueryPipeClass(func, reg, operand)
        if pipe > 0 && reg.regclass == 6:          // uniform register
            stall_bits += pipe & 0x1FF
    sched_node.stall_field = stall_bits | (sched_node.stall_field & 0xFE00)
    sched_node.stall_field &= ~0x200               // clear dirty bit
```

```
// Stage 2 -- ComputeYieldHint (sub_8F3650 + sub_8F3EA0)
// Yield is set for long-latency ops that consume barrier registers.
function ComputeYieldHint(func, instr):
    opcode = instr+72;  nops = instr+80
    dest_idx = 2*(nops - ((opcode>>11)&2) - 4)
    dest = (instr+84) + dest_idx
    if (dest[0] ^ 0x70000000) & 0x70000000 != 0:
        return false                               // not a standard dest

    // Clamp yield threshold from architecture profile (set by sub_8F3AB0)
    yield_max = func.yield_threshold               // ctx+7
    if dest.flags & 1:                             // paired register
        cap = *(*(*(func+88) + 8*(dest[0] & 0xFFFFFF)) + 73)
        yield_max = min(yield_max, cap)
    else:
        yield_max = min(yield_max, 4)
    if yield_max <= 2: return false

    masked = opcode & 0xFFFFCFFF
    if masked == 288:                              // TEX/TLD/TXQ family
        part_id = arch_backend.GetPipePartition(instr.modifier)  // vtable+904
        sub_part = 0
        if arch_backend.vtable[936] != sub_7D7040:
            sub_part = arch_backend.GetSubPartition(instr) / part_id
        // Mark barrier usage bitmap (4-slot mod ring)
        func.barrier_usage[sub_part & 3] = 1
        for k in 1 .. (dest.reg_count & 7):
            func.barrier_usage[(sub_part + k) & 3] = 1
        if dest.flags & 1:
            func.barrier_lo = 0
            func.barrier_hi = func.barrier_budget / 4
        else:
            func.barrier_lo = min(func.barrier_lo, sub_part)
            func.barrier_hi = max(func.barrier_hi, sub_part + reg_count + 1)
        return true
    return (dest.last_word & 7) == 0               // yield only if no read deps
```

```
// Stage 3 -- AssignBarrier (sub_8F31F0) + ScoreboardDeps (sub_8F3860)
// Allocates HW barrier, then encodes scoreboard tag into operand descriptor.
function AssignBarrier(func, instr, slots):
    rdesc = *(func.reg_descriptors + 8*(instr+84 & 0xFFFFFF))
    if rdesc.flags & 0x20: return                  // no-barrier flag
    if rdesc.refcount <= 1: return                 // single use

    alias = rdesc.alias_chain                      // linked list at +112
    if alias == NULL:
        rdesc.refcount = 1
        new = SplitRegDescriptor(rdesc, func)      // sub_7E5350
        func.split_ctx = new;  func.split_bb = instr.bb_id
        CommitBarrierSplit(func, instr, 0)         // sub_932720
        FlushSplitCtx(func)                        // sub_7E5FA0
        rdesc.flags &= ~0x2000000                  // clear pending-split
        rdesc.alias_chain = NULL;  return

    // Walk alias chain -- verify all aliases share same BB
    count = 0;  bb = *(*(rdesc.def_instr) + 24)
    for node in alias:
        if node.instr.bb_id != bb: break
        count += 1
    rdesc.refcount = count;  rdesc.alias_chain = NULL

function ComputeScoreboardDeps(func, instr, slots, slot_idx):
    opcode = instr+72;  nops = instr+80
    src = instr+84 + 8*(nops - ((opcode>>11)&2) - 5)
    src_type = (*src >> 28) & 7
    rdesc = (src_type == 5)
          ? *(func.reg_descriptors + 8*(*src & 0xFFFFF))
          : *(func.reg_descriptors + 8*(src[1] & 0xFFFFF))

    part_id = arch_backend.GetPipePartition(instr.modifier)
    sub_part = arch_backend.HasSubPartition()
             ? arch_backend.GetSubPartition(instr) : 0

    base = sub_part - 4 * func.barrier_lo
    sb = ((base & ~slot_idx) >> slot_idx)
       + slots[(base & slot_idx) / part_id + 3]
       + (base & slot_idx)

    if !(src.flags & 1):                           // not paired
        cfg = LookupScoreboardConfig(func, rdesc, sb)  // sub_926780
        if !(src[1] & 0x1000000):
            *src = (cfg | (*src & 0xFFF00000)) & 0xFFCFFFFF
        else:
            src[1] = cfg | (src[1] & 0xFFF00000)
    else:                                          // paired register
        func.current_instr = instr
        adj = sb - rdesc.latency_offset
        EncodePairedScoreboard(slots, src, func, adj)  // sub_7DF0D0

    // Clear wait-mask field of last operand slot
    dep = instr+84 + 8*(nops - ((opcode>>11)&2) - 2)
    *dep &= 0xFF000000                             // zero bits [23:0]
```

```
// Stage 4 -- EncodeControlWord (sub_8F4140)
// Reuse-flag eligibility check + 23-bit SASS control word packing.
function EncodeControlWord(func, instr):
    if IsSchedulingFence(instr, func): return
    if GetInstrFlags(instr, func) & 2:  return     // no-encode flag

    opcode = instr+72;  nops = instr+80
    // Skip writeback-dependency opcodes
    if nops - ((opcode>>11)&2) > 1:
        tt = (*(instr+84+8*(nops-((opcode>>11)&2)-1)) >> 28) & 7
        if tt == 6:
            m = opcode & 0xFFFFCFFF
            if m in {190, 95, 96, 27, 29}: return

    if GetInstrFlags(instr,func) & 0x10: return    // vector op
    if IsVectorOp(instr, func):          return
    m = opcode & 0xFFFFCFFF
    if m in {288, 183}:
        if LookupScbClass(rdesc) == 18: return     // class 18: no reuse
    if IsNoReuse(instr,func) || (GetInstrFlags(instr,func) & 8): return

    // 330-entry opcode dispatch (0x21D9EF8):
    //   guard: cmp opcode, 0x149; ja default (0x8A2119)
    //   206 specialized, 124 default (no-reuse)
    reuse = OpcodeDispatchTable[m](func, instr)
    if !reuse: return
    if m in {304..322 & 0x401C5, 123, 118, 43}: return
    if m in {236, 32, 159, 271, 109, 46} || nops <= 1: return

    // Validate each source: reg type 1, valid descriptor, not uniform
    for i in 0 .. nops-1:
        op = *(instr+84+8*i);  hi = *(instr+88+8*i)
        if ((op>>28)&7)==1 && !(hi&0x1000000):
            reg = *(*(func+88)+8*(op&0xFFFFFF))
            if reg.regclass==4: return 0           // uniform: no reuse
            if reg.refcount>1 || !reg.def_instr: return 0
            if reg.hw_size<=45 || (reg.regclass-5)>1: return 0

    // Pack 23-bit control word into instr+196
    //   [3:0]   stall  (4 bits, 0--15)
    //   [4]     yield  (1 bit)
    //   [7:5]   wr_bar (3 bits, 0--5; 7=none)
    //   [13:8]  rd_mask (6 bits, one-hot per barrier)
    //   [19:14] wt_mask (6 bits, one-hot per barrier)
    //   [22:20] reuse  (3 bits)
    instr.ctrl = (stall & 0xF)
               | (yield << 4)
               | ((wr_bar & 7) << 5)
               | ((rd_mask & 0x3F) << 8)
               | ((wt_mask & 0x3F) << 14)
               | ((reuse & 7) << 20)
```

Key encoding functions:

| Address | Size | Purpose |
|---|---|---|
| `sub_8F6530` | 10 KB | Stage 0: per-function entry, circular barrier buffer, BB iteration |
| `sub_8F3130` | 0.2 KB | Stage 1: accumulate stall contribution per source operand |
| `sub_A09530` | 0.4 KB | Stage 1 (cont.): finalize stall count into SchedNode+12 |
| `sub_8F3650` | 0.4 KB | Stage 2: yield-hint decision from dest type and pipe partition |
| `sub_8F3AB0` | 0.8 KB | Stage 2 setup: calibrate yield threshold and barrier budget (knob 487) |
| `sub_8F3EA0` | 0.4 KB | Stage 2 gate: skip yield if function too small or -O0 |
| `sub_8F31F0` | 1.3 KB | Stage 3: barrier allocation via alias-chain walk and descriptor split |
| `sub_8F3860` | 0.7 KB | Stage 3 (cont.): encode scoreboard dependency tag into operand |
| `sub_8F4140` | 1.0 KB | Stage 4: reuse-flag eligibility and 23-bit control word packing |
| `sub_8F1EB0` | 2.0 KB | Constructor: allocate 99-slot knob array (7128 B) + barrier index array |
| `sub_8F2FD0` | 0.4 KB | Reverse operand walk: clear stale refcounts before barrier assignment |

The 330-entry opcode dispatch table at `0x21D9EF8` (extracted in `sched_encoder_dispatch.json`) routes the reuse-eligibility check in Stage 4. Of the 330 entries, 206 have specialized handlers and 124 fall through to the default (no-reuse) handler at `0x8A2119`. The guard instruction `cmp r15d, 0x149; ja default` bounds the table to opcodes 0--329.

Seven verification functions at 0x8F7610--0x8F8CB0 validate the generated schedule: stall counts, barrier assignments, dependency chains, scoreboard correctness, control word format, yield hints, and overall schedule integrity.

See [Scoreboards](scoreboards.md) for the scoreboard and dependency barrier encoding format.

## Memory Management

The scheduler uses two allocator strategies:

1. **Arena allocator** (`sub_8E3970`): bump allocator with 10 KB block granularity, 8-byte alignment. Allocations within a scheduling pass use the arena for fast allocation. `sub_8E3A80` frees all blocks at once at pass completion.

2. **Free-list allocator** (`sub_8DA6D0`): free-list with block coalescing for persistent scheduling data. Maintains multiple free lists for different size classes. Blocks larger than 0x1FF bytes go to a separate large-block list. Adjacent free blocks are merged on deallocation.

## Per-Instruction Scheduling Metadata (SchedNode)

Each instruction has a pointer at `instr+40` (`sched_slot`) to a separate heap-allocated scheduling metadata block called a SchedNode. The metadata offsets documented throughout the scheduling pages (e.g., `metadata+24`, `metadata+32`, `metadata+108`) are relative to this SchedNode, **not** to the 296-byte Ori instruction object itself. The SchedNode block is at least 240 bytes (the cross-block scheduling loop accesses fields up to +236); all nodes are linked into a singly-linked list at `func+104` (Code Object offset +104), separate from the instruction linked list at `func+272`.

### SchedNode Layout

| Offset | Size | Type | Init | Name | Purpose |
|---|---|---|---|---|---|
| +0 | 8 | `ptr` | -- | `nextInList` | Singly-linked next pointer for the `func+104` metadata chain |
| +8 | 4 | `i32` | 0 | `depCount` | Unsatisfied dependency count; decremented as predecessors are scheduled; instruction is ready when this reaches 0 |
| +12 | 4 | -- | -- | (pad) | Alignment padding |
| +16 | 8 | `ptr` | -- | `nextReady` | Ready list singly-linked next pointer; threaded by `sub_6820B0` (BuildReadyList) |
| +24 | 4 | `i32` | seq | `bbSlot` | 1-based position within the BB (assigned sequentially by `sub_8D9930`); used for program-order tiebreaking in priority decisions |
| +28 | 4 | `i32` | 0 | `latencyCounter` | Remaining latency cycles until the instruction's result is available; reset to 0 when placed on the ready list; updated by `sub_A09530` (UpdateStallCycles) |
| +32 | 4 | `i32` | -- | `earliestCycle` | Earliest available cycle -- the latest completion time among all producer instructions; stall-free when `earliestCycle >= scheduler+480` (current cycle) |
| +36 | 4 | -- | -- | (reserved) | Alignment padding or internal use |
| +40 | 4 | `i32` | 0 | `latestDeadline` | Latest deadline cycle for scheduling; secondary tiebreaker in the candidate comparison cascade |
| +44 | 4 | `i32` | -- | `barrierGroupIndex` | Barrier group assignment; identifies which of the 6 hardware barriers this instruction participates in |
| +48 | 4 | `i32` | -- | `schedulingFenceCode` | Scheduling fence code from knob 313 (`FenceCode`) / 314 (`FenceInterference`) checks; controls per-instruction scheduling boundaries |
| +56 | 8 | `i64` | 0 | `depChainHead` | Dependency chain data; reset to 0 between scheduling passes |
| +76 | 4 | `i32` | 0 | `schedulingCost` | Per-instruction scheduling cost; accumulated during priority evaluation; reset between passes |
| +84 | 4 | `i32` | -1 | `schedulingClass` | Scheduling class index assigned by the latency model (`sub_89FBA0`); indexes into per-architecture latency tables; -1 = unclassified (sentinel) |
| +88 | 4 | `i32` | -- | `maxPredecessorCycle` | Highest cycle value among predecessor instructions; used in the priority pre-scan to compute `max_pred_cycle` |
| +92 | 4 | `i32` | -- | `maxDependencyCycle` | Highest cycle value along the dependency chain; used to compute `max_dep_cycle` for critical-path analysis |
| +104 | 8 | `i64` | 0 | `extendedState` | Extended scheduling state; reset to 0 between scheduling passes |
| +108 | 1 | `byte` | -- | `flags` | Primary flag byte: bit 0 = barrier-target, bit 1 = has-dependency-set, bit 2 = fence-early (knob 314), bit 3 = fence-late (knob 313), bit 4 = has-register-operand |
| +111 | 1 | `byte` | -- | `extendedFlags` | Extended flags: bit 7 = uses expensive register file (triggers barrier tracking update in `sub_8C7120`) |
| +128 | 8 | `ptr` | 0 | `regionChainNext` | Cross-block region chain next pointer; walked by `sub_68B9C0` to iterate BB-representative nodes; separate from the `func+104` chain at +0 |
| +144 | 4 | `i32` | -- | `schedRegionIndex` | Index into the 72-byte per-block scheduling record array (`scheduler+184`); also used as FNV-1a hash key in the region dedup cache |
| +164 | 4 | `i32` | -- | `resourceClassIndex` | Index into the 40-byte resource-class record array; `sub_688DD0` uses `src + 40 * index` to look up the 10-element register-delta vector |
| +236 | 4 | `i32` | -- | `regionOrderWeight` | Region ordering weight for cross-block BB traversal; sentinels `INT_MIN`/`INT_MAX` mark region boundaries |

### Relationship to the Instruction Object

```
 Ori Instruction (296 bytes)              SchedNode (>= 240 bytes)
 +--------------------------+             +---------------------------+
 | +0:  prev (BB list)      |   instr+40  | +0:  nextInList           |
 | +8:  next (BB list)      |---sched_slot-->                         |
 | +16: id                  |             | +8:  depCount             |
 | +72: opcode              |             | +16: nextReady            |
 | +80: operand_count       |             | +24: bbSlot               |
 | +84: operands[]          |             | +28: latencyCounter       |
 |                          |             | +32: earliestCycle        |
 |                          |             | +40: latestDeadline       |
 |                          |             | +88: maxPredecessorCycle  |
 |                          |             | +92: maxDependencyCycle   |
 |                          |             | +108: flags               |
 |                          |             | +128: regionChainNext     |
 |                          |             | +144: schedRegionIndex    |
 |                          |             | +164: resourceClassIndex  |
 |                          |             | +236: regionOrderWeight   |
 +--------------------------+             +---------------------------+
```

### Lifecycle

1. **Allocation**: `InitScheduleData` (vtable[29], called from `sub_8D0640`) allocates one SchedNode per instruction from the scheduling arena and stores the pointer at `instr+40`. Nodes are linked into the `func+104` chain.

2. **Initialization**: `sub_8D9930` (EdgeBuilder) initializes `depCount`, `bbSlot`, `latencyCounter`, `latestDeadline`, and `flags` while building dependency edges. Between scheduling phases, the orchestrator resets pass-specific fields: `+56 = 0`, `+104 = 0`, `DWORD+76 = 0`, `DWORD+84 = -1`.

3. **Population**: The dependency graph builder populates `depCount` from edge analysis. Critical-path computation fills `earliestCycle`, `maxPredecessorCycle`, and `maxDependencyCycle`.

4. **Use**: `sub_6820B0` (BuildReadyList) checks `depCount == 0` and threads ready instructions via `nextReady`. `sub_8C9320` (PriorityFunction) reads all fields to compute the 8-bit scheduling priority.

5. **Cleanup**: `sub_8E3A80` (ArenaFreeAll) reclaims all SchedNode blocks when the scheduling pass completes.

### Sentinel Values

- `bbSlot = -1`: unscheduled (set during inter-pass reset at `DWORD+84`)
- `latencyCounter = 99999` (`0x1869F`): infinity (used as `min_barrier_latency` initial value in the priority pre-scan)
- `earliestCycle` bit 31 set (`>= 0x80000000`): not-yet-available (tested in `sub_8C9320` pre-scan via `< 0x80000000` comparison)

## Large Function Handling

Functions exceeding 16383 instructions (`*(a1+372) > 0x3FFF`) trigger chunk-based scheduling via `sub_A9DDD0` (11.5 KB). The function is split into chunks that are scheduled independently and then merged. This avoids quadratic blowup in the dependency DAG construction for very large kernels.

## Per-Block Scheduling Record (72 bytes)

`sub_6833F0` (InitScheduleRegion, 10 KB) allocates an array of `(numBBs + 1)` records at 72 bytes each, stored at `scheduler+184`. Each record tracks the register pressure snapshot, region context pointers, and scheduling characteristic flags for a single basic block. The scheduling engine loads a BB's pressure state from this record at region entry and saves it back when moving to the next BB.

### Field Map

| Offset | Size | Type | Init | Name | Purpose |
|---|---|---|---|---|---|
| +0 | 4 | `i32` | 0 | `crossBlockId` | Non-zero when the BB is active/scheduled; set to the predecessor BB index during cross-block merging. Tested as a boolean gate by 8+ functions before processing a BB. |
| +4 | 4 | `i32` | 0 | `pressure[0]` | Register pressure snapshot -- R (general-purpose 32-bit registers) |
| +8 | 4 | `i32` | 0 | `pressure[1]` | Register pressure snapshot -- P (predicate registers) |
| +12 | 4 | `i32` | 0 | `pressure[2]` | Register pressure snapshot -- UR (uniform registers) |
| +16 | 4 | `i32` | 0 | `pressure[3]` | Register pressure snapshot -- UP (uniform predicate registers) |
| +20 | 4 | `i32` | 0 | `pressure[4]` | Register pressure snapshot -- B (barrier registers) |
| +24 | 4 | `i32` | 0 | `pressure[5]` | Register pressure snapshot -- arch-specific class 0 |
| +28 | 4 | `i32` | 0 | `pressure[6]` | Register pressure snapshot -- arch-specific class 1 |
| +32 | 4 | `i32` | 0 | `pressure[7]` | Register pressure snapshot -- arch-specific class 2 |
| +36 | 4 | `i32` | 0 | `pressure[8]` | Register pressure snapshot -- arch-specific class 3 |
| +40 | 4 | `i32` | 0 | `pressure[9]` | Register pressure snapshot -- arch-specific class 4 / control total |
| +44 | 4 | -- | -- | (padding) | Not initialized, not accessed |
| +48 | 8 | `ptr` | 0 | `regionContext` | Pointer to 136-byte per-region scheduling state allocated by `sub_682F10`. Contains region boundaries, mode flags, and instruction range metadata. |
| +56 | 8 | `ptr` | 0 | `regionContext2` | Second region context pointer, written via successor-BB index mapping. Dereferenced by `sub_681C00` to check barrier presence (bit 4 of pointed-to byte). |
| +64 | 1 | `byte` | `& 0x80` | `flags` | Per-BB characteristic flags (see below). Low 7 bits cleared on init; bit 7 preserved. |
| +65 | 7 | -- | -- | (padding) | Padding to 72-byte stride |

### Pressure Counter Transfer

At the start of each BB's scheduling pass, `sub_A091C0` (InitResourceTracking) copies the 10 DWORDs at record offsets +4 through +40 into the scheduler context at context offsets +48 through +87. The scheduling engine then updates the context counters as instructions are scheduled. When cross-block scheduling produces a new pressure snapshot, the engine writes it back with SSE bulk stores:

```
*(OWORD*)(record + 4)  = pressure[0..3]    // 16 bytes via _mm_store_si128
*(OWORD*)(record + 20) = pressure[4..7]    // 16 bytes via _mm_store_si128
*(QWORD*)(record + 36) = pressure[8..9]    // 8 bytes
```

During the main scheduling loop, the engine decrements `pressure[1]` through `pressure[9]` (9 counters) from a 40-byte per-opcode resource cost table. `pressure[0]` (R class) is handled via a separate path.

### Flags Byte (+64)

| Bit | Name | Set by | Meaning |
|---|---|---|---|
| 0 | `crossBlockBoundary` | `sub_688DD0` (ScheduleEngine) | BB is a cross-block scheduling boundary |
| 1 | `regionActive` | `sub_688DD0` (ScheduleEngine) | BB belongs to an active scheduling region |
| 2 | `hasCall` | `sub_6833F0` for opcode 96 | BB contains a CALL instruction |
| 3 | `hasBranch` | `sub_6833F0` for opcodes 188, 190 | BB contains a branch instruction |
| 4 | `hasBarrierInstr` | `sub_6833F0` via `sub_7DF3A0` test (bit 6) | BB contains a barrier-flagged instruction |
| 5 | `hasLongLatencyOp` | `sub_6833F0` for memory/texture/tensor opcodes; also vtable[183] arch check | BB contains a long-latency operation (memory, texture, or tensor) |
| 6 | `crossBlockTarget` | `sub_6833F0` cross-block merge | BB is the target of a cross-block scheduling region |
| 7 | (preserved) | Not cleared during init | Carries data from a prior pipeline stage; purpose unknown |

The opcodes that set bit 5 (`hasLongLatencyOp`): 18 (with knob 62 gate), 23, 26, 32, 57, 81, 101, 124 (with knob 461 gate), 178, 188, 190, 197, 236, 248, 271, 315. Additionally, any instruction where vtable[183] returns true (architecture-specific long-latency classification) sets bit 5.

### Cross-Block Scheduling Setup

After per-BB initialization, `sub_6833F0` walks the CFG to identify cross-block scheduling opportunities, with the master gate being knob 744 (`SchedCrossBlockLimit`): when its boolean form is true the integer value supplies the speculative distance threshold; when disabled the default threshold is 2. (The per-BB walk also stores `sched+177` from knob 742 (`SchedCrossBlock`): byte 0 = enabled, byte 1 = conditional on `options+53648 != 0`, byte >= 2 = disabled.)

```
// Phase 1: compute speculative distance threshold (sub_6833F0, LABEL_49 block)
crossblock_enabled = ReadKnobBool(744)              // options+53568
if crossblock_enabled:
    spec_dist = ReadKnobInt(744)                     // options+53576
else:
    spec_dist = 2                                    // hardcoded default

// Phase 2: CFG walk — identify cross-block pairs (lines 296-394)
for each bb in RPO_order(func):                      // bb_array[ctx+296]
    pred_rpo = bb.rpo_index                          // *(bb+144)
    existing_id = record[pred_rpo].crossBlockId
    if existing_id == 0:
        existing_id = bb.rpo_index                   // use own index as fallback

    // Select best predecessor: walk bb.predecessor_list (bb+136),
    // pick the predecessor with the highest RPO index
    best_pred = first_predecessor(bb)
    max_rpo   = best_pred.rpo_index
    for each pred in bb.predecessors:
        if pred.rpo_index > max_rpo:
            max_rpo   = pred.rpo_index
            best_pred = pred

    // Gate 1: predecessor must not have a branch (BB +280 bit 3)
    if best_pred.bb_flags & 0x8:
        continue
    // Gate 2: forward edge only
    if pred_rpo >= best_pred.rpo_index:
        continue

    // Gate 3: eligibility — sub_682D40 (CrossBlockEligible)
    if not CrossBlockEligible(sched, bb, best_pred):
        continue

    // Gate 4: speculative distance check
    succ_rpo = best_pred.rpo_index
    if spec_dist + pred_rpo + 1 < succ_rpo:
        // Base distance exceeded; count texture blocks for extension
        tex_count = 0
        for idx in range(pred_rpo, succ_rpo):
            if IsTextureBlock(bb_by_rpo[idx]):       // sub_7E5120
                tex_count += 1
        if tex_count == 0 or spec_dist < tex_count
           or tex_count + spec_dist + pred_rpo + 1 < succ_rpo:
            continue

    // Commit the cross-block pair
    record[pred_rpo].crossBlockId  = existing_id
    record[pred_rpo].flags        &= ~0x40           // clear crossBlockTarget
    record[succ_rpo].flags        |=  0x40           // set crossBlockTarget
    record[succ_rpo].crossBlockId  = existing_id
    AllocRegionContext(sched, bb, best_pred)          // sub_682F10
```

**`CrossBlockEligible`** (`sub_682D40`). Returns false if `vtable[23]` (arch override, default `sub_661250` = always-eligible) vetoes the pair. Otherwise walks forward from `bb` toward `best_pred`, checking at each intermediate BB: (a) no `hasLongLatencyOp` (bit 5 of `record[rpo].flags`), (b) all predecessor RPOs lie strictly between the pair's RPO bounds, (c) all successor RPOs lie strictly within bounds. Any violation returns false.

**`IsTextureBlock`** (`sub_7E5120`). Returns true when a BB contains texture/surface instructions. Tests four conditions (short-circuit OR): (1) arch-specific classifier via double vtable deref at `ctx+1784`, (2) per-BB scheduling class table at `ctx+1776`, (3) `*(instr+283) & 1`, (4) fallback `sub_7A1A90(ctx+1664, 91, instr)`.

**`AllocRegionContext`** (`sub_682F10`). Allocates 136 bytes from the scheduling arena:

| Offset | Size | Init | Name | Purpose |
|---|---|---|---|---|
| +0 | 1 | composed | `mode` | Bit 0 = pred `hasFallthrough`; bit 1 = pred `hasCall OR hasBranch`; bit 4 = succ `hasBarrierInstr` |
| +4 | 4 | 1 | `active` | Always 1 on allocation |
| +8 | 4 | pred_rpo+1 | `startBB` | First BB RPO index in the cross-block region |
| +12 | 4 | succ_rpo-1 | `endBB` | Last BB RPO index in the region |
| +16 | 4 | 0 | `instrCount` | Accumulated during per-BB instruction walk |
| +20..+52 | 36 | 0 | `counters[3][3]` | 3 classes x 3 counts: total, src-reads, dst-writes |
| +56..+79 | 24 | 0 | `latencyBuckets` | Resource latency accumulators |
| +80 | 4 | 0 | `maxLatency` | Max single-instruction latency in region |
| +96..+127 | 32 | 0 | `resourceVec` | SSE-zeroed resource vector (two OWORDs) |
| +120 | 4 | tail_lat | `succTailLatency` | Last-instruction latency of successor BB |
| +128 | 8 | 0 | (reserved) | |

Pointers stored at `record[pred_rpo].regionContext` (+48) and `record[succ_rpo].regionContext2` (+56). The instruction scan inside `sub_682F10` classifies each instruction into barrier-class (bit 6 of `sub_7DF3A0`), arch long-latency (`vtable[228]`), or default, accumulating the counter rows. For branch variants (`(opcode & 0xFFFFCFFD) == 0xBC`), it checks operand encoding to set bit 3 and record branch stall cost at `+4`.

```
+0                  +4                                           +44  +48              +56              +64  +72
| crossBlockId (4B) | pressure[0..9] (40B = 10 x i32)           |pad | regionCtx (8B) | regionCtx2 (8B)| fl | pad  |
+-------------------+----+----+----+----+----+----+----+----+----+----+----------------+----------------+----+------+
```

## Scheduler Context Object Layout

The scheduling context object (`sched` / `a1`) is the central state structure passed as the first argument to every function in the scheduling subsystem. It is populated by `sub_A95DC0` (SchedulingContext::configure, 35 KB) which reads dozens of knob values and architecture parameters. The object spans approximately 1600 bytes, from a vtable pointer at offset 0 through architecture-specific SSE vectors at offset +1584.

### Core Fields (offsets 0--176)

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +0 | 8 | `void*` | vtable | Polymorphic dispatch; pre/post scheduling hooks at `*(a1+40)`, `*(a1+48)` |
| +8 | 8 | `ptr` | funcContext | Pointer to CompilationContext; all func/arch queries go through this |
| +16 | 8 | `ptr` | allocator | Memory allocator interface (vtable-dispatched alloc/free) |
| +40 | 8 | `ptr` | preHookVtable | Pre-scheduling callback (mode-specific polymorphic hook) |
| +48 | 40 | `int32[10]` | regPressureCounters | Per-register-class live counts (copied from per-BB record +4..+40): R, P, UR, UP, B, and 5 arch-specific. The engine decrements counters [1]..[9] in the scheduling loop; counter [0] (R class) uses a separate path. |
| +60 | 4 | `int32` | mode | Scheduling mode: 0 = ILP/Latency, 1 = ReduceReg, 2 = DynBatch |
| +88 | 4 | `int32` | maxBBDepth | Maximum dependency depth across all basic blocks |
| +92 | 4 | `int32` | maxBBDepthNonTensor | Maximum depth excluding tensor instructions |
| +176 | 1 | `byte` | scheduleActive | 1 during ReduceReg and DynBatch phases, 0 during ILP/Latency |
| +178 | 1 | `byte` | reduceRegMode | When set, tightens register budget by ~12.5% + 3 |

### Phase Control (offsets 240--312)

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +240 | 4 | `int32` | currentPhase | Phase ID: 0 = budget computation, 1 = ReduceReg, 2 = ILP |
| +248 | 8 | `ptr` | regBitvector1 | Register pressure bitvector (`numRegs + 1` words) |
| +256 | 8 | `ptr` | regBitvector2 | Second bitvector for dual-register tracking (knob 420, `LivenessUseHiLo`) |
| +280 | 8 | `ptr` | analysisSlots | 48-byte per-BB analysis slots (allocated when `opt_level > 2`) |
| +292 | 1 | `byte` | regTargetValid | Whether register targets from knobs 776/778 (`SchedReduceIncLimit`/`SchedReduceIncLimitHigh`) are valid |
| +296 | 4 | `int32` | regTargetPrimary | Forward-pass primary register target (knob 776 `SchedReduceIncLimit`, in HW register units) |
| +300 | 4 | `int32` | regTargetSecondary | Forward-pass secondary register target (knob 778 `SchedReduceIncLimitHigh`, in HW register units) |
| +311 | 1 | `byte` | cfgFlag1 | Priority queue depth configuration flag |
| +312 | 4 | `int32` | cfgParam1 | Configuration parameter (default 10) |

### Register Budget (offsets 316--432)

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +316 | 4 | `int32` | minRegs | Minimum register count from architecture register limit |
| +320 | 4 | `int32` | pressureSlack | Register pressure headroom (initialized to 0) |
| +324 | 4 | `int32` | committedTarget | Committed register target (set to `regBudget` after budget computation) |
| +328 | 4 | `int32` | dualIssueBenefit | Dual-issue benefit score from `sub_8CF5D0` (sm_50 only) |
| +380 | 4 | `int32` | latencyCutoff | Barrier-target latency cutoff; controls critical-path bit activation |
| +384 | 1 | `byte` | hasTextureOps | Set to 1 when opcode 246 (texture operation) found in any BB |
| +388 | 4 | `int32` | maxBBSize | Maximum basic block size in instructions (capped at 4095) |
| +392 | 4 | `int32` | maxBBSizeForAlloc | Copy of `maxBBSize` used for resource slot allocation sizing |

### Stall / Batch Parameters (offsets 404--420)

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +404 | 4 | `int32` | maxStallCycles | Max stall cycles; from knob 805/806 (`SchedTexBatchTargetSelect{Register,Scheduler}Target`), capped at 16 |
| +408 | 4 | `int32` | stallThreshold | Stall threshold; knob 741 (`SchedCountLoadsPerTex`), default 3 |
| +412 | 4 | `int32` | batchDepth | Batch depth; knob 761 (`SchedMaxRLiveOKslack`), default 3 (6 or 12 for sm_50 with dual-issue) |
| +416 | 4 | `int32` | extraRegReserve | Extra register reservation; knob 762 (`SchedMaxRLiveOKslackColdBlocks`), default -1 (disabled) |
| +420 | 4 | `int32` | spillModeCountdown | Spill-mode countdown; when > 0, forces aggressive scheduling with critical-path bit always set |

### Register Budget and Pressure Tracking (offsets 432--485)

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +432 | 4 | `int32` | regBudget | Target register count (occupancy-aware, from `sub_8CEE80`) |
| +440 | 8 | `ptr` | livenessBV.data | Register liveness bitvector data (via `sub_BDBA60`); sized to `numRegs+1` or `2*numRegs+2` if dual-reg |
| +448 | 8 | `ptr` | livenessBV.alloc | Bitvector allocator reference |
| +456 | 4 | `int32` | livenessBV.size | Bitvector size in 64-bit words |
| +464 | 4 | `int32` | depthThreshold | Number of barrier-target instructions required to activate critical-path bit |
| +480 | 4 | `int32` | currentCycle | Current scheduling cycle; used for stall-free evaluation |
| +484 | 1 | `byte` | phaseActive | Phase activity flag: 1 = ReduceReg active, 0 = ILP/budget |
| +485 | 1 | `byte` | schedDirty | Reset to 0 at orchestrator start |

### Hot-Cold and Yield State (offsets 523--532)

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +523 | 1 | `byte` | hotColdEnable | Hot-cold memory tracking enable; result of `sub_8CF5D0` (dual-issue check) |
| +524 | 1 | `byte` | yieldState | Current yield state; propagated to CONTROL instructions via priority bit 6 |
| +532 | 4 | `int32` | hotColdBudget | Hot-cold budget counter; decremented per cold instruction; tracking deactivates at zero |

### Architecture Parameters (offsets 604--616)

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +604 | 4 | `int32` | archParam1 | Architecture-dependent parameter (6 for sm_60 era) |
| +616 | 4 | `int32` | archParam2 | Architecture-dependent limit (63 for sm_50 era, 255 for sm_60+) |

### Resource Tracking and Dependency Data (offsets 672--744)

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +672 | 8 | `ptr` | resourceSlots | Per-BB resource cost table; 84 bytes per slot (21 DWORDs: 10 FU usage + 10 FU delta + 1 flag) |
| +680 | 8 | `ptr` | depData | Dependency tracking data (zeroed at orchestrator start) |
| +720 | 8 | `ptr` | arenaAllocRef | Arena allocator reference for bitvector buffer resizing |
| +728 | 8 | `ptr` | bvBuffer | Growable bitvector buffer pointer (1.5x growth factor on realloc) |
| +736 | 4 | `int32` | bvCapacity | Bitvector capacity in words (-1 = uninitialized sentinel) |
| +740 | 4 | `int32` | bvAllocated | Bitvector allocated word count |
| +744 | 8 | `ptr` | funcContextRef2 | Second reference to function context for bitvector sizing |

### Liveness Bitvector (offset 832)

The scheduler tracks register liveness via a bitvector at offset +832 (referenced only in the scheduling algorithm). Each bit represents one register; pressure is computed as `popcount(live_bv)`. This field is part of the larger scheduling state managed by the engine and priority function.

### Arena Allocator (offset 840+)

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +840 | ~120 | `ArenaAllocator` | arena | Embedded bump allocator; freed via `sub_8E3A80(sched+840)` at each pass end; 10 KB block granularity, 8-byte alignment |

### Configuration Bitfields (offsets 1032--1098)

The region from +1032 through +1098 (~67 bytes) is a dense bitfield array set by `sub_A95DC0` (SchedulingContext::configure). Individual bits control fine-grained scheduling features, gated by architecture version, optimization level, and knob queries. Key fields:

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +1032 | 1 | `byte` | featureFlags0 | Pipeline feature enables (OR'd with 0x4F) |
| +1052 | 4 | `int32` | cfgMaxDepth | Knob 449 value (default 5); scheduling depth limit |
| +1064 | 1 | `byte` | cfgSmFlags | Bit 0: SM-specific flag (knob 931 or arch > 16386) |
| +1072 | 8 | `double` | pressureCoeff | Knob 366 value (default 0.25); register pressure coefficient |
| +1080 | 1 | `byte` | cfgBitmask | Bits: [7] always set, [6] knob 868, [5] hot-cold, [4] knob 410, [3] knob 868 alt |
| +1084 | 4 | `int32` | cfgThreshold | Knob 876 value (default 50) |
| +1088 | 1 | `byte` | cfgBitmask2 | Bit 3: knob 752 related |
| +1089 | 1 | `byte` | cfgBitmask3 | Bit 7: arch == 16387 or arch == 0x4000 |
| +1096 | 1 | `byte` | cfgBitmask4 | Bit 7: external flag from target descriptor +788 |
| +1097 | 1 | `byte` | cfgBitmask5 | Bits: [7] target+1844, [4] arch <= 16386, [3] sm_50 dual-issue, [1,0] target+788 |
| +1098 | 1 | `byte` | cfgBitmask6 | Bit 0: knob 462 (scheduling heuristic), Bit 5: arch == 16386 |

### Architecture-Specific Defaults (offsets 1408--1584)

Set early in `sub_A95DC0` based on `*(a1+372) >> 12` (architecture class). Three code paths populate these fields for sm_50 era (class < 3), sm_60--sm_89 era (class == 4), and sm_90+ era (class >= 5):

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +1408 | 1 | `byte` | archMode0 | Architecture scheduling mode flag |
| +1411 | 1 | `byte` | archMode1 | Scheduling sub-mode |
| +1412 | 1 | `byte` | archMode2 | Scheduling sub-mode |
| +1413 | 1 | `byte` | archMode3 | Scheduling sub-mode |
| +1414 | 1 | `byte` | archMode4 | Architecture mode flag |
| +1415 | 1 | `byte` | archMode5 | Architecture mode flag; bit 2 checked during batch depth selection |
| +1416 | 1 | `byte` | archMode6 | Architecture mode flag |
| +1440 | 16 | `__m128i` | archVector | SSE-loaded scheduling parameters (4 x int32) |
| +1452 | 4 | `int32` | archWarpSize | Warp/thread configuration: 64 or 128 |
| +1456 | 4 | `int32` | archDispatchSize | Dispatch slot parameter: 16, 32, or 64 |
| +1460 | 4 | `int32` | archMaxThreads | Max threads per SM: 512 or 1024 |
| +1464 | 4 | `int32` | archParam5 | Architecture parameter: 4 (sm_60+ only) |
| +1472 | 4 | `int32` | archBlockSize | Block size parameter: 32 |
| +1480 | 8 | `int64` | archSpecData | Architecture-specific encoded scheduling data |
| +1584 | 16 | `__m128i` | archProfile | SSE-loaded architecture profile vector |

### Memory Layout Diagram

```
SchedulerContext (~1600 bytes)
+--------+--------+--------+--------+--------+--------+--------+--------+
|+0  vtable       |+8  funcContext   |+16 allocator    |+24 (padding)    |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+32 (padding)    |+40 preHookVtable |+48  regPressureCounters[0..9]     |
+--------+--------+--------+--------+--------+--------+--------+--------+
|  ...counters... |+60 mode |+64..84 |+88  maxBBDepth  |+92 maxBBDpthNT |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+96..175 (internal state)           |+176 active|+178 rrMode|           |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+240 phase       |+248 regBV1       |+256 regBV2      |                 |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+280 analysisSlots         |+292 valid|+296 tgtPri|+300 tgtSec|         |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+316 minR|+320 slack|+324 commit|+328 dualIss|  ...  |+380 latCut|     |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+384 tex |+388 maxBB|+392 alloc |    |+404 stall|+408 thresh|+412 batch|
+---------+-------+---------+--------+---------+-------+--------+--------+
|+416 xtraReg|+420 spillCnt|    |+432 budget|+440..456 livenessBV       |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+464 depth  |    |+480 cycle|+484 act|+485 dirty|    |+523 hcE|+524 yld|
+---------+-------+---------+--------+---------+-------+--------+--------+
|+532 hcBudget|  |+604 archP1|  |+616 archP2|                           |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+672 resourceSlots         |+680 depData       |    ...bitvector mgr... |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+720 arenaRef    |+728 bvBuf        |+736 cap  |+740 alloc|+744 funcRef|
+---------+-------+---------+--------+---------+-------+--------+--------+
|                      ...gap / internal state...                        |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+832 liveness bitvector ref |                                           |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+840    ArenaAllocator (embedded sub-object, ~120 bytes)                |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+960..1031 (internal/padding)                                           |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+1032..1098  configuration bitfield array (~67 bytes)                   |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+1099..1407 (internal state, ~308 bytes)                                |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+1408..1416  architecture mode flags (9 bytes)                          |
+---------+-------+---------+--------+---------+-------+--------+--------+
|+1440 archVector (16B) |+1452..1484 arch params |+1584 archProfile (16B)|
+---------+-------+---------+--------+---------+-------+--------+--------+
```

## Function Map

| Address | Size | Identity |
|---|---|---|
| `sub_6820B0` | 1.5 KB | BuildReadyList -- zero-dep instruction scan |
| `sub_682200` | -- | UnlinkFromReadyList -- remove and update deps |
| `sub_682490` | 14 KB | RegisterPressureAnalyzer -- per-class deltas |
| `sub_6833F0` | 10 KB | InitScheduleRegion -- per-BB setup and knob query |
| `sub_685A10` | 11 KB | InstructionBarrierCheck -- opcode analysis |
| `sub_687FE0` | 12 KB | ScheduleBlock -- per-BB scheduling entry |
| `sub_688DD0` | 20 KB | **ScheduleEngine** -- unified 3-mode engine |
| `sub_68A690` | 31 KB | BuildDependencies -- def-use chain DAG |
| `sub_68B9C0` | 46 KB | DependencyGraphBuilder -- full DAG construction |
| `sub_692200` | 18 KB | SchedulingHeuristic -- priority with FP scoring |
| `sub_695530` | 15 KB | ComputeLatencies -- instruction latency computation |
| `sub_69B7D0` | 17 KB | TopologicalSort -- valid execution ordering |
| `sub_69F170` | 12 KB | CriticalPathAnalysis -- DAG critical path |
| `sub_893100` | 17 KB | ClassifyInstruction -- opcode/operand analysis |
| `sub_894290` | 27 KB | BuildOperandDependencies -- operand-level edges |
| `sub_896D50` | 90 KB | InitOpcodeTable -- ROT13 SASS mnemonic table |
| `sub_89FBA0` | 85 KB | SetOpcodeLatencies -- per-opcode latency table |
| `sub_8BF890` | 929 B | AllocDynBatchData -- DynBatch context allocation |
| `sub_8C1BA0` | 6.3 KB | InitDynBatchState -- batch initialization |
| `sub_8C67A0` | 3.7 KB | ComputeResourceCost -- per-instruction FU cost |
| `sub_8C7290` | 5.1 KB | GetResourceVector -- SSE-optimized copy |
| `sub_8C7720` | 20 KB | ReorderInstructions -- red-black tree reordering |
| `sub_8C9320` | 47 KB | **ComputePriority** -- multi-criteria heuristic |
| `sub_8CBAD0` | 2.9 KB | PreScheduleSetup -- BB scan, 4095-instr limit |
| `sub_8CCF80` | 2.3 KB | IsLongLatencyOp -- latency > 19 check |
| `sub_8CD160` | 9.3 KB | ScheduleBasicBlock -- per-BB ordering loop |
| `sub_8CD6E0` | 1.3 KB | ReverseSchedule -- reverse post-order BBs |
| `sub_8CE520` | 12 KB | RegisterBudgetCurve -- piecewise linear model |
| `sub_8CEE80` | 8.7 KB | **ComputeRegisterBudget** -- occupancy-aware |
| `sub_8CF5D0` | 3.5 KB | CheckDualIssueEligibility |
| `sub_8CF880` | 28 KB | BuildDependencyGraph -- pre-scheduling DAG |
| `sub_8D0640` | 22 KB | **ScheduleInstructions** -- top-level orchestrator |
| `sub_8D9930` | 19 KB | BuildDependencyEdges -- RAW/WAR/WAW edges |
| `sub_8E3970` | ~53 B | ArenaAlloc -- bump allocator |
| `sub_8E3A80` | ~22 ln | ArenaFreeAll -- release all blocks |
| `sub_8E4400` | 3.3 KB | InitHWProfile_Warp -- warp dispatch params |
| `sub_8E5CA0` | 20 KB | MasterHWProfileBuilder -- latency/throughput |
| `sub_8F1EB0` | 15 KB | EncodeScheduleWords -- SASS control word output |
| `sub_8F6530` | 13 KB | OutputCompleteSchedule -- final output assembly |
| `sub_A95DC0` | 35 KB | SchedulingContext::configure -- knob loading |
| `sub_A97600` | 42 KB | PostSchedulePass::runOnFunction |
| `sub_A9DDD0` | 11.5 KB | HandleLargeFunction -- chunk-based scheduling |

## Cross-References

- [Scheduling Algorithm](algorithm.md) -- priority list scheduling internals, ready list management, backtracking
- [Latency Model](latency-model.md) -- per-opcode latency tables, functional unit mapping, architecture profiles
- [Scoreboards & Barriers](scoreboards.md) -- scoreboard encoding, dependency barrier assignment, stall/yield format
- [Register Allocation](../regalloc/overview.md) -- register allocator that the scheduler interacts with
- [Phase Manager](../passes/phase-manager.md) -- how ScheduleInstructions fits in the 159-phase pipeline
- [Knobs](../config/knobs.md) -- the 76 scheduling knobs and the knob query infrastructure
- [GMMA Pipeline](../passes/gmma-pipeline.md) -- GMMA/WGMMA operations targeted by DynBatch
