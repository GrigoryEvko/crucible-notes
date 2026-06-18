# Penguin Scheduling: the MinReg Ready-List Scheduler

> *All symbols, strings, and offsets on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 layouts are identical). The subject modules ship as compiled Cython `.so` files — no `.py` source is shipped — so every cited string is recovered from the binary's interned-name pool unless noted otherwise.*

## Abstract

Penguin is the Python middle-end of neuronx_cc. A reader arriving from an LLVM background asks the obvious question: does Penguin reorder instructions before lowering to BIR, or is all scheduling deferred to the backend? The answer is **yes, Penguin schedules** — and it does so with a recognizable construct. `penguin/targets/tonga/passes/Scheduler.py` (shipped as `Scheduler.cpython-310-…-.so`, 1,597,656 bytes) is a min-register-pressure ready-list scheduler whose own docstring declares it a re-implementation of LLVM AMDGPU's `GCNMinRegStrategy`. It runs *post* instruction-selection on `TongaISAInst` engine instructions, builds a data-dependency graph (DDG) over "scheduling units," and greedily picks the ready unit that minimizes peak SBUF/PSUM live bytes. Original program order is preserved on ties via `static_lex_order`.

The crucial framing for a reimplementer is the **two-level division of labour**. Penguin produces a *logical* order: a register-pressure-minimizing linear sequence of scheduling units, plus typed dependency edges, plus collective-ordering and software-pipelining decisions. It does **not** allocate engines, count cycles, weight latencies, or insert cross-engine synchronization. Those are the backend's job — `libwalrus`' PreSched / PostSched / TimeAware / `synchronizer` family, which runs *after* `BirCodeGenLoop` lowers the Penguin order into BIR. `BirCodeGenLoop` itself is a structural lowering walk; it emits the post-schedule order verbatim, honoring only a per-region `no_reorder` opt-out. So Penguin decides *what good order means*; the backend decides *the final physical, sync-correct schedule*.

This page documents the `Scheduler` class as its primary subject (the DDG, the five-phase ready-list loop, the `TongaLiveInterval` pricing model), then the two sibling Penguin ordering passes that also perturb instruction order — `AGOrderingAnalysis` (a greedy feedback-arc-set total-order over global all-gathers) and `SoftwarePipelineCodeGen` (annotation-driven modulo-pipeline materialization) — and closes with the dependency substrate they all schedule over and the clean handoff to BIR.

For reimplementation, the contract is:

- The **scheduling-unit (SU) data model**: an SU is `Union[NeuronInst, Block]`; the DDG is `Dict[SUType, Set[SUType]]` (successors), with a mirrored predecessor map and a `Dict[SUType, int]` priority map.
- The **five-phase MinReg algorithm**: build DDG → Kahn-style readiness init → priority assignment with predecessor-bumping → greedy min-pressure pick → commit + release. The pressure objective is `def_bytes − kill_bytes` per SU, priced by `TongaLiveInterval`.
- The **logical-vs-physical boundary**: what Penguin emits (typed `DependencyEdge` set + post-schedule order + `no_reorder` flags) versus what the backend re-derives (physical FLOW/ANTI edges post-allocation, the resource/latency/cycle schedule, semaphore sync).
- The two **sibling ordering passes** and why they are separate from MinReg.

| | |
|---|---|
| **Primary module** | `penguin/targets/tonga/passes/Scheduler.py` → `Scheduler.cpython-310-…-.so` (1,597,656 bytes) |
| **Class** | `Scheduler` (+ helper `SchedulerScope`; `SUType` type alias) |
| **Provenance** | LLVM AMDGPU `GCNMinRegStrategy` re-implementation (verbatim docstring) |
| **IR level** | Tonga ISA instructions (`TongaISAInst`) — runs **post-ISel**, **pre-BIR** |
| **Objective** | Minimize peak SBUF/PSUM live bytes (register/buffer pressure), **not** ILP/latency |
| **SU type** | `Union[NeuronInst, Block]` |
| **DDG type** | `Dict[SUType, Set[SUType]]` (successors); mirror = predecessors |
| **Tie-break** | `static_lex_order` — original program order (deterministic anchor) |
| **Pressure model** | `targets/transforms/TongaLiveInterval` + `Dataflow` def/use sets |
| **Lowering consumer** | `targets/codegen/BirCodeGenLoop` — emits SU order verbatim; reads `no_reorder` |
| **Option gate** | `Options` flag `compatible_mode` — *"Compatible mode for the old scheduler"* |

---

## The Scheduling-Unit Data Model

### Purpose

Before the algorithm, fix the vocabulary. A *scheduling unit* (SU) is the atom the scheduler reorders. The verbatim type-annotation strings recovered from the `Scheduler.so` interned-name pool define the entire data model — these are Cython `__pyx_kp_s_*` annotation constants, so they are the literal source-level type hints baked into the binary.

### Data Model

| Symbol | Type annotation (verbatim string) | Meaning | Confidence |
|---|---|---|---|
| `SUType` | `Union[NeuronInst, Block]` | A scheduling unit is an instruction **or** a block | CONFIRMED |
| DDG / successors | `Dict[SUType, Set[SUType]]` | SU → set of successor SUs (the forward edges) | CONFIRMED |
| predecessors/successors maps | `Optional[Dict[SUType, Set[SUType]]]` | mirror maps; built/copied by `build_ddg`/`_copy_edges` | CONFIRMED |
| `priority_map` | `Optional[Dict[SUType, int]]` | SU → integer priority | CONFIRMED |
| `heuristic_func` | `Callable[[SUType], Any]` | the sort key applied to ready SUs | CONFIRMED |
| ready-set ops | `Iterable[SUType]` / `List[SUType]` / `Optional[List[SUType]]` | ready-list element types | CONFIRMED |
| `LiveVariableT` | (live-variable type alias) | the live-bytes pricing element | CONFIRMED |

> **NOTE —** that an SU can be a whole `Block`, not only a leaf `NeuronInst`, is the structural reason a `no_reorder` opt-out exists at the *scope-region* level (see [§Logical-vs-Physical Boundary](#the-logical-vs-physical-boundary)). The scheduler moves blocks, so a region must be able to say "treat my contents as a fixed unit."

Two further model facts come from the pool. **Weight tensors are special-cased**: the strings `is_weight_buffer`, `NeuronWeightTensor`, and `static_weights` co-occur, and the pressure roster carries `is_static`. Constant weight buffers are *static* — they are live for the whole kernel and therefore do not contribute to the *dynamic* pressure delta the heuristic optimizes. **Globals widen dependencies**: `is_global_mod` / `is_global_ref` / `get_global_mod_vars` / `get_global_ref_vars` distinguish globally-visible tensor accesses, which must be conservatively ordered against all other accesses of the same global.

---

## Phase 1 — Build the DDG

### Purpose

Construct the data-dependency graph over SUs from their def/use (modify/reference) variable sets. This is the substrate the ready-list walks. The graph is built once per scheduling scope, then mutated destructively (`remove_node`) as units are scheduled.

### Entry Point

```text
Scheduler.schedule (driver)
  └─ initialize                     ── per-scope setup
  └─ build_ddg                      ── construct successor edges
       ├─ enumerate_dependencies    ── per-SU def/use → edges
       │    ├─ enumerate_defs       ── Dataflow def set
       │    ├─ enumerate_mod_vars   ── modified (written) vars
       │    └─ enumerate_ref_vars   ── referenced (read) vars
       └─ _copy_edges               ── build the mirror predecessor map
```

### Algorithm

```c
// Scheduler.build_ddg — DDG construction over SUs
// SUType = Union[NeuronInst, Block]; edges: Dict[SUType, Set[SUType]]
function build_ddg(scope):
    succ = {}                                   // Dict[SU, Set[SU]]
    last_writer  = {}                           // var -> SU (last def)
    last_readers = {}                           // var -> Set[SU] (since last def)
    for su in scope.statements_in_program_order:        // static_lex_order order
        defs = enumerate_defs(su)               // Dataflow: written tensors
        uses = enumerate_ref_vars(su)           // Dataflow: read tensors
        mods = enumerate_mod_vars(su)           // in-place modified tensors

        // FLOW (true / read-after-write): producer -> consumer
        for v in uses:
            if v in last_writer:
                add_edge(succ, last_writer[v], su)        // FLOW edge

        // ANTI (write-after-read): reader must precede writer
        for v in (defs | mods):
            for r in last_readers.get(v, {}):
                add_edge(succ, r, su)                     // ANTI edge

        // OUTPUT (write-after-write): serialize same-var writers
        for v in (defs | mods):
            if v in last_writer:
                add_edge(succ, last_writer[v], su)        // OUTPUT edge

        // globals widen: a global mod/ref orders vs ALL global touchers
        if is_global_mod(su) or is_global_ref(su):
            order_against_global_accessors(succ, su)      // is_global_*

        // update tracking
        for v in uses: last_readers.setdefault(v, set()).add(su)
        for v in (defs | mods):
            last_writer[v] = su
            last_readers[v] = set()             // a write resets the reader set

    preds = _copy_edges(succ)                   // mirror: predecessor map
    return succ, preds
```

> **GOTCHA —** weight buffers must be filtered out of the *pressure* accounting but **not** out of the *dependency* graph. `is_static` / `is_weight_buffer` gate the live-bytes delta (a static weight that is always live contributes zero to the dynamic peak), yet a read of that weight still imposes a FLOW edge ordering. Conflating the two — dropping weight edges to "save" pressure — produces an illegal schedule.

The edge *kinds* themselves (FLOW / ANTI / OUTPUT / ORDERED) are not invented here — they are the same four-kind model the dependency object carries; see [§The Dependency Substrate](#the-dependency-substrate). The pseudocode above is the standard last-writer/last-readers construction (STRONG: method names `build_ddg`/`enumerate_dependencies`/`_copy_edges` and the `enumerate_*` Dataflow helpers are CONFIRMED in the pool; the precise edge-insertion order is the standard list-scheduler contract, INFERRED).

---

## Phase 2 — Initialize Readiness

### Purpose

Seed the ready set with the topological roots and install per-SU predecessor counters that drive Kahn-style release. This is the classic in-degree initialization specialized to the SU graph.

### Algorithm

```c
// Scheduler init: ref_count[su] = in_degree(su); ready = {su : preds == 0}
// names CONFIRMED: init_ref_counts, num_preds, in_degree, enumerate_ready,
//                  filter_num_preds, filter_readys
function init_ref_counts(succ, preds):
    for su in all_sus:
        ref_count[su] = in_degree(su)           // == len(preds[su]) == num_preds
    return ref_count

function enumerate_ready(ref_count):
    // initial ready set = SUs with no unscheduled predecessor
    return filter_num_preds(all_sus, want_preds=0)   // filter_readys wraps this
```

`in_degree(su) == |predecessors(su)| == su.num_preds`. The three counter views (`num_ready_succs`, `num_not_ready_succs`, `num_preds`) exist because the heuristic needs both directions: how many predecessors still block this SU (readiness), and how many of an SU's successors are/aren't yet ready (used to estimate whether scheduling this SU *frees* a value, i.e. whether it is a last use). All four names — `in_degree`, `out_degree`, `num_ready_succs`, `num_not_ready_succs` — are CONFIRMED in the pool.

---

## Phase 3 — Priority Assignment

### Purpose

`GCNMinRegStrategy`'s defining behavior: pull a value's *producer* earlier so the value goes live as late as possible — equivalently, pull a value's *last consumer* earlier so the value dies as soon as possible. This is implemented as a priority bump that propagates *backward* to predecessors.

### Algorithm

```c
// Scheduler priority — names CONFIRMED: priority, priority_map,
//                       update_priority, bump_preds_priority
function bump_preds_priority(su, delta):
    // raise the priority of su's predecessors so the producers of a
    // soon-to-be-needed value are scheduled earlier (shrinks live window)
    for p in predecessors(su):
        priority_map[p] += delta                 // SU -> int

function update_priority(state):
    // recompute after each scheduling step: as successors become ready,
    // the producers feeding them gain urgency
    for su in newly_relevant_sus(state):
        recompute_priority(su)                   // feeds heuristic_func
```

> **QUIRK —** the priority flows *up* the DAG (to predecessors), which is the opposite of a latency/critical-path scheduler that pushes priority *down* toward sinks. The objective is not "finish the long chain early" but "keep the live set small" — so the scheduler rewards scheduling the *producers* of values that are about to be consumed, collapsing their live window. A reimplementer who copies a latency-list-scheduler priority will get ILP-friendly but pressure-hostile schedules and miss the entire point of the pass. (STRONG: derived from `bump_preds_priority`'s direction + the `MinBufPressure` docstring.)

---

## Phase 4 — Greedy Min-Pressure Pick

### Purpose

The heuristic core. From the current ready set, pick the SU whose scheduling produces the smallest increase (ideally a decrease) in live register/buffer bytes. This is where `TongaLiveInterval` pricing meets the ready list.

### Algorithm

```c
// Scheduler.pick_candidate — names CONFIRMED: heuristics, heuristic_func,
//   pick_candidate, pick_candidate_from, pick_candidate_from_list,
//   pick_n_candidates, def_bytes, kill_bytes, alloc_size_in_bytes
function pick_candidate(ready):
    // pressure delta of scheduling su now:
    //   + def_bytes(su)   : bytes newly made live by su's results
    //   - kill_bytes(su)  : bytes freed because su is the LAST use of them
    best = None
    for su in ready:                              // pick_candidate_from_list
        delta = def_bytes(su) - kill_bytes(su)    // MinReg pressure delta
        key   = (delta,                           // primary: minimize live bytes
                 -priority_map[su],               // then: bump_preds urgency
                 su.static_lex_order)             // tie-break: original order
        if best is None or key < best_key:
            best, best_key = su, key
    return best                                   // heuristic_func == this key fn

function pick_n_candidates(ready, n):
    // limited look-ahead: evaluate the n best ready SUs for the
    // resulting pressure of scheduling each, not just the immediate delta
    return n_smallest(ready, key=heuristic_func, n=n)
```

The pressure delta is `def_bytes(su) − kill_bytes(su)`: `def_bytes` is the bytes su's outputs make newly live, `kill_bytes` is the bytes freed when su consumes the last use of an input. `alloc_size_in_bytes` converts a tensor's shape/dtype to a byte count; `TongaLiveInterval` supplies the live-interval bookkeeping. All three pricing names plus the four `pick_*` candidate selectors are CONFIRMED.

> **NOTE —** the docstring carries a literal `"Non-deterministic heuristics"` token, indicating an *alternate* heuristic mode distinct from the deterministic primary key above. The deterministic mode terminates the key with `static_lex_order` so equal-pressure ready SUs schedule in original program order; the non-deterministic mode presumably randomizes among equal-cost candidates (useful for autotuning / breaking pathological tie patterns). The exact non-deterministic key is INFERRED — only the mode's existence is CONFIRMED.

`static_lex_order` as the final tie-break is what makes the whole pass deterministic and minimally-perturbing: when two ready SUs cost the same, the one earlier in the original program wins, so the scheduler never gratuitously reorders. The attribute is pervasive — it appears in **53** Penguin `.so` modules (CONFIRMED by string sweep), establishing it as the IR's canonical "position in original program order" anchor, not a scheduler-local invention.

---

## Phase 5 — Commit and Release

### Purpose

Append the picked SU to the output schedule, decrement its successors' predecessor counters, and admit any successor that just hit zero into the ready set. The Kahn loop runs until every SU is scheduled.

### Algorithm

```c
// Scheduler.schedule — the driver — names CONFIRMED: schedule, schedule_stmt,
//   schedule_ready_su, release_successors, release_sus, remove_node, all_scheduled
function schedule(scope):
    succ, preds = build_ddg(scope)               // Phase 1
    ref_count   = init_ref_counts(succ, preds)   // Phase 2
    ready       = enumerate_ready(ref_count)
    order       = []                             // the new linear SU order

    while not all_scheduled():
        su = pick_candidate(ready)               // Phase 4 (+ Phase 3 priority)
        order.append(su)                         // schedule_stmt: emit su
        schedule_ready_su(su)                    // mark scheduled

        for s in successors(su):                 // release_successors
            ref_count[s] -= 1
            if ref_count[s] == 0:
                ready.add(s)                      // release_sus
        bump_preds_priority(su, ...)             // Phase 3 update
        update_priority(ready)
        remove_node(su)                          // detach from live graph
        ready.remove(su)

    write_back(scope, order)                     // "moving loopnest around"
    return order
```

The net effect: a **new linear SU order** is written back onto the loop body / scope region (the docstring's *"moving loopnest around"*), minimizing peak SBUF/PSUM live bytes, with original order preserved on ties. This is a genuine pre-BIR reorder of the instruction stream — the direct answer to "does Penguin schedule?"

### What It Deliberately Is Not

```text
NOT in the Penguin MinReg scheduler          OWNED INSTEAD BY
─────────────────────────────────────────    ─────────────────────────────────
engine reservation table                      backend PostSched (per-engine)
cycle clock / cycle-accurate timing           backend TimeAware/Lnc
Hwm instruction-latency weighting             backend PostSched (latency model)
cross-engine sync (semaphore) insertion       backend order_constraints/synchronizer
physical (post-allocation) FLOW/ANTI edges    backend build_fdeps/anti_dep_analyzer
```

The Penguin scheduler's *single* objective is buffer-pressure minimization. It has no notion of which engine an instruction runs on, how many cycles it takes, or how two engines synchronize. (STRONG — the absence of any reservation-table / cycle / latency vocabulary in the `Scheduler.so` pool, cross-checked against the backend's exclusive ownership of those concepts.)

---

## The Two Sibling Penguin Ordering Passes

MinReg is **the** list scheduler, but it is not the only Penguin-level pass that perturbs instruction order. A vocabulary sweep for ready-list scheduler tokens (`ready_list`, `schedule_ready`, `build_ddg`, `enumerate_ready`, `pick_candidate`) hits *exactly one* module — `Scheduler.so` — bounding the list-scheduler count to one. Two other passes make ordering *decisions* without running a list scheduler.

### AGOrderingAnalysis — Greedy Feedback-Arc-Set over Global All-Gathers

`targets/transforms/AGOrderingAnalysis.py` computes an optimal total order among *global all-gather* collectives. Its verbatim docstrings: *"AGOrderingAnalysis - discover optimal order among global and local AGs"*, *"build graph containing all partial ordering relationships between global AGs"*, *"find the optimal ordering of global AGs. Also find the forced and removable edges"*, *"find the optimal ordering of nodes (each node is a global ag) such that we minimize…"*, and the provenance *"adapted from GR algorithm of http://ajc.maths.uq.edu.au/pdf/12/ocr-ajc-v12-p15.pdf"* — the Eades–Lin–Smyth **greedy feedback-arc-set** heuristic (AJC vol. 12, p. 15).

```c
// AGOrderingAnalysis — greedy feedback-arc-set total ordering of global AGs
// classes CONFIRMED: AGForcedOrderingEdge, AGRemovableOrderingEdge,
//   AGGlobalOrderingEdge, GlobalAxesGroupOrderer, PartialToTotalAGOrder
function analyzeFunction(func):                  // AGOrderingAnalysisPass
    g = build_partial_order_graph(global_ags)    // forced + removable edges
    // forced edge  = hard ordering constraint (.is_forced == True)
    // removable edge = soft; may be broken to break a cycle, at .cost
    while g.has_cycle():
        e = pick_min_cost_removable_edge(g)      // "BREAK REMOVABLE EDGE:"
        if no_removable_edge_in_cycle:
            e = pick_forced_edge_to_break(g)     // "BREAK FORCED EDGE:" (rare)
        g.delete(e)                              // GR edge removal step
    total = PartialToTotalAGOrder.sort(g)        // linearize partial -> total
    emit "GLOBAL AG ORDERING (low to high):" total
```

The edge classes each carry `.cost`, `.is_forced`, and `.priority` (CONFIRMED via `AGForcedOrderingEdge.cost` / `AGRemovableOrderingEdge.cost` / `.is_forced` qualnames). This is a **second**, collective-specific Penguin ordering decision: it fixes the relative order of global all-gathers so they fuse and overlap well downstream. The comm/compute *overlap reschedule* itself was already set up at the HLO level (the MLIR `ScheduleFusion` pass, see cross-refs) and is finalized by the backend synchronizer; `AGOrderingAnalysis` decides the order *among* the resulting global AGs. (All CONFIRMED.)

### SoftwarePipelineCodeGen — Annotation-Driven Modulo Pipeline

`targets/transforms/SoftwarePipelineCodeGen.py` is a software-pipelining / modulo-scheduling **codegen** pass — it materializes a pipeline from per-statement `stage_id` + `execution_order` annotations that were computed upstream. Verbatim docstrings: *"Creates a new loop axis with extended bounds (ub + num_stages - 1) to accommodate all pipeline stages"*, *"Reorder instructions in the loop body based on execution_order"*, *"execution_orders: List of execution orders for each group (None means keep original position)"*, and the rotating-buffer sizing rule from *"earliest stage where a tensor is defined and the latest stage where it's used"*.

```c
// SoftwarePipelineCodeGen — names CONFIRMED: schedule_software_pipeline,
//   _create_software_pipeline_schedule, _reorder_instructions
function schedule_software_pipeline(loop):
    if not _has_valid_stage_annotations(loop):
        return                                   // nothing to pipeline
    new_axis = extend_bounds(loop, ub + num_stages - 1)   // prologue+epilogue room
    for tensor in defined_tensors(loop):
        size = latest_use_stage - earliest_def_stage + 1  // rotating buffer depth
        _allocate_tensor(tensor, size)
    _adjust_indices(loop, by_stage_offset)       // offset accesses by stage
    if has_execution_order(loop):
        _reorder_instructions(loop, execution_order)      // the reorder
    else:
        keep_original_order(loop)                // explicit fallback
    insert_prologue_epilogue_predicates(loop)
```

> **NOTE —** this is the *order-materializing lowering* of a pipeline, not the *scheduling decision*. The stage assignment and intra-iteration order are computed elsewhere and arrive as annotations; if no `execution_order` annotation is present the pass explicitly *keeps original order* (CONFIRMED docstring *"No execution_order specified, keeping original stage order"*). It is a **third** Penguin reorder site, distinct from MinReg and `AGOrderingAnalysis`. See the dedicated software-pipelining page in cross-refs.

---

## The Dependency Substrate

All three reorder passes schedule over the same dependency machinery, which also defines the typed edges that cross into BIR.

### `ir/Dependency` — the Typed Edge Object

`ir/Dependency.py` defines `DependencyEdge(src, dst, kind)` with `kind ∈ {FLOW, ANTI, OUTPUT, ORDERED}` and a `.serialize()` method (json fields `kind` / `src` / `dst`). All seven tokens — `DependencyEdge`, `FLOW`, `ANTI`, `OUTPUT`, `ORDERED`, `serialize`, plus the `kind`/`src`/`dst` field names — are CONFIRMED in the module's string pool.

> **QUIRK —** this four-kind edge set is byte-for-byte the same model the backend's `libBIR` uses (`bir::EdgeKind`, with the ordering `Invalid < Ordered < Anti < Output < Flow`). `DependencyEdge.serialize` is therefore the **explicit Penguin→BIR dependency hand-off**: Penguin emits typed logical edges and BIR receives them rather than recomputing the logical set from scratch. A reimplementer who recomputes dependencies on the BIR side from program order alone will lose the global-widening and alias-induced edges Penguin already discovered. (CONFIRMED, cross-binary.)

### `LoadStoreDependencyAnalysis` — the Instruction DAG

`transforms/LoadStoreDependencyAnalysis.py` builds the instruction-level dependency DAG from tensor load/store accesses: classes `InstDependencyGraph` / `NeuronInstDependencyGraph` with `build_inst_dependency_graph` (+ `insert_dep_edges_on_insts`), `build_inst_2_tensor_access_mapping`, `build_dags`, transitive-closure reachability (`closure_within_dep_graph`, `has_path`), `node_in_graph`, `is_dependent`. This is the memory-dependency analysis that backs both MinReg's DDG and the serialized BIR edges.

### `AliasDependency*` — Alias-Induced Ordering and Its Guards

The `AliasDependency{Induction,Elimination,Reset,Verifier,VerificationPass}` family handles tensors that *alias* (overlapping buffers). `AliasDependencyInduction.build_inst_to_dag` *induces* extra ordering edges where buffers overlap, so the scheduler / codegen cannot illegally reorder aliasing accesses; the Elimination/Reset/Verifier passes prune and validate them. The very existence of an alias-safety verifier at the Penguin level is corroborating proof that reordering is a Penguin-level activity that needs alias guards — a backend-only design would not carry these IR passes.

### `Region` and `static_lex_order` — the Ordering Basis

`transforms/Region.py` computes the structural numbering basis: `preorder_counter`, `rev_preorder`, `pdom_rev_preorder` (pre/post-order and post-dominator-reverse-preorder DFS numberings over the region CFG). `static_lex_order` is the canonical original-program-order key derived from this numbering and threaded through the IR (`ir/Function`, `ir/StmtMixIn`, `ir/Tensor`, and the 53 modules counted above). The IR preserves a stable original order at all times; scheduling perturbs the *emitted* order while `static_lex_order` remains the deterministic anchor.

---

## The Logical-vs-Physical Boundary

### Where MinReg Runs

```text
… TongaISel (instruction selection)
   → tiling / loop-nest formation
      → [Tonga MinReg Scheduler reorders SUs]      ← THIS PAGE
         → GlobalLayoutOpt (… "no inst reordering before this pass")
            → BirCodeGenLoop (structural lowering, emits SU order verbatim)
               → BIR
                  → libwalrus: PreSched → PostSched → TimeAware/Lnc
                             → order_constraints → alloc_semaphores
                             → synchronizer → lower_sync
```

Pipeline position is triangulated (STRONG): the scheduler **imports `TongaISAInst`**, so it operates on ISA-mapped engine instructions and therefore runs *after* ISel and loop-nest formation; `GlobalLayoutOpt`'s own docstring says verbatim *"(due to elementwise SSA inherited from tensorflow, and no inst reordering before this pass)"* (CONFIRMED), explicitly marking a point before which no reordering has happened.

### `BirCodeGenLoop` Does Not Schedule

`BirCodeGenLoop` is a structural lowering walk: its roster is `runOnModule` / `runOnFunction` / `transformInstruction` / `transformScopeRegion` / `codegen<Op>` (`codegenAllGatherOp`, `codegenDMACopyOp`, `codegenLoop`, …) / `addInstToBir` (all CONFIRMED). It contains **none** of the ready-list vocabulary (`build_ddg`, `pick_candidate`, …). Its only ordering token is `no_reorder`, which it *reads* — the flag is *defined* in `ir/ScopeRegion` and *read* in `BirCodeGenLoop`, and in no other Penguin module (CONFIRMED by string sweep: exactly those two `.so` files contain `no_reorder`). So `BirCodeGenLoop` emits whatever order the scheduler left, honoring only a per-region "don't move my contents" opt-out.

### What Crosses Into BIR vs. What the Backend Re-Derives

| Crosses Penguin → BIR | Re-derived / owned by backend (`libwalrus`) |
|---|---|
| Typed `DependencyEdge` set {FLOW,ANTI,OUTPUT,ORDERED} via `.serialize()` (CONFIRMED) | Physical FLOW/ANTI edges over real addresses, rebuilt *post-allocation* (STRONG) |
| The post-schedule SU order (MinReg + AG + SW-pipeline), emitted verbatim (STRONG) | The resource/latency/cycle-accurate schedule (PreSched/PostSched/TimeAware) |
| `no_reorder` scope-region flags as IR attributes (CONFIRMED) | Cross-engine semaphore sync (`order_constraints`/`synchronizer`/`lower_sync`) |

> **CORRECTION (SCHED-DYNDISPATCH) —** an earlier reading treated the *absence* of the literal substring `"Scheduler"` from every other `.so` as evidence the scheduler is dead code. It is not. `penguin/PassConstructor.PassConstructorBuilder` loads passes by string name via `__import__`, and dynamically-built module names leave no static literal in the caller's pool. Liveness is corroborated by the `compatible_mode` option (*"Compatible mode for the old scheduler"*, CONFIRMED — an old-vs-new scheduler split shipped as a user-facing flag) and the `no_reorder` opt-out (which is meaningless unless a scheduler can reorder). The exact `PassConstructor` registration call site and option gate remain INFERRED (built via dynamic import); the scheduler's existence and behavior are CONFIRMED.

---

## Adversarial Self-Verification

The five strongest claims on this page, re-challenged against the binary:

1. **"Scheduler is a `GCNMinRegStrategy` re-implementation."** Re-verified: `strings Scheduler.so` yields verbatim `MinBufPressure - Minimize memory pressure by moving loopnest around`, the `github.com/llvm/llvm-project/.../GCNMinRegStrategy.cpp` URL, `Current implementation is based on`, the LLVM license line, and `Copyright (C) 2020, Amazon.com`. **CONFIRMED.**
2. **"The five-phase method roster exists."** Re-verified: an exact-match `rg -x` over the pool returns all of `build_ddg`, `enumerate_ready`, `pick_candidate{,_from,_from_list}`, `pick_n_candidates`, `schedule_ready_su`, `release_successors`/`release_sus`, `bump_preds_priority`, `update_priority`, `init_ref_counts`, `all_scheduled`, `def_bytes`/`kill_bytes`/`alloc_size_in_bytes`, `static_lex_order`. **CONFIRMED** as names; the *bodies* are Cython (not decompilable), so the phase logic is tagged STRONG/INFERRED in place.
3. **"Edge model is {FLOW,ANTI,OUTPUT,ORDERED} + serialize."** Re-verified: `Dependency.so` pool contains exactly `FLOW`, `ANTI`, `OUTPUT`, `ORDERED`, `DependencyEdge`, `serialize`, `kind`, `src`, `dst`. **CONFIRMED.**
4. **"`no_reorder` is defined in `ScopeRegion`, read in `BirCodeGenLoop`, nowhere else."** Re-verified by a per-`.so` string sweep across the whole penguin tree: exactly those two modules match. **CONFIRMED.**
5. **"Runs post-ISel on `TongaISAInst`, priced by `TongaLiveInterval`."** Re-verified: the import strings `…targets.tonga.TongaISAInst` and `…targets.transforms.TongaLiveInterval` are present in `Scheduler.so`; `TongaLiveInterval.so` exposes `alloc_size`. **CONFIRMED** (import edges); the exact pressure formula `def_bytes − kill_bytes` is STRONG (names CONFIRMED, arithmetic is the GCNMinReg contract).

Anything not pinnable to a string/symbol is tagged INFERRED inline: the precise edge-insertion order in `build_ddg`, the non-deterministic heuristic key, and the dynamic-import registration site.

---

## Related Components

| Name | Relationship |
|---|---|
| `BirCodeGenLoop` | Consumes MinReg's SU order; lowers to BIR; reads `no_reorder` |
| `ir/ScopeRegion` | Defines the `no_reorder` opt-out attribute |
| `AGOrderingAnalysis` | Sibling Penguin ordering pass (global all-gather total order) |
| `SoftwarePipelineCodeGen` | Sibling Penguin reorder (annotation-driven modulo pipeline) |
| `LoadStoreDependencyAnalysis` | Builds the inst DAG backing both the DDG and BIR edges |
| `TongaLiveInterval` | Live-interval pricing for the MinReg pressure objective |
| `libwalrus` PreSched / PostSched | Backend physical, resource/latency/cycle-accurate scheduler |
| `synchronizer` / `order_constraints` | Backend semaphore-sync materialization |

## Cross-References

- [Penguin Dependency Model](dependency-model.md) — the `DependencyEdge` kinds and dep-edge construction this scheduler walks
- [Software Pipelining](software-pipelining.md) — the `SoftwarePipelineCodeGen` modulo-pipeline reorder in full
- [Post-Scheduling (walrus)](../post_sched/overview.md) — the backend physical schedule that consumes Penguin's logical order and BIR edges
