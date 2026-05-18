# Serial and Cost-Based Schedule Generators

## Abstract

Tileiras carries two schedule generators with the same output shape and very different ambitions. The serial generator is a deterministic baseline — it walks operations in dataflow order, emits edges, and validates the resulting topological order. The cost-based generator is the full modulo-scheduling path: it ranks candidates with resource constraints, structural distances, and RRT probes, then retries with heavier strategies when cheaper placement fails.

Downstream passes consume either generator through the same schedule analysis interface. Picking a generator changes compile-time cost and schedule quality, not the public IR contract after generation succeeds.

## Generator Roles

| Generator | Intended use | Algorithmic shape |
|---|---|---|
| serial | deterministic baseline, forced-serial regions, low optimization paths | one walk, no II search, no RRT placement |
| cost-based | optimized TileAS scheduling for warp-specialized and resource-heavy loops | iterative placement with resource gates and cost ranking |

The serial generator earns its place by giving the compiler a simple, predictable schedule when the region does not need modulo scheduling or when a constraint asks for serial execution. The cost-based generator takes over when the compiler wants throughput and must reason about Blackwell issue slots, tensor memory, shared memory, barriers, and async pipelines.

## Serial Generator

The serial generator is a single greedy walk over the dependence DAG. It builds the dependence graph from the region, computes in-degree for every operation, seeds a ready queue with the zero-in-degree roots, and then repeatedly pops a ready operation, emits it at the next free `(stage, order)` position, and decrements its successors' in-degree counters — pushing each newly-ready successor onto the queue. Tie-breaking inside the ready queue is by program order so the output is bitwise deterministic across builds.

```c
bool generate_serial_schedule(Schedule *out, Operation *region) {
    DependenceGraph g = build_dependence_graph(region);
    InDegreeMap  in   = compute_in_degree(g);
    ReadyQueue   ready = collect_zero_in_degree(g);          // initial roots
    uint32_t     order = 0;

    while (!ready.empty()) {
        Operation *op = ready.pop_in_program_order();
        out->stage[op] = 0;                                  // single steady-state stage
        out->order[op] = order++;

        for (Operation *succ : g.successors(op)) {
            if (--in[succ] == 0) {
                ready.push(succ);
            }
        }
    }

    return order == g.node_count();                          // false ⇒ dependence cycle
}
```

The serial generator never builds an RRT, never searches for `II`, and never ranks candidate seats. It produces a schedule in which every operation lives at stage `0` and runs strictly after its dependences, which is the correct shape for forced-serial regions and for the low-optimisation paths that do not need software pipelining. When the walk does not visit every operation, the input has a dependence cycle and the caller falls back to a stronger strategy or reports failure.

## Cost-Based Generator

The cost-based generator runs a multi-arm strategy over candidate placement orderings. At each iteration it collects the ready set, dispatches it through the four placement arms documented in [Modulo Scheduler and Rau](modulo-scheduler-and-rau.md) — permute, fuse, retry, cost-based — and seats the candidate whose arm produces the lowest cost. Each arm independently proposes a candidate schedule; the lowest-cost legal one wins. When every arm rejects, the generator returns failure.

```c
bool generate_cost_based_schedule(ScheduleGenState *state) {
    while (!all_candidates_scheduled(state)) {
        CandidateList ready = collect_ready_candidates(state);
        if (ready.empty()) return false;                           // dependence cycle

        // Each arm proposes a candidate seat. Costs share a common origin so
        // the arm comparison is meaningful.
        ArmResult arms[4] = {
            try_permute     (state, ready),
            try_fuse        (state, ready),
            try_retry       (state, ready, state->snapshot),
            try_cost_based  (state, ready, state->snapshot),
        };

        const ArmResult *best = best_cost_arm(arms);               // skips arms that rejected
        if (best == NULL) return false;

        commit_seat(state, best->candidate, best->cycle);
    }
    return true;
}
```

The cost vector itself is lexicographic:

| Component | Role |
|---|---|
| hard resource gate | rejects candidates that violate depth, resource mask, or already-scheduled constraints |
| pipeline-slot pressure | prefers placements that reduce issue-slot and transport pressure |
| structural distance | breaks ties using dependence distance and critical-path shape |

Do not collapse this into one scalar without proving equivalence. The hard gate decides whether a candidate is legal; the later components only rank legal candidates.

## Placement Arms in Detail

The cost-based generator's four arms each implement a different placement heuristic. They share the same input — a ready set of candidate ops — and the same output shape — an `ArmResult` that either holds a chosen `(op, cycle)` seat or marks the arm as having rejected. Cost is compared across arms by the same lexicographic vector documented in [Schedule Solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md#cost-evaluators), so the cheapest legal seat wins regardless of which arm proposed it.

### Permute Arm

The permute arm enumerates every permutation of the ready set that respects the partial order from the dependence graph, scores each permutation by seating its ops greedily, and picks the permutation whose total cost is lowest. The arm bails out as soon as the permutation count rises past a threshold; for small ready sets it explores exhaustively, for larger ones it samples a fixed number of random permutations.

```c
ArmResult try_permute(ScheduleGenState *state, CandidateList ready) {
    ArmResult best = { .cost = COST_INFINITY, .accepted = false };
    PermutationEnumerator perm = enumerate_topo_permutations(ready, state->dep_graph);

    for (uint32_t i = 0; i < perm.count && i < PERMUTE_BUDGET; ++i) {
        Permutation order = perm.next();
        ScheduleSnapshot snap = snapshot_state(state);

        bool legal = true;
        for (Operation *op : order) {
            uint32_t t = find_earliest_legal_cycle(&snap, op);
            if (t == NO_LEGAL_CYCLE) { legal = false; break; }
            commit_seat_in_snapshot(&snap, op, t);
        }
        if (!legal) { restore_state(state, snap); continue; }

        CostVector cost = score_snapshot(&snap);
        if (cost_lexless(cost, best.cost)) {
            best = (ArmResult){ .cost = cost, .order = order, .accepted = true };
        }
        restore_state(state, snap);
    }
    return best;
}
```

A worked example: the ready set `{tiled_tma_load, smem_write, smem_read}` has six topo-permutations, two of which respect the load-before-read edge. The permute arm seats each of those two permutations greedily and picks the one whose total resource pressure is lowest — typically `(load, write, read)` over `(load, read, write)` because the latter delays the SMEM write into a stage where the next iteration's TMA load already claims `tp_smem_wr`.

### Fuse Arm

The fuse arm merges adjacent compatible ops that can share a resource slot in the same cycle. Two SMEM reads from the same buffer can fuse if their pool counts sum below the pool cap; a TMA load and an unrelated SMEM write cannot fuse because they claim different rows and the fusion would not reduce pressure. The arm is the only one that emits a single `(op-pair, cycle)` seat for two source ops.

```c
ArmResult try_fuse(ScheduleGenState *state, CandidateList ready) {
    ArmResult best = { .cost = COST_INFINITY, .accepted = false };
    for (uint32_t i = 0; i < ready.size; ++i) {
        for (uint32_t j = i + 1; j < ready.size; ++j) {
            Operation *a = ready.ops[i];
            Operation *b = ready.ops[j];
            if (!can_fuse(a, b, state)) continue;

            FusedOp fused = compose_resource_vectors(a, b);
            uint32_t t = find_earliest_legal_cycle_for(state, fused.vec);
            if (t == NO_LEGAL_CYCLE) continue;

            CostVector cost = score_with_fusion(state, fused, t);
            if (cost_lexless(cost, best.cost)) {
                best = (ArmResult){ .cost = cost, .fused = fused,
                                    .cycle = t, .accepted = true };
            }
        }
    }
    return best;
}

bool can_fuse(Operation *a, Operation *b, ScheduleGenState *state) {
    if (has_dependence(state, a, b))           return false;
    if (different_slot_groups(a, b))           return false;
    if (combined_pool_pressure(a, b) > caps()) return false;
    return true;
}
```

Worked example: two `smem_read` ops `r1, r2` reading disjoint buffers from the same SMEM bank. The fuse arm composes their resource vectors into a single triple `(slot=15, duration=7, occupancy=2)`; pool index `4` for the `tp_smem_rd` cap allows up to `5` simultaneous reads, so the fused op is legal at cycle `0` where either op alone would have been legal. The arm wins over permute when the buffer pair shares an SMEM bank but differs only in offset — the cost reducer scores the fused seat as one row contribution instead of two.

### Retry Arm

The retry arm consumes the snapshot overlay maintained by the driver and re-attempts ops that earlier arms marked dead. It does not re-score; it simply re-probes the same `(op, cycle)` candidate against a fresh RRT in case an earlier rejection was caused by transient pressure that has since cleared. The arm is the cheapest of the four — no permutation, no fusion, no cost reduction.

```c
ArmResult try_retry(ScheduleGenState *state, CandidateList ready,
                    RetrySnapshot *snap) {
    for (uint32_t i = 0; i < ready.size; ++i) {
        Operation *op = ready.ops[i];
        if (!snapshot_is_dead(snap, op)) continue;     // skip live ops
        uint32_t t = find_earliest_legal_cycle(state, op);
        if (t == NO_LEGAL_CYCLE) {
            continue;                                  // still dead
        }
        snapshot_mark_live(snap, op);
        CostVector cost = score_seat(state, op, t);
        return (ArmResult){ .cost = cost, .op = op, .cycle = t, .accepted = true };
    }
    return (ArmResult){ .cost = COST_INFINITY, .accepted = false };
}
```

The arm returns on the first revived op rather than scanning the full snapshot. This is intentional — the snapshot is small and the cost-based generator runs the arm again on the next iteration if more revived ops are available. Walking the full snapshot in one pass would burn time on ops that are guaranteed to remain dead until later state changes.

Worked example: an `smem_write` was marked dead by the permute arm because the TMA load occupied `tp_smem_wr` at cycle `0`. After the TMA load committed at cycle `0` of stage `0` and the modulo wrap exposed cycle `8` as a fresh seat, the retry arm finds `tp_smem_wr` clear at the new candidate cycle and revives the write.

### Cost-Based Arm

The cost-based arm is the most expensive of the four. It enumerates every legal `(op, cycle)` pair across the entire ready set, scores each with the full lexicographic cost vector, and picks the global minimum. The arm runs only when permute, fuse, and retry have all rejected — they cover the common cases, and the cost-based arm exists to find seats that the cheaper heuristics miss.

```c
ArmResult try_cost_based(ScheduleGenState *state, CandidateList ready,
                         RetrySnapshot *snap) {
    ArmResult best = { .cost = COST_INFINITY, .accepted = false };
    for (Operation *op : ready) {
        if (snapshot_is_dead(snap, op)) continue;
        for (uint32_t t = 0; t < state->ii; ++t) {
            if (!gate_g3_rrt_clean (state, op, t)) continue;
            if (!gate_g4_leader_gid_consistent(state, op,
                                               leader_gid_of(state, op))) continue;
            CostVector cost = sub_988080_search(state, op, t);
            if (cost_lexless(cost, best.cost)) {
                best = (ArmResult){ .cost = cost, .op = op, .cycle = t,
                                    .accepted = true };
            }
        }
        if (!best.accepted) snapshot_mark_dead(snap, op);
    }
    return best;
}
```

The two inner calls — `sub_988080_search` and the gate ladder — pull from the same cost tables that the slot model documents at rodata `0x4CC9D10..0x4CC9D70`. The arm's per-iteration cost is `O(|ready| × II)` probes, against `O(|ready|)` for the cheaper arms. Worked example: a ready set of `8` ops at `II = 16` produces `128` candidate `(op, cycle)` pairs; the cost-based arm probes each and returns the global minimum, while the permute arm would have explored only `8! / 6 ≈ 6720` permutations of a fixed seating order without varying the cycle.

The arm's worst case is exactly the case the cost reducer was designed for: small ready sets where every op claims a different slot and the right packing depends on aligning the SMEM transports across stages. The cheaper arms reject because their heuristics cannot see the cross-stage interaction; the cost-based arm sees it because it evaluates the full lexicographic vector for every candidate.

## Admission Gates

Before the placement driver `sub_981D50` commits a seat for a candidate op, four ordered gates run against every candidate the cost-sort surfaces. All four must pass for the seat to commit; failure at any one gate triggers a specific recovery path rather than rejecting the entire candidate set. Gate order stays fixed across all four placement arms (permute, fuse, retry, cost-based), so the same predicates execute in the same sequence no matter which arm is in play. The Rau termination proof depends on it: G3 (the RRT veto) must run strictly after G1/G2 but strictly before G4 so the resource snapshot it sees is the one the cost-sort produced.

The four gates draw on the cost tables documented in the [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md). G2 reads the constraint-attribute table parsed by `sub_97B770`, G3 reads the global RRT alongside the per-op latency view, and G4 walks the DSU at offset `+112` of the scheduler state. G1 fires first because it costs a single SwissTable probe.

### G1: Pending-Set Membership

The first gate is a membership probe against an Abseil-layout SwissTable rooted at offset `49 * 8 = 392` of the scheduler state. The table is seeded by the attribute parser alongside the DSU at `state + 112`; the full seeding picture lives in [Schedule Constraint Attributes — Twin Seeding](schedule-constraint-attributes.md#twin-seeding-dsu-and-pending-set). The probe runs first because it costs a single hash plus a 16-byte slot stride, and rejection on this gate holds the op over to the next placement attempt rather than killing it.

```c
bool gate_g1_pending_set_clean(SchedulerState *state, Op *op) {
    // Membership probe on the carry-state SwissTable seeded by the attribute
    // parser. Empty sentinel -4096, tombstone -8192; see container-fingerprints.md.
    return !pending_set_contains(state->pending_set, op);
}
```

### G2: Max-Depth Viability

The second gate consults the ConstraintMap that the attribute parser `sub_97B770` built from `tileas.schedule.constraint.max_depth`. The decompiled expression reads `*((int*)sub_94A550(state, op) + 2) <= 1`. The probe `sub_94A550` returns a pointer to the constraint slot; its third `i32` (offset `+8`) is the `max_depth` field the parser wrote from the MLIR attribute. The literal bound `1` is hard-coded into the cost-sort body.

```c
bool gate_g2_max_depth_viable(SchedulerState *state, Op *op) {
    /* ConstraintMap lookup; the max_depth field at byte offset +8
     * is written by the attribute parser sub_97B770 from
     * `tileas.schedule.constraint.max_depth`. The decompiled
     * expression `*((int*)slot + 2) <= 1` reads that same field. */
    ConstraintSlot *slot = sub_94A550(state, op);
    return slot->max_depth <= 1;
}
```

Failure on G2 means the op is unreachable at the current depth level. The placement driver marks the op dead in the snapshot for the current attempt; the retry arm picks it up once the depth horizon expands.

### G3: RRT Veto

The third gate is the resource veto `!sub_94A450(state+88, op)`. The probe at offset `+88 = 11 * 8` delegates to the canonical Rau RRT test in `sub_12D0800`. The op's per-op RRT footprint at `*(u64*)(op + 96)` must AND-clean against `globalRRT[(t + i) mod II]` for every cycle `i` of the footprint duration. This is the hard gate — lexicographic component one in the cost-model decomposition. No lattice element can sit above a state that fails G3.

```c
bool gate_g3_rrt_clean(SchedulerState *state, Op *op, uint32_t t) {
    /* Canonical Rau RRT probe. The per-op footprint at op+96
     * must not collide with the global RRT row mask at any of
     * the duration cycles starting at modulo cycle t. */
    const uint64_t *node_rows = op->footprint_rows;        /* op+96 */
    const RRT *global = state->global_rrt;                 /* state+88 */

    for (uint32_t i = 0; i < op->duration; ++i) {
        uint32_t row = (t + i) % global->ii;
        if ((global->rows[row] & node_rows[i]) != 0) {
            return false;
        }
    }
    return true;
}
```

Failure on G3 bumps the seat time forward by one cycle and reruns the same gate ladder against the next candidate cycle; the cost-sort itself does not change ordering on a G3 miss.

### G4: Leader-Group DSU Consistency

The fourth gate is `sub_96A7D0(state, &candidate, 1, &leader_gid, 1)`. It walks the DSU at offset `+112` of the scheduler state (parent-pointer table, `find` is `sub_976BE0`, `union` is `sub_976DE0`) and returns non-zero when the candidate's leader-gid find-root coincides with every already-committed group leader that shares the target cycle. The leader gids are parsed by `sub_97B770` from `tileas.schedule.constraint.gid` and `tileas.schedule.constraint.leader_gid`.

```c
bool gate_g4_leader_gid_consistent(SchedulerState *state, Op *op,
                                   uint32_t leader_gid) {
    /* DSU consistency check at scheduler state offset +112.
     * Two ops with the same leader_gid must share the same
     * depth (= start_cycle / II) for the seat to be legal. */
    return sub_96A7D0(state, &op, 1, &leader_gid, 1) != 0;
}
```

G4 is slot-agnostic at the bit-mask level but slot-dependent at the timing level — two ops in the same group must share the same depth. Fine-slot ties trigger most G4 rejections, for example two `tp_tmem_rd` candidates belonging to different leader gids competing for the same cycle. Failure on G4 forces the cost-sort to reorder the group rather than reject any single candidate; the driver retries with a different leader ordering before moving on to the next op.

### Gate Recovery Summary

Each gate has a distinct failure response. Treating them uniformly would either lose useful candidates (by killing on a recoverable G1) or waste retries (by reordering on a structurally impossible G3).

| Gate | Predicate | On Failure |
|---|---|---|
| G1 | `!sub_7E30D0(state+392, op)` | hold the op over to the next attempt |
| G2 | `sub_94A550(state, op) + 8 <= 1` | mark the op dead in the snapshot for the current attempt |
| G3 | `!sub_94A450(state+88, op)` | bump seat time by one cycle and retry |
| G4 | `sub_96A7D0(state, op, leader_gid, ...)` | force a different group ordering |

The G3 RRT veto ties the gate ladder to the cost tables in the slot model — the same global RRT the per-cycle pressure summariser `sub_12CEBF0` reads through the 9-element pool capacity vector is what G3 probes for resource conflicts. The latency view that `sub_12C8DF0` writes into the per-op pool is what the cost reducer reads to produce the ranking the gate ladder iterates over.

## Generator Selection

The driver chooses between the serial and cost-based generators on two thresholds. Small regions and forced-serial regions take the serial path because pipelining cannot help — the compile-time savings outweigh any throughput improvement the cost-based generator could buy. Regions with more than the threshold operation count and at least one resource-bearing op enter the cost-based path because their pipelined throughput dominates compile time.

| Trigger | Selected generator |
|---|---|
| `force_serial_execution` attribute on the region | serial |
| op count below `serial_threshold` (default 8) | serial |
| no async pipeline, TMA, or WGMMA op present | serial |
| otherwise | cost-based |

The thresholds are conservative. Falling back from cost-based to serial is correct but slow; the inverse — taking the cost-based path on a region that the serial generator would have handled — is also correct but burns compile time on a search whose result is identical to the serial walk's.

### Selector Predicate

The driver reads the region's MLIR attributes and op-count summary and applies a single ordered predicate. The first matching rule wins.

```c
ScheduleStrategy select_strategy(Region *region, ScheduleOptions *opts) {
    if (region->attrs.force_serial_execution) {
        return STRATEGY_SERIAL;                          // attribute trumps everything
    }
    if (region->op_count < opts->serial_threshold /* 8 */) {
        return STRATEGY_SERIAL;                          // not worth the cost-based price
    }
    if (!region->summary.has_async_pipeline &&
        !region->summary.has_tma            &&
        !region->summary.has_wgmma) {
        return STRATEGY_SERIAL;                          // no resource-bearing op
    }
    return STRATEGY_COST_BASED;
}
```

The `has_async_pipeline`, `has_tma`, and `has_wgmma` flags are byproducts of the per-block summary that the constraint builder produces — the same pass that computes the per-op resource vectors documented in [Resource Constraint Builder and RRT](resource-constraint-builder-and-rrt.md#per-op-resource-vector-encoding). Reusing that data is the only practical way to keep the selector's cost below the serial generator's own cost; running the selector on every region for free is what allows the conservative thresholds.

A region with an async pipeline but `force_serial_execution = true` still picks serial — the attribute is the override of last resort. A region with no resource-bearing ops but the attribute unset still picks serial because the cost-based path's benefit comes entirely from packing tensor-memory and SMEM transports; with neither present, the cost-based path's cost vector reduces to the structural-distance term alone, which the serial generator's program-order traversal already satisfies.

## Strategy Orchestration

Inside the cost-based path the driver runs a fixed strategy ladder rather than a single attempt. Cheap strategies run first: a Rau-style refinement, then a deepest-depth retry, then the initial placement. The driver escalates to heavier cost-based placement only when those refuse the candidate. When even the cost-based pass fails, the driver clears intermediate scheduling state and reruns initial and cost-based placement from a known-empty starting point. Each rung returns success immediately on a match, so the ladder short-circuits at the first strategy that produces a feasible schedule.

```c
bool run_schedule_strategies(ScheduleGenState *state) {
    if (try_rau_refinement       (state)) return true;
    if (try_deepest_retry        (state)) return true;
    if (try_initial_placement    (state)) return true;
    if (try_cost_based_placement (state)) return true;

    clear_intermediate_schedule_state(state);
    if (try_initial_placement    (state)) return true;
    return try_cost_based_placement(state);
}
```

The order is pragmatic. Cheap strategies run first, cost-based placement is the most expensive fallback, and the clear-and-retry tail handles the case where intermediate state accumulated during earlier strategies blocks a feasible schedule that the initial placement would have found from scratch.

## Constraints Consumed

Several constraint families shape which candidates the cost-based path even considers. Hard constraints — force-serial execution, max depth, resource footprint — gate legality. Soft constraints — same-depth, group unions, structural shape — rank only candidates that already cleared the hard gates. The serial path consumes only the force-serial-execution constraint; every other family is silently ignored.

> ⚡ **QUIRK — serial generator silently drops every constraint except force-serial-execution**
> The serial scheduler accepts the same `Constraint` set as the cost-based path but consults only `force-serial-execution`; same-depth, group-union, structural-shape, max-depth, and resource-footprint constraints are all dropped without warning when the serial generator runs. A frontend that pins a critical resource bound expecting both paths to honour it sees the cost-based schedule respect it and the serial schedule violate it — and the user-facing diagnostic stream is identical in both cases. Bug reports of "my constraint stopped working after `--force-serial-schedule`" land here.

| Constraint | Effect |
|---|---|
| force-serial execution | selects or emulates serial ordering |
| max depth | prevents seating a candidate beyond a configured depth |
| same depth | forces related operations to share a depth or stage relation |
| union/group constraints | tie operations into shared scheduling groups |
| structural constraints | rank or reject candidates based on dependency shape |
| resource constraints | reject candidates whose RRT footprint conflicts |

Resolution of these constraints happens before materialization. The later `Schedule::solve` pass should see only the final analysis, not the live constraint-search state.

## Output Contract

Both generators publish the same logical analysis so the downstream materializer can consume either result without dispatch. The analysis carries the operation-to-node map, an ordered operation/node list, the per-op `(stage, order)` assignment, the dependency edges, optional slot/depth/resource annotations populated only by the optimized path, and a success or failure flag. The materializer should not need to know which generator produced the analysis — except for diagnostics or instrumentation.

## Usage and Contract

Callers select a generator by setting the schedule strategy field on the `ScheduleOptions` record before invoking `TileASGenerateSchedule`. The serial generator consumes only the operation tree of the scheduled block plus the `tileas.schedule.constraint.force_serial_execution` attribute; it ignores the per-op slot, latency, and capacity inputs. The cost-based generator additionally reads the `tileas.schedule.constraint.gid`, `leader_gid`, and `max_depth` attributes parsed by [Schedule Constraint Attributes](schedule-constraint-attributes.md), the per-op footprint vectors from the [Resource Constraint Builder](resource-constraint-builder-and-rrt.md), and the 9-element pool-capacity vector from the [Blackwell Pipeline 15-Slot Model — Pool Capacity Vector](blackwell-pipeline-15-slot-model.md#pool-capacity-vector). Both paths produce `ScheduleAnalysis` with the same field set — the optimized path simply fills the optional slot/depth/resource cells that the serial path leaves zeroed. Consumers must treat the `(stage, order)` pair as the public ordering key and ignore the optional cells unless they are explicitly probing the optimized path's annotations.

