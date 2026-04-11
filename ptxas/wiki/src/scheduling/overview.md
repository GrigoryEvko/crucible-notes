# Instruction Scheduler Overview

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas instruction scheduler is a priority list scheduler with a 3-phase architecture. A single top-level orchestrator (`sub_8D0640`, ScheduleInstructions) drives three passes through one unified scheduling engine (`sub_688DD0`), each configured by a mode parameter that selects a different optimization objective: register pressure reduction, ILP/latency hiding, or dynamic batch optimization for tensor warpgroup operations. The scheduler runs twice in the ptxas pipeline -- once before register allocation on virtual registers (pre-scheduling) and once after physical register assignment (post-scheduling).

The scheduler consumes a dependency DAG built over the instruction list and produces a final instruction ordering together with SASS control words encoding stall counts, yield hints, barrier assignments, and scoreboard dependencies. The entire subsystem spans roughly 436 KB of code (0x893000--0x8FE000) with an additional 250 KB of supporting infrastructure in the 0x67F000--0x6A0000 range.

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

### Phase 1 -- ReduceReg (mode 1, callback 0x39)

Goal: minimize register pressure so the register allocator has headroom. This phase reorders instructions to reduce the maximum number of simultaneously-live virtual registers.

- Enabled by the named option `"ScheduleInstructionsReduceReg"` (default: on at `-O3`).
- Register targets set from knobs 776 (`SchedReduceIncLimit`) and 778 (`SchedReduceIncLimitHigh`) (defaults approximately 250 and 300).
- The mode byte 0x39 selects the register-pressure-minimizing priority weights inside the unified engine.
- The engine's inner dispatch reads `*(DWORD*)(scheduler+60) == 1` to enter the ReduceReg path.

### Phase 2 -- ILP / Latency Hiding (mode 0, callback 0x49)

Goal: maximize instruction-level parallelism and hide memory latencies by interleaving independent operations.

- Always runs (no separate enable gate).
- Uses reverse post-order BB iteration via `sub_8CD6E0`: iterates basic blocks from last to first after resetting liveness with `sub_781F80(func, 0)`.
- Computes a register budget capped at `min(archLimit, 0.95 * maxRegs)` via `sub_8CEE80`.
- The mode byte 0x49 selects latency-oriented priority weights.
- After this phase, `sub_8CF5D0` evaluates dual-issue eligibility and produces a dual-issue benefit score stored at `scheduler+328`.

### Phase 3 -- DynBatch (mode 2, callback 0x41)

Goal: batch-aware scheduling for GMMA/WGMMA warpgroup tensor operations. Groups tensor instructions into batches that can execute as warpgroup-cooperative operations with minimal pipeline stalls.

- Enabled by the named option `"ScheduleInstructionsDynBatch"` and only activates when the function has varying instruction counts across BBs.
- Controlled by knob 742 (`SchedCrossBlock`, cross-block scheduling mode).
- Reads stall/batch depth limits from knobs 805 (`SchedTexBatchTargetSelectRegisterTarget`), 741 (`SchedCountLoadsPerTex`), 761 (`SchedMaxRLiveOKslack`), 762 (`SchedMaxRLiveOKslackColdBlocks`).
- Allocates a 184-byte DynBatch context (`sub_8BF890`) with an `8 * numBBs` sub-array for per-BB batch tracking.
- Context initialization (`sub_8C1BA0`): sets batch window to 0xFFFFFFFF (sentinel), copies register liveness from `func+832`.
- The mode byte 0x41 selects batch-aware priority weights.

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

| Mode byte | Phase | `mode - 1` | Vtable offset | Index | Target function |
|---|---|---|---|---|---|
| `0x39` (57) | ReduceReg | +56 | `[7]` | core slot 7 | `sub_8DA6A0` -- ReduceRegPriority |
| `0x41` (65) | DynBatch | +64 | `[8]` | pipeline_A slot 0 | `sub_8E0F18` -- DynBatchPriority |
| `0x49` (73) | ILP/Latency | +72 | `[9]` | pipeline_A slot 1 | `sub_8E0F90` -- ILPPriority |

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

    // For sm_50: apply special dual-issue budget
    if arch_id == 5:
        budget = DualIssueBudget(budget)

    pressureCurve = ComputePressureCurve(sched, budget - 2)  // sub_8CE520

    sched.regBudget         = budget     // offset +432
    sched.committedTarget   = ...        // offset +324
    sched.minRegs           = ...        // offset +316
    sched.pressureSlack     = ...        // offset +320
```

### Pressure Curve (`sub_8CE520`)

`sub_8CE520(sched, regLimit, &nopDensity)` computes instruction density over a sliding window to decide whether reducing register pressure would improve ILP. It returns weighted instruction density (double); `nopDensity` receives weighted NOP density. It also writes `sched.usePressure` (offset +522) and `sched.minPhysRegs` (offset +512).

**Seed initialization.** A seed object at `func[223]` (byte offset +1784) drives the curve. Two paths:

- **Default** -- `seed.SetBreakpoints(4, 2, 6)` via vtable slot +16. The three arguments are `windowSize`, `minIssueWidth`, `maxIssueWidth`, defining a piecewise linear mapping from occupancy to effective issue width.
- **Knob 750 override** -- when `KnobIsSet(750)` is true, fetches the string value of `SchedEstimatedLoopIterations` and calls `seed.ParseString(str)` via vtable slot +24. This encodes custom per-loop iteration hints that replace the (4,2,6) defaults.

If the function has no loops (`sched[668] == 0`), both paths set `usePressure = 0` and return 0.0 immediately.

**Sliding window algorithm:**

```
function ComputePressureCurve(sched, regLimit):
    seed = sched.func[223]                          // pressure seed object
    if KnobIsSet(750):
        seed.ParseString(KnobGetString(750))        // vtable+24
    else:
        seed.SetBreakpoints(4, 2, 6)                // vtable+16
    if !sched.hasLoops:                             // byte at offset +668
        sched.usePressure = 0;  return 0.0

    hwBudget = RegToHWUnits(sched, regLimit)        // sub_6818D0
    W  = min(sched.maxStallCycles, 16)              // offset +404, capped
    Wt = sched.stallThreshold                       // offset +408

    // Two parallel tracks: A (real instructions), B (NOP-weighted)
    weightA = 0.0;  totalA = 0.0;  fillA = 0
    weightB = 0.0;  totalB = 0.0;  fillB = 0
    loopWt = 1.0
    peakSpanA = 0;  minPeakA = 0x1869F (sentinel)
    peakSpanB = 0;  minPeakB = 0x1869F

    for each instruction in BB linked list:
      if opcode == 52 (loop header) or masked opcode == 96 (BB boundary):
          totalA += ceil(fillA / W) * loopWt        // flush partial window
          totalB += ceil(fillB / W) * loopWt
          fillA = fillB = 0
          if opcode == 52:
              loopWt = EstimateTrip(func, bb)       // vtable at func+1784
          continue to first instruction of new region

      span = instr.programPoint - window.startPoint
      if isReal(instr):                             // not NOP, not dead
          // Track A: fill slot, accumulate span
          if fillA > 0 and accumulated_span > regLimit and > peakSpanA:
              peakSpanA = accumulated_span
          if peakSpanA > 0 and minPeakA > peakSpanA:
              minPeakA = peakSpanA
          // Reset window on overflow: peakSpanA = 0, weightA += loopWt
          slot[fillA] = instr.latencyClass
          fillA++; advance with Wt-wide multi-fill if stallThreshold > 1
      else:                                         // NOP goes to track B
          (symmetric logic for track B)

      if window full (fillA == W) or both tracks exhausted:
          weightA += loopWt;  reset window A

    // Final flush
    totalA += ceil(fillA / W) * loopWt
    totalB += ceil(fillB / W) * loopWt
    bestPeak = min(minPeakA, minPeakB)
    physRegs = RegToHWUnits(sched, bestPeak)
    archCap  = hw.vtable[93](hw, totalB, ...)       // hw[154] + hw[159]

    if physRegs > archCap:
        usePressure = 0                             // exceeds arch limit
    elif weightA <= totalA:
        usePressure = (weightB > totalB)            // B-track dominates
    sched.usePressure  = usePressure                // offset +522
    sched.minPhysRegs  = physRegs                   // offset +512
    *nopDensity        = weightB                    // out parameter
    return weightA
```

The caller (`sub_8CEE80`) converts densities to an ILP ratio: `ratio = occupancy / density`. It iterates up to 5 candidate budgets (3 when `hw[360] > 2`), picking the one that maximizes the ratio. Knob 740 (default 0.045) scales a per-register penalty when the budget overshoots the live-register count.

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

    if arch <= sm_52 AND scheduler+328 > 0:  // dual-issue benefit > 0
        return DualIssueScheduler          // sub_8B77C0

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

Pairs compatible instructions into dual-issue slots on architectures that support it (sm_50/Maxwell, sm_52). The outer loop walks scheduling slots; the inner loop finds a co-issuable partner via the dependency rule table.

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
function EmitScheduleForBB(sched, bb):
    for each instruction in scheduled order:
        stall   = ComputeStallCycles(sched, instr)   // distance to consumer
        yield   = ComputeYieldHint(sched, instr)      // warp scheduling hint
        barrier = AssignBarrier(sched, instr)          // 6 barriers available
        sb_deps = ComputeScoreboardDeps(sched, instr)  // read/write dependencies

        control_word = EncodeControlWord(stall, yield, barrier, sb_deps)
        EmitControlWord(instr, control_word)
```

Key encoding functions:

| Address | Purpose |
|---|---|
| `sub_8F1EB0` | Main schedule encoding entry |
| `sub_8F3130` | Encode stall count field |
| `sub_8F31F0` | Encode barrier field |
| `sub_8F3650` | Encode yield hint field |
| `sub_8F3860` | Encode scoreboard dependency field |
| `sub_8F4140` | Encode complete control word |
| `sub_8F6530` | Output complete schedule for function |

Seven verification functions at 0x8F7610--0x8F8CB0 validate the generated schedule: stall counts, barrier assignments, dependency chains, scoreboard correctness, control word format, yield hints, and overall schedule integrity.

See [Scoreboards](scoreboards.md) for the scoreboard and dependency barrier encoding format.

## Memory Management

The scheduler uses two allocator strategies:

1. **Arena allocator** (`sub_8E3970`): bump allocator with 10 KB block granularity, 8-byte alignment. Allocations within a scheduling pass use the arena for fast allocation. `sub_8E3A80` frees all blocks at once at pass completion.

2. **Free-list allocator** (`sub_8DA6D0`): free-list with block coalescing for persistent scheduling data. Maintains multiple free lists for different size classes. Blocks larger than 0x1FF bytes go to a separate large-block list. Adjacent free blocks are merged on deallocation.

## Per-Instruction Scheduling Metadata (SchedNode)

Each instruction has a pointer at `instr+40` (`sched_slot`) to a separate heap-allocated scheduling metadata block called a SchedNode. The metadata offsets documented throughout the scheduling pages (e.g., `metadata+24`, `metadata+32`, `metadata+108`) are relative to this SchedNode, **not** to the 296-byte Ori instruction object itself. The SchedNode block is at least 112 bytes; all nodes are linked into a singly-linked list at `func+104` (Code Object offset +104), separate from the instruction linked list at `func+272`.

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

### Relationship to the Instruction Object

```
 Ori Instruction (296 bytes)              SchedNode (>= 112 bytes)
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

After per-BB initialization, `sub_6833F0` walks the CFG to identify cross-block scheduling opportunities, gated by knob 744 (`SchedCrossBlockReorder`). For each predecessor-successor pair within the speculative distance threshold (knobs 743 `SchedCrossBlockInstsToSpeculate` and 747 `SchedCrossBlockTexToSpeculate`):

1. Sets `record[pred].crossBlockId = succ_bb_index` (marks predecessor active).
2. Clears bit 6 of `record[pred].flags` (predecessor is not a cross-block target).
3. Sets bit 6 of `record[succ].flags` (successor is a cross-block target).
4. Calls `sub_682F10` to allocate the 136-byte region scheduling context and store pointers at `record[pred]+48` and `record[succ]+56`.

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
