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

`TileASGenerateSchedule` starts from a scheduled candidate block, picks out the operations that participate in the pipeline, and refines constraints until the schedule is feasible or the configured iteration limit is reached. The pass option `max-constraint-iterations` bounds the outer refinement loop so pathological inputs cannot drive compile time without bound; it defaults to 16.

Scheduling policy enters the algorithm through the constraint builder. The builder reads register pressure, resource-footprint density, pipeline depth, and structural grouping, then emits constraints that restrict the search space. Recurring constraint families include `SameDepthConstraint` for operations that must remain at the same pipeline depth, `MaxDepthConstraint` for operations that must not drift beyond a depth limit, `ForceSerialExecutionConstraint` for regions that must execute single-lane, and structural grouping constraints that tie operations into one scheduling unit. Each constraint family carries the rationale that justifies the restriction so a later refinement round can lift it when the schedule becomes infeasible.

The modulo scheduler then tries candidate placements. The dependence graph enforces legal order; the RRT enforces resource feasibility. The two checks stay separate: dependences answer when an operation may run relative to other operations, the RRT answers whether the machine has capacity at a candidate cycle. The probe-and-commit mechanics are covered in [Modulo Scheduler and Rau — Resource Reservation Table](modulo-scheduler-and-rau.md#resource-reservation-table); this page does not duplicate them.

Each refinement round records a 2-bit status: bit 0 marks `converged`, bit 1 marks `budget_exceeded`. When the iteration budget runs out without convergence, the scheduler raises the budget-truncated flag on `Schedule.flags` so the materializer can distinguish a clean schedule from a truncated one and emit the matching diagnostic.

## Pass 2: MaterializeSchedule

`MaterializeSchedule` consumes the fixed schedule and turns it into IR. It rebuilds two maps from the analysis — async handle to producer operation, and operation to scheduled depth — then walks the scheduled region and seeds preliminary `Pipe_` and `Mutex_` skeletons. Once the skeletons are in place, the materializer iterates over the producer/consumer candidate pairs the walker discovered and runs `Schedule::solve` on each. After the per-pair solves complete, the materializer collapses duplicate pipe skeletons, splices the surviving SSA values back into the region in stage/order, and verifies the rewritten region against the postcondition that producer and consumer placement still agrees with the analysis.

Pass separation is the keystone invariant. Before materialization, scheduling edges live behind opaque async handles because no final `Pipe_` SSA value exists yet. After materialization, those handles resolve to concrete `Pipe_` and `Mutex_` values whose identity is stable for the rest of the pipeline. A reimplementation that fuses the two passes ties the resource search to handle identities the materializer is free to rewrite, which breaks the cache invalidation contract documented below.

## Analysis Handoff

A single cached analysis couples the two passes. `ScheduleGenerator` allocates and populates `ScheduleAnalysis`; `MaterializeSchedule` retrieves the same slot through the AnalysisManager. Neither pass touches the other's internals — every cross-pass datum flows through the cached analysis.

The analysis is keyed by the RTTI string `"mlir::nv_tile_ir::as::schedule_utils::ScheduleAnalysis]"` and registered through the Meyers-cached TypeID idiom documented in [TypeID Sentinels and Anchors](../mlir-infra/typeid-sentinels-and-anchors.md). The AnalysisManager keys on the resulting `TypeID*` so the second pass picks up the exact slot the first pass wrote.

## Schedule-Data Map

Several data structures cross the analysis-vs-materialization boundary. Each one has a single canonical home page that documents its layout and probing rules; this overview only names them and points outward.

| Structure | Owner page | Role across the seam |
|---|---|---|
| `ScheduleAnalysis` | this page | Cached handoff record; AnalysisManager key |
| `Schedule` view | [Modulo Scheduler and Rau](modulo-scheduler-and-rau.md) | Internal view materializer reconstructs from the analysis |
| `RRT` and `NodeRRT` | [Modulo Scheduler and Rau — Resource Reservation Table](modulo-scheduler-and-rau.md#resource-reservation-table) | Resource feasibility check; consumed only by Pass 1 |
| Per-op footprint rows | [Resource Constraint Builder](resource-constraint-builder-and-rrt.md) | Built in Pass 1, frozen by the time materialization starts |
| `ConstraintMap` + DSU | [Schedule Constraint Attributes — ConstraintMap Layout](schedule-constraint-attributes.md#constraintmap-layout) | Parsed attribute state consulted by placement and by `Schedule::solve` |
| Pending-set SwissTable | [Serial vs Cost-Based Generators — G1: Pending-Set Membership](serial-vs-cost-based-generators.md#g1-pending-set-membership) | Gate-G1 retry filter, seeded by the attribute parser |
| `origMap` + second-table | [Schedule::solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) | Producer/consumer resolution during materialization |
| Buffer-assignment record | [Buffer Assignment and Named Barriers](buffer-assignment-and-mbarriers.md) | Bridge between schedule and physical SMEM/TMEM allocation |
| `Pipe_` / `Mutex_` headers | [Pipe and Mutex Value Layout](pipe-mutex-value-layout.md) | Final SSA shape emitted by materialization |

## Schedule::solve

`Schedule::solve` is not a solver in the integer-programming sense and not a second modulo scheduler. It is the inner producer/consumer grouping pass that runs during materialization. For each candidate pair the walker discovers, it sorts the relevant ops by `(stage, order)`, classifies producers and consumers, closes the producer set over the live-at-consumer relation, unions producers that share the same raw value through a disjoint-set forest, sweeps each root to pick the earliest scheduled owner, and emits a `Pipe_` value per group. It never changes a chosen stage, never picks a new `II`, and never asks whether a placement fits the RRT.

The deep treatment lives in [Schedule::solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md). The point for this overview is the invariant: any reimplementation that ends up doing resource search inside `Schedule::solve` has blurred the pass boundary and broken the handoff contract.

## Stage/Order Invariant

Stage and order together form a total order inside every scheduled block. Two operations may share a stage, but their `order` value tie-breaks deterministically. The materializer leans on that determinism when sorting producers and consumers before emitting pipe groups — reordering even a single pair changes the producer/consumer identity each `Pipe_` value sees and breaks the cache contract every later pass relies on.

## Usage and Contract

The TileAS pipeline invokes the two passes in fixed order. `TileASGenerateSchedule` consumes the `nv_tileas` block, its operand axis-analysis facts, the buffer-lifetime records published by the layout passes, and the nine `tileas.schedule.constraint.*` and `tileas.*` attributes parsed by [Schedule Constraint Attributes](schedule-constraint-attributes.md). It writes a populated `ScheduleAnalysis` into the AnalysisManager slot keyed by its RTTI TypeID, sets validity bits on the analysis, and stores the chosen `II` and stage count on the per-block records.

`MaterializeSchedule` consumes only that cached analysis; it never inspects upstream constraint state directly. Its output is the rewritten `nv_tileas` block with `Pipe_` and `Mutex_` SSA values inserted between producer and consumer regions and `cute_nvgpu.arch.agent_switch` partitioning emitted along the warp-specialized boundaries. Downstream passes must not invalidate the analysis between the two passes — the PassManager preservation contract is what lets the second pass pick up exactly the slot the first pass wrote.

## Cross-links

- [Modulo Scheduler and Rau](modulo-scheduler-and-rau.md) covers the initiation-interval search and RRT mechanics.
- [Resource Constraint Builder and RRT](resource-constraint-builder-and-rrt.md) covers resource-pressure constraints and table construction.
- [Schedule::solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) covers pipe materialization and pass separation.
- [Modulo Driver Or-Chain](modulo-driver-or-chain.md) covers the placement arm selector that the generator runs at each `II`.
- [Pipe/Mutex Value Layout](pipe-mutex-value-layout.md) covers the SSA shape of the coordination values that the materializer emits.
- [Serial vs Cost-Based Generators](serial-vs-cost-based-generators.md) contrasts the fallback and high-optimization scheduling paths.
- [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md) explains the target-specific slot model used by Blackwell pipeline scheduling.
