# Modulo Scheduler and Rau-Style Placement

## Abstract

Tileiras software-pipelines TileAS loops with a Rau-style modulo scheduler. The scheduler searches for an initiation interval, places operations into cycles modulo that interval, and respects both data dependences and target-machine resource capacity. This is the throughput-critical scheduler for loops that use Blackwell tensor memory, shared memory, WGMMA, TMA, named barriers, and async-copy queues.

The output of this pass is schedule analysis: stage, order, resource placement, and depth information. A later materialization pass consumes that analysis to emit `Pipe_` and `Mutex_` IR.

## Scheduling Model

A software-pipelined loop overlaps multiple logical iterations. If the initiation interval is `II`, the steady-state loop starts one new iteration every `II` cycles. Smaller `II` means higher throughput, but only if every recurrence and resource constraint can be satisfied.

Tileiras starts from the usual lower bound:

```text
II >= max(resource_mii, recurrence_mii, fine_density_mii, dependency_mii)
```

| Bound | Meaning |
|---|---|
| resource MII | lower bound from total resource demand and per-cycle capacity |
| recurrence MII | lower bound from dependence cycles crossing loop iterations |
| fine-density MII | lower bound from resource groups with fractional or pooled capacity |
| dependency MII | lower bound from known longest dependence depth |

## Resource Reservation Table

The scheduler represents resource occupancy with a Resource Reservation Table. The global table has one row per cycle modulo `II`; each row is a bitset of resources. Each operation has a footprint table with one row per occupied cycle.

```c
bool rrt_probe(const RRT *global, const NodeRRT *node, uint32_t start) {
    for (uint32_t k = 0; k < node->duration; ++k) {
        uint32_t row = (start + k) % global->ii;
        if ((global->rows[row] & node->rows[k]) != 0) {
            return false;
        }
    }

    return true;
}

void rrt_commit(RRT *global, const NodeRRT *node, uint32_t start) {
    for (uint32_t k = 0; k < node->duration; ++k) {
        uint32_t row = (start + k) % global->ii;
        global->rows[row] |= node->rows[k];
    }
}
```

Probe-before-commit is mandatory. Committing a partially probed footprint can make retry behavior nondeterministic.

## Candidate-II Search

The scheduler tries the lower bound first, then increases or searches until it finds a feasible interval or reaches the configured cap.

```c
bool schedule_loop(Schedule *sched, RauBounds bounds) {
    uint32_t lower = max4(bounds.resource_mii,
                         bounds.recurrence_mii,
                         bounds.fine_density_mii,
                         bounds.dependency_mii);

    for (sched->ii = lower; sched->ii <= bounds.ii_cap; ++sched->ii) {
        clear_resource_table(&sched->global_rrt, sched->ii);

        if (place_all_groups_at_current_ii(sched)) {
            sched->stage_count = compute_stage_count(sched);
            return true;
        }
    }

    sched->failed = true;
    return false;
}
```

Some subproblems use galloping plus binary search rather than a linear increment. Both forms obey the same semantic contract: find the smallest feasible value according to the probe.

## Placement Arms

For a fixed `II`, Tileiras tries a deterministic sequence of placement arms:

| Arm | Role |
|---|---|
| permute | cheap priority order and earliest legal seat |
| fuse | merge compatible groups to improve packing |
| retry | use a snapshot overlay to skip or reconsider failed operations |
| cost-based | choose the lowest-cost legal placement with RRT-backed scoring |

The first successful arm wins for the current group. If the full group pass fails, the driver clears temporary state and reruns heavier retry/fallback arms.

```c
bool place_group(Schedule *sched, Group group, RetrySnapshot *snapshot) {
    if (try_permute(sched, group)) {
        return true;
    }
    if (try_fuse(sched, group)) {
        return true;
    }
    if (try_retry(sched, group, snapshot)) {
        return true;
    }
    return try_cost_based(sched, group, snapshot);
}
```

## Permute Arm

The permute arm sorts the current group by a predecessor-priority map and seats each operation at the earliest legal cycle.

```c
bool try_permute(Schedule *sched, Group group) {
    PriorityMap priority = build_predecessor_priority(group);
    stable_sort(group.nodes, [&](Node *a, Node *b) {
        return priority.less(a, b);
    });

    for (Node *node : group.nodes) {
        if (!seat_earliest_legal_cycle(sched, node)) {
            return false;
        }
    }

    return true;
}
```

## Fuse Arm

The fuse arm merges compatible scheduling groups. It must reject fusions that violate dependence order or MLIR nesting relationships.

```c
bool groups_can_fuse(OperationSet a, OperationSet b) {
    for (Operation *left : a) {
        for (Operation *right : b) {
            if (has_required_order(left, right)) {
                return false;
            }
            if (is_proper_ancestor(left, right) || is_proper_ancestor(right, left)) {
                return false;
            }
        }
    }

    return true;
}
```

Stable sorting is part of the contract. It keeps tied fusions deterministic across builds and platforms.

## Retry and Snapshot Overlay

Retry uses a snapshot map instead of mutating the canonical group state. A failed attempt can mark a candidate dead in the overlay, and later strategies can clear or rebuild the overlay without corrupting the schedule's operation list.

```c
bool try_retry(Schedule *sched, Group group, RetrySnapshot *snapshot) {
    for (Node *node : group.nodes) {
        if (snapshot_is_dead(snapshot, node)) {
            continue;
        }

        if (!seat_earliest_legal_cycle(sched, node)) {
            snapshot_mark_dead(snapshot, node);
            return false;
        }
    }

    return true;
}
```

## Cost-Based Arm

The cost-based arm ranks legal placements instead of accepting the first legal seat.

```c
bool try_cost_based(Schedule *sched, Group group, RetrySnapshot *snapshot) {
    for (Node *node : group.nodes) {
        Placement best = find_lowest_cost_legal_placement(sched, node, snapshot);
        if (!best.valid) {
            snapshot_mark_dead(snapshot, node);
            return false;
        }

        rrt_commit(&sched->global_rrt, &node->footprint, best.start);
        node->start = best.start;
    }

    return true;
}
```

Cost ranking is lexicographic. Resource legality is a hard gate; pressure and structural distance rank candidates that already pass.

## Prologue and Stage Count

After successful placement, the scheduler computes the latest occupied cycle. This determines the number of overlapped stages in the steady-state loop.

```c
uint32_t compute_stage_count(const Schedule *sched) {
    uint32_t end = 0;

    for (Node *node : sched->nodes) {
        if (node->start < 0) {
            return UINT32_MAX;
        }

        uint32_t finish = (uint32_t)node->start + node->latency;
        end = max(end, finish);
    }

    return ceil_div(end + 1, sched->ii);
}
```

## Diagnostics

When resource placement fails, useful diagnostics should include:

- candidate `II`;
- operation duration and requested slot footprint;
- global RRT rows at the conflicting modulo cycles;
- dependency window for the operation;
- active capacity pool that rejected the candidate, if any;
- group and stage/order information for the failed operation.

These diagnostics let users distinguish an impossible loop body from a heuristic failure.

## Cross-References

[Resource Constraint Builder and RRT](resource-constraint-builder-and-rrt.md) documents footprint construction and feasible-II probing. [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md) documents the resource slots. [Schedule Solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) explains the materialization boundary.
