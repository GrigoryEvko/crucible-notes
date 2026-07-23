# Tritium Fusion: Plan Generator, Beam Search, and Roofline Cost

> *All symbols, strings, addresses, and float constants on this page apply to `neuronx_cc` **2.24.5133.0+58f8de22**, cp310 wheel. The Tritium fusion modules are Cython extensions built with `-g` (full DWARF, **not** stripped), so the `__pyx_*` symbol table embeds the original `Class.method` names verbatim and the `.rodata` string table preserves identifiers, the class docstring, format literals, and float constants byte-exact. The cp311/cp312 wheels export only `PyInit`/dynsym (stripped) but compile from identical Cython source, so cp310 facts hold for all three. Evidence is the IDA sidecar set under `neuronx-cc/ida/…TritiumFusionBase.cpyt_ceb5e7fc5397bd32/` (functions, strings, decompiled C ctree) and `…__TreeSearchV_ffdf9ae8c89948e7/`; predicate bodies are reconstructed from the recovered name set + attribute-access op sequence + docstrings, never read line-for-line from a source tree.*

## Abstract

A single tensor-contract (matmul) loop-nest can have a producer fused into it in many shapes. **Tritium** is the penguin autotuner component that enumerates that shape space, scores each candidate with a *coefficient-free roofline model*, and ranks them with a *relative-tolerance beam search* — all as a static analytic pass, before any layout is chosen and without ever compiling a candidate. It sits one altitude below the layout/MCTS autotuner described in 8.50 (`penguin-autotuner`) and 8.51 (`autotuner-orchestration`): those search over whole designs by *measured* latency, while Tritium answers the inner question "given this matmul, what is the best way to fuse its producer?" purely from geometry and a per-arch latency oracle.

The whole mechanism is three small things bolted onto one struct, `FusionPlan`:

1. **A 2³+2 = 10-plan generator.** Three binary toggles — fuse-lhs? · fuse-rhs? · fuse-contract? — give 8 corners of a cube; the two corners that fuse *both* operands each spawn a second plan for the "which operand is reloaded" choice (`lhs_on_top`), giving 10. A `noop_plan` baseline makes 11. Enumerated by `enumerate_fusion_plans` (`0x161240`), deduplicated into an `OrderedSet`.
2. **A 3-stage lexicographic beam, width = an rtol band, not an integer k.** `beam_search_best_plan` (`0xb6230`) prunes the survivor set three times — minimise spill, then compute-roofline II, then DMA-roofline II — each via `beam_search_min` (`0x1403b0`), which keeps every plan whose metric is within a relative tolerance `rtol = 0.01` (1%) of the stage minimum. There is no top-k anywhere in the module.
3. **A coefficient-free cost model.** The roofline (`is_compute_bound`/`is_memory_bound`, `arithmetic_intensity`) carries **no** hard-coded peak-FLOPs, bandwidth, or capacity numbers — the module's *entire* double-precision constant pool is `{1.0, 0.01, 0.25, -1.0, 1/1024}`. Every cycle, byte, and capacity figure is read at runtime from the hardware `target` descriptor (the same per-arch latency oracle the backend PerfSim consumes, see [`perfsim-cost-model`](perfsim-cost-model.md)).

A separate observability module, `_TreeSearchVisualizer`, renders the *outer* MCTS search tree (not Tritium's beam) to a Graphviz `.dot` with a red→green reward-coloured gradient; it is documented in §6 because it is the other half of Strand-L's debug surface.

| | |
|---|---|
| **Plan generator** | `FusionPlanGenerator.enumerate_fusion_plans` @ `0x161240` (`TritiumFusionBase.py`) |
| **The 10 plans** | `plan1` `0xec9c0` · `plan2` `0xed8f0` · `plan3` `0xd5c30` · `plan4` `0xd6b40` · `plan5` `0xd4d10` · `plan6` `0xd3df0` · `plan7` `0xdb7e0` · `plan8` `0xdc760` · `plan9` `0xdd6c0` · `plan10` `0xfb240` · `noop_plan` `0xb8e90` |
| **Plan factory** | `FusionPlan.build` (classmethod) @ `0x122740` → `FusionPlan.__init__` @ `0x171c90` |
| **Beam orchestrator** | `FusionPlanGenerator.beam_search_best_plan` @ `0xb6230` (src 1171–1230) |
| **Beam survivor filter** | `FusionPlanGenerator.beam_search_min` @ `0x1403b0` (src 1164–1168) |
| **Roofline predicate** | `FusionPlan.is_compute_bound` @ `0x94b20` / `is_memory_bound` @ `0x93cc0` |
| **Arithmetic intensity** | `FusionPlan.arithmetic_intensity` @ `0xa5680` |
| **The only tunable scalar** | `rtol = 0.01` (`__pyx_float_0_01`, IEEE-754 `3f847ae147ae147b` @ `.rodata` file-off `0x1b150`) |
| **Tree visualizer** | `_TreeSearchVisualizer.get_color` @ `0x8790` / `writeGraph` pf @ `0x9900` |

---

## 1. The axis taxonomy and the 2³+2 = 10 plan space

Tritium models a single tensor contract as the Einsum the class docstring spells out (recovered **verbatim** from `.rodata`; the lines below are byte-identical to the string table):

```text
    Tensor contract can be written into the following Einsum expression
    O[b, i, j] += L[b, i, k] * R[b , j, k]

    The fusion plans consider four types of axes:
    1. batch_axes(b): Axes that are common to both LHS and RHS and are not contract axes.
    2. contract_axes(k): Axes that are common to both LHS and RHS and are used for contraction (reduction).
    3. lhs_free_axes(i): Axes that are unique to the LHS operand.
    4. rhs_free_axes(j): Axes that are unique to the RHS operand.
    ...
    therefore, there are 2^3 = 8 fusion options to choose whether fuse the
    lhs/rhs/contract axes.
    ...
    The operand fused on bottom (rhs R in the above case), will be reload for 4 times
    ...
    2^3 + 2 = 10 plans in total.
    - `*_wosl`: Filtered to avoid splitting LHS
    - `*_wos`: Intersection of `*_wosl` and `*_wosr` (avoids splitting either)
```

Every quoted line is present in the strings sidecar; the floats `0.01` and `0.25` sit adjacent at `.rodata` file-offset `0x1b150` (`7b14ae47e17a843f 0000000000d03f`), the IEEE-754 little-endian encodings of `0.01` and `0.25`.

### The four axes and the three free bits

`batch_axes(b)` are common to both operands and *always helpful to fuse*, so they are **not** one of the free decisions — they are passed to every non-noop plan. That leaves three binary toggles:

| bit | controls | "fused" means |
|---|---|---|
| fuse-lhs? | `lhs_free_axes(i)` | the LHS-unique loop is brought inside the matmul nest |
| fuse-rhs? | `rhs_free_axes(j)` | the RHS-unique loop is brought inside the matmul nest |
| fuse-contract? | `contract_axes(k)` | the reduction loop is fused (split into the accumulation group) |

Three bits ⇒ 2³ = 8 plans, corroborated by the per-plan census in §1.1.

### Why exactly +2

When *both* `lhs` and `rhs` are fused, there are two producers in the nest, and a second decision appears: which producer is **stationary (on top, read once)** and which is **reloaded (on bottom, re-read once per top iteration)**. The docstring illustrates the cost with a 3×3 nest where the bottom operand is reloaded; the `lhs_on_top` boolean carries the choice. This decision *only exists* when both operands are fused, so precisely the two cube corners with (fuse-lhs ∧ fuse-rhs) get a second plan — hence **+2, not +4 or +8**. `FusionPlan.rhs_on_top` (`0x167aa0`) has body `return not self.lhs_on_top`, the exact complement.

### 1.1 The plan↔corner map (byte-grounded)

Each `planN()` calls `FusionPlan.build(id, n, batch_axes=…, lhs_free_axes=…, rhs_free_axes=…, contract_axes=…, lhs_on_top=…)`, passing either the **real** axis set (→ that axis fused) or an empty `OrderedSet` (→ not fused). The selection per plan is read from a getter census over each decompiled body — counting how many times each axis getter is wired into the `build()` call (the "19×" count is the unfiltered + `wosl` + `wosr` + `wos` GetAttr expansion of one logical reference; `empty_set` marks an unfused slot):

| plan | addr | batch | lhs (i) | rhs (j) | contract (k) | cube corner | census evidence |
|---|---|---|---|---|---|---|---|
| `plan1` | `0xec9c0` | ✓ | ✓ | ✓ | ✓ | (1,1,1), `lhs_on_top=True` | lhs×19, rhs×19, contract×2, batch×2 |
| `plan2` | `0xed8f0` | ✓ | ✓ | ✓ | ✓ | (1,1,1) **reload twin** | **identical** census to plan1 |
| `plan3` | `0xd5c30` | ✓ | ✓ | ✓ | ∅ | (1,1,0), `lhs_on_top=True` | lhs×19, rhs×19, **2×empty_set** |
| `plan4` | `0xd6b40` | ✓ | ✓ | ✓ | ∅ | (1,1,0) **reload twin** | identical census to plan3 |
| `plan5` | `0xd4d10` | ✓ | ✓ | ∅ | ✓ | (1,0,1) | lhs×19, contract×19, **2×empty_set** (rhs) |
| `plan6` | `0xd3df0` | ✓ | ∅ | ✓ | ✓ | (0,1,1) | rhs+contract real, lhs empty |
| `plan7` | `0xdb7e0` | ✓ | ✓ | ∅ | ∅ | (1,0,0) | lhs only |
| `plan8` | `0xdc760` | ✓ | ∅ | ✓ | ∅ | (0,1,0) | rhs only |
| `plan9` | `0xdd6c0` | ✓ | ∅ | ∅ | ✓ | (0,0,1) | contract only |
| `plan10` | `0xfb240` | ✓ | ∅ | ∅ | ∅ | (0,0,0) batch-only | batch×19, **6×empty_set** |
| `noop_plan` | `0xb8e90` | ∅ | ∅ | ∅ | ∅ | *not in the cube* | all empty — no fusion at all |

The four pivotal census facts are read directly from the decompiled bodies: `plan1` and `plan2` carry a **byte-identical** axis-getter census (batch×2, contract×2, lhs×19, rhs×19, one `build`), so the *only* difference between the pair is the `lhs_on_top` literal — the reload twin. `plan3`/`plan4` likewise share a census (lhs×19, rhs×19, contract = 2×`empty_set`). `plan5` shows lhs+contract real with rhs = 2×`empty_set`. `plan10` shows batch×19 + 6×`empty_set`. The exact `True`/`False` byte of `lhs_on_top` inside plan2/plan4 is obscured by Cython tuple-packing — the plan bodies reference both `Py_True` and `Py_False` const objects — so that literal is HIGH rather than byte-firm. The symmetric body structure plus the docstring fix the semantics unambiguously: the twin flips `lhs_on_top`.

> Count check: 8 cube corners (`plan1,3,5,6,7,8,9,10`) + 2 reload twins (`plan2`, `plan4`) = **10**. `noop_plan` is the 11th, separate "don't fuse the producer at all" baseline — it is *not* part of the 2³+2 lattice (`is_noop` @ `0x92ea0` = all four fused sets empty).

### 1.2 The `*_wos`/`*_wosl`/`*_wosr` filtered axis views

Each axis set has three filtered variants the generator precomputes once: `*_wosl` ("**w**ith**o**ut **s**plitting **l**hs"), `*_wosr` (rhs), `*_wos` (the intersection — splits neither). They are produced by the getters `batch_axes_wos/wosl/wosr` (`0xbdfc0`/`0xbe830`/`0xbe350`) and siblings, which call `filter_split_axes` (`0x15dc50`) / `is_split_axis` (`0x132ac0`). The qualified names `FusionPlanGenerator.{batch,contract,lhs_free,rhs_free}_axes_{wos,wosl,wosr}` are all present in the string table, and the docstring lines describing them are verbatim. These let a chosen fusion *avoid splitting an axis that would break a matmul accumulation group* — selected by the `dont_split_acc_grp` flag (§2).

---

## 2. `enumerate_fusion_plans` — the 10-plan loop

```c
// FusionPlanGenerator.enumerate_fusion_plans(self, dont_split_acc_grp)  @0x161240
// pyargnames {self, dont_split_acc_grp}; 31 in-body refs to dont_split_acc_grp.
// add_plan inner closure @0x81fb0 / 0x825d0 ; generate_tuple @0x107e50.
static PyObject *enumerate_fusion_plans(Self *self, PyObject *dont_split_acc_grp) {
    // 1. Precompute the filtered axis variants once (GetAttr sequence):
    //    batch/contract/lhs_free/rhs_free  ×  {_wosl, _wosr, _wos}      (§1.2)
    OrderedSet *result = OrderedSet_New();

    // 2. add_plan: dedup by the plan's canonical fusion-set identity tuple.
    //    add_plan body references exactly {generate_tuple, add};
    //    generate_tuple references {axis_contract_factor, get_fusion_set_id}.
    void add_plan(FusionPlan *p) {
        PyObject *key = FusionPlan_generate_tuple(p);  // (get_fusion_set_id, axis_contract_factor)
        OrderedSet_add(result, p);                     // OrderedSet drops duplicate fusion-set ids
    }

    add_plan(noop_plan(self));                         // baseline first  (noop_plan x1)

    // 3. Attempt each of the 10 plans. Census: plan1..plan10 each appear TWICE
    //    in this body — once over the unfiltered axis set, once over a _wos-filtered
    //    variant; dont_split_acc_grp selects which path (dual GetAttr per plan).
    add_plan(plan1(self,  ..., dont_split_acc_grp));   // (1,1,1)  lhs_on_top=True
    add_plan(plan2(self,  ..., dont_split_acc_grp));   // (1,1,1)  reload twin
    add_plan(plan3(self,  ..., dont_split_acc_grp));   // (1,1,0)  lhs_on_top=True
    add_plan(plan4(self,  ..., dont_split_acc_grp));   // (1,1,0)  reload twin
    add_plan(plan5(self,  ..., dont_split_acc_grp));   // (1,0,1)
    add_plan(plan6(self,  ..., dont_split_acc_grp));   // (0,1,1)
    add_plan(plan7(self,  ..., dont_split_acc_grp));   // (1,0,0)
    add_plan(plan8(self,  ..., dont_split_acc_grp));   // (0,1,0)
    add_plan(plan9(self,  ..., dont_split_acc_grp));   // (0,0,1)
    add_plan(plan10(self, ..., dont_split_acc_grp));   // (0,0,0)  batch-only

    return (PyObject *)result;   // a DEDUPLICATED OrderedSet of <=10 (+noop) FusionPlan objects
}
```

**Net effect.** Degenerate matmuls collapse several corners to the same fusion-set id (e.g. no contract axes makes the fuse-contract bit a no-op), so the `OrderedSet` may hold **fewer than 10** distinct plans — the generator over-enumerates and the dedup prunes. The enumerate census shows `noop_plan`×1, each `plan1`..`plan10`×2, `generate_tuple`×1, `dont_split_acc_grp`×31.

`dont_split_acc_grp` is the bool that, when true, routes plan construction through the `*_wos*` filtered axis sets so a chosen fusion never splits an axis that would break a matmul accumulation group; when false the unfiltered sets are used. The flag name and the dual GetAttr paths per plan carry this; the precise branch test is one Cython `if` on the kwarg, so the routing is HIGH rather than read line-for-line.

### Pipeline placement

```text
build()  ->  fusion_plan_search_preprocessing  (0x181910)
         ->  enumerate_fusion_plans(dont_split_acc_grp)        [THIS PAGE §1-2]
         ->  best_fusion_plan_search (0xb5720)                 [§3]
         ->  beam_search_best_plan -> beam_search_min          [§3-4]
```

`generate_fusion_plans_for_search` (`0x188a00`) is the wrapper that calls preprocessing then `enumerate_fusion_plans`; `best_fusion_plan` (`0x123960`) is the top entry that chains generation → search. The subclass `AutotuneFusionPlanGenerator` (in the sibling `TritiumFusion.so`, which imports `TritiumFusionBase`) adds trip-count-multiplied variants on top of the 10 — `generate_fusion_plans_for_search_more_trip`, `try_more_tripcounts`, `try_smart_tripcounts`, `force_tripcount` — gated by the `cl::opt` string `internal-autotune-tritium-use-more-tripcounts` ("use more tritium" …). All five identifiers and the `cl::opt` string are present in the subclass string table.

> **GOTCHA — `enumerate_basic_fusion_plans` and `compute_bound_plans` are strings, not functions.** Both appear in the string table but neither is a compiled `__pyx_pf` body. The sole `.text` consumer of `enumerate_basic_fusion_plans` is the diagnostic `dump_plans` (`0xb3730`); `compute_bound_plans` has no `.text` consumer at all. The live enumeration method is `enumerate_fusion_plans` — singular, "basic" dropped — and compute/memory-bound tagging is the per-plan predicate pair (§4).

---

## 3. `best_fusion_plan_search` — feasibility filter → beam

```c
// FusionPlanGenerator.best_fusion_plan_search(self, plans)  @0xb5720  (src 1237-1241)
static PyObject *best_fusion_plan_search(Self *self, PyObject *plans) {
    // Drop SB-overflow plans: keep p iff NOT p.overflow_sb.
    // overflow_sb (0x95980): p.memory_pressure >= matmult.sb_par_size_in_bytes  (Py_GE @0x95ada)
    PyObject *feasible = filter(plans, lambda p: !p->overflow_sb);   // GetAttr "overflow_sb"
    if (PyObject_Size(feasible))                                     // any feasible plan?
        return beam_search_best_plan(self, feasible);               // §4
    else
        return noop_plan(self);                                     // nothing fits on-chip -> no fusion
}
```

So the "full search" path is: filter the plans that fit in SBUF, then run the 3-stage roofline beam; if **nothing** fits on-chip, fall back to `noop_plan`. The `overflow_sb` GetAttr and the size short-circuit are byte-pinned; the surrounding control flow is reconstructed from the line table, so it is HIGH.

---

## 4. The rtol-band beam search

### 4.1 `beam_search_min` — the per-stage survivor filter

```c
// FusionPlanGenerator.beam_search_min(self, plans, metric_func, rtol, max_metric=0)
//   @0x1403b0  (src 1164-1168).  arg-parse: {self, plans, metric_func, rtol, max_metric};
//   max_metric default = __pyx_int_0 (0).
static PyObject *beam_search_min(Self *self, PyObject *plans,
                                 PyObject *metric_func, PyObject *rtol, PyObject *max_metric) {
    // 1165  metrics = [(p, metric_func(p)) for p in plans]
    // 1166  best   = min(metrics, key=itemgetter(1))     // itemgetter(1) -> the METRIC (2nd elt)
    //                                                     // _pyx_builtin_min + itemgetter
    // 1167  thresh = best_metric * (1.0 + rtol)          // PyNumber_Multiply  (the band)
    // 1168  return [p for (p, m) in metrics if m < thresh]   // Py_LT filter
    ...
}
```

The decompiled body of `0x1403b0` references the `rtol` argument, builds the `(plan, metric)` list, calls the builtin `min` with `operator.itemgetter`, executes a `PyNumber_Multiply` (the `(1.0 + rtol)` band) and a `PyObject_RichCompare` (the `m < thresh` filter), under source line 1164. The `1.0` operand is the module's `__pyx_float_1_0`.

**The beam "width" is a relative-tolerance band, not an integer k.** `beam_search_min` returns *every* plan whose metric is strictly within `(1 + rtol)` of the stage minimum — the effective width is data-dependent (1..N), bounded only by how many plans tie within the tolerance. An exhaustive sweep of the module symbol and string tables finds **zero** hits for `beam_width`, `nsmallest`, `heapq`, `top_k`, or `argsort` — there is no top-k anywhere. `rtol = 0.01` (1%) is the IEEE-754 double `3f847ae147ae147b` at `.rodata` file-offset `0x1b150` (`__pyx_float_0_01`).

> **GOTCHA — this is not "keep top-k by `memory_pressure`".** Both halves of that reading fail: there is no *k* (the survivor set is the rtol band), and the metric is not `memory_pressure`. The metrics are, in priority order, `additional_spill_size_in_bytes` → `max_total_kernel_ii` → `normalized_kernel_dma_ii` (§4.2). `memory_pressure` feeds a *separate* gate, `has_low_memory_pressure` (§5).

### 4.2 `beam_search_best_plan` — the 3-stage lexicographic roofline beam

```c
// FusionPlanGenerator.beam_search_best_plan(self, plans)  @0xb6230  (src 1171-1230)
static PyObject *beam_search_best_plan(Self *self, PyObject *plans) {

    // STAGE 1 - minimise SPILL (exact-min band: max_metric=0, rtol absent -> keep strict argmin set)
    plans = beam_search_min(self, plans,
                attrgetter("additional_spill_size_in_bytes"),   // tuple__101
                /*max_metric=*/0);
    if (PyObject_Size(plans) == 1) return plans[0];             // short-circuit on a unique winner

    // (branch on self.matmult.attention_score_access(): if a score tensor exists,
    //  an attention-tensor sub-ordering by lambda19 (max_cc_buffer_store_dma_stride) /
    //  lambda20 (fuse_as_top_access) is layered in; else skip straight to stages 2/3.)

    // STAGE 2 - minimise the COMPUTE roofline II (rtol band)
    plans = beam_search_min(self, plans,
                attrgetter("max_total_kernel_ii"),              // tuple__102
                /*rtol=*/0.01);

    // STAGE 3 - minimise the MEMORY roofline II (rtol band)
    plans = beam_search_min(self, plans,
                attrgetter("normalized_kernel_dma_ii"),         // tuple__103
                /*rtol=*/0.01);

    return best_fusion_plan_heuristics(self, plans);           // final closed-form tie-break (§4.3)
}
```

The three `attrgetter` const tuples are byte-located in modinit: `tuple__101` → `"additional_spill_size_in_bytes"`, `tuple__102` → `"max_total_kernel_ii"`, `tuple__103` → `"normalized_kernel_dma_ii"`; both stage-2 and stage-3 load `__pyx_float_0_01` as the `rtol`. The two `CyFunction` lambdas (`lambda19` @ `0x158770` → `x.max_cc_buffer_store_dma_stride`; `lambda20` @ `0x81ac0` → `x.fuse_as_top_access`) are constructed in the attention-score sub-branch as alternative metric funcs.

**Roofline interpretation.** The beam is a strict **priority order**: avoid spill ≫ minimise compute II ≫ minimise DMA II. Each later stage only re-orders the survivors of the earlier one. The 1% rtol band keeps near-ties alive between stages, so a plan that is marginally worse on compute but much better on DMA can still win — the classic roofline tie-handling. Cython scope-var aliasing obscures where the lambda sub-ordering sits relative to the main stage keys, so that placement is HIGH.

### 4.3 `best_fusion_plan_heuristics` — the closed-form tie-break

```c
// FusionPlanGenerator.best_fusion_plan_heuristics(self, plans)  @0xb48d0  (src 1275-1286)
static PyObject *best_fusion_plan_heuristics(Self *self, PyObject *plans) {
    if (any(p->is_compute_bound for p in plans))                       // genexpr key is_compute_bound
        return max(plans, key=attrgetter("how_good_compute_bound"));   // tuple__117
    else
        return max(plans, key=attrgetter("how_good_memory_bound"));    // tuple__118
}
```

Not a beam — a single `argmax` by a precomputed "how good" roofline-goodness score, branched on whether *any* surviving plan is compute-bound. `tuple__117` → `"how_good_compute_bound"`, `tuple__118` → `"how_good_memory_bound"`, both with `_pyx_builtin_max`. `how_good_compute_bound` (`0xe8940`) returns a min-sort tuple `(generate_spill_lists?, len(batch_axes), len(lhs_free_axes), len(rhs_free_axes), len(contract_axes), -tensortensor_accumulation_flops, -memory_pressure, -total_dma_traffic_in_bytes_with_efficiency, -total_dma_traffic_in_bytes)` — the 6 negations prefer *more* fusion / *more* flops / *less* traffic / *less* pressure. `how_good_memory_bound` (`0xe7780`) has the same components, reordered to put DMA traffic first. The phrase `how_good_cost_tuple=` is present in `.rodata` as a diagnostic prefix.

---

## 5. The coefficient-free roofline cost model

### 5.1 The headline: no coefficients in the module

The Tritium roofline carries **no** hard-coded peak-FLOPs, bandwidth, or capacity magic numbers. The module's *entire* module-global double-precision constant pool is three floats plus two helpers, all dumped byte-exact from `.rodata`:

| `.rodata` off | IEEE-754 | value | role |
|---|---|---|---|
| `0x1b138`* | `3ff0000000000000` | `1.0` | `(1.0 + rtol)` band; dma_efficiencies normalization cap |
| `0x1b150` | `3f847ae147ae147b` | `0.01` | `__pyx_float_0_01` → `rtol` (the beam band) |
| `0x1b158` | `3fd0000000000000` | `0.25` | `__pyx_float_0_25` (secondary weight/margin) |
| (pool) | `bff0000000000000` | `-1.0` | int8 stride sentinel in `MemAccess.tile_int8_copy` |
| (pool) | `3f50000000000000` | `1/1024` | inside a generic `TrueDivide` helper — **not** cost |

\* the `0.01`/`0.25` pair sits byte-exact at file-offset `0x1b150`, with the `1.0` in the same pool. The VMA labels `0x1ac138`/`150`/`158` and the `rodata.bin` file-offset `0x1b150` name the same constants under two different address frames. These are the only operands of the three module-init `PyFloat_FromDouble` calls. **Every** FLOPs, byte-throughput, cycle, and capacity figure is sourced at runtime from the hardware `target` descriptor and from tensor/loopnest geometry — `0.01` and `0.25` are the *only* tunable dimensionless scalars in the whole cost model.

This makes the roofline a thin Python wrapper over the **same** per-arch latency oracle the backend PerfSim consumes — see [`perfsim-cost-model`](perfsim-cost-model.md). Where the backend cycle model calls the C++ `bir::Hwm` (`getLatency*`), Tritium calls `target.get_{matmult,memset,copy,dma}_latency(...)`, each returning a sub-object with `.get_pipeline_ii` / `.get_pipeline_latency` / `.get_latency`. Coefficients are therefore **shared via the oracle**, never duplicated; only the roofline *aggregation* is Tritium-specific. The preserved typo `get_tenortensor_sp_latency` (interned as `__pyx_n_s_get_tenortensor_sp_latency`) is a firm fingerprint of that shared oracle call surface.

### 5.2 Arithmetic intensity (the roofline x-coordinate)

```c
// FusionPlan.arithmetic_intensity(self)  @0xa5680  (src 2327)
static PyObject *arithmetic_intensity(FusionPlan *self) {
    PyObject *flops   = self->n->matmult->total_flops;       // GetAttr n->matmult->total_flops
    PyObject *traffic = self->total_dma_traffic_in_bytes;
    if (traffic == 0) return flops;                          // div-by-zero guard -> raw FLOPs
    return PyNumber_TrueDivide(flops, traffic);              // FLOPs per DMA byte
}
```

⇒ `arithmetic_intensity = matmult.total_flops / total_dma_traffic_in_bytes`. The op sequence is GetAttr `n→matmult→total_flops`, GetAttr `total_dma_traffic_in_bytes`, RichCompare vs `0`, `PyNumber_TrueDivide`. The `_with_efficiency` variant (`0xa4dd0`) divides the same numerator by the **efficiency-weighted** traffic (`total_dma_traffic_in_bytes_with_efficiency`), so a less-efficient DMA inflates the effective byte count and *lowers* intensity — the efficiency lives entirely in the denominator. Note: there is no hard-coded peak-intensity *ridge* constant — the ridge is the engine-II crossover (§5.3), itself oracle-derived.

### 5.3 The roofline core: the kernel II tuple

The actual compute-bound/memory-bound crossover is a **tuple of per-engine initiation intervals**, each sourced from the oracle, not a scalar threshold:

```c
// FusionPlan.total_kernel_ii_tuple   @0x153bc0      (per-engine IIs)
//   compute_II = matmult.loopnest_matmult_pipeline_latency
//                  = target.get_matmult_latency(...).get_pipeline_ii(...) * num_dynamic_instances
//   dve_II     = num_tensortensor_accumulations * accumulation_pipeline_ii
//                  (accumulation_pipeline_ii = target.get_tenortensor_sp_latency(...).get_latency(...))
//   memset_II  = memset_pipeline_ii = target.get_memset_latency(...).get_pipeline_ii(...)
//   dma_II     = SUM over read accesses of (dma_bytes * reload_mult) // (contract / axes_contract_factor)
// FusionPlan.normalized_kernel_ii_tuple  @0x9c8a0   = total tuple element-wise / 4 throughput divisors
//                                                     (exactly 4 x PyNumber_TrueDivide)

// FusionPlan.is_compute_bound(self)  @0x94b20  (src 2829)
static PyObject *is_compute_bound(FusionPlan *self) {
    PyObject *t = normalized_kernel_ii_tuple(self);
    // compare compute-II element vs the DMA/DVE-II elements: opcodes Py_GT x3, Py_EQ x1
    // (byte-confirmed @0x94d3c / 0x94db5 / 0x94e86 / 0x94f04)
    return (compute_II > dma_II && compute_II > dve_II && ...);   // converse for is_memory_bound @0x93cc0
}
```

⇒ a plan is **compute-bound** iff the matmult/PE II dominates the memory engines' II — the engine-balance "ridge point" test, all numbers oracle-derived. `is_compute_bound`/`is_memory_bound` reference *exactly* `{normalized_kernel_ii_tuple}` and execute the byte-confirmed `Py_GT`/`Py_EQ` tuple compare; `max_total_kernel_ii` (`0x9e020`) and `normalized_kernel_dma_ii` (`0x9c0b0`) are the scalar reductions the beam stages 2/3 key on.

### 5.4 Memory pressure, DMA latency, capacity gates

`memory_pressure` (`0x15c6f0`) = `read_memory_pressure + write_memory_pressure + matmult_group.approx_memory_pressure`. The two capacity gates are byte-confirmed RichCompare predicates against **target attributes** (no module literals):

- `overflow_sb` (`0x95980`): `memory_pressure >= matmult.sb_par_size_in_bytes` (`Py_GE`) — the SBUF per-partition byte budget. The predicate is pinned; the numeric value lives on the target/arch node, not here.
- `has_low_memory_pressure` (`0x95de0`): `memory_pressure <= matmult.low_memory_pressure_threshold` (`Py_LE`). The threshold is a *per-matmult attribute* `self.n.matmult.low_memory_pressure_threshold`, **not** a module constant; the literal `low_memory_pressure_threshold` appears as a config-key string and a local var, consistent with it being read from settings and stamped on the matmult node upstream.

`dma_latency` (`0x186150`) = `target.get_dma_latency(...).get_pipeline_latency(...)` scaled by the per-tensor reload multiplier, plus a transpose term, with two interchangeable models selected by the `use_profile_based_dma_latency` flag (analytical vs `profile_based_dma_latency_model`). `dma_efficiencies` (`0x118ae0`) builds a `{length : efficiency}` dict where each bandwidth is computed per `(length, npartitions)` from `target.get_dma_latency(...)` and normalized to the peak (cap `1.0`) — there is **no static bandwidth table**, and the only literal is the `1.0` normalization cap.

The symbolic estimation toggle `enable-symbolic-memory-pressure-estimation-tf` (CLI; `_enable_symbolic_memory_pressure_estimation_tf` interned) gates the *expensive* symbolic memory-pressure path inside `read_memory_pressure` (and `MemAccess.producer_memory_pressure`); when off, the cheap `read_memory_pressure_simple` is used. It selects *how* pressure is estimated, not whether a plan is accepted (acceptance is `overflow_sb` + `has_low_memory_pressure`).

---

## 6. The tree visualizer (`_TreeSearchVisualizer`)

A distinct, tiny Cython module renders the **outer** MCTS layout-search tree (not Tritium's beam) to Graphviz. It is the other half of Strand-L's debug surface, and is co-documented here because it is the observability companion to the autotuner. The module is exactly **two** module-level functions — no class.

> **Scope note.** The visualizer renders the MCTS tree built by the layout autotuner (8.50/8.51), whose nodes carry `best_reward`/`num_visits`. Tritium's own 3-stage beam (§4) is *not* visualized — Tritium picks a fusion plan analytically; the MCTS picks among Tritium+layout+… designs by measured latency. The two are nested.

### 6.1 `get_color` — reward → HSV gradient

```c
// get_color(x, M, m)  @0x8790  (.py lines 8-10).  pyargnames {x, M, m}.
static PyObject *get_color(PyObject *x, PyObject *M, PyObject *m) {
    // return f"{(x - m) / (M - m) / 4.0} 1.0 1.0"
    //   PyNumber_Subtract (x-m), PyNumber_Subtract (M-m), TrueDivide (ratio in [0,1]),
    //   then /4.0 (x0.25 fast path / __pyx_float_4_0), PyUnicode_Concat with kp " 1.0 1.0".
}
```

The leading-space literal `" 1.0 1.0"` is byte-located at `.rodata` `0xfe30`; the float `4.0` and the `0.25` fast-path constant are in the pool. A Graphviz "H S V" triple (all 0–1) is accepted as a colour; here `S = V = 1.0` (fully saturated, full brightness) and `H = normalized_reward / 4`, confining hue to `[0.0, 0.25]`. On the Graphviz hue wheel `0.0 = RED`, `0.25 = GREEN`, so the **worst-reward** node (`x = m`) is RED and the **best-reward** node (`x = M`) is GREEN — a monotone red→green gradient that makes the winning lineage visually obvious. `x` is fed `child.best_reward`, so the gradient tracks each node's *best* observed reward, not its mean.

### 6.2 `writeGraph` — iterative DFS → `.dot`

```c
// writeGraph(file_name, root, max_reward, min_reward)  pf @0x9900  (.py lines 13-32)
static PyObject *writeGraph(PyObject *file_name, MCTSNode *root,
                            PyObject *max_reward, PyObject *min_reward) {
    if (max_reward == min_reward) return Py_None;          // L14 guard: degenerate window -> skip
    Digraph *graph = Digraph(...);                          // `from graphviz import Digraph`
    graph.node("0", label=root->graph_label);              // root, id 0
    stack = [(root, 0)];  int child_id = 1;                 // explicit stack, NOT recursion
    while (stack) {
        (node, parent_id) = stack.pop();
        for (action, child) in node->_children.items() {   // MCTS action->child dict
            graph.node(str(child_id),
                       label=child->graph_label,            // "best: {best_reward}\nvisits: {num_visits}"
                       color=get_color(child->best_reward, max_reward, min_reward));
            graph.edge(str(parent_id), str(child_id), label=str(action)[:N]);  // edge = the action taken
            stack.append((child, child_id));
            child_id += 1;
        }
    }
    graph.render("TreeVis_" + file_name + ".dot");          // writes .dot + shells out to `dot`
}
```

The literals `"TreeVis_"` (`0xfe18`), `".dot"`, `graphviz`, `Digraph`, `render`, `graph_label`, `best_reward`, `_children`, `min_reward`, `max_reward`, `relative_perf`, `get_color` are all present in the visualizer string table. The DFS is an **explicit `stack` list**, not recursion — robust to deep MCTS trees with no Python recursion-limit risk. Node identity in the DOT is a monotonically increasing integer (`parent_id`/`child_id`), **not** Python `id()`, so dumps are deterministic and diffable across runs. The artifact path is `TreeVis_<file_name>.dot`; `graph.render()` writes the `.dot` source and (library behaviour) shells out to the Graphviz `dot` binary to produce a rendered image. That subprocess is graphviz-library behaviour rather than an explicit call in this `.so`, so it is HIGH.

> **GOTCHA — the node box carries less than it looks like it should.** It shows *only* `best_reward` and `num_visits` (the `graph_label`) — not total reward, mean reward, UCT score, or any design/plan_id list. The plan_id appears on the **edge**, as `str(action)`. There is no visit-count → node-size encoding either: visits are text, and the sole visual channel is the red→green reward colour. The `graph_label` body and the absence of any size attribute in the node call are what pin this.

---

## 7. How the three pieces compose

```text
best_fusion_plan(self, dont_split_acc_grp)            @0x123960
  -> generate_fusion_plans_for_search                 @0x188a00   (preprocess + enumerate)
       -> enumerate_fusion_plans  (THE 10-PLAN SPACE)  @0x161240   [§1-2]
  -> best_fusion_plan_search                           @0xb5720    [§3]
       -> filter !overflow_sb  (feasibility)
       -> beam_search_best_plan  (3-STAGE ROOFLINE BEAM)  @0xb6230 [§4]
            stage 1: min additional_spill_size_in_bytes  (exact-min)
            stage 2: min max_total_kernel_ii    (compute roofline, rtol=0.01)
            stage 3: min normalized_kernel_dma_ii (memory roofline, rtol=0.01)
            -> best_fusion_plan_heuristics  (closed-form tie-break) @0xb48d0
       -> (no feasible plan) noop_plan
```

The cost attributes the beam keys on (`additional_spill_size_in_bytes`, `max_total_kernel_ii`, `normalized_kernel_dma_ii`, `how_good_*`, `is_compute_bound`) are all `FusionPlan` members built from the coefficient-free roofline (§5). The beam **never** consults `PostSchedEstLatency`/`nc_latency` — Tritium's rank is a static analytic roofline, distinct from the MCTS/`_Search` empirical-latency reward of the *outer* autotuner. The chosen `FusionPlan` becomes a `TiledFusionPlan` (the codegen-facing subclass: `tile`, `fuse_producers`, `spill_tensors`, `interchange`) that the penguin middle-end then *applies* — see [`data-movement-fusion`](../penguin/data-movement-fusion.md) ("the Tritium autotuner *decides which* fusions to attempt") and [`layout-middle-end`](../penguin/layout-middle-end.md) for where the chosen fusions are materialized.

---

## 8. Confidence ledger

**CERTAIN** — the `2^3 + 2 = 10 plans in total` docstring verbatim + `0.01`/`0.25` floats byte-located at `.rodata` `0x1b150`; the 4-axis taxonomy; the 3 free bits + the `+2` reload origin (`rhs_on_top = not lhs_on_top`); all 10 `planN` addresses + `noop_plan`; the per-plan axis-getter census (plan1≡plan2, plan3≡plan4, plan5 lhs+contract, plan10 batch-only); `enumerate_fusion_plans(self, dont_split_acc_grp)` signature + the noop-first/×2-per-plan/`generate_tuple`-dedup census; `beam_search_min(self, plans, metric_func, rtol, max_metric=0)` with `rtol=0.01`, `itemgetter`, `min`, the `(1.0+rtol)` multiply and `Py_LT` filter; the 3-stage `attrgetter` tuples (`additional_spill_size_in_bytes` → `max_total_kernel_ii` → `normalized_kernel_dma_ii`); **no** `beam_width`/`heapq`/`nsmallest`/`top_k` anywhere; the coefficient-free pool `{1.0,0.01,0.25,-1.0,1/1024}`; `arithmetic_intensity = total_flops / total_dma_traffic_in_bytes`; `is_compute_bound`/`is_memory_bound` = `Py_GT`/`Py_EQ` over `normalized_kernel_ii_tuple`; `overflow_sb` (`Py_GE`) / `has_low_memory_pressure` (`Py_LE`) gates; `AutotuneFusionPlanGenerator` + `internal-autotune-tritium-use-more-tripcounts`; the visualizer `get_color`/`writeGraph` bodies + `" 1.0 1.0"`/`TreeVis_` literals.

**HIGH** — the exact `lhs_on_top` `True`/`False` byte in plan2/plan4 (semantics fixed by docstring + symmetric census); `dont_split_acc_grp` `*_wos` routing; the roofline compute>memory engine-II direction; the lambda19/20 attention sub-ordering placement; the `target`-oracle ↔ `bir::Hwm` shared-coefficient cross-check; the visualizer's `dot`-subprocess (graphviz-library behaviour).

**MEDIUM** — numeric values of `sb_par_size_in_bytes` and `low_memory_pressure_threshold` (per-arch/target attributes, not in this module); the `0.25` (`__pyx_float_0_25`) consumer mapping (a `how_good_*` weight or low-pressure margin — reached by global-name, not a body-local `movsd`); the visualizer's `relative_perf` exact expression (L25 local).

**What static analysis cannot reach.** The plan/beam/cost *structure* is reimplementation-grade. Without the arch descriptor, two things stay open: the absolute numeric of the two capacity thresholds, and the precise call-site of the `0.25` scalar. The reload-twin `lhs_on_top` byte and the attention-lambda placement are HIGH rather than byte-firm because Cython tuple-packing and scope-aliasing blur the literal positions — but the docstring plus the byte-identical plan1 ≡ plan2 census make the *semantics* unambiguous.
