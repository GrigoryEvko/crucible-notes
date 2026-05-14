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

The serial generator runs in four steps that together implement a deterministic dataflow walk. If the caller already produced a refined schedule, the generator reuses it; otherwise it allocates a fresh `Schedule` and root node, walks the region in program order to emit a node per operation, walks it a second time to wire operand edges, then checks the resulting graph for a valid topological order. No resource search, no II selection, no cost ranking on this path.

```c
bool generate_serial_schedule(ScheduleOut *out,
                              Operation *root,
                              Schedule *refine_from) {
    if (refine_from != NULL) {
        out->schedule = refine_from;
        out->refined = true;
        return true;
    }

    Schedule *schedule = allocate_schedule();
    Node *root_node = schedule->create_root(root);

    walk_region(root, [&](Operation *op) {
        schedule->create_node(op, root_node);
    });

    walk_region(root, [&](Operation *op) {
        for (Value operand : op->operands()) {
            schedule->add_edge(operand.defining_op(), op);
        }
    });

    out->schedule = schedule;
    out->refined = schedule->is_valid_topological_order();
    return out->refined;
}
```

The serial generator never builds an RRT, never searches for `II`, and never ranks multiple candidate seats. If validation fails, the caller must pick a stronger strategy or report failure.

## Cost-Based Generator

The cost-based generator is an iterative placement algorithm. It extracts candidates, applies dependency and resource constraints, ranks admissible candidates lexicographically, and seats operations while updating the RRT and schedule maps.

```c
bool generate_cost_based_schedule(ScheduleGenState *state,
                                  CandidateSet candidates,
                                  ConstraintSet constraints,
                                  ResourceBuilder *resources) {
    initialize_candidate_state(state, candidates, constraints);

    while (!all_candidates_scheduled(state)) {
        CandidateList ready = collect_ready_candidates(state);
        if (ready.empty()) {
            return false;
        }

        CostVectorList costs = {};
        for (Candidate c : ready) {
            if (!passes_hard_constraints(c, constraints, resources)) {
                continue;
            }
            costs.push_back(score_candidate(c, state, resources));
        }

        stable_sort(costs, compare_cost_vectors);

        if (!seat_best_candidate(state, costs, resources)) {
            return false;
        }
    }

    return true;
}
```

The cost vector is lexicographic:

| Component | Role |
|---|---|
| hard resource gate | rejects candidates that violate depth, resource mask, or already-scheduled constraints |
| pipeline-slot pressure | prefers placements that reduce issue-slot and transport pressure |
| structural distance | breaks ties using dependence distance and critical-path shape |

Do not collapse this into one scalar without proving equivalence. The hard gate decides whether a candidate is legal; the later components only rank legal candidates.

## Admission Gates

Before the placement driver `sub_981D50` commits a seat for a candidate op, four ordered gates run against every candidate the cost-sort surfaces. All four must pass for the seat to commit; failure at any one gate triggers a specific recovery path rather than rejecting the entire candidate set. Gate order stays fixed across all four placement arms (permute, fuse, retry, cost-based), so the same predicates execute in the same sequence no matter which arm is in play. The Rau termination proof depends on it: G3 (the RRT veto) must run strictly after G1/G2 but strictly before G4 so the resource snapshot it sees is the one the cost-sort produced.

The four gates draw on the cost tables documented in the [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md). G2 reads the constraint-attribute table parsed by `sub_97B770`, G3 reads the global RRT alongside the per-op latency view, and G4 walks the DSU at offset `+112` of the scheduler state. G1 fires first because it costs a single SwissTable probe.

### G1: Pending-Set Membership

The first gate is the membership probe `!sub_7E30D0(state+392, op)`. It rejects any candidate already on the pending set — an Abseil-layout SwissTable rooted at offset `49 * 8 = 392` of the scheduler state. The probe runs first because it costs a single hash plus a 16-byte slot stride; rejection on this gate holds the op over to the next placement attempt rather than killing it.

```c
bool gate_g1_pending_set_clean(SchedulerState *state, Op *op) {
    /* Membership probe on the "already-scheduled" SwissTable.
     * H1 mixer 0x9DDFEA08EB382D69, empty sentinel -4096,
     * tombstone -8192. Returns true when op is NOT yet scheduled. */
    return sub_7E30D0(state->pending_set /* state+392 */, op) == 0;
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

## Strategy Orchestration

The optimized path is a fixed strategy ladder rather than a single attempt. The driver tries cheap strategies first — a Rau-style refinement, then a deepest-depth retry, then the initial placement — and escalates to heavier cost-based placement only when the cheaper passes refuse the candidate. When even the cost-based pass fails, the driver clears intermediate scheduling state and reruns the initial and cost-based passes from a known-empty starting point. Each rung returns success immediately on a match, so the ladder short-circuits at the first strategy that produces a feasible schedule.

```c
bool run_schedule_strategies(ScheduleGenState *state) {
    if (try_rau_refinement(state)) {
        return true;
    }
    if (try_deepest_retry(state)) {
        return true;
    }
    if (try_initial_placement(state)) {
        return true;
    }
    if (try_cost_based_placement(state)) {
        return true;
    }

    clear_intermediate_schedule_state(state);

    if (try_initial_placement(state)) {
        return true;
    }
    return try_cost_based_placement(state);
}
```

The order is pragmatic — cheaper strategies run first, cost-based placement is the most expensive fallback.

## Constraints Consumed

Several constraint families shape which candidates the cost-based path even considers. Hard constraints — force-serial execution, max depth, resource footprint — gate legality. Soft constraints — same-depth, group unions, structural shape — rank only candidates that already cleared the hard gates. The serial path consumes only the force-serial-execution constraint; every other family is silently ignored.

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

Callers select a generator by setting the schedule strategy field on the `ScheduleOptions` record before invoking `TileASGenerateSchedule`. The serial generator consumes only the operation tree of the scheduled block plus the `tileas.schedule.constraint.force_serial_execution` attribute; it ignores the per-op slot, latency, and capacity inputs. The cost-based generator additionally reads the `tileas.schedule.constraint.gid`, `leader_gid`, and `max_depth` attributes parsed by [Schedule Constraint Attributes](schedule-constraint-attributes.md), the per-op footprint vectors from the [Resource Constraint Builder](resource-constraint-builder-and-rrt.md), and the 9-element pool-capacity vector from the [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md). Both paths produce `ScheduleAnalysis` with the same field set — the optimized path simply fills the optional slot/depth/resource cells that the serial path leaves zeroed. Consumers must treat the `(stage, order)` pair as the public ordering key and ignore the optional cells unless they are explicitly probing the optimized path's annotations.

