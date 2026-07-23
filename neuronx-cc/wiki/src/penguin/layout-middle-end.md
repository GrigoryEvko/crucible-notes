# Penguin Layout Middle-End

> *All symbols, strings, and addresses on this page are from neuronx_cc **2.24.5133.0+58f8de22**, cp310 wheel (the layout subsystem is byte-identical across cp310/311/312). Evidence is static analysis of the Cython extension modules under `neuronxcc/starfish/penguin/targets/`: the `__pyx_*` symbol names embed the original Python `class.method` names, and the Cython string table preserves identifiers, format literals, and (where not stripped) docstrings verbatim.*

## Abstract

After Penguin has lowered HLO to its tensor IR and *before* the SBUF/PSUM coloring allocators run, one middle-end decides the **physical layout** of every tensor: which logical axis maps to the 128 SBUF partitions, how the remaining (free) axes pack, and where a physical **transpose** must be inserted when a producer's layout disagrees with a consumer's requirement. This is the layout middle-end. It is the Neuron analogue of an XLA layout-assignment pass, but the hardware constraint it serves is specific to the NeuronCore systolic array: a matmul contracts *along partitions*, so the contraction axis of both operands is forced onto the partition dimension, and any mismatch is paid for in transpose cycles.

The subsystem is built as a **two-stage family**. Stage A, `PAGLayoutAnalysis.PAGLayoutFramework`, builds the search structures: it groups `(tensor, axis)` nodes into *Partition-Axis Groups* (PAGs), assembles candidate *PAGSets* per dataflow sub-graph (DAG), and decomposes the coupled-layout problem into independent *undecided connected components* (UCCs). Stage B, `PAGLayoutGreedySearchAlgorithm.PAGLayoutGreedySearchAlgorithm.solve`, searches each UCC for the minimum-cost assignment under a static cycle cost model (`CycleBasedLayoutCostModel`) — and crucially, the search **is not a single greedy pass**: it runs *exhaustive* search when the UCC's solution space is small, *divide-and-conquer* when it is medium, and a *greedy* fallback only when it is large.

A second, optional path exists. The CLI flag `--layout-transform-heuristic=mcts` arms the generic Penguin **autotuner** around the layout-transform decision, turning each layout candidate into a *plan*, building a *design* (an ordered list of plan ids) with Monte-Carlo tree search, and evaluating every candidate design by a **real subprocess compile** that reports the scheduler's post-schedule estimated latency. The default is `none`: the deterministic PAG static search runs and no subprocess compile happens. This page documents both, plus a third trap — a *legacy* in-process MCTS (`LayoutDecisionTree.LayoutState`) that predates the PAG super-pass and is driven by the now-deprecated `GlobalLayoutOpt`. There are therefore **two distinct Monte-Carlo searches over layout** in this binary, and the default `-O2` compile uses neither: it uses the PAG static greedy/exhaustive search.

For reimplementation, the contract is:

- **The data model** — PAG / PAGSet / DAG / UCC, and how `dag2pagsets` encodes a global layout.
- **The partition-axis decision** — legal candidate enumeration, matmul contract→partition forcing, the `allow_tc_swap` peephole, and conflict→transpose insertion.
- **The search** — the exhaustive / divide-and-conquer / greedy tri-modal `solve`, costed by `CycleBasedLayoutCostModel`.
- **The cost model** — the per-instruction cycle table, the transpose price, axis categorization (B/P/F), tripcount scaling, legality pruning, and memoization.
- **The autotuner action space** — plan/design semantics (`add_plan`, `_record_plan_to_design`, `_fetch_plan_from_design`), the six interchangeable search backends, and the two `PerformanceMetric` reward backends.
- **The boundary** — what the `none` default produces vs. what `mcts` produces, and how the winning layout is materialized by `TransformLayout`.

| | |
|---|---|
| **Default assignment** | `PAGLayoutGreedySearchAlgorithm.PAGLayoutGreedySearchAlgorithm.solve` (tri-modal: exhaustive / D&C / greedy) |
| **Analysis (structures)** | `PAGLayoutAnalysis.PAGLayoutFramework` — `build_initial_pags_and_pagsets` → `calculate_final_par_axes` |
| **Candidate enumerator** | `LayoutCandidateEnumerator` — `enumerate_legal_partition_axes_subsets` × `enumerate_free_axes` (+ `enumerate_par_const_dims_candidates`) |
| **Static cost** | `CycleBasedLayoutCostModel` — `dag_cost` / `tc_dag_cost`, `_inst_cost_*`, `transpose_cost`, memoized via `_build_inst_cost_cache_key` |
| **CLI flag** | `--layout-transform-heuristic={mcts\|none}` (default `none`); help text `"Heuristic for layout transformation, mcts/none"` in `tonga/CodeGenFlow.so` |
| **Autotuner** | `autotune/Autotuner.so` — `add_plan` / `add_no_change_plan`; design = ordered plan-id list |
| **Reward (mcts)** | `_PerformanceMetric.PostSchedMetric._evaluate` → `nc_latency` (PostSchedEstLatency) from a child compile |
| **Apply pass** | `tonga/passes/TransformLayout` — `calculateTargetLayout`, `buildTranspose`, `calculateIOTranspose` |
| **Two MCTS layers** | `#1` legacy `LayoutDecisionTree.LayoutState` (in-process, static cost) · `#2` `_TreeSearch.MCTSTuneState` (autotuner, compile reward) |

---

## 1. The CLI Hook — `--layout-transform-heuristic`

### Purpose

A single user-facing toggle, independent of the `-O` optimization level, that selects whether the layout decision is made by the deterministic PAG static search (`none`, default) or by the compile-in-the-loop autotuner MCTS (`mcts`).

### Evidence

All strings below are verbatim from `targets/tonga/CodeGenFlow.cpython-310-x86_64-linux-gnu.so`:

```text
"Heuristic for layout transformation, mcts/none"   ── the argparse help
"layout-transform-heuristic"                        ── the CLI option spelling
"_layout_transform_heuristic"                       ── the parsed attribute on the flow
"used_mcts_layout"                                  ── output telemetry flag
"mcts"                                              ── the value tested for equality
"TransformLayout" / "GlobalLayoutOpt" / "LowerTranspose"  ── the passes it sequences
```

`CompileCommand.so` carries the request-side names `mcts_layout` and `used_mcts_layout`. The accepted value set `{mcts, none}` and the default `none` come from the help literal plus the `== "mcts"` equality test in the CodeGenFlow pipeline assembly. The argparse choices list is not a single contiguous rodata array, so a third accepted value cannot be excluded byte-for-byte.

> **NOTE —** `_layout_transform_heuristic` (request) and `used_mcts_layout` (telemetry) are distinct. MCTS can be *requested* and still not *fire*: a trivial graph, a single real candidate after `add_no_change_plan`, or a failed design-space generation (`"Design space generation failed."`) all fall back to the static assignment, and `used_mcts_layout` records that it did not run.

### Considerations

This flag is read once, in the `tonga` backend `CodeGenFlow`, when the pipeline is assembled. When `mcts`, the autotuner is wrapped around the layout-transform pass (§5); otherwise the deterministic PAG pipeline runs with no search infrastructure instantiated. The `sunda` target ships its own `CodeGenFlow.so`; the layout middle-end documented here is the `tonga` backend.

---

## 2. The Data Model — PAG / PAGSet / DAG / UCC

### Purpose

The layout problem is a constrained assignment: many tensors' partition-axis choices are *coupled* (choosing the partition axis of one operand of a matmul constrains the other), and the goal is a global assignment minimizing transpose cost. `PAGLayoutFramework` represents this with four nested abstractions so the coupled problem can be decomposed into independent sub-problems.

### The four abstractions

| Term | What it is | Grounding (`PAGLayoutAnalysis.so`) |
|---|---|---|
| **PAG** | A *Partition-Axis Group*: a set of `(tensor, axis)` nodes that, if chosen as the partition dimension, must be partitioned **together**. | `axis2pag`, `buildPAGs`, `buildAndVisitPAGs`; docstring fragment `"build partition axes groups originating from src_p_node_set"` |
| **PAGSet** | A set of PAGs assigned to a DAG as that operator's partition-axis layout. The global layout is `dag2pagsets : Dict[DAG, Set[PAGSet]]`. | `build_pag_set_candidates`, `dag2pagsets`, `dag_to_chosen_pag_set` |
| **DAG** | A dataflow sub-graph (one op, or a fused op region) whose layout is **one** decision. | `dag2pagsets` keys; `chosen_par_axes_for_dag` |
| **UCC** | An *undecided connected component*: a maximal set of DAGs whose layouts are mutually coupled and not yet decided. The solver solves each UCC independently. | `undecided_cc`, `undecided_dag_set`; greedy docstring `"each dag in a undecided connected component (UCC) in PAGLayoutAnalysis"` |

> **NOTE — the acronym expansions are reconstructions, not strings.** The string table
> holds only the identifier forms `axis2pag` and `undecided_cc`; neither `"Partition Axes
> Group"` nor `"undecided connected component"` appears spelled out, so do not grep for
> them. Likewise, the owning class is `PAGLayoutFramework` — `PAGLayoutAnalysis` is the
> *module*, not a class.

### Construction order

The framework builds its structures in a fixed sequence (method symbols read from the `__pyx` table, in ordinal order within `PAGLayoutFramework`):

```text
build_initial_pags_and_pagsets        ── seed PAGs from each tensor/axis, initial PAGSet candidates
  └─ buildAndVisitPAGs / buildPAGs     ── group coupled (tensor,axis) nodes into PAGs
build_pag_set_candidates               ── per-DAG candidate PAGSets (the per-decision action set)
build_final_pags_and_pagsets           ── finalize after the search has chosen
calculate_final_par_axes               ── emit the chosen partition axes per DAG
```

`build_pag_set_candidates` is the call that produces the raw per-DAG candidate set the search (§4) and the autotuner (§5) consume.

---

## 3. The Partition-Axis Decision and Transpose Insertion

### Purpose

The core question: *which logical axis of each tensor maps to the 128 SBUF partitions?* For most ops this is a free choice ranked by cost; for matmul it is forced; and a wrong global choice forces a physical transpose, which is the dominant cost the search minimizes.

### Candidate partition axes

`PAGLayoutFramework` enumerates, per DAG, the legal partition-axis candidates and the chosen ones:

```text
all_par_axes_candidates       ── the legal partition-axis candidates for a DAG
all_chosen_par_axes           ── the chosen partition axes (global solution)
chosen_par_axes_for_dag       ── per-DAG chosen partition axes
check_par_candidates          ── filter / validate the candidate set
calculate_final_par_axes      ── emit the final per-DAG partition axes
```

Two legal corner cases are worth a reimplementer's attention (both are verbatim strings in `PAGLayoutAnalysis.so`):

```text
"Consider empty partition candidate only for indirect loadstore DAGs"
"DAG Flip: Set DAG to have no partition axes"
```

A DAG may legally have **no** partition axis at all — the empty partition candidate is only admitted for indirect load/store DAGs, and `should_flip_dag_layout` can flip a DAG to the no-partition state. The "guess" path for compute-light components lives in the legacy `GlobalLayoutOpt` (§6): `"Connected component %s has no matmult/reduce/batchnorm. Guessing layout. Considering putting on CPU."`

### Matmul (tensor-contract) layout

The matmul machinery is the constrained part. `PAGLayoutAnalysis.so` carries the operand-role identifiers and the swap peephole:

```text
contract_axes        ── the contraction axes (mapped to the partition dim on BOTH operands)
lhs_free / rhs_free  ── moving (lhs) vs stationary (rhs) free axes, tracked separately
consumer_lhs_axes / consumer_rhs_axes
allow_tc_swap        ── may the assigner swap lhs↔rhs operand roles?
has_swap_benefit     ── does swapping cut transpose count/size?
"adjusting rhs_free_axes after swap:"   ── verbatim log literal
can_bridge_with_pf_transpose            ── can a PE-array (partition-free) transpose reconcile a mismatch w/o DMA?
```

The rule set, reconstructed from the identifiers above:

```c
// Matmul partition-axis forcing + operand-swap peephole (PAGLayoutFramework)
function assign_matmul_layout(mm_dag):
    // 1. The contract axis must land on the partition dimension on BOTH operands,
    //    so the PE systolic array contracts along partitions.
    force_partition(mm_dag.lhs, mm_dag.contract_axes)        // contract_axes
    force_partition(mm_dag.rhs, mm_dag.contract_axes)

    // 2. lhs free axes (moving) and rhs free axes (stationary) are tracked separately;
    //    orientation decides which operand is loaded stationary into the PE array.
    record_free(mm_dag.lhs_free, mm_dag.rhs_free)

    // 3. After all layouts are determined, a cheap peephole: swap operand roles if
    //    that reduces the inserted transposes. NOT a re-search — a local fixup.
    if allow_tc_swap(mm_dag) and has_swap_benefit(mm_dag):    // allow_tc_swap / has_swap_benefit
        swap_lhs_rhs(mm_dag)
        adjust_rhs_free_axes_after_swap(mm_dag)              // "adjusting rhs_free_axes after swap:"
```

> **QUIRK —** `allow_tc_swap` is applied *after* the global layout is fixed, not during the search. It is a peephole: it checks only whether swapping the moving/stationary roles of an already-decided matmul cuts transposes. A reimplementation that folds operand-swap into the cost-driven search will explore a larger space than this binary does.

### Conflict → transpose insertion

When a producer's chosen layout disagrees with a consumer's requirement, a physical transpose is inserted. `PAGLayoutAnalysis.so` detects and prices the boundary:

```text
has_conflict / has_pp_to_bp_conflict    ── partition-partition vs broadcast-partition mismatch
enumerate_transposes_on_boundary        ── enumerate transposes on a producer/consumer edge
io_transposes_needed / estimate_IO_transpose_info_for_inst
IOTransposeInfoGenerator
is_legal_static_transpose_candidate
```

The actual op insertion is performed by separate passes, partitioned by where the transpose lives:

| Pass (`.so`) | Inserts transposes for |
|---|---|
| `tonga/passes/InsertIOTransposes` | I/O (entry/exit) boundaries |
| `tonga/passes/InsertLocalTransposes` | local on-chip tensors |
| `transforms/InsertOffloadedTransposes` | DMA/DRAM-offloaded transposes |
| `transforms/StaticTransposeLocalTensor`, `RoundtripTranspose`, `DramToDramTranspose` | static/roundtrip/DRAM-to-DRAM forms |
| `transforms/LowerTranspose` | lowers an abstract transpose to engine ops |
| `tonga/TongaTranspose` | the tonga transpose op model |

Transpose *kinds* (priced by the cost model, §4): PF-transpose (an identity-matmul on the PE array), stream/DVE transpose, and DMA transpose — each in IO / local / nonlocal variants.

---

## 4. The Search — `solve` (exhaustive / divide-and-conquer / greedy)

### Purpose

Given the per-UCC candidate PAGSets, pick the assignment minimizing total static cycle cost. The decisive (and counter-intuitive) fact: the module is named `PAGLayoutGreedySearchAlgorithm`, but `solve` is **not** greedy by default — greedy is only the large-UCC fallback.

### Entry point

```text
PAGLayoutGreedySearchAlgorithm.PAGLayoutGreedySearchAlgorithm.solve
  ├─ identify_sorted_ranges_and_candidates_for_ucc   ── prep candidate ranges for the UCC
  ├─ solution_space_size_for_ucc                     ── size of the per-UCC Cartesian product
  ├─ can_exhaustive_search_ucc                       ── is the space small enough to enumerate?
  ├─ exhaustive_search                               ── small: try every combination
  │    └─ _sequential_exhaustive_search / _finalize_exhaustive_search
  ├─ split_search                                    ── medium: divide-and-conquer over sub-UCCs
  └─ heuristic_based_search                          ── large: greedy locally-cheapest per DAG
```

### Algorithm

```c
// PAGLayoutGreedySearchAlgorithm.solve — tri-modal per-UCC layout search
// (method symbols all __pyx-confirmed on PAGLayoutGreedySearchAlgorithm class)
function solve(self):
    for ucc in self.undecided_dag_set:                       // undecided_dag_set — the UCCs
        self.identify_sorted_ranges_and_candidates_for_ucc(ucc)
        size = self.solution_space_size_for_ucc(ucc)         // |∏ candidate PAGSets|

        if self.can_exhaustive_search_ucc(ucc):              // size below threshold
            // Enumerate the full Cartesian product of per-DAG candidate PAGSets,
            // cost each with CycleBasedLayoutCostModel (§4.x), keep the minimum.
            self.exhaustive_search(ucc)                      // _sequential_exhaustive_search loop
            // log: "CHOSEN SOLUTION with min cost %s:"  with min_cost early-pruning:
            //      "min_cost: Optional current minimum cost for early pruning."
        else:
            // Too large to enumerate. Split the UCC into sub-UCCs, solve each, stitch.
            self.split_search(ucc)                           // Divide & Conquer
            // log: "[Divide & Conquer Search] Split UCC with {} nodes for analysis"
            //      "[Divide & Conquer UCC Search] Analysis on sub UCC with {} nodes"
            // FIXME (verbatim): "Split UCCs into sub UCCs when connectivity(UCC) > 1"
        // heuristic_based_search is the greedy fallback when even D&C is too large:
        //   sort candidates by static cost, pick locally cheapest per DAG.
```

The exhaustive arm carries an explicit early-prune: `min_cost` is threaded as the running minimum so a partial assignment already exceeding it is abandoned (`min_cost_solution`, and the docstring fragment *"min_cost: Optional current minimum cost for early pruning. If the cost ..."*). The cost is read through `dag_cost_handler` / `tc_dag_cost_handler` → `get_exec_cost_on_dag` / `get_exec_cost_on_tc_dag`, which delegate to `CycleBasedLayoutCostModel`.

> **QUIRK —** the class and module are named *GreedySearch*, yet the default behavior is *exhaustive when affordable*. The greedy `heuristic_based_search` is the last resort for UCCs whose Cartesian product is too large even to divide-and-conquer. A reimplementer who builds a pure greedy hill-climb will under-search small components and produce worse layouts than this binary on common (small-UCC) graphs.

> **NOTE — docstring survival is uneven across these modules.** The greedy module and the
> analysis module keep their docstrings (the matmul swap log and the empty-partition note
> below are quoted from them), but `CycleBasedLayoutCostModel` and the autotuner methods
> (`add_plan`, `add_no_change_plan`) are stripped down to identifiers and short format
> strings. Everything §5 and §6 say about those two rests on identifiers and format
> literals, so expect no prose to grep for there.

### Vectorization-driven post-merge: `AdditionalPAGSetAdder`

After the base assignment, a cost-guarded post-pass tries to *merge* PAG groups across neighboring nodes to enable vectorization — but only if it does not regress IO transpose cost. This is a nested class `PAGLayoutGreedySearchAlgorithm.AdditionalPAGSetAdder` (the Cython class-length prefix confirms nesting):

```c
// AdditionalPAGSetAdder.run — vectorization-driven PAGSet merge with IO-cost guard
function run(self):
    for node in graph:
        neighbors = self.identify_neighboring_nodes(node)            // identify_neighboring_nodes
        cands = self.build_candidate_vectorizable_pags_from_neighboring_nodes(node, neighbors)
        for cand in cands:
            if not self.is_vectorizable_pag(cand): continue          // is_vectorizable_pag
            base_io = self.get_io_transpose_cost(base_pag_group)     // base_pag_group_IO_cost
            merged_io = self.get_io_transpose_cost(merged_pag)       // merged_pag_IO_cost
            // Reject the merge if it raises IO transpose cost:
            if self.check_IO_cost_regression_pag_set_merge(base_io, merged_io):   // has_io_cost_regression
                continue
            self.evaluate_additional_pag_set_candidate(cand)         // accept the merge
```

The IO comparison is `check_IO_cost_regression_pag_set_merge_on_IO_loadstore`, and `split_pag_set_by_IO_consecutiveness` keeps merged PAG groups IO-consecutive so the merge does not silently introduce strided IO.

---

## 5. The Static Cost — `CycleBasedLayoutCostModel`

### Purpose

The score for every static layout decision. It estimates, in **cycles**, the cost of executing a DAG under a chosen access-layout, so a layout that forces transposes or strided DMAs costs more. No compile or run is needed — this is what makes the `none` path cheap. The whole layout search is effectively `MINIMIZE(Σ instruction cycles + Σ transpose cycles)`.

### Aggregation and axis categorization

```text
dag_cost / tc_dag_cost          ── per-DAG total (tc = tensor-contract DAG variant)
inst_cost / _inst_cost          ── per-instruction cost (the memoized core)
AggregateCostWithDetail         ── a (cost, breakdown) record; __str__ format literal "cost="
categorize_axes / has_free / must_be_block   ── classify each access axis as B / P / F
```

Each access axis of an instruction is categorized as **B** (block), **P** (partition), or **F** (free). The categorization drives the per-op latency: partition-resident axes are the systolic dimension, free axes are the streamed dimension, and the remainder are block-iterated. (The descriptive docstring fragments for this — *"all P's as F. The remaining dimensions are estimated as B"*, *"broadcast_par tripcount doesn't affect latency"*, *"when innermost dimension is block"*, *"1. model PE latency"* — were observed in the analysis-side prose; in this cost-model `.so` only the identifiers survive, the long docstrings are stripped.)

### The per-instruction cycle table

`_inst_cost` dispatches to a per-op-kind cost function. The full family (all `__pyx`-confirmed on `CycleBasedLayoutCostModel`):

| Category | Cost functions |
|---|---|
| Matmul / contract | `_inst_cost_matmul`, `inst_cost_tensor_contract` |
| Elementwise | `_inst_cost_unary`, `_inst_cost_binary`, `binary_ts_latency` (tensor-scalar), `binary_tt_latency` (tensor-tensor) |
| Reductions | `_inst_cost_reduce`, `_inst_cost_par_reduce_add`, `_inst_cost_pe_reduce_add`, `_inst_cost_cascaded_reduce_add` |
| Norm / softmax | `_inst_cost_bn`, `_inst_cost_rmsnorm`, `_inst_cost_softmax` (+ `_softmax_exp` / `_softmax_dx` / `_softmax_rsum`) |
| Load / store / DMA | `_inst_cost_loadstore`, `_inst_cost_indirect_loadstore`, `io_dma_cost`, `get_dma_latency` |
| Quantize / broadcast | `_inst_cost_quantize_mx`, `_inst_cost_broadcast_partition` |
| Index ops | `_inst_cost_index_value_op` |

> **NOTE —** `calculate_generic_access_cost` lives on the **greedy** class (`PAGLayoutGreedySearchAlgorithm.calculate_generic_access_cost`), not on the cost model. The exact numeric cycle constants inside each `_inst_cost_*` are not transcribed here; the per-op latency constants live in the separate `InstructionLatencyModel` / `KernelLatencyInfo` modules (out of scope for this page).

### The transpose price — why a bad layout is expensive

```text
transpose_cost / transpose_cost_from_tripcounts   ── base transpose price
io_transpose_cost / _io_transpose_cost            ── IO-boundary transpose
nonlocal_transpose_cost                           ── nonlocal (cross-region) transpose
get_stream_transpose_latency / get_stream_shuffle_latency   ── DVE/stream transpose
applyStaticTranspose
identify_strided_copy_cost_on_tensor_access       ── strided = layout-mismatch penalty
```

A layout that forces a producer→consumer relayout adds a transpose op whose cycles are added to the DAG cost; a strided copy from a partial mismatch is priced via `identify_strided_copy_cost_on_tensor_access`. Cost scales with the estimated **tiled tripcounts** (`_estimate_tiled_axis_tripcount`, `decayed_dma_free_axes_indices`): `cost ≈ tripcount × per-iter engine latency`.

### Legality and memoization

```text
invalid_layout_for_inst / invalid_layout_for_dag  ── prune layouts an inst cannot physically execute
cached_invalid_layout_for_dag                     ── memoized legality
_build_inst_cost_cache_key                        ── structural cache key for _inst_cost
```

Illegal layouts return an invalid/∞ marker so the search skips them. `_inst_cost` is memoized on a structural cache key: the same `(op-shape, layout)` is priced once. This matters because exhaustive search re-prices many overlapping combinations; without the cache, the Cartesian enumeration would re-cost identical sub-DAGs repeatedly. (A `"Disable structural caching in CycleBasedLayoutCostModel"` knob string exists to turn the cache off for debugging.)

---

## 6. The Autotuner Action Space (the `mcts` path)

### Purpose

The autotuner is **layout-agnostic** in its core: it benchmarks and tunes *any* per-pass decision during compilation. A layout decision is one instance. When `--layout-transform-heuristic=mcts`, each layout candidate is registered as a generic *plan*, and a sequential design-building search (MCTS) finds the best ordered list of plans, evaluated by real subprocess compiles.

### The "plan" — the atomic tunable decision

```text
add_plan            ── self.plans.append(plan); a plan = one candidate decision object
add_plans           ── extend with an iterable of plans
add_no_change_plan  ── append the IDENTITY (no-change) plan — always present
```

A plan's identity is `plan.autotune_id` (a uuid), or — if absent — its index in the plan list. For layout, a plan = one concrete candidate PAGSet/partition-axis decision for a DAG. The guards `"autotuner already has plans"` and `"Mixing automatic and manual plan ids is not supported."` enforce that a tuning point uses all-uuid or all-index plans, never both.

> **NOTE —** `add_no_change_plan` injects the identity action at *every* tuning point. This guarantees the baseline (current layout) is always reachable by the search — MCTS can always choose "leave it alone." A reimplementation that omits the no-change arm can produce a *worse* result than the unsearched baseline.

### The "design" — an ordered list of plan ids

```text
_generate_plan_dict      ── plan_dict = { p.autotune_id : p for p in self.plans }
                            asserts each id present ("autotune_id cannot be None")
_record_plan_to_design   ── self.design.append(<plan_id>)   (append the chosen id)
_fetch_plan_from_design  ── REPLAY: plan = plan_dict[ self.design[tuning_depth] ]
```

A design is a *path* through the sequence of tuning points: `design[i]` is the chosen plan id at the i-th decision, and `tuning_depth` is the running cursor. This is exactly what resolves the open question of how an MCTS "plan id" maps to a concrete transform: **a plan id is an `autotune_id` key into the per-decision `plan_dict`, and the concrete transform is `plan_dict[plan_id]`** — the candidate object the layout pass enqueued via `add_plan`. `takeAction` in the MCTS state is `design + [plan_id]`.

### The six interchangeable search backends

`autotune/_Search.so` defines a `Search` base and six concrete strategies; `Search.build` is a factory that matches `search_name` against each subclass `__name__`:

| Backend | Strategy |
|---|---|
| `ExhaustiveSearch` | every design (full Cartesian product) |
| `ExhaustiveSequentialSearch` | exhaustive, decision-by-decision (`_do_search`) |
| `DownsampledSequentialSearch` | sequential with random candidate sub-sampling |
| `GreedySearch` | hill-climb (`_find_nn` = next-best neighbor) |
| `RandomSearch` | sample random designs |
| `MonteCarloTreeSearch` | UCT tree search — what `--layout-transform-heuristic=mcts` selects |

Shared helpers `_find_best` / `_find_best_with_sp_output` pick the best-metric design from a batch of subprocess-compile outputs. (Log: `"Autotuner search algorithm will be "` + `search_name`.)

> **QUIRK —** MCTS is just one of six interchangeable explorers over the *same* plan/design action space. The action space — the set of layout candidate plans per tuning point — is defined entirely by the layout passes' `add_plan` calls, independent of which search drives it.

### The two reward backends — `PerformanceMetric`

`autotune/_PerformanceMetric.so` builds one of two concrete metrics (factory `PerformanceMetric.build`):

| Metric | Reward source |
|---|---|
| `PostSchedMetric._evaluate` | **Default.** Compiles the candidate design in a child process and reads the scheduler's post-schedule estimated latency via attribute `nc_latency`. A *compile*, not a hardware run. |
| `NeuronBenchMetric._evaluate_on_remote` | Real on-hardware benchmarking on `remote_benchmark_machines` (`verify_machine_baselines`). Used only when real benchmark hosts are configured. |

Evaluation API consumed by the MCTS driver: `evaluate_with_subprocess_result(design) → (metric, sp_result)`, `evaluate_batch(designs)` (parallel, `future_task` per design). Results persist to `/global_metric_store.json` for cross-process memoization so re-seen designs are not recompiled.

> **GOTCHA —** the empty-machine path emits `"A PerformanceMetric which uses remote benchmarking machines was used, but the 'remote_benchmark_machines' setting was empty."` — configuring `NeuronBenchMetric` without hosts is a silent mis-config that the default `PostSchedMetric` avoids. The default MCTS reward is therefore still a *child-process compile*, not a hardware measurement.

### The autotuner class hierarchy

`_AutotunerBase` (carries `_fetch_plan_from_design` / `_record_plan_to_design` / `_generate_plan_dict`) is subclassed by the dispatch classes that run at each stage of a tuning session:

| Class | Role (docstrings where preserved) |
|---|---|
| `_MainAutotuner` | `"Main autotuner class, which is used by the compiler in the original process."` — `_generate_design_space`, `_do_tuning` |
| `_MCTSHelperAutotuner` | per-decision hook handing current candidate plans to the MCTS tree |
| `_SubprocessAutotuner` | `"Subprocess used to compile a particular design"` (`internal-autotune-subprocess`) |
| `_DesignSpaceGenerationAutotuner` | `"Subprocess used to generate the design space."` (`internal-autotune-extraction-process`; writes `generated_design_space_raw.txt`) |
| `_SequentialHelperAutotuner` | sequential-search helper |
| `_DesignedAutotuner` | replay a fixed design (re-apply `best_design`) |
| `_BaselineAutotuner` | `"...overrides the Autotuning behavior to just run the baseline."` |
| `_BypassedAutotuner` | pass-through, autotuning disabled for this pass |

A layout tuning session: `_DesignSpaceGenerationAutotuner` enumerates plans in an extraction subprocess → the design space is searched (MCTS via `_MCTSHelperAutotuner`, each design compiled by a `_SubprocessAutotuner`) → the winning design is replayed by `_DesignedAutotuner` to produce the final BIR. Plans cross process boundaries serialized by `autotune_id` (`_generate_serialized_plans`, `_use_serialized_plans`).

---

## 7. The Two MCTS Layers — a critical disambiguation

There are **two distinct Monte-Carlo tree searches over layout** in this build. Both subclass the generic engine `penguin/MCTS.so` `State` (`enumerateActions`, `takeAction`, `getReward`, `isLeaf`, `estimateReward`, `chooseBestChild`, `rollout`, `executeRound`, `backpropogate` — all `__pyx`-confirmed). Confusing them is the main trap.

### #1 — In-process layout MCTS: `LayoutDecisionTree.LayoutState`

`targets/tonga/LayoutDecisionTree.so`. Docstrings survive verbatim: *"LayoutDecisionTree - Implement the state and decisions of the decision tree for ..."* and *"encodes the necessary information for a state in MCTS, including the layout groups and ..."*. It is a `State` subclass:

```text
LayoutState.build_action      ── action = merge the next frontier DAG into a layout group (UnionGroup)
LayoutState.get_action_cost   ── cost over the frontier DAG = all_transpose_costs / break_tie_cost
LayoutState.isLeaf            ── terminal when len(layout_grps)==0 and num_pending_dags==0
LayoutState.find_initial_ccs / merge_initial_layout_grps / cur_frontier_dags
LayoutState.find_best_global_layout / get_final_layout_grp / build_signature
```

Its reward is the **static** transpose cost (`all_transpose_costs`, `break_tie_cost`) — this MCTS does **not** compile in a subprocess. Supporting classes: `LayoutGroup` (a set of DAGs sharing a layout — `collectTransposes`, `enumerate_transposes`, `fpi_within_layout_grp` = fixed-point layout propagation within a group, `mergeDisjointGroups`) and `UnionGroup` (`candidate_priority`, `break_tie_cost`, `evaluate_candidate_group`, `filter_candidates`).

This is the **legacy** tonga global layout search, driven by `GlobalLayoutOpt` (`LayoutAnalysis.buildLayoutState` / `buildLayoutStateFromGuessing`), which carries the verbatim NOTE:

```text
"NOTE: this pass is deprecated in favor of the super pass PAGLayoutOpt"
```

The search is selected by the config key `/Tensorizer/LayoutSearchAlgorithm`.

### #2 — Autotuner layout MCTS: `_TreeSearch.MCTSTuneState`

`autotune/_TreeSearch.so`. The `State` subclass whose action = appending a plan id to a design, and whose reward = `ctx.reward_f(PostSchedEstLatency)` from a real subprocess compile (`PerformanceMetric`, §6). This is the `--layout-transform-heuristic=mcts` autotuned path.

### Relationship

`#1` is the deterministic/heuristic in-process layout decision (static cost; legacy). `#2` is the optional, expensive, compile-in-the-loop autotuner layered on via the CLI flag. **The default `-O2` compile uses neither MCTS for real:** the current default is the PAG static greedy/exhaustive search (§4), because `PAGLayoutOpt` is the "super pass" that supersedes `GlobalLayoutOpt`. MCTS `#1` fires only when the deprecated `GlobalLayoutOpt` path / `LayoutSearchAlgorithm=mcts` internal setting is selected; MCTS `#2` fires only on `--layout-transform-heuristic=mcts`.

> **GOTCHA —** "MCTS for layout" is ambiguous in this binary. A reader who finds `MCTS.so` and a `LayoutState` and concludes the default layout pass is Monte-Carlo is wrong on two counts: that `LayoutState` MCTS is the *legacy* path, and even when an MCTS *is* requested via the CLI, it is the autotuner's `MCTSTuneState`, a different `State` subclass with a different reward (real compile vs. static cost).

---

## 8. The Layout-Candidate Enumerator — the raw action set

Whichever search runs, the per-DAG candidate layouts come from `targets/tonga/LayoutCandidateEnumerator.so`:

```text
enumerate_dag_layouts(dag)                  ── the candidate layouts for one DAG (a cross product of:)
enumerate_legal_partition_axes_subsets(...) ── legal subsets of axes that can be the partition dim
    └─ yield_p_subsets / yield_p_subsets_helper   ── generate the P-subsets
enumerate_free_axes(...)                    ── legal free-axis orderings/packings
enumerate_par_const_dims_candidates(...)    ── partition candidates from constant/broadcast dims
are_tilable_axes_for_dve_transpose_insts    ── whether a DVE (stream) transpose can serve a layout
analyze_complexity / bottleneck_analysis    ── pruning/ranking so the action space stays tractable
```

The action space, for **any** search, is:

```text
ACTION_SPACE(dag) = { (partition-axes-subset, free-axes-arrangement) legal for dag }  ∪  {no-change}
                  = enumerate_legal_partition_axes_subsets(dag)  ×  enumerate_free_axes(dag)
                    (+ enumerate_par_const_dims_candidates)  (+ add_no_change_plan)
```

`analyze_complexity` / `bottleneck_analysis` prune the raw Cartesian set so it stays tractable. The default greedy/exhaustive `solve` (§4) enumerates this directly; the autotuner (§6) wraps each surviving candidate as a plan.

---

## 9. The Boundary — what each path produces, and how the layout is applied

Both paths produce the **same artifact**: a partition/free-axis assignment per DAG plus a set of inserted transpose ops. They differ only in *how* the assignment is chosen.

### `--layout-transform-heuristic=none` (default)

```text
CodeGenFlow → GlobalLayoutOpt/PAGLayoutOpt
  → PAGLayoutAnalysis.PAGLayoutFramework  builds PAG / PAGSet / DAG / UCC
  → PAGLayoutGreedySearchAlgorithm.solve  picks per-UCC layouts MINIMIZING the static
        CycleBasedLayoutCostModel cycle cost: exhaustive (small UCC) / divide-&-conquer
        (medium) / greedy (large)
  → AdditionalPAGSetAdder                 cost-guarded vectorization merge
  → TransformLayout                       materializes the chosen layout
  → InsertIOTransposes / InsertLocalTransposes / InsertOffloadedTransposes
```

One static pass; **no** subprocess compiles.

### `--layout-transform-heuristic=mcts`

```text
_DesignSpaceGenerationAutotuner  enumerates LayoutCandidateEnumerator candidates → plans
  → Search.build("MonteCarloTreeSearch")  constructs the MCTS driver (#2)
  → MCTS sequentially builds a design (one layout plan per tuning point); each candidate
       design EVALUATED BY A REAL SUBPROCESS COMPILE via PerformanceMetric (PostSchedEstLatency)
  → run() returns (best_design, best_metric)
  → _DesignedAutotuner replays best_design through _fetch_plan_from_design
  → TransformLayout applies exactly the winning sequence of layout plans
  → "used_mcts_layout" telemetry set iff MCTS actually fired
```

### The apply pass — `TransformLayout`

`tonga/passes/TransformLayout.so` is the materializer for either path. Its methods are `calculateTargetLayout`, `annotatePartitionAxes`, `buildTranspose`, `buildOutputTranspose`, `calculateIOTranspose`, `calculateTranspose`, `createIntermediateTensor`, `build_dst_indices`. It rewrites each DAG to the chosen partition/free axes and emits the implied transpose(s).

---

## Related Components

| Component | Relationship |
|---|---|
| `PAGLayoutAnalysis.PAGLayoutFramework` | Stage A — builds PAG/PAGSet/UCC search structures |
| `PAGLayoutGreedySearchAlgorithm` | Stage B — the tri-modal `solve` over UCCs |
| `CycleBasedLayoutCostModel` | the static cycle cost both stages minimize |
| `LayoutCandidateEnumerator` | the per-DAG raw candidate set for any search |
| `LayoutDecisionTree.LayoutState` | legacy in-process layout MCTS (static cost) |
| `autotune/{Autotuner,_Search,_TreeSearch,_PerformanceMetric,MCTS}` | the optional `mcts` autotuner stack |
| `TransformLayout` | materializes the chosen layout + transposes |
| `Insert{IO,Local,Offloaded}Transposes`, `LowerTranspose`, `TongaTranspose` | physical transpose insertion/lowering |

## Cross-References

- [Layout & Tiling Pipeline](layout-tiling-pipeline.md) — `LayoutTilingPipeline` assembles {layout assign → tiling}; layout (this page) precedes tiling
- [HLO Layout Passes](../hlo-opt/layout-passes.md) — the XLA HLO layout normalization runs in hlo-opt *before* Penguin; a different binary and IR layer, disjoint from this tonga middle-end
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — the 128-partition SBUF the partition-axis decision targets
- [Front Pipeline](../front/pipeline.md) — where the layout middle-end sits in the overall compile flow
- [Worked Example: Matmul](../front/worked-example-matmul.md) — the contract→partition forcing in a concrete matmul lowering
