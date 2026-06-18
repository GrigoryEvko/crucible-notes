# Penguin Software Pipelining (annotation-driven)

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, cp310 build of `neuronxcc/starfish/penguin/targets/transforms/SoftwarePipelineCodeGen.cpython-310-x86_64-linux-gnu.so` (ELF64, BuildID `133df136e17db950d285a3e3fce11144435e68a0`, Cython 3.0.10, `-O3 -g`, **not stripped**). The .so carries the complete Python docstrings and the worked example verbatim in `.rodata`, and every identifier in the `__pyx_n_s_` / `__pyx_kp_u_` interned pools. The cp311 and cp312 wheels ship the identical source (`SoftwarePipelineCodeGen.py`) at different ABIs; addresses below are cp310. Treat every address as version-pinned.*

## Abstract

`SoftwarePipelineCodeGen` is the **declarative** software pipeliner of the Penguin middle-end, and it is the **only** Penguin client that drives the ISL polyhedral schedule checker (cross-ref [5.17 — ISL legality](isl-schedule-tree-legality.md)). It does not search for a pipeline. It reads an **explicit user annotation** on a loop axis — either a coarse `num_stages` attribute or a fine per-child `stage_id` assignment — and *realizes* the pipeline the annotation describes: it slices the body into stages, extends the trip count to `ub + num_stages − 1`, rewrites the whole thing as **one extended loop guarded by per-stage prologue/epilogue predicates** (no physical prologue/epilogue blocks), offsets each stage's iteration index by `−stage`, widens the affected SBUF tensor allocations by a derived buffer factor, and validates the result for dependence legality via `NeuronIslDependenceAnalysis`. Its own class docstring is the confession: *"No automatic dependency analysis"* — the user owns the schedule; the pass realizes and validates it.

This is one of two software-pipelining mechanisms in the toolchain, and they are **parallel paths, not a chain**. The other — the *emergent* pipeline that the libwalrus backend builds for ordinary un-annotated tiled loops by splitting load/compute and letting the list scheduler overlap them — is covered in [Part 8 address-rotation](../walrus/address-rotation.md). A given loop takes exactly one of the two. The handoff boundary for the *annotated* path is the subject of the last section.

| | |
|---|---|
| **Module** | `SoftwarePipelineCodeGen.cpython-310` (1,799,256 B) |
| **Source** | `neuronxcc/starfish/penguin/targets/transforms/SoftwarePipelineCodeGen.py` (embedded `__FILE__`) |
| **Entry** | `transformAxis` @ `0x1e0a0` — *"Main entry point for the transformation"* |
| **Driver** | `schedule_axis_hierarchy` @ `0x38fe0` — recurses the axis tree, runs the ISL legality gate |
| **Stage resolver** | `_create_schedule_context` @ `0x306f0` — Mode A vs Mode B, builds `ScheduleContext` |
| **Schedule applier** | `schedule_software_pipeline` @ `0x21050` — extends the loop, inserts `pred_ge`/`pred_lt` |
| **ISL band builder** | `_create_software_pipeline_schedule` @ `0x47600` — `band_shift` on the ISL schedule tree |
| **Buffer sizer** | `_get_tensor_buffer_size_requirement` @ `0x2bd00` — `(max_use − min_def) + 1` |
| **Allocation widener** | `_allocate_tensor` @ `0x40820` — `block/bank_shape ×= buffer_count` (4× `imul`) |
| **Annotation it reads** | `axis.num_stages` (int > 1) **or** `child.stage_id` (+ optional `child.execution_order`) |

> **CORRECTION — two addresses in the prior method roster were swapped.** Earlier recovery notes attribute the *"extend the iteration space and add predicates"* work to `_create_software_pipeline_schedule` @ `0x47600` and the ISL/legality drive to `schedule_software_pipeline` @ `0x21050`. Reading the decompiled bodies, it is the reverse split of duties: **`schedule_software_pipeline` @ `0x21050`** is the function that holds the predicate machinery (`pred_ge`, `pred_lt`, `addPredicate`, `stealChildren`, `replaceChild`, the *"Created new loop with bounds"* and *"Processing stage"* logs), while **`_create_software_pipeline_schedule` @ `0x47600`** holds the ISL **schedule-tree** band construction (`band_shift`, `band_get_partial_schedule`, `add_band`, `add_sequence_filter`, `MultiUnionPwAff`, `from_union_pw_aff`, `get_child_domain_union_set`, `ScheduleNode`, `val_on_domain`). The ISL **legality gate** itself (`NeuronIslDependenceAnalysis`, `check_valid_schedule`, `DependenceViolation`, `DependenceType`, `is_sequential_axis`) lives in **`schedule_axis_hierarchy` @ `0x38fe0`**. All three confirmed by the `__pyx_n_s_*` reference set in each decompiled body.

## Stage-count selection — a decision, not a search

The stage count `N` is **user-supplied** through one of two mutually-exclusive annotation modes; the transform only *resolves, validates, and applies* it. There is no cost model, no II search, no recurrence/resource-MII. The resolution is done in `_create_schedule_context` (`0x306f0`, docstring *"Create scheduling context based on num\_stages attribute or stage\_id annotation"*), which builds a `ScheduleContext` object (the `__pyx_n_s_ScheduleContext` constructor is referenced at `0x306f0`) carrying the resolved stage groups.

**Mode A — coarse `num_stages` attribute** *(CONFIRMED — class docstring + identifier pool).* The loop **axis** carries an integer attribute `num_stages > 1`. The body is sliced into `num_stages` equal stage-groups **in program order**: group *k* is the *k*-th contiguous slice of the body. This is the path a NKI kernel author takes by simply declaring `num_stages` on a loop. The class docstring: *"Automatically implements software pipelining based on user-specified 'num\_stages' attribute. … Target loops must have 'num\_stages' attribute > 1. … Divides loop body into pipeline stages."*

**Mode B — fine `stage_id` per-child annotation** *(CONFIRMED — `__pyx_n_u_stage_id`, `__pyx_n_u_stage_ids` interned at `0x306f0`).* Each **child statement** of the loop carries a `stage_id` attribute; children are grouped by `stage_id`. This is the manually-scheduled path: the author assigns each instruction to a stage and may additionally supply a per-instruction `execution_order` (`__pyx_n_u_execution_order` / `_orders` at `0x306f0`) to interleave the stages globally (see *Instruction reordering* below).

```c
/* _create_schedule_context @ 0x306f0 — stage-count resolution (decision procedure).
   Cython -O3 inlined the body across the 0x306f0/0x1ef70/0x38fe0 cluster; symbols
   confirmed from the __pyx_n_u_/__pyx_kp_u_ reference set in each. */
int resolve_num_stages(Axis *axis, ScheduleContext *ctx) {
    PyObject *num_stages = PyObject_GetAttr(axis, "num_stages");   /* Mode A probe   */
    PyObject *child_stage_ids = collect_stage_ids(axis.children);  /* Mode B probe   */

    /* MUTUAL EXCLUSION — both annotation modes present is an error.
       Raised as a Python `assert` (AssertionError), NOT TypeError. */
    if (num_stages != NULL && child_stage_ids_nonempty) {
        assert(0 && "Cannot use both num_stages attribute and stage_id "
                    "annotations simultaneously");   /* __pyx_kp_u_Cannot_use_both_num_stages_attri
                                                         -> _Pyx_Raise(AssertionError) @ 0x1ef70:1017 */
    }

    int N;
    if (num_stages != NULL) {                  /* Mode A */
        N = (int)num_stages;                   /* must be > 1 (gated in _is_target_loop) */
        ctx->groups = slice_body_in_program_order(axis.body, N);
    } else {                                   /* Mode B */
        N = 1 + max_stage_id(child_stage_ids); /* must be consecutive 0..N-1, validated below */
        ctx->groups = group_children_by_stage_id(axis.children);
    }
    log_info("Chosen number of pipeline stages: %d", N);  /* __pyx_kp_u_Chosen_number_of_pipeline_stages */
    ctx->num_stages = N;
    return N;
}
```

> **CORRECTION — the "Cannot use both…" raise is an `AssertionError`, not `TypeError`.** The decompiled body at `0x1ef70:1017` raises the message via `_Pyx_Raise((PyTypeObject *)_pyx_builtin_AssertionError, __pyx_kp_u_Cannot_use_both_num_stages_attri, …)` — a plain Python `assert`. The `PyExc_TypeError` site at `0x30771` is unrelated: its disassembly (`lea rdx, "_create_schedule_context"`, format string `"%.200s() takes %.8s %zd positional argument…"`, `PyErr_Format`) is the **Cython argument-count guard** for the `_create_schedule_context` wrapper, not the mode conflict.

```text
DECISION SUMMARY — the literal answer to "how many stages?"
    N = axis.num_stages                          (Mode A)  — must be > 1
    N = 1 + max(child.stage_id over children)     (Mode B)  — must be consecutive 0..N-1
```

### Mode-B annotation validation

`_validate_stage_annotations` @ `0x34360` (docstring *"Validate that stage annotations are correct"*) and the boolean predicate `_has_valid_stage_annotations` @ `0x3f420` enforce two rules: (1) children with `stage_id` form a **consecutive** sequence starting at 0; (2) `execution_order` is **either all-`None` or consecutive 0..n**. The interned diagnostics fix the exact invariants:

- consecutive from 0 — *"…children with stages … do not form consecutive sequence from 0 to "* `<N>`
- monotonic — *"Stage IDs must be monotonically increasing, but found "* `<…>`
- order all-or-nothing — *"Mix of None and non-None execution\_orders is invalid: "* `<…>` / *"All execution\_orders are None - valid"*
- per-stage order monotonic — *"Execution orders within stage "* `<s>` *" must be monotonically increasing, but found "* `<…>`
- every instr in an ordered stage has an order — *"Execution order for instruction "* `<…>` *" is None, but should be defined"* (`__pyx_kp_u_Execution_order_for_instruction` + `__pyx_kp_u_is_None_but_should_be_defined`, both confirmed at `0x47600`)

A `disable_execution_order_check` option skips the order-consecutiveness rule.

## Target-loop gating

`_is_target_loop` @ `0x1ef70` (docstring *"Check if this is a loop we want to schedule"*) is the filter. Its disassembly is a chain of `PyObject_RichCompare` (the `num_stages > 1` test) and `PyObject_IsTrue` branches; the interned token set and confirmed `__pyx_n_s_is_canonical` / `__pyx_n_s_tripcount` references fix the gates:

| Gate | Rule | Reject diagnostic |
|---|---|---|
| g1 | `num_stages > 1` **or** valid `stage_id` children | accept: *"Target loop … with num\_stages="* `<N>` |
| g2 | `is_canonical` — non-canonical loops skipped | *"Skipping non-canonical loop "* `<axis>` |
| g3 | `is_sequential_axis` — must be a loop-carried (not parallel/spatial) axis | — |
| g4 | non-trivial `tripcount` | *"Skipping loop … with trivial tripcount "* `<n>` |
| g5 | (Mode B) **all** children annotated | *"Not all children of axis … have stage\_id annotations. Found "* `<n>` |

`schedule_axis_hierarchy` @ `0x38fe0` (docstring *"Try to schedule `root` and all nested child axes"*) recurses the axis tree, applying `_is_target_loop` at each axis and, on a hit, invoking `schedule_software_pipeline` @ `0x21050`.

> **GOTCHA — "Does not handle nested pipelining scenarios."** The class docstring states this flatly. The recursion *finds* nested axes but pipelines only the matching leaf loop; it does **not** pipeline a loop nested inside an already-pipelined loop. The recursion is a search-for-targets walk, not a nesting-aware transform.

## Buffer-count selection — the multi-buffering decision

Unlike `N` (user-supplied), the buffer count is **per-tensor and derived**. It is the producer→consumer **stage skew + 1**. Computed in `_get_tensor_buffer_size_requirement` @ `0x2bd00` (nested helper `get_def_use` @ `0x2a660`), whose docstring states it exactly:

> *"The buffer size is determined by the distance between the earliest stage where a tensor is defined and the latest stage where it's used. … Buffer size is calculated as `(latest_use_stage − earliest_def_stage + 1)`, which represents the number of iterations' worth of data that must be kept alive."*

```
buffer_count(t) = max(use_stages(t)) − min(def_stages(t)) + 1
```

**The "subtract then add-one" is present byte-for-byte in the cp310 disassembly** (this is the firmest single fact on the page). In `_get_tensor_buffer_size_requirement`:

```text
0x2d1f7 / 0x2f3e8 / 0x2f5fe : call __Pyx_GetBuiltinName     ; load `min` / `max` builtins,
                                                              ; apply to the def/use stage lists
0x2fc3c                     : mov rsi, __pyx_int_1           ; op2 = the integer literal 1
0x2fbff                     : call PyNumber_Subtract         ; max(use) − min(def)
0x2fc5c                     : call __Pyx_PyInt_AddObjC       ; … + 1   (the +1 constant loaded above)
0x2fcba                     : call PyObject_SetItem          ; store into tensor_buffer_size[t]
```

```c
/* get_def_use @ 0x2a660 + _get_tensor_buffer_size_requirement @ 0x2bd00 */
void compute_buffer_sizes(Region *pipelined, map<Tensor,int> *tensor_buffer_size) {
    for (Tensor t : pipelined.local_tensors) {
        /* WHICH TENSORS QUALIFY — confirmed diagnostics @ 0x2bd00 */
        if (!t.isNeuronLocalTensor() || t.isInputOrOutput())
            continue;   /* "<t> is not NeuronLocalTensor or isInputOrOutput, skip" */
        StageSet def_stages = stages_where_defined(t);   /* via LiveVariableAnalysis def */
        StageSet use_stages = stages_where_used(t);      /* via LiveVariableAnalysis use */
        if (def_stages.empty()) continue;   /* "<t> has no def, skip"  (kp_u_has_no_def_skip) */
        if (use_stages.empty()) continue;   /* "<t> has no use, skip"  (kp_u_has_no_use_skip) */

        int size = PyNumber_Subtract(max(use_stages), min(def_stages));  /* 0x2fbff */
        size = size + 1;                                                 /* 0x2fc5c, __pyx_int_1 */
        (*tensor_buffer_size)[t] = size;                                 /* 0x2fcba */
        /* "<t> … the buffer size needed is <size>" */
    }
}
```

**Interpretation** *(STRONG):* double/triple-buffering is an **output** of the stage assignment, not an independent knob. For the canonical `num_stages = 3` worked example, tensor `A` (defined in stage 0, used in stage 1) gets `1 − 0 + 1 = 2` (double buffering); a tensor live across two stage hops gets `3` (triple). The deeper the def→use skew, the more buffers.

> **GOTCHA — PSUM is refused here.** `_allocate_tensor` raises `NotImplementedError` on a `NeuronPSUMTensor` (the `__pyx_n_s_NeuronPSUMTensor` token is in the pool; the message says *not supported yet*). PSUM ring rotation is left entirely to the backend `address_rotation_psum` — see the handoff section.

## Prologue / steady / epilogue as predicates on one loop

This is the structural heart, and the way it differs from the backend is the headline. The backend's three regions **emerge** from the list scheduler (separate basic blocks, hoisted fill, drained tail). This pass materializes the three regions **explicitly but logically**: there is no physical prologue or epilogue code — the regions are *predicated slices of a single extended loop*. The work is done in `schedule_software_pipeline` @ `0x21050` (the function whose docstring is *"Implements software pipelining by extending the iteration space and adding predicates."*).

**Step 1 — extend the trip count.** A new axis `new_axis` with a new induction variable `new_iv` is built with upper bound `ub + (num_stages − 1)`; the old iv is rewritten to `new_iv` everywhere. The body is moved onto the new axis via the IR `stealChildren` / `replaceChild` methods (both `__pyx_n_s_stealChildren` / `_replaceChild` confirmed at `0x21050`). Log on creation: *"Created new loop with bounds ["* `<lb>` *","* `<ub'>` *"]"*.

**Step 2 — per-stage predicate insertion (= the three regions).** For each stage `s` in `0..N−1`, two affine guards are attached to that stage's instructions via the IR `addPredicate` method. The guard constructors are referenced as **module-global callables** `pred_ge` and `pred_lt` (`__pyx_n_s_pred_ge` / `__pyx_n_s_pred_lt`, both confirmed at `0x21050`, loaded via `_Pyx_GetBuiltinName` / `_Pyx__GetModuleGlobalName`):

```c
/* schedule_software_pipeline @ 0x21050 — predicate machinery (decompiled symbol order) */
Axis *new_axis = make_axis(lb, ub + (N - 1));         /* Step 1: extended bound        */
old_iv->replaceUseOfWith(new_iv);                     /* rewrite iv everywhere          */
log_info("Created new loop with bounds [%d,%d]", lb, ub + N - 1);

for (int s = 0; s < N; ++s) {                          /* Step 2: per-stage predicates  */
    log_info("Processing stage %d", s);                /* __pyx_kp_u_Processing_stage    */
    Stmt *stage_stmts = ctx->groups[s];

    /* PROLOGUE guard: stage s does nothing until iteration s (the pipe is filling). */
    Predicate g_lo = pred_ge(new_iv, s);               /* new_iv >= s        @ 0x21050:2317 */
    /* EPILOGUE guard: stage s stops once its source iterations run out (draining). */
    Predicate g_hi = pred_lt(new_iv, ub + s);          /* new_iv <  ub + s   @ 0x21050:2545 */

    for (Inst *i : stage_stmts) {
        adjust_indices(i, /*shift=*/s);                /* Step 3: index offset, below      */
        i->addPredicate(g_lo);                         /* @ 0x21050:2969 (__pyx_n_s_addPredicate) */
        i->addPredicate(g_hi);
        add_attr(i, "software_pipelined", /*stage=*/s);/* _add_attr @ 0x292e0               */
    }
}
log_info("Applied software pipelining with num_stages=%d", N);  /* __pyx_kp_u_Applied_software_pipelining_with */
```

The three **regions** are the intersection pattern of these guards over `s`:

```text
  iteration:   0 .. N-2        N-1 .. ub-1          ub .. ub+N-2
               PROLOGUE        STEADY               EPILOGUE
               (filling)       (fully overlapped)   (draining)
  stage 0:     fires           fires                stopped   (g_hi: new_iv < ub  fails)
  stage 1:     waits 1 iter    fires                stopped
  ...
  stage N-1:   waits N-1 iters fires                fires until ub+N-2
```

This matches the embedded `num_stages = 3` worked example exactly:

```text
  stage 0 (producer):  epilogue only      ->  i < ub          (pred_lt only fires)
  stage 1 (middle):    prologue+epilogue   ->  i >= 1 and i < ub+1
  stage 2 (consumer):  prologue only       ->  i >= 2          (pred_ge only fires)
```

**Step 3 — per-stage index offset.** Stage `s` reads/writes iteration `new_iv − s`. Done by `_adjust_indices` @ `0x1a720` (docstring *"Recursively adjust indices in a statement or block. Replace uses of old\_iv with new\_iv"*), a recursive tree-rewriter (the disassembly is ~72 method calls of stmt/block descent) that offsets the iteration variable by the stage position using the IR `replaceUseOfWith` method. `_add_attr` @ `0x292e0` then tags each emitted statement with the `software_pipelined` attribute and its `stage` (docstring: *"Add attributes to the statement to indicate it belongs to a software pipelined stage … for debugging and analysis purposes"*).

> **NOTE — the structuring is purely predicative at the Penguin level.** After this pass the loop is *still one loop*, just with a longer trip count and predicated bodies. A correct (if naively-buffered) software pipeline already exists in the IR. The physical three-block split is never emitted here; that is the backend's job ([Part 8](../walrus/separate-load-prefetch.md)).

### The parallel ISL schedule-tree (band shift)

In parallel with the predicate form, `_create_software_pipeline_schedule` @ `0x47600` expresses the same pipeline as a **band shift on the ISL schedule tree**. Its confirmed call surface — `get_child_domain_union_set`, `from_union_pw_aff`, `val_on_domain`, `MultiUnionPwAff`, `UnionPwAff`, `band_get_partial_schedule`, `band_shift`, `add_band`, `add_sequence_filter`, `get_schedule`, `ScheduleNode` — is the polyhedral encoding of the per-stage `i − s` offset, built over stock `islpy` (`__pyx_n_s_isl` / `_islpy` referenced module-wide). This schedule object is what the legality gate then checks.

## Dependence analysis — what it reads, and the legality gate

The picture is two-sided and subtle.

**(i) Stage assignment is NOT dependence-derived** *(CONFIRMED — class docstring "No automatic dependency analysis").* No modulo schedule, no II search, no MII. The user owns which instruction is in which stage (the `num_stages` slice or the `stage_id` map).

**(ii) Legality IS dependence-checked, via a full ISL polyhedral layer** — and this pass is the *only* Penguin client that drives it (cross-ref [5.17 — ISL legality](isl-schedule-tree-legality.md)). The gate lives in `schedule_axis_hierarchy` @ `0x38fe0`, whose decompiled body references `NeuronIslDependenceAnalysis` (the ISL dependence-graph builder, imported at module init — 6× in `pymod_exec`), `LiveVariableAnalysis` (def/use liveness feeding `get_def_use`), `is_sequential_axis`, `check_valid_schedule` (the final legality gate), `DependenceType` and `DependenceViolation` (the verdict). On a violating reorder/shift the pass reports the interned fragments *"checked dependences between Axis of size "* `<…>`, *"<…> causes dependencies to be violated:"* `<edges>`, *"found first violation in "* `<…>`.

```c
/* schedule_axis_hierarchy @ 0x38fe0 — recurse + legality gate (symbol-confirmed surface) */
void schedule_axis_hierarchy(Axis *root) {
    for (Axis *axis : walk(root)) {
        if (!_is_target_loop(axis)) continue;            /* §gating, 0x1ef70           */
        if (!axis.is_sequential_axis()) continue;        /* g3                          */

        DepGraph deps = NeuronIslDependenceAnalysis(axis);  /* imported @ pymod_exec    */
        Liveness  lv  = LiveVariableAnalysis(axis);

        ScheduleContext ctx = _create_schedule_context(axis);          /* resolve N      */
        Schedule sched = schedule_software_pipeline(axis, ctx);        /* 0x21050: apply */

        /* THE GATE: the predicated/shifted schedule must respect FLOW/ANTI/OUTPUT deps */
        if (!check_valid_schedule(sched, deps)) {        /* DependenceType verdict       */
            report_DependenceViolation(deps.first_violation());
            /* "… causes dependencies to be violated:" -> reject this reorder/shift */
        }
        /* else commit: the user's stage assignment is TRUSTED for scheduling,
           VERIFIED for legality. */
    }
}
```

**What deps feed a *number* vs a *yes/no*:** `get_def_use` @ `0x2a660` reads, per `NeuronLocalTensor`, the DEF and USE stage-sets (backed by `LiveVariableAnalysis` + the IR `isInputOrOutput` query). Those are the **only** deps that feed a numeric decision in this pass — they drive the `buffer_count` formula. The ISL deps feed **only** the legality yes/no; they never alter `N` or the buffer counts.

## Instruction reordering and allocation widening

**`_reorder_instructions` @ `0x267c0`** *(CONFIRMED docstring).* In Mode B with `execution_order` set, this interleaves instructions across stages into the user's requested global order — the mechanism that lets a manual pipeline place `load(i+2)` ahead of `compute(i)`. Docstring: *"Reorder instructions in the loop body based on execution\_order. groups: instruction groups per stage; execution\_orders: per-group orders (None = keep original position)."* When all orders are `None` it logs *"No execution\_order specified, keeping original stage order"*.

**`allocate` @ `0x253f0` + `_allocate_tensor` @ `0x40820`** *(CONFIRMED).* `allocate` *"identifies which tensors require buffering and expands their allocated memory along the pipeline axis by the calculated buffer factor."* `_allocate_tensor` does the widen. The mechanic is confirmed firsthand: **four `imul rdx, rax`** at `0x40fe0`, `0x412f0`, `0x41508`, `0x41d4b` — the block-shape × `buffer_count` multiply along the pipeline-axis dimension.

```c
/* _allocate_tensor @ 0x40820 — block/bank shape ×= buffer_count along the pipeline axis */
void _allocate_tensor(Tensor *t, int dim, int buffer_count) {
    if (t->isNeuronPSUMTensor())
        raise NotImplementedError;                       /* PSUM refused here */
    assert(t->allocated_block_shape[dim] == 1);          /* "<t> … block/bank shape but should be all 1s" */

    t->allocated_block_shape[dim] *= buffer_count;       /* imul @ 0x40fe0 / 0x412f0 */
    t->allocated_bank_shape[dim]  *= buffer_count;       /* imul @ 0x41508 / 0x41d4b */
    log_info("Updated allocated_block_shape[%d] … with new allocation", dim);
    log_info("Updated allocated_bank_shape[%d]", dim);

    t->resetAllocation();                                /* re-align via start_bank /      */
    realign(t, required_local_tensor_align_in_bytes);    /* required_local_tensor_align…   */
}
```

The auto path is gated by `disable_auto_buffer`; when enabled it logs *"Automatically allocating buffers for tensors in "* `<axis>` and *"Allocated tensor "* `<t>` *" with buffer size "* `<n>`.

> **GOTCHA — mutual exclusion with `multi_buffer`.** The `allocate` docstring states this widening *"should not be used with multi\_buffer at the same time, and is implicitly done"*. The widening is the same logical job as the separate `multi_buffer` mechanism — whichever runs, the SB tensor ends up `N`-wide along the pipeline axis. Running both on one tensor double-widens it.

## The Penguin ↔ backend handoff boundary

This is the headline result, and it is the most-misread part. The annotation-driven pass (here) and the backend's emergent pipeline are **parallel paths sharing no plan object**.

**The two paths are parallel, not serial.** There is no data structure that `SoftwarePipelineCodeGen` writes and that the backend `separate_load_and_compute` then reads as "the pipeline plan." They are two different *entry conditions*:

- **Penguin path** fires **iff a loop axis is annotated** (`num_stages > 1` / `stage_id`). Runs on Penguin IR, *before* BIR lowering / libwalrus.
- **Backend path** fires for **ordinary tiled loops with no annotation** — the unroller + list scheduler + `address_rotation` produce pipelining as an *emergent* property; there is no monolithic backend pipeliner pass.

A given loop takes exactly one. The "upstream heuristic that picks N" referenced by the backend is the `HeuristicUnroll` + prefetch-depth choice for the *emergent* path — **not** this pass. `SoftwarePipelineCodeGen`'s `N` is not a heuristic at all; it is the literal user annotation.

**What the Penguin level decides (this pass owns):**

- `N` = the stage count (from the annotation).
- The stage→instruction assignment (which inst in which stage).
- `buffer_count(t) = max_use_stage − min_def_stage + 1` per local tensor.
- The **logical** pipeline: one extended loop (`ub + N − 1`) with per-stage prologue/epilogue predicates and index offset `i − s`. Complete and self-contained at the Penguin-IR level.
- The SB-tensor **allocation widening**: `allocated_block/bank_shape ×= buffer_count` along the pipeline axis — pre-committing the multi-buffer *space*.
- **Legality**: the ISL dep-check that the predicated schedule respects FLOW/ANTI/OUTPUT deps.

**What the backend realizes (libwalrus owns):**

- **PSUM multi-buffering** — this pass explicitly refused it (`NotImplementedError`). PSUM banking is rotated by `address_rotation_psum` using `pelican::ModuloExpr` (addr = `base + (iv mod N)·stride`, `denom = N` = the ring size = the stage count). So: **Penguin decides N; backend realizes the N-deep PSUM ring** ([Part 7 ModuloExpr](../bir/pelican-moduleexpr.md)).
- The **physical** three-region split for the *emergent* path (separate blocks, hoisted fill, drained tail). For the *annotated* path the regions are already predicated here and stay one loop; the backend list scheduler still overlaps the predicated bodies across engines.
- **Physical buffer colouring** — `coloring_allocator_{psum,sb,dram}` assign one physical slot per logical buffer; `address_rotation_sb` commits the SB ring. The Penguin widening reserves the space; the backend rotation hands successive iterations *distinct* slots in it.

**The one shared contract (the actual handoff artifact)** is **not** a "pipeline object" — it is the **BIR-level memory shape plus the per-iteration index expression**:

1. `SoftwarePipelineCodeGen` widens the SB tensor to `N` copies along the axis and offsets each stage's index by `i − s`. When this lowers to BIR, the tensor is *already* `N`-wide and the accesses are *already* iteration-skewed.
2. The backend `address_rotation` sees an `N`-wide tensor whose accesses cycle through the axis, rebinds them to physical `(iv mod N)` slots, and emits the `ModuloExpr` (cross-ref [Part 8 address-rotation](../walrus/address-rotation.md)).

> **The boundary in one line.** PENGUIN decides `N`, the stage assignment, the predicated logical pipeline, and reserves `N`-wide SB space; the BACKEND turns `N`-wide-logical into `N`-distinct-physical (`ModuloExpr` ring + colouring) and — for the *non*-annotated loops — also invents the pipeline from scratch.

## Method roster (cp310)

| Method | Addr | Role |
|---|---|---|
| `__init__` | `0x1bd60` | ctor (LogContext, options) |
| `_adjust_indices` | `0x1a720` | recursive `i → i − s` rewrite (`replaceUseOfWith`) |
| `transformAxis` | `0x1e0a0` | per-axis entry driver |
| `_is_target_loop` | `0x1ef70` | gate: canonical / sequential / `N>1` / tripcount; holds the inlined mode-conflict assert |
| `schedule_software_pipeline` | `0x21050` | **extend bound + insert `pred_ge`/`pred_lt` + `addPredicate`** |
| `allocate` | `0x253f0` | widen buffered tensors |
| `_reorder_instructions` | `0x267c0` | interleave by `execution_order` |
| `_add_attr` | `0x292e0` | tag stmt `software_pipelined` + `stage` |
| `get_def_use` | `0x2a660` | collect per-tensor DEF/USE stage-sets |
| `_get_tensor_buffer_size_requirement` | `0x2bd00` | `(max_use − min_def) + 1` |
| `_create_schedule_context` | `0x306f0` | resolve N / Mode A\|B; build `ScheduleContext` |
| `_validate_stage_annotations` | `0x34360` | consecutive / monotonic |
| `schedule_axis_hierarchy` | `0x38fe0` | recurse axis tree; **ISL legality gate** |
| `_has_valid_stage_annotations` | `0x3f420` | bool of validation |
| `_allocate_tensor` | `0x40820` | `shape ×= factor` (4× `imul`); PSUM `NotImplementedError` |
| `_create_software_pipeline_schedule` | `0x47600` | **ISL schedule-tree band shift** |

Imported IR/ISL surface (interned import strings): `neuronxcc.starfish.penguin.ir.ir` / `.Axis` / `.Stmt`; `…targets.tonga.TongaTensor` / `.TongaISAInst`; `…targets.transforms.TargetLowering` (base pass), `.TongaLiveInterval`, `.experimental.TongaIslDependenceAnalysis`; runtime imports `NeuronIslDependenceAnalysis`, `LiveVariableAnalysis`; `neuronxcc.starfish.support.LogContext`. IR methods used: `stealChildren`, `replaceChild`, `replaceUseOfWith`, `addPredicate`, `resetAllocation`, `isInputOrOutput`, `get/set_attrs_dict`, `get_tuple_name`.

## Adversarial self-verification

The five strongest claims, re-challenged against the binary:

1. **`buffer_count = (max_use − min_def) + 1`** — *holds.* `_PyNumber_Subtract` @ `0x2fbff`, then `__pyx_int_1` loaded as op2 @ `0x2fc3c`, then `__Pyx_PyInt_AddObjC` @ `0x2fc5c`, then `_PyObject_SetItem` @ `0x2fcba`, all in the `0x2bd00` SoftwarePipelineCodeGen disasm. Byte-for-byte.
2. **`_allocate_tensor` widens shape by `buffer_count` (4× `imul`)** — *holds.* `imul rdx, rax` at `0x40fe0`, `0x412f0`, `0x41508`, `0x41d4b` in the `0x40820` disasm, paired with the `Updated_allocated_{block,bank}_shape` log strings.
3. **The mode-conflict raise is an `AssertionError`, not `TypeError`** — *corrected and holds.* `_Pyx_Raise(_pyx_builtin_AssertionError, __pyx_kp_u_Cannot_use_both_num_stages_attri, …)` at `0x1ef70:1017`; the `PyExc_TypeError` @ `0x30771` is the Cython arg-count guard (format string `"%.200s() takes %.8s %zd positional argument…"`), a different site.
4. **The predicate machinery lives in `schedule_software_pipeline` @ `0x21050`, the ISL band in `_create_software_pipeline_schedule` @ `0x47600`** — *corrected and holds.* `pred_ge`/`pred_lt`/`addPredicate`/`stealChildren`/`Created_new_loop_with_bounds`/`Processing_stage` are all in the `0x21050` body; `band_shift`/`band_get_partial_schedule`/`MultiUnionPwAff`/`add_sequence_filter` are all in the `0x47600` body. The prior roster swapped their docstring duties.
5. **The ISL legality gate (`NeuronIslDependenceAnalysis`, `check_valid_schedule`, `DependenceViolation`) is in `schedule_axis_hierarchy` @ `0x38fe0`** — *corrected and holds.* `__pyx_n_s_NeuronIslDependenceAnalysis`, `_check_valid_schedule`, `_DependenceViolation`, `_DependenceType`, `_is_sequential_axis` all resolve to the `0x38fe0` decompiled body (and `NeuronIslDependenceAnalysis`/`LiveVariableAnalysis` are imported 6× each at `pymod_exec`).

**Tagged as INFERRED / not byte-walked:** the exact `AffinePredicate` immediate per stage (`pred_ge = i − s ≥ 0`, `pred_lt = i − (ub + s) < 0`) is STRONG from the worked example + `pred_ge`/`pred_lt` naming, but the affine call args are hidden behind Cython's vectorcall and were not reduced to a single literal. Whether `_reorder_instructions` runs before or after predicate insertion inside the apply step is INFERRED (both are present; ordering not forced by the disasm). The `ScheduleContext` field layout beyond the recovered locals (`stage_children`, `stage_accesses`, `stage_units`, `group_sizes`, `stage_config`, `stage_distance`, `num_stages`) is not fully enumerated. The ISL dependence-graph *construction* detail is delegated to `NeuronIslDependenceAnalysis` / `TongaIslDependenceAnalysis` (separate modules — only the call surface is recovered here).
