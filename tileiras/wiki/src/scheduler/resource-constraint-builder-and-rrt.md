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

Before placement starts, the driver computes the minimum feasible initiation interval from a three-way split in `sub_989380`. Each component is its own helper: `sub_9890C0` returns `RecMII` by walking the dependence graph for cycles that cross loop iterations, `sub_989160` returns `FineMII` from fine-grained dependence distances within a single iteration, and `sub_989340` returns `DepMII` by reading the cached per-op depth at field offset `+0x48` on the op record. The split helper takes the maximum, and that becomes the starting value of `Schedule.ii` for the placement driver `sub_981D50` documented in [Modulo Driver and 4-Arm OR-Chain — Driver Signature](modulo-driver-or-chain.md#driver-signature).

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

## Per-Op Resource-Vector Encoding

Each op enters the builder as an MLIR operation with operand types and dialect attributes; it leaves as a resource vector — a small array of `(slot_id, duration, occupancy)` triples plus an optional capacity-pool count. The builder reads the op's opcode to pick the primary slot, reads the operand and result types to pick the transport slot, and reads the latency family to pick the duration. Occupancy stays at `1` for every singleton transport and rises only for the capacity pools whose caps are above `1`.

The triples are what the cost reducer ranks and what the apply driver writes into the qword row stack. They are the single canonical input/output of the builder body.

### Concrete Encodings

The four ops most worth pinning down are the TMA tiled load, the WGMMA matmul, the SMEM write, and the SMEM read. Every other op in the Blackwell dialect either reduces to one of these four or composes them.

```c
// nv_tileas.async.tiled_tma_load — TMA descriptor parked on slot 12 (tma),
// tensor payload flowing through slot 16 (tp_smem_wr). Both stay live for the
// full TMA round-trip duration of 8 cycles. Occupancy is 1 on each row —
// singleton transports cannot share.
ResourceVector encode_tiled_tma_load(Operation *op) {
    return (ResourceVector){
        .triples = { { .slot = 12, .duration = 8, .occupancy = 1 },
                     { .slot = 16, .duration = 8, .occupancy = 1 } },
        .n_triples = 2,
        .pool_counts = { /* no pool pressure beyond singleton rows */ },
    };
}

// nv_tileas.async.wgmma — issue stage on slot 11 (tc_and_mma), transport on
// slot 19 (tp_mma). The MMA accumulator latency is 16 cycles, but the
// scheduler models only the 8-cycle issue window in which the warpgroup
// holds the slots; the rest is dependence latency, not slot occupancy.
ResourceVector encode_wgmma(Operation *op) {
    return (ResourceVector){
        .triples = { { .slot = 11, .duration = 8, .occupancy = 1 },
                     { .slot = 19, .duration = 8, .occupancy = 1 } },
        .n_triples = 2,
        .pool_counts = { /* TMEM-bank pressure pushed through pool index 1 */ },
    };
}

// nv_tileas.async.smem_write — single-row footprint on slot 16 (tp_smem_wr)
// for 7 cycles. The only op that participates directly in the SMEM byte
// budget pool (pool index 5) so the SMEM byte-budget cap of 232 448 sees the
// store size accumulated across all live writes.
ResourceVector encode_smem_write(Operation *op, uint32_t store_bytes) {
    return (ResourceVector){
        .triples = { { .slot = 16, .duration = 7, .occupancy = 1 } },
        .n_triples = 1,
        .pool_counts = { [5] = store_bytes },     // SMEM byte budget
    };
}

// nv_tileas.async.smem_read — symmetric mirror of smem_write on slot 15
// (tp_smem_rd). 7-cycle hold, no pool pressure.
ResourceVector encode_smem_read(Operation *op) {
    return (ResourceVector){
        .triples = { { .slot = 15, .duration = 7, .occupancy = 1 } },
        .n_triples = 1,
        .pool_counts = { /* read transport is row-only */ },
    };
}
```

The builder's classifier is a flat switch on the dialect opcode; every case sets up a triple list of length one or two and copies the duration from the latency-family table. Multi-triple encodings exist exclusively for ops that issue on one slot while transporting on another — TMA and WGMMA. A `tiled_tma_load` cannot be reduced to a single-row footprint because the descriptor must stay parked on the `tma` row even when the tensor payload is in flight on the SMEM transport; if either row is occupied, the candidate is rejected.

### Apply-Side Lowering

The per-op triples lower to qword rows by accumulating bits across slots before the apply driver writes them into the cycle stack.

```c
NodeRRT lower_resource_vector(const ResourceVector *vec) {
    NodeRRT rrt = { .duration = 0 };
    for (uint32_t i = 0; i < vec->n_triples; ++i) {
        uint32_t bit = vec->triples[i].slot - 1;
        if (vec->triples[i].duration > rrt.duration) {
            rrt.duration = vec->triples[i].duration;
        }
        for (uint32_t k = 0; k < vec->triples[i].duration; ++k) {
            rrt.rows[k] |= (1ull << bit);
        }
    }
    return rrt;
}
```

The duration is the maximum over all triples — two triples with different durations co-occupy the same set of cycles for as long as the longer one runs. Slots that drop out earlier leave their bits clear on the trailing cycles; the OR-fold makes that automatic.

The triple list is also the unit of diagnostics: when the placement driver reports an admission failure, it prints the triple that caused the conflict together with the global RRT row at the failing modulo cycle. Two triples merged into one qword would hide which slot rejected the candidate.

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

### Cost-Term Formula

The surcharge attached to an `(op, cycle)` pair is a linear function of predicted register-pressure excess, clamped to a fixed cap so a single op cannot saturate the cost vector.

```c
// pressure: per-op register-pressure estimate at the candidate seat cycle.
// budget:   register-file budget for the current SM partition.
// W:        SPILL_SURCHARGE_WEIGHT (= 17, sourced from the cost-table seeder).
// CAP:      SPILL_SURCHARGE_CAP    (= 4096, the cost-vector saturation cap).
uint32_t spill_surcharge(uint32_t pressure, uint32_t budget) {
    if (pressure <= budget) return 0;
    uint64_t raw = (uint64_t)(pressure - budget) * W;
    return raw > CAP ? CAP : (uint32_t)raw;
}
```

A schedule's accumulated spill surcharge is the sum of these per-`(op, cycle)` terms across every op that received a surcharge. The cost-based arm reads the sum as the third component of its lexicographic cost vector, immediately after the hard resource gate and the pipeline-slot pressure. Two schedules with identical resource and slot-pressure components tie-break on this sum; the schedule with the smaller surcharge wins.

The cap matters for the proof obligation: without it, a sufficiently large pressure overshoot could push the surcharge above the budget the structural-distance term reserves at the bottom of the lexicographic vector, and the ranking would no longer respect the intended priority. The 4 096 cap leaves three orders of magnitude of headroom for the structural-distance term to express its preferences inside.

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
