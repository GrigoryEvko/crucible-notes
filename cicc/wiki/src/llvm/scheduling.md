# Instruction Scheduling

> **Prerequisites:** Familiarity with [Register Allocation](register-allocation.md), [NVPTX register classes](../reference/register-classes.md), and the [codegen pipeline](../pipeline/codegen.md). Understanding of the [GPU execution model](../gpu-execution-model.md) (warp scheduling, latency hiding) is essential.

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **Upstream source:** `llvm/lib/CodeGen/MachineScheduler.cpp` (`ScheduleDAGMILive`), `llvm/lib/CodeGen/MachinePipeliner.cpp` (Swing Modulo Scheduler) (LLVM 20.0.0). The MRPA incremental pressure tracker and Texture Group Merge pass are NVIDIA-only with no upstream equivalent.

CICC v13.0 implements three distinct scheduling subsystems: MRPA (Machine Register Pressure Analysis) for incremental pressure tracking during MCSE, a Swing Modulo Scheduling pipeliner for loop bodies, and `ScheduleDAGMILive` for post-RA instruction ordering. All three maintain per-register-class pressure arrays but differ in granularity and update frequency. A texture group merge pass (`sub_2DDE8C0`) acts as a scheduling-adjacent optimization that groups texture load instructions for hardware coalescing.

| | |
|---|---|
| **MRPA incremental tracker** | `sub_2E5A4E0` (primary), `sub_1E00370` (backend variant) |
| **MachinePipeliner (SMS)** | `sub_3563190` |
| **ScheduleDAGMILive** | `sub_355F610` |
| **Instruction selection heuristic** | `sub_3557A10` |
| **Texture group merge** | `sub_2DDE8C0` |
| **Scheduling mode switch** | `sub_21668D0` (post-RA), `sub_2165850` (pre-RA) |

## MRPA: Incremental Register Pressure Tracking

MRPA (Machine Register Pressure Analysis) provides incremental register pressure tracking for the Machine Common Subexpression Elimination (MCSE) pass. Rather than recomputing pressure from scratch after each instruction move or elimination, MRPA applies delta updates to maintain a running pressure state.

The primary implementation lives at `sub_2E5A4E0` (48KB), with a backend variant at `sub_1E00370` (78KB). Both use DenseMap hash tables for per-instruction pressure data with the hash function `(ptr >> 9) ^ (ptr >> 4)`, empty sentinel `-8`, tombstone sentinel `-16`, minimum 64 buckets, and power-of-two sizing. The `sub_1E00370` backend variant calls the pressure computation core at `sub_1DF7390` (8 call sites) and `sub_1DFB9D0` (6 call sites), plus pressure set queries via `sub_1E1C690` / `sub_1E15D60`.

The MRPA pressure cluster spans the address range `0x1DF0000`--`0x1E0FFFF`:

| Function | Role |
|---|---|
| `sub_1DF3D00` | Scheduler support (lowest address) |
| `sub_1DF4120` | Scheduler support |
| `sub_1DF4FB0` | Scheduler support |
| `sub_1DF5810` | Machine function pass (pressure-aware scheduling) |
| `sub_1DF7390` | Pressure computation core (called 8x from `sub_1E00370`) |
| `sub_1DF76E0` | Register liveness query |
| `sub_1DF7A80` | Code motion feasibility check |
| `sub_1DF81C0` | Pressure computation core |
| `sub_1DF9E90` | Schedule optimization pass |
| `sub_1DFB810` | DenseMap (64-bit value variant) |
| `sub_1DFB9D0` | DenseMap (32-bit value variant, called 6x) |
| `sub_1E00370` | MRPA entry -- backend variant |

### Incremental Update Flow

The incremental update is the core algorithm. Rather than performing a full O(n) pressure recomputation after every MCSE transform, it maintains a running pressure state through delta operations. The pseudocode below is reconstructed from `sub_2E5A4E0`:

```
function mrpa_incremental_update(context, basicBlock):
    // Phase 1: Build worklist via DFS
    visited = DenseSet()                        // v292--v295
    worklist = []
    dfs_push(worklist, basicBlock, visited)     // standard DFS seed

    while worklist is not empty:
        bb = worklist.pop()

        // Phase 2: Create instruction tracking entries
        tracking = context.densemap[+80..+104]  // DenseMap at context offsets
        for each instr in bb.instructions:
            tracking.insert(instr, PressureEntry{})

        // Phase 3: Filter schedulable instructions
        if not sub_2E501D0(instr):              // schedulability predicate
            continue

        // Phase 4: Scan operands (40-byte entries)
        for i in range(instr.num_operands):     // iterated at v69/v70
            operand = instr.operand[i]          // 40-byte stride
            if not operand.isVirtualRegister():
                continue

            // Phase 5: Virtual register operand processing
            old_reg = sub_2EBEF70(operand)      // find existing rename mapping
            reg_info = sub_2EBEE10(operand)     // query register class, constraints
            new_reg = sub_2EBE820(operand)      // attempt rename if profitable
            if new_reg != old_reg:
                sub_2EBF120(old_reg)            // free old register

            // Phase 6: Register class constraint validation
            sub_reg_list = sub_E922F0(reg_info) // sub-register list for class
            for each sub_reg in sub_reg_list:
                validate_class_constraint(sub_reg, context.class_limits)

        // Phase 7: Pressure feasibility check
        bb_pressure = context.per_bb_data[bb]   // at v279[36]
        if not sub_2E4F9C0(bb_pressure):        // exceeds class limits?
            // Rename was unprofitable -- roll back
            context.rename_count_fail++         // *((_DWORD*)v254 + 17)
            sub_2E88E20(instr)                  // erase unprofitable instruction
        else:
            context.rename_count_success++      // *((_DWORD*)v254 + 16)
```

The key insight is that steps 5--7 form a speculative rename-then-validate loop: MRPA tentatively renames a virtual register, checks whether the rename reduces pressure below the class limit, and rolls back if it does not. The rename counts at `*((_DWORD*)v254 + 16)` (success) and `*((_DWORD*)v254 + 17)` (failure) provide a diagnostic ratio of how often speculative renames succeed.

### Register Liveness Queries

Register liveness (`sub_1DF76E0`) checks whether a register is live in an instruction range `[a3, a4]` using `_bittest` on register class bitmaps. A compressed alias table at context offset `+240` stores sub-register overlap information in 24-byte entries containing alias counts and alias data offsets.

The alias table structure:

| Offset | Size | Content |
|---|---|---|
| +0 | 8 | Sub-table pointer |
| +8 | 56 | Alias data block |
| +8..+10 (per entry) | 2 | Alias count (uint16) |
| +10.. | variable | Alias register IDs (2 bytes each) |

Sub-register overlap is resolved through an incremental alias walk: for each register in the query range, the alias table is consulted to expand the register into its physical sub-registers, and each sub-register is tested against the liveness bitmap.

### Code Motion Feasibility

Code motion feasibility (`sub_1DF7A80`) validates whether an instruction can be moved between basic blocks:

1. Check single-predecessor relationship between source and destination BBs.
2. Validate against the allocation bitmask at allocator offset `+38`.
3. Walk an instruction window bounded by offset `+296` (configurable window size).
4. Count conflicting operands within the window.
5. Track affected registers in an rb-tree set (offsets 56--88) with node structure `[left(16), right(24), value(32)]`.

An instruction is movable only if the conflicting operand count within the window is zero and the allocation bitmask permits the move.

### MRPA Verification

A debug-only verification path checks incremental update correctness against full recomputation. The trigger path in `sub_2E5A4E0` (decompiled lines 1702--1708):

```c
if ( *(_BYTE *)(v7 + 40)               // [1] context enable flag -- always ON during MCSE
  && (_BYTE)qword_501F8A8              // [2] verify-update-mcse -- user must enable
  && (_BYTE)qword_501F988              // [3] incremental-update-mcse -- default ON
  && !sub_2E59B70(                     // [4] full recomputation DISAGREES
       *(_QWORD*)(v7+48),
       qword_501F7C8, ...) )
{
  sub_C64ED0("Incorrect RP info from incremental MRPA update\n", 1u);
}
```

All four conditions must hold simultaneously:

1. Context enable flag (`v7 + 40`) is set -- always true during MCSE.
2. `verify-update-mcse` is ON -- user must explicitly enable this debug knob.
3. `incremental-update-mcse` is ON -- default is ON.
4. `sub_2E59B70` returns false -- full recomputation disagrees with the incremental state.

When all conditions hold, the error `"Incorrect RP info from incremental MRPA update"` fires via `sub_C64ED0` (LLVM's `report_fatal_error`). The `print-verify` knob controls whether detailed per-register-class mismatch data is printed.

The backend variant (`sub_1E00370`, decompiled lines 2416--2420) uses `byte_4FC6020` as its guard flag, calls `sub_1DFF720` for verification, and falls back to `byte_4FC62C0` (a cached result) if verification is disabled.

| Knob | Default | Description |
|---|---|---|
| `incremental-update-mcse` | true | Incrementally update register pressure analysis |
| `verify-update-mcse` | false | Verify incremental update by full RP analysis |
| `print-verify` | false | Print problematic RP info if verification failed |

To trigger verification: `cicc -Xcuda -verify-update-mcse input.cu`. NVIDIA keeps this check off by default since the full rescan is O(n) and expensive.

## MachinePipeliner: Swing Modulo Scheduling

**Complexity.** Let N = number of instructions in the loop body and E = number of dependency edges in the DDG. DDG construction is O(N + E). RecMII computation (`computeRecMII`) finds the maximum cycle ratio via enumeration of elementary circuits in the DDG -- worst-case exponential, but bounded in practice by small loop sizes (N < 100) and sparse dependency graphs. ResMII computation is O(N) (sum of resource vectors). ASAP/ALAP computation is O(N + E) each (topological traversals). The II search probes at most `pipeliner-ii-search-range` (default 10) candidate IIs. For each II, node placement is O(N * II) -- each of N nodes probes up to II cycle slots. The total scheduling cost is O((N + E) + R * N * II_max) where R = search range. The `pipeliner-max-stages` (default 3) and `pipeliner-max-mii` (default 27) provide additional constant-factor bounds. For MRPA, the incremental pressure update is O(1) per instruction move (delta update), compared to O(N) for a full recomputation -- this is the key efficiency gain over a naive approach.

The MachinePipeliner (`sub_3563190`, ~2030 decompiled lines, ~58KB) implements Swing Modulo Scheduling (SMS) for software pipelining of loop bodies. It overlaps iterations of a loop body to improve throughput on pipelined hardware by interleaving instructions from different iterations. The upstream LLVM equivalent is `SwingSchedulerDAG::schedule()`.

Pass discovery: the pipeliner walks an analysis array at `this+3456` (offset 3456) looking for vtable `unk_4F86530` (the MachinePipeliner analysis pass), then extracts the SwingSchedulerDAG context at offset `+176`.

### Phase 1: Initialization and DDG Construction

The setup chain builds the data dependence graph and computes MII lower bounds:

| Step | Function | Description |
|---|---|---|
| 1 | `sub_2F97F60` | `initializeDAG` -- build data dependence graph (DDG) over the single-BB loop body |
| 2 | `sub_3559990` | `computeNodeLatencies` -- fill latency fields per SUnit from the target scheduling model |
| 3 | `sub_3542B20` | `addDependencies` -- add register/memory/order dependency edges to the DDG |
| 4 | `sub_2F90200` | `updateRegPressure` -- compute initial register pressure state for the loop body |
| 5 | `sub_354CBB0` | `computeRecMII` -- find the maximum cycle length of any recurrence in the DDG |
| 6 | `sub_35449F0` | `computeResMII` -- compute `ceil(total_resource_usage / functional_unit_count)` |

The context object `SwingSchedulerDAG` occupies approximately 4100 bytes:

| Offset | Field |
|---|---|
| +32 | `MachineFunction*` |
| +48..56 | BB range (iterated at 256-byte stride) |
| +944 | DenseMap: pre-existing ordering constraints |
| +3456 | Analysis pass vector |
| +3472 | MII (int32) |
| +3480 | `schedulingSucceeded` (bool) |
| +3488 | DiagnosticsEngine / remark context |
| +3520 | `TargetSubtargetInfo*` |
| +3944..3952 | DDG node storage (vector) |
| +4016..4072 | Recurrence DenseMap (24-byte entries) |

### Phase 2: MII Computation and II Search

MII computation combines two lower bounds:

- **RecMII** (Recurrence MII): the longest cycle in the DDG, computed by `sub_354CBB0`. Each recurrence (loop-carried dependency cycle) constrains the minimum II because the cycle must fit within one iteration interval. If `pipeliner-ignore-recmii` is set, RecMII is forced to zero so only resource constraints matter.
- **ResMII** (Resource MII): `ceil(sum of resource usage across all instructions / number of available functional units)`, computed by `sub_35449F0`. This reflects the throughput bottleneck of the hardware.

```
function compute_MII():
    recMII = sub_354CBB0()           // max recurrence length
    resMII = sub_35449F0()           // resource throughput limit
    if pipeliner-ignore-recmii:      // qword_503E888
        recMII = 0
    MII = sub_3542AB0(resMII, recMII)  // max(resMII, recMII)
    sub_3542AE0()                    // store MII at this+3472
    return MII
```

The II search algorithm starts at MII and probes upward:

```
function ii_search(MII):
    max_ii = MII + pipeliner-ii-search-range      // default: MII + 10
    if pipeliner-force-ii != 0:                    // qword_503EB80
        return try_schedule(pipeliner-force-ii)    // skip search entirely

    for II = MII to max_ii:
        // 1. Compute ASAP/ALAP at this II
        asap = compute_ASAP(DDG, II)               // sub_354BFF0 -> v369
        alap = compute_ALAP(DDG, II)               // sub_354BFF0 -> v373

        // 2. Place all nodes into II-wide modulo reservation table
        success = place_nodes(asap, alap, II)       // sub_354C3A0

        if not success:
            continue                                // try next II

        // 3. Compute stage count
        numStages = (lastCycle - firstCycle) / II   // (v84 - v80) / v88

        // 4. Validate stage count
        if numStages > pipeliner-max-stages:        // default 3
            continue

        // 5. Register pressure check (if enabled)
        if pipeliner-register-pressure:             // qword_503E2C0
            if not verify_pressure(II, pipeliner-register-pressure-margin):
                continue                            // sub_355C7C0

        return (II, schedule)

    return FAILURE   // "Unable to find schedule"
```

The `pipeliner-force-ii` knob (default 0) bypasses the search entirely and forces a specific II value. This is useful for testing or when the compiler team knows the optimal II for a specific loop shape.

### Phase 3: ASAP/ALAP Computation

ASAP (As Soon As Possible) and ALAP (As Late As Possible) define the scheduling window for each instruction at a given II:

**ASAP computation** (`sub_354BFF0`, first invocation producing `v369`): traverses the DDG in topological order. For each node, ASAP = max over all predecessors of `(predecessor.ASAP + edge.latency)`. The root nodes (no predecessors) have ASAP = 0. This gives the earliest cycle each instruction can execute without violating data dependencies.

**ALAP computation** (`sub_354BFF0`, second invocation producing `v373`): traverses the DDG in reverse topological order. For each node, ALAP = min over all successors of `(successor.ALAP - edge.latency)`. Leaf nodes (no successors) have ALAP = II - 1 (or the schedule length bound). This gives the latest cycle an instruction can execute.

The **scheduling window** for instruction `i` is `[ASAP(i), ALAP(i)]`. Instructions with narrow windows (ASAP close to ALAP) are more constrained and are typically scheduled first by the node ordering heuristic.

### Phase 4: Node Placement

Node placement (`sub_354C3A0`) attempts to assign each instruction to a specific cycle in the modulo reservation table (MRT). The MRT has II columns (one per cycle in the initiation interval) and tracks resource usage per cycle.

The placement algorithm follows the Swing Modulo Scheduling strategy:

1. **Node ordering** (`sub_35630A0`): nodes are prioritized by a combination of critical-path depth, recurrence membership, and scheduling freedom (ALAP - ASAP). Nodes in tight recurrences and on the critical path are placed first.
2. **Direction selection**: for each node, the scheduler decides whether to place it "forward" (from ASAP toward ALAP) or "backward" (from ALAP toward ASAP) based on its dependency relationships. The "swing" refers to alternating direction between predecessor-constrained and successor-constrained nodes.
3. **Cycle probing**: starting from the preferred direction, the scheduler tries each cycle in the node's `[ASAP, ALAP]` window. At each candidate cycle, it checks resource availability in the MRT (the cycle modulo II must have sufficient functional unit capacity) and verifies that all dependency constraints remain satisfied.
4. **Conflict resolution**: if no cycle in the window is feasible, the placement fails for this II and the search continues with II+1.

### Phase 5: Kernel Generation

After a valid schedule is found, the pipeliner builds the kernel, prolog, and epilog. The numStages value (`(lastCycle - firstCycle) / II`) determines how many iterations overlap.

```
function build_kernel(schedule, II, numStages):
    // Build instruction-to-stage and instruction-to-cycle DenseMaps
    instrToStage = DenseMap<SUnit*, int>()      // v317/v318/v319
    instrToCycle = DenseMap<SUnit*, int>()       // v320/v321/v322
    // DenseMap config: hash=(key>>9)^(key>>4), empty=-4096, tombstone=-8192

    for stage in range(numStages):
        for each SUnit in schedule.stage_bundle(stage):
            instrToStage[SUnit] = stage
            instrToCycle[SUnit] = SUnit.assigned_cycle

    // Cross-reference recurrence edges with stage assignments
    if this+4064 (recurrence count) != 0:
        for each recurrence_edge in this+4056:
            edge.stage = instrToStage[edge.instruction]
            // Build per-recurrence analysis DenseMap (24-byte entries)

    // Select codegen backend (priority order):
    if pipeliner-annotate-for-testing:          // testing mode: annotate only
        sub_359AD80(schedule)
        return
    if pipeliner-experimental-cg:               // peeling code generator
        if numStages == 0:
            sub_35A5710()                       // trivial kernel (no overlap)
        else:
            sub_35A93B0()                       // experimental peeling CG
            sub_3598EB0()                       // finalize prolog/epilog
        return
    if pipeliner-mve-cg:                        // MVE code generator (DEFAULT)
        if numStages == 0 and target_supports_mve():
            sub_35A7730()                       // MVE compatibility check
            sub_35A76E0()                       // MVE code generator
            return
        // else fall through to experimental CG
    // Default fallthrough: experimental CG path
```

The codegen backend priority is: (1) `pipeliner-annotate-for-testing` for test infrastructure, (2) `pipeliner-experimental-cg` for peeling-based generation, (3) `pipeliner-mve-cg` (default enabled) for the MVE (Modulo Variable Expansion) code generator. The MVE path is gated on `numStages == 0` and a target callback at `**(this+3520)+72` returning non-default (i.e., not `sub_2FDC510`).

The SBO (Small Buffer Optimization) pattern is used for nodeInfo arrays: `v416 = v418` (inline buffer of 704 bytes = 8 nodes x 88 bytes). When the loop body exceeds 8 instructions, `sub_35498F0` sorts and possibly heap-allocates.

### Error Conditions

| Condition | Diagnostic | Severity |
|---|---|---|
| MII == 0 | `"Invalid Minimal Initiation Interval: 0"` | 0x15 (missed) |
| MII > `pipeliner-max-mii` | `"Minimal Initiation Interval too large: MII > SwpMaxMii"` | 0x15 (missed) |
| Scheduling failure | `"Unable to find schedule"` | 0x15 (missed) |
| numStages == 0 | `"No need to pipeline - no overlapped iterations in schedule."` | 0x15 (missed) |
| numStages > `pipeliner-max-stages` | `"Too many stages in schedule: numStages > SwpMaxStages"` | 0x15 (missed) |
| Success | `"Pipelined succesfully!"` [sic] | 0x13 (passed) |

The typo "succesfully" (single 's') is preserved from upstream LLVM.

### Pipeliner Knobs

| Knob | Global | Default | Description |
|---|---|---|---|
| `enable-pipeliner` | `unk_503EE20` | true | Master switch for SMS |
| `enable-pipeliner-opt-size` | `qword_503ED40` | false | Enable SWP at -Os |
| `pipeliner-max-mii` | `qword_503ECE8` | 27 | Maximum allowed MII |
| `pipeliner-force-ii` | `qword_503EB80` | 0 | Force specific II (0 = auto) |
| `pipeliner-max-stages` | `qword_503EB28` | 3 | Maximum pipeline stages |
| `pipeliner-prune-deps` | `qword_503E9C0` | true | Prune deps between unrelated Phi nodes |
| `pipeliner-prune-loop-carried` | `qword_503E8E0` | true | Prune loop-carried order deps |
| `pipeliner-ignore-recmii` | `qword_503E888` | false | Ignore RecMII (hidden knob) |
| `pipeliner-show-mask` | `qword_503E720` | false | Debug: show scheduling mask |
| `pipeliner-dbg-res` | `qword_503E640` | false | Debug: resource usage |
| `pipeliner-annotate-for-testing` | `qword_503E5E8` | false | Annotate instead of codegen |
| `pipeliner-experimental-cg` | `qword_503E508` | false | Use peeling code generator |
| `pipeliner-ii-search-range` | `qword_503E3A0` | 10 | Range to search for II |
| `pipeliner-register-pressure` | `qword_503E2C0` | false | Consider register pressure |
| `pipeliner-register-pressure-margin` | `qword_503E1E0` | 5 | Margin % for reg pressure limit |
| `pipeliner-mve-cg` | `unk_503E100` | true | Use MVE code generator |
| `pipeliner-enable-copytophi` | `qword_503E020` | true | Enable CopyToPhi DAG Mutation |
| `pipeliner-force-issue-width` | `qword_503DF40` | 0 | Force issue width (0 = auto) |

All registered in `ctor_676_0_0x5a3430.c`.

### MachinePipeliner Function Map

| Function | Identity |
|---|---|
| `sub_3563190` | Top-level SMS orchestrator (`SwingSchedulerDAG::schedule` analog) |
| `sub_2F97F60` | DDG builder (`initializeDAG` analog) |
| `sub_3559990` | Node-latency computation |
| `sub_3542B20` | Register/memory/order dependence-edge addition |
| `sub_2F90200` | Register-pressure update |
| `sub_354CBB0` | `computeRecMII` (recurrence-bounded MII) |
| `sub_35449F0` | `computeResMII` (resource-bounded MII) |
| `sub_3542AB0` | `setMII` = max(ResMII, RecMII) (sets +3472) |
| `sub_3542AE0` | MII validation / store at +3472 |
| `sub_3556270` | Per-node info gather (88-byte per-node records) |
| `sub_35476E0` | Initial scheduling-order computation |
| `sub_35523F0` | SUnit ordering builder |
| `sub_35546F0` | Dependence-order topological sort |
| `sub_3543340` | ASAP/ALAP time computation |
| `sub_35630A0` | Schedule cycle-number normalization |
| `sub_35568E0` | Core SMS node placement |
| `sub_35433F0` | Post-placement schedule adjustment |
| `sub_3557A10` | Stage/cycle finalization |
| `sub_354A760` | Iteration-to-stage map builder |
| `sub_355F610` | II search loop (2351 lines) |
| `sub_354BE50` | Per-stage schedule accessor |
| `sub_35498F0` | Node-info sort (taken when >8 nodes) |
| `sub_359AD80` | Annotate-for-testing instrumentation |
| `sub_35A5710` | Trivial kernel generator |
| `sub_35A93B0` | Experimental peeling code-gen path |
| `sub_3598EB0` | Experimental-kernel finalization |
| `sub_35A76E0` | MVE code generator |
| `sub_35A7730` | MVE compatibility check |

## ScheduleDAGMILive: Post-RA Instruction Ordering

`ScheduleDAGMILive` (`sub_355F610`, 64KB) is the post-RA machine instruction scheduler. It takes the pipeliner's output (or standalone scheduling regions) and determines the final instruction order while respecting register pressure limits.

Data structures:

- **SUnit** (Scheduling Unit): 88 bytes per instruction, consistent across both the pipeliner and `ScheduleDAGMILive`.
- **Instruction-to-node hash map**: 632-byte entries per instruction. The unusually large entry size suggests extensive caching of per-instruction metadata (RP deltas, latency info, dependency edges) to avoid recomputation.
- **RP tracking structure**: 112 bytes, with per-register-class pressure arrays at offsets 32--48 (current) and 56--72 (limits).

The scheduling flow:

1. Initialize RP tracking via `sub_3551AB0` (if `pipeliner-register-pressure` is set).
2. Set per-class pressure defaults via `sub_2F60A40`.
3. Walk BB instruction list, build instruction-to-node hash map.
4. Compute ASAP (earliest cycle) via `sub_354BFF0` -> `v369`.
5. Compute ALAP (latest cycle) via `sub_354BFF0` -> `v373`.
6. Place instructions via `sub_354C3A0` (returns success/failure).
7. Calculate stage count: `(lastCycle - firstCycle) / II` = `(v84 - v80) / v88`.
8. Verify placement via `sub_355C7C0`.
9. Build stage descriptors via `sub_355D7E0` (80 bytes per stage, 10 QWORDs each).

### Instruction Selection Heuristic

The instruction selection heuristic (`sub_3557A10`, 47KB) determines which instruction to schedule next from the ready set. It implements a multi-level priority scheme operating on 88-byte SUnit entries:

**Level 1 -- Latency/Depth priority** (SUnit offset `+240`): instructions deeper in the dependency graph are scheduled first. Depth is measured as the longest path from the instruction to a sink node in the DDG. This ensures that critical-path instructions are placed early, preventing them from becoming bottlenecks. Latency recomputation occurs via `sub_2F8F5D0` during priority comparison to account for any scheduling decisions already made.

**Level 2 -- Target priority table** (context `a1+3944`): a table of 16-byte entries, each containing:

| Offset | Size | Field |
|---|---|---|
| +0 | 4 | `start` -- first cycle of priority window |
| +4 | 4 | `end` -- last cycle of priority window |
| +8 | 4 | `priority` -- target-assigned priority value |
| +12 | 4 | `window_width` -- scheduling window size |

The target (NVPTX backend) populates this table to express hardware-specific ordering preferences -- for example, prioritizing memory operations that can be overlapped with computation, or ensuring that warp-synchronous instructions are scheduled in specific relative positions. Instructions that fall within a priority window with a higher priority value are selected first.

**Level 3 -- Schedule window width**: when levels 1 and 2 are tied, the instruction with the narrower scheduling window (`ALAP - ASAP`) is preferred. Narrower windows mean fewer legal placement options, so these instructions should be placed before more flexible ones to avoid creating conflicts.

The ready queue is managed by `sub_3553D90`. Pattern matching on ready instructions proceeds through `sub_35540D0` (applicability check) and `sub_35543E0` (pattern application), with validation via `sub_3546B80`. A hash table at `a1+3976` maps instructions to schedule nodes for O(1) lookup during priority comparison.

```
function select_next_instruction(ready_set):
    best = null
    for each candidate in ready_set:
        if best is null:
            best = candidate
            continue

        // Level 1: depth comparison
        if candidate.depth > best.depth:        // offset +240
            best = candidate
            continue
        if candidate.depth < best.depth:
            continue

        // Level 2: target priority table
        cand_prio = lookup_target_priority(candidate, priority_table)
        best_prio = lookup_target_priority(best, priority_table)
        if cand_prio > best_prio:
            best = candidate
            continue
        if cand_prio < best_prio:
            continue

        // Level 3: prefer narrower window
        cand_width = candidate.ALAP - candidate.ASAP
        best_width = best.ALAP - best.ASAP
        if cand_width < best_width:
            best = candidate

    return best
```

## Texture Group Merge

The Texture Group Merge pass (`sub_2DDE8C0`, 74KB, 2382 decompiled lines) groups texture load instructions that access related memory locations, enabling the hardware texture unit to coalesce them into fewer requests. This is an NVIDIA-specific pass not present in upstream LLVM.

### Fibonacci Hashing

The pass uses Fibonacci hashing for candidate bucketing:

```
hash = (ptr * 0xBF58476D1CE4E5B9) >> shift
```

The constant `0xBF58476D1CE4E5B9` is the 64-bit Fibonacci hash multiplier, derived from `2^64 / phi` where `phi = (1 + sqrt(5)) / 2` is the golden ratio. This multiplicative hash provides near-optimal distribution for pointer-based keys because the golden ratio's irrational nature ensures that consecutive multiples are maximally spread across the output range. The same constant appears in:

- Linux kernel's `hash_64()` in `include/linux/hash.h`
- LLVM's `FoldingSet` and `DenseMap` internals
- CICC's SCEV expression uniquing (`sub_DC2B70`)
- CICC's OpenMP SPMD region hash table

The `shift` parameter controls how many high bits are retained, effectively determining the number of hash buckets as `2^(64 - shift)`. For a 1024-bucket table, shift would be 54.

### Algorithm Detail

1. Walk the BB instruction list.
2. For each instruction, call `sub_2DDC600` (candidate identification) to determine if it is a texture load eligible for merging.
3. Hash the candidate's key pointer using Fibonacci hashing to assign it to a bucket.
4. Insert the candidate into the group table.

Group table entries are 56 bytes (7 QWORDs):

| Offset | Size | Content |
|---|---|---|
| +0 | 8 | Key pointer (texture base address or descriptor) |
| +8 | 8 | Data pointer (to member array) |
| +16 | 4 | Member count |
| +20 | 4 | Member capacity |
| +24 | 32 | Reserved / padding |

Group members are 32 bytes each:

| Offset | Size | Content |
|---|---|---|
| +0 | 8 | `MachineInstr*` -- the texture load instruction |
| +8 | 8 | Symbol -- the texture symbol reference |
| +16 | 8 | Debug info -- source location |
| +24 | 8 | Scope info -- DWARF scope |

Generated group names carry a `.Tgm` (Texture Group Merge) suffix via `sub_2241490`. This suffix appears in debug output and internal symbol tables.

### 4-Callback Framework

The pass operates through a general instruction grouper framework (`sub_3147BA0`) that supports multiple types of instruction grouping through a common callback interface. Four callbacks are registered for texture group merge:

| # | Callback | Function | Purpose |
|---|---|---|---|
| 1 | Candidate identification | `sub_2DDC600` | Examines each `MachineInstr` and returns true if it is a texture load eligible for grouping. Checks opcode, address space (texture memory), and operand constraints. |
| 2 | Group formation | `sub_2DDBF40` | After candidates are identified and hashed into buckets, this callback decides which candidates within a bucket should form a group. It checks address proximity, common base registers, and compatible access patterns. |
| 3 | Merge execution | `sub_2DDB3F0` | Applies the actual merge transformation. Replaces individual texture loads with a single grouped load instruction, rewrites operands, and updates dependency edges. |
| 4 | Cleanup | `sub_2DDB400` | Frees temporary data structures (group tables, member arrays, hash buckets) after merging is complete. |

Additional helper functions in the texture group merge:

| Function | Role |
|---|---|
| `sub_2DDD850` | Node insertion into group table |
| `sub_2DDDD70` | Resize/grow scheduling data |
| `sub_2DDD530` | Scheduling iteration over groups |
| `sub_2DDDAB0` | Node analysis (profitability check) |
| `sub_2DDB710` | Data dependency edge creation |
| `sub_2DDE490` | Grouping operation (merge groups) |
| `sub_2DDBC50` | Constraint application |
| `sub_2DDBBA0` | Constraint application (secondary) |
| `sub_2DDBA80` | Finalize group (seal and emit) |

The grouper framework is designed to be reusable: by registering different callback tuples, the same framework can group surface loads, shared memory accesses, or other coalescing-friendly instruction patterns.

## Scheduling Mode: The usedessa Knob

The `usedessa` knob (`dword_4FD26A0`, default 2) controls the scheduling pass pipeline configuration despite its name suggesting deSSA (de-Static Single Assignment) method selection. Pre-RA scheduling dispatches through `sub_2165850`; post-RA through `sub_21668D0`.

**Mode 1 (simple)**: Pre-RA scheduling is skipped entirely. Post-RA runs only `unk_4FCE24C` (the post-RA scheduler). This minimal configuration is useful for debugging or when scheduling is harmful to performance.

**Mode 2 (full, default)**: Pre-RA scheduling runs `unk_4FC8A0C`. Post-RA scheduling runs three passes sequentially:

1. `unk_4FC8A0C` -- pre-RA pass (disabled/noop in post-RA context).
2. `unk_4FCE24C` -- post-RA scheduler.
3. `unk_4FC9D8C` -- extra scheduling pass.

After scheduling completes, the framework prints `"After Machine Scheduling"`, optionally runs `sub_21F9D90`, then runs `unk_4FCAC8C` and prints `"After StackSlotColoring"`.

The "disabled" passes in mode 2 are registered but gated internally, allowing the framework to maintain a uniform pass list while selectively activating passes based on the current compilation phase.

## Cross-Cutting Observations

Register pressure tracking appears in three distinct places within the scheduling infrastructure, each serving a different consumer:

| Tracker | Consumer | Update Frequency |
|---|---|---|
| MRPA incremental (`sub_2E5A4E0`) | MCSE decisions | Per instruction move/elimination |
| ScheduleDAGMILive (`sub_355F610`) | Scheduling decisions | Per scheduling region |
| MachinePipeliner stage tracking | II feasibility | Per pipeline stage |

All three maintain per-register-class pressure arrays but with different granularities. The MRPA tracker uses incremental delta updates for efficiency; the scheduler computes ASAP/ALAP bounds per region; the pipeliner tracks pressure per modulo stage.

The DenseMap hash function `(ptr >> 9) ^ (ptr >> 4)` is shared across both the 32-bit value variant (`sub_1DFB9D0`) and 64-bit value variant (`sub_1DFB810`), indicating a common template instantiation pattern consistent with LLVM's `DenseMap<K, V>` template.

Contrast with **ptxas scheduling**: ptxas has its own instruction scheduling subsystem with 195 knobs (including scoreboard-aware scheduling via the `AdvancedSB*` family, `SchedDisableAll`, `SchedForceReverseOrder`, and the `GemmPipeliner*` family of 8 knobs for matrix multiply detection and pipelining). CICC's scheduling operates at the MachineInstr level before PTX emission; ptxas re-schedules at the SASS level after PTX assembly. The two scheduling layers are independent but complementary.

## What Upstream LLVM Gets Wrong for GPU

Upstream LLVM's instruction scheduling framework was designed for CPU cores with out-of-order execution, branch prediction, and deep reorder buffers. On a GPU SM, these hardware features do not exist:

- **Upstream assumes out-of-order hardware will hide scheduling mistakes.** Modern CPUs have 200+ entry reorder buffers that dynamically reorder instructions, making compiler scheduling a second-order optimization. GPU SMs execute instructions in-order within each warp -- every scheduling decision is final. A poorly ordered instruction stream on GPU means stalls that no hardware can recover from.
- **Upstream optimizes for pipeline hazards and port pressure.** CPU schedulers model execution port contention (e.g., port 0 vs. port 1 on Intel), dispatch group rules, and pipeline bubble avoidance. GPU scheduling targets register pressure minimization (`nvptx-sched4reg`) because the SM's warp scheduler handles instruction-level parallelism through warp interleaving, not through instruction reordering within a single thread.
- **Upstream assumes a single scheduling pass produces the final order.** On CPU, LLVM's `ScheduleDAGMILive` emits the final instruction sequence. On NVPTX, cicc's scheduling is the first of two layers -- ptxas re-schedules the entire program at the SASS level with its own 195-knob subsystem (including scoreboard-aware scheduling via the `AdvancedSB*` family). CICC's scheduler optimizes for ptxas consumption, not for direct hardware execution.
- **Upstream has no concept of texture instruction grouping.** CPU scheduling never considers grouping memory operations for hardware coalescing units. NVIDIA adds a dedicated Texture Group Merge pass (`sub_2DDE8C0`, 74KB) that groups texture load instructions by base address for the hardware texture unit -- an entirely GPU-specific optimization absent from upstream.
- **Upstream does not track register pressure incrementally during CSE.** Upstream LLVM recomputes register pressure from scratch after each Machine CSE transform. NVIDIA's MRPA subsystem (`sub_2E5A4E0`, 48KB) maintains running pressure state through delta updates, because on GPU the pressure-to-occupancy relationship makes every CSE decision a potential occupancy cliff crossing that must be evaluated cheaply.

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Scheduling subsystems** | `ScheduleDAGMILive` + optional `MachinePipeliner`; no incremental pressure tracker | Three distinct subsystems: MRPA incremental tracker, Swing Modulo Scheduler, `ScheduleDAGMILive`; plus texture group merge pass |
| **MRPA (incremental pressure)** | Not present; pressure recomputed from scratch after each CSE transform | `sub_2E5A4E0` (48 KB) + backend variant `sub_1E00370` (78 KB) maintain running pressure state through delta operations during MCSE |
| **Texture group merge** | No concept of texture instruction grouping | Dedicated pass (`sub_2DDE8C0`) groups texture load instructions for hardware coalescing; scheduling-adjacent optimization absent from upstream |
| **Scheduling target** | Optimize for hardware pipeline hazards and port pressure | Optimize the MachineInstr stream for `ptxas` consumption; focus on register pressure reduction (`nvptx-sched4reg`) rather than hardware pipeline timing |
| **Two-level scheduling** | Single scheduling pass produces final instruction order | CICC scheduling is first layer; `ptxas` re-schedules at SASS level with its own 195-knob subsystem |
| **Register pressure model** | Per-register-class pressure sets from TRI | Same model but with GPU occupancy awareness; pressure arrays used to detect occupancy cliff crossings |
| **Scheduling mode switch** | Configured at pipeline construction time | Runtime mode switch between pre-RA (`sub_2165850`) and post-RA (`sub_21668D0`) with different heuristic weights |

## ptxas Interaction

cicc's instruction scheduling operates at the MachineInstr level and produces a PTX instruction order that is not final. `ptxas` re-schedules the entire program at the SASS level using its own 195-knob scheduling subsystem, including scoreboard-aware scheduling (`AdvancedSB*` family), the `GemmPipeliner*` family for matrix multiply detection and software pipelining, and `SchedForceReverseOrder` for debugging. cicc's scheduler therefore optimizes for ptxas consumption rather than direct hardware execution: its primary goal is minimizing register pressure (`nvptx-sched4reg`) so that ptxas starts from a low-pressure baseline. The two scheduling layers are independent but complementary -- cicc controls the virtual register count visible to ptxas, and ptxas maps the resulting instruction stream onto the SM's hardware pipeline with full knowledge of scoreboard latencies and functional unit availability.
