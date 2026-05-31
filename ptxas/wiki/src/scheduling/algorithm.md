# Priority List Scheduling Algorithm

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The scheduling engine implements a classical priority list scheduling algorithm extended with GPU-specific heuristics for register pressure management, functional unit contention avoidance, yield hint generation, and barrier-aware instruction ordering. A single unified engine (`sub_688DD0`, 20 KB) serves all three scheduling phases -- ReduceReg, ILP/Latency, and DynBatch -- differentiated only by a mode byte that selects different priority weight configurations. The algorithm iterates basic blocks, builds a ready list of zero-dependency instructions, selects the highest-priority candidate via an 8-bit packed heuristic, emits it into the final schedule, updates the dependency DAG, and repeats until all instructions in the block are placed.

| | |
|---|---|
| **Unified engine** | `sub_688DD0` (20 KB) -- mode-parameterized core loop |
| **Priority function** | `sub_8C9320` (47 KB, ~1300 lines) -- 8-bit packed heuristic |
| **Ready list builder** | `sub_6820B0` (1.5 KB) -- zero-predecessor scan |
| **Dependency pre-scan** | `sub_8CF880` (28 KB) -- reverse BB iteration |
| **Edge builder** | `sub_8D9930` (19 KB) -- RAW/WAR/WAW/memory/barrier edges |
| **Instruction mover** | `sub_925510` (341 bytes) -- doubly-linked list relink |
| **Resource tracker** | `sub_A09530` (365 bytes) -- per-instruction stall update |
| **Stall/barrier encoder** | `sub_8D7760` (41 KB) -- control word generation |
| **Alternative loop** | `sub_68B9C0` (46 KB) -- combined DAG + scheduling |
| **BB size limit** | 4095 instructions (split via `sub_931920`) |
| **Large function limit** | 16383 instructions (chunk-based via `sub_A9DDD0`) |

## Core Algorithm

The unified scheduling engine executes the following sequence for each basic block. All three phases (ReduceReg mode 0x39, ILP mode 0x49, DynBatch mode 0x41) follow this identical structure; only the priority weight selection differs.

```c
function ScheduleEngine(sched, mode, arg3, rebuild):
    if rebuild:
        InitScheduleRegion(sched)                 // sub_6833F0
        // Allocates 72-byte per-BB records
        // Queries knobs 595, 743, 747
        // Calls sub_7E5120 for instruction characterization

    for each bb in sched.basic_blocks:            // 72-byte stride
        InitResourceTracking(sched, bb)           // sub_A091C0
        // Zeroes 40-byte resource records (one per register class + 1)

        BuildReadyList(sched)                     // sub_6820B0

        while ready_list is not empty:
            best = SelectHighestPriority(sched)   // via priority vtable
            UnlinkFromReadyList(sched, best)      // sub_682200
            MoveInstruction(sched, best, ref)     // sub_925510
            UpdateStallCycles(sched, best)         // sub_A09530
            UpdateWARTracking(sched, best)         // sub_A09D40

            for each successor of best:
                successor.dep_count -= 1
                if successor.dep_count == 0:
                    ready_list.insert(successor)
                    // Sorted insertion using priority value

        PostBBCleanup(sched, bb)                  // sub_BDC200 / sub_BDCDE0
```

The outer loop iterates basic blocks via an array of 72-byte records (`v112 = 72 * bb_index`). The inner loop is a standard worklist algorithm: remove the highest-priority ready instruction, schedule it, and propagate readiness to its successors in the dependency DAG by decrementing their predecessor counts.

### Mode Selection

The mode byte stored at `*(DWORD*)(scheduler+60)` (byte offset 240 from the scheduler object base) controls which priority weight set the engine uses. The orchestrator `sub_8D0640` writes this field before each call to the unified engine:

| Mode | Value | Callback | Objective |
|---|---|---|---|
| ReduceReg | 1 | 0x39 | Minimize register pressure. Prioritizes instructions that release registers (last-use operands). |
| ILP/Latency | 0 | 0x49 | Maximize instruction-level parallelism. Prioritizes critical-path and long-latency instructions. |
| DynBatch | 2 | 0x41 | Batch-aware tensor scheduling. Groups GMMA/WGMMA operations for warpgroup cooperation. |

The engine reads the mode byte at the top of each basic block's scheduling loop to select the per-BB knob gate and the vtable pipeline group (`sub_688DD0` line 281):

```c
function decode_mode(sched):                                    // sub_688DD0, line 281
    mode = *(DWORD*)(sched + 240)                               // scheduler DWORD index 60

    switch mode:
      case 1:   // ReduceReg -------------------------------------------------
        // Per-BB gate: skip this BB if knob "ScheduleInstructionsReduceReg" says so
        skip = CheckKnob(ctx, "ScheduleInstructionsReduceReg", bb)
        if skip: goto next_bb

        // Weight configuration (set by orchestrator before engine entry):
        //   sched+484 = 1        (ReduceReg flag -- enables budget reduction in sub_8C9320)
        //   sched+176 = 1        (pressure-tracking active)
        //   sched+296 = RegToHWUnits(knob 776, default 250)   // GPR budget target
        //   sched+300 = RegToHWUnits(knob 778, default 300)   // GPR budget ceiling
        //   sched+292 = 1        (budget validity flag)
        //   sched+412 = knob 761 (default 3; or 6/12 for sm_90 with GMMA present)
        //   sched+416 = knob 762 (default -1 = auto-compute)
        // Priority effect: bit 4 (pressure overflow) dominates the comparison;
        //   sub_8C9320 applies the budget reduction formula -(target/8 + 3) regs
        // Vtable: pipeline_group_B (indices 31-53 at 0x8E14xx-0x8E15xx)

      case 0:   // ILP/Latency -----------------------------------------------
        // No per-BB knob gate (always executes every BB)

        // Weight configuration:
        //   sched+484 = 0        (ReduceReg flag off)
        //   sched+176 = 0        (no pressure reduction in priority function)
        //   sched+324 = budget from sub_8CEE80: min(archLimit, 0.95 * maxRegs)
        //   sched+316 = context+616 (latency-depth scaling factor)
        //   sched+404 = min(context+508, 16), or knob 805 (queue depth cap)
        //   sched+408 = knob 741 (default 3)
        // Priority effect: bits 1 (critical-path) and 2 (stall-free) dominate;
        //   latency hiding is the primary scheduling objective
        // Vtable: pipeline_group_A (indices 8-30 at 0x8E0Exx-0x8E10xx)

      case 2:   // DynBatch ---------------------------------------------------
        // Per-BB gate: skip this BB if knob "ScheduleInstructionsDynBatch" says so
        //   Default: enabled unless knob 742 overrides, or ctx+1419 bit 5 is set
        skip = CheckKnob(ctx, "ScheduleInstructionsDynBatch", bb)
        if skip: goto next_bb

        // Weight configuration (inherits ILP budget, adds batch context):
        //   sched+176 = 1        (pressure-tracking re-enabled)
        //   Batch context: 184-byte struct from sub_8BF890 with 8*numBBs array
        //   sched+404 = knob 806 (capped at 16), or 6 if context+507 is set
        //   sched+408 = knob 741 (default 3)
        // Priority effect: bits 5 (hot-cold / batch grouping) and 3 (same-BB)
        //   emphasize keeping GMMA instruction groups together
        // Vtable: pipeline_group_C (indices 54-76 at 0x8E22xx-0x8E24xx)
```

The three vtable pipeline groups contain 23 method pointers each (one per scheduling callback slot). All groups share the same 8 core methods (indices 0-7) but substitute specialized implementations for the 23 pipeline-specific hooks that control ready-list insertion order, candidate comparison, and post-scheduling resource updates. The callback index passed to the engine (0x39=57, 0x41=65, 0x49=73 decimal) selects the entry point within the core methods, indexing into the vtable at `off_21DBEF8`.

The engine uses vtable dispatch at `*(a1+40)` and `*(a1+48)` for polymorphic pre/post scheduling hooks. This allows each mode to inject custom behavior at scheduling boundaries without modifying the core loop.

## Ready List Construction

`sub_6820B0` (1.5 KB) scans the instruction linked list and collects every instruction with zero unsatisfied dependencies into a sorted ready list.

```c
function BuildReadyList(sched):
    for instr in sched.instruction_list:          // linked list at sched[20]
        if instr.opcode == 52:                    // NOP / BB boundary marker
            continue                              // follow to real instruction

        metadata = *(QWORD*)(instr + 40)          // SchedNode pointer
        if *(DWORD*)(metadata + 8) == 0:          // depCount == 0
            *(QWORD*)(metadata + 16) = sched[5]   // link to current head
            sched[5] = instr                       // new head
            vtable_callback(sched, 104, instr)     // insertion hook
            *(DWORD*)(metadata + 28) = 0           // reset latency counter
```

The ready list is a singly-linked list threaded through SchedNode offset `+16` (the `nextReady` field). Sort order is maintained at insertion time by the priority function -- each new instruction is inserted at its correct position so that the head of the list is always the highest-priority candidate. All `metadata+N` offsets throughout the scheduling pages refer to fields within the SchedNode block pointed to by `instr+40` (`sched_slot`), not offsets from the instruction object itself. See the [SchedNode layout](overview.md#per-instruction-scheduling-metadata-schednode) for the complete field map.

Opcode 52 instructions are phantom BB boundary markers. The builder skips them but follows their linked-list successors to reach real instructions beyond the boundary.

The vtable+104 callback provides a polymorphic insertion hook. Different scheduling strategies can override this to implement custom ready-list policies (e.g., the dual-issue scheduler uses it to pair co-issuable instructions).

## Priority Function

`sub_8C9320` (47 KB decompiled, ~1300 lines) is the heart of instruction selection. It computes a scheduling priority as an integer with 8-bit packed fields. Each bit encodes a different heuristic criterion. Because priority comparison reduces to a single integer comparison, the ready list maintains sort order without multi-key sorting overhead.

### 8-Bit Priority Encoding

| Bit | Name | Meaning | Notes |
|---|---|---|---|
| 7 (MSB) | yield-related | Instruction is near a yield boundary | Higher priority ensures yield hints align with scheduling boundaries |
| 6 | yield flag | Instruction triggers or participates in a yield sequence | Controls warp scheduler round-robin interaction |
| 5 | hot-cold | Memory access temperature (1 = hot, 0 = cold) | Hot = global/texture/surface loads with long latencies; cold = constant/shared |
| 4 | hot-cold / pressure | Packed byte holds hot-cold flag; pressure overflow acts as comparison override | See Priority Function Internals for the dual mechanism |
| 3 | same-BB preference | Instruction belongs to the currently-scheduled BB | Discourages cross-BB instruction motion |
| 2 | stall-free | Scheduling this instruction introduces zero stall cycles | All producer latencies have completed |
| 1 | latency-bound | Instruction is on the DAG critical path | Prioritizes latency-sensitive dependency chains |
| 0 (LSB) | tiebreaker | Additional ordering factor | Encodes instruction position, operand count, or FU preference |

Higher numeric value = higher priority. Bit 7 is the most significant criterion: yield-boundary instructions always schedule before non-yield instructions when both are ready.

### Hot-Cold Classification

The hot-cold flag (bit 5) classifies memory operations by expected latency:

- **Hot** (bit 5 = 1, higher priority): global memory loads (`LDG`), texture fetches (`TEX`, `TLD`), surface operations. These have high latency (hundreds of cycles) and benefit most from early scheduling to overlap with computation. Detected by `sub_A9CDE0`.
- **Cold** (bit 5 = 0, lower priority): constant memory loads (`LDC`), shared memory operations (`LDS`, `STS`). These have low latency and do not need early scheduling. Detected by `sub_A9CF90`, which also suppresses the pressure-overflow and critical-path extension signals.

Classification uses `sub_A9CDE0` (hot detection) and `sub_A9CF90` (cold detection). Memory space type is determined by `sub_693BC0`, which returns space codes: 3 = shared, 16 = global, 2 = local, 11 = surface, 7 = constant, 1 = generic. Hot-cold tracking is gated by `scheduler+523` and `scheduler+532`; when the hot-cold budget (`scheduler+532`) reaches zero, the feature deactivates for the remainder of the BB.

### Pressure Overflow

Bit 4 in the packed byte holds the hot-cold flag (see above). The pressure overflow signal is a separate Boolean computed by checking all four register classes (GPR, predicate, address, UGP) against their respective limits. When any class exceeds its budget, the pressure overflow flag activates and acts as a **comparison override**: the candidate wins regardless of the packed priority byte, forcing the scheduler to select the instruction that relieves register pressure. This is the primary mechanism by which the ReduceReg phase achieves its objective: the mode sets a tight register budget via `scheduler+178`, causing pressure overflow to activate frequently and driving the scheduler toward pressure-reducing orderings. See the Priority Function Internals section for the exact per-class threshold checks.

### Priority Evaluation Sequence

The priority function evaluates criteria in this order for each candidate instruction:

1. `sub_8C7290`: extract 4-class register deltas, same-BB flag, and per-BB resource vector (SSE-optimized)
2. Compute yield saturation: check write-barrier counters for predicate, GPR, and UGP register classes against their ceilings (7, 7, and `target_desc+624` respectively)
3. `sub_8C67A0`: compute per-instruction resource cost if BB slot not yet committed
4. `sub_8C7120`: update barrier tracking state (if `metadata+111` bit 7 set)
5. Evaluate register pressure: compute per-class overflow against budget (`scheduler+432`) and per-class limits; derive pressure-overflow Boolean
6. Evaluate stall-free: compare earliest cycle (`metadata+32`) vs current cycle (`scheduler+480`)
7. Evaluate critical path: compare barrier-target count vs depth threshold (`scheduler+464`)
8. Evaluate yield bits: opcode 39 (yield-related) and opcode 96 (yield flag from `scheduler+524`)
9. Pack 8 bits into priority byte
10. Evaluate hot/cold: `sub_A9CDE0` / `sub_A9CF90` (only when `scheduler+523` active)
11. Multi-stage comparison against running best: resource vectors, then XOR-based bit scan, then secondary tiebreakers

The function scans the full ready list in a single pass (not limited by knob 770 for the scan itself). Knob 770 (priority queue depth, default 4) controls the depth threshold mechanism for critical-path activation, not the number of candidates evaluated.

### Key Internal Variables

| Variable | Source | Content |
|---|---|---|
| `budget_hw` | `sub_6818D0(scheduler, scheduler[432] - scheduler[412])` | Register budget in HW register units |
| `reduced_hw` | `sub_6818D0(scheduler, budget - budget/16)` | Tighter budget for critical-path threshold (or knob 760 override) |
| `queue_depth` | knob 770 | Depth threshold parameter (default 4); controls critical-path activation |
| `per_bb_flag` | knob 769 | Per-BB scheduling flag; when set, resets yield state between BBs |
| `scheduler+420` | state | Spill-mode countdown; when > 0, forces aggressive scheduling with bit 1 = 1 |
| `scheduler+464` | state | Depth threshold -- number of barrier targets that must be ready before critical-path activates |
| `scheduler+480` | state | Current scheduling cycle; used for stall-free evaluation |
| `scheduler+523` | state | Hot-cold tracking enable flag; gated by knob |
| `scheduler+524` | state | Current yield state; propagated to CONTROL instructions via bit 6 |
| `scheduler+532` | state | Hot-cold budget counter; decremented per cold instruction, disables tracking at zero |
| `scheduler+672` | allocation | Per-BB resource cost table (84 bytes per slot) |

### Support Subroutines

| Address | Size | Purpose |
|---|---|---|
| `sub_8C67A0` | 3.7 KB | Compute per-instruction resource cost. Calls `sub_A08A00` (resource model) three times: mode 1 = instruction's own cost, mode 2 = operand release cost, mode 3 = combined BB-level impact. Uses SSE `_mm_add_epi32` for vector accumulation. |
| `sub_8C7290` | 5.1 KB | Copy 10-element int32 resource vector from per-BB table at `scheduler+672`. SSE `_mm_loadu_si128` bulk copy. Special case: opcode 97 (`STG` in ROT13; used as control/boundary marker) returns base scheduler state with zeroed deltas. |
| `sub_8C7720` | 20 KB | Red-black tree operations for instruction reordering within BB. Maintains a balanced BST of scheduling candidates for O(log N) insertion, removal, and priority update. |
| `sub_8C7120` | -- | Barrier tracking state update. |
| `sub_693BC0` | -- | Memory space classification and latency query. |
| `sub_6818D0` | 24 B | `RegToHWUnits`: conditionally doubles register count when `LivenessCountRegComp` (bit 3 of `scheduling_mode_flags`) is set (sm\_70+). |

### Priority Function Internals

The full logic of `sub_8C9320` divides into three phases: (1) pre-scan the ready list to collect aggregate BB statistics, (2) iterate the ready list a second time evaluating each candidate and maintaining a running best, and (3) update scheduler state and return the winner. The function signature is `(scheduler, &second_best) -> best_instruction`.

#### Phase 1: Pre-Scan Statistics

Before priority evaluation begins, the function iterates the entire ready list (linked via `metadata+16`) and accumulates per-BB statistics that feed into the per-instruction priority decisions:

| Variable | Init | Accumulation | Meaning |
|---|---|---|---|
| `shared_mem_count` | 0 | `++` when opcode 183 and `sub_693BC0` returns space 3 | Count of shared-memory operations in ready list |
| `neg_reg_deficit` | 0 | `+= delta` when register delta < 0 | Total register pressure reduction from ready instructions |
| `max_dep_cycle` | -1 | `max(current, metadata+92)` | Highest dependency cycle among all ready instructions |
| `max_pred_cycle` | 0 | `max(current, metadata+88)` | Highest predecessor cycle among all ready instructions |
| `barrier_count` | 0 | `++` when `metadata+108 & 1` | Count of barrier-target instructions in ready list |
| `dep_flag_count` | 0 | `++` when `metadata+108 & 2` | Count of instructions with dependency-set flag |
| `pos_pressure_sum` | 0 | `+= delta` when register delta > 0 | Total register pressure increase from ready instructions |
| `filtered_pressure` | 0 | `+= delta` when within depth threshold | Pressure increase from depth-eligible instructions |
| `max_barrier_slot` | -1 | `max(current, metadata+24)` for barrier targets | Latest BB slot among barrier-target instructions |
| `min_barrier_latency` | 99999 | `min(current, metadata+28)` for barrier targets | Shortest latency counter among barrier-target instructions |
| `max_nonbarrier_cycle` | -1 | `max(current, metadata+32)` for non-barrier | Latest earliest-available-cycle for non-barrier instructions |
| `any_stall_free` | 0 | `\|= (metadata+32 >= 0)` | Whether any instruction can issue without stalling |
| `total_ready` | 0 | `++` for every instruction | Total instructions in ready list |
| `preferred_instr` | NULL | non-barrier instr with max `metadata+24` | The program-order-latest non-barrier instruction |

The pre-scan also maintains a depth-threshold table: an array of up to 32 barrier-target instruction pointers sorted by their latency counter (`metadata+28`). This table is scanned to compute `scheduler+464` (depth threshold) and `scheduler+380` (latency cutoff), which control when the critical-path bit activates.

**Depth-threshold derivation algorithm.** On the first invocation per BB (`scheduler+464 == -1`), the state initializes to `scheduler+464 = 0, scheduler+480 = 0, scheduler+420 = 0`. If the BB has active barriers (`scheduler+396 > 0`) and barrier-target instructions exist in the ready list, the threshold computation runs:

```c
// Phase 1a: collect barrier-target pointers (during pre-scan)
barrier_table[32]     // local stack array, max 32 entries
barrier_table_count = 0
barrier_ready_count = 0   // subset with metadata+32 < INT_MAX (non-stalling)
for each instr in ready_list:
    if metadata+108 & 1:                          // barrier-target flag
        if barrier_table_count <= 31:
            barrier_table[barrier_table_count] = instr
        barrier_table_count++
        barrier_ready_count += (metadata+32 < 0x80000000)

// Phase 1b: derive threshold from sorted latency scan
scheduler[424] = barrier_table_count              // total barrier targets
scheduler[428] = barrier_ready_count              // non-stalling subset
old_threshold  = scheduler[464]

if old_threshold < barrier_table_count:
    cap       = min(barrier_table_count, 32)
    lower     = -1                                // latency lower bound
    accum     = 0                                 // accumulated instruction count

    do:
        // --- find next distinct latency above lower bound ---
        next_lat  = 999999
        tie_count = 0
        for i in 0 .. cap-1:
            lat = metadata+28 of barrier_table[i]
            if lat > lower and lat <= next_lat:
                tie_count++
                if lat != next_lat:
                    next_lat  = lat               // new minimum found
                    tie_count = 1

        accum += tie_count                        // add this tier
        lower  = next_lat                         // advance lower bound
        if barrier_table_count == 0:
            lower = 999999                        // sentinel for empty list
    while old_threshold > accum

    scheduler[376] = 0                            // threshold is now valid
    scheduler[464] = accum                        // depth threshold
    scheduler[380] = lower                        // latency cutoff

// Fallback (at LABEL_87): if scheduler[376]==1 and barrier_count >= scheduler[464],
// set scheduler[464]=barrier_count, scheduler[380]=max_barrier_latency
```

The inner loop is equivalent to an iterative selection of the next-smallest distinct latency tier from an unsorted array. Each tier groups barrier targets that share the same latency counter value. The `do...while old_threshold > accum` condition ensures the threshold absorbs at least `old_threshold` instructions before stopping. On subsequent invocations within the same BB, `scheduler+464 >= 0` and the computation is skipped.

During Phase 2 candidate evaluation, the critical-path bit activates when `scheduler+464 > 0 && scheduler+464 <= barrier_count` -- i.e., enough barrier targets are ready to meet the depth threshold.

#### Phase 2: Register Budget Prologue

Before the main loop, the function computes two register budgets from `scheduler+432` (target register count):

```c
budget_base = scheduler[432] - scheduler[412]     // target minus committed

if ReduceReg_mode (scheduler+178):               // ReduceReg tightens budget
    if scheduler[416] < 0:
        budget_base -= (scheduler[432] / 8) + 3   // reduce by ~12.5% + 3
    else:
        budget_base -= scheduler[416]              // explicit reduction

budget_hw    = RegToHWUnits(scheduler, budget_base)     // sub_6818D0
reduced_hw   = RegToHWUnits(scheduler, budget_base - budget_base/16)
                                                         // ~6.25% tighter

if knob_760_active:
    reduced_hw = RegToHWUnits(scheduler, budget_base - knob_760_value)

queue_depth  = 4                                  // default
if knob_770_active:
    queue_depth = knob_770_value                  // override
```

The `RegToHWUnits` conversion (`sub_6818D0`) maps a virtual register count to hardware allocation units:

```c
fn RegToHWUnits(scheduler, count):
    flags = *(*(scheduler + 8) + 1376)        // scheduling_mode_flags
    if flags & 0x08:                           // bit 3: LivenessCountRegComp
        return 2 * count                       // doubled for HW-aligned tracking
    else:
        return count                           // 1:1 passthrough
```

Bit 3 of `scheduling_mode_flags` (knob 419 `LivenessCountRegComp`) is set on sm\_70+ targets, where the hardware allocates registers in 8-register blocks (256 registers/warp). When set, the scheduler internally tracks pressure in 4-register half-units, so `RegToHWUnits` doubles to produce full 8-register hardware units. On sm\_30--sm\_60, registers allocate in 2-register blocks (64 registers/warp) and the scheduler tracks in native units, so the function is an identity.

| SM Generation | HW Alloc Granularity | Bit 3 | Multiplier | Effect |
|---|---|---|---|---|
| sm\_30 -- sm\_60 | 2 regs/thread (64/warp) | 0 | 1x | count passes through unchanged |
| sm\_70+ | 8 regs/thread (256/warp) | 1 | 2x | count doubled to match 8-reg blocks |

`budget_hw` sets the threshold for bit 4 (pressure overflow). `reduced_hw` provides a tighter threshold used in the critical-path assessment. `queue_depth` (knob 770) limits how many candidates receive full priority evaluation; the rest use cached values from initial insertion.

#### Phase 3: Per-Bit Computation

For each instruction in the ready list, `sub_8C7290` extracts its per-register-class deltas (4 classes: GPR, predicate, address, UGP) and the same-BB flag. Then each priority bit is computed:

**Bit 7 -- Yield-related.** Determined by opcode. Only opcode 39 (YIELD instruction variant) can set this bit. The condition checks the last operand's low 2 bits:

```c
if opcode_masked == 39:
    operand_index = operand_count - 1 - ((opcode >> 11) & 2)
    yield_related = (instr[84 + 8*operand_index] & 3) == 0
else:
    yield_related = 0
```

When set, the instruction is a yield boundary marker and receives absolute highest priority regardless of all other heuristics.

**Bit 6 -- Yield flag.** Set only for opcode 96 (CONTROL instruction):

```c
if opcode_masked == 96:
    yield_flag = scheduler[524]       // current yield state
else:
    yield_flag = 0

// Post-adjustment: suppress when hot/pressure bits dominate
if (bit5_set || bit4_set):
    yield_flag = 0
    if metadata[32] < scheduler[480]:    // behind schedule
        yield_flag = scheduler[396] ? original_yield : 0
```

The yield flag propagates the scheduler's warp yield state only through CONTROL instructions, ensuring yield hints align with scheduling barriers.

**Bit 5 -- Hot-cold classification.** Requires hot-cold tracking to be active (`scheduler+523` set, gated by `scheduler+532 > 0`):

```c
if hot_cold_active:
    is_hot = sub_A9CDE0(target_desc, context, instruction)
else:
    is_hot = 0

// Cold detection suppresses priority
if sub_A9CF90(target_desc, context, instruction):    // is_cold?
    pressure_overflow = 0                             // suppress bit 4
    critical_extension = 0                            // suppress lookahead
```

`sub_A9CDE0` returns true for global memory loads (`LDG`), texture fetches (`TEX`, `TLD`), and surface operations -- instructions with latencies in the hundreds of cycles. `sub_A9CF90` returns true for constant loads (`LDC`), shared memory operations (`LDS`/`STS`) -- low-latency operations. Hot instructions (bit 5 = 1) get higher priority to schedule early and overlap their long latencies with computation. Cold instructions (bit 5 = 0) are deprioritized.

**Bit 4 -- Pressure overflow.** This bit does NOT appear directly in the initial packing as a single variable. Instead, the pressure overflow signal (`v81` in decompiled source) feeds into the candidate comparison logic as an override. The mechanism:

```c
// For barrier-target instructions:
budget_in_units = RegToHWUnits(scheduler, scheduler[432])
headroom        = RegToHWUnits(scheduler, 8)
if budget_in_units > headroom + scheduler[72]:   // plenty of headroom
    pressure_overflow = 0
elif latency_counter > min_barrier_latency + 9:  // far from ready
    pressure_overflow = 0
else:
    // Check all 4 register classes against their limits:
    overflow = false
    overflow |= (scheduler[72] + gpr_delta > budget_hw)
    overflow |= (scheduler[68] + pred_delta > 7)
    overflow |= (scheduler[56] + addr_delta > 7)
    overflow |= (scheduler[60] + ugp_delta >= target_desc[624])
    pressure_overflow = overflow
```

When pressure_overflow = 1, the candidate wins the comparison regardless of other bits -- it is the scheduler's mechanism for emergency register pressure relief. In the packed byte's bit 4 position, the hot-cold flag occupies the slot. The pressure overflow signal operates at a higher level: it can force the candidate to win even when its packed priority byte is lower.

**Per-register-class overflow limits.** The four register classes each have a fixed overflow threshold. Three are hardcoded constants; the fourth is dynamic:

| Class | Scheduler field | Limit | Comparison | Source |
|---|---|---|---|---|
| 0 -- GPR | `scheduler[72]` | `budget_hw` | `current + delta > budget_hw` | `RegToHWUnits(scheduler, budget_base)`, dynamic per-kernel |
| 1 -- Predicate | `scheduler[68]` | 7 | `current + delta > 7` | Hardcoded in `sub_8C9320` line 1044 |
| 2 -- Address | `scheduler[56]` | 7 | `current + delta > 7` | Hardcoded in `sub_8C9320` line 1045 |
| 3 -- UGP | `scheduler[60]` | `sm_backend[624]` | `current + delta >= limit` | Read from target descriptor at runtime |

Note the asymmetry: classes 0--2 use strict greater-than (`>`), while class 3 uses greater-than-or-equal (`>=`). In the decompiled source, the class 3 check is `v81 = (target_desc[624] < delta + current)` at line 1086 -- strict less-than from the limit's perspective, which is `>=` from the sum's perspective. This means UGP overflow triggers one count sooner than the other classes relative to their limits.

**`sm_backend[624]` values by architecture.** The UGP limit is a field on the SM backend object, accessed via `*(*(context+1584) + 624)`. Its value tracks the architectural uniform register file size:

| Architecture | SM | `sm_backend[624]` | UR file | Notes |
|---|---|---|---|---|
| Volta | sm_70--sm_73 | 0 | absent | No UR hardware; UGP class check is vacuous |
| Turing | sm_75 | 63 | UR0--UR62 | First architecture with dedicated UR file |
| Ampere | sm_80--sm_87 | 63 | UR0--UR62 | Same UR geometry as Turing |
| Ada | sm_89 | 63 | UR0--UR62 | Same UR geometry |
| Hopper | sm_90 | 63 | UR0--UR62 | Same UR geometry |
| Blackwell | sm_100+ | 63 | UR0--UR62 | Same UR geometry |

All sm_75+ architectures share a 63-entry UR file (UR63 = URZ, the zero register). On pre-Turing targets, `sm_backend[624]` is 0, making the `>= 0` comparison always true for any nonzero UGP delta -- but this is moot because no UGP registers are allocated on those targets, so `ugp_delta` is always 0.

**Bit 3 -- Same-BB preference.** Output parameter from `sub_8C7290`:

```c
same_bb = sub_8C7290.output_param_5     // boolean from resource copy
```

Set when the instruction belongs to the currently-scheduled basic block. Instructions imported from other BBs by global scheduling get `same_bb = 0`, reducing their priority relative to local instructions.

**Bit 2 -- Stall-free.** Computed from the earliest-available-cycle field:

```c
if countdown_active (scheduler[420] != 0):
    if metadata[32] < scheduler[480] AND instr != preferred_instr:
        stall_free = 0
        if pressure_plus_reg_sum > 0:
            goto full_evaluation    // positive pressure = needs analysis
    else:
        stall_free = 1
else:
    // Normal mode: stall-free when producers have completed
    if metadata[32] >= scheduler[480]:
        stall_free = 1
    elif instr == preferred_instr:
        stall_free = 1
    else:
        stall_free = 0
```

`metadata+32` is the instruction's earliest available cycle -- the latest completion time among all its producer instructions. `scheduler+480` is the current scheduling cycle. When earliest >= current, all producers have retired and the instruction can issue with zero pipeline stalls.

**Bit 1 -- Critical-path / latency-bound.** Complex multi-path computation:

```c
if countdown_active (scheduler[420] != 0):
    // Spill mode: almost always critical
    if !(barrier_bits_set_in_priority):
        if slot_limit_exceeded:
            critical = 1
        else:
            critical = !(pressure_sum <= 0 && max_reg_class == 0)
    else:
        critical = 0
else:
    // Normal mode: depth threshold comparison
    if barrier_count >= scheduler[464]:
        critical = 1      // enough barriers ready -> critical path active
    else:
        critical = 0
```

In spill mode (active when `scheduler+420 > 0`), the critical-path bit is set for nearly all instructions to maximize scheduling throughput. In normal mode, it activates when the number of barrier-target instructions in the ready list meets or exceeds the depth threshold computed during the pre-scan, indicating that the scheduler is processing a latency-critical dependency chain.

**Spill mode state machine.** The scheduler tracks three fields that together form a NORMAL/SPILL/NORMAL state machine: `spillModeCountdown` (+420), `spillSequenceCounter` (+396), and `depthThreshold` (+464). The post-processing phase of each `SelectBestInstruction` call updates them:

```c
// === ENTER SPILL MODE (computed_countdown) ===
// Triggered when winner is a barrier-target AND countdown == 0.
// Pre-scan populates: barrierTargetCount (+424), readyBarrierCount (+428).
if winner_is_barrier_target AND spillModeCountdown == 0:
    N = barrierTargetCount                     // from pre-scan of ready list
    T = depthThreshold
    if N <= 3 OR readyBarrierCount > 0:        // few targets or some ready
        if barrierReadyCount <= T OR allPressureInThreshold:
            countdown = barrierReadyCount       // use ready-list count
        else:
            if totalPressure + winnerGPR > budget:
                countdown = depthThreshold      // fall back to threshold
            else:
                countdown = barrierReadyCount
    else:                                       // many targets, none ready
        if N > T AND partialPressure != totalPressure
                  AND totalPressure + winnerGPR > budget:
            countdown = depthThreshold          // pressure-driven threshold
        // else: countdown stays at N (default from barrierTargetCount)

// === TICK (every scheduling step while in spill mode) ===
spillModeCountdown -= 1
spillSequenceCounter -= 1
if winner.earliestCycle >= 0:                   // valid producer cycle
    stallFreeCounter -= 1                       // decrement stall-free tracker
    if stallFreeCounter == 0
       AND spillSequenceCounter > 0
       AND spillSequenceCounter < maxStallCycles
       AND spillModeCountdown > 1:
        goto exit_spill                         // stall-free supply exhausted

// === EXIT SPILL MODE (two paths) ===
// Path A -- barrier-target winner while countdown active:
//   normal tick above, countdown naturally reaches 0.
if spillModeCountdown == 0:
    depthThreshold = -1                         // sentinel: force re-scan

// Path B -- non-barrier winner forces immediate exit:
if !winner_is_barrier_target AND spillModeCountdown > 0:
    pack winner's saved priority bits
    if !(same_bb_bit | stall_free_bit)          // no scheduling pressure
       AND slotContention + slotBase + winnerCycle <= slotLimit:
        skip exit                               // low-pressure, keep going
    else:
exit_spill:
        spillModeCountdown = 0
        depthThreshold = -1                     // force full re-init next call
```

The sentinel value `depthThreshold = -1` causes the next `SelectBestInstruction` invocation to zero all three counters and re-run the barrier-target pre-scan, which recomputes the depth threshold from the updated ready list. This ensures the transition back to NORMAL mode uses fresh pressure data rather than stale values from the previous spill episode.

**Bit 0 -- Tiebreaker (barrier-target).** Read directly from instruction metadata:

```c
tiebreaker = metadata[108] & 1      // barrier-target flag
```

Barrier-target instructions (those waiting on a hardware barrier) get bit 0 = 1. Since this is the lowest-priority bit, it only affects ordering when all higher bits are identical. Scheduling barrier targets promptly allows the barrier resource to be retired sooner, freeing scoreboard entries for other instructions.

#### Packed Byte Assembly

The 8 bits are packed into a single byte using shift-and-mask arithmetic:

```c
priority = (yield_related    << 7)         // bit 7
         | (yield_flag       << 6) & 0x7F  // bit 6
         | (hot_cold         << 5) & 0x3F  // bit 5  (initially yield copy)
         | (hot_flag         << 4) & 0x3F  // bit 4
         | (same_bb          << 3) & 0x0F  // bit 3
         | (stall_free       << 2) & 0x0F  // bit 2
         | (critical_path    << 1) & 0x03  // bit 1
         | (tiebreaker       << 0) & 0x03  // bit 0
```

The `& 0xNN` masks ensure each bit occupies exactly one position. In the initial packing, bit 5 and bit 6 both derive from the yield variable; the hot-cold flag (`sub_A9CDE0` result) overwrites bit 5 in subsequent repackings that occur during the spill-mode and comparison paths.

#### Candidate Comparison

The comparison between the current candidate and the running best is NOT a simple integer comparison of the packed bytes. The function performs a multi-stage refinement:

1. **Resource vector comparison**: A 4-tuple lexicographic comparison of per-register-class pressure scores. The four classes are compared in fixed order; the first class whose scores differ determines the winner. Lower score wins (ascending), so the instruction that increases register pressure less is preferred:

```c
// Per-class pressure scores (computed earlier for each candidate):
//
//   score_GPR  = max(0, gpr_delta + scheduler[64] - 1)
//                  +1 when gpr_delta > 0 AND cumulative_gpr > 0
//                       AND instr_latency < max_predecessor_cycle
//                  +1 for control ops (opcode 97) when gpr_sum > 0
//   score_pred = pred_delta + scheduler[68]        // live predicates
//                  zeroed when total <= 6, or == 7 with pred_delta <= 0
//   score_addr = addr_delta + scheduler[56]        // live address regs
//                  zeroed when total <= 6, or == 7 with addr_delta <= 0
//   score_UGP  = ugp_delta  + scheduler[60]        // live uniform GPRs
//                  zeroed when total < UGP_limit (= context[1584]+624)
//
// Best-so-far scores stored as: best_GPR, best_pred, best_addr, best_UGP
// (all initialized to -1 before the first candidate is evaluated)

// 4-tuple lexicographic compare  (sub_8C9320 lines 1375-1386)
classes = [GPR, pred, addr, UGP]              // fixed priority order
for class in classes:
    if current.score[class] != best.score[class]:
        if current.score[class] < best.score[class]:
            current_wins = true               // ascending: lower is better
        else:
            current_wins = false
        goto apply_result                     // first difference decides
// all four equal -> fall through to priority byte XOR scan
```

The direction is uniformly **ascending** across all four classes: the candidate with the smaller pressure score wins. A score of 0 means the instruction does not push the register class above its safe occupancy threshold; larger values indicate increasing pressure overflow. By checking GPR first, the scheduler prioritizes the widest register file -- the class with the greatest spill cost impact.

2. **Priority byte XOR scan**: When resource vectors are equal, the function XORs the current and best packed bytes and checks differing bits in this order:
   - Bit 4 (0x10) -- pressure: winner has bit 4 set (higher pressure need)
   - Bit 6 (0x40) -- yield: winner has bit 6 set (yield participation)
   - Bit 1 (0x02) -- critical: winner has bit 1 set
   - Bit 2 (0x04) -- stall-free: winner has bit 2 set
   - Bit 5 (0x20) -- hot-cold: winner has bit 5 set (hot memory op)

3. **Secondary tiebreakers** (when all checked bits match):
   - Barrier group index (`v213` vs `v253`)
   - Latency counter comparison (`v223` vs `v248`)
   - Bit 7 yield-related (only when shared-memory count > 0)
   - Contention score (see formula below)
   - Slot manager cycles (`max(ComputeCost(instr) - cycleCursor, 0)`; see Slot Manager Object Layout)
   - Earliest available cycle (`metadata+32`)
   - Dependency cycle (`metadata+92`)
   - Latest deadline (`metadata+40`)
   - Register delta magnitude

4. **Positional fallback**: When all heuristic comparisons are tied, the instruction with the higher BB slot (`metadata+24`) wins, preserving original program order.

The multi-stage comparison explains why the packed byte uses non-obvious bit ordering. Bits 4, 6, 1, 2, 5 are checked before bit 7 in the refinement path, even though bit 7 is the MSB. The packed byte enables fast ready-list insertion sort (integer comparison), while the full comparison function provides nuanced selection for the actual scheduling decision.

##### Contention Score Formula

The contention score (`v216` in decompiled source) quantifies how much scheduling a given instruction would increase register pressure beyond the budget. It has two cases:

```c
live_after_gpr = scheduler[72] + gpr_delta       // projected GPR live count
pressure_sum   = pos_pressure_sum + gpr_delta     // v289 + v283

if reduced_hw >= live_after_gpr:
    // instruction stays within budget -- no pressure penalty
    contention_score = base_contention

else:
    // instruction would exceed the reduced budget
    pressure_delta  = live_after_gpr - reduced_hw
    pressure_sign   = (pressure_sum <= 0) ? 1 : 0

    contention_score = base_contention
                     + 2 * RegHalfUnits(scheduler, pressure_delta)
                     - pressure_sign
```

| Term | Decompiled | Meaning |
|---|---|---|
| `base_contention` | `v72` | Slot-manager scheduling cost. For barrier-target instructions, clamped to `max(v72, slotLimit - slotBase)`. For non-barrier non-stalling instructions, 0 or 1 from vtable dispatch; +100 on the fallthrough path when no stall-free candidates exist. |
| `gpr_delta` | `v283` | GPR register delta from `sub_8C7290` resource vector (element 5 of the 10-element vector). Positive means the instruction defines more registers than it consumes. |
| `pos_pressure_sum` | `v289` | Accumulated positive GPR deltas from prior scheduling steps (element 9 of `v284` resource vector). Tracks cumulative register pressure increase within the BB. |
| `reduced_hw` | `v222` | Tighter register budget in HW units: `RegToHWUnits(budget_base - budget_base/16)`, or `RegToHWUnits(budget_base - knob_760)` when knob 760 is active. Approximately 6% below the full budget. |
| `live_after_gpr` | `v191` | Projected GPR live count after scheduling this instruction: `scheduler[72] + gpr_delta`. |
| `pressure_sign` | `v212` | Boolean: 1 when `pressure_sum <= 0` (net pressure is non-positive across ready candidates including this one), 0 otherwise. Provides a one-unit discount when pressure is already declining. |
| `RegHalfUnits` | `sub_6818B0` | Inverse of `RegToHWUnits` (`sub_6818D0`): when `LivenessCountRegComp` (bit 3 of scheduling mode flags) is set, returns `ceil(count / 2)`; otherwise identity. Converts register-count overshoot into HW-aligned penalty units. |

The guard condition `reduced_hw < live_after_gpr` activates the penalty term only when scheduling this instruction would push projected GPR pressure above the reduced budget. The `2 *` multiplier doubles the penalty to make register overflow a strong differentiator in the tiebreaker cascade. The `- pressure_sign` term subtracts 1 when the overall pressure direction is downward, slightly favoring instructions that are part of a declining pressure trend even if they individually overshoot the budget.

#### Scheduler State Updates

After selecting the best candidate, the function updates scheduler state:

```c
// Spill mode countdown
if winner is barrier-target:
    scheduler[420] = computed_countdown - 1
    scheduler[396] -= 1                       // spill sequence counter
    if metadata[32] >= 0:
        scheduler[400] -= 1                   // stall-free counter
        if stall_free_count==0 AND remaining>0 AND countdown>1:
            scheduler[420] = 0                // force exit spill mode
            scheduler[464] = -1               // reset depth threshold
else:
    // Non-barrier winner in countdown mode
    if !(barrier_bits in priority) AND slot_cost within budget:
        // do nothing, continue countdown
    else:
        scheduler[420] = 0                    // exit spill mode
        scheduler[464] = -1                   // reset depth threshold

// Slot manager update (when winner has positive scheduling cost)
if best_cost > 0 AND slotManager.pendingCount > 0:
    if slotManager.advanceReady:
        slotManager.cycleCursor += slotManager.cycleStride  // advance epoch
        slotManager.pendingCount = 0                        // reset batch
        slotManager.anchorInstr  = NULL                     // clear anchor
    best.metadata[28] = slotManager.vtable.ComputeCost(slotManager, winner, 0)

// Hot-cold counter update
if hot_cold_active AND winner is cold (sub_A9CF90 returns true):
    scheduler[532] -= 1                       // decrement hot-cold budget
elif hot_flag was set for winner:
    scheduler[523] = 0                        // disable hot-cold tracking
```

The function returns the best instruction pointer and writes the second-best to `*a2` for lookahead scheduling.

#### Slot Manager Object Layout

The slot manager is a polymorphic C++ object at `scheduler+16`. It tracks scheduling-cycle budgets so the priority function can detect when issuing an instruction would exceed the per-BB throughput limit. Binary evidence: `sub_8C9320` lines 911--945, 1113, 1119, 1789, 1832--1848; `sub_688DD0` lines 631--638.

| Offset | Size | Name | Purpose |
|--------|------|------|---------|
| +0 | 8 | `vtable*` | Per-architecture method table pointer |
| +28 | 4 | `cycleCursor` | Cumulative issue-cycle position. Incremented by 1 per instruction via vtable[12] (`sub_680080`) and by `cycleStride` on epoch advance |
| +44 | 4 | `cycleStride` | Epoch advance delta added to `cycleCursor` when `advanceReady` fires |
| +52 | 4 | `cycleLimit` | Upper throughput bound; budget test is `cycleCursor + contention + cycleStride <= cycleLimit` |
| +76 | 4 | `pendingCount` | Instructions scheduled since last epoch advance; guards the advance path |
| +80 | 8 | `anchorInstr` | Instruction that anchored the current epoch; cleared on advance |
| +140 | 1 | `advanceReady` | Boolean; when set the next winner-update advances `cycleCursor` by `cycleStride` |

Three vtable slots drive the slot manager:

| Vtable idx | Offset | Signature | Role |
|---|---|---|---|
| 12 | +96 | `IncrementCount(slotMgr, instr)` | Per-instruction: `++cycleCursor`. Fast path `sub_680080` inlines to a single `++DWORD[7]` |
| 36 | +288 | `ComputeCost(slotMgr, instr, 0) -> int` | Returns the cycle cost for `instr`. Contention = `max(cost - cycleCursor, 0)` |
| 43 | +344 | `CompareAnchors(slotMgr, anchor, cand, 0) -> bool` | When `cycleCursor >= cost` and `anchorInstr != NULL`, tests whether `cand` shares the anchor's slot. Result XOR'd to 0/1 contention |

The contention value feeds into two budget checks: (1) spill-mode critical-path test `cycleStride + contention + cycleCursor <= cycleLimit` (line 1119), and (2) second-best spill-exit comparison at line 1789. A third guard at line 1113 checks `cycleCursor + 100 >= minReadyCycle` to suppress non-barrier candidates when the cursor is near the earliest ready cycle.

During the scheduling loop (`sub_688DD0` line 637--638), each non-BB-boundary instruction triggers `sub_682490(scheduler, instr, prev, 1)`. That function walks register operands, matches source/destination conflicts against the live-register bitmask, and returns a register-port-conflict cost written to `metadata+28`. This becomes `best_cost` (`v233`) that gates the slot manager advance.

## Dependency DAG Construction

The dependency graph is built in two stages before the scheduling loop begins. The DAG is a directed acyclic graph where nodes are instructions and edges represent ordering constraints with associated latency values.

### Stage 1: Pre-Scan (`sub_8CF880`, 28 KB)

Iterates basic blocks in **reverse order** (bb[N-1] to bb[0]) using the BB ordering array at `func+512`.

For each BB:
1. Check knobs 314/313 for per-BB scheduling skip flags
2. Walk the instruction linked list, identifying NOP/control instructions
3. Set `bb->next` pointers and configure BB scheduling state
4. Delegate to Stage 2 (`sub_8D9930`) for edge construction
5. Manage memory arenas with SSE-optimized copies for metadata arrays

Contains approximately 14 nested loops for edge construction. The reverse iteration order ensures that when the scheduler processes a BB, all of its successors have already been characterized.

### Stage 2: Edge Construction (`sub_8D9930`, 19 KB)

For each pair of instructions within a BB, checks for five dependency types:

| Type | Abbreviation | Condition | Edge Latency |
|---|---|---|---|
| True | RAW | Read-after-write on same register | Producer's pipeline latency |
| Anti | WAR | Write-after-read on same register | 0 (ordering constraint only) |
| Output | WAW | Write-after-write on same register | 1 (minimum separation) |
| Memory | -- | Store before load to same memory space | Conservative; full ordering |
| Barrier | -- | Instruction depends on barrier/sync result | Barrier completion latency |

Operand analysis is performed by `sub_894290` (27 KB), which processes 16-bit operand descriptors encoding:

| Bits | Field |
|---|---|
| 12--15 | Register class |
| 8--11 | Bank number |
| 0--7 | Dependency type |

Memory dependencies are conservative: all stores are ordered before subsequent loads to the same memory space. The scheduler does not perform alias analysis -- it relies on the memory space classification from `sub_693BC0` to determine whether two operations might conflict.

#### Pair-check pruning via last-writer tracking

Despite the "for each pair" description above, the implementation does **not** perform an all-pairs O(N^2) comparison. `sub_68A690` (BuildDependencies) maintains two RB-trees keyed by register ID -- one for the last **writer** of each register, one for the last **reader** set -- and uses bitvector-indexed iteration to touch only the registers an instruction actually references.

```c
function BuildDependencies(reg_tree, bb, slot_index):
    // reg_tree:     RB-tree  { reg_id -> last_writer_instr }
    // reader_tree:  RB-tree  { reg_id -> last_reader_instr_list }
    // Both are reset at BB entry, so they track only intra-BB state.

    def_bv  = BitVector.alloc(slot_index)      // sub_BDBEF0: defs bitvector
    use_bv  = BitVector.alloc(slot_index)       // sub_BDBF80: uses bitvector

    for instr in bb.instructions (program order):
        // --- extract operand bitvectors from instr+84 descriptors ---
        ProcessOperands(instr, &def_bv, &use_bv)   // sub_6A78F0

        // --- RAW edges: for each source register, find its last writer ---
        for reg_id in use_bv.set_bits():            // O(K_use) iterations
            writer = reg_tree.lookup(reg_id)        // O(log R) RB-tree search
            if writer != NULL:
                AddEdge(writer, instr, RAW, reg_id) // sub_6848D0

        // --- WAR edges: for each dest register, find its last reader ---
        for reg_id in def_bv.set_bits():            // O(K_def) iterations
            reader = reader_tree.lookup(reg_id)     // O(log R)
            if reader != NULL:
                AddEdge(reader, instr, WAR, reg_id) // sub_684970

            // --- WAW edges: dest-vs-previous-dest ---
            prev_writer = reg_tree.lookup(reg_id)   // O(log R)
            if prev_writer != NULL:
                AddEdge(prev_writer, instr, WAW, reg_id) // sub_684920

            reg_tree.insert_or_update(reg_id, instr)   // update last writer
            reader_tree.remove(reg_id)                  // clear stale readers

        // --- update reader set for sources ---
        for reg_id in use_bv.set_bits():
            reader_tree.insert(reg_id, instr)

        def_bv.clear()
        use_bv.clear()

    // Complexity: O(N * K * log R) where:
    //   N = instructions in BB, K = avg operands per instruction (~3),
    //   R = distinct live registers (bounded by register file size, typ. <256).
    // For a BB of 500 instructions this is ~4,500 tree ops instead of 125,000 pairs.
```

The `SBPruning` knob (DAG knobs region, ROT13-encoded) enables or disables this pruning strategy. When disabled, the fallback path in `sub_8D9930` reverts to the straightforward pair-wise scan. A separate `AdvancedSBPruningBudget` knob caps the total number of edges that can be created per BB, providing an additional compile-time safety valve for pathological cases.

### Supplementary Dependency Builders

These functions handle specific aspects of dependency construction in the 0x6A0000--0x6B0000 range:

| Address | Size | Purpose |
|---|---|---|
| `sub_68A690` | 31 KB | BuildDependencies -- walks instruction lists and creates producer-consumer dependency edges from def-use chains |
| `sub_6A97B0` | 26 KB | AddDependencyEdges -- register-level data dependency edges |
| `sub_6A2D30` | 11 KB | ChainDependencies -- memory ordering constraints (ordering edges between memory operations even without explicit data deps) |
| `sub_6A78F0` | 23 KB | ProcessOperands -- iterates operand arrays at instruction `+84`, extracts register file pressure and dependency distance information |

## Instruction Emission

`sub_925510` (341 bytes, 57 lines) is the universal instruction relocation primitive. It moves an instruction to a new position in the doubly-linked instruction list.

```c
function MoveInstruction(block, instr, insert_before):
    // 1. Unlink from current position
    instr.prev.next = instr.next
    instr.next.prev = instr.prev

    // 2. Insert before reference instruction
    instr.next = insert_before
    instr.prev = insert_before.prev
    insert_before.prev.next = instr
    insert_before.prev = instr

    // 3. Update block boundaries if needed
    if instr was block.head (block+272):
        block.head = instr.next
    if instr was block.tail (block+280):
        block.tail = instr.prev

    // 4. Notify subsystems
    UpdateDependencyGraph(block, instr)     // sub_7EEC10
    UpdateBlockTimestamp(block)             // sub_7DDCA0
```

This function has 13 callers across the codebase. It serves as the shared instruction movement primitive for the scheduler, register allocator, and peephole optimizer.

## Resource Tracking

The scheduler maintains 10 functional unit resource counters per basic block, tracking pipeline utilization to avoid saturating any single execution unit.

### Resource Vector Layout

Each per-BB resource slot occupies 84 bytes (21 DWORDs) stored at `*(scheduler+672) + 84 * slot_index`:

| Offset | Size | Content |
|---|---|---|
| 0--36 | 10 x int32 | Current resource usage per functional unit |
| 40--76 | 10 x int32 | Resource pressure delta (change since last step) |
| 80 | int32 | BB-entered flag and auxiliary bits |

### Functional Unit Pipes

| Index | Pipe | Typical Instructions |
|---|---|---|
| 0 | Integer ALU | IADD, IMAD, ISETP, LOP, SHF |
| 1 | FP32 | FADD, FFMA, FMUL, FSETP |
| 2 | FP64 | DADD, DFMA, DMUL |
| 3 | Tensor core | HMMA, IMMA, BMMA, BGMMA |
| 4 | Load/store | LD, ST, LDG, STG, LDS, STS |
| 5 | Texture | TEX, TLD, TXQ |
| 6 | Branch/control | BRA, JMP, EXIT, RET, BAR |
| 7 | Shared memory | ATOMS, REDS (overlaps with pipe 4 for LDS/STS) |
| 8 | Special function | MUFU (RCP, RSQ, SIN, COS, EX2, LG2) |
| 9 | Uniform/predicate | UPLOP, UISETP, uniform operations |

### Resource Tracking Helpers

| Address | Size | Purpose |
|---|---|---|
| `sub_A091C0` | -- | Initialize per-BB resource arrays to zero |
| `sub_A09530` | 365 bytes | Update stall cycle counters after scheduling an instruction. Decrements pending latency counters for all tracked resources. |
| `sub_A09D40` | -- | Update WAR (anti-dependency) resource tracking for register operands |
| `sub_A08A00` | -- | Resource model query (called in 3 modes by `sub_8C67A0`) |

The resource model `sub_A08A00` is called three times per instruction by `sub_8C67A0`:
- **Mode 1**: instruction's own execution cost (FU assignment + pipeline latency)
- **Mode 2**: operand release costs (freed resources when an operand reaches last-use)
- **Mode 3**: combined instruction + BB-level impact (aggregate pressure)

The 10-DWORD resource vectors are copied via `_mm_loadu_si128` and accumulated with `_mm_add_epi32` in `sub_8C67A0` (one occurrence each); `sub_A08A00` itself contains no SSE intrinsics — all arithmetic on the resource vector lives in the caller.

## Register Pressure Tracking

The scheduler tracks register liveness via a bitvector at `scheduler+832`. Each bit represents one register; the pressure is the popcount of the live set.

```c
function UpdateRegisterPressure(sched, instr):
    for each operand in instr.operands:
        if operand.is_def:
            set_bit(sched.live_bv, operand.reg)       // DEF: mark live
        if operand.is_last_use:
            clear_bit(sched.live_bv, operand.reg)     // LAST-USE: mark dead
    sched.current_pressure = popcount(sched.live_bv)
```

The bitvector is sized to `(numRegs + 1)` words, or `(2 * numRegs + 2)` when knob 420 (dual-register tracking) is active. Dual-register tracking separately tracks register pairs for instructions that consume or produce 64-bit values.

Pressure state fields:

| Offset | Content |
|---|---|
| `scheduler+432` | Target register count (from budget computation) |
| `scheduler+324` | Committed register target |
| `scheduler+316` | Minimum register count |
| `scheduler+320` | Register pressure slack (headroom) |

When `current_pressure > scheduler+432`, the priority function sets bit 4 (pressure overflow) in the encoding, biasing the scheduler toward instructions that release registers.

## Per-Instruction Scheduling Metadata (SchedNode)

Each instruction has a pointer at `instr+40` to a heap-allocated SchedNode block. The offsets below are relative to the SchedNode base, **not** the 296-byte Ori instruction. See the [SchedNode layout](overview.md#per-instruction-scheduling-metadata-schednode) for the authoritative field map.

| Offset | Type | Content |
|---|---|---|
| +8 | int32 | `dep_count` -- unsatisfied predecessor count (0 = ready) |
| +16 | QWORD | `next_ready` -- linked-list pointer in ready list |
| +24 | int32 | `bbSlot` -- 1-based BB position (-1 = unscheduled) |
| +28 | int32 | `latency_counter` -- current stall counter |
| +32 | int32 | `earliestCycle` -- earliest available cycle |
| +40 | int32 | `latestDeadline` -- latest deadline cycle |
| +44 | int32 | Barrier group index |
| +88 | int32 | `maxPredecessorCycle` |
| +92 | int32 | `maxDependencyCycle` |
| +108 | byte | Flags: bit 0 = barrier target, bit 1 = has dependency, bit 2 = early schedulable, bit 3 = late schedulable, bit 4 = has register operand |
| +111 | byte | Flags: bit 7 = uses expensive register file |

### Extended SchedNode Fields (+112 through +236)

The alternative scheduling loop `sub_68B9C0` (46 KB) and the main scheduling driver `sub_688DD0` access fields well beyond +111. These fields are populated during cross-block scheduling setup and consumed by the region-ordering and resource-accounting phases. The total SchedNode allocation is at least 240 bytes, not the 112 documented above.

| Offset | Size | Type | Name | Purpose |
|---|---|---|---|---|
| +112 | 16 | -- | (reserved) | Alignment padding / internal state between +111 and +128 |
| +128 | 8 | `ptr` | `regionChainNext` | Singly-linked next pointer for the cross-block region chain. `sub_68B9C0` walks this chain to iterate over BB-representative nodes: `for (node = head; node; node = *(QWORD*)(node + 128))`. Also written during chain construction: `*(QWORD*)(node + 128) = prev_head`. Separate from the `func+104` metadata chain at +0. |
| +136 | 8 | -- | (reserved) | Padding between chain pointer and scheduling region index |
| +144 | 4 | `i32` | `schedRegionIndex` | Index into the 72-byte per-block scheduling record array at `scheduler+184`. Used as `72 * *(int*)(node + 144)` to reach the block's pressure snapshot and resource state. Also serves as the hash key for the FNV-1a region dedup cache in `sub_68B9C0` (hashed with constants `0x811C9DC5` / `16777619`). |
| +148 | 16 | -- | (reserved) | Gap between region index and resource class |
| +164 | 4 | `i32` | `resourceClassIndex` | Index into the resource-class record array (40 bytes per entry). `sub_688DD0` computes `src + 40 * *(int*)(node + 164)` to look up the 10-element register-delta vector for pressure subtraction during unscheduling. |
| +168 | 68 | -- | (reserved) | Internal state for the cross-block scheduling engine. Offsets +168 through +232 are not directly accessed via named patterns in `sub_68B9C0` or `sub_688DD0`. |
| +236 | 4 | `i32` | `regionOrderWeight` | Region ordering weight used by the cross-block BB traversal. `sub_68B9C0` reads this to decide whether to skip or schedule a region: values near `INT_MIN` (`0x80000001`) or `INT_MAX` (`0x7FFFFFFF`) are sentinels meaning "boundary" (the test `(weight + 0x7FFFFFFF) > 0x7FFFFFFE` catches both). The loop iterates through the region chain comparing consecutive weights to detect region transitions. |

The scheduling loop also reads Ori instruction fields directly (not via the SchedNode): `instr+72` (opcode), `instr+80` (operand count), `instr+84` (operand descriptors).

Sentinel values: bbSlot -1 (unscheduled), latency 0x1869F (99999 = infinity), regionOrderWeight `INT_MIN`/`INT_MAX` (boundary marker).

The `dep_count` field at +8 is the key scheduling control: it counts unsatisfied predecessors in the dependency DAG. When a predecessor is scheduled, the engine decrements every successor's `dep_count`. When `dep_count` reaches zero, the instruction becomes ready and is inserted into the ready list.

## Stall and Barrier Insertion

After the scheduling loop determines instruction order, `sub_8D7760` (41 KB) converts the abstract schedule into SASS control words.

For each instruction in the scheduled order:

| Field | Computation | Range |
|---|---|---|
| Stall count | Distance in cycles to the nearest dependent consumer | 0--15 (capped by knob 805, max 16) |
| Yield hint | Warp scheduling hint -- should the HW scheduler switch to another warp? | 0 or 1 |
| Barrier assignment | Which of the 6 available barriers this instruction writes/waits on | 0--5, or none |
| Scoreboard deps | Read/write dependency tracking for the hardware scoreboard | Bitmask |

### Per-Architecture Barrier Model

The barrier model parameters come from per-SM data tables indexed through the architecture backend. The scheduling context reaches the SM profile via `ctx+8 -> backend[198] -> profile`, carrying the scoreboard config table at profile+54216 and a secondary gate at +54224. Two polymorphic vtable variants (A/B, differing in 2 of 65 slots at `0x21DAA00`/`0x21DAC10`) are selected by the profile's scoreboard model byte.

Per-SM parameters from [`per_sm_scoreboard_configs.json`](../../../extracted/per_sm_scoreboard_configs.json) and [`per_sm_dependency_rules.json`](../../../extracted/per_sm_dependency_rules.json):

| SM | Gen | HW barriers | SB entries | Max triplets | Dep rules | Max stall | Max barrier lat |
|---|---|---|---|---|---|---|---|
| sm_60 | Pascal | 6 | -- | -- | 619 | 39 | 56 |
| sm_70 | Volta | 6 | -- | -- | 619 | 39 | 56 |
| sm_75 | Turing | 6 | -- | -- | 619 | 39 | 56 |
| sm_80 | Ampere | 6 | 31 | 1 | 256 | 39 | 56 |
| sm_86 | Ampere | 6 | 31 | 1 | 256 | 39 | 56 |
| sm_89 | Ada | 6 | 32 | 1 | 256 | 39 | 56 |
| sm_90 | Hopper | 6 | 31 | 1 | 256 | 39 | 56 |
| sm_90a | Hopper | 6 | 32 | 1 | 256 | 39 | 56 |
| sm_100 | Blackwell | 6 | 75 | 6 | 430 | 39 | 56 |
| sm_103 | Blackwell | 6 | 46 | 1 | 430 | 39 | 56 |

All architectures share 6 HW barriers and raw max stall of 39 (capped to 15 by the 4-bit field or knob 805). Yield threshold is arch-dependent (typically 4 on sm_80+). The key differentiator is the scoreboard config table: sm_80--90a use 31--32 single-triplet entries (`{scoreboard_id, threshold, mask}`), while sm_100 uses 75 entries with up to 6 triplets, reflecting Blackwell's richer scoreboard hierarchy.

### Dispatch Pseudocode for `sub_8D7760`

```c
function EmitScoreboardRecords(sched_ctx, instr, dep_table, out):
    backend = *(sched_ctx + 8)
    profile = *(backend[198] + 16)              // SM profile
    sb_mode = *(u8*)(profile + 54216)           // scoreboard model selector
    sb_gate = *(u32*)(profile + 54224)          // secondary gate

    vtable  = **(backend + 1584)                // polymorphic vtable A or B
    // vtable+1816: isScoredInstruction(obj, instr) -> bool
    // vtable+1432: isBarrierExempt(obj, instr)     -> bool

    opcode = instr[18] & 0xFFFFCFFF            // mask modifier bits 12-13
    if opcode in {0x55/*TEX*/, 0x6D/*TLD4*/}:
        group = lookup_texture_tables(instr, backend)
    elif opcode == 0x5B: group = 6             // BSSY
    elif opcode == 0x5C: group = 5             // BSYNC
    elif opcode == 0xB7 && has_ld_flag: group = 13
    else: group = sub_7DFFC0(instr, backend)   // resource class query

    // 330-entry jump table at 0x21D9EF8 (guard: opcode <= 0x149)
    // 206 specialized handlers, 124 default-path
    emit_records_via_sub_8C25B0(group, instr, out)

    if backend[1368] & 0x20:                   // extended scoreboard
        if vtable[+1816](*(backend+1584), instr):
            emit_extended_record(out, instr)
```

The function manages a 32-entry barrier table for tracking active assignments.

See [Scoreboards & Barriers](scoreboards.md) for the control word encoding format.

## Alternative Scheduling Loop

`sub_68B9C0` (46 KB) is a monolithic function that combines dependency graph construction with the scheduling loop. It serves as an alternative entry point for scheduling passes that need to build the DAG inline rather than using the pre-built graph from Stage 1.

Internal structure:

1. Initialize scheduling state (`sub_685700`)
2. Initialize ready-list management (`sub_687080`)
3. Check resource conflicts (`sub_687410`)
4. **Inner loop** (`while(2)` infinite loop with break conditions):
   - Check if ready list is empty -- break if so
   - Check opcode 97 (`STG` in ROT13; used as scheduling barrier/control marker) -- special handling
   - Select best instruction from ready list
   - Schedule it: assign cycle, update resources, process edges
   - For each successor: decrement `dep_count`, add to ready list if zero
   - Check boundary condition (`v236`) -- break if done
5. Track first-pass initialization via `v215`

This function accesses the Ori instruction's opcode at `instr+72`, plus SchedNode fields (via `instr+40` pointer): `+24` (bbSlot), `+144` (scheduling slot), `+164` (resource class), and `+236` (latency).

## Specialized Scheduling Strategies

The region 0x89C550--0x8BE320 contains 17+ specialized scheduling strategies. These are selected based on code characteristics (loop structure, tensor operations, function size) and optimization level. Each strategy implements a variation of the core list scheduling algorithm with different heuristics or search strategies.

| Address | Size | Strategy | Description |
|---|---|---|---|
| `sub_8B1190` | 16 KB | Backtracking | Undo and retry on scheduling conflicts. Rolls back the last N steps and tries alternative orderings. Bounded depth prevents exponential blowup. |
| `sub_8B2D90` | 18 KB | Global optimization | Cross-BB scheduling. Moves instructions across BB boundaries when safe (no side effects, dominance preserved). |
| `sub_8B4590` | 13 KB | Permutation search | Exhaustive permutation of instruction orderings for small BBs. Falls back to heuristic for larger blocks. |
| `sub_8B5400` | 14 KB | Latency-optimized | Maximizes memory latency hiding by aggressive interleaving of independent operations. |
| `sub_8B6D60` | 12 KB | Pressure-optimized | Minimizes live range overlap by scheduling defs as close to their uses as possible. |
| `sub_8B77C0` | 15 KB | Dual-issue | Pairs co-issuable instructions for dual-issue architectures (sm_50/Maxwell). Uses `sub_A9CDE0` and `sub_A9CF90` for compatibility checks. |
| `sub_8B8900` | 12 KB | Tensor scheduling | HMMA/BMMA/BGMMA grouping for warpgroup tensor operations. |
| `sub_8B9390` | 23 KB | Software pipelining | Loop body overlapping -- interleaves iterations to fill pipeline bubbles. |
| `sub_8BAAE0` | 15 KB | Loop-aware | Trip count + register awareness for loop bodies. |
| `sub_8BB9C0` | 8.2 KB | Prefetch scheduling | Inserts and schedules memory prefetch instructions. |
| `sub_8BC0B0` | 6.1 KB | Barrier coalescence | Merges adjacent barrier instructions to reduce overhead. |
| `sub_8BC990` | 7.6 KB | Scoreboard optimization | Minimizes scoreboard entries by reusing barrier registers. |
| `sub_8BCFA0` | 6.8 KB | Warp schedule optimization | Warp-level yield tuning for multi-warp scheduling. |
| `sub_8BDC40` | 7.9 KB | Dual-issue pairing | Instruction pair selection for dual-issue slots. |
| `sub_8BE320` | 25 KB | Complex combined pass | Multi-strategy combined pass for complex code patterns. |
| `sub_8A9D80` | 21 KB | Depth-first | DFS-based instruction ordering for deep dependency chains. |
| `sub_8AB750` | 9.8 KB | Critical path | DAG analysis for priority weight computation. |

### Backtracking Scheduler

The backtracking strategy (`sub_8B1190`) is notable because it breaks from the greedy nature of standard list scheduling. When a scheduling decision leads to excessive stalls or resource conflicts, it can undo the last N steps (where N is bounded by a configurable depth), re-insert the affected instructions into the ready list, and try a different selection. This provides limited but effective lookahead without the full cost of optimal scheduling.

### Dual-Issue Scheduling

For sm_50 (Maxwell), `sub_8B77C0` pairs instructions that can execute simultaneously on different functional units. Eligibility is checked by `sub_8CF5D0` (3.5 KB), which verifies architecture support and computes a dual-issue benefit score at `scheduler+328`. Pairing compatibility uses `sub_A9CDE0` (is instruction dual-issuable?) and `sub_A9CF90` (can this pair with the next instruction?).

## Size Limits and Chunking

Two mechanisms prevent the scheduling algorithm from hitting quadratic complexity on large inputs:

### BB Size Limit

`sub_8CBAD0` scans all basic blocks during pre-scheduling setup. Any BB exceeding **4095 instructions** is split by inserting scheduling barriers (`sub_931920`). This caps the per-BB scheduling problem size, ensuring the O(n^2) dependency graph construction remains tractable. The maximum BB size is tracked at `scheduler+388`.

### Large Function Chunking

Functions exceeding **16383 instructions** (`*(a1+372) > 0x3FFF`) trigger chunk-based scheduling via `sub_A9DDD0` (11.5 KB). The function is partitioned into chunks that are scheduled independently, then the results are merged. This avoids the full O(n^2) DAG construction for very large kernels. The chunk boundary selection respects BB boundaries and dependency chains to minimize cross-chunk constraint violations.

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_6820B0` | 1.5 KB | BuildReadyList -- zero-dep instruction scan | HIGH |
| `sub_682200` | -- | UnlinkFromReadyList -- remove and update deps | HIGH |
| `sub_682490` | 14 KB | RegisterPressureAnalyzer -- per-class deltas | HIGH |
| `sub_6833F0` | 10 KB | InitScheduleRegion -- per-BB setup, knob query | HIGH |
| `sub_685700` | -- | InitSchedulingState -- loop initialization | MEDIUM |
| `sub_685A10` | 11 KB | InstructionBarrierCheck -- opcode analysis | HIGH |
| `sub_687080` | -- | ReadyListManagementHelper | MEDIUM |
| `sub_687410` | -- | ResourceConflictCheck | MEDIUM |
| `sub_687FE0` | 12 KB | ScheduleBlock -- per-BB scheduling entry | HIGH |
| `sub_688DD0` | 20 KB | **ScheduleEngine** -- unified 3-mode core loop | HIGH |
| `sub_68A690` | 31 KB | BuildDependencies -- def-use chain DAG | HIGH |
| `sub_68B9C0` | 46 KB | MainSchedulingLoop -- combined DAG + scheduling | HIGH |
| `sub_692200` | 18 KB | SchedulingHeuristic -- priority with FP scoring | HIGH |
| `sub_695530` | 15 KB | ComputeLatencies -- instruction latency computation | HIGH |
| `sub_69B7D0` | 17 KB | TopologicalSort -- valid execution ordering | HIGH |
| `sub_69F170` | 12 KB | CriticalPathAnalysis -- DAG critical path | HIGH |
| `sub_893100` | 17 KB | ClassifyInstruction -- opcode/operand analysis | HIGH |
| `sub_894290` | 27 KB | BuildOperandDependencies -- operand-level edges | HIGH |
| `sub_89C550` | 14 KB | InnerScheduleLoop -- inner scheduling iteration | HIGH |
| `sub_89EFC0` | 16 KB | ReadyListManager -- BST management | HIGH |
| `sub_8A9D80` | 21 KB | DepthFirstSchedule | MEDIUM |
| `sub_8AB750` | 9.8 KB | CriticalPathCompute | MEDIUM |
| `sub_8B1190` | 16 KB | ScheduleWithBacktrack | MEDIUM |
| `sub_8B2D90` | 18 KB | GlobalScheduleOpt -- cross-BB scheduling | MEDIUM |
| `sub_8B4590` | 13 KB | PermuteSchedule -- permutation search | MEDIUM |
| `sub_8B5400` | 14 KB | ScheduleForLatency | MEDIUM |
| `sub_8B6D60` | 12 KB | ScheduleForPressure | MEDIUM |
| `sub_8B77C0` | 15 KB | DualIssueScheduler | MEDIUM |
| `sub_8B8900` | 12 KB | TensorScheduler | MEDIUM |
| `sub_8B9390` | 23 KB | SoftwarePipeline | MEDIUM |
| `sub_8BAAE0` | 15 KB | LoopScheduler | MEDIUM |
| `sub_8BB9C0` | 8.2 KB | PrefetchScheduler | MEDIUM |
| `sub_8BC0B0` | 6.1 KB | BarrierCoalescence | MEDIUM |
| `sub_8BC990` | 7.6 KB | ScoreboardOpt | MEDIUM |
| `sub_8BCFA0` | 6.8 KB | WarpScheduleOpt | MEDIUM |
| `sub_8BDC40` | 7.9 KB | DualIssuePairing | MEDIUM |
| `sub_8BE320` | 25 KB | ComplexSchedulePass | MEDIUM |
| `sub_8C67A0` | 3.7 KB | ComputeResourceCost -- per-instruction FU cost | HIGH |
| `sub_8C7120` | -- | BarrierTrackingUpdate | MEDIUM |
| `sub_8C7290` | 5.1 KB | GetResourceVector -- SSE-optimized copy | HIGH |
| `sub_8C7720` | 20 KB | ReorderInstructions -- red-black tree | HIGH |
| `sub_8C9320` | 47 KB | **ComputePriority** -- 8-bit packed heuristic | HIGH |
| `sub_8CBAD0` | 2.9 KB | PreScheduleSetup -- BB scan, 4095-instr limit | HIGH |
| `sub_8CCF80` | 2.3 KB | IsLongLatencyOp -- latency > 19 check | HIGH |
| `sub_8CD160` | 9.3 KB | ScheduleBasicBlock -- per-BB ordering loop | HIGH |
| `sub_8CF880` | 28 KB | BuildDependencyGraph -- pre-scan stage 1 | HIGH |
| `sub_8D0640` | 22 KB | ScheduleInstructions -- top-level orchestrator | HIGH |
| `sub_8D1730` | 19 KB | ExecuteSchedulePass | HIGH |
| `sub_8D2510` | 3.6 KB | UpdateDependencies -- post-schedule dep update | HIGH |
| `sub_8D3150` | 2.0 KB | CheckResourceConflict | MEDIUM |
| `sub_8D32D0` | 14 KB | ScheduleInstruction -- schedule single instruction | HIGH |
| `sub_8D3D60` | 1.4 KB | InsertStall | HIGH |
| `sub_8D3E20` | 2.1 KB | ComputeStallCycles | HIGH |
| `sub_8D4000` | 3.0 KB | InsertBarrier | HIGH |
| `sub_8D5E00` | 38 KB | MainSchedulingLoop -- workhorse | HIGH |
| `sub_8D7760` | 41 KB | StallAndBarrierInsertion -- control word generation | HIGH |
| `sub_8D9930` | 19 KB | BuildDependencyEdges -- RAW/WAR/WAW/memory/barrier | HIGH |
| `sub_925510` | 341 bytes | MoveInstruction -- doubly-linked list relink | HIGH |
| `sub_A08A00` | -- | ResourceModel -- FU cost query (3 modes) | HIGH |
| `sub_A091C0` | -- | InitResourceTracking | MEDIUM |
| `sub_A09530` | 365 bytes | UpdateStallCycles -- per-instruction latency update | HIGH |
| `sub_A09D40` | -- | UpdateWARTracking -- anti-dependency tracking | MEDIUM |
| `sub_A9DDD0` | 11.5 KB | HandleLargeFunction -- chunk-based scheduling | MEDIUM |

## Per-SM Scheduling Backends

Everything documented above describes the **main scheduler** (Backend A), which covers approximately 436 KB at 0x680000--0x8FE000. ptxas contains two additional complete scheduling implementations activated for newer SM architectures. The three backends coexist in the binary; SM-version-gated dispatch selects which combination runs.

### Architecture Dispatch

The function `sub_7DDB50` reads an SM architecture index from `context+2104` and returns it as an integer. Four dispatch stubs in the 0xC5FE00--0xC61000 range use this value to select the scheduling backend:

| Dispatch Stub | Condition | Backend Selected | Pipeline Stage |
|---|---|---|---|
| `sub_C5FEF0` | `SmVersion > 1` | Backend B (SM89/90 Codec) | Codec/ISel scheduling |
| `sub_C60910` | `SmVersion > 1 && (context+1392 & 1)` | Backend B (SM89/90 Codec) | Codec/ISel scheduling |
| `sub_C5FFC0` | `SmVersion > 1` | Backend C (RBT List), mode 1 | Pre-scheduling |
| `sub_C5FFF0` | `SmVersion > 1` | Backend C (RBT List), mode 0 | Post-scheduling |

When `SmVersion <= 1` (sm_50 through sm_75 -- Maxwell through Volta), control falls through to the main Backend A documented in the preceding sections. When `SmVersion >= 2` (sm_80+ -- Ampere, Ada Lovelace, Hopper, Blackwell), Backends B and C replace Backend A entirely.

`sub_C60910` has a secondary activation path: if `*(options + 23544) == 1 && *(options + 23552) != 0`, Backend B activates regardless of SM version, providing a knob override for testing the codec scheduler on older architectures.

Backends B and C are **complementary, not competing**. Backend C handles pre-scheduling and post-scheduling (the same pipeline stages as Backend A's 3-phase ReduceReg/ILP/DynBatch), while Backend B handles a separate codec/ISel scheduling step that has no equivalent in the legacy path.

### Backend B -- SM89/90 Codec Scheduler (0x1225000)

Backend B is a forward-then-backward scheduling pass with continuous floating-point priority weighting. It replaces Backend A's discrete 8-bit packed heuristic with a configurable pressure/ILP tradeoff expressed as doubles.

| | |
|---|---|
| **Entry** | `sub_1233D70` (1,527 B, 321 lines) -- pass phase 5 |
| **Forward scheduler** | `sub_122AD60` (17.5 KB, 4,118 lines) -- largest function in range |
| **Backward scheduler** | `sub_122F650` (18.2 KB, 3,917 lines) |
| **Preparation** | `sub_123E0D0` -- instruction characterization |
| **Post-fixup** | `sub_A112C0` -- scheduling result finalization |
| **Priority structure** | BST with FNV-1a hash tracking |
| **Code region** | 0x1225000--0x1240000 (132 functions, 111 KB) |

#### Float Weighting System

The entry point `sub_1233D70` initializes two pairs of floating-point weights from the options object at `*(context+1664) + 72`:

**Pair 1 -- Pressure/ILP tradeoff** (options offsets 7200/7208):

| Weight | Default | Meaning |
|---|---|---|
| `pressure_weight` | **1.8** | Contribution of register pressure to scheduling priority. Positive = favors orderings that reduce live register count. |
| `ilp_weight` | **-0.8** | Contribution of instruction-level parallelism. Negative = penalizes moves that reduce available parallelism. |

The two weights sum to 1.0 and form a weighted combination on a unit scale. The default 1.8/-0.8 split heavily favors register pressure reduction, accepting moderate ILP degradation -- appropriate for register-hungry Ada Lovelace and Hopper kernels.

**Pair 2 -- Secondary scoring axis** (options offsets 7560/7568):

| Weight | Default | Meaning |
|---|---|---|
| `forward_weight` | **3.2** | Forward-looking scheduling contribution |
| `backward_penalty` | **-2.2** | Backward-looking penalty factor |

Both pairs are overridable. When the configuration byte at the respective offset equals 3, the weight is read from the adjacent double field and the complement is computed as `1.0 - weight`:

```c
if (*(BYTE*)(options + 7200) == 3):
    pressure_weight = *(double*)(options + 7208)
    ilp_weight = 1.0 - pressure_weight
```

After loading, both weight pairs are normalized by dividing by the register range `(float)(max_regs - min_regs)`, producing per-register slopes:

```c
range = (float)(max_regs) - (float)(min_regs)
pressure_slope = ilp_weight / range
secondary_slope = backward_penalty / range
```

This normalization ensures the scheduling heuristic scales consistently regardless of the target architecture's register file size.

#### Priority Score Formula

Both the forward and backward passes evaluate candidate instructions using the same weighted quadratic score. Given a candidate whose scheduling would result in register pressure `P` (an integer from `popcount` of the live-register bitvector):

```text
score(P) = P * (pressure_slope * (P - min_regs) + pressure_weight)
```

where:
- `P` = live register count after tentatively scheduling the candidate (int, converted to double)
- `min_regs` = `GetMinRegs()` from the target profile (vtable+720), stored as `(double)min_regs`
- `pressure_weight` = Pair 1 first weight (default **1.8**)
- `pressure_slope` = `ilp_weight / range` (default **-0.8** / range)
- `range` = `(double)max_regs - (double)min_regs` where `max_regs` comes from vtable+768

Expanding the inner expression:

```text
score(P) = P * ( ilp_weight * (P - min_regs) / range + pressure_weight )
```

With defaults (1.8 / -0.8, range = max_regs - min_regs):

```text
score(P) = P * ( -0.8 * (P - min_regs) / range + 1.8 )
```

The backward scheduler uses the secondary pair identically for a second acceptance gate:

```text
score2(P) = floor( P * (secondary_slope * (P - min_regs) + forward_weight) )
         = floor( P * (backward_penalty / range * (P - min_regs) + forward_weight) )
```

With defaults (3.2 / -2.2):

```text
score2(P) = floor( P * ( -2.2 * (P - min_regs) / range + 3.2 ) )
```

Both scores serve as **acceptance thresholds** during list scheduling. After computing `score(P)`, the scheduler evaluates `depth_cost = popcount_sum(live_bv_tree)` via `sub_1229BD0` (BST walk accumulating `__popcnt` across bitvector nodes). A candidate is accepted if:

```text
Pair 1:  P + depth_cost                 > score(P)      // forward pass (sub_122AD60)
Pair 2:  P + depth_cost + accum_count   > score2(P)     // backward pass (sub_122F650)
```

The guard condition `P > min_regs` short-circuits evaluation: when pressure is at or below the minimum, the instruction is accepted unconditionally. This makes the score a pressure *penalty* that only activates when the register file is under contention. Binary evidence: the score computation appears at `0x123320f` (Pair 1) and `0x1233a4b` (Pair 2) in `sub_122F650`, as the sequence `cvtsi2sd / subsd [r13+off] / mulsd [r13+off] / addsd [r13+off] / mulsd`, followed by `call _floor` for Pair 2 only.

#### Forward Pass (sub_122AD60)

The forward scheduler implements list scheduling with a BST priority queue, iterating basic blocks front-to-back. It uses FNV-1a hash tables (seed 0x811C9DC5, multiplier 16777619) for tracking scheduled instruction mappings. Instruction properties are queried via `sub_7DF3A0`. The function manages a ref-counted working set with proper cleanup at function exit. The Pair 1 score is evaluated inline via the stored `pressure_weight`, `min_regs`, and `pressure_slope` fields in the scheduling context at offsets +112, +104, and +120 respectively. At 4,118 decompiled lines, it is the largest function in the 0x1225000 scheduling range.

#### Backward Pass (sub_122F650)

The backward scheduler receives `range` and `secondary_slope` as direct double parameters (a2, a3) and processes basic blocks in reverse order. It evaluates both Pair 1 and Pair 2 scores, using the scheduling context fields at offsets +136/+144/+152 for the secondary pair. It calls into the barrier/scoreboard system (`sub_BDC080`, `sub_BDBA60`, `sub_BDC0A0`) and performs register liveness analysis via `sub_A0EDE0`. The function uses BST operations with left/right/parent pointer traversal and explicit rebalancing, then performs iterative tree cleanup at exit.

### Backend C -- RBT List Scheduler (0x18CD000)

Backend C is a complete reimplementation of the list scheduling algorithm using a red-black tree priority queue, double-precision scoring, and an evaluate-then-commit model with hash-table solution caching. It replaces Backend A for all sm_80+ targets.

| | |
|---|---|
| **Orchestrator** | `sub_1908D90` -- pre/post mode dispatch (`mode=1` for pre-RA via `sub_C5FFC0`, `mode=0` for post-RA via [phase 110 `PostSchedule`](post-schedule.md) and side-channel `sub_C5FFF0`) |
| **Driver** | `sub_1906090` -- per-block scheduling loop |
| **Core scheduler** | `sub_1902B70` (19 KB) -- RBT-based list scheduling |
| **Solution evaluator** | `sub_1904B70` (26 KB) -- constraint check + commit |
| **Constraint validator** | `sub_19043F0` (10 KB) -- feasibility testing |
| **Pressure cost model** | `sub_18F3CB0` (16 KB) -- register-pressure cost model |
| **Recursive cost propagation** | `sub_18FFD70` (23 KB) -- call-graph-aware scoring |
| **Dependency update** | `sub_1902100` (15 KB) -- post-scheduling DAG update |
| **RBT insert** | `sub_18FD370` -- balanced insertion with 3-key comparison |
| **RBT extract** | `sub_18FCDA0` -- pop highest-priority node |
| **RBT reset** | `sub_18F7EC0` -- tree cleanup |
| **Score computation** | `sub_18FDAF0` -- double-precision weighted score |
| **Hash table** | `sub_1906510` (14 KB) -- FNV-1a instruction ID lookup |
| **Code region** | 0x18CD000--0x190FFFF (392 functions, 275 KB) |

#### Red-Black Tree Priority Queue

The critical difference from Backend A is the priority queue data structure. Backend A uses a sorted singly-linked list (O(N) insertion per instruction). Backend C uses a red-black tree that maintains balance through rotation operations in `sub_18FD170` (called at the end of every insertion).

Each RBT node is 40 bytes allocated from a pool, with `node+24` pointing to the instruction's scheduling entry. The tree ordering uses a **three-key comparison** in `sub_18FD370`:

1. **Priority integer** at `scheduling_entry + 384` (descending -- higher priority nodes are left children)
2. **Latency double** at `scheduling_entry + 368` (descending -- higher latency scheduled first among equal-priority instructions)
3. **Instruction ID** at `*(scheduling_entry + 16) + 12` (ascending -- deterministic tiebreaker)

This three-key comparison provides O(log N) insertion and extraction, a significant improvement for basic blocks with hundreds of instructions where Backend A's O(N) sorted insertion becomes a bottleneck.

#### RBT Score Computation (sub_18FDAF0)

`sub_18FDAF0` produces the double at `scheduling_entry+368` (key 2 in the RBT comparison). It accumulates three components: per-operand pressure cost, an excess live-range penalty, and dependency-depth weighting.

```c
function ComputeRBTScore(this):                         // sub_18FDAF0
    ctx        = *(this + 0)
    latency_wt = *(double*)(ctx + 472)                  // per-context latency weight
    // -- Phase 1: pressure cost (stored at this+376) --
    pressure_cost = 0.0
    for each node in linked_list(this + 32):            // operand entries
        operand  = *(node + 16)
        base     = PressureCost(ctx, operand, latency_wt)  // sub_18FD7A0
        scale    = base
        last_dep = FindLastRegDep(operand)              // walk operand+84, skip pred/imm
        if last_dep is negative-encoded register ref:
            scale = base * *(double*)(ctx + 448)        // damping weight
        vtable = *(*(ctx + 8) + 1784)
        if (**vtable)(vtable, operand + 84, last_dep):  // arch-specific eligibility
            scale = LatencyScale(ctx, sched_class_entry) * scale  // sub_18F5460
        pressure_cost += scale
    // -- Phase 2: excess live-range penalty --
    mode = *(int*)(this + 200)
    if mode != 2:
        live_count = 0
        for each node in linked_list(this + 160):
            e = *(node + 16)
            if *(int*)(e + 20) > 1 and (*(byte*)(e + 48) & 0x40) == 0:
                live_count++
        excess = live_count - *(int*)(this + 264) + 2
        if excess > 0 and excess / (mode - 2) > *(double*)(ctx + 464):
            pressure_cost *= *(double*)(ctx + 456)      // penalty multiplier
    // -- Phase 3: dependency-depth weighting --
    dep_score = 0.0;  max_conflict_lat = 1.0
    if *(int*)(this + 288) != 0:                        // has dependency hash map
        for each dep_entry in HashMapIter(this + 280):  // sub_18F4D30
            lat = LatencyScale(ctx, class_of(dep_entry))
            // Binary search sorted 12-byte conflict array at *(this+8)+224
            // (uses 0xAAAAAAAAAAAAAAAB multiply for div-by-3 index computation)
            if BinarySearchConflict(array, *(int*)(dep_entry + 52)):
                max_conflict_lat = fmax(max_conflict_lat, lat)
            dep_score += lat * pressure_cost
    if *(byte*)(this + 364):                            // long-latency flag
        dep_score += max_conflict_lat * 10.0
    *(double*)(this + 368) = dep_score                  // final RBT score (key 2)
    return dep_score
```

`PressureCost` (`sub_18FD7A0`) counts live non-predicate register operands and returns `count * *(double*)(ctx+480)`, plus opcode-specific bonuses: 88/89 (TEX/TLD) +5.0, 44/45 (LD/ST) +0.5/+1.0, 86 +5.0, 195 (LDG) +6.0, 288 (ATOMS) +0.4*latency_weight, 130/137 +1.0 if no cross-dep. `LatencyScale` (`sub_18F5460`) queries the vtable at `ctx+8->+1784` for a normalized latency ratio of the scheduling class.

#### Core Scheduling Loop (sub_1902B70)

```c
function RBTListSchedule(context, block, dep_info, bound, constraint):
    InitRegisterPressure(context, block)           // sub_18F8580
    InitRBTree(tree)                               // sub_18F7EC0

    for each instruction in block.instruction_list:
        node = AllocPoolNode(40 bytes)
        node.scheduling_entry = instruction
        RBTreeInsert(tree, node)                   // sub_18FD370

    ResizeScheduleArray(block, tree.count)          // sub_18F9CC0

    while tree is not empty:
        best_node = RBTreeExtractMax(tree)          // sub_18FCDA0
        ReturnNodeToPool(best_node)

        instruction = best_node.scheduling_entry
        valid = vtable_check(context, block, instruction)
        *(instruction + 365) = valid

        UpdateDependencies(context, instruction, tree)  // sub_1902100

        if not valid:
            InsertRejection(block + 112, instruction_id)
            continue

        // Record scheduling decision
        position = ++(block + 360)
        entry = *(block + 352) + 24 * position
        entry[0] = instruction_id
        entry[1] = instruction + 2         // scheduling state
        entry[2] = instruction             // back-pointer

        // Compute and accumulate scores
        latency = LookupLatency(context, instruction)  // sub_18F5460
        *(block + 96) += (priority - 2) * latency
        *(block + 88) += latency * *(instruction + 376)

        // Process successors, check conflicts (binary search on
        // sorted 12-byte conflict array with 0xAAAAAAAAAAAAAAAB
        // division-by-3 trick for index computation)
```

#### Evaluate-Then-Commit Model

Backend A uses a greedy approach: each scheduling decision is final. Backend C introduces a two-phase model where `sub_1904B70` evaluates a proposed schedule against constraints before committing it:

1. Build a candidate schedule (408-byte evaluation nodes with def/use/pred-deps/succ-deps lists)
2. Validate via `sub_19043F0` (checks scheduling mode at `+64` must be 5 or 6)
3. Run architecture-specific check via vtable at `*context + 16`
4. Verify register pressure via `sub_19016E0`
5. Compute score via `sub_18FDAF0` (returns double)
6. Apply threshold gate: if score meets the threshold, cache in solution hash table at `block + 304`; otherwise reject into the block's rejection set at `instr + 112`

This allows Backend C to explore multiple scheduling alternatives and commit only the best-scoring solution, a capability Backend A's greedy model lacks.

##### Threshold Gate (`context+360`, `context+368`)

The threshold mechanism controls which evaluated solutions proceed to hash table caching versus immediate rejection. Two fields on the scheduling context govern the gate:

| Offset | Type | Initial | Meaning |
|---|---|---|---|
| `context+360` | double | 0.0 | `best_threshold` -- score ceiling for acceptance |
| `context+368` | byte | 0 | `threshold_active` -- gate enable flag |

`sub_18FEE60` (context constructor) initializes both to zero. The gate logic in `sub_1904B70` is:

```c
function EvaluateAndCache(context, block, ...):           // sub_1904B70, line 820+
    candidate = *(block + 168)                            // current evaluation node
    sched_entry = candidate + 16                          // scheduling entry within node

    // --- 4-step validation ---
    if not ConstraintCheck(context, sched_entry, bound):  return   // sub_19043F0
    if not VtableArchCheck(context, sched_entry):         return   // *context+16
    if not PressureCheck(context, sched_entry):           return   // sub_19016E0

    score = ComputeRBTScore(sched_entry)                  // sub_18FDAF0 -> double

    // --- threshold gate ---
    if !context.threshold_active OR score > context.best_threshold:
        // REJECT: score too high (lower is better) or gate disabled
        InsertRejection(candidate.instr + 112, instr_id)  // sub_122F1E0
        return

    // ACCEPT: score <= threshold, insert into solution hash table
    SolutionHashInsert(block, candidate, sched_entry)
```

At initialization, `threshold_active = 0`, so `!0 = true` short-circuits the OR and all candidates are rejected. The threshold is activated externally (via vtable callback or iterative refinement pass) by setting `context+368 = 1` and writing a ceiling score into `context+360`. Once active, only solutions with `score <= best_threshold` pass through to the hash table. Lower scores are better -- the threshold acts as a ceiling that tightens as the scheduler discovers improving solutions.

##### Solution Hash Table (`block+304`)

Each per-block evaluation record (368 bytes, iterated at stride 368 by the driver `sub_1906090`) contains a chained hash table for caching accepted solutions. The hash table layout occupies offsets +304 through +336:

| Offset | Type | Content |
|---|---|---|
| `block+304` | ptr | Free-list head for 32-byte hash chain nodes |
| `block+312` | int32 | `total_entries` -- total distinct keys inserted |
| `block+316` | int32 | `total_chain_length` -- sum of per-bucket chain lengths |
| `block+320` | ptr | `buckets` -- pointer to array of 24-byte bucket headers |
| `block+328` | int64 | `capacity` -- number of bucket slots (power of 2) |

Each 24-byte bucket header contains: `{chain_head (ptr), chain_tail (ptr), chain_count (int32)}`. Chain nodes are 32 bytes: `{next (ptr), key (int32), value (ptr), hash (int32)}`.

The hash function is FNV-1a on the 4-byte instruction scheduling class ID (at `sched_entry+8`):

```c
function FNV1a_32(key):                                   // inline in sub_1904B70
    h = 0x811C9DC5                                        // FNV offset basis
    for byte in key[0..3]:
        h = (h ^ byte) * 16777619                         // 0x01000193 = FNV prime
    return h

function SolutionHashInsert(block, candidate, sched_entry):
    key = *(int32*)(sched_entry + 8)                      // scheduling class ID
    hash = FNV1a_32(key)
    buckets = *(block + 320)
    capacity = *(block + 328)
    if !buckets:
        InitHashTable(block + 304, initial_size=8)        // sub_18FBD20
        buckets = *(block + 320)
        capacity = *(block + 328)

    slot = hash & (capacity - 1)
    bucket = buckets + 24 * slot
    node = bucket.chain_head

    // --- probe chain for existing key ---
    while node:
        if *(int32*)(node + 8) == key:
            // HIT: update existing entry's scheduling pointer
            *(node + 16) = sched_entry
            return
        node = *node

    // --- MISS: allocate and insert new 32-byte node ---
    node = FreeListPop(block + 304)                       // or arena alloc 32 bytes
    node.next = 0
    *(int32*)(node + 8) = key
    *(ptr*)(node + 16) = sched_entry
    *(int32*)(node + 24) = hash
    PrependToBucket(bucket, node)

    // --- update statistics ---
    bucket.chain_count++
    *(block + 316) += bucket.chain_count                  // total_chain_length
    *(block + 312)++                                      // total_entries

    // --- rehash if load exceeds threshold ---
    if total_chain_length > total_entries AND total_entries > capacity / 2:
        Resize(block + 304, 4 * capacity)                 // sub_18FBD20
```

The separate function `sub_1906510` (14 KB) provides the same hash-table-plus-BST lookup interface used by the cross-block scheduling path. It combines the FNV-1a hash chain with a red-black tree at `*(context_base + 312)` that orders solutions by a bitvector comparison (`sub_19061B0`), enabling efficient retrieval of the best cached solution across multiple evaluation blocks.

#### Recursive Cost Propagation (sub_18FFD70)

Backend C uniquely supports cross-function scheduling awareness through recursive cost propagation. `sub_18FFD70` walks the call graph:

1. For a given schedule entry, iterate predecessor blocks (linked list at +12/+13)
2. Look up each predecessor in the scheduling map via `sub_18F4E70`
3. If predecessor is live (byte at +365), recursively process it
4. After recursion, scan instruction operand lists (offsets 80, 84), identifying register operands by the type tag `(operand >> 28) & 7 == 1`
5. Clear register usage bits at `reg_entry + 28`
6. Update double-precision scores at offsets 88 and 96

This propagation allows scheduling decisions in callee functions to influence caller scheduling priorities -- a form of interprocedural scheduling absent from both Backend A and Backend B.

### Backend Comparison

| Feature | Backend A (Main) | Backend B (SM89/90 Codec) | Backend C (RBT List) |
|---|---|---|---|
| Address range | 0x680000--0x8FE000 | 0x1225000--0x1240000 | 0x18CD000--0x190FFFF |
| Code size | ~436 KB | ~111 KB | ~275 KB |
| SM gate | `SmVersion <= 1` | `SmVersion >= 2` | `SmVersion >= 2` |
| Pipeline stage | Pre + post scheduling | Codec/ISel scheduling | Pre + post scheduling |
| Priority encoding | 8-bit packed integer | Float-weighted BST | RB-tree (int + double + ID) |
| Priority function size | 47 KB monolithic | Distributed across weights | 3-key comparison |
| Ready list structure | Sorted singly-linked list | Binary search tree | Red-black tree |
| Insertion complexity | O(N) per instruction | O(log N) | O(log N) |
| Scheduling passes | 3 (ReduceReg / ILP / DynBatch) | 2 (Forward / Backward) | 2 (Pre / Post) |
| Pressure tracking | Bitvector + popcount | Float slope per register | Bitmap + scalar cost model |
| Weight configuration | Knobs 769--805 (integer) | Options 7200/7560 (double) | Vtable dispatch |
| Score type | Integer (packed bits) | Double (weighted sum) | Double (accumulated) |
| Solution search | Greedy (single pass) | Forward + backward | Evaluate + commit |
| Cross-function awareness | None | None | Recursive cost propagation |
| Hash infrastructure | None | FNV-1a | FNV-1a |
| Backtracking | Optional (sub_8B1190) | None | Rejection set + retry |

### Backend B + C Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_1233D70` | 1.5 KB | SM89/90 CodecScheduleEntry -- pass phase 5, float weight init | HIGH |
| `sub_122AD60` | 17.5 KB | ForwardCodecScheduler -- BST list scheduling, FNV-1a hash tracking | HIGH |
| `sub_122F650` | 18.2 KB | BackwardCodecScheduler -- reverse pass, barrier/scoreboard integration | HIGH |
| `sub_123ADD0` | 5.8 KB | CodecDependencyGraphBuilder -- dispatched via vtable | MEDIUM |
| `sub_12371D0` | 3.8 KB | CodecInstructionClassifier -- convergence-based property testing | MEDIUM |
| `sub_123E0D0` | -- | CodecSchedulePreparation -- instruction characterization | MEDIUM |
| `sub_A112C0` | -- | CodecSchedulePostFixup -- result finalization | MEDIUM |
| `sub_1908D90` | -- | RBTScheduleOrchestrator -- pre/post mode dispatch (mode 1 = pre-RA, mode 0 = post-RA; entered through `sub_C5FFC0`, `sub_C5FFF0`, or sub-target vtable+0x90 at phase 110) | HIGH |
| `sub_1906090` | -- | RBTScheduleDriver -- per-block loop, 368-byte block stride | HIGH |
| `sub_1902B70` | 19 KB | RBTCoreListScheduler -- RB-tree priority queue loop | HIGH |
| `sub_1904B70` | 26 KB | RBTSolutionEvaluator -- constraint check, score threshold, hash commit | HIGH |
| `sub_19043F0` | 10 KB | RBTConstraintValidator -- mode 5/6 feasibility | HIGH |
| `sub_19038E0` | 15 KB | RBTInitialEvaluation -- per-block constraint bootstrapping | MEDIUM |
| `sub_18F3CB0` | 16 KB | RBTPressureCostModel -- register-pressure cost model | HIGH |
| `sub_18FFD70` | 23 KB | RBTRecursiveCostPropagation -- call-graph-aware scoring | HIGH |
| `sub_1902100` | 15 KB | RBTDependencyUpdate -- post-scheduling DAG maintenance | HIGH |
| `sub_18FD370` | -- | RBTreeInsert -- 3-key balanced insertion + fix-up | HIGH |
| `sub_18FCDA0` | -- | RBTreeExtractMax -- pop highest-priority node | HIGH |
| `sub_18F7EC0` | -- | RBTreeReset -- tree cleanup | HIGH |
| `sub_18F8580` | -- | RBTRegisterPressureInit -- pressure state initialization | MEDIUM |
| `sub_18F5460` | -- | RBTLatencyLookup -- vtable-dispatched latency query | MEDIUM |
| `sub_18FDAF0` | -- | RBTScoreComputation -- double-precision weighted score | HIGH |
| `sub_1906510` | 14 KB | RBTHashLookup -- FNV-1a instruction ID hash table | HIGH |
| `sub_18FB850` | -- | RBTHashResize -- power-of-2 growth, 0.5 load factor | HIGH |
| `sub_1901200` | -- | RBTScorePropagationDriver -- calls sub_18FFD70 | MEDIUM |
| `sub_19081F0` | 17 KB | RBTBlockDependencyGraphBuild -- per-block DAG construction | HIGH |
| `sub_19072F0` | 14 KB | RBTInterBlockScheduling -- cross-BB register dependency | MEDIUM |
| `sub_18FEE60` | -- | RBTScheduleStateCreate -- 528-byte state construction | MEDIUM |
| `sub_18FE320` | -- | RBTScheduleDataPrepare -- pre-scheduling data setup | MEDIUM |
| `sub_18F94C0` | -- | RBTCleanup -- state teardown | MEDIUM |
| `sub_C5FFC0` | -- | DispatchPreSchedule -- SM gate -> Backend C (mode 1) | CERTAIN |
| `sub_C5FFF0` | -- | DispatchPostSchedule -- SM gate -> Backend C (mode 0) | CERTAIN |
| `sub_C5FEF0` | -- | DispatchCodecSchedule -- SM gate -> Backend B | CERTAIN |
| `sub_C60910` | -- | DispatchConditionalCodecSchedule -- SM gate + knob override | CERTAIN |
| `sub_7DDB50` | -- | GetSmVersionIndex -- reads context+2104 | HIGH |

## Scheduling Guidance Output

After scheduling completes, ptxas can emit statistics comments into the SASS output and DUMPIR stream. Three emitter functions produce scheduling guidance in different contexts, all reading from a shared ~1400-byte statistics object. `sub_A46CE0` controls the "SCHEDULING GUIDANCE:" header that wraps per-block scheduling output. `sub_A3A7E0` emits per-function statistics as `# [field=value]` comment lines during DUMPIR. Eight post-regalloc clones at `sub_ABBA50`--`sub_ABEB50` emit a variant with hardware pipe names.

### Verbosity Controls

Two independent verbosity mechanisms gate the output:

**Scheduling guidance level** at `*(DWORD*)(vtable + 992)`:

| Level | Behavior |
|---|---|
| 0 | No scheduling guidance output |
| 1+ | `"SCHEDULING GUIDANCE:"` header emitted; per-block scheduling dispatched |
| 2+ | Pre-formatting hook called via `vtable+816` before header emission |
| 4+ | `"LOOP STATIC METRICS : "` sub-header appended |

**DUMPIR detail bits** at `context+1416`:

| Bit | Mask | Behavior |
|---|---|---|
| 3 | `0x08` | Enable detailed statistics (FP16 vectorization, functional unit breakdown, throughput estimates) |
| 4 | `0x10` | Show worst-case latency: `# [worstcaseLat=%f]` |
| 5 | `0x20` | Show average-case latency: `# [avgcaseLat=%f]` |

Bits 4 and 5 are mutually exclusive -- only one latency variant is emitted.

### Emitter Functions

| Address | Size | Identity | Confidence | Context |
|---|---|---|---|---|
| `sub_A3A7E0` | 1,236 B | Statistics::emitFunctionStats | CERTAIN | Pre-regalloc DUMPIR statistics. 20+ format strings at `0x21EBF76`--`0x21EC3B0`. Uses abstract FU names (fp, half, shared, controlFlow, loadStore). |
| `sub_A46CE0` | 1,793 B | SchedulingGuidance::buildAndEmit | HIGH | Scheduling guidance header + BB classification. Walks BB array at `context+296`, dispatches schedulable blocks to vtable+336. |
| `sub_A4B8F0` | 248 B | StatsEmitter::emitInstrRegStats | HIGH | Binary-embedded metadata. Writes record type 3 (string) into SASS code object at `*(a1+1000) + *(a1+996)`. |
| `sub_ABBA50`--`sub_ABEB50` | 8 x 1,771 B | PostSchedStats::emit (SM-variant) | CERTAIN | Post-regalloc statistics. 8 clones at 0x700 spacing. Format strings at `0x21FA008`--`0x21FA400`. Uses hardware pipe names (adu, alu, cbu, fma, lsu). |

### Pre-Regalloc Output Format (sub_A3A7E0)

Emitted during DUMPIR. All lines prefixed with `# `. Lines marked `[COND]` are gated by the stated condition.

```text
# 142 instructions, 24 R-regs
# [inst=142] [texInst=0] [tepid=0] [rregs=24]
# [urregs=8]                                              [COND: SM > 0x5FFF]
# [_lat2inst=0.0]
# [FP16 inst=0] [FP16 VectInst=0] [Percentage Vectorized=0.00]  [COND: +1416 bit 3]
# [est latency = 87] [LSpillB=0] [LRefillB=0], [SSpillB=0], [SRefillB=0], [LowLmemSpillSize=0] [FrameLmemSpillSize=0]
# [LNonSpillB=0] [LNonRefillB=0], [NonSpillSize=0]
# [Occupancy = 0.750000], [est numDivergentBranches=2] [attributeMemUsage=0], [programSize=1024]
# [est fp=12] [est half=0], [est trancedental=0], [est ipa=0], [est shared=0], [est controlFlow=8], [est loadStore=24]
# [est tex=0] [est pairs=4]
# [issue thru=0.888889] [fp thru=0.111111] [half thru=0.000000], [trancedental thru=0.000000], [ipa thru=0.000000]
# [shared thru=0.000000] [controlFlow thru=0.062500] [texLoadStore thru=0.187500], [reg thru=0.000000], [warp thru=0.000000]
# [SharedMem Alloc thru=0.125000]                         [COND: value != 0.0]
# [partially unrolled loops=0] [non-unrolled loops=1]
# [CB-Bound Tex=0] [UR-Bound Tex=0] [Bindless Tex=0] [Partially Bound Tex=0]
# [UDP inst=0] [numVecToURConverts inst=0]
# [maxNumLiveValuesAtSuspend=0]
# [Precise inst=0]
# [worstcaseLat=87.000000]                                [COND: +1416 bits 4-5 == 0x10]
# [avgcaseLat=52.500000]                                  [COND: +1416 bits 4-5 == 0x20]
# [instHint=142] [instPairs=4]                            [COND: instPairs != 0]
# <custom annotation>                                     [COND: linked list at stats[55] != NULL]
```

Key format details: pre-regalloc uses commas between some bracket groups (`[SSpillB=%d], [SRefillB=%d],`) and abstract functional unit names (fp, half, trancedental, shared, controlFlow, loadStore, texLoadStore). The typo "trancedental" (missing "s") is present in the binary.

### Post-Regalloc Output Format (sub_ABBA50 clones)

Emitted after scheduling by SM-variant clones dispatched via vtable. Same `# ` prefix. Differs from the pre-regalloc format in three ways:

1. **No commas** between bracket groups
2. **SpillSize** replaces `LowLmemSpillSize` + `FrameLmemSpillSize`
3. **Hardware pipe names** replace abstract unit names; MMA variant breakdown added

The unique lines (lines shared with pre-regalloc use the same structure minus commas):

```text
# [est latency = %d] [LSpillB=%d] [LRefillB=%d] [SSpillB=%d] [SRefillB=%d] [SpillSize=%d]
# [LNonSpillB=%d] [LNonRefillB=%d] [NonSpillSize=%d]
# [Occupancy = %f] [est numDivergentBranches=%d] [attributeMemUsage=%d] [programSize=%d]
# [est adu=%d] [est alu=%d] [est cbu=%d] [est fma2x=%d] [est fma=%d] [est half=%d]
# [est trancedental=%d] [est ipa=%d] [est lsu=%d] [est redux=%d]
# [est schedDisp=%d] [est tex=%d] [est ttu=%d] [est udp=%d]
# [est imma16816=%d] [est imma16832=%d] [est immaSp8832=%d] [est immaSp16832=%d]
# [est dmma=%d] [est fma64=%d] [est hmma16816=%d] [est hmma16816f16=%d]
# [est hmma1688=%d] [est hmma1688f16=%d] [est hmmaSp1688=%d] [est hmmaSp1688f16=%d]
# [issue thru=%f] [adu thru=%f] [alu thru=%f] [cbu thru=%f] [fma2x thru=%f] [fma thru=%f]
# [trancedental thru=%f] [ipa thru=%f] [lsu thru=%f] [redux thru=%f]
# [schedDisp thru=%f] [tex thru=%f] [ttu thru=%f] [udp thru=%f]
# [imma16816 thru=%f] [imma16832 thru=%f] [immaSp8832 thru=%f] [immaSp16832 thru=%f]
# [dmma thru=%f] [fma64 thru=%f] [hmma16816 thru=%f] [hmma16816f16 thru=%f]
# [hmma1688 thru=%f] [hmma1688f16 thru=%f] [hmmaSp1688 thru=%f] [hmmaSp1688f16 thru=%f]
# [reg thru=%f] [warp thru=%f]
```

### Hardware Pipe Name Mapping

The post-regalloc format maps abstract functional unit names to hardware execution pipe identifiers:

| Post-Regalloc Pipe | Pre-Regalloc Equivalent | Description |
|---|---|---|
| `adu` | -- | Address Divergence Unit (address computation) |
| `alu` | `fp` | Arithmetic Logic Unit (integer + FP32 combined) |
| `cbu` | `controlFlow` | Control/Branch Unit (branch, exit, barrier) |
| `fma2x` | -- | Double-precision FMA (separate pipe on sm_80+) |
| `fma` | `fp` | Fused Multiply-Add (FP32) |
| `half` | `half` | FP16 operations |
| `lsu` | `loadStore` + `shared` | Load/Store Unit (unified) |
| `redux` | -- | Reduction Unit (warp-level reductions) |
| `schedDisp` | -- | Scheduler Dispatch (internal overhead) |
| `tex` | `tex` | Texture Unit |
| `ttu` | -- | Tensor Texture Unit (Ada Lovelace+) |
| `udp` | -- | Uniform Data Path operations |

### Binary-Embedded Statistics Record (sub_A4B8F0)

Separate from the DUMPIR comment output, `sub_A4B8F0` writes a compact binary record into the SASS code object during emission:

```text
Format string: "instr/R-regs: %d instructions, %d R-regs"
  instructions = stats[335] - stats[341]     (total minus removed)
  R-regs       = stats[159] + stats[102]     (extra + base allocation)

Record layout in output buffer:
  +0  DWORD  type = 3                        (string record type)
  +4  DWORD  string_length
  +8  char[] string_content                  (formatted text)
```

The companion function `sub_A4B9F0` writes record type 2 for undefined register warnings: `"Referencing undefined register: %s%d"`.

### Scheduling Guidance Header (sub_A46CE0)

`sub_A46CE0` emits the scheduling guidance wrapper, then walks the BB array to classify and dispatch blocks for scheduling. The header is emitted into the output stream via `sub_7FE930` (string builder) at `context + 1440`.

**BB classification algorithm:**

For each BB in `context+296` (index 0 through `context+304`):

1. **Schedulable**: `sub_7544D0(context, bb)` returns true AND `sub_754510(context, bb)` returns false. Dispatched immediately to scheduling via `vtable+336`.

2. **Type-8 (deferred)**: `*(bb+16) == 8`. Added to a dynamically-grown `src` array for second-pass processing.

3. **Loop back-edge**: When `*(bb+148) != 0` and `*(bb+128) != NULL`, the function walks the predecessor linked list at `bb+128`. For each predecessor, it checks whether the predecessor's iteration order (`bb+144`) exceeds the current block's, and whether the predecessor's terminal instruction is a branch (opcode `0x5D` after masking with `0xFFFFCFFD`) with a matching program counter at `(instr+84) & 0xFFFFFF`. If a back-edge is detected, scheduling dispatch includes the back-edge source instruction as a hint parameter.

After the first pass, deferred type-8 blocks are processed in a second loop with the same back-edge detection logic.

### Statistics Object Field Map

Both emitter families read from the same ~1400-byte statistics object. The object is accessed as a `float*` array; integer fields use the same DWORD index but read as `int32`.

| Index | Type | Field | Description |
|---|---|---|---|
| 8 | int32 | `est_latency` | Estimated schedule length in cycles |
| 9 | float | `FP16_vectorization_pct` | Fraction of FP16 instructions vectorized |
| 10 | int32 | `worstcase_latency` | Worst-case latency (cast to float for output) |
| 11 | int32 | `avgcase_latency` | Average-case latency (cast to float for output) |
| 12 | int32 | `LSpillB` | Long spill byte count |
| 13 | int32 | `LRefillB` | Long refill byte count |
| 14 | int32 | `SRefillB` | Short refill byte count |
| 15 | int32 | `SSpillB` | Short spill byte count |
| 16 | int32 | `LowLmemSpillSize` | Local-memory low spill allocation |
| 17 | int32 | `FrameLmemSpillSize` | Frame local-memory spill allocation |
| 18 | int32 | `LNonSpillB` | Long non-spill byte count |
| 19 | int32 | `LNonRefillB` | Long non-refill byte count |
| 20 | int32 | `NonSpillSize` | Non-spill allocation size |
| 26 | float | `Occupancy` | Occupancy ratio (0.0--1.0) |
| 27 | int32 | `numDivergentBranches` | Estimated divergent branch count |
| 28 | int32 | `attributeMemUsage` | Attribute memory usage in bytes |
| 29 | int32 | `programSize` | Program binary size in bytes |
| 42 | int32 | `preciseInst` | Count of precise (non-approximate) instructions |
| 44 | int32 | `UDPinst` | Uniform data-path instruction count |
| 45 | int32 | `vecToURConverts` | Vector-to-uniform-register conversion count |
| 49 | int32 | `maxLiveAtSuspend` | Max live register values at suspend points |
| 50 | float | `issue_thru` | Overall issue throughput (fraction of peak) |
| 54 | float | `fp_thru` | FP32 pipe throughput |
| 57 | float | `half_thru` | FP16 pipe throughput |
| 58 | float | `transcendental_thru` | Transcendental function pipe throughput |
| 59 | float | `ipa_thru` | Interpolation pipe throughput |
| 61 | float | `shared_thru` | Shared memory pipe throughput |
| 62 | float | `controlFlow_thru` | Control flow pipe throughput |
| 65 | float | `texLoadStore_thru` | Texture and load/store pipe throughput |
| 84 | float | `reg_thru` | Register throughput |
| 85 | float | `warp_thru` | Warp throughput |
| 86 | float | `sharedMemAlloc_thru` | Shared memory allocation throughput |
| 87 | int32 | `partiallyUnrolledLoops` | Partially unrolled loop count |
| 88 | int32 | `nonUnrolledLoops` | Non-unrolled loop count |
| 89 | int32 | `CBBoundTex` | Constant-bank-bound texture count |
| 90 | int32 | `PartiallyBoundTex` | Partially bound texture count |
| 91 | int32 | `BindlessTex` | Bindless texture count |
| 92 | int32 | `URBoundTex` | Uniform-register-bound texture count |
| 93 | int32 | `SM_architecture_enum` | SM version discriminator (>0x5FFF enables UR stats) |
| 99 | int32 | `uniform_reg_count` | Uniform register count |
| 102 | int32 | `R_reg_base` | Base R-register allocation |
| 159 | int32 | `R_reg_extra` | Extra R-register allocation |
| 303 | int32 | `est_fp` | Estimated FP32 instruction count |
| 306 | int32 | `est_half` | Estimated FP16 instruction count |
| 307 | int32 | `est_transcendental` | Estimated transcendental instruction count |
| 308 | int32 | `est_ipa` | Estimated IPA instruction count |
| 310 | int32 | `est_shared` | Estimated shared memory operation count |
| 311 | int32 | `est_controlFlow` | Estimated control flow operation count |
| 315 | int32 | `est_loadStore` | Estimated load/store instruction count |
| 316 | int32 | `est_tex` | Estimated texture instruction count |
| 334 | int32 | `est_pairs` | Estimated co-issuable instruction pairs |
| 335 | int32 | `total_inst` | Total instruction count (before removal) |
| 336 | int32 | `texInst` | Texture instruction count |
| 337 | int32 | `FP16inst` | FP16 instruction count |
| 338 | int32 | `FP16VectInst` | FP16 vectorized instruction count |
| 339 | int32 | `instHint` | Instruction hint value |
| 340 | int32 | `instPairs` | Instruction pair count (also output gate) |
| 341 | int32 | `removed_inst` | Removed instruction count |
| 342 | int32 | `tepid_inst` | TEPID (texture-pending) instruction count |

## Cross-References

- [Scheduler Overview](overview.md) -- 3-phase architecture, register budget, scheduling knobs
- [Latency Model](latency-model.md) -- per-opcode latency tables, functional unit mapping, architecture profiles
- [Scoreboards & Barriers](scoreboards.md) -- scoreboard encoding, dependency barrier assignment, stall/yield format
- [Register Allocation](../regalloc/overview.md) -- the allocator that the scheduler interacts with
- [Phase Manager](../passes/phase-manager.md) -- how ScheduleInstructions fits in the 159-phase pipeline
- [Knobs](../config/knobs.md) -- the 76 scheduling knobs and the knob query infrastructure
- [GMMA Pipeline](../passes/gmma-pipeline.md) -- GMMA/WGMMA operations targeted by DynBatch
- [DUMPIR Configuration](../config/dumpir.md) -- DUMPIR levels that trigger statistics output
- [Spilling](../regalloc/spilling.md) -- spill metrics (LSpillB, SSpillB) referenced in guidance output
