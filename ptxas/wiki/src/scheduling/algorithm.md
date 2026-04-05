# Priority List Scheduling Algorithm

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

```
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

The mode byte stored at `*(DWORD*)(scheduler+60)` controls which priority weight set the engine uses:

| Mode | Value | Callback | Objective |
|---|---|---|---|
| ReduceReg | 1 | 0x39 | Minimize register pressure. Prioritizes instructions that release registers (last-use operands). |
| ILP/Latency | 0 | 0x49 | Maximize instruction-level parallelism. Prioritizes critical-path and long-latency instructions. |
| DynBatch | 2 | 0x41 | Batch-aware tensor scheduling. Groups GMMA/WGMMA operations for warpgroup cooperation. |

The engine uses vtable dispatch at `*(a1+40)` and `*(a1+48)` for polymorphic pre/post scheduling hooks. This allows each mode to inject custom behavior at scheduling boundaries without modifying the core loop.

## Ready List Construction

`sub_6820B0` (1.5 KB) scans the instruction linked list and collects every instruction with zero unsatisfied dependencies into a sorted ready list.

```
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
| `sub_6818D0` | -- | Register count to hardware-aligned unit conversion. |

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

#### Phase 2: Register Budget Prologue

Before the main loop, the function computes two register budgets from `scheduler+432` (target register count):

```
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

`budget_hw` sets the threshold for bit 4 (pressure overflow). `reduced_hw` provides a tighter threshold used in the critical-path assessment. `queue_depth` (knob 770) limits how many candidates receive full priority evaluation; the rest use cached values from initial insertion.

#### Phase 3: Per-Bit Computation

For each instruction in the ready list, `sub_8C7290` extracts its per-register-class deltas (4 classes: GPR, predicate, address, UGP) and the same-BB flag. Then each priority bit is computed:

**Bit 7 -- Yield-related.** Determined by opcode. Only opcode 39 (YIELD instruction variant) can set this bit. The condition checks the last operand's low 2 bits:

```
if opcode_masked == 39:
    operand_index = operand_count - 1 - ((opcode >> 11) & 2)
    yield_related = (instr[84 + 8*operand_index] & 3) == 0
else:
    yield_related = 0
```

When set, the instruction is a yield boundary marker and receives absolute highest priority regardless of all other heuristics.

**Bit 6 -- Yield flag.** Set only for opcode 96 (CONTROL instruction):

```
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

```
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

```
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

**Bit 3 -- Same-BB preference.** Output parameter from `sub_8C7290`:

```
same_bb = sub_8C7290.output_param_5     // boolean from resource copy
```

Set when the instruction belongs to the currently-scheduled basic block. Instructions imported from other BBs by global scheduling get `same_bb = 0`, reducing their priority relative to local instructions.

**Bit 2 -- Stall-free.** Computed from the earliest-available-cycle field:

```
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

```
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

**Bit 0 -- Tiebreaker (barrier-target).** Read directly from instruction metadata:

```
tiebreaker = metadata[108] & 1      // barrier-target flag
```

Barrier-target instructions (those waiting on a hardware barrier) get bit 0 = 1. Since this is the lowest-priority bit, it only affects ordering when all higher bits are identical. Scheduling barrier targets promptly allows the barrier resource to be retired sooner, freeing scoreboard entries for other instructions.

#### Packed Byte Assembly

The 8 bits are packed into a single byte using shift-and-mask arithmetic:

```
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

1. **Resource vector comparison**: If knob-gated architecture checks pass (SM index > 5 at `context+1704`), a 4-tuple lexicographic comparison of per-register-class resource vectors occurs first. The four classes are compared in order: GPR delta, predicate delta, address delta, UGP delta. The first class that differs determines the winner.

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
   - Contention score (a derived value incorporating register overflow penalty: `contention + 2 * RegToHWUnits(pressure_delta) - pressure_sum_sign`)
   - Slot manager cycles (scheduling cost estimate from `sub_682490`)
   - Earliest available cycle (`metadata+32`)
   - Dependency cycle (`metadata+92`)
   - Latest deadline (`metadata+40`)
   - Register delta magnitude

4. **Positional fallback**: When all heuristic comparisons are tied, the instruction with the higher BB slot (`metadata+24`) wins, preserving original program order.

The multi-stage comparison explains why the packed byte uses non-obvious bit ordering. Bits 4, 6, 1, 2, 5 are checked before bit 7 in the refinement path, even though bit 7 is the MSB. The packed byte enables fast ready-list insertion sort (integer comparison), while the full comparison function provides nuanced selection for the actual scheduling decision.

#### Scheduler State Updates

After selecting the best candidate, the function updates scheduler state:

```
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
if best_cost > 0 AND slotManager[76] > 0:
    if slotManager[140]:
        slotManager[28] += slotManager[44]    // advance base
        slotManager[76] = 0                   // reset count
        slotManager[80] = NULL                // reset anchor
    best.metadata[28] = sub_682490(...)       // recompute latency

// Hot-cold counter update
if hot_cold_active AND winner is cold (sub_A9CF90 returns true):
    scheduler[532] -= 1                       // decrement hot-cold budget
elif hot_flag was set for winner:
    scheduler[523] = 0                        // disable hot-cold tracking
```

The function returns the best instruction pointer and writes the second-best to `*a2` for lookahead scheduling.

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

```
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

SSE intrinsics (`_mm_add_epi32`, `_mm_loadu_si128`) are used throughout for vectorized resource accumulation and copying.

## Register Pressure Tracking

The scheduler tracks register liveness via a bitvector at `scheduler+832`. Each bit represents one register; the pressure is the popcount of the live set.

```
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

The scheduling loop also reads Ori instruction fields directly (not via the SchedNode): `instr+72` (opcode), `instr+80` (operand count), `instr+84` (operand descriptors).

Sentinel values: bbSlot -1 (unscheduled), latency 0x1869F (99999 = infinity).

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

The function contains architecture-variant switches for different barrier models (sm_70 vs sm_80 vs sm_90+). It manages a 32-entry barrier table for tracking active barrier assignments.

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

| Address | Size | Identity |
|---|---|---|
| `sub_6820B0` | 1.5 KB | BuildReadyList -- zero-dep instruction scan |
| `sub_682200` | -- | UnlinkFromReadyList -- remove and update deps |
| `sub_682490` | 14 KB | RegisterPressureAnalyzer -- per-class deltas |
| `sub_6833F0` | 10 KB | InitScheduleRegion -- per-BB setup, knob query |
| `sub_685700` | -- | InitSchedulingState -- loop initialization |
| `sub_685A10` | 11 KB | InstructionBarrierCheck -- opcode analysis |
| `sub_687080` | -- | ReadyListManagementHelper |
| `sub_687410` | -- | ResourceConflictCheck |
| `sub_687FE0` | 12 KB | ScheduleBlock -- per-BB scheduling entry |
| `sub_688DD0` | 20 KB | **ScheduleEngine** -- unified 3-mode core loop |
| `sub_68A690` | 31 KB | BuildDependencies -- def-use chain DAG |
| `sub_68B9C0` | 46 KB | MainSchedulingLoop -- combined DAG + scheduling |
| `sub_692200` | 18 KB | SchedulingHeuristic -- priority with FP scoring |
| `sub_695530` | 15 KB | ComputeLatencies -- instruction latency computation |
| `sub_69B7D0` | 17 KB | TopologicalSort -- valid execution ordering |
| `sub_69F170` | 12 KB | CriticalPathAnalysis -- DAG critical path |
| `sub_893100` | 17 KB | ClassifyInstruction -- opcode/operand analysis |
| `sub_894290` | 27 KB | BuildOperandDependencies -- operand-level edges |
| `sub_89C550` | 14 KB | InnerScheduleLoop -- inner scheduling iteration |
| `sub_89EFC0` | 16 KB | ReadyListManager -- BST management |
| `sub_8A9D80` | 21 KB | DepthFirstSchedule |
| `sub_8AB750` | 9.8 KB | CriticalPathCompute |
| `sub_8B1190` | 16 KB | ScheduleWithBacktrack |
| `sub_8B2D90` | 18 KB | GlobalScheduleOpt -- cross-BB scheduling |
| `sub_8B4590` | 13 KB | PermuteSchedule -- permutation search |
| `sub_8B5400` | 14 KB | ScheduleForLatency |
| `sub_8B6D60` | 12 KB | ScheduleForPressure |
| `sub_8B77C0` | 15 KB | DualIssueScheduler |
| `sub_8B8900` | 12 KB | TensorScheduler |
| `sub_8B9390` | 23 KB | SoftwarePipeline |
| `sub_8BAAE0` | 15 KB | LoopScheduler |
| `sub_8BB9C0` | 8.2 KB | PrefetchScheduler |
| `sub_8BC0B0` | 6.1 KB | BarrierCoalescence |
| `sub_8BC990` | 7.6 KB | ScoreboardOpt |
| `sub_8BCFA0` | 6.8 KB | WarpScheduleOpt |
| `sub_8BDC40` | 7.9 KB | DualIssuePairing |
| `sub_8BE320` | 25 KB | ComplexSchedulePass |
| `sub_8C67A0` | 3.7 KB | ComputeResourceCost -- per-instruction FU cost |
| `sub_8C7120` | -- | BarrierTrackingUpdate |
| `sub_8C7290` | 5.1 KB | GetResourceVector -- SSE-optimized copy |
| `sub_8C7720` | 20 KB | ReorderInstructions -- red-black tree |
| `sub_8C9320` | 47 KB | **ComputePriority** -- 8-bit packed heuristic |
| `sub_8CBAD0` | 2.9 KB | PreScheduleSetup -- BB scan, 4095-instr limit |
| `sub_8CCF80` | 2.3 KB | IsLongLatencyOp -- latency > 19 check |
| `sub_8CD160` | 9.3 KB | ScheduleBasicBlock -- per-BB ordering loop |
| `sub_8CF880` | 28 KB | BuildDependencyGraph -- pre-scan stage 1 |
| `sub_8D0640` | 22 KB | ScheduleInstructions -- top-level orchestrator |
| `sub_8D1730` | 19 KB | ExecuteSchedulePass |
| `sub_8D2510` | 3.6 KB | UpdateDependencies -- post-schedule dep update |
| `sub_8D3150` | 2.0 KB | CheckResourceConflict |
| `sub_8D32D0` | 14 KB | ScheduleInstruction -- schedule single instruction |
| `sub_8D3D60` | 1.4 KB | InsertStall |
| `sub_8D3E20` | 2.1 KB | ComputeStallCycles |
| `sub_8D4000` | 3.0 KB | InsertBarrier |
| `sub_8D5E00` | 38 KB | MainSchedulingLoop -- workhorse |
| `sub_8D7760` | 41 KB | StallAndBarrierInsertion -- control word generation |
| `sub_8D9930` | 19 KB | BuildDependencyEdges -- RAW/WAR/WAW/memory/barrier |
| `sub_925510` | 341 bytes | MoveInstruction -- doubly-linked list relink |
| `sub_A08A00` | -- | ResourceModel -- FU cost query (3 modes) |
| `sub_A091C0` | -- | InitResourceTracking |
| `sub_A09530` | 365 bytes | UpdateStallCycles -- per-instruction latency update |
| `sub_A09D40` | -- | UpdateWARTracking -- anti-dependency tracking |
| `sub_A9DDD0` | 11.5 KB | HandleLargeFunction -- chunk-based scheduling |

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

```
if (*(BYTE*)(options + 7200) == 3):
    pressure_weight = *(double*)(options + 7208)
    ilp_weight = 1.0 - pressure_weight
```

After loading, both weight pairs are normalized by dividing by the register range `(float)(max_regs - min_regs)`, producing per-register slopes:

```
range = (float)(max_regs) - (float)(min_regs)
pressure_slope = ilp_weight / range
secondary_slope = backward_penalty / range
```

This normalization ensures the scheduling heuristic scales consistently regardless of the target architecture's register file size.

#### Forward Pass (sub_122AD60)

The forward scheduler implements list scheduling with a BST priority queue, iterating basic blocks front-to-back. It uses FNV-1a hash tables (seed 0x811C9DC5, multiplier 16777619) for tracking scheduled instruction mappings. Instruction properties are queried via `sub_7DF3A0`. The function manages a ref-counted working set with proper cleanup at function exit. At 4,118 decompiled lines, it is the largest function in the 0x1225000 scheduling range.

#### Backward Pass (sub_122F650)

The backward scheduler receives the floating-point weights as direct parameters and processes basic blocks in reverse order. It calls into the barrier/scoreboard system (`sub_BDC080`, `sub_BDBA60`, `sub_BDC0A0`) and performs register liveness analysis via `sub_A0EDE0`. The function uses BST operations with left/right/parent pointer traversal and explicit rebalancing, then performs iterative tree cleanup at exit.

### Backend C -- RBT List Scheduler (0x18CD000)

Backend C is a complete reimplementation of the list scheduling algorithm using a red-black tree priority queue, double-precision scoring, and an evaluate-then-commit model with hash-table solution caching. It replaces Backend A for all sm_80+ targets.

| | |
|---|---|
| **Orchestrator** | `sub_1908D90` -- pre/post mode dispatch |
| **Driver** | `sub_1906090` -- per-block scheduling loop |
| **Core scheduler** | `sub_1902B70` (19 KB) -- RBT-based list scheduling |
| **Solution evaluator** | `sub_1904B70` (26 KB) -- constraint check + commit |
| **Constraint validator** | `sub_19043F0` (10 KB) -- feasibility testing |
| **Pressure cost model** | `sub_18F3CB0` (16 KB) -- SIMD register pressure |
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

#### Core Scheduling Loop (sub_1902B70)

```
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
6. If score exceeds threshold at `context + 360`, insert into solution hash table at `block + 304`

This allows Backend C to explore multiple scheduling alternatives and commit only the best-scoring solution, a capability Backend A's greedy model lacks.

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
| Pressure tracking | Bitvector + popcount | Float slope per register | SIMD bitmap + cost model |
| Weight configuration | Knobs 769--805 (integer) | Options 7200/7560 (double) | Vtable dispatch |
| Score type | Integer (packed bits) | Double (weighted sum) | Double (accumulated) |
| Solution search | Greedy (single pass) | Forward + backward | Evaluate + commit |
| Cross-function awareness | None | None | Recursive cost propagation |
| Hash infrastructure | None | FNV-1a | FNV-1a |
| Backtracking | Optional (sub_8B1190) | None | Rejection set + retry |

### Backend B + C Function Map

| Address | Size | Identity |
|---|---|---|
| `sub_1233D70` | 1.5 KB | SM89/90 CodecScheduleEntry -- pass phase 5, float weight init |
| `sub_122AD60` | 17.5 KB | ForwardCodecScheduler -- BST list scheduling, FNV-1a hash tracking |
| `sub_122F650` | 18.2 KB | BackwardCodecScheduler -- reverse pass, barrier/scoreboard integration |
| `sub_123ADD0` | 5.8 KB | CodecDependencyGraphBuilder -- dispatched via vtable |
| `sub_12371D0` | 3.8 KB | CodecInstructionClassifier -- convergence-based property testing |
| `sub_123E0D0` | -- | CodecSchedulePreparation -- instruction characterization |
| `sub_A112C0` | -- | CodecSchedulePostFixup -- result finalization |
| `sub_1908D90` | -- | RBTScheduleOrchestrator -- pre/post mode dispatch |
| `sub_1906090` | -- | RBTScheduleDriver -- per-block loop, 368-byte block stride |
| `sub_1902B70` | 19 KB | RBTCoreListScheduler -- RB-tree priority queue loop |
| `sub_1904B70` | 26 KB | RBTSolutionEvaluator -- constraint check, score threshold, hash commit |
| `sub_19043F0` | 10 KB | RBTConstraintValidator -- mode 5/6 feasibility |
| `sub_19038E0` | 15 KB | RBTInitialEvaluation -- per-block constraint bootstrapping |
| `sub_18F3CB0` | 16 KB | RBTPressureCostModel -- SIMD register pressure computation |
| `sub_18FFD70` | 23 KB | RBTRecursiveCostPropagation -- call-graph-aware scoring |
| `sub_1902100` | 15 KB | RBTDependencyUpdate -- post-scheduling DAG maintenance |
| `sub_18FD370` | -- | RBTreeInsert -- 3-key balanced insertion + fix-up |
| `sub_18FCDA0` | -- | RBTreeExtractMax -- pop highest-priority node |
| `sub_18F7EC0` | -- | RBTreeReset -- tree cleanup |
| `sub_18F8580` | -- | RBTRegisterPressureInit -- pressure state initialization |
| `sub_18F5460` | -- | RBTLatencyLookup -- vtable-dispatched latency query |
| `sub_18FDAF0` | -- | RBTScoreComputation -- double-precision weighted score |
| `sub_1906510` | 14 KB | RBTHashLookup -- FNV-1a instruction ID hash table |
| `sub_18FB850` | -- | RBTHashResize -- power-of-2 growth, 0.5 load factor |
| `sub_1901200` | -- | RBTScorePropagationDriver -- calls sub_18FFD70 |
| `sub_19081F0` | 17 KB | RBTBlockDependencyGraphBuild -- per-block DAG construction |
| `sub_19072F0` | 14 KB | RBTInterBlockScheduling -- cross-BB register dependency |
| `sub_18FEE60` | -- | RBTScheduleStateCreate -- 528-byte state construction |
| `sub_18FE320` | -- | RBTScheduleDataPrepare -- pre-scheduling data setup |
| `sub_18F94C0` | -- | RBTCleanup -- state teardown |
| `sub_C5FFC0` | -- | DispatchPreSchedule -- SM gate -> Backend C (mode 1) |
| `sub_C5FFF0` | -- | DispatchPostSchedule -- SM gate -> Backend C (mode 0) |
| `sub_C5FEF0` | -- | DispatchCodecSchedule -- SM gate -> Backend B |
| `sub_C60910` | -- | DispatchConditionalCodecSchedule -- SM gate + knob override |
| `sub_7DDB50` | -- | GetSmVersionIndex -- reads context+2104 |

## Cross-References

- [Scheduler Overview](overview.md) -- 3-phase architecture, register budget, scheduling knobs
- [Latency Model](latency-model.md) -- per-opcode latency tables, functional unit mapping, architecture profiles
- [Scoreboards & Barriers](scoreboards.md) -- scoreboard encoding, dependency barrier assignment, stall/yield format
- [Register Allocation](../regalloc/overview.md) -- the allocator that the scheduler interacts with
- [Phase Manager](../passes/phase-manager.md) -- how ScheduleInstructions fits in the 159-phase pipeline
- [Knobs](../config/knobs.md) -- the 76 scheduling knobs and the knob query infrastructure
- [GMMA Pipeline](../passes/gmma-pipeline.md) -- GMMA/WGMMA operations targeted by DynBatch
