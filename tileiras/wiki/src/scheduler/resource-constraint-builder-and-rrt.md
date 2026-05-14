# Resource Constraint Builder and RRT

## Abstract

The resource constraint builder is the pipeline that produces per-op `NodeRRT` footprints and commits chosen placement rows back into the global RRT during `TileASGenerateSchedule`. The reservation-table model itself — bitset rows per cycle, probe-and-commit semantics, the lower-bound formula, and the galloping-plus-binary `II` search — lives in [Modulo Scheduler and Rau](modulo-scheduler-and-rau.md). This page picks up where that one leaves off: how the builder constructs the footprints, how the MII split is computed, and how the apply-mode driver writes accepted rows back into the bitset.

The builder lives in schedule generation, not pipe materialization. `MaterializeSchedule` consumes the completed schedule analysis and never reruns the `II` search.

## Slot Encoding

The scheduling model uses one-based pipeline slot identifiers. The RRT row bit for slot `slot_id` is `1 << (slot_id - 1)`. Blackwell currently uses up to 24 slot identifiers, which fits in one 64-bit row; coarse slots group broad resource families while fine slots model concrete issue and transport pressure. [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md) documents the fine-slot taxonomy.

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

Once every block is summarised, the builder reduces them into per-resource pressure counts and feeds those counts into the lower-bound calculation and the feasibility probe.

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

When the builder detects that an op would force a register spill if seated at its earliest legal cycle, it adds a soft constraint that biases the placement driver away from that cycle without making it illegal. The constraint is a cost term, not a legality predicate — the placement driver may still seat the op at the original cycle if no cheaper alternative is feasible, and the bias only ranks candidates that already cleared the hard resource and dependence gates.

The cost term encodes as a small integer surcharge attached to the candidate cycle for that specific op. The cost-based arm reads the surcharge as a separate component of its lexicographic cost vector, ranked below the hard resource gate but above structural distance. Multiple spill-bias surcharges for the same op accumulate by addition — the builder caps the accumulated bias so a single op cannot push every cycle out of the feasible region.

```c
void tryAddConstraintToAvoidRegSpilling(ScheduleState *state, Op *op,
                                        uint32_t earliest_cycle) {
    PressureEstimate p = estimate_register_pressure_at(state, op, earliest_cycle);
    if (p.peak <= p.budget) {
        return;                          // no spill predicted; no constraint needed
    }

    // Encode bias as a cost surcharge on the (op, cycle) pair. Range and cap
    // keep accumulated surcharges from saturating the cost vector.
    uint32_t surcharge = clamp((p.peak - p.budget) * SPILL_SURCHARGE_WEIGHT,
                               0, SPILL_SURCHARGE_CAP);
    cost_surcharge_add(state->cost, op, earliest_cycle, surcharge);
}
```

The surcharge is a hint that ranks otherwise-equivalent candidates; it never rejects a seat by itself. A placement that satisfies every hard constraint but carries spill surcharges at every cycle still commits — the schedule is correct, only the register-pressure heuristic is unhappy.

The same bit-row geometry that drives the per-op footprints resurfaces in the schedule analyser when it computes stage counts and emits diagnostics. The 24-bit width and the per-cycle qword layout therefore belong to the schedule's serialisation contract, not an apply-mode-only detail.

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
