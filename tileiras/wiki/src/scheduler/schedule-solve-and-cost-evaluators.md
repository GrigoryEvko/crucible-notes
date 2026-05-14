# Schedule Solve and Cost Evaluators

## Abstract

`Schedule::solve` is not the modulo scheduler and not an optimization solver. It is the materialization step that consumes an already-computed schedule analysis and emits the SSA pipe values the TileAS program needs. Resource search, cost ranking, and initiation-interval work all happen earlier during `TileASGenerateSchedule`.

The cost evaluators on this page belong to that earlier generation pass. They use paired RRT probes and structural distance constraints to rank candidate placements. `Schedule::solve` runs none of that machinery — it performs classification, closure, disjoint-set merging, and pipe emission.

## Pass Boundary

| Phase | Pass | Main job |
|---|---|---|
| schedule generation | `TileASGenerateSchedule` | compute stage/order, resource placement, and schedule analysis |
| schedule materialization | `MaterializeSchedule` | emit `Pipe_` and `Mutex_` values from the preserved analysis |

The split matters for reimplementation. Treating `Schedule::solve` as the place where II search happens tangles the compiler architecture and ties the materialization pass to mutable generation state. In Tileiras, generation publishes analysis; materialization reads it.

## `Schedule::solve`

`Schedule::solve` fires once for each relevant outer operation and candidate consumer. Its output is a set of pipe values that connect producer groups to consumer positions. The body is a greedy disjoint-set propagation pass over six phases — never choosing a new `II`, never probing the RRT. It sorts candidate operations by `(stage, order)`, builds maps from operation to schedule info, filter state, and owning outer operation, closes the producer set under the "live at this consumer" relation, links producers that share the same original value through a disjoint-set forest, sweeps each disjoint-set root to pick the canonical owner by schedule order, and emits `Pipe_` values for producer-to-consumer groups — plus a consumer-only fallback when the live set is empty.

```c
void solve_schedule(Schedule *schedule,
                    RawValue raw_value,
                    Operation *consumer) {
    Worklist work = collect_candidate_producers(schedule, raw_value, consumer);
    stable_sort(work, compare_stage_then_order);

    Map<Operation *, ScheduleInfo> info = build_schedule_info(work);
    Map<Operation *, Operation *> owner = build_owner_map(work);
    DisjointSet dsu = {};

    ProducerSet live = close_under_live_at_consumer(work, consumer, info);

    for (Operation *producer : live) {
        for (Value operand : producer->operands()) {
            Operation *def = operand.defining_op();
            if (origin(schedule, operand) == raw_value) {
                dsu.union_nodes(producer, def);
            }
        }
    }

    for (DsuRoot root : dsu.roots()) {
        SmallVector<Operation *> members = dsu.members(root);
        Operation *canonical = choose_earliest_owner(members, info);
        emit_pipe_for_group(schedule, canonical, members, consumer);
    }

    if (live.empty()) {
        emit_consumer_only_pipe(schedule, consumer);
    }
}
```

The comparator is lexicographic — lower stage first, then lower order. This solver never consults a scalar cost function, a resource row, or an initiation interval.

## Pipe and Mutex Emission

Before `Schedule::solve` emits final pipes, materialization builds auxiliary maps from the preserved schedule analysis. One path emits mutex values for exclusion relationships, another emits preliminary pipe placeholders, and the solver reconciles those placeholders into final pipe SSA values from the disjoint-set groups. The naming convention is deliberately visible in the IR: `Pipe_` values model dataflow between scheduled producer and consumer regions, while `Mutex_` values model exclusion or ordering constraints that ordinary value dependencies cannot express.

```c
void emit_pipe_and_mutex(Schedule *S, const ScheduleAnalysis *analysis) {
    build_orig_map(S, analysis);                   // sub_8E2790 probe target
    build_second_map(S, analysis);                 // sub_8E2F00 probe target

    seed_mutex_placeholders(S);                    // exclusion edges first
    seed_pipe_placeholders(S);                     // dataflow edges next

    for (CandidatePair p : S->consumer_worklist) {
        solve_schedule(S, p.raw_value, p.consumer);
    }

    collapse_skeleton_pipes(S);                    // dedup producer groups
    rebuild_scheduled_region(S);                   // splice Pipe_/Mutex_ ops in
    verify_scheduled_region(S);                    // hard postcondition
}
```

Mutex placeholders go first because their exclusion semantics are stricter than the pipe placeholders. A missing mutex is a correctness bug; a missing pipe edge is a missed-optimization bug. The materializer commits the harder constraint before relaxing into the softer one.

## Cost Evaluators

Cost evaluators run during generation. They answer one question: can this candidate fit at this interval or cycle, and how expensive is that choice? Tileiras uses two paired evaluators.

| Evaluator | Pair being modeled | Role |
|---|---|---|
| bank-pressure evaluator | current-iteration and next-iteration resource shadows | checks bank and carry-over resource conflicts |
| pipe-slot evaluator | resource RRT and structural distance matrix | checks resource slots and dependence distance together |

A generic feasible-search driver calls both evaluators. They return a boolean success flag and, on success, a candidate placement state the caller can commit.

```c
bool evaluate_candidate(SearchOutput *out,
                        Candidate candidate,
                        uint32_t ii,
                        ResourceModel *resources,
                        DistanceMatrix *distances) {
    if (!resource_rows_are_free(resources, candidate, ii)) {
        return false;
    }

    if (!distance_window_allows(distances, candidate, ii)) {
        return false;
    }

    out->placement = candidate.placement;
    out->cost = compute_lexicographic_cost(candidate, resources, distances);
    return true;
}
```

The cost is lexicographic. Resource feasibility is the hard gate; pipeline-slot utilization and structural distance rank only the candidates that clear it.

## Structural Distance Matrix

The pipe-slot evaluator builds an all-pairs distance matrix for the candidate interval. Each edge encodes how far apart two operations must sit after dependence latency, iteration distance, and skew are accounted for. A transitive closure then lets the evaluator query a legal placement window in constant time.

```c
void build_distance_closure(DistanceMatrix *matrix, Graph graph, uint32_t ii) {
    matrix->fill(INFINITE_DISTANCE);

    for (Edge edge : graph.edges()) {
        int32_t distance = edge.latency - (int32_t)(ii * edge.iteration_distance);
        matrix->set(edge.src, edge.dst, distance);
    }

    for (Node k : graph.nodes()) {
        for (Node i : graph.nodes()) {
            for (Node j : graph.nodes()) {
                int32_t through = matrix->get(i, k) + matrix->get(k, j);
                if (through > matrix->get(i, j)) {
                    matrix->set(i, j, through);
                }
            }
        }
    }
}
```

The closure is generation-only state. It is not carried into `Schedule::solve`.

## Search Driver

The evaluators share the same galloping-plus-binary search shape the resource builder uses. The outer search chooses `II`; the inner search may pick a cycle threshold or candidate row. That nesting lets generation find a feasible schedule without linearly scanning every interval and cycle.

```c
bool search_with_probe(SearchOutput *out,
                       ProbeFn probe,
                       uint32_t lower,
                       uint32_t upper) {
    uint32_t hi = lower;

    while (hi < upper && !probe(hi, out)) {
        hi = min(upper, hi * 2);
    }

    if (!probe(hi, out)) {
        return false;
    }

    uint32_t lo = lower;
    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2;
        SearchOutput candidate = {};

        if (probe(mid, &candidate)) {
            hi = mid;
            *out = candidate;
        } else {
            lo = mid + 1;
        }
    }

    return true;
}
```

## Shared Helpers

Several helpers run alongside `Schedule::solve` on the scheduling-adjacent lowering path. Persistent-loop construction emits the canonical widened-index `scf.for` used by persistent kernels; shape verification checks that problem shape, tile shape, and cluster-group agree; result-type verification keeps work-tile info results in one consistent representation; generic work-tile info construction adapts async values and CUTLASS work-tile descriptors. None of them participate in the solve itself, but each enforces a contract that scheduling and materialization assume — failing any of these checks marks the schedule invalid before the solver runs.

## `Schedule::solve` Body (`sub_8EEE70`)

The previous section described `Schedule::solve` as five abstract steps. The actual implementation in `sub_8EEE70`
is a 2 269-line, six-phase state machine that materialises five distinct hashtables and a Union-Find forest on the
stack, runs a generator dispatcher, falls back to a cost-based generator on failure, and emits one trivial-schedule
fallback when every generator path fails. The trampoline `sub_8F19D0` invokes the body, and that trampoline reaches
the body from `Schedule::buildAndSolve` (`sub_8F1AA0`) inside the materialization pass. The constraint graph and
schedule analysis arrive from `ResourceConstraintBuilder` upstream; `sub_8EEE70` consumes that analysis and produces
the final op-to-stage and op-to-order mapping.

### Stack-Local State

Five hashtables and one Union-Find bucket array live on the solver stack frame. Bucket strides vary with the payload
size — `schedInfo` carries a 32-byte payload of stage/order/RRT-row metadata, while the three pointer-typed maps
store a single 8-byte payload after the key.

| Table        | Bucket stride | Key      | Value stored                                             | Producer                          |
|--------------|--------------:|----------|----------------------------------------------------------|-----------------------------------|
| `schedInfo`  | 40 B          | `Op*`    | current stage, order, RRT row, classification flag bytes | filled by phase 2 topo walk       |
| `filterMap`  | 16 B          | `Op*`    | "dead in retry" sentinel byte at `+8`                    | written by retry-arm failures     |
| `parentMap`  | 16 B          | `Op*`    | DSU group leader pointer                                 | seeded from constraint-graph slot |
| `opToOwner`  | 16 B          | `Op*`    | `Pipe_` / `Mutex_` owner reference                       | written by phase 3 generator      |
| UF buckets   | 72 B          | u64      | `{list_ptr, tail_cap32, tail_size32, Op* inline[3]}`     | seeded from `Schedule+0x70`       |

All four `Op*`-keyed maps use the standard `llvm::DenseMap` open-addressing layout with `hash = (op>>9) ^ (op>>4)`,
empty sentinel `0xFFFFFFFFFFFFE000`, tombstone `0xFFFFFFFFFFFFF000`, and the `4*(size+1) >= 3*capacity` grow rule.
The Union-Find bucket layout is custom: the inline three-slot tail amortises the common case where a DSU group has
at most three members, and overflow spills to the heap via `list_ptr`. The shape matches the empirical group sizes
`ResourceConstraintBuilder` produces, which rarely group more than three ops together.

### Six-Phase State Machine

```c
void schedule_solve_body(Schedule *schedule,
                         uint64_t  raw_value,
                         Operation *consumer) {
    // Phase 1: init. Construct five tables and the ready-queue staging vector.
    DenseMap sched_info = dense_map_create(/*bucket_stride=*/40);
    DenseMap filter_map = dense_map_create(/*bucket_stride=*/16);
    DenseMap parent_map = dense_map_create(/*bucket_stride=*/16);
    DenseMap op_to_owner = dense_map_create(/*bucket_stride=*/16);
    UfBuckets uf = uf_buckets_create(/*bucket_stride=*/72);
    SmallVector<Operation *, 0> ready_queue = {};

    uf_seed_from_constraint_dsu(&uf, /*src=*/schedule->dsu_at_0x70);

    // Phase 2: topo prep. Stamp parentMap[op] = leader for every op.
    for (Operation *op : schedule_ops_topological(schedule)) {
        ConstraintSlot *slot = sub_94A550(schedule, op);
        parent_map_set(&parent_map, op, slot->leader);
    }

    // Phase 3: generator dispatch. Cheap placement driver runs first.
    if (sub_981D50(schedule, &sched_info, &parent_map, &op_to_owner)) {
        goto finalize;
    }

    // Phase 4: cost-based fallback. CostBasedScheduleGenerator with pre-warmed UF.
    if (cost_based_schedule_generator(schedule,
                                      &sched_info,
                                      &parent_map,
                                      &op_to_owner,
                                      &uf,
                                      /*cmp=*/sub_8F7900,
                                      /*sort=*/sub_8F7EF0)) {
        goto finalize;
    }

    // Phase 5: zero-producers fallback. Trivial Pipe_ flavour-A schedule.
    sub_8E9450(schedule, raw_value, /*producers=*/NULL, /*n_producers=*/0,
                                     /*consumers=*/&consumer, /*n_consumers=*/1);
    schedule->flags |= 4u;                       // mark trivial-fallback exit

finalize:
    // Phase 6: materialise Op.start and Op.stage per op, then free everything.
    for (Operation *op : schedule_ops(schedule)) {
        SchedInfoRow *row = dense_map_find(&sched_info, op);
        op->stage = row->stage;
        op->start = row->order;
    }

    dense_map_destroy(&sched_info);    // sub_4560420 — bucket slab
    dense_map_destroy(&filter_map);    // sub_4560420
    dense_map_destroy(&parent_map);    // sub_4560420
    dense_map_destroy(&op_to_owner);   // sub_4560420
    uf_buckets_destroy(&uf);           // sub_4560420 per overflow tail + slab
}
```

The eight distinct `sub_4560420` aligned-free sites in the finalize phase match the four `DenseMap` bucket
slabs, the UF bucket slab, the UF per-bucket overflow tails, and two scratch `SmallVector` tails. An inline-vs-heap
check on the SSO capacity word guards every free so the deallocator never runs on stack-resident storage.

### Comparator and Heap-Sort

`sub_8F7900` is the lexicographic `(stage, order)` comparator. It reads `schedInfo[op].stage` first and tie-breaks
on `schedInfo[op].order`. No resource row enters the comparison — `ResourceConstraintBuilder` already consulted the
RRT upstream. `sub_8F7EF0` is a textbook libc++ `__push_heap` / `__pop_heap` pair operating on the 24-strided `Op*`
ready-queue vector. Both functions appear only in phase 4; phase 3's generator dispatch uses its own internal
ordering driven by the constraint-graph topology.

```c
int sub_8F7900(Operation *a, Operation *b, DenseMap *sched_info) {
    SchedInfoRow *ra = dense_map_find(sched_info, a);
    SchedInfoRow *rb = dense_map_find(sched_info, b);
    if (ra->stage != rb->stage) {
        return (ra->stage > rb->stage) ? +1 : -1;
    }
    return (ra->order > rb->order) ? +1 : -1;
}
```

The comparator never returns zero. Ties on `(stage, order)` are impossible at this point because phase 2's topo
walk assigns a unique `order` to every op — which is what lets the heap-sort stay stable without an explicit
tie-breaker.

### Callee Inventory (Sampled)

`sub_8EEE70` calls 41 distinct functions. The ten most relevant beyond the dispatchers and the eight free sites
appear below.

| Callee       | Role                                                                                   |
|--------------|----------------------------------------------------------------------------------------|
| `sub_94A550` | Constraint-slot lookup — returns the `parentMap` leader for an op                      |
| `sub_8E4510` | Constraint propagator — walks the DSU forest to finalise group leaders                 |
| `sub_8E2790` | `origMap` probe at `Schedule+80..96` — used by phase 3 to find raw-value producers     |
| `sub_8E2F00` | Second-table fmix64 probe at `Schedule+104..120` — depth-keyed lookup                  |
| `sub_8F19D0` | Per-pair solve trampoline — caller of `sub_8EEE70`                                     |
| `sub_8EC560` | Union-Find coalesce — merges groups that share a producer post-generator               |
| `sub_8E1900` | DSU snapshot copy-out — preserves the final group leaders for the materializer        |
| `sub_8E4F10` | Alias materialisation, 10 430 bytes — rewrites operand references through `Pipe_` SSA |
| `sub_8FB180` | `parseFromAttrs` reading `nv_tile.aws.stage` and `nv_tile.aws.order`                   |
| `sub_981D50` | Placement-driver entry — generator dispatched in phase 3                               |

`sub_8E4F10` is the heaviest callee — it materialises the `Pipe_` and `Mutex_` operand rewrites that bring the IR
into final form. The 10 430-byte size reflects every operand shape the constraint graph can produce, including the
asymmetric cases where a `Pipe_` consumer sits at a different stage than its producer.

### Zero-Producers Fallback Semantics

The phase 5 fallback fires only when both the placement driver and the cost-based generator report failure. The body emits a single `Pipe_` flavour-A value (the scalar-shaped pipe constructor — see [Pipe and Mutex Value Layout](pipe-mutex-value-layout.md#three-constructors) for the flavour-A/flavour-B split) with zero producers and exactly one consumer — the consumer argument — then sets the trivial-schedule flag on `Schedule.flags`. Neither the `Mutex_` constructor nor the `Pipe_` flavour-B constructor runs from this branch; those primitives are materialised earlier by the walker that precedes the per-pair solve trampoline. The trivial schedule means "ship the consumer with no producers and let later passes diagnose the missing dataflow", not a recovery attempt.

## Dual-RRT Cost Evaluators

Once `Schedule::solve` invokes the placement driver, the cost-based fallback ranks candidate placements through two
dual-RRT cost evaluators. Both wrap in `std::function<bool(int)>`-shaped lambda thunks and route through a shared
exponential-then-binary search driver. The two evaluators answer disjoint feasibility questions: the first measures
actual hardware-resource occupancy under the current `II`, the second measures structural distance between a
candidate op and its data dependencies. The cost-based scheduler runs both and combines them lexicographically —
pipe-slot first as the legality gate, then bank-pressure as the preference signal.

### Exponential-Then-Binary Search Driver `sub_988080`

The driver accepts a candidate-cost predicate lambda. It expands the cost threshold exponentially
(`1, 2, 4, 8, ...`) until the predicate flips from false to true, then binary-searches inside the bracketing range
to pin down the smallest threshold at which the candidate becomes feasible. Two lambda thunks ride the driver:
`sub_987E70` for the bank-pressure evaluator and `sub_987EE0` for the pipe-slot evaluator. Each captures a pointer
to the surrounding `Schedule` state, the candidate `Op*`, and the cycle index being probed; the threshold value
flows through the lambda's single integer argument.

```c
uint32_t sub_988080(std::function<bool(int)> probe, uint32_t lower, uint32_t upper) {
    uint32_t hi = (lower > 0) ? lower : 1;

    // Exponential expansion: double hi until probe flips true or we hit the cap.
    while (hi < upper && !probe((int)hi)) {
        hi = (hi * 2u <= upper) ? hi * 2u : upper;
    }

    if (!probe((int)hi)) {
        return UINT32_MAX;                       // candidate never becomes feasible
    }

    // Binary search inside [lower, hi] for the smallest threshold that satisfies probe.
    uint32_t lo = lower;
    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2u;
        if (probe((int)mid)) {
            hi = mid;
        } else {
            lo = mid + 1u;
        }
    }

    return lo;                                   // smallest feasible threshold
}
```

The return value is the cost-threshold cap at which the candidate first becomes feasible. The cost-based generator
treats that value as the candidate's score and picks the minimum across all candidates at the current cycle.

### Bank-Pressure Evaluator `sub_98C440`

The bank-pressure evaluator probes a dual-RRT pair: `rrt0` holds in-iteration occupancy (resources used by ops
placed at the current iteration of the kernel), `rrt1` holds cross-iteration carry (resources still occupied
from the previous iteration's tail at the same modulo cycle). The evaluator reads pool caps `4` (in-iteration)
and `3` (cross-iteration) from the 9-element pool-capacity vector at indices 1 and 6 — TMEM and named-barrier
pools respectively (see [Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md)). The trampoline
`sub_98E6A0` wires the evaluator into the driver, and the thunk `sub_987E70` adapts the call site to the
`std::function<bool(int)>` shape `sub_988080` expects.

```c
bool sub_98C440(const Schedule *S, const Op *op, uint32_t t, uint32_t cost_cap) {
    // rrt0 = in-iteration occupancy; rrt1 = cross-iteration carry.
    uint32_t in_iter  = countRrtBits(S->rrt0, op->node_rrt, t);
    uint32_t cross_it = countRrtBits(S->rrt1, op->node_rrt, t);

    // Pool caps from the 9-element capacity vector: index 1 = TMEM, index 6 = named-barrier.
    // Hard cap per pool, then a combined budget that the cost driver tightens via cost_cap.
    if (in_iter  > 4u)  return false;            // TMEM bank pressure
    if (cross_it > 3u)  return false;            // named-barrier carry
    if (in_iter + cross_it > cost_cap) return false;

    return true;
}
```

The combined `in_iter + cross_it <= cost_cap` term is what the exponential-then-binary search inside `sub_988080`
walks. The two per-pool caps (`4` and `3`) are hard gates no amount of cost relaxation can lift — they reflect
physical hardware limits on TMEM banks and named-barrier slots, baked into the binary as immediate constants.

### Pipe-Slot Evaluator `sub_98E6C0`

The pipe-slot evaluator probes a single RRT against an N×N all-pairs distance matrix produced upstream by
`sub_98BEE0` — an SSE2-unrolled Floyd-Warshall over the dependence graph. `sub_12D0EA0` initialises the matrix,
filling every cell with the sentinel `0x7FFFFFFF` before the relaxation loops run. The trampoline `sub_990C20`
wires the evaluator into the driver, and the thunk `sub_987EE0` adapts the call site to the
`std::function<bool(int)>` shape.

```c
bool sub_98E6C0(const Schedule *S, const Op *op, uint32_t t, uint32_t slot_cap) {
    // Single-RRT probe: does the candidate's footprint fit in the current pipe slot?
    if (!rrt_probe(&S->rrt_pipe, &op->footprint, t)) {
        return false;
    }

    // All-pairs structural distance gate: every predecessor must reach `op` within slot_cap.
    const int32_t *D = S->dist_matrix;           // n_ops x n_ops i32, row-major
    uint32_t n = S->n_ops;
    for (uint32_t p = 0; p < n; ++p) {
        if (!is_predecessor(S, p, op)) continue;
        int32_t d = D[p * n + op->index];
        if (d == 0x7FFFFFFF) continue;           // no path between these ops — vacuous
        if ((uint32_t)d > slot_cap) return false;
    }

    return true;
}
```

### All-Pairs Distance Matrix `sub_98BEE0`

The matrix is `n_ops × n_ops` `i32` cells stored row-major, allocated by `sub_44A8C20(4 * n_ops * n_ops)`. The
inner kernel processes four cells at a time with SSE2 unrolling. The surrounding outer-`k` / middle-`i` /
inner-`j` triple loop is the canonical Floyd-Warshall shape — recognisable in the disassembly by the three-deep
nest with a `cmp + cmov` (or `pminsd` after vectorisation) sequence on each inner iteration. The initialiser
`sub_12D0EA0` is a `memset`-shaped fill that writes the `0x7FFFFFFF` infinity sentinel into every cell before
the relaxation loops run; the loop then relaxes only edges that exist in the dependence graph to finite distances.

```c
void sub_98BEE0(int32_t *D, const Graph *g, uint32_t ii) {
    uint32_t n = g->n_nodes;
    sub_12D0EA0(D, /*value=*/0x7FFFFFFF, /*n_cells=*/n * n);

    for (Edge e : g->edges) {
        int32_t d = e.latency - (int32_t)(ii * e.iter_distance);
        D[e.src * n + e.dst] = d;
    }

    // Floyd-Warshall with SSE2-unrolled inner loop (4 cells per iteration).
    for (uint32_t k = 0; k < n; ++k) {
        for (uint32_t i = 0; i < n; ++i) {
            int32_t dik = D[i * n + k];
            if (dik == 0x7FFFFFFF) continue;     // skip rows with no path through k
            uint32_t j = 0;
            for (; j + 4u <= n; j += 4u) {
                // SSE2 block: load 4 D[k*n+j..j+3], add dik, pminsd against D[i*n+j..j+3].
                __m128i dkj = _mm_loadu_si128((const __m128i *)&D[k * n + j]);
                __m128i dij = _mm_loadu_si128((const __m128i *)&D[i * n + j]);
                __m128i thru = _mm_add_epi32(dkj, _mm_set1_epi32(dik));
                _mm_storeu_si128((__m128i *)&D[i * n + j], _mm_min_epi32(dij, thru));
            }
            for (; j < n; ++j) {                 // scalar tail
                int32_t thru = D[k * n + j] + dik;
                if (thru < D[i * n + j]) D[i * n + j] = thru;
            }
        }
    }
}
```

The unrolled block uses `pminsd` (SSE4.1) where available and falls back to a scalar `cmp + cmov` pair on the
SSE2-only path; the binary carries both code paths under a CPU-feature dispatch handled higher in the resource
builder. The infinity sentinel survives the relaxation untouched for unreachable pairs because adding any finite
`dik` to `0x7FFFFFFF` overflows, and the `dik != 0x7FFFFFFF` guard at the top of the middle loop masks it off.

### Why Two Evaluators

The two evaluators answer different questions and feed different terms of the lexicographic cost. Bank-pressure
ranks placements by actual hardware-resource occupancy under the current `II` — the signal that stops the
scheduler from overcommitting TMEM banks or named-barrier slots when several candidates are otherwise tied.
Pipe-slot ranks by structural distance between the candidate op and its data dependencies, using the all-pairs
matrix as a constant-time legality oracle — the signal that rejects placements which would force a dependence
edge to span more than the available pipeline depth. The cost-based scheduler runs both and combines them
lexicographically: pipe-slot is the hard legality gate, bank-pressure is the preference between the candidates
that survive the gate.

### Sentinel `0x7FFFFFFF`

The constant `0x7FFFFFFF` plays two distinct roles inside the scheduler, and both stay correct because the value
never appears as a real cost in either context. Inside the distance matrix it means "no path between these two
ops" and survives the Floyd-Warshall relaxation; elsewhere in the scheduler the same value marks snapshot-dead
retry-arm entries. Reimplementations must keep the two roles separate — some downstream cost combiners would
otherwise treat the snapshot-dead value as a finite cost and mis-rank candidates.

## Usage and Contract

`Schedule::solve` runs inside `MaterializeSchedule` once per producer-consumer candidate pair. It consumes the cached `ScheduleAnalysis` produced by `TileASGenerateSchedule`, the constraint-attribute DSU at `Schedule+0x70` seeded by the parser, and the per-pair raw-value handle plus consumer pointer the materializer walker discovered. It produces the per-op stage and order fields on the schedule record and the `Pipe_` and `Mutex_` SSA values emitted by the alias materialiser `sub_8E4F10`. Callers must invoke the cost-based fallback only after the cheap placement driver `sub_981D50` has declined — inverting that order makes every schedule pay the cost-evaluator price even when the cheap path would have succeeded.

The dual-RRT cost evaluators live inside the cost-based fallback. They consume the candidate op, the cycle index `t`, the current `II`, the per-op footprint at `op+96`, the global RRT pair `rrt0` / `rrt1` from the schedule state, the all-pairs distance matrix built by `sub_98BEE0`, and the per-pool caps `4` and `3` at indices `1` and `6` of the 9-element pool-capacity vector. Their output is a smallest-feasible cost threshold per candidate that the cost reducer then ranks lexicographically. The evaluators never mutate the RRT — they only probe.

## Cross-References

[Modulo Scheduler and Rau-Style Placement](modulo-scheduler-and-rau.md) documents the surrounding modulo
scheduler and the placement-arm sequence that invokes these evaluators.
[Resource Constraint Builder and RRT](resource-constraint-builder-and-rrt.md) documents the RRT bit-counting
primitives consumed by the bank-pressure evaluator.
[Blackwell Pipeline 15-Slot Model](blackwell-pipeline-15-slot-model.md) documents the 9-element pool-capacity
vector and the TMEM / named-barrier slots referenced by the per-pool caps `4` and `3`.
[Pipe_ and Mutex_ Value-Header Layout](pipe-mutex-value-layout.md) documents the 808-byte header that the alias materialiser writes into the IR.
