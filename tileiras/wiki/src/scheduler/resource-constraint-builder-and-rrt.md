# Resource Constraint Builder and RRT

## Abstract

The resource constraint builder turns a candidate TileAS schedule into a resource-feasibility problem. It builds reservation rows for each operation, computes a lower bound on the initiation interval, probes candidate intervals with a Resource Reservation Table, and optionally commits the accepted placement back into the schedule state.

The builder lives in schedule generation, not pipe materialization. It runs while `TileASGenerateSchedule` searches for a resource-feasible modulo schedule. `MaterializeSchedule` later consumes the completed schedule analysis and never repeats the initiation-interval search.

## Reservation Table Model

An RRT carries one row per cycle modulo the candidate initiation interval, each row a bitset of resource classes. An operation owns a footprint table whose rows describe which resources it occupies at each cycle of its duration.

```c
typedef struct ResourceTable {
    uint64_t *rows;
    uint32_t ii;
} ResourceTable;

typedef struct OperationFootprint {
    uint64_t *rows;
    uint32_t duration;
} OperationFootprint;
```

Placement runs as a pure probe followed by an explicit commit. The probe checks for conflicts; commit ORs the footprint into the global table.

```c
bool rrt_can_place(const ResourceTable *global,
                   const OperationFootprint *op,
                   uint32_t start) {
    for (uint32_t k = 0; k < op->duration; ++k) {
        uint32_t row = (start + k) % global->ii;
        if ((global->rows[row] & op->rows[k]) != 0) {
            return false;
        }
    }

    return true;
}

void rrt_commit(ResourceTable *global,
                const OperationFootprint *op,
                uint32_t start) {
    for (uint32_t k = 0; k < op->duration; ++k) {
        uint32_t row = (start + k) % global->ii;
        global->rows[row] |= op->rows[k];
    }
}
```

The modulo formulation handles footprints longer than `II` because every occupied cycle folds back through `(start + k) % II`. A long instruction therefore conflicts with itself and with other operations across repeated steady-state iterations.

## Apply and Commit Modes

The builder has two modes.

| Mode | Purpose |
|---|---|
| apply/check | compute lower bounds, search for a feasible `II`, and record placement data |
| commit | take an accepted schedule and write the chosen rows/depths into the global maps |

Apply mode is the expensive path. It builds per-block summaries, computes the minimum feasible interval, and searches for the smallest `II` that passes the evaluator. Commit mode is deterministic — it walks accepted block records, finds the first available modulo row for each footprint, commits the row, and records the selected depth.

```c
bool resource_builder_run(ResourceBuilder *builder,
                          ScheduleState *schedule,
                          bool apply_mode) {
    if (apply_mode) {
        return compute_and_apply_candidate_schedule(builder, schedule);
    }

    commit_candidate_schedule(builder, schedule);
    return true;
}
```

## Lower-Bound Calculation

The candidate search starts at the maximum of three bounds.

| Bound | Meaning |
|---|---|
| resource bound | maximum resource demand over the scheduler's slot classes |
| density bound | fine-grained pressure bound for slot groups with fractional capacity |
| dependency bound | longest per-node dependence-chain depth already known to the scheduler |

```c
uint32_t compute_minimum_ii(const ScheduleState *schedule) {
    uint32_t resource = compute_resource_mii(schedule);
    uint32_t density = compute_fine_density_mii(schedule);
    uint32_t dependency = compute_dependency_mii(schedule);

    return max3(resource, density, dependency);
}
```

The resource component counts demand across the resource-slot vocabulary. The density component captures capacity pressure that no single bit can represent. The dependency component blocks the search from trying intervals that violate recurrence or depth constraints regardless of resource availability.

## Feasible-II Search

The builder searches for the smallest feasible interval with a galloping phase followed by binary refinement. The common case stays fast when the lower bound is already feasible, and a linear scan is avoided when the feasible interval sits far above it.

```c
bool find_smallest_feasible_ii(SearchResult *out,
                               FeasibilityFn probe,
                               uint32_t lower,
                               uint32_t upper) {
    uint32_t lo = lower;
    uint32_t hi = lower;

    while (hi < upper && !probe(hi, out)) {
        uint32_t next = hi * 2;
        hi = next < upper ? next : upper;
    }

    if (!probe(hi, out)) {
        out->found = false;
        return false;
    }

    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2;
        SearchResult candidate = {};

        if (probe(mid, &candidate)) {
            hi = mid;
            *out = candidate;
        } else {
            lo = mid + 1;
        }
    }

    out->ii = hi;
    out->found = true;
    return true;
}
```

The probe callback owns the actual resource placement attempt. It may consult several resource views, but from the search driver's perspective it must behave like a pure predicate — the accepted placement is copied into the output only when the probe succeeds.

## Slot Encoding

The scheduling model uses one-based pipeline slot identifiers. The RRT row bit for a slot is `slot_id - 1`.

```c
uint64_t slot_mask(uint32_t slot_id) {
    return 1ull << (slot_id - 1);
}
```

The current Blackwell model uses up to 24 slot identifiers, so one 64-bit row covers it. Coarse slots group broad resource families; fine slots model concrete issue and transport pressure. [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md) documents the fine-slot taxonomy.

## Per-Block Summaries

The builder visits each scheduled block and records the operation tags it uses. Two structures coexist: an open-addressed set de-duplicates tags, while a deterministic list preserves iteration order for stable diagnostics and repeatable scheduling.

```c
BlockSummary summarize_block(Block *block) {
    BlockSummary summary = {};

    for (OperationNode *node : block->scheduled_nodes()) {
        if (summary.tags.insert(node->slot_id)) {
            summary.ordered_tags.push_back(node->slot_id);
        }
    }

    return summary;
}
```

Once every block is summarized, the builder reduces them into per-resource pressure counts and feeds those counts into the lower-bound calculation and feasibility probe.

## Constraint-Builder Pipeline

The pipeline that populates the per-op `NodeRRT` footprints before scheduling has a small, fixed shape. The top-level entry point `sub_98BBE0` is a 2-way dispatcher keyed on its third argument, the build-mode flag: `a3 == 0` selects build mode and tail-calls into `sub_98A3B0` (the 1 296 LOC builder body that walks the dependence graph, materializes per-op slot footprints, and stages them on the per-block constraint state); `a3 == 1` selects apply mode and tail-calls into `sub_988710`, which consumes the staged state and writes resource bits into the global RRT. Both modes share the same per-block constraint record, so the dispatcher is purely a phase selector — no per-call setup beyond the branch.

Before placement starts, the driver computes the minimum feasible initiation interval from a three-way split in `sub_989380`. Each component is its own helper: `sub_9890C0` returns `RecMII` by walking the dependence graph for cycles that cross loop iterations, `sub_989160` returns `FineMII` from fine-grained dependence distances within a single iteration, and `sub_989340` returns `DepMII` by reading the cached per-op depth at field offset `+0x48` on the op record. The split helper takes the maximum, and that becomes the starting value of `Schedule.ii` for the placement driver `sub_981D50` documented in [Modulo Driver and Chain](modulo-driver-or-chain.md).

```c
uint32_t compute_min_ii(const ScheduleState *schedule) {
    uint32_t rec  = compute_rec_mii(schedule);     // sub_9890C0
    uint32_t fine = compute_fine_mii(schedule);    // sub_989160
    uint32_t dep  = compute_dep_mii(schedule);     // sub_989340, reads op[+0x48]

    uint32_t mii = rec;
    if (fine > mii) mii = fine;
    if (dep  > mii) mii = dep;
    return mii;
}
```

## 24-Slot Apply Driver

Apply mode walks a 24-bit resource row stored as a qword at field offset `+80` on each block record. Bit `i` set in that qword means resource class `i` is occupied by the current op on cycle 0 of its footprint. Multi-cycle footprints occupy companion qwords at `+88`, `+96`, and so on — one qword per footprint cycle, contiguous and in cycle order. `sub_989410` is the per-block apply driver, iterating over the staged op list for one block and updating the qword row stack. `sub_989BE0` is the per-op variant that runs the same update for a single op record without the block-level iteration.

The active class count matches the Blackwell pipeline-resource model documented in [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md): 8 bits for TMEM/SMEM banks, 4 bits for WGMMA queue slots, 4 bits for TMA descriptors, 4 bits for named barriers, and 4 bits for cp.async queues. That partitioning is why a single 64-bit qword covers each cycle row, and why the apply drivers can read and write each row with a single load/store rather than a vector spread.

## Bit Extraction Idiom

The decompilation tests slot occupancy with the x86 idiom `shl rax, cl` followed by `bt rdx, rax`, where `cl == slot_id - 1`. The `-1` bias is the canonical fingerprint — the dispatcher uses 1-based slot identifiers in its public interface and 0-based bit positions in the qword. Any code that performs a `(slot_id - 1)` shift before a `bt`-style test against a resource qword belongs to the constraint pipeline.

```c
static inline bool slot_occupied(uint64_t row, uint32_t slot_id) {
    uint32_t bit = slot_id - 1;        // shl rax, cl
    return ((row >> bit) & 1ull) != 0; // bt  rdx, rax
}
```

## Soft Constraints and Bit-Row Geometry

When the builder detects that an op would spill if scheduled at its earliest start, it calls `tryAddConstraintToAvoidRegSpilling` at `sub_9762E0` to add a soft constraint that biases the placement driver away from that start cycle. The constraint is a hint, not a hard reject — the placement driver may still seat the op at the original cycle if no cheaper alternative is feasible, and the soft constraint feeds the cost-based arm rather than the legality probe.

The same bit-row geometry resurfaces in the schedule analyzer when it computes stage counts and emits diagnostics. The 24-bit width and the per-cycle qword layout therefore belong to the schedule's serialization contract, not an apply-mode-only detail.

## Helper Table

| Function | Size | Role |
|---|---:|---|
| `sub_98BBE0` | — | 2-way build/apply dispatcher keyed on `a3` |
| `sub_98A3B0` | 1 296 LOC | Build-mode body, populates per-op footprints |
| `sub_988710` | — | Apply-mode body, writes staged state into the global RRT |
| `sub_989380` | — | MII split — `max(RecMII, FineMII, DepMII)` |
| `sub_9890C0` | — | `RecMII` from recurrence cycles |
| `sub_989160` | — | `FineMII` from fine-grained dependence distances |
| `sub_989340` | — | `DepMII` from per-op depth at `+0x48` |
| `sub_989410` | — | 24-slot per-block apply driver |
| `sub_989BE0` | — | 24-slot per-op apply driver |
| `sub_9762E0` | — | `tryAddConstraintToAvoidRegSpilling` soft-constraint hook |

## Usage and Contract

The builder runs inside `TileASGenerateSchedule`, invoked twice per schedule attempt — once in build mode (`a3 == 0`) to materialise per-op footprints and once in apply mode (`a3 == 1`) to commit accepted rows. Build mode consumes the per-op slot identifier, duration, and capacity-pool counts produced by the Blackwell slot classifier, plus the dependence graph for the MII split. Apply mode consumes the accepted `(stage, order)` placement and writes the chosen footprint rows into the global RRT at qword offsets `+80, +88, +96, ...` on each block record. The builder publishes the smallest feasible `II`, the per-op start cycles, and the populated RRT into the surrounding `ScheduleState`; downstream consumers — the placement driver, the cost evaluators, and the materializer — read those fields without rerunning the search.

## Cross-References

[Modulo Scheduler and Rau](modulo-scheduler-and-rau.md) consumes the `II` and the populated RRT this builder produces. [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md) defines the slot identifiers and capacity pools the footprints reference. [Modulo Driver and 4-Arm OR-Chain](modulo-driver-or-chain.md) probes the global RRT through Arms 1 and 3's commit paths. [Schedule Solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) consumes the `tryAddConstraintToAvoidRegSpilling` hints during cost ranking.
