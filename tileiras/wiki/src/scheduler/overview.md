# Scheduler Overview

The TileAS scheduler turns an operational `nv_tileas` block into a staged pipeline. Its visible output is a stable `(stage, order)` assignment for the operations in the scheduled block, followed by explicit async coordination values such as `Pipe_` and `Mutex_`. Downstream lowering reads that assignment to decide which operations belong to the same software-pipeline stage, which values cross stage boundaries, and where barrier-like coordination must appear.

Two responsibilities split into two passes. `TileASGenerateSchedule` chooses the schedule: it builds dependence and resource constraints, searches for a feasible initiation interval, and records per-operation stage/order information in `ScheduleAnalysis`. `MaterializeSchedule` then consumes that analysis and rewrites the IR: it builds the concrete async coordination graph, emits `Pipe_` and `Mutex_` values, and verifies that the scheduled region still satisfies the chosen ordering.

The split is part of the contract. The first pass works on operations, dependence edges, opaque async handles, and resource footprints; the second consumes the already-chosen schedule and materializes SSA values. A faithful reimplementation must not merge these phases — fusing them makes the resource search depend on temporary pipe identities that the materializer is free to rewrite.

## Mental Model

The scheduler answers two questions. Placement: for each operation, at what logical stage and order should it run so that dependencies and hardware resource budgets are respected? Communication: after placement, which producer and consumer operations need an explicit async value between them?

The placement pass is a modulo scheduler. It issues loop iterations at a fixed initiation interval, written as `II`. A Resource Reservation Table (`RRT`) tracks hardware usage: each row corresponds to one cycle modulo `II`, and each bit in a row represents a resource class. An operation carries its own footprint table. Placing an operation at a cycle is legal when every footprint row is disjoint from the corresponding global row.

The materialization pass is not another modulo scheduler. Its `Schedule::solve` step is a greedy disjoint-set pass over producer and consumer groups. It reads the fixed `(stage, order)` relation, groups operations that must communicate through the same async value, and emits the concrete `Pipe_` values. It never searches for a new `II` and never runs the RRT feasibility test.

## Pipeline Shape

```text
nv_tileas block
    |
    | TileASGenerateSchedule
    | - build dependence graph
    | - build resource constraints
    | - search initiation interval
    | - assign stage/order
    v
ScheduleAnalysis
    |
    | MaterializeSchedule
    | - recover scheduled depths
    | - seed Pipe_/Mutex_ skeletons
    | - solve producer/consumer groups
    | - rebuild and verify scheduled IR
    v
scheduled nv_tileas block
```

The handoff object is `ScheduleAnalysis`. Conceptually, it contains the scheduled blocks, validity state, per-operation depth information, resource footprints, and the opaque handles that let the materializer connect async producers and consumers before final `Pipe_` SSA values exist.

## Pass 1: GenerateSchedule

`TileASGenerateSchedule` starts from a scheduled candidate block, picks out the operations that participate in the pipeline, and refines constraints until the schedule is feasible or the configured iteration limit is reached. The option that matters is `max-constraint-iterations`, which bounds the outer refinement loop and prevents pathological compile-time blowups.

```c
ScheduleAnalysis generate_schedule(TileASBlock block, ScheduleOptions opts) {
    ScheduleAnalysis analysis = seed_schedule_analysis(block);

    for (int round = 0; round < opts.max_constraint_iterations; ++round) {
        ConstraintSet constraints = build_resource_constraints(block, analysis, opts);
        ApplyResult applied = check_and_apply_constraints(block, constraints, &analysis);

        if (applied.converged) {
            break;
        }

        analysis = refine_with_modulo_scheduler(block, constraints, analysis, opts);

        if (analysis.valid && analysis.within_resource_budget) {
            break;
        }
    }

    require(analysis.valid);
    return analysis;
}
```

Scheduling policy enters the algorithm through the constraint builder. It reads register pressure, resource footprint density, pipeline depth, and structural grouping, then emits constraints that restrict the search space. Common constraints include `SameDepthConstraint` for operations that must remain at the same pipeline depth, `MaxDepthConstraint` for operations that must not drift beyond a depth limit, `ForceSerialExecutionConstraint` for blocks that must be kept single-lane, and structural grouping constraints for operations that should be considered as one scheduling unit.

The modulo scheduler then tries candidate placements. The dependence graph enforces legal order; the RRT enforces resource feasibility. A good implementation keeps the two checks separate: dependencies say when an operation may run relative to other operations, while the RRT says whether the machine has capacity at a candidate cycle.

## RRT Probe and Commit

The RRT is deliberately simple. A placement is legal when every occupied resource bit in the operation footprint is zero in the global table at the corresponding modulo cycle. Commit ORs the footprint into the global table.

```c
typedef struct {
    uint64_t *rows;
    int initiation_interval;
    int resource_classes;
} RRT;

typedef struct {
    const uint64_t *rows;
    int duration;
} NodeRRT;

bool rrt_probe(const RRT *rrt, const NodeRRT *node, int start_cycle) {
    for (int i = 0; i < node->duration; ++i) {
        int row = (start_cycle + i) % rrt->initiation_interval;

        if ((rrt->rows[row] & node->rows[i]) != 0) {
            return false;
        }
    }

    return true;
}

void rrt_commit(RRT *rrt, const NodeRRT *node, int start_cycle) {
    for (int i = 0; i < node->duration; ++i) {
        int row = (start_cycle + i) % rrt->initiation_interval;
        rrt->rows[row] |= node->rows[i];
    }
}
```

The bitset model is what makes retry cheap. Backtracking is not a general symbolic solve — it amounts to trying candidate cycles, probing resource rows, committing successful footprints, and refining constraints when the current `II` cannot satisfy both dependence and resource requirements.

## Pass 2: MaterializeSchedule

`MaterializeSchedule` consumes the fixed schedule and turns it into IR. It first rebuilds two maps — original async handles to producer operations, and operations to scheduled depths — then walks the scheduled region and seeds preliminary `Pipe_` and `Mutex_` skeletons. `Schedule::solve` then runs once for each producer/consumer candidate pair that needs a concrete coordination value.

```c
void materialize_schedule(TileASBlock block, const ScheduleAnalysis *analysis) {
    Schedule schedule = create_schedule(block, analysis);

    schedule.orig_map = build_original_async_handle_map(block, analysis);
    schedule.depth_map = build_scheduled_depth_map(block, analysis);

    seed_mutex_and_pipe_skeletons(&schedule);

    for (CandidatePair pair : schedule.consumer_worklist) {
        solve_pipe_group(&schedule, pair.outer_region, pair.consumer);
    }

    collapse_skeleton_pipes(&schedule);
    rebuild_scheduled_region(&schedule);
    verify_scheduled_region(&schedule);
}
```

Pass separation matters here. Before materialization, the schedule refers to raw async handles because no final `Pipe_` SSA value exists yet. After it, the raw handles have served their purpose and the IR carries concrete coordination values.

## Analysis Handoff Plumbing

A single cached analysis couples the two passes. `ScheduleGenerator` allocates and populates the analysis; `MaterializeSchedule` retrieves it from the AnalysisManager and walks it. Neither pass touches the other's internals — everything flows through the cached `ScheduleAnalysis` slot.

`ScheduleAnalysis` is keyed by the RTTI string `"mlir::nv_tile_ir::as::schedule_utils::ScheduleAnalysis]"`. That string is interned by `sub_44A6CA0` on first use and the resulting TypeID handle is cached at the global `qword_5B38E78` (the same Meyers-cached TypeID idiom documented in [TypeID Sentinels and Anchors](../mlir-infra/typeid-sentinels-and-anchors.md)). The PassManager's analysis-DenseMap is probed with the hash `(ptr >> 9) ^ (ptr >> 4) & (cap - 1)` applied to the cached TypeID pointer, so the second pass picks up exactly the same slot the first pass wrote.

Two pass entry points anchor the handoff. `sub_982510` is `ScheduleGenerator::run` (493 source lines in the recovered control flow): it drives the modulo scheduler at increasing `II` until `Schedule::solve` succeeds, then stores the result as the cached analysis. `sub_824000` is `MaterializeSchedule::runOnOperation` (4 175 bytes of code): it loads the cached analysis, calls `sub_8FDE40` → `sub_8F1AA0` to drive the materialization walk, and emits the matching `Pipe_`/`Mutex_` IR plus `cute_nvgpu.arch.agent_switch` partitioning.

The materializer's internal call chain is fixed. `sub_8FDE40` is the top-level entry that fetches the cached analysis and unpacks the `Schedule` view; `sub_8F1AA0` is the driver dispatch that picks the walker. `sub_8E2790` probes `origMap` at `Schedule+80..96` to recover the producer side of each async handle, and `sub_8E2F00` probes the second-table with an `fmix64` hash over `Schedule+104..120` to recover the consumer side. `sub_8EE9D0` and `sub_8EE700` are preludes that materialize the per-region context the walker needs. `sub_8EAD70` is the actual `Mutex_`/`Pipe_`-B walker that emits SSA values, and `sub_8F19D0` is the per-pair solve that runs once for every producer/consumer pair the walker finds.

| Helper | Role |
| --- | --- |
| `sub_982510` | `ScheduleGenerator::run`; drives modulo scheduling and writes `ScheduleAnalysis`. |
| `sub_824000` | `MaterializeSchedule::runOnOperation`; consumes the cached analysis and emits coordination IR. |
| `sub_44A6CA0` | Interns the `ScheduleAnalysis` RTTI string and returns its TypeID handle. |
| `sub_8FDE40` | Top-level materialization entry; fetches the cached analysis from the AnalysisManager. |
| `sub_8F1AA0` | Driver dispatch; selects the materialization walker for the current schedule. |
| `sub_8E2790` | `origMap` probe at `Schedule+80..96`; recovers the producer side of each async handle. |
| `sub_8E2F00` | Second-table `fmix64` probe at `Schedule+104..120`; recovers the consumer side. |
| `sub_8EE9D0` / `sub_8EE700` | Preludes that materialize per-region context for the walker. |
| `sub_8EAD70` | `Mutex_`/`Pipe_`-B walker; emits coordination SSA values in stage/order. |
| `sub_8F19D0` | Per-pair solve; runs once for every producer/consumer pair the walker identifies. |
| `sub_97B770` | Seeds the `group_dsu` disjoint-set at `Schedule+112`. |

`max-constraint-iterations` bounds the outer refinement loop in `ScheduleGenerator::run`, defaulting to 16. The bound sits at the caller's `a2 + 16` and feeds the iteration cap. Each refinement round returns a 2-bit status: bit 0 records `converged`, bit 1 records `budget_exceeded`. When the budget runs out without convergence, the scheduler sets `Schedule.flags & 4` so the materializer can distinguish a clean schedule from a budget-truncated one and emit the matching diagnostics.

The `Schedule` fields involved in the handoff sit at predictable offsets:

```c
typedef struct Schedule {
    /* ... fields documented in modulo-scheduler-and-rau.md ... */
    /*+0x50*/ DenseMap<Op*, OrigInfo>   origMap;     // probed by sub_8E2790
    /*+0x68*/ DenseMap<Op*, SecondInfo> secondMap;   // probed by sub_8E2F00
    /*+0x70*/ DSU                       group_dsu;   // seeded by sub_97B770
    /*+0x80*/ /* ... see also pipe-mutex-value-layout.md ... */
} Schedule;
```

Keep this plumbing visible in the type system. `ScheduleAnalysis` is the public handoff object; `Schedule` is the internal view the materializer reconstructs from it. Mixing the two, or letting the materializer reach into `ScheduleGenerator` state directly, breaks the cache invalidation contract that the PassManager relies on.

## Schedule::solve

`Schedule::solve` is the inner producer/consumer grouping algorithm. The name misleads: it is not an integer-programming solver and not a second modulo scheduler. It is a deterministic greedy pass driven by the existing `(stage, order)` comparator.

```c
void solve_pipe_group(Schedule *schedule, Operation *outer, Operation *candidate_consumer) {
    Worklist work = collect_relevant_operations(schedule, outer, candidate_consumer);
    sort_by_stage_then_order(&work);

    ProducerSet producers = classify_producers(work);
    ConsumerSet consumers = classify_consumers(work, candidate_consumer);

    close_producer_set_over_operands(&producers, work);

    DisjointSet dsu = create_disjoint_set(work);

    for (Operation *op : work) {
        for (Value operand : op->operands) {
            Operation *def = defining_operation_inside(operand, outer);

            if (def != NULL && must_share_pipe(def, op, schedule)) {
                dsu_union(&dsu, def, op);
            }
        }
    }

    for (DsuRoot root : dsu_roots_in_stage_order(&dsu)) {
        ProducerSet root_producers = producers_in_root(root, producers);
        ConsumerSet root_consumers = consumers_in_root(root, consumers);

        if (!empty(root_producers) || !empty(root_consumers)) {
            emit_pipe(schedule, root_producers, root_consumers);
        }
    }
}
```

Monotonicity is the key property. The algorithm classifies, closes, unions, sweeps, and emits — nothing more. It never changes the chosen stage, never picks a new `II`, and never asks whether a placement fits the RRT. A reimplementation that ends up doing resource search inside `Schedule::solve` has blurred the pass boundary.

## Data Contracts

The data carried between passes is small but precisely shaped. `ScheduleAnalysis` is the handoff record the AnalysisManager caches between `TileASGenerateSchedule` and `MaterializeSchedule`. Inside it, `ScheduleSlot` carries one operation's stage/order placement plus the scheduling metadata that placement, sorting, and materialization read. The `RRT` and `NodeRRT` pair model resource feasibility — one global bitset per cycle modulo `II`, one per-op footprint over its occupied cycles. Constraint sets carry depth, serial, grouping, and resource-pressure constraints consumed by the refinement loop. `Pipe_` and `Mutex_` are the concrete coordination values that survive into the scheduled IR after materialization.

| Object | Conceptual contents | Used by |
| --- | --- | --- |
| `ScheduleAnalysis` | Scheduled blocks, validity flag, op-to-depth data, resource footprints, opaque async handles | Handoff from `TileASGenerateSchedule` to `MaterializeSchedule` |
| `ScheduleSlot` | One operation's stage/order placement plus scheduling metadata | Placement, sorting, materialization |
| `RRT` | One bitset row per cycle modulo `II`; each bit is a resource class | Modulo-scheduler feasibility checks |
| `NodeRRT` | Per-operation resource footprint over its occupied cycles | RRT probe and commit |
| Constraint set | Depth, serial, grouping, and resource-pressure constraints | Schedule refinement |
| `Pipe_` | Concrete producer/consumer coordination value | Scheduled IR after materialization |
| `Mutex_` | Concrete mutual-exclusion coordination value | Scheduled IR after materialization |

Stage/order order is total inside a scheduled block — that is the scheduler's keystone invariant. Two operations may share a stage, but their `order` value makes tie-breaking deterministic. The materializer relies on that determinism when sorting producers and consumers before emitting pipe groups.

## Usage and Contract

The TileAS pipeline invokes the two passes in fixed order. `TileASGenerateSchedule` consumes the `nv_tileas` block, its operand axis-analysis facts, the buffer-lifetime records published by the layout passes, and the nine `tileas.schedule.constraint.*` and `tileas.*` attributes parsed by [Schedule Constraint Attributes](schedule-constraint-attributes.md). It writes a populated `ScheduleAnalysis` into the AnalysisManager slot keyed by its RTTI TypeID, sets validity bits on the analysis, and stores the chosen `II` and stage count on the per-block records.

`MaterializeSchedule` consumes only that cached analysis; it never inspects upstream constraint state directly. Its output is the rewritten `nv_tileas` block with `Pipe_` and `Mutex_` SSA values inserted between producer and consumer regions and `cute_nvgpu.arch.agent_switch` partitioning emitted along the warp-specialised boundaries. Downstream passes must not invalidate the analysis between the two passes — the PassManager preservation contract is what lets the second pass pick up exactly the slot the first pass wrote.

## Reimplementation Checklist

Start with correctness, not cost modeling. Build the dependence graph and the `(stage, order)` representation first. Then implement the RRT feasibility check exactly — most scheduling bugs show up as false accepts or false rejects in the bitset probe. After that, add the resource constraint builder and the bounded refinement loop. Only once pass 1 produces stable schedules should `MaterializeSchedule` and `Schedule::solve` arrive.

Required pieces:

- A deterministic dependence graph over operations inside the scheduled block.
- A stage/order assignment with total ordering inside each block.
- Resource footprints for every operation that participates in modulo scheduling.
- An RRT implementation with separate probe and commit operations.
- A constraint builder for depth, serial, structural, and resource-pressure constraints.
- A bounded pass-1 refinement loop controlled by `max-constraint-iterations`.
- A preserved analysis object that carries schedule state across the pass boundary.
- A materializer that emits `Pipe_` and `Mutex_` only after the schedule is fixed.
- A greedy `Schedule::solve` implementation based on classification, closure, DSU union, sweep, and pipe emission.
- Verification that no scheduled region contains a consumer without a valid producer/consumer coordination path.

## Cross-links

- [Modulo Scheduler and Rau](modulo-scheduler-and-rau.md) covers the initiation-interval search and RRT mechanics.
- [Resource Constraint Builder and RRT](resource-constraint-builder-and-rrt.md) covers resource-pressure constraints and table construction.
- [Schedule::solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) covers pipe materialization and pass separation.
- [Modulo Driver Or-Chain](modulo-driver-or-chain.md) covers the placement arm selector that the generator runs at each `II`.
- [Pipe/Mutex Value Layout](pipe-mutex-value-layout.md) covers the SSA shape of the coordination values that the materializer emits.
- [Serial vs Cost-Based Generators](serial-vs-cost-based-generators.md) contrasts the fallback and high-optimization scheduling paths.
- [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md) explains the target-specific slot model used by Blackwell pipeline scheduling.
